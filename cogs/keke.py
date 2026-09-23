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
stop the keke waits STOP_SECONDS for boarding, publishes the arrival/
departure block (message 3), burns the segment's fuel, and drives MOVE_SECONDS
to the next stop. Route ends are turnarounds (direction flips).

Fares are checked against the player's cash at hand at BOARD time and
deducted (plus state treasury credit) at DROP-OFF (Q3). Exception drop-off:
when the keke's fuel runs out mid-run, every passenger is dropped at the
keke's current stop and told to trek the rest (message 5).
"""

import asyncio
import logging
from collections import defaultdict

import discord
from discord.ext import commands

import bank_config as cfg
import database
import keke_config as kc
import keke_database as kdb
import location_permissions as perms
from bank_database import BankError
from bank_messages import announce_transaction
from locations import LOCATIONS

log = logging.getLogger("nvv.keke")

STATE = "Delta"
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

ARRIVAL_DEPARTURE = """━━━━━━━━━━━━━━━━━━━━
🛺 KEKE ARRIVAL/DEPARTURE
From: {from_zone}
To: {to_zone}
Passengers: {count}/{capacity}
Passengers on Board:
{pax}
Status: 🟢 Arrival/Departing
Estimated Arrival: {eta}
━━━━━━━━━━━━━━━━━━━━"""

MSG_DROPPED = "@player you don reach o. Your money na ₦{fare}, Thank you o 😊💸"
MSG_EXCEPTION_DROPOFF = (
    "@player, tax force nor gree us reach {destination}. "
    "Come down here {dropoff} trek. Nor vex 😭🚶🏾"
)


def _route_name(route):
    return kc.ROUTES[route]["name"]


def _zone_fullname(letter):
    return f"Delta {kc.ZONES[letter]}"


def _is_on_route(stop_code, route):
    return stop_code in kc.ROUTES[route]["stop_codes"]


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

    def __init__(self, row, channel_map):
        self.keke_id = row["keke_id"]
        self.zone = row["zone"]
        self.route = ZONE_ROUTE[self.zone]
        self.bot = None
        self.channel_map = channel_map
        self.passengers = []  # list of dicts: {member_id, player_id, name,
        #                                       destination, fare, hub_dropoff}
        self.task = None
        self._loop_stop = asyncio.Event()

    @property
    def direction(self):
        """+1 = north->south (towards route end), -1 = south->north."""
        return self._dir

    def stop_channel_id(self):
        """Channel ID of the stop the keke is currently at, or None."""
        channel = self.channel_map.get(self.stop_code)
        return channel.id if channel is not None else None

    def spawn(self, bot):
        self.bot = bot
        self._loop_stop.clear()
        start = kc.ROUTES[self.route]["start"]
        self._stop = start
        self._dir = 1
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
        route = kc.ROUTES[self.route]
        next_stop = self._next_stop()
        if channel is not None:
            try:
                await channel.send(self._status_block(next_stop))
            except discord.HTTPException:
                log.exception("keke %s could not post stop block", self.keke_id)
        await self._sleep(kc.STOP_SECONDS)

    def _status_block(self, next_stop_code):
        pax = "\n".join(f"<@{p['member_id']}>" for p in self.passengers) or "—"
        from_zone = _zone_fullname(_stop_zone(self.stop_code, self.route))
        to_zone = _zone_fullname(_stop_zone(next_stop_code, self.route))
        remaining_segments = self._remaining_segments(next_stop_code)
        seconds = kc.STOP_SECONDS + remaining_segments * (
            kc.MOVE_SECONDS + kc.STOP_SECONDS
        )
        eta = f"{max(1, round(seconds / 60))}mins"
        return ARRIVAL_DEPARTURE.format(
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
        await self._sleep(kc.MOVE_SECONDS)
        self._stop = next_stop

    async def _emergency_dropoff(self, reason):
        channel = self.channel_map.get(self.stop_code)
        dropoff_name = None
        if channel is not None:
            dropoff_name = channel.name
        for p in list(self.passengers):
            if channel is not None:
                text = MSG_EXCEPTION_DROPOFF.format(
                    destination=p["destination"],
                    dropoff=dropoff_name or self.stop_code,
                ).replace("@player", f"<@{p['member_id']}>")
                try:
                    await channel.send(text)
                except discord.HTTPException:
                    pass
            await self._drop_passenger(p, channel, paid=False)
        self.passengers.clear()
        # Park until the Commissioner refuels (deferred) — end the loop.
        self._loop_stop.set()
        log.warning("keke %s stranded: %s", self.keke_id, reason)

    async def _drop_passenger(self, p, channel, paid=True):
        """Move the passenger to the destination channel and settle the fare."""
        dest_code = p["location_code"]
        dest = self.channel_map.get(dest_code)
        member = None
        if self.bot is not None:
            member = self.bot.get_user(p["member_id"])
        # Drop at the destination channel when the keke is parked there;
        # otherwise (emergency drop-off) the passenger stays put and treks.
        if paid and dest is not None and self.stop_code == dest_code and member is not None:
            try:
                await member.move_to(dest)
            except discord.HTTPException:
                log.exception("keke %s move_to failed", self.keke_id)
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
            if channel is not None:
                text = MSG_DROPPED.replace(
                    "@player", f"<@{p['member_id']}>"
                ).replace("₦{fare}", fare_text)
                try:
                    await channel.send(text)
                except discord.HTTPException:
                    pass
            await self._set_player_location(p, dest_code)

    async def _set_player_location(self, p, location_code):
        try:
            await database.update_player_field(p["player_id"], "current_sub_location", location_code)
        except Exception:
            log.exception("keke location update failed for %s", p["player_id"])
        if self.bot is not None:
            player = await database.get_player(p["player_id"])
            member = self.bot.get_user(p["member_id"])
            if player and member:
                try:
                    await perms.sync_member_permissions(member)
                except Exception:
                    log.exception("keke permission sync failed for %s", p["member_id"])

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
        self._guild_map = {}  # guild_id -> {stop_code: channel}
        self._ensure_engine_task()

    async def cog_load(self):
        await kdb.init_tables()

    def cog_unload(self):
        for unit in list(self.kekes.values()):
            unit._loop_stop.set()

    def _ensure_engine_task(self):
        if self._engine_task is None:
            self._engine_task = asyncio.create_task(self._engine())

    _engine_task = None

    async def _engine(self):
        # Spawn loops for every purchased keke (survives bot restarts).
        try:
            rows = await kdb.get_kekes(STATE)
        except Exception:
            log.exception("keke engine: could not load kekes")
            return
        for row in rows:
            if row["keke_id"] in self.kekes:
                continue
            unit = KekeUnit(row, self._guild_map)
            unit.spawn(self.bot)
            self.kekes[row["keke_id"]] = unit

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        self._guild_map[guild.id] = perms.state_location_channels(guild, STATE)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            self._guild_map.setdefault(
                guild.id, perms.state_location_channels(guild, STATE)
            )
        if not self.kekes:
            await self._engine()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _keke_here(self, channel):
        """The keke currently parked at `channel` (its stop), if any."""
        for unit in self.kekes.values():
            if unit.stop_code == channel.name and unit.stop_channel_id() == channel.id:
                return unit
        return None

    def _player(self, member):
        return database.get_player_by_discord_id(member.id)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def _has_role(self, member, names):
        return any(r.name.casefold() in {n.casefold() for n in names} for r in member.roles)

    async def _revoke_write(self, member, channel):
        """Boarding rule (1.9/section 7): the departure channel is locked for
        the passenger for the whole ride. sync_member_permissions never clears
        an explicit send-deny, so the keke writes its own deny and clears it at
        drop-off."""
        try:
            perms = await channel.set_permissions(member, send_messages=False)
            member.guild.members.cache_clear()
            await channel.set_permissions(member, send_messages=None)
        except discord.HTTPException:
            pass

    async def _grant_write(self, member, channel):
        try:
            await channel.set_permissions(member, send_messages=None)
        except discord.HTTPException:
            pass

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
            await ctx.reply(MSG_INVALID_DEST)
            return
        hub_cat, hub_loc, _hub_parent, is_hub_dropoff = dest
        unit = self._keke_here(ctx.channel)
        if unit is None:
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
        # Capacity
        if len(unit.passengers) >= kc.CAPACITY:
            await ctx.reply(MSG_INVALID_DEST)
            return
        # Cash at hand (Q3: checked at board time, deducted at drop-off).
        player = await self._player(ctx.author)
        if player is None:
            await ctx.reply(MSG_INVALID_DEST)
            return
        fare = self._fare_to(unit, hub_loc)
        if fare is None or float(player["cash_balance"]) < fare:
            await ctx.reply(MSG_CANT_AFFORD)
            return
        # Board: revolve write access on the departure channel, record passenger.
        unit.passengers.append({
            "member_id": ctx.author.id,
            "player_id": player["player_id"],
            "name": player["character_name"],
            "fare": fare,
            "location_code": hub_loc,
            "destination": hub_parent or hub_loc,
            "hub_cat": hub_cat,
            "is_hub_dropoff": is_hub_dropoff,
        })
        await self._revoke_write(ctx.author, ctx.channel)

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
        rows = await kdb.get_kekes(STATE)
        for row in rows:
            if row["keke_id"] in self.kekes:
                continue
            unit = KekeUnit(row, self._guild_map)
            unit.spawn(self.bot)
            self.kekes[row["keke_id"]] = unit
        await announce_transaction(self.bot, ctx.guild, result)


async def setup(bot):
    await bot.add_cog(Keke(bot))