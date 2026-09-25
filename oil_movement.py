"""
oil_movement.py

Runs a single trailer/tanker trip from wherever it's currently parked to a
requested destination, over the real oil_config.OIL_STOP_CODES line.

This is deliberately NOT shaped like keke's KekeUnit: a keke loops back and
forth over its route forever, picking up and dropping passengers at every
stop. A trailer/tanker trip is one-shot — drive to the destination, post
messages along the way, park, done. Nothing boards it, so there's no
boarding wait at intermediate stops, just the drive.

The cog owns the parked <-> in-transit transition: before starting a trip it
should pop_parked() the vehicle's current parked row and delete that
message; this module only handles the trip itself and leaves a fresh
PARKED_BLOCK message at the destination on arrival (its id saved via
oil_database.set_parked so the next trip can clean it up the same way).
"""

import asyncio
import logging

import oil_config as oc
import oil_database as odb

log = logging.getLogger(__name__)

TRAILER_EMOJI = "🚛"
TANKER_EMOJI = "🛢️"

MOVING_BLOCK = """━━━━━━━━━━━━━━━━━━━━
{emoji} {vehicle_type} {title}
From: {from_name}
To: {to_name}
Cargo: {cargo_amount} {cargo_type}
Status: {status}
━━━━━━━━━━━━━━━━━━━━"""

PARKED_BLOCK = """━━━━━━━━━━━━━━━━━━━━
{emoji} {vehicle_type} PARKED
Location: {stop_name}
Cargo: {cargo_amount} {cargo_type}
Fuel: {fuel_liters:.1f} L
━━━━━━━━━━━━━━━━━━━━"""


def build_path(from_code, to_code):
    """Ordered list of real stop codes from from_code to to_code, inclusive,
    walking the straight OIL_STOP_CODES line — no skipping, no doubling back."""
    start = oc.OIL_STOP_INDEX[from_code]
    end = oc.OIL_STOP_INDEX[to_code]
    step = 1 if end >= start else -1
    return [oc.OIL_STOP_CODES[i] for i in range(start, end + step, step)]


def _display_name(stop_code):
    return stop_code.replace("-", " ").title()


class OilTrip:
    """
    One in-progress trip. Created by the cog when !send-trailer / !send-tanker
    is called; the cog should drop its own reference once on_complete fires.
    """

    def __init__(self, vehicle_id, vehicle_type, name, path, channel_map,
                 cargo_type, cargo_amount, fuel_liters, on_complete=None):
        self.vehicle_id = vehicle_id
        self.vehicle_type = vehicle_type
        self.name = name
        self.path = path                 # path[0] = origin, path[-1] = destination
        self.channel_map = channel_map    # {stop_code: discord.TextChannel}
        self.cargo_type = cargo_type
        self.cargo_amount = cargo_amount
        self.fuel_liters = fuel_liters    # for the arrival PARKED block only
        self.on_complete = on_complete
        self._cancel = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    @property
    def emoji(self):
        return TRAILER_EMOJI if self.vehicle_type == "trailer" else TANKER_EMOJI

    @property
    def origin_name(self):
        return _display_name(self.path[0])

    @property
    def destination_name(self):
        return _display_name(self.path[-1])

    async def _send(self, stop_code, block):
        post_code = oc.MESSAGE_STOP_OVERRIDE.get(stop_code, stop_code)
        channel = self.channel_map.get(post_code)
        if channel is None:
            log.warning("No channel mapped for stop %s (posting as %s) — %s block dropped.",
                        stop_code, post_code, self.name)
            return None
        try:
            return await channel.send(block)
        except Exception:
            log.exception("Failed posting a block for %s at %s", self.name, stop_code)
            return None

    async def _post_moving(self, stop_code, title, status):
        block = MOVING_BLOCK.format(
            emoji=self.emoji,
            vehicle_type=self.vehicle_type.upper(),
            title=title,
            from_name=self.origin_name,
            to_name=self.destination_name,
            cargo_amount=self.cargo_amount,
            cargo_type=self.cargo_type,
            status=status,
        )
        await self._send(stop_code, block)

    async def _park_at_destination(self):
        stop_code = self.path[-1]
        block = PARKED_BLOCK.format(
            emoji=self.emoji,
            vehicle_type=self.vehicle_type.upper(),
            stop_name=_display_name(stop_code),
            cargo_amount=self.cargo_amount,
            cargo_type=self.cargo_type,
            fuel_liters=self.fuel_liters,
        )
        message = await self._send(stop_code, block)
        post_code = oc.MESSAGE_STOP_OVERRIDE.get(stop_code, stop_code)
        channel = self.channel_map.get(post_code)
        if message is not None and channel is not None:
            # stop_code (the parent) is what's stored for routing purposes;
            # channel.id is wherever the message actually landed.
            await odb.set_parked(self.vehicle_id, channel.id, message.id, stop_code)

    async def _run(self):
        try:
            if len(self.path) < 2:
                # Already there — just park, no trip to run.
                await self._park_at_destination()
                return

            await self._post_moving(self.path[0], "DEPARTING", "🟡 Departing")
            for i in range(1, len(self.path)):
                if self._cancel.is_set():
                    return
                await asyncio.sleep(oc.MOVE_SECONDS)
                stop_code = self.path[i]
                is_final = (i == len(self.path) - 1)
                if is_final:
                    await self._park_at_destination()
                elif i % oc.TRANSIT_MESSAGE_EVERY_N_STOPS == 0:
                    await self._post_moving(stop_code, "IN TRANSIT", "🔵 In Transit")
        except Exception:
            log.exception("Oil trip for vehicle %s crashed", self.vehicle_id)
        finally:
            if self.on_complete:
                self.on_complete(self.vehicle_id)

    def cancel(self):
        self._cancel.set()
