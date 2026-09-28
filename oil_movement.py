"""
oil_movement.py

Runs a single trailer/tanker trip (inside one state, or straight across a border)
from wherever it is currently parked to a
requested destination, within a single state's route (oil_config.STATE_ROUTES).

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
{emoji} {name} PARKED
Location: {stop_name}
Cargo: {cargo_amount:.0f} {cargo_type}
⛽: {fuel_liters:.2f} L
Status: 🔴 Not in Service
━━━━━━━━━━━━━━━━━━━━"""


def _walk(seq, start, end):
    """Inclusive walk of seq's indices from start to end, either direction."""
    step = 1 if end >= start else -1
    return [seq[i] for i in range(start, end + step, step)]


def build_path(state, from_code, to_code):
    """
    Ordered list of real stop codes from from_code to to_code, inclusive,
    within a single state's route (oil_config.STATE_ROUTES[state]).

    Two stops on the main spine walk the spine directly, in whichever
    direction, and NEVER detour through the industrial-district/oil-well
    spur — that matters most for Lagos, where the spur's branch point
    (commercial-district) sits mid-spine rather than at an endpoint.

    A trip touching the spur (industrial-district, or oil-well in Delta)
    walks the main spine to the branch point, then continues onto the
    spur (or the reverse) — never doubling back through the branch twice.
    """
    route = oc.STATE_ROUTES[state]
    main = route["main_stops"]
    main_index = {code: i for i, code in enumerate(main)}
    spur = route["spur_chain"]  # spur[0] is the branch point, shared with main
    spur_index = {code: i for i, code in enumerate(spur)}

    def locate(code):
        if code in spur_index and spur_index[code] > 0:
            return ("spur", spur_index[code])
        if code in main_index:
            return ("main", main_index[code])
        raise KeyError(f"'{code}' is not a stop in {state}'s route.")

    kind_from, pos_from = locate(from_code)
    kind_to, pos_to = locate(to_code)

    if kind_from == "main" and kind_to == "main":
        return _walk(main, pos_from, pos_to)

    if kind_from == "spur" and kind_to == "spur":
        return _walk(spur, pos_from, pos_to)

    branch_index = main_index[spur[0]]
    if kind_from == "main":
        main_leg = _walk(main, pos_from, branch_index)
        spur_leg = _walk(spur, 0, pos_to)
        return main_leg + spur_leg[1:]   # branch point isn't duplicated
    else:
        spur_leg = _walk(spur, pos_from, 0)
        main_leg = _walk(main, branch_index, pos_to)
        return spur_leg + main_leg[1:]


def _display_name(stop_code):
    return stop_code.replace("-", " ").title()


def build_steps(origin_state, origin_stop, dest_state, dest_stop, channel_maps):
    """
    One continuous trip as an ordered list of steps. Each step is a dict:
        {"state", "stop", "channel_map", "delay"}
    where delay = seconds to wait BEFORE arriving at that stop (0 for the
    departure stop).

    Same state: just build_path within that state, MOVE_SECONDS per hop.

    Different states: origin state's real path up to its Immigration
    Office, then straight into the destination state's Immigration Office
    (that one hop takes the interstate time, e.g. 15 min), then the
    destination state's real path onward from its Immigration Office.
    Immigration Office is just another stop on the way — nothing parks
    there and nothing needs re-commanding.
    """
    def leg(state, path, first_delay):
        steps = []
        for i, code in enumerate(path):
            steps.append({
                "state": state, "stop": code,
                "channel_map": channel_maps.get(state, {}),
                "delay": first_delay if i == 0 else oc.MOVE_SECONDS,
            })
        return steps

    if origin_state == dest_state:
        return leg(origin_state, build_path(origin_state, origin_stop, dest_stop), 0)

    origin_imm = oc.STATE_ROUTES[origin_state]["immigration_stop"]
    dest_imm = oc.STATE_ROUTES[dest_state]["immigration_stop"]
    first = leg(origin_state, build_path(origin_state, origin_stop, origin_imm), 0)
    second = leg(dest_state, build_path(dest_state, dest_imm, dest_stop),
                 oc.interstate_leg_seconds(origin_state, dest_state))
    return first + second


