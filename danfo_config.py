"""
danfo_config.py

All the danfo numbers and the location data for the Lagos State danfo (bus)
system. Same mechanism as keke_config.py (Delta): 17-stop spine, 3 zones,
exempt channels jumped over and dropped at a zone hub instead. Only the
vehicle stats, fare purchase cost, zone names and the state-specific business
stop (sea-port instead of oil-well) differ.

The rules live in danfo_database.py; the commands and channel moves live in
cogs/danfo.py. The wording of every player-facing message lives in
cogs/danfo.py (MSG_* constants), same as keke.

Source of truth: this conversation, 2026-09-25 (danfo numbers + Lagos zone
order confirmed by the user; mechanism mirrors keke_config.py's 21st pass).
"""

import csv
from pathlib import Path

import locations as locmod

# ---------------------------------------------------------------------------
# Vehicle & ownership
# ---------------------------------------------------------------------------

DANFO_COST = 8_000_000           # ₦ per unit, from the State Treasury
MAX_DANFOS = 9                   # max danfos a state can own, in total (same cap as keke)
TANK_CAPACITY_L = 70              # litres per danfo
FUEL_PER_KM = 1                   # litres burned per km
RANGE_KM = TANK_CAPACITY_L / FUEL_PER_KM   # 70 km on a full tank
CAPACITY = 10                     # passengers per danfo
FUEL_LEDGER = "Lagos Danfo Fuel Ledger"    # treasury narration for fuel debits

# ---------------------------------------------------------------------------
# Zones & routes
# ---------------------------------------------------------------------------
# CORRECTED geography (2026-09-26): the LOCATION-to-zone groupings are fixed
# facts (confirmed) — Island = {rental-desk, police-station, clerk-office,
# hotel-reception, banking-hall}, Mainland = {help-desk, hospital-lobby,
# school-building, broadcasting-station, market-district, automotive-
# district, transport-district, commercial-district}, Ghetto =
# {administrative-block, bed-sitter, line-houses, immigration-office}. What
# changed here is the ORDER these three groups sit in along the spine, top
# to bottom: Island first, Mainland in the middle, Ghetto last.
#   stops 0-4    (top, 5 stops)    = Lagos Island
#   stops 5-12   (middle, 8 stops) = Lagos Mainland
#   stops 13-16  (bottom, 4 stops) = Lagos Ghetto
#
# The three named danfo routes the user asked for map onto that geography
# like this:
#   Danfo A = Island <-> Mainland   -> short leg, stops 0-12   (adjacent)
#   Danfo B = Mainland <-> Ghetto   -> short leg, stops 5-16   (adjacent)
#   Danfo C = Island <-> Ghetto     -> the two spine EXTREMES, so it is the
#                                       long, full-spine route (stops 0-16),
#                                       physically driving through Mainland
#                                       without stopping there for boarding —
#                                       same "long route skips the middle
#                                       zone" pattern as Delta's keke C<->A.
#
# Route dict keys below are "A"/"B"/"C" directly (matching "Danfo A/B/C"),
# and a danfo purchased for zone letter X runs route X (see ZONE_ROUTE in
# cogs/danfo.py) — there is no separate "physical zone a danfo lives in";
# zone letter means "which of the three named routes it runs".

ZONES = {
    "A": "Island ⇄ Mainland",
    "B": "Mainland ⇄ Ghetto",
    "C": "Island ⇄ Ghetto",
}

# The three PHYSICAL zone names a stop can belong to (spine order, low->high
# index): used by cogs/danfo.py's _stop_zone() for wrong-route checks and
# hub lookups. Not the same namespace as the ZONES letters above.
PHYSICAL_ZONES_IN_SPINE_ORDER = ["Island", "Mainland", "Ghetto"]

# The 17 ACTUAL stops. Identical parent-location codes to Delta's keke spine
# (shared categories across states) — only the display name of the
# university admin stop differs (Lagos: "Administrative Block"). Each
# location's zone group is a fixed fact (confirmed); only the top-to-bottom
# ORDER of the three groups changed (Island, then Mainland, then Ghetto).
STOP_CODES = [
    # Lagos Island (5) — top of the spine
    "rental-desk",             # turnaround — the very start of the spine
    "police-station",
    "clerk-office",
    "hotel-reception",         # ISLAND exception drop-off (hub)
    "banking-hall",            # turnaround for the Island<->Mainland (route A) loop
    # Lagos Mainland (8) — middle of the spine
    "help-desk",
    "hospital-lobby",
    "school-building",
    "broadcasting-station",
    "market-district",
    "automotive-district",
    "transport-district",      # MAINLAND exception drop-off (hub)
    "commercial-district",     # turnaround for the Mainland<->Ghetto (route B) loop
    # Lagos Ghetto (4) — bottom of the spine
    "administrative-block",
    "bed-sitter",
    "line-houses",             # GHETTO exception drop-off (hub) — for rural-district
    "immigration-office",      # turnaround — the very end of the spine
]

assert len(STOP_CODES) == 17
STOP_INDEX = {code: i for i, code in enumerate(STOP_CODES)}

# 16 effective segments over a 30 km spine, same split as keke (1.875 km each).
SEGMENTS = len(STOP_CODES) - 1                 # 16
TOTAL_KM = 30.0
KM_PER_SEGMENT = TOTAL_KM / SEGMENTS           # 1.875
ONE_WAY_KM = {
    "A": 22.5,     # Island<->Mainland, one way  (12 segments, stops 0-12)
    "B": 20.625,   # Mainland<->Ghetto, one way  (11 segments, stops 5-16)
    "C": 30.0,     # Island<->Ghetto, one way    (16 segments, full spine)
}

