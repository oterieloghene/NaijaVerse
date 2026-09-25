"""
cogs/oil.py

Commands for the tanker/trailer oil supply chain: fleet purchase, drilling,
refining, loading/offloading, movement, refueling, and interstate requests.

Delta-first, mirroring keke.py's own STATE-scoped shape (see keke.py's
`STATE = "Delta"`): this cog only knows Delta's real stop-by-stop route
(oil_config.OIL_STOP_CODES) for now. Lagos/Abuja have no intrastate spine
built yet, so interstate arrivals there just park at Immigration Office —
nothing routes onward from there yet.

Command split (per the design decisions):
    Commissioner of Petroleum  - intrastate movement (well<->refinery<->NNPC)
                                  + drilling/refining/loading/offloading, for
                                  their OWN state's fleet only.
    Commissioner of Commerce   - fleet purchase + interstate movement (any
                                  trip ending at Immigration Office) +
                                  interstate request/quote.

The petroleum role name differs per state, so it's resolved from
locations.PETROLEUM_COMMISSIONER, never hardcoded to one string.
"""

import logging

from discord.ext import commands, tasks

import bank_messages as bank_msgs
import location_permissions as perms
import locations
import oil_config as oc
import oil_database as odb
from oil_movement import OilTrip, build_path
from bank_database import BankError

log = logging.getLogger(__name__)

STATE = "Delta"  # Delta-first; extend once Lagos/Abuja get a real intrastate spine.

PETROLEUM_ROLE = locations.PETROLEUM_COMMISSIONER[STATE]
REFINE_JOB_POLL_SECONDS = 15


def _commerce_role(state):
    return f"{state} Commissioner of Commerce"