class OilTrip:
    """
    One in-progress trip (intrastate OR interstate — same class, the steps
    list decides). Created by the cog when !send-trailer / !send-tanker is
    called; the cog should drop its own reference once on_complete fires.

    On arrival it parks the vehicle at the final stop, and updates the
    vehicle's current_state to the final step's state (ownership never
    changes — only where it physically is).
    """

    def __init__(self, vehicle_id, vehicle_type, name, steps,
                 cargo_type, cargo_amount, fuel_liters, on_complete=None):
        self.vehicle_id = vehicle_id
        self.vehicle_type = vehicle_type
        self.name = name
        self.steps = steps               # steps[0] = origin, steps[-1] = destination
        self.cargo_type = cargo_type
        self.cargo_amount = cargo_amount
        self.fuel_liters = fuel_liters    # for the arrival PARKED block only
        self.on_complete = on_complete
        self._cancel = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    @property
    def emoji(self):
        return TRAILER_EMOJI if self.vehicle_type == "trailer" else TANKER_EMOJI

    def _label(self, step, with_state):
        name = _display_name(step["stop"])
        return f"{name} ({step['state']})" if with_state else name

    @property
    def _interstate(self):
        return self.steps[0]["state"] != self.steps[-1]["state"]

    async def _send(self, step, block):
        stop_code = step["stop"]
        post_code = oc.MESSAGE_STOP_OVERRIDE.get(stop_code, stop_code)
        channel = step["channel_map"].get(post_code)
        if channel is None:
            log.warning("No channel mapped for stop %s in %s (posting as %s) — %s block dropped.",
                        stop_code, step["state"], post_code, self.name)
            return None, None
        try:
            return await channel.send(block), channel
        except Exception:
            log.exception("Failed posting a block for %s at %s", self.name, stop_code)
            return None, None

    async def _post_moving(self, step, title, status):
        block = MOVING_BLOCK.format(
            emoji=self.emoji,
            vehicle_type=self.vehicle_type.upper(),
            title=title,
            from_name=self._label(self.steps[0], self._interstate),
            to_name=self._label(self.steps[-1], self._interstate),
            cargo_amount=self.cargo_amount,
            cargo_type=self.cargo_type,
            status=status,
        )
        await self._send(step, block)

    async def _park_at_destination(self):
        step = self.steps[-1]
        block = PARKED_BLOCK.format(
            emoji=self.emoji,
            name=self.name.upper(),
            stop_name=_display_name(step["stop"]),
            cargo_amount=self.cargo_amount,
            cargo_type=self.cargo_type,
            fuel_liters=self.fuel_liters,
        )
        # Physical location changes here (no-op if it never left its state).
        await odb.update_vehicle_state(self.vehicle_id, step["state"])
        message, channel = await self._send(step, block)
        if message is not None and channel is not None:
            # stop (the parent) is what's stored for routing purposes;
            # channel.id is wherever the message actually landed.
            await odb.set_parked(self.vehicle_id, channel.id, message.id, step["stop"])

    async def _run(self):
        try:
            if len(self.steps) < 2:
                # Already there — just park, no trip to run.
                await self._park_at_destination()
                return

            await self._post_moving(self.steps[0], "DEPARTING", "🟡 Departing")
            for i in range(1, len(self.steps)):
                if self._cancel.is_set():
                    return
                await asyncio.sleep(self.steps[i]["delay"])
                is_final = (i == len(self.steps) - 1)
                if is_final:
                    await self._park_at_destination()
                elif i % oc.TRANSIT_MESSAGE_EVERY_N_STOPS == 0:
                    await self._post_moving(self.steps[i], "IN TRANSIT", "🔵 In Transit")
        except Exception:
            log.exception("Oil trip for vehicle %s crashed", self.vehicle_id)
        finally:
            if self.on_complete:
                self.on_complete(self.vehicle_id)

    def cancel(self):
        self._cancel.set()