# Route definitions: (physical zones served, stop span of the 17-stop
# backbone, one-way km). "zones" lists the PHYSICAL zone names (from
# PHYSICAL_ZONES_IN_SPINE_ORDER) this route boards/drops for — route C
# spans the full spine but, like Delta's C<->A keke, does not stop for the
# middle zone (Mainland) it drives through.
ROUTES = {
    "A": {"zones": ("Island", "Mainland"), "start": 0, "end": 12, "one_way_km": 22.5,
          "name": "Danfo A (Lagos Island ⇄ Lagos Mainland)"},
    "B": {"zones": ("Mainland", "Ghetto"), "start": 5, "end": 16, "one_way_km": 20.625,
          "name": "Danfo B (Lagos Mainland ⇄ Lagos Ghetto)"},
    "C": {"zones": ("Island", "Ghetto"), "start": 0, "end": 16, "one_way_km": 30.0,
          "name": "Danfo C (Lagos Island ⇄ Lagos Ghetto)"},
}
for _name, _route in ROUTES.items():
    _route["stop_codes"] = STOP_CODES[_route["start"]:_route["end"] + 1]
    assert len(_route["stop_codes"]) == _route["end"] - _route["start"] + 1

# ---------------------------------------------------------------------------
# Fares
# ---------------------------------------------------------------------------

FARE_PER_KM = 100                  # ₦ per km — same rate as keke
MIN_FARE = 100                     # under 1 km still costs ₦100

# ---------------------------------------------------------------------------
# Movement timing — unchanged mechanism from keke
# ---------------------------------------------------------------------------

MOVE_SECONDS = 2                   # drive between two adjacent ACTUAL stops
STOP_SECONDS = 60                  # dwell at each ACTUAL stop (boarding window), incl. turnarounds


def fare_for(km):
    """₦100 per km, fractional km rounds UP to the next whole km, never below
    ₦100 (same rule as keke: 3.75 km -> ceil 4 -> ₦400, not ₦375; under 1 km
    is still ₦100)."""
    import math
    return max(MIN_FARE, int(math.ceil(float(km))) * FARE_PER_KM)


# ---------------------------------------------------------------------------
# Codenames — the approved code names (locations_codenames_lagos.csv).
# `!danfo <codename>` resolves through this table, never through typing names.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent


def load_codename_table():
    """{codename: (location_code, parent_name)} from locations_codenames_lagos.csv."""
    with open(_ROOT / "locations_codenames_lagos.csv", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["code", "name"], "locations_codenames_lagos.csv header changed"
    table = {}
    for code, name in rows[1:]:
        if not code:
            continue
        table[code.strip().lower()] = (name.strip(), code)
    return table


CODENAMES = load_codename_table()

# Reverse lookup: (state category, location code) -> (codename, parent name).
CODENAME_BY_LOCATION = {}
for _code, (_parent, _cmd) in CODENAMES.items():
    _cmd_code = _cmd[len("!danfo "):] if _cmd.startswith("!danfo ") else _cmd
    _hit = None
    for _cat_code, _cat in locmod.LOCATIONS.get("Lagos", {}).items():
        for _loc_code, _loc in _cat["locations"].items():
            if _loc["name"] == _parent:
                _hit = (_cat_code, _loc_code)
    if _hit is None:
        raise RuntimeError(f"danfo codename {_code!r} -> {_parent!r}: no matching Lagos location")
    CODENAME_BY_LOCATION[_hit] = (_cmd_code, _parent)

# Codenames whose destination is an exempt channel (parent location only).
# Bare codename words (the user types `!danfo <word>`); the danfo never stops
# there — the passenger is dropped at the zone HUB instead. Same set as keke,
# except "oilwell" -> "seaport" (Lagos's state-specific business stop).
EXEMPT_CODES = {
    # MAINLAND hub = transport-district
    "miniflat", "2bedroom", "3bedroom", "industrial", "seaport",
    # ISLAND hub = hotel-reception
    "estate", "duplex", "penthouse", "govhouse", "govoffice", "depoffice",
    "cos", "secretariat", "council", "cityhall",
    # GHETTO hub = line-houses
    "rural",
}

# Where each exempt codename's parent channel actually sits, by PHYSICAL
# zone name (Island = stops 0-4, Mainland = stops 5-12, Ghetto = stops
# 13-16 — see the geography note above ROUTES) — needed for the hub
# drop-off.
EXEMPT_ZONES = {
    "miniflat": "Mainland", "2bedroom": "Mainland", "3bedroom": "Mainland",
    "industrial": "Mainland", "seaport": "Mainland",
    "estate": "Island", "duplex": "Island", "penthouse": "Island", "govhouse": "Island",
    "govoffice": "Island", "depoffice": "Island", "cos": "Island",
    "secretariat": "Island", "council": "Island", "cityhall": "Island",
    "rural": "Ghetto",
}

ZONE_HUB = {
    "Island": "hotel-reception",       # ISLAND exception drop-off
    "Mainland": "transport-district",  # MAINLAND exception drop-off
    "Ghetto": "line-houses",           # GHETTO exception drop-off
}


def codename_dest(codename):
    """codename -> (category_code, location_code, parent_name) or None if unknown."""
    if codename not in CODENAMES:
        return None
    parent, cmd = CODENAMES[codename]
    for cat_code, cat in locmod.LOCATIONS.get("Lagos", {}).items():
        for loc_code, loc in cat["locations"].items():
            if loc["name"] == parent:
                return (cat_code, loc_code, parent)
    return None


def codename_dest_by_code(location_code):
    """location_code -> (category_code, location_code, parent_name) or None."""
    for cat_code, cat in locmod.LOCATIONS.get("Lagos", {}).items():
        loc = cat["locations"].get(location_code)
        if loc is not None:
            return (cat_code, location_code, loc["name"])
    return None