class Oil(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.trips = {}  # vehicle_id -> OilTrip, only while in transit
        self._stop_channels = {}
        self._refresh_channels()  # best-effort; guilds are usually empty here
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
        # empty at that point and _stop_channels stays empty forever unless
        # something refreshes it later. Do that here, before every command —
        # same defensive pattern keke.py uses.
        self._refresh_channels()

    @commands.Cog.listener()
    async def on_ready(self):
        self._refresh_channels()

    # ------------------------------------------------------------------
    # channel / role helpers
    # ------------------------------------------------------------------

    def _refresh_channels(self):
        fresh = {}
        for guild in self.bot.guilds:
            for code, channel in perms.state_location_channels(guild, STATE).items():
                fresh.setdefault(code, channel)
        self._stop_channels = fresh
        missing = [c for c in oc.OIL_STOP_CODES if c not in self._stop_channels]
        if missing:
            log.warning("oil: missing channels for stops %s", missing)

    def _message_channel(self, stop_code):
        """The channel a stop's messages actually post to — the sub-location
        override (nnpc-fuel-station / refinery) if one exists for this stop,
        else the stop's own channel."""
        post_code = oc.MESSAGE_STOP_OVERRIDE.get(stop_code, stop_code)
        return self._stop_channels.get(post_code)

    def _commerce_channel(self, state):
        """<state>'s ministry-of-commerce channel, searched across every
        guild the bot is in — interstate requests post cross-guild."""
        for guild in self.bot.guilds:
            channel = perms.state_location_channels(guild, state).get("ministry-of-commerce")
            if channel is not None:
                return channel
        return None

    def _has_role(self, member, name):
        return any(r.name.casefold() == name.casefold() for r in member.roles)

    async def _require_petroleum(self, ctx):
        if not self._has_role(ctx.author, PETROLEUM_ROLE):
            await ctx.send(f"Only the {PETROLEUM_ROLE} can do that.")
            return False
        return True

    async def _require_commerce(self, ctx, state=None):
        role = _commerce_role(state or STATE)
        if not self._has_role(ctx.author, role):
            await ctx.send(f"Only the {role} can do that.")
            return False
        return True

    async def _find_vehicle(self, ctx, name):
        rows = await odb.get_vehicles(STATE)
        for row in rows:
            if row["name"].casefold() == name.casefold():
                return row
        await ctx.send(f"No vehicle named **{name}** in {STATE}'s fleet. Try `!fleet`.")
        return None

    # ------------------------------------------------------------------
    # background: resolve due refine jobs (refining is not instant)
    # ------------------------------------------------------------------

    @tasks.loop(seconds=REFINE_JOB_POLL_SECONDS)
    async def _refine_ticker(self):
        try:
            jobs = await odb.get_due_refine_jobs()
            for job in jobs:
                result = await odb.complete_refine_job(job["job_id"])
                if result is None:
                    continue
                channel = self._message_channel(oc.REFINERY_STOP)
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
                channel = self._message_channel(oc.OIL_WELL_STOP)
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
    # fleet & purchase (Commissioner of Commerce)
    # ------------------------------------------------------------------

    @commands.command(name="buy-trailer")
    async def buy_trailer(self, ctx):
        if not await self._require_commerce(ctx):
            return
        try:
            result = await odb.buy_vehicle(STATE, "trailer", ctx.author.id)
        except BankError as e:
            await ctx.send(str(e))
            return
        await self._park_new_vehicle(result["vehicle_id"], "trailer", result["name"], oc.TRAILER_TANK_CAPACITY_L)
        await ctx.send(
            f"🚛 **{result['name']}** purchased for ₦{oc.TRAILER_COST:,} — parked at NNPC Fuel Station."
        )
        await bank_msgs.post_treasury_debit(self.bot, ctx.guild, STATE, result)

    @commands.command(name="buy-tanker")
    async def buy_tanker(self, ctx):
        if not await self._require_commerce(ctx):
            return
        try:
            result = await odb.buy_vehicle(STATE, "tanker", ctx.author.id)
        except BankError as e:
            await ctx.send(str(e))
            return
        await self._park_new_vehicle(result["vehicle_id"], "tanker", result["name"], oc.TANKER_TANK_CAPACITY_L)
        await ctx.send(
            f"🛢️ **{result['name']}** purchased for ₦{oc.TANKER_COST:,} — parked at NNPC Fuel Station."
        )
        await bank_msgs.post_treasury_debit(self.bot, ctx.guild, STATE, result)

    async def _park_new_vehicle(self, vehicle_id, vehicle_type, name, fuel_liters):
        channel = self._message_channel(oc.NNPC_STOP)
        if channel is None:
            return
        emoji = "🚛" if vehicle_type == "trailer" else "🛢️"
        message = await channel.send(
            f"━━━━━━━━━━━━━━━━━━━━\n{emoji} {name.upper()} PARKED\n"
            f"Location: NNPC Fuel Station\nCargo: 0 none\n"
            f"⛽: {fuel_liters:.2f} L\nStatus: 🔴 Not in Service\n━━━━━━━━━━━━━━━━━━━━"
        )
        await odb.set_parked(vehicle_id, channel.id, message.id, oc.NNPC_STOP)

    @commands.command(name="fleet")
    async def fleet(self, ctx):
        rows = await odb.get_vehicles(STATE)
        if not rows:
            await ctx.send(f"{STATE} owns no trailers or tankers yet.")
            return
        lines = []
        for r in rows:
            status = "in transit" if r["vehicle_id"] in self.trips else "parked"
            cargo = f"{r['cargo_amount']:.0f} {r['cargo_type']}" if r["cargo_type"] != "none" else "empty"
            lines.append(f"**{r['name']}** — {status}, cargo: {cargo}, fuel: {r['fuel_liters']:.0f}L")
        await ctx.send("\n".join(lines))

    @commands.command(name="park")
    async def park(self, ctx, vehicle: str, stop: str = None):
        """Recovery command: manually park a vehicle that has no parked
        record (e.g. bought before the channel map had loaded). Defaults to
        the NNPC Fuel Station if no stop is given."""
        if not await self._require_petroleum(ctx):
            return
        row = await self._find_vehicle(ctx, vehicle)
        if row is None:
            return
        stop = (stop or oc.NNPC_STOP).lower()
        if stop not in oc.OIL_STOP_INDEX:
            await ctx.send(f"Unknown stop `{stop}`.")
            return
        channel = self._message_channel(stop)
        if channel is None:
            await ctx.send(f"No channel found for `{stop}` — check the channel setup.")
            return
        emoji = "🚛" if row["vehicle_type"] == "trailer" else "🛢️"
        cargo = f"{row['cargo_amount']:.0f} {row['cargo_type']}" if row["cargo_type"] != "none" else "0 none"
        message = await channel.send(
            f"━━━━━━━━━━━━━━━━━━━━\n{emoji} {row['name'].upper()} PARKED\n"
            f"Location: {stop.replace('-', ' ').title()}\nCargo: {cargo}\n"
            f"⛽: {row['fuel_liters']:.2f} L\nStatus: 🔴 Not in Service\n━━━━━━━━━━━━━━━━━━━━"
        )
        await odb.set_parked(row["vehicle_id"], channel.id, message.id, stop)
        await ctx.send(f"**{row['name']}** parked at {stop.replace('-', ' ').title()}.")

    # ------------------------------------------------------------------
    # drilling & refining (Commissioner of Petroleum)
    # ------------------------------------------------------------------

    @commands.command(name="drill")
    async def drill(self, ctx, resource: str = None):
        if resource is None or resource.lower() != "crude":
            await ctx.send("Usage: `!drill crude`")
            return
        if not await self._require_petroleum(ctx):
            return
        try:
            result = await odb.drill_crude(STATE, ctx.author.id)
        except BankError as e:
            await ctx.send(str(e))
            return
        await ctx.send(
            f"🛢️ Drilling... ready around <t:{int(result['due_at'].timestamp())}:R>."
        )

    @commands.command(name="refine")
    async def refine(self, ctx, resource: str = None, qty: float = None):
        if resource is None or resource.lower() != "crude" or qty is None:
            await ctx.send("Usage: `!refine crude <qty>`")
            return
        if not await self._require_petroleum(ctx):
            return
        try:
            result = await odb.refine_crude(STATE, qty, ctx.author.id)
        except BankError as e:
            await ctx.send(str(e))
            return
        await ctx.send(
            f"⚗️ Refining {qty:.0f} barrels — ready around <t:{int(result['due_at'].timestamp())}:R>."
        )

    # ------------------------------------------------------------------
    # loading / offloading (Commissioner of Petroleum)
    # ------------------------------------------------------------------

    @commands.command(name="load")
    async def load(self, ctx, vehicle: str, resource: str, qty: float):
        if not await self._require_petroleum(ctx):
            return
        row = await self._find_vehicle(ctx, vehicle)
        if row is None:
            return
        resource = resource.lower()
        try:
            if resource == "crude":
                result = await odb.load_crude(row["vehicle_id"], STATE, qty)
                unit = "barrels"
            elif resource in ("fuel", "gas"):
                result = await odb.load_product(row["vehicle_id"], STATE, resource, qty)
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
        if not await self._require_petroleum(ctx):
            return
        row = await self._find_vehicle(ctx, vehicle)
        if row is None:
            return
        resource = resource.lower()
        try:
            if resource == "crude":
                result = await odb.offload_crude(row["vehicle_id"], STATE, qty)
            elif resource == "fuel":
                result = await odb.offload_product(row["vehicle_id"], STATE, qty)
            else:
                await ctx.send("Resource must be `crude` or `fuel` (gas isn't wired to NNPC yet).")
                return
        except BankError as e:
            await ctx.send(str(e))
            return
        await ctx.send(f"Offloaded {result['offloaded']:.0f} {resource} from **{row['name']}**.")

    # ------------------------------------------------------------------
    # movement — intrastate: Petroleum. interstate: Commerce.
    # ------------------------------------------------------------------

    @commands.command(name="send-trailer")
    async def send_trailer(self, ctx, vehicle: str, destination: str):
        await self._send_vehicle(ctx, vehicle, destination, "trailer")

    @commands.command(name="send-tanker")
    async def send_tanker(self, ctx, vehicle: str, destination: str):
        await self._send_vehicle(ctx, vehicle, destination, "tanker")

    async def _send_vehicle(self, ctx, vehicle_name, destination, expected_type):
        destination = destination.lower()
        if destination not in oc.OIL_STOP_INDEX:
            await ctx.send(f"Unknown destination `{destination}`.")
            return

        row = await self._find_vehicle(ctx, vehicle_name)
        if row is None:
            return
        if row["vehicle_type"] != expected_type:
            await ctx.send(f"**{row['name']}** is a {row['vehicle_type']}, not a {expected_type}.")
            return
        if row["vehicle_id"] in self.trips:
            await ctx.send(f"**{row['name']}** is already in transit.")
            return

        parked = await odb.pop_parked(row["vehicle_id"])
        if parked is None:
            await ctx.send(f"**{row['name']}** isn't parked anywhere known — can't route it.")
            return
        origin = parked["stop_code"]

        # Any trip ending at Immigration Office (and not starting there) is
        # the interstate leg; everything else is intrastate.
        is_interstate = destination == oc.IMMIGRATION_STOP and origin != oc.IMMIGRATION_STOP
        allowed = await (self._require_commerce(ctx) if is_interstate else self._require_petroleum(ctx))
        if not allowed:
            await odb.set_parked(row["vehicle_id"], parked["channel_id"], parked["message_id"], origin)
            return

        old_channel = self._message_channel(origin)
        if old_channel is not None:
            try:
                old_message = await old_channel.fetch_message(parked["message_id"])
                await old_message.delete()
            except Exception:
                pass

        path = build_path(origin, destination)
        fuel_needed = self._fuel_needed(row, path)
        try:
            burn = await odb.burn_fuel(row["vehicle_id"], fuel_needed, f"{origin} -> {destination}")
        except BankError as e:
            await odb.set_parked(row["vehicle_id"], parked["channel_id"], parked["message_id"], origin)
            await ctx.send(str(e))
            return

        def _on_complete(vehicle_id):
            self.trips.pop(vehicle_id, None)

        trip = OilTrip(
            vehicle_id=row["vehicle_id"], vehicle_type=row["vehicle_type"], name=row["name"],
            path=path, channel_map=self._stop_channels,
            cargo_type=row["cargo_type"], cargo_amount=row["cargo_amount"],
            fuel_liters=burn["new_fuel"], on_complete=_on_complete,
        )
        self.trips[row["vehicle_id"]] = trip
        await ctx.send(f"**{row['name']}** departing {origin.replace('-', ' ').title()} for "
                        f"{destination.replace('-', ' ').title()}.")

    def _fuel_needed(self, row, path):
        segments = len(path) - 1
        if segments <= 0:
            return 0
        if row["vehicle_type"] == "trailer":
            return oc.TRAILER_FUEL_PER_TRIP
        if path[0] == oc.IMMIGRATION_STOP or path[-1] == oc.IMMIGRATION_STOP:
            return oc.TANKER_FUEL_PER_INTERSTATE_TRIP
        return segments * oc.KM_PER_SEGMENT * oc.TANKER_FUEL_PER_KM

    # ------------------------------------------------------------------
    # refueling — litres only, no money (see oil_database.refuel_vehicle)
    # ------------------------------------------------------------------

    @commands.command(name="refuel")
    async def refuel(self, ctx, vehicle: str, liters: float):
        row = await self._find_vehicle(ctx, vehicle)
        if row is None:
            return
        try:
            result = await odb.refuel_vehicle(row["vehicle_id"], STATE, liters)
        except BankError as e:
            await ctx.send(str(e))
            return
        await ctx.send(
            f"⛽ **{row['name']}** refueled +{result['liters']:.0f}L — tank now {result['new_fuel']:.0f}L."
        )

    # ------------------------------------------------------------------
    # interstate requests (Commissioner of Commerce, both states)
    # request -> quote -> manual bank payment -> !confirm-payment -> dispatch
    # ------------------------------------------------------------------

    @commands.command(name="request-fuel")
    async def request_fuel(self, ctx, owning_state: str, product: str, qty: float):
        if not await self._require_commerce(ctx):
            return
        product = product.lower()
        if product not in ("fuel", "gas"):
            await ctx.send("Product must be `fuel` or `gas`.")
            return
        request = await odb.create_request(STATE, owning_state, product, qty, ctx.author.id)
        channel = self._commerce_channel(owning_state)
        if channel is not None:
            await channel.send(
                f"⛽ FUEL REQUEST #{request['request_id']}\n"
                f"From: {STATE}\nTo: {owning_state}\nProduct: {qty:.0f} {product}\n"
                f"Status: 🔵 Pending Quote\n\n"
                f"Awaiting a price from {owning_state}'s Commissioner of Commerce."
            )
        await ctx.send(f"Request #{request['request_id']} sent to {owning_state}.")

    @commands.command(name="quote")
    async def quote(self, ctx, request_id: int, price: float):
        request = await odb.get_request(request_id)
        if request is None:
            await ctx.send("No such request.")
            return
        if not await self._require_commerce(ctx, state=request["owning_state"]):
            return
        try:
            updated = await odb.quote_request(request_id, price, ctx.author.id)
        except BankError as e:
            await ctx.send(str(e))
            return
        channel = self._commerce_channel(updated["requesting_state"])
        if channel is not None:
            await channel.send(
                f"⛽ FUEL REQUEST #{request_id}\n"
                f"From: {updated['requesting_state']}\nTo: {updated['owning_state']}\n"
                f"Product: {updated['qty']:.0f} {updated['product']}\nQuoted Price: ₦{price:,.2f}\n"
                f"Status: 🟠 Awaiting Payment\n\n"
                f"{updated['requesting_state']}: pay {updated['owning_state']}'s treasury via "
                f"the bank, then run `!confirm-payment {request_id}`."
            )
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
        if not await self._require_commerce(ctx, state=request["requesting_state"]):
            return
        try:
            updated = await odb.mark_paid(request_id)
        except BankError as e:
            await ctx.send(str(e))
            return
        for state in (updated["requesting_state"], updated["owning_state"]):
            channel = self._commerce_channel(state)
            if channel is not None:
                await channel.send(
                    f"⛽ FUEL REQUEST #{request_id}\n"
                    f"From: {updated['requesting_state']}\nTo: {updated['owning_state']}\n"
                    f"Product: {updated['qty']:.0f} {updated['product']}\n"
                    f"Status: 🟢 Paid — Dispatching\n"
                )
        await ctx.send(f"Request #{request_id} marked paid — ready to dispatch.")


async def setup(bot):
    await bot.add_cog(Oil(bot))
