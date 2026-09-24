"""
keke_config.py

All the keke numbers and the location data for the Delta State keke system.
The rules live in keke_database.py; the commands and channel moves live in cogs/keke.py.
The wording of every player-facing message lives in keke_messages.py.

Source of truth: /workspace/keke_notes.md (21st pass, 2026-09-22 — final).
"""

import csv
from pathlib import Path

import locations as locmod

# ---------------------------------------------------------------------------
# Vehicle & ownership (notes 1.1)
# ---------------------------------------------------------------------------

KEKE_COST = 2_500_000            # ₦ per unit, from the State Treasury
MAX_KEKES = 9                    # max kekes a state can own, in total
TANK_CAPACITY_L = 30             # litres per keke
FUEL_PER_KM = 0.25               # litres burned per km
RANGE_KM = TANK_CAPACITY_L / FUEL_PER_KM   # 120 km on a full tank
CAPACITY = 3                     # passengers per keke
FUEL_LEDGER = "Delta Keke Fuel Ledger"    # treasury narration for fuel debits

# ---------------------------------------------------------------------------
# Zones & routes (notes 1.4, 1.6, 1.7)
# ---------------------------------------------------------------------------
# A = North, B = Central, C = South. Delta only.

ZONES = {"A": "North", "B": "Central", "C": "South"}
ZONETAGS = {"north": "A", "central": "B", "south": "C"}

# The 17 ACTUAL stops, north -> south (21st pass). The keke ticks only at these;
# exempt channels are jumped over and appear nowhere in this list.
STOP_CODES = [
    # A = North (4)
    "administrative-block",   # turnaround — Rural District (rural-district) jumped over
    "bed-sitter",
    "line-houses",             # NORTH exception drop-off (hub) — for rural-district
    "immigration-office",
    # B = Central (5) — the 10 exempt central channels are jumped over
    "rental-desk",
    "police-station",
    "clerk-office",
    "hotel-reception",         # CENTRAL exception drop-off (hub)
    "banking-hall",            # turnaround for the A<->B loop
    # C = South (8)
    "help-desk",
    "hospital-lobby",
    "school-building",
    "broadcasting-station",
    "market-district",
    "automotive-district",
    "transport-district",      # SOUTH exception drop-off (hub)
    "commercial-district",     # turnaround — industrial-district / oil-well jumped over
]

assert len(STOP_CODES) == 17
STOP_INDEX = {code: i for i, code in enumerate(STOP_CODES)}

# The continuous spine of 16 effective segments. The 21st pass restores the 30 km
# total and splits it EVENLY across the 16 effective segments: 1.875 km each.
# km does NOT accrue across exempt channels (20th pass, kept in 21st pass).
SEGMENTS = len(STOP_CODES) - 1                 # 16
TOTAL_KM = 30.0
KM_PER_SEGMENT = TOTAL_KM / SEGMENTS           # 1.875
ONE_WAY_KM = {
    "AB": 15.0,    # A<->B loop, one way  (8 segments)
    "BC": 22.5,    # B<->C loop, one way  (12 segments)
    "CA": 30.0,    # C<->A loop, one way  (16 segments)
}

# Route definitions: (zone pair, stop span of the 17-stop backbone, one-way km).
# Each loop covers its two zones in full, so the spans share B's five stops:
# the A<->B keke runs administrative-block .. banking-hall, the B<->C keke
# runs rental-desk .. commercial-district, and the C<->A keke runs the whole
# spine.
ROUTES = {
    "AB": {"zones": ("A", "B"), "start": 0, "end": 8, "one_way_km": 15.0,
           "name": "A↔B (Delta North ⇄ Delta Central)"},
    "BC": {"zones": ("B", "C"), "start": 4, "end": 16, "one_way_km": 22.5,
           "name": "B↔C (Delta Central ⇄ Delta South)"},
    "CA": {"zones": ("C", "A"), "start": 0, "end": 16, "one_way_km": 30.0,
           "name": "C↔A (Delta South ⇄ Delta North)"},
}
for _name, _route in ROUTES.items():
    _route["stop_codes"] = STOP_CODES[_route["start"]:_route["end"] + 1]
    assert len(_route["stop_codes"]) == _route["end"] - _route["start"] + 1

# ---------------------------------------------------------------------------
# Fares (notes 1.5, Q9)
# ---------------------------------------------------------------------------

FARE_PER_KM = 100                  # ₦ per km
MIN_FARE = 100                     # under 1 km still costs ₦100

# ---------------------------------------------------------------------------
# Movement timing (notes 1.7 / 1.9, 20th pass; times unchanged in 21st pass)
# ---------------------------------------------------------------------------

MOVE_SECONDS = 2                   # drive between two adjacent ACTUAL stops
STOP_SECONDS = 10                  # boarding wait at each ACTUAL stop, incl. turnarounds


def fare_for(km):
    """₦100 per km, fractional km rounds UP to the next whole km, never below
    ₦100 (notes 1.5/Q9: 3.75 km -> ceil 4 -> ₦400, not ₦375; under 1 km is
    still ₦100)."""
    import math
    return max(MIN_FARE, int(math.ceil(float(km))) * FARE_PER_KM)


# ---------------------------------------------------------------------------
# Codenames — the approved code names (locations_codenames.csv, 33 rows).
# `!keke <codename>` resolves through this table, never through typing names.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent


def load_codename_table():
    """{codename: (location_code, parent_name)} from locations_codenames.csv."""
    with open(_ROOT / "locations_codenames.csv", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["code", "name"], "locations_codenames.csv header changed"
    table = {}
    for code, name in rows[1:]:
        if not code:
            continue
        table[code.strip().lower()] = (name.strip(), code)
    return table


CODENAMES = load_codename_table()

# Reverse lookup: (state category, location code) -> (codename, parent name).
# Parent location = the CSV "name" column (keke_notes 1.2: "Every parent location
# should have a CODE NAME" — hospital -> hospital-lobby, bank -> banking-hall).
CODENAME_BY_LOCATION = {}
for _code, (_parent, _cmd) in CODENAMES.items():
    _cmd_code = _cmd[len("!keke "):] if _cmd.startswith("!keke ") else _cmd
    # map CSV parent name -> the actual (category, location) in locations.py
    _hit = None
    for _cat_code, _cat in locmod.LOCATIONS.get("Delta", {}).items():
        for _loc_code, _loc in _cat["locations"].items():
            if _loc["name"] == _parent:
                _hit = (_cat_code, _loc_code)
    if _hit is None:
        raise RuntimeError(f"keke codename {_code!r} -> {_parent!r}: no matching Delta location")
    CODENAME_BY_LOCATION[_hit] = (_cmd_code, _parent)

# Codenames whose destination is an exempt channel (parent location only, 1.6).
# Bare codename words (the user types `!keke <word>`); the keke never stops
# there — the passenger is dropped at the zone HUB instead.
EXEMPT_CODES = {
    # SOUTH hub = transport-district
    "miniflat", "2bedroom", "3bedroom", "industrial", "oilwell",
    # CENTRAL hub = hotel-reception
    "estate", "duplex", "penthouse", "govhouse", "govoffice", "depoffice",
    "cos", "secretariat", "council", "cityhall",
    # NORTH hub = line-houses
    "rural",
}

# Where each exempt codename's parent channel actually sits on the spine, by
# zone (A = North, B = Central, C = South) — needed for the hub drop-off.
EXEMPT_ZONES = {
    "miniflat": "C", "2bedroom": "C", "3bedroom": "C",
    "industrial": "C", "oilwell": "C",
    "estate": "B", "duplex": "B", "penthouse": "B", "govhouse": "B",
    "govoffice": "B", "depoffice": "B", "cos": "B",
    "secretariat": "B", "council": "B", "cityhall": "B",
    "rural": "A",
}

ZONE_HUB = {
    "A": "line-houses",        # NORTH exception drop-off
    "B": "hotel-reception",    # CENTRAL exception drop-off
    "C": "transport-district", # SOUTH exception drop-off
}

# Which parent channel each codename physically lives in (category/location) —
# needed for channel moves and permission checks at the cog layer.
def codename_dest(codename):
    """codename -> (category_code, location_code, parent_name) or None if unknown."""
    if codename not in CODENAMES:
        return None
    parent, cmd = CODENAMES[codename]
    for cat_code, cat in locmod.LOCATIONS.get("Delta", {}).items():
        for loc_code, loc in cat["locations"].items():
            if loc["name"] == parent:
                return (cat_code, loc_code, parent)
    return None


def codename_dest_by_code(location_code):
    """location_code -> (category_code, location_code, parent_name) or None."""
    for cat_code, cat in locmod.LOCATIONS.get("Delta", {}).items():
        loc = cat["locations"].get(location_code)
        if loc is not None:
            return (cat_code, location_code, loc["name"])
    return None