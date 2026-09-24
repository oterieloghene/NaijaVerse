"""Delta State keke (tricycle) transport — keke_notes.md, 21st-pass spec.

Commands
--------
!keke <codename>    Board the keke at your current stop, heading for the
                    codename's destination (keke_notes 1.3, 1.9, 1.10).
!buy-keke <zone>    Delta Commissioner of Commerce buys a state keke out of
                    the Delta Treasury (1.1, Q5).

Engine
------
The bot drives every purchased keke in a continuous to-and-fro loop over its
route's ACTUAL stops (17-stop spine, exempt channels jumped over). At each
stop the keke posts an ARRIVAL block, deletes it after ARRIVAL_BLOCK_SECONDS and
replaces it with a "departing in N secs..." block (edited live every
NOTICE_REFRESH_SECONDS) that stays until the STOP_SECONDS dwell is over, then burns the segment's fuel and drives
MOVE_SECONDS to the next stop. No two kekes are ever in the same channel: a
keke reserves the next stop before driving to it and waits (parked) while
another keke is there or heading there. Two kekes facing each other on
adjacent stops swap places, so they can never block each other. Route ends are turnarounds (direction flips).

Fares are checked against the player's cash at hand at BOARD time and
deducted (plus state treasury credit) at DROP-OFF (Q3). Exception drop-off:
when the keke's fuel runs out mid-run, every passenger is dropped at the
keke's current stop and told to trek the rest (message 5).
"""

import asyncio
import logging
import math
import re
from collections import defaultdict

import discord
from discord.ext import commands

import bank_config as cfg
import database
import keke_config as kc
import keke_database as kdb
import location_permissions as perms
import locations
from bank_database import BankError
from bank_messages import announce_transaction
from locations import LOCATIONS

log = logging.getLogger("nvv.keke")

STATE = "Delta"

# Refuelling is deferred, so for now every bot (re)start refills all kekes to a
# full tank. Set to False once real refuelling exists.
RESET_FUEL_ON_DEPLOY = True
COMMISSIONER_ROLES = ["Delta Commissioner of Commerce"]

# A purchased keke's zone letter is the FIRST letter of the route it runs:
# zone A -> route AB (North<->Central), zone B -> BC, zone C -> CA.
ZONE_ROUTE = {"A": "AB", "B": "BC", "C": "CA"}

# --- verbatim custom messages (keke_notes 1.10 — character-for-character) ---
MSG_CANT_AFFORD = "Oga/Madam, your money nor reach, use leg waka. 😂🚶🏾"
MSG_WRONG_ROUTE_MULTI = "i nor dy go abeg! This keke dey go {route} 🛺💨"
MSG_WRONG_ROUTE_SINGLE = "I nor dy go that area oga/madam 🛺💨"
MSG_GATE_PASS = "Not accessible❗You need gate pass 🪪"
MSG_INVALID_DEST = "invalid destination ❌"
MSG_NO_KEKE_HERE = "No keke here, Wait for Keke!🤦"
MSG_KEKE_FULL = "Make i lap you? 😂 🛺 💨"
MSG_NO_PLAYER_RECORD = "Not authorised! Meet @immigrationofficer for help❗"

# Two blocks per stop (message 3). ARRIVAL is posted when the keke pulls in and
# is deleted after ARRIVAL_BLOCK_SECONDS; it is immediately replaced by a
# "DEPARTING IN N SECS..." block that stays for the rest of the STOP_SECONDS
# dwell and is deleted when the keke leaves.
ARRIVAL_BLOCK_SECONDS = 5
# The departing block is edited every NOTICE_REFRESH_SECONDS to show the time left.
NOTICE_REFRESH_SECONDS = 15
# A keke whose next stop is taken re-checks every WAIT_POLL_SECONDS.
WAIT_POLL_SECONDS = 1
# The traffic message is posted once a keke has been held up this long (a hold-up
# of a second or two is not worth a message); it is deleted when the keke moves.
TRAFFIC_MESSAGE_AFTER_SECONDS = 5

STOP_BLOCK = """━━━━━━━━━━━━━━━━━━━━
🛺 KEKE {title}
From: {from_zone}
To: {to_zone}
Passengers: {count}/{capacity}
Passengers on Board:
{pax}
Status: 🟢 {status}
Estimated Arrival: {eta}
━━━━━━━━━━━━━━━━━━━━"""

# Left in the keke's final parking channel when it is stopped (!keke-stop) or
# runs out of fuel. It is NOT auto-deleted: it stays until the keke is back in
# service (!keke-start, or a refuel / refill).
PARKED_BLOCK = """━━━━━━━━━━━━━━━━━━━━
🛺 KEKE PARKED
From: {from_zone}
To: {to_zone}
⛽: {fuel}
Status: 🔴 Not in Service
━━━━━━━━━━━━━━━━━━━━"""

MSG_BOARDED = "@player sitdown well o, we go soon move o"
MSG_TRAFFIC = "🚦 traffic dey today o 😩"

MSG_DROPPED = "@player you don reach o. Your money na ₦{fare}, Thank you o 😊💸"
MSG_EXCEPTION_DROPOFF = (
    "@player, tax force nor gree us reach {destination}. "
    "Come down here {dropoff} trek. Nor vex 😭🚶🏾\n"
    "Your money na {fare}."
)
MSG_FUEL_EMPTY = "Make ona nor vex, fuel don finish ⛽ {passengers}"


def _fmt_fuel(liters):
    return f"{float(liters):.2f} L"


async def _clear_parked(bot, keke_id):
    """Delete the keke's KEKE PARKED block (if it has one) and forget it. Called
    when the keke goes back into service. Returns (stop_code, direction): where it
    was parked and which way it was heading, i.e. how it must re-enter the road
    ((None, None) if it had no block)."""
    try:
        row = await kdb.pop_parked(keke_id)
    except Exception:
        log.exception("keke %s: could not read parked block record", keke_id)
        return None, None
    if row is None:
        return None, None
    channel = bot.get_channel(row["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(row["channel_id"])
        except discord.HTTPException:
            return row["stop_code"], row["direction"]
    try:
        await channel.get_partial_message(row["message_id"]).delete()
    except discord.HTTPException:
        pass  # already gone
    return row["stop_code"], row["direction"]


def _route_name(route):
    return kc.ROUTES[route]["name"]


def _zone_fullname(letter):
    return f"Delta {kc.ZONES[letter]}"


def _is_on_route(stop_code, route):
    return stop_code in kc.ROUTES[route]["stop_codes"]


async def _fetch_member(channel, member_id):
    """The guild Member (not just a User) for `member_id` in the guild of
    `channel` — needed for roles/permissions. None if they can't be found."""
    if channel is None:
        return None
    guild = channel.guild
    member = guild.get_member(member_id)
    if member is None:
        try:
            member = await guild.fetch_member(member_id)
        except discord.HTTPException:
            member = None
    return member


async def _send_quiet(channel, content):
    """Post a keke status message without notifying anyone: Discord's @silent
    flag (no push/desktop notification for the channel) and no mention pings,
    even though the text may name passengers. Messages addressed to one player
    (boarding, drop-off) do NOT use this — they are meant to ping."""
    quiet = discord.AllowedMentions.none()
    try:
        return await channel.send(content, silent=True, allowed_mentions=quiet)
    except TypeError:
        # discord.py older than 2.2 has no `silent`; still suppress the mentions.
        return await channel.send(content, allowed_mentions=quiet)


async def _unlock_write(channel, member):
    """Remove the personal send-deny the keke placed on the departure channel at
    boarding. Mirrors location_permissions._apply: drop the overwrite entirely
    when nothing else is left in it (a personal Read Message History allow, for
    example, is kept). Returns True when the lock is gone or was never there,
    False when it could not be lifted."""
    if channel is None or member is None:
        return False
    try:
        overwrite = channel.overwrites_for(member)
        if overwrite.send_messages is not False:
            return True
        overwrite.send_messages = None
        if overwrite.is_empty():
            await channel.set_permissions(member, overwrite=None, reason="Keke drop-off")
        else:
            await channel.set_permissions(member, overwrite=overwrite, reason="Keke drop-off")
        return True
    except discord.HTTPException:
        log.exception("keke: could not lift boarding lock on #%s for %s", channel, member)
        return False


def _stop_zone(stop_code, route=None):
    """True zone letter of a stop (A/B/C) by its spine position (notes 2):
    A = North (stops 0-3), B = Central (stops 4-8), C = South (stops 9-16).
    The zone of a stop is a fact about the stop, not about which route the
    keke happens to be on — a C<->A keke still passes Delta Central."""
    index = kc.STOP_INDEX[stop_code]
    if index < 4:
        return "A"
    if index < 9:
        return "B"
    return "C"


def _effective_destination(word):
    """
    Where the passenger actually gets dropped: the codename's parent channel,
    or the zone HUB when the destination is an exempt channel the keke jumps
    over (1.6 / 21st pass). `word` is the bare codename word typed after
    `!keke`. Returns (category_code, location_code, parent_name,
    is_hub_dropoff), or None for an unknown codename.
    """
    dest = kc.codename_dest(f"!keke {word}")
    if dest is None:
        return None
    cat, loc, parent = dest
    if word in kc.EXEMPT_CODES:
        # Exempt channels are not stops, so take the zone straight from the
        # config map (notes 2) instead of spine position.
        zone = kc.EXEMPT_ZONES[word]
        hub = kc.ZONE_HUB[zone]
        hub_cat, hub_loc, hub_parent = kc.codename_dest_by_code(hub)
        return (hub_cat, hub_loc, hub_parent, True)
    return (cat, loc, parent, False)


class KekeUnit:
    """Runtime state of one purchased keke and its loop task."""

    def __init__(self, row, channel_map, fleet=None):
        self.keke_id = row["keke_id"]
        self.zone = row["zone"]
        self.route = ZONE_ROUTE[self.zone]
        self.bot = None
        self.channel_map = channel_map
        # Every keke on the road (the cog's {keke_id: KekeUnit}), so a unit can
        # see who is where. Shared by reference, never copied.
        self.fleet = fleet if fleet is not None else {}
        self._moving_to = None   # stop index reserved/being driven to, else None
        self._want = None        # stop index this keke is waiting to enter
        self.passengers = []  # list of dicts: {member_id, player_id, name,
        #                                       destination, fare, hub_dropoff}
        self.task = None
        self._loop_stop = asyncio.Event()
        self._notice = None      # the live "departing in" message, if any
        self._depart_at = 0.0
        # Set by !keke-stop when the keke still has passengers: it finishes the
        # trip (drops everyone at their destination), then pulls in and parks.
        self.stopping = False

    @property
    def direction(self):
        """+1 = north->south (towards route end), -1 = south->north."""
        return self._dir

    @property
    def running(self):
        """True while the keke is on the road (a stranded keke is out of
        service: it never blocks a stop and cannot be boarded)."""
        return self.task is not None and not self._loop_stop.is_set()

    def stop_channel_id(self):
        """Channel ID of the stop the keke is parked at, or None (also None
        while it is driving between stops or out of service)."""
        if not self.running or self._moving_to is not None:
            return None
        channel = self.channel_map.get(self.stop_code)
        return channel.id if channel is not None else None

    # ------------------------------------------------------------------
    # One keke per channel
    # ------------------------------------------------------------------

    def _claimant(self, index):
        """The other running keke that holds stop `index` — parked there or
        driving into it — or None if the stop is free."""
        for other in self.fleet.values():
            if other is self or not other.running:
                continue
            if other._moving_to == index:
                return other
            if other._moving_to is None and other._stop == index:
                return other
        return None

    def _try_reserve(self, target):
        """Atomically (no awaits) claim `target` for this keke. Returns True on
        success. If the keke parked on `target` is waiting to enter OUR stop
        (a head-on pair), swap: both are granted at the same instant, so
        neither is ever in the other's channel and neither can block the other."""
        other = self._claimant(target)
        if other is None:
            self._moving_to = target
            return True
        if other._moving_to is None and other._want == self._stop:
            self._moving_to = target
            other._moving_to = self._stop
            return True
        return False

    async def _await_clear(self, target):
        """Wait, parked, until the next stop is free (or we are swapped in),
        then hold the reservation in self._moving_to."""
        self._want = target
        loop = asyncio.get_running_loop()
        blocked_since = None
        traffic_posted = False
        traffic_msg = None
        try:
            while True:
                if self._moving_to is not None:   # a head-on partner swapped with us
                    return
                if self._try_reserve(target):
                    return
                now = loop.time()
                if blocked_since is None:
                    blocked_since = now
                if not traffic_posted and now - blocked_since >= TRAFFIC_MESSAGE_AFTER_SECONDS:
                    traffic_posted = True
                    traffic_msg = await self._post_traffic()
                await self._sleep(WAIT_POLL_SECONDS)
        finally:
            self._want = None
            await self._delete_msg(traffic_msg)   # traffic cleared: message goes

    async def _post_traffic(self):
        channel = self.channel_map.get(self.stop_code)
        if channel is None:
            return None
        try:
            return await _send_quiet(channel, MSG_TRAFFIC)
        except discord.HTTPException:
            log.exception("keke %s could not post traffic message", self.keke_id)
            return None

    def _pick_start(self):
        """Where this keke enters the road. The route's own start if it is
        free; otherwise the free stop on its route that is furthest from the
        kekes already running, so kekes are spread out from the beginning."""
        route = kc.ROUTES[self.route]
        lo, hi = route["start"], route["end"]
        if self._claimant(lo) is None:
            return lo
        free = [i for i in range(lo, hi + 1) if self._claimant(i) is None]
        if not free:
            return lo
        others = [o._stop for o in self.fleet.values() if o is not self and o.running]
        return max(free, key=lambda i: (min(abs(i - t) for t in others), -i))

    def spawn(self, bot, start_code=None, direction=None):
        """Put the keke on the road. `start_code` is the stop it was parked at and
        `direction` the way it was heading: it re-enters exactly there, that way
        (if no other keke is in that channel)."""
        self.bot = bot
        self._loop_stop.clear()
        self._moving_to = None
        self._want = None
        self.stopping = False
        route = kc.ROUTES[self.route]
        idx = kc.STOP_INDEX.get(start_code) if start_code else None
        if idx is not None and route["start"] <= idx <= route["end"] and self._claimant(idx) is None:
            self._stop = idx
            heading = direction if direction in (1, -1) else 1
        else:
            self._stop = self._pick_start()
            heading = 1
        self._dir = heading
        self.task = asyncio.create_task(self._run())

    @property
    def stop_code(self):
        return kc.STOP_CODES[self._stop]

    @property
    def at_turnaround_end(self):
        return self._stop == kc.ROUTES[self.route]["end"]

    async def _run(self):
        try:
            while not self._loop_stop.is_set():
                await self._tick_stop()
                if self._loop_stop.is_set():
                    break
                await self._tick_move()
        except Exception:
            log.exception("keke %s loop crashed", self.keke_id)
        finally:
            self.task = None
            self._moving_to = None
            self._want = None

    def _next_stop(self):
        if self._dir > 0:
            if self._stop >= kc.ROUTES[self.route]["end"]:
                self._dir = -1
                return self._stop - 1
            return self._stop + 1
        if self._stop <= kc.ROUTES[self.route]["start"]:
            self._dir = 1
            return self._stop + 1
        return self._stop - 1

    async def _tick_stop(self):
        """Boarding wait + arrival/departure block at the current stop."""
        channel = self.channel_map.get(self.stop_code)
        # Arrivals: drop everyone whose destination is THIS stop (1.5: fare
        # is deducted at arrival; 1.8: only passengers heading here get off).
        for p in [p for p in self.passengers if p["location_code"] == self.stop_code]:
            self.passengers.remove(p)
            await self._drop_passenger(p, channel, paid=True)
        # !keke-stop was issued and the last passenger has just got down: the
        # trip is complete, so pull in here and park (no arrival/departing block).
        if self.stopping and not self.passengers:
            await self._finish_stop()
            return
        arrival_secs = min(ARRIVAL_BLOCK_SECONDS, kc.STOP_SECONDS)
        wait_secs = kc.STOP_SECONDS - arrival_secs
        # 1) ARRIVAL block, then delete it and switch to the departing block.
        arrival_msg = await self._send_block("ARRIVAL", "Arrived")
        try:
            await self._sleep(arrival_secs)
        finally:
            await self._delete_msg(arrival_msg)
        if wait_secs <= 0:
            return
        # 2) "Departing in N secs..." block for the rest of the dwell; deleted
        #    when the delay is over (i.e. as the keke leaves).
        self._depart_at = asyncio.get_running_loop().time() + wait_secs
        self._notice = await self._send_block(self._departing_title(), "Departing")
        try:
            loop = asyncio.get_running_loop()
            while True:
                left = self._depart_at - loop.time()
                if left <= 0:
                    break
                await self._sleep(min(NOTICE_REFRESH_SECONDS, left))
                # Live countdown: edit the block while there is still time left.
                if self._depart_at - loop.time() > 0.5:
                    await self.refresh_notice()
        finally:
            notice, self._notice = self._notice, None
            await self._delete_msg(notice)

    def _departing_title(self):
        left = max(1, math.ceil(self._depart_at - asyncio.get_running_loop().time()))
        return f"DEPARTING IN {left} SEC{'S' if left != 1 else ''}..."

    async def _send_block(self, title, status):
        """Post a status block at the current stop; returns the message (or None)."""
        channel = self.channel_map.get(self.stop_code)
        if channel is None:
            return None
        next_stop = self._next_stop()
        try:
            return await _send_quiet(
                channel, self._status_block(kc.STOP_CODES[next_stop], title, status)
            )
        except discord.HTTPException:
            log.exception("keke %s could not post %s block", self.keke_id, title)
            return None

    async def _delete_msg(self, message):
        if message is None:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            pass  # already gone

    async def refresh_notice(self):
        """Re-render the "departing in" block (passenger list + time left). Runs
        only on the NOTICE_REFRESH_SECONDS cadence; boarding does not trigger it
        (the "sitdown well" reply is what tells a player they are seated)."""
        message = self._notice
        if message is None:
            return
        try:
            next_stop = self._next_stop()
            await message.edit(
                content=self._status_block(
                    kc.STOP_CODES[next_stop], self._departing_title(), "Departing"
                ),
                allowed_mentions=discord.AllowedMentions.none(),  # no re-ping on edit
            )
        except discord.HTTPException:
            pass

    def _status_block(self, next_stop_code, title="ARRIVAL", status="Arrived"):
        pax = "\n".join(f"<@{p['member_id']}>" for p in self.passengers) or "—"
        # From/To describe the whole leg the keke is on, not the next hop:
        # e.g. an A<->B keke shows Delta North -> Delta Central on the way out
        # and Delta Central -> Delta North on the way back. self._dir already
        # points the way the keke is about to travel (see _next_stop).
        route = kc.ROUTES[self.route]
        first_zone = _zone_fullname(_stop_zone(kc.STOP_CODES[route["start"]]))
        last_zone = _zone_fullname(_stop_zone(kc.STOP_CODES[route["end"]]))
        if self._dir > 0:
            from_zone, to_zone = first_zone, last_zone
        else:
            from_zone, to_zone = last_zone, first_zone
        remaining_segments = self._remaining_segments(next_stop_code)
        seconds = kc.STOP_SECONDS + remaining_segments * (
            kc.MOVE_SECONDS + kc.STOP_SECONDS
        )
        eta = f"{max(1, round(seconds / 60))}mins"
        return STOP_BLOCK.format(
            title=title,
            status=status,
            from_zone=from_zone,
            to_zone=to_zone,
            count=len(self.passengers),
            capacity=kc.CAPACITY,
            pax=pax,
            eta=eta,
        )

    def _remaining_segments(self, from_stop_code):
        """Segments from the current stop to the route's far end (turnaround)."""
        start, end = kc.ROUTES[self.route]["start"], kc.ROUTES[self.route]["end"]
        if self._dir > 0:
            return end - self._stop
        return self._stop - start

    async def _tick_move(self):
        """Drive one segment: burn fuel, then drive MOVE_SECONDS."""
        next_stop = self._next_stop()
        km = kc.KM_PER_SEGMENT
        try:
            await kdb.burn_fuel(
                self.keke_id, km, f"Segment {self.stop_code} -> {kc.STOP_CODES[next_stop]}"
            )
        except BankError as exc:
            # Out of fuel mid-run: exception drop-off for everyone aboard.
            await self._emergency_dropoff(str(exc))
            self._loop_stop.set()
            return
        # Never enter a channel another keke is in or heading to: wait here
        # until it is free (or swap with a head-on keke), then drive.
        await self._await_clear(next_stop)
        await self._sleep(kc.MOVE_SECONDS)
        self._stop = next_stop
        self._moving_to = None

    async def _emergency_dropoff(self, reason):
        """Fuel ran out mid-route: everyone aboard is set down right here (not
        their booked destination) and debited a partial fare for the distance
        actually covered — departure stop to this stranded stop — not the
        full fare they'd have paid on arrival."""
        channel = self.channel_map.get(self.stop_code)
        parts = []
        for p in list(self.passengers):
            try:
                board_idx = kc.STOP_INDEX[p.get("board_stop", self.stop_code)]
                current_idx = kc.STOP_INDEX[self.stop_code]
                fare = kc.fare_for(abs(current_idx - board_idx) * kc.KM_PER_SEGMENT)
            except KeyError:
                fare = 0
            fare_text = "₦0"
            if fare > 0:
                try:
                    result = await kdb.credit_fare(STATE, p["player_id"], p["name"], fare)
                    fare_text = cfg.money(result["fare"])
                except BankError:
                    log.exception(
                        "keke %s: could not debit stranded fare for player %s",
                        self.keke_id, p["player_id"],
                    )
            parts.append(f"{fare_text} <@{p['member_id']}>")
            member = await self._get_member(channel, p["member_id"])
            await self._release_lock(p, member)
            moved = await self._set_player_location(p, member, self.stop_code)
            if not moved and member is not None:
                try:
                    await perms.sync_member_permissions(member)
                except Exception:
                    log.exception("keke permission sync failed for %s", p["member_id"])
        self.passengers.clear()
        if channel is not None and parts:
            text = MSG_FUEL_EMPTY.format(passengers=", ".join(parts))
            try:
                await channel.send(text)
            except discord.HTTPException:
                pass
        # Park until the keke is refuelled: leave the KEKE PARKED block here.
        await self.post_parked(here=True)
        self._loop_stop.set()
        log.warning("keke %s stranded: %s", self.keke_id, reason)

    async def _finish_stop(self):
        """Trip complete after !keke-stop: park here with the KEKE PARKED block
        and take the keke off the road."""
        await self.post_parked()
        self.fleet.pop(self.keke_id, None)
        self._loop_stop.set()

    def _pick_home(self, parked_codes):
        """Where this keke parks — and re-enters the road: the route's own start
        stop, or, if a running or already-parked keke has it, the free stop on
        its route furthest from the others (same spread as _pick_start)."""
        route = kc.ROUTES[self.route]
        lo, hi = route["start"], route["end"]
        taken = {kc.STOP_INDEX[c] for c in parked_codes if c in kc.STOP_INDEX}

        def free(i):
            return self._claimant(i) is None and i not in taken

        if free(lo):
            return lo
        candidates = [i for i in range(lo, hi + 1) if free(i)]
        if not candidates:
            return lo
        others = [o._stop for o in self.fleet.values() if o is not self and o.running]
        others += list(taken)
        if not others:
            return candidates[0]
        return max(candidates, key=lambda i: (min(abs(i - t) for t in others), -i))

    async def post_parked(self, here=False):
        """Leave the KEKE PARKED block and park the keke: at its STARTING stop
        after !keke-stop, or right where it stands (here=True) when the fuel ran
        out. Stays until the keke is back in service (see _clear_parked); on
        !keke-start / refuel it re-enters the road at this same stop."""
        if self.bot is None:
            return
        await _clear_parked(self.bot, self.keke_id)   # never two blocks for one keke
        try:
            others = {r["stop_code"] for r in await kdb.get_parked()
                      if r["keke_id"] != self.keke_id and r["stop_code"]}
        except Exception:
            log.exception("keke %s: could not read other parked kekes", self.keke_id)
            others = set()
        if here:
            home_code = self.stop_code          # out of fuel: exactly where it finished
        else:
            home_code = kc.STOP_CODES[self._pick_home(others)]
        channel = self.channel_map.get(home_code)
        if channel is None:
            home_code = self.stop_code
            channel = self.channel_map.get(home_code)
        if channel is None:
            return
        try:
            row = await kdb.get_keke(self.keke_id)
            fuel = _fmt_fuel(row["fuel_liters"]) if row is not None else "—"
        except Exception:
            log.exception("keke %s: could not read fuel for parked block", self.keke_id)
            fuel = "—"
        # Out of fuel: it resumes the way it was heading. After !keke-stop it
        # leaves its starting stop for the far end.
        direction = self._dir if here else 1
        route = kc.ROUTES[self.route]
        first_zone = _zone_fullname(_stop_zone(kc.STOP_CODES[route["start"]]))
        last_zone = _zone_fullname(_stop_zone(kc.STOP_CODES[route["end"]]))
        if direction > 0:
            from_zone, to_zone = first_zone, last_zone
        else:
            from_zone, to_zone = last_zone, first_zone
        text = PARKED_BLOCK.format(from_zone=from_zone, to_zone=to_zone, fuel=fuel)
        try:
            message = await _send_quiet(channel, text)
        except discord.HTTPException:
            log.exception("keke %s could not post parked block", self.keke_id)
            return
        try:
            await kdb.set_parked(self.keke_id, channel.id, message.id, home_code, direction)
        except Exception:
            log.exception("keke %s: could not record parked block", self.keke_id)

    async def _drop_passenger(self, p, channel, paid=True):
        """Settle the fare, put the passenger at the destination and lift the
        boarding lock so the arrival channel becomes writable for them.
        Emergency drop-off (paid=False): they get down at the keke's CURRENT
        stop, which becomes their location. Whatever happens, the lock is
        lifted and their permissions re-synced, so nobody stays muted."""
        board_channel = p.get("board_channel")
        member = await self._get_member(board_channel, p["member_id"])
        moved = False
        try:
            dest_code = p["location_code"]
            dest = self.channel_map.get(dest_code)
            if paid and dest is not None and self.stop_code == dest_code:
                try:
                    result = await kdb.credit_fare(
                        STATE, p["player_id"], p["name"], p["fare"]
                    )
                    fare_text = cfg.money(result["fare"])
                except BankError as exc:
                    if channel is not None:
                        try:
                            await channel.send(str(exc))
                        except discord.HTTPException:
                            pass
                    return
                # Lift the send-deny BEFORE syncing: sync_member_permissions never
                # clears an explicit member-level send-deny.
                await self._release_lock(p, member)
                moved = await self._set_player_location(p, member, dest_code)
                if channel is not None:
                    if p.get("is_hub_dropoff"):
                        text = MSG_EXCEPTION_DROPOFF.format(
                            destination=p["destination"],
                            dropoff=channel.name,
                            fare=fare_text,
                        ).replace("@player", f"<@{p['member_id']}>")
                    else:
                        text = MSG_DROPPED.replace(
                            "@player", f"<@{p['member_id']}>"
                        ).replace("₦{fare}", fare_text)
                    try:
                        await channel.send(text)
                    except discord.HTTPException:
                        pass
            elif not paid:
                await self._release_lock(p, member)
                moved = await self._set_player_location(p, member, self.stop_code)
        finally:
            await self._release_lock(p, member)
            if not moved and member is not None:
                # Location unchanged: restore the write access the boarding lock
                # replaced on their current channel.
                try:
                    await perms.sync_member_permissions(member)
                except Exception:
                    log.exception("keke permission sync failed for %s", p["member_id"])

    async def _get_member(self, channel, member_id):
        return await _fetch_member(channel, member_id)

    async def _release_lock(self, p, member):
        """Lift every boarding lock this passenger holds (the departure channel
        and the rooms inside it). A lock that can't be lifted stays on the list
        and on record, so the startup sweep will retry it."""
        for channel in list(p.get("locks", [])):
            if await _unlock_write(channel, member):
                p["locks"].remove(channel)
                try:
                    await kdb.remove_lock(p["member_id"], channel.id)
                except Exception:
                    log.exception("keke: could not clear lock record for %s", p["member_id"])

    async def _set_player_location(self, p, member, location_code):
        """Update the player's location and re-sync their channel permissions
        (the new location becomes writable, the old one read-only). Returns True
        when the location was updated."""
        try:
            await database.update_player_field(p["player_id"], "current_sub_location", location_code)
        except Exception:
            log.exception("keke location update failed for %s", p["player_id"])
            return False
        if member is not None:
            try:
                await perms.sync_member_permissions(member)
            except Exception:
                log.exception("keke permission sync failed for %s", p["member_id"])
        return True

    async def _sleep(self, seconds):
        try:
            await asyncio.wait_for(self._loop_stop.wait(), timeout=seconds)
            raise asyncio.CancelledError
        except asyncio.TimeoutError:
            pass

    async def shutdown(self):
        self._loop_stop.set()
        if self.task:
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None


def _route_channels(bot, guild, state):
    return perms.state_location_channels(guild, state)


class Keke(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.kekes = {}  # keke_id -> KekeUnit
        # ONE flat map {stop_code: channel}. Every KekeUnit holds a reference to
        # this same dict, and it is refreshed IN PLACE (never replaced), so the
        # units always see the current channels.
        self._stop_channels = {}
        self._ensure_engine_task()

    async def cog_load(self):
        await kdb.init_tables()

    def cog_unload(self):
        for unit in list(self.kekes.values()):
            unit._loop_stop.set()
        if self._engine_task is not None:
            self._engine_task.cancel()

    def _ensure_engine_task(self):
        if self._engine_task is None:
            self._engine_task = asyncio.create_task(self._engine())

    _engine_task = None

    def _refresh_channels(self):
        """Rebuild {stop_code: channel} from every guild the bot is in."""
        fresh = {}
        for guild in self.bot.guilds:
            for code, channel in perms.state_location_channels(guild, STATE).items():
                fresh.setdefault(code, channel)
        self._stop_channels.clear()
        self._stop_channels.update(fresh)
        missing = [c for c in kc.STOP_CODES if c not in self._stop_channels]
        if missing:
            log.warning(
                "keke: %d of %d stop channels not found (category name must contain "
                "%r and channel name must match the code): %s",
                len(missing), len(kc.STOP_CODES), STATE, ", ".join(missing),
            )

    async def _engine(self):
        # Wait until the bot has connected and can see its guilds/channels,
        # otherwise the kekes would start driving with no channels to post in.
        await self.bot.wait_until_ready()
        self._refresh_channels()
        try:
            await self._clear_stale_locks()
        except Exception:
            log.exception("keke engine: stale-lock sweep failed")
        try:
            await self._ensure_stop_history()
        except Exception:
            log.exception("keke engine: stop history setup failed")
        if RESET_FUEL_ON_DEPLOY:
            try:
                await kdb.reset_fuel(STATE)
            except Exception:
                log.exception("keke engine: could not reset fuel")
        # Spawn loops for every purchased keke in an active zone (survives bot
        # restarts). A zone stopped with !keke-stop stays stopped here too —
        # the flag lives in the DB, not just in this process's memory.
        try:
            rows = await kdb.get_kekes(STATE)
        except Exception:
            log.exception("keke engine: could not load kekes")
            return
        try:
            active_zones = await kdb.get_active_zones(STATE)
        except Exception:
            log.exception("keke engine: could not load zone start/stop state — defaulting to all zones active")
            active_zones = set(kc.ZONES)
        try:
            parked = {r["keke_id"] for r in await kdb.get_parked()}
        except Exception:
            log.exception("keke engine: could not load parked blocks")
            parked = set()
        for row in rows:
            if row["keke_id"] in self.kekes:
                continue
            if row["zone"] not in active_zones:
                # Still stopped: keep its parked block, but show the tank as it is now.
                if row["keke_id"] in parked:
                    await self._refresh_parked_fuel(row)
                continue
            # Back on the road: its old parked block (stopped / out of fuel) goes.
            start_code, heading = await _clear_parked(self.bot, row["keke_id"])
            unit = KekeUnit(row, self._stop_channels, self.kekes)
            unit.spawn(self.bot, start_code, heading)
            self.kekes[row["keke_id"]] = unit
        # Console summary: several kekes on overlapping routes post their own
        # arrival/departure blocks, so list what is actually running.
        print(f"[keke] {len(self.kekes)} keke(s) running: " + ", ".join(
            f"#{u.keke_id} zone {u.zone} ({u.route}, starts at {u.stop_code})"
            for u in self.kekes.values()
        ))

    async def _refresh_parked_fuel(self, row):
        """Update the fuel line of a stopped keke's parked block (e.g. after the
        deploy refill)."""
        try:
            rec = next(r for r in await kdb.get_parked() if r["keke_id"] == row["keke_id"])
            channel = self.bot.get_channel(rec["channel_id"]) or await self.bot.fetch_channel(rec["channel_id"])
            message = await channel.fetch_message(rec["message_id"])
            text = re.sub(r"(?m)^⛽:.*$", f"⛽: {_fmt_fuel(row['fuel_liters'])}", message.content)
            if text != message.content:
                await message.edit(content=text, allowed_mentions=discord.AllowedMentions.none())
        except (StopIteration, discord.HTTPException):
            pass
        except Exception:
            log.exception("keke %s: could not refresh parked block", row["keke_id"])

    async def _clear_stale_locks(self):
        """Riders exist only in memory, so after a restart nobody is riding:
        lift every boarding lock still on record and restore those players'
        normal access (otherwise they would stay muted in their departure
        channel and its rooms forever, because the permission sync never
        touches an explicit send-deny)."""
        try:
            rows = await kdb.get_locks()
        except Exception:
            log.exception("keke: could not read boarding locks")
            return
        to_sync = {}
        for row in rows:
            channel = self.bot.get_channel(row["channel_id"])
            member = await _fetch_member(channel, row["member_id"])
            done = channel is None or member is None or await _unlock_write(channel, member)
            if not done:
                continue
            try:
                await kdb.remove_lock(row["member_id"], row["channel_id"])
            except Exception:
                log.exception("keke: could not clear lock record")
            if member is not None:
                to_sync[member.id] = member
        for member in to_sync.values():
            try:
                await perms.sync_member_permissions(member)
            except Exception:
                log.exception("keke permission sync failed for %s", member.id)

    async def _ensure_stop_history(self):
        """Read Message History is permanently ON at every keke stop channel for
        everyone whose ROLE gives them access there, wherever they are and for as
        long as they are in the state. Set once on the roles themselves (not per
        player), so it can never depend on a player's location or on a permission
        sync having run. Viewing is still gated exactly as before: roles decide
        who can see a stop, and the bot hides it from anyone failing the full
        rules or living in another state."""
        for code in kc.STOP_CODES:
            channel = self._stop_channels.get(code)
            if channel is None:
                continue
            for target, overwrite in list(channel.overwrites.items()):
                if not isinstance(target, discord.Role):
                    continue                      # only roles, never people
                if overwrite.view_channel is not True:
                    continue                      # not a role with access here
                if overwrite.read_message_history is True:
                    continue                      # already on
                overwrite.read_message_history = True
                try:
                    await channel.set_permissions(
                        target, overwrite=overwrite, reason="Keke stop: permanent message history"
                    )
                except discord.HTTPException:
                    log.exception("keke: could not enable history for %s on #%s", target, channel)

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        self._refresh_channels()

    @commands.Cog.listener()
    async def on_ready(self):
        self._refresh_channels()

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        self._refresh_channels()
        await self._ensure_stop_history()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        self._refresh_channels()

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        # Renames or moving a channel to another category change the mapping.
        if before.name != after.name or before.category_id != after.category_id:
            self._refresh_channels()
            await self._ensure_stop_history()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _keke_here(self, channel):
        """The keke currently parked at `channel` (its stop), if any.
        Matched by channel ID, so emoji/decoration in the channel name
        (e.g. '🏥┃hospital-lobby') does not matter."""
        for unit in self.kekes.values():
            if unit.stop_channel_id() == channel.id:
                return unit
        return None

    def _player(self, member):
        return database.get_player_by_discord_id(member.id)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def _has_role(self, member, names):
        return any(r.name.casefold() in {n.casefold() for n in names} for r in member.roles)

    async def _lock_ride(self, member, channel, player):
        """Boarding rule: the departure channel AND every room inside it the
        rider could write in are locked for the whole ride. Returns the list of
        channels WE locked (a pre-existing staff mute is left alone)."""
        targets = [channel]
        try:
            writable = await perms.writable_codes(member, player)
        except Exception:
            log.exception("keke: could not work out the rooms to lock for %s", member.id)
            writable = set()
        for code in sorted(writable):
            room = self._stop_channels.get(code)
            if room is not None and room.id != channel.id:
                targets.append(room)
        locked = []
        for target in targets:
            if await self._lock_write(member, target):
                locked.append(target)
        return locked

    async def _lock_write(self, member, channel):
        """Boarding rule (1.9/section 7): the departure channel is locked for
        the passenger for the whole ride. sync_member_permissions never clears
        an explicit send-deny, so the keke writes its own deny here and lifts
        it at drop-off (KekeUnit._release_lock). Returns True if WE placed the
        deny (a pre-existing staff mute is left alone, and never lifted)."""
        try:
            overwrite = channel.overwrites_for(member)
            if overwrite.send_messages is False:
                return False
            overwrite.send_messages = False
            await channel.set_permissions(member, overwrite=overwrite, reason="Keke boarding")
        except discord.HTTPException:
            log.exception("keke: could not lock #%s for %s", channel, member)
            return False
        try:
            await kdb.add_lock(member.id, channel.id)
        except Exception:
            log.exception("keke: could not record lock for %s", member.id)
        return True

    async def _gate_check(self, ctx, member, dest):
        """Message 7: role-gated channels need the gate-pass role to board.
        For exempt-channel drop-offs the keke jumps the channel and the player
        treks from the zone hub, so the HUB channel is gated instead."""
        hub_cat, hub_loc, _hub_parent, _is_hub_dropoff = dest
        node = locations.get_location(STATE, hub_cat, hub_loc)
        if node is None:
            return True
        access = node.get("access")
        if access is None:
            return True
        return locations.has_access([r.name for r in member.roles], access)

    def _fare_to(self, unit, hub_loc):
        """Fare from the keke's CURRENT stop to the drop-off stop (Q9)."""
        try:
            current = kc.STOP_INDEX[unit.stop_code]
            target = kc.STOP_INDEX[hub_loc]
        except KeyError:
            return None
        segments = abs(target - current)
        return kc.fare_for(segments * kc.KM_PER_SEGMENT)

    @commands.command(name="keke", help="Board the keke at your current stop.")
    async def keke(self, ctx, codename: str = None):
        if codename is None:
            await ctx.reply("invalid destination ❌")
            return
        codename = codename.strip().lower()
        dest = _effective_destination(codename)
        if dest is None:
            log.info("keke: unknown codename %r", codename)
            await ctx.reply(MSG_INVALID_DEST)
            return
        hub_cat, hub_loc, hub_parent, is_hub_dropoff = dest
        # What the player actually asked for (differs from hub_parent when the
        # destination is an exempt channel the keke jumps over).
        asked = kc.codename_dest(f"!keke {codename}")
        asked_name = asked[2] if asked else hub_parent

        self._refresh_channels()
        unit = self._keke_here(ctx.channel)
        if unit is None:
            where = ", ".join(f"#{u.keke_id}@{u.stop_code}" for u in self.kekes.values()) or "none running"
            log.info("keke: no keke parked in #%s (kekes: %s)", ctx.channel, where)
            await ctx.reply(MSG_NO_KEKE_HERE)
            return
        # !keke-stop issued: this keke is only finishing its current trip.
        if unit.stopping:
            await ctx.reply(MSG_NO_KEKE_HERE)
            return
        # Already riding a keke.
        if any(p["member_id"] == ctx.author.id for u in self.kekes.values() for p in u.passengers):
            await ctx.reply(MSG_INVALID_DEST)
            return
        # Gate pass (message 7) — checked before boarding.
        if not await self._gate_check(ctx, ctx.author, dest):
            await ctx.reply(MSG_GATE_PASS)
            return
        # Wrong route (messages 2a/2b) — the keke must serve the destination's zone.
        route_zones = kc.ROUTES[unit.route]["zones"]
        dest_zone = _stop_zone(hub_loc, unit.route) if hub_loc in kc.STOP_CODES else None
        if dest_zone is None or dest_zone not in route_zones:
            if len(unit.passengers) > 0:
                await ctx.reply(
                    MSG_WRONG_ROUTE_MULTI.format(route=_route_name(unit.route))
                )
            else:
                await ctx.reply(MSG_WRONG_ROUTE_SINGLE)
            return
        # Player record (fetched BEFORE the capacity check so there is no await
        # between checking for a free seat and taking it).
        player = await self._player(ctx.author)
        if player is None:
            await ctx.reply(MSG_NO_PLAYER_RECORD)
            return
        # Capacity
        if len(unit.passengers) >= kc.CAPACITY:
            await ctx.reply(MSG_KEKE_FULL)
            return
        # Cash at hand (Q3: checked at board time, deducted at drop-off).
        fare = self._fare_to(unit, hub_loc)
        if fare is None or float(player["cash_balance"]) < fare:
            await ctx.reply(MSG_CANT_AFFORD)
            return
        # Board: record the passenger, then lock the departure channel for them.
        passenger = {
            "member_id": ctx.author.id,
            "player_id": player["player_id"],
            "name": player["character_name"],
            "fare": fare,
            "location_code": hub_loc,
            "destination": asked_name,
            "hub_cat": hub_cat,
            "is_hub_dropoff": is_hub_dropoff,
            "board_channel": ctx.channel,
            "board_stop": unit.stop_code,
            "locks": [],
        }
        unit.passengers.append(passenger)
        await ctx.send(MSG_BOARDED.replace("@player", ctx.author.mention))
        passenger["locks"] = await self._lock_ride(ctx.author, ctx.channel, player)

    @commands.command(name="buy-keke", help="Delta Commissioner of Commerce buys a state keke.")
    async def buy_keke(self, ctx, zone: str):
        if not self._has_role(ctx.author, COMMISSIONER_ROLES):
            await ctx.reply("Not accessible❗You need gate pass 🪪")
            return
        zone = zone.strip().upper()
        if zone not in kc.ZONES:
            await ctx.reply("invalid destination ❌")
            return
        try:
            result = await kdb.buy_keke(STATE, zone, ctx.author.id)
        except BankError as exc:
            await ctx.reply(str(exc))
            return
        await ctx.reply(
            f"🛺 New {STATE} keke (zone {zone}) purchased for ₦{result['cost']:}. "
            f"Treasury balance: ₦{result['new_treasury_balance']:}."
        )
        # Spawn the new keke immediately.
        self._refresh_channels()
        rows = await kdb.get_kekes(STATE)
        for row in rows:
            if row["keke_id"] in self.kekes:
                continue
            unit = KekeUnit(row, self._stop_channels, self.kekes)
            unit.spawn(self.bot)
            self.kekes[row["keke_id"]] = unit
        await announce_transaction(self.bot, ctx.guild, result)

    def _parse_zones(self, arg):
        """'A' / 'A,B' / 'A,B,C' (any spacing/case) -> sorted unique valid
        zone letters, or None if nothing in it was a real zone."""
        zones = {z.strip().upper() for z in arg.split(",") if z.strip()}
        zones &= set(kc.ZONES)
        return sorted(zones) or None

    @commands.command(
        name="keke-start",
        help="Delta Commissioner of Commerce restarts keke service in one or more zones, e.g. !keke-start A,B",
    )
    async def keke_start(self, ctx, zones: str):
        if not self._has_role(ctx.author, COMMISSIONER_ROLES):
            await ctx.reply("Not accessible❗You need gate pass 🪪")
            return
        parsed = self._parse_zones(zones)
        if parsed is None:
            await ctx.reply("invalid destination ❌ — use zone letters A, B and/or C, e.g. `!keke-start A,B`")
            return
        for zone in parsed:
            await kdb.set_zone_active(STATE, zone, True)
        self._refresh_channels()
        try:
            rows = await kdb.get_kekes(STATE)
        except Exception:
            log.exception("keke-start: could not load kekes")
            rows = []
        # A keke still finishing its trip for an earlier !keke-stop just carries on.
        for unit in self.kekes.values():
            if unit.zone in parsed:
                unit.stopping = False
        started = []
        for row in rows:
            if row["zone"] not in parsed or row["keke_id"] in self.kekes:
                continue
            # Back in service: the parked block goes and the keke leaves from there.
            start_code, heading = await _clear_parked(self.bot, row["keke_id"])
            unit = KekeUnit(row, self._stop_channels, self.kekes)
            unit.spawn(self.bot, start_code, heading)
            self.kekes[row["keke_id"]] = unit
            started.append(row["keke_id"])
        note = f" (#{', #'.join(str(i) for i in started)} back on the road)" if started \
            else " (no kekes owned there yet)"
        await ctx.reply(f"🟢 Keke service started in zone(s) {', '.join(parsed)}.{note}")

    @commands.command(
        name="keke-stop",
        help="Delta Commissioner of Commerce halts keke service in one or more zones, e.g. !keke-stop A,B",
    )
    async def keke_stop(self, ctx, zones: str):
        if not self._has_role(ctx.author, COMMISSIONER_ROLES):
            await ctx.reply("Not accessible❗You need gate pass 🪪")
            return
        parsed = self._parse_zones(zones)
        if parsed is None:
            await ctx.reply("invalid destination ❌ — use zone letters A, B and/or C, e.g. `!keke-stop A,B`")
            return
        for zone in parsed:
            await kdb.set_zone_active(STATE, zone, False)
        stopped, finishing = [], []
        for unit in list(self.kekes.values()):
            if unit.zone not in parsed or not unit.running:
                continue
            if unit.passengers:
                # Carrying passengers: complete the trip first, then pull in.
                unit.stopping = True
                finishing.append(unit.keke_id)
            else:
                await unit.shutdown()
                await unit.post_parked()   # KEKE PARKED block stays until !keke-start
                self.kekes.pop(unit.keke_id, None)
                stopped.append(unit.keke_id)
        notes = []
        if stopped:
            notes.append(f"#{', #'.join(str(i) for i in stopped)} pulling in")
        if finishing:
            notes.append(f"#{', #'.join(str(i) for i in finishing)} finishing trip with passengers first")
        note = f" ({'; '.join(notes)})" if notes else " (none currently running there)"
        await ctx.reply(f"🔴 Keke service stopped in zone(s) {', '.join(parsed)}.{note}")


async def setup(bot):
    await bot.add_cog(Keke(bot))