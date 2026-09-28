"""
cogs/oil.py

Commands for the tanker/trailer oil supply chain: fleet purchase, drilling,
refining, loading/offloading, movement, refueling, product requests, and
vehicle hire.

EVERY command acts on the CALLER's own state, resolved from their role — a
Lagos Commissioner of Petroleum works on Lagos's refinery/NNPC/fleet, a
Delta Commissioner of Commerce buys for Delta, and so on. The petroleum
role name differs per state (locations.PETROLEUM_COMMISSIONER); the
commerce role is "<State> Commissioner of Commerce".

    Commissioner of Petroleum  drill (Delta only — only Delta has an oil
                               well), refine, load, offload, refuel, park,
                               and movement INSIDE a state.
    Commissioner of Commerce   buy, requests/quotes/hire, and movement
                               ACROSS a border.

Movement is one continuous trip, never a two-step handoff:
    !send-tanker <vehicle> <stop>            inside the vehicle's state
    !send-tanker <vehicle> <state> <stop>    across a border, all the way
                                             to <stop> there

Hire: a state can hire another state's trailer/tanker. Ownership never
changes. Once a hire is paid and the owner activates the lease (!lease),
the LESSEE's Commissioner of Petroleum can operate the vehicle while it is
physically in the lessee's state — load from their refinery, send it within
their state, offload into their NNPC — but cannot take it across a border.
The lessee ends the hire with !return-lease and the owner sends it home.
"""

import logging

from discord.ext import commands, tasks

import bank_messages as bank_msgs
import location_permissions as perms
import locations
import oil_config as oc
import oil_database as odb
from oil_movement import OilTrip, build_steps
from bank_database import BankError

log = logging.getLogger(__name__)

REFINE_JOB_POLL_SECONDS = 15


def _petroleum_role(state):
    return locations.PETROLEUM_COMMISSIONER[state]


def _commerce_role(state):
    return f"{state} Commissioner of Commerce"


def _display(stop_code):
    return stop_code.replace("-", " ").title()


class Oil(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.trips = {}  # vehicle_id -> OilTrip, only while in transit
        self._state_channels = {}  # {state: {stop_code: channel}}
        self._refresh_channels()   # best-effort; guilds are usually empty here
        self._refine_ticker.start()
        self._drill_ticker.start()

    async def cog_load(self):
        await odb.init_tables()

    def cog_unload(self):
        self._refine_ticker.cancel()
        self._drill_ticker.cancel()
        for trip in list(self.trips.values()):
            trip.cancel()

    async def cog_before_invoke(self, ctx):
        # __init__ runs before the bot has connected, so self.bot.guilds is
        # empty at that point — refresh before every command, same
        # defensive pattern keke.py uses.
        self._refresh_channels()

    @commands.Cog.listener()
    async def on_ready(self):
        self._refresh_channels()

    # ------------------------------------------------------------------
    # channel / role helpers
    # ------------------------------------------------------------------

    def _refresh_channels(self):
        fresh = {}
        for state in oc.STATE_ROUTES:
            state_map = {}
            for guild in self.bot.guilds:
                for code, channel in perms.state_location_channels(guild, state).items():
                    state_map.setdefault(code, channel)
            fresh[state] = state_map
            missing = [c for c in self._all_stops(state) if c not in state_map]
            if missing:
                log.warning("oil: %s missing channels for stops %s", state, missing)
        self._state_channels = fresh

    def _all_stops(self, state):
        route = oc.STATE_ROUTES[state]
        return set(route["main_stops"]) | set(route["spur_chain"])

    def _message_channel(self, state, stop_code):
        """The channel a stop's messages actually post to, in `state` — the
        sub-location override (nnpc-fuel-station / refinery) if one exists
        for this stop, else the stop's own channel."""
        post_code = oc.MESSAGE_STOP_OVERRIDE.get(stop_code, stop_code)
        return self._state_channels.get(state, {}).get(post_code)

    def _commerce_channel(self, state):
        """<state>'s ministry-of-commerce channel, searched across every
        guild the bot is in — requests post cross-guild."""
        for guild in self.bot.guilds:
            channel = perms.state_location_channels(guild, state).get("ministry-of-commerce")
            if channel is not None:
                return channel
        return None

    def _has_role(self, member, name):
        return any(r.name.casefold() == name.casefold() for r in member.roles)

    def _role_state(self, member, kind):
        """The state whose Commissioner (petroleum/commerce) this member is."""
        for state in oc.STATE_ROUTES:
            role = _petroleum_role(state) if kind == "petroleum" else _commerce_role(state)
            if self._has_role(member, role):
                return state
        return None

    async def _require_petroleum(self, ctx):
        state = self._role_state(ctx.author, "petroleum")
        if state is None:
            await ctx.send("Only a state's Commissioner of Petroleum can do that.")
        return state

    async def _require_commerce(self, ctx):
        state = self._role_state(ctx.author, "commerce")
        if state is None:
            await ctx.send("Only a state's Commissioner of Commerce can do that.")
        return state

    async def _find_vehicle(self, ctx, name, state, include_leased_in=True):
        """A vehicle by name, from `state`'s own fleet or (optionally) the
        vehicles `state` is currently hiring from other states."""
        rows = list(await odb.get_vehicles(state))
        if include_leased_in:
            rows += list(await odb.get_leased_in(state))
        for row in rows:
            if row["name"].casefold() == name.casefold():
                return row
        await ctx.send(f"No vehicle named **{name}** available to {state}. Try `!fleet`.")
        return None

    async def _operable(self, ctx, row, acting_state):
        """May `acting_state`'s Petroleum Commissioner operate this vehicle
        right now? Owner: yes, unless it's leased out and sitting in the
        lessee's state. Lessee: only while the lease is active AND the
        vehicle is physically in the lessee's state."""
        lease = await odb.get_active_lease(row["vehicle_id"])
        current = row["current_state"] or row["state"]
        if row["state"] == acting_state:
            if lease and current == lease["lessee_state"]:
                await ctx.send(
                    f"**{row['name']}** is leased to {lease['lessee_state']} and is in their hands right now."
                )
                return False
            return True
        if lease and lease["lessee_state"] == acting_state and current == acting_state:
            return True
        await ctx.send(f"**{row['name']}** isn't in {acting_state} under an active hire.")
        return False

    async def _parked_at(self, ctx, row, acting_state, stop_code, what):
        """The vehicle must be in `acting_state`, parked at `stop_code`."""
        current = row["current_state"] or row["state"]
        if current != acting_state:
            await ctx.send(f"**{row['name']}** is in {current}, not {acting_state}.")
            return False
        parked = await odb.get_parked_for(row["vehicle_id"])
        if parked is None or row["vehicle_id"] in self.trips:
            await ctx.send(f"**{row['name']}** isn't parked — it's on the road.")
            return False
        if parked["stop_code"] != stop_code:
            await ctx.send(f"**{row['name']}** has to be at {what} for that (it's at {_display(parked['stop_code'])}).")
            return False
        return True

    # ------------------------------------------------------------------
    # background: resolve due refine/drill jobs (neither is instant)
    # ------------------------------------------------------------------

    @tasks.loop(seconds=REFINE_JOB_POLL_SECONDS)
    async def _refine_ticker(self):
        try:
            jobs = await odb.get_due_refine_jobs()
            for job in jobs:
                result = await odb.complete_refine_job(job["job_id"])
                if result is None:
                    continue
                channel = self._message_channel(job["state"], oc.STATE_ROUTES[job["state"]]["refinery_stop"])
                if channel is not None:
                    await channel.send(
                        f"⚗️ Refining complete — +{result['fuel_added']:.1f} L fuel, "
                        f"+{result['gas_added']:.1f} kg gas added to {job['state']}'s reserve."
                    )
        except Exception:
            log.exception("oil: refine ticker failed")

    @_refine_ticker.before_loop
    async def _before_refine_ticker(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=REFINE_JOB_POLL_SECONDS)
    async def _drill_ticker(self):
        try:
            jobs = await odb.get_due_drill_jobs()
            for job in jobs:
                result = await odb.complete_drill_job(job["job_id"])
                if result is None:
                    continue
                channel = self._message_channel(job["state"], oc.STATE_ROUTES[job["state"]]["oil_well_stop"])
                if channel is not None:
                    await channel.send(
                        f"🛢️ Drilling complete — +{result['barrels_added']:.0f} barrels — "
                        f"well now at {result['new_well_barrels']:.0f}/{oc.WELL_STOCKPILE_CAP}."
                    )
        except Exception:
            log.exception("oil: drill ticker failed")

    @_drill_ticker.before_loop
    async def _before_drill_ticker(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # fleet & purchase (Commissioner of Commerce, caller's own state)
    # ------------------------------------------------------------------

    @commands.command(name="buy-trailer")
    async def buy_trailer(self, ctx):
        await self._buy(ctx, "trailer", oc.TRAILER_COST, oc.TRAILER_TANK_CAPACITY_L, "🚛")

    @commands.command(name="buy-tanker")
    async def buy_tanker(self, ctx):
        await self._buy(ctx, "tanker", oc.TANKER_COST, oc.TANKER_TANK_CAPACITY_L, "🛢️")

    async def _buy(self, ctx, vehicle_type, cost, tank, emoji):
        state = await self._require_commerce(ctx)
        if state is None:
            return
        try:
            result = await odb.buy_vehicle(state, vehicle_type, ctx.author.id)
        except BankError as e:
            await ctx.send(str(e))
            return
        await bank_msgs.post_treasury_debit(self.bot, ctx.guild, state, result)
        await self._park_new_vehicle(state, result["vehicle_id"], vehicle_type, result["name"], tank)
        await ctx.send(f"{emoji} **{result['name']}** purchased for ₦{cost:,} — parked at NNPC Fuel Station.")

    async def _park_new_vehicle(self, state, vehicle_id, vehicle_type, name, fuel_liters):
        stop = oc.STATE_ROUTES[state]["nnpc_stop"]
        channel = self._message_channel(state, stop)
        if channel is None:
            return
        emoji = "🚛" if vehicle_type == "trailer" else "🛢️"
        message = await channel.send(
            f"━━━━━━━━━━━━━━━━━━━━\n{emoji} {name.upper()} PARKED\n"
            f"Location: NNPC Fuel Station\nCargo: 0 none\n"
            f"⛽: {fuel_liters:.2f} L\nStatus: 🔴 Not in Service\n━━━━━━━━━━━━━━━━━━━━"
        )
        await odb.set_parked(vehicle_id, channel.id, message.id, stop)

    @commands.command(name="fleet")
    async def fleet(self, ctx):
        state = self._role_state(ctx.author, "petroleum") or self._role_state(ctx.author, "commerce")
        if state is None:
            await ctx.send("Only a state's Commissioner of Petroleum or Commerce can do that.")
            return
        owned = await odb.get_vehicles(state)
        leased_in = await odb.get_leased_in(state)
        if not owned and not leased_in:
            await ctx.send(f"{state} owns no trailers or tankers yet.")
            return
        lines = []
        for r in list(owned) + list(leased_in):
            status = "in transit" if r["vehicle_id"] in self.trips else "parked"
            cargo = f"{r['cargo_amount']:.0f} {r['cargo_type']}" if r["cargo_type"] != "none" else "empty"
            current = r["current_state"] or r["state"]
            note = ""
            if r["state"] != state:
                note = f", hired from {r['state']}"
            else:
                lease = await odb.get_active_lease(r["vehicle_id"])
                if lease:
                    note = f", leased to {lease['lessee_state']}"
                elif current != state:
                    note = f", currently in {current}"
            lines.append(f"**{r['name']}** — {status}{note}, cargo: {cargo}, fuel: {r['fuel_liters']:.0f}L")
        await ctx.send("\n".join(lines))

    @commands.command(name="park")
    async def park(self, ctx, vehicle: str, stop: str = None):
        """Recovery command: manually park a vehicle that has no parked
        record. Defaults to the NNPC Fuel Station where the vehicle is."""
        state = await self._require_petroleum(ctx)
        if state is None:
            return
        row = await self._find_vehicle(ctx, vehicle, state)
        if row is None or not await self._operable(ctx, row, state):
            return
        current = row["current_state"] or row["state"]
        stop = (stop or oc.STATE_ROUTES[current]["nnpc_stop"]).lower()
        if stop not in self._all_stops(current):
            await ctx.send(f"Unknown stop `{stop}` for {current}.")
            return
        channel = self._message_channel(current, stop)
        if channel is None:
            await ctx.send(f"No channel found for `{stop}` in {current} — check the channel setup.")
            return
        emoji = "🚛" if row["vehicle_type"] == "trailer" else "🛢️"
        cargo = f"{row['cargo_amount']:.0f} {row['cargo_type']}" if row["cargo_type"] != "none" else "0 none"
        message = await channel.send(
            f"━━━━━━━━━━━━━━━━━━━━\n{emoji} {row['name'].upper()} PARKED\n"
            f"Location: {_display(stop)}\nCargo: {cargo}\n"
            f"⛽: {row['fuel_liters']:.2f} L\nStatus: 🔴 Not in Service\n━━━━━━━━━━━━━━━━━━━━"
        )
        await odb.set_parked(row["vehicle_id"], channel.id, message.id, stop)
        await ctx.send(f"**{row['name']}** parked at {_display(stop)} ({current}).")

    # ------------------------------------------------------------------
    # drilling & refining (Commissioner of Petroleum, caller's own state)
    # ------------------------------------------------------------------

    @commands.command(name="drill")
    async def drill(self, ctx, resource: str = None):
        if resource is None or resource.lower() != "crude":
            await ctx.send("Usage: `!drill crude`")
            return
        state = await self._require_petroleum(ctx)
        if state is None:
            return
        if oc.STATE_ROUTES[state]["oil_well_stop"] is None:
            await ctx.send(f"{state} has no oil well to drill.")
            return
        try:
            result = await odb.drill_crude(state, ctx.author.id)
        except BankError as e:
            await ctx.send(str(e))
            return
        await ctx.send(f"🛢️ Drilling... ready around <t:{int(result['due_at'].timestamp())}:R>.")

    @commands.command(name="refine")
    async def refine(self, ctx, resource: str = None, qty: float = None):
        if resource is None or resource.lower() != "crude" or qty is None:
            await ctx.send("Usage: `!refine crude <qty>`")
            return
        state = await self._require_petroleum(ctx)
        if state is None:
            return
        try:
            result = await odb.refine_crude(state, qty, ctx.author.id)
        except BankError as e:
            await ctx.send(str(e))
            return
        await ctx.send(
            f"⚗️ Refining {qty:.0f} barrels — ready around <t:{int(result['due_at'].timestamp())}:R>."
        )

    # ------------------------------------------------------------------
    # loading / offloading (Commissioner of Petroleum) — always against the
    # stock of the state the vehicle is standing in, and only while it is
    # parked at the right place.
    # ------------------------------------------------------------------

    @commands.command(name="load")
    async def load(self, ctx, vehicle: str, resource: str, qty: float):
        state = await self._require_petroleum(ctx)
        if state is None:
            return
        row = await self._find_vehicle(ctx, vehicle, state)
        if row is None or not await self._operable(ctx, row, state):
            return
        resource = resource.lower()
        route = oc.STATE_ROUTES[state]
        try:
            if resource == "crude":
                if route["oil_well_stop"] is None:
                    await ctx.send(f"{state} has no oil well to load crude from.")
                    return
                if not await self._parked_at(ctx, row, state, route["oil_well_stop"], "the Oil Well"):
                    return
                result = await odb.load_crude(row["vehicle_id"], state, qty)
                unit = "barrels"
            elif resource in ("fuel", "gas"):
                if not await self._parked_at(ctx, row, state, route["refinery_stop"], "the Refinery"):
                    return
                result = await odb.load_product(row["vehicle_id"], state, resource, qty)
                unit = "L" if resource == "fuel" else "kg"
            else:
                await ctx.send("Resource must be `crude`, `fuel`, or `gas`.")
                return
        except BankError as e:
            await ctx.send(str(e))
            return
        await ctx.send(f"Loaded {result['loaded']:.0f} {unit} of {resource} onto **{row['name']}**.")

    @commands.command(name="offload")
    async def offload(self, ctx, vehicle: str, resource: str, qty: float):
        state = await self._require_petroleum(ctx)
        if state is None:
            return
        row = await self._find_vehicle(ctx, vehicle, state)
        if row is None or not await self._operable(ctx, row, state):
            return
        resource = resource.lower()
        route = oc.STATE_ROUTES[state]
        try:
            if resource == "crude":
                if not await self._parked_at(ctx, row, state, route["refinery_stop"], "the Refinery"):
                    return
                result = await odb.offload_crude(row["vehicle_id"], state, qty)
            elif resource == "fuel":
                if not await self._parked_at(ctx, row, state, route["nnpc_stop"], "the NNPC Fuel Station"):
                    return
                result = await odb.offload_product(row["vehicle_id"], state, qty)
            else:
                await ctx.send("Resource must be `crude` or `fuel` (gas isn't wired to NNPC yet).")
                return
        except BankError as e:
            await ctx.send(str(e))
            return
        await ctx.send(f"Offloaded {result['offloaded']:.0f} {resource} from **{row['name']}**.")

    # ------------------------------------------------------------------
    # movement — one continuous trip, inside a state or straight across a
    # border. Immigration Office is just another stop on the way.
    #
    #   !send-tanker <vehicle> <stop>            inside the vehicle's current
    #                                            state — Commissioner of
    #                                            Petroleum (owner, or the
    #                                            lessee while it is hired)
    #   !send-tanker <vehicle> <state> <stop>    into another state, all the
    #                                            way to <stop> there —
    #                                            Commissioner of Commerce of
    #                                            the OWNING state only
    # ------------------------------------------------------------------

    @commands.command(name="send-trailer")
    async def send_trailer(self, ctx, vehicle: str, target: str, stop: str = None):
        await self._send_vehicle(ctx, vehicle, target, stop, "trailer")

    @commands.command(name="send-tanker")
    async def send_tanker(self, ctx, vehicle: str, target: str, stop: str = None):
        await self._send_vehicle(ctx, vehicle, target, stop, "tanker")

    async def _send_vehicle(self, ctx, vehicle_name, target, stop, expected_type):
        is_interstate = stop is not None
        if is_interstate:
            acting = await self._require_commerce(ctx)
        else:
            acting = await self._require_petroleum(ctx)
        if acting is None:
            return

        # Crossing a border is the owner's call only — a lessee can't do it.
        row = await self._find_vehicle(ctx, vehicle_name, acting, include_leased_in=not is_interstate)
        if row is None:
            return
        if row["vehicle_type"] != expected_type:
            await ctx.send(f"**{row['name']}** is a {row['vehicle_type']}, not a {expected_type}.")
            return
        if row["vehicle_id"] in self.trips:
            await ctx.send(f"**{row['name']}** is already in transit.")
            return

        current_state = row["current_state"] or row["state"]
        if is_interstate:
            dest_state, dest_stop = target.strip().title(), stop.lower()
            if dest_state not in oc.STATE_ROUTES:
                await ctx.send(f"Unknown state `{target}`. Try Delta, Lagos, or Abuja.")
                return
            if dest_state == current_state:
                await ctx.send(f"**{row['name']}** is already in {current_state} — "
                               f"use `!send-{expected_type} \"{row['name']}\" <stop>` instead.")
                return
            lease = await odb.get_active_lease(row["vehicle_id"])
            if lease and current_state == lease["lessee_state"]:
                await ctx.send(f"**{row['name']}** is leased to {lease['lessee_state']} — it comes home "
                               f"after they run `!return-lease`.")
                return
        else:
            if not await self._operable(ctx, row, acting):
                return
            dest_state, dest_stop = current_state, target.lower()
        if dest_stop not in self._all_stops(dest_state):
            await ctx.send(f"Unknown destination `{dest_stop}` in {dest_state}.")
            return

        parked = await odb.pop_parked(row["vehicle_id"])
        if parked is None:
            await ctx.send(f"**{row['name']}** isn't parked anywhere known — can't route it.")
            return
        origin = parked["stop_code"]

        old_channel = self._message_channel(current_state, origin)
        if old_channel is not None:
            try:
                old_message = await old_channel.fetch_message(parked["message_id"])
                await old_message.delete()
            except Exception:
                pass

        try:
            steps = build_steps(current_state, origin, dest_state, dest_stop, self._state_channels)
        except KeyError as e:
            await odb.set_parked(row["vehicle_id"], parked["channel_id"], parked["message_id"], origin)
            await ctx.send(str(e))
            return

        fuel_needed = self._fuel_needed(row, steps, is_interstate)
        try:
            burn = await odb.burn_fuel(row["vehicle_id"], fuel_needed, f"{origin} -> {dest_stop}")
        except BankError as e:
            await odb.set_parked(row["vehicle_id"], parked["channel_id"], parked["message_id"], origin)
            await ctx.send(str(e))
            return

        def _on_complete(vehicle_id):
            self.trips.pop(vehicle_id, None)

        trip = OilTrip(
            vehicle_id=row["vehicle_id"], vehicle_type=row["vehicle_type"], name=row["name"],
            steps=steps, cargo_type=row["cargo_type"], cargo_amount=row["cargo_amount"],
            fuel_liters=burn["new_fuel"], on_complete=_on_complete,
        )
        self.trips[row["vehicle_id"]] = trip
        where = (f"{_display(dest_stop)} in {dest_state}" if is_interstate
                 else f"{_display(dest_stop)} within {current_state}")
        await ctx.send(f"**{row['name']}** departing {_display(origin)} for {where}.")

    def _fuel_needed(self, row, steps, is_interstate):
        segments = len(steps) - 1
        if segments <= 0:
            return 0
        if is_interstate:
            return (oc.TRAILER_FUEL_PER_INTERSTATE_TRIP if row["vehicle_type"] == "trailer"
                    else oc.TANKER_FUEL_PER_INTERSTATE_TRIP)
        if row["vehicle_type"] == "trailer":
            return oc.TRAILER_FUEL_PER_TRIP
        return segments * oc.KM_PER_SEGMENT * oc.TANKER_FUEL_PER_KM

    # ------------------------------------------------------------------
    # refueling — litres only, no money. Uses the NNPC stock of the state
    # the vehicle is standing in.
    # ------------------------------------------------------------------

    @commands.command(name="refuel")
    async def refuel(self, ctx, vehicle: str, liters: float):
        state = await self._require_petroleum(ctx)
        if state is None:
            return
        row = await self._find_vehicle(ctx, vehicle, state)
        if row is None or not await self._operable(ctx, row, state):
            return
        current = row["current_state"] or row["state"]
        if not await self._parked_at(ctx, row, current, oc.STATE_ROUTES[current]["nnpc_stop"],
                                     "the NNPC Fuel Station"):
            return
        try:
            result = await odb.refuel_vehicle(row["vehicle_id"], current, liters)
        except BankError as e:
            await ctx.send(str(e))
            return
        await ctx.send(
            f"⛽ **{row['name']}** refueled +{result['liters']:.0f}L — tank now {result['new_fuel']:.0f}L."
        )

    # ------------------------------------------------------------------
    # requests & hire (Commissioner of Commerce, both states)
    # request -> quote -> manual bank payment -> !confirm-payment ->
    #   fuel/gas: dispatch as agreed   |   tanker/trailer: !lease
    # ------------------------------------------------------------------

    def _request_block(self, r, status, footer=""):
        if r["product"] in ("fuel", "gas"):
            title = f"⛽ FUEL REQUEST #{r['request_id']}"
            what = f"{float(r['qty']):.0f} {r['product']}"
        else:
            title = f"🚚 {r['product'].upper()} HIRE #{r['request_id']}"
            what = f"1 {r['product']} (hire)"
        price = f"Quoted Price: ₦{float(r['quoted_price']):,.2f}\n" if r["quoted_price"] is not None else ""
        block = (f"{title}\nFrom: {r['requesting_state']}\nTo: {r['owning_state']}\n"
                 f"Product: {what}\n{price}Status: {status}\n")
        return block + (f"\n{footer}" if footer else "")

    async def _open_request(self, ctx, owning_state, product, qty):
        state = await self._require_commerce(ctx)
        if state is None:
            return
        owning_state = owning_state.strip().title()
        if owning_state not in oc.STATE_ROUTES or owning_state == state:
            await ctx.send("Name another state: Delta, Lagos, or Abuja.")
            return
        request = await odb.create_request(state, owning_state, product, qty, ctx.author.id)
        channel = self._commerce_channel(owning_state)
        if channel is not None:
            await channel.send(self._request_block(
                request, "🔵 Pending Quote",
                f"Awaiting a price from {owning_state}'s Commissioner of Commerce."))
        await ctx.send(f"Request #{request['request_id']} sent to {owning_state}.")

    @commands.command(name="request-fuel")
    async def request_fuel(self, ctx, owning_state: str, product: str, qty: float):
        product = product.lower()
        if product not in ("fuel", "gas"):
            await ctx.send("Product must be `fuel` or `gas`.")
            return
        await self._open_request(ctx, owning_state, product, qty)

    @commands.command(name="request-tanker")
    async def request_tanker(self, ctx, owning_state: str):
        await self._open_request(ctx, owning_state, "tanker", None)

    @commands.command(name="request-trailer")
    async def request_trailer(self, ctx, owning_state: str):
        await self._open_request(ctx, owning_state, "trailer", None)

    @commands.command(name="quote")
    async def quote(self, ctx, request_id: int, price: float):
        request = await odb.get_request(request_id)
        if request is None:
            await ctx.send("No such request.")
            return
        if self._role_state(ctx.author, "commerce") != request["owning_state"]:
            await ctx.send(f"Only {request['owning_state']}'s Commissioner of Commerce can quote that.")
            return
        try:
            updated = await odb.quote_request(request_id, price, ctx.author.id)
        except BankError as e:
            await ctx.send(str(e))
            return
        channel = self._commerce_channel(updated["requesting_state"])
        if channel is not None:
            await channel.send(self._request_block(
                updated, "🟠 Awaiting Payment",
                f"{updated['requesting_state']}: pay {updated['owning_state']}'s treasury via "
                f"the bank, then run `!confirm-payment {request_id}`."))
        await ctx.send(f"Quoted request #{request_id} at ₦{price:,.2f}.")

    @commands.command(name="confirm-payment")
    async def confirm_payment(self, ctx, request_id: int):
        """Marks a quoted request as paid once the manual bank transfer has
        gone through — called by the REQUESTING state's Commissioner of
        Commerce. This does not move money itself; it only unlocks dispatch.
        """
        request = await odb.get_request(request_id)
        if request is None:
            await ctx.send("No such request.")
            return
        if self._role_state(ctx.author, "commerce") != request["requesting_state"]:
            await ctx.send(f"Only {request['requesting_state']}'s Commissioner of Commerce can confirm that.")
            return
        try:
            updated = await odb.mark_paid(request_id)
        except BankError as e:
            await ctx.send(str(e))
            return
        hire = updated["product"] in ("tanker", "trailer")
        for state in (updated["requesting_state"], updated["owning_state"]):
            channel = self._commerce_channel(state)
            if channel is not None:
                await channel.send(self._request_block(
                    updated, "🟢 Paid — Awaiting Lease" if hire else "🟢 Paid — Dispatching"))
        await ctx.send(f"Request #{request_id} marked paid — ready to dispatch.")

    @commands.command(name="lease")
    async def lease(self, ctx, request_id: int, vehicle: str):
        """Owner's Commissioner of Commerce activates a paid hire, naming the
        vehicle. The lessee can operate it once it is in their state."""
        state = await self._require_commerce(ctx)
        if state is None:
            return
        request = await odb.get_request(request_id)
        if request is None or request["owning_state"] != state:
            await ctx.send("No such hire request for your state.")
            return
        row = await self._find_vehicle(ctx, vehicle, state, include_leased_in=False)
        if row is None:
            return
        try:
            lease = await odb.start_lease(request_id, row["vehicle_id"])
        except BankError as e:
            await ctx.send(str(e))
            return
        channel = self._commerce_channel(lease["lessee_state"])
        if channel is not None:
            await channel.send(
                f"🚚 **{row['name']}** is now hired to {lease['lessee_state']}. Once it arrives, "
                f"{lease['lessee_state']}'s Commissioner of Petroleum can operate it. "
                f"End the hire with `!return-lease \"{row['name']}\"`."
            )
        await ctx.send(
            f"**{row['name']}** leased to {lease['lessee_state']}. Send it with "
            f"`!send-{row['vehicle_type']} \"{row['name']}\" {lease['lessee_state']} <stop>`."
        )

    @commands.command(name="return-lease")
    async def return_lease(self, ctx, vehicle: str):
        """Lessee's Commissioner of Commerce hands a hired vehicle back. The
        owner's Commerce then sends it home."""
        state = await self._require_commerce(ctx)
        if state is None:
            return
        leased = await odb.get_leased_in(state)
        row = next((r for r in leased if r["name"].casefold() == vehicle.casefold()), None)
        if row is None:
            await ctx.send(f"{state} isn't hiring a vehicle called **{vehicle}**.")
            return
        if row["vehicle_id"] in self.trips:
            await ctx.send(f"**{row['name']}** is on the road — wait until it's parked.")
            return
        await odb.end_lease(row["vehicle_id"])
        channel = self._commerce_channel(row["state"])
        if channel is not None:
            await channel.send(
                f"🚚 {state} has handed back **{row['name']}**. It's parked in {state} — "
                f"send it home with `!send-{row['vehicle_type']} \"{row['name']}\" {row['state']} <stop>`."
            )
        await ctx.send(f"**{row['name']}** handed back to {row['state']}.")


async def setup(bot):
    await bot.add_cog(Oil(bot))
