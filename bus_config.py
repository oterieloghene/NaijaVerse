"""
bus_config.py

All the korope numbers and the location data for the Abuja korope (minibus)
system. Same mechanism as keke_config.py (Delta): 17-stop spine, 3 zones,
exempt channels jumped over and dropped at a zone hub instead. Abuja's
zoning is IDENTICAL to Delta's (North/Central/South, same physical order,
same 17 real stops) — unlike Lagos, there is no reordering here. What
differs from keke: the vehicle stats, purchase cost, boarding command
(!bus), and Abuja's own extra Central-zone destinations (Aso Rock's four
locations, the Central Bank) in place of Delta's Governor-related ones —
Abuja has no Governor, so Delta's govhouse/govoffice/depoffice codenames
don't exist here — plus two state-specific businesses in the South zone
(Mining Ground and Kanji Dam) instead of Delta's single Oil Well. Supreme
Court is NOT a separate stop — it shares Clerk Office's channel (the
courtroom), no codename of its own.

The rules live in bus_database.py; the commands and channel moves live in
cogs/bus.py. The wording of every player-facing message lives in
cogs/bus.py (MSG_* constants), same as keke/danfo.

Source of truth: this conversation, 2026-09-27 (korope numbers, codenames,
and light Hausa flavoring all confirmed by the user).
"""

import csv
from pathlib import Path

import locations as locmod

# ---------------------------------------------------------------------------
# Vehicle & ownership
# ---------------------------------------------------------------------------

BUS_COST = 6_000_000             # ₦ per unit, from the State Treasury
MAX_BUSES = 9                     # max koropes a state can own, in total (same cap as keke/danfo)
TANK_CAPACITY_L = 50               # litres per korope
FUEL_PER_KM = 0.75                 # litres burned per km
RANGE_KM = TANK_CAPACITY_L / FUEL_PER_KM   # ~66.67 km on a full tank
CAPACITY = 7                       # passengers per korope
FUEL_LEDGER = "Abuja Korope Fuel Ledger"   # treasury narration for fuel debits

# ---------------------------------------------------------------------------
# Zones & routes — identical structure/order to Delta's keke (no reordering):
# A = Abuja North, B = Abuja Central, C = Abuja South.
# ---------------------------------------------------------------------------

ZONES = {"A": "North", "B": "Central", "C": "South"}

# The 17 ACTUAL stops, North -> South. Identical parent-location codes to
# Delta's keke spine (shared categories across states) — only the display
# name of the university admin stop differs (Abuja: "Administrative Block").
STOP_CODES = [
    # A = Abuja North (4)
    "administrative-block",   # turnaround — Rural District (rural-district) jumped over
    "bed-sitter",
    "line-houses",             # NORTH exception drop-off (hub) — for rural-district
    "immigration-office",
    # B = Abuja Central (5) — the exempt central channels below are jumped over
    "rental-desk",
    "police-station",
    "clerk-office",
    "hotel-reception",         # CENTRAL exception drop-off (hub)
    "banking-hall",            # turnaround for the A<->B loop
    # C = Abuja South (8)
    "help-desk",
    "hospital-lobby",
    "school-building",
    "broadcasting-station",
    "market-district",
    "automotive-district",
    "transport-district",      # SOUTH exception drop-off (hub)
    "commercial-district",     # turnaround — industrial-district / mining-ground / kanji-dam jumped over
]

assert len(STOP_CODES) == 17
STOP_INDEX = {code: i for i, code in enumerate(STOP_CODES)}

# 16 effective segments over a 30 km spine, same split as keke (1.875 km each).
SEGMENTS = len(STOP_CODES) - 1                 # 16
TOTAL_KM = 30.0
KM_PER_SEGMENT = TOTAL_KM / SEGMENTS           # 1.875
ONE_WAY_KM = {
    "AB": 15.0,    # A<->B loop, one way  (8 segments)
    "BC": 22.5,    # B<->C loop, one way  (12 segments)
    "CA": 30.0,    # C<->A loop, one way  (16 segments)
}

# Route definitions: (zone pair, stop span of the 17-stop backbone, one-way km).
ROUTES = {
    "AB": {"zones": ("A", "B"), "start": 0, "end": 8, "one_way_km": 15.0,
           "name": "A↔B (Abuja North ⇄ Abuja Central)"},
    "BC": {"zones": ("B", "C"), "start": 4, "end": 16, "one_way_km": 22.5,
           "name": "B↔C (Abuja Central ⇄ Abuja South)"},
    "CA": {"zones": ("C", "A"), "start": 0, "end": 16, "one_way_km": 30.0,
           "name": "C↔A (Abuja South ⇄ Abuja North)"},
}
for _name, _route in ROUTES.items():
    _route["stop_codes"] = STOP_CODES[_route["start"]:_route["end"] + 1]
    assert len(_route["stop_codes"]) == _route["end"] - _route["start"] + 1

# ---------------------------------------------------------------------------
# Fares
# ---------------------------------------------------------------------------

FARE_PER_KM = 100                  # ₦ per km — same rate as keke/danfo
MIN_FARE = 100                     # under 1 km still costs ₦100

# ---------------------------------------------------------------------------
# Movement timing — unchanged mechanism from keke
# ---------------------------------------------------------------------------

MOVE_SECONDS = 2                   # drive between two adjacent ACTUAL stops
STOP_SECONDS = 60                  # dwell at each ACTUAL stop (boarding window), incl. turnarounds


def fare_for(km):
    """₦100 per km, fractional km rounds UP to the next whole km, never below
    ₦100 (same rule as keke/danfo)."""
    import math
    return max(MIN_FARE, int(math.ceil(float(km))) * FARE_PER_KM)


# ---------------------------------------------------------------------------
# Codenames — the approved code names (locations_codenames_abuja.csv).
# `!bus <codename>` resolves through this table, never through typing names.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent


def load_codename_table():
    """{codename: (parent_name, cmd)} from locations_codenames_abuja.csv."""
    with open(_ROOT / "locations_codenames_abuja.csv", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["code", "name"], "locations_codenames_abuja.csv header changed"
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
    _cmd_code = _cmd[len("!bus "):] if _cmd.startswith("!bus ") else _cmd
    _hit = None
    for _cat_code, _cat in locmod.LOCATIONS.get("Abuja", {}).items():
        for _loc_code, _loc in _cat["locations"].items():
            if _loc["name"] == _parent:
                _hit = (_cat_code, _loc_code)
    if _hit is None:
        raise RuntimeError(f"korope codename {_code!r} -> {_parent!r}: no matching Abuja location")
    CODENAME_BY_LOCATION[_hit] = (_cmd_code, _parent)

# Codenames whose destination is an exempt channel (parent location only).
# Bare codename words (the user types `!bus <word>`); the korope never stops
# there — the passenger is dropped at the zone HUB instead.
#
# Central-zone exemptions differ from Delta's: Abuja has no Governor, so
# govhouse/govoffice/depoffice don't exist here. In their place: Aso Rock's
# four locations (president, vp, cos, villa), FCT Minister, and the Central
# Bank (cbn) — all confirmed by the user. Supreme Court is NOT a separate
# stop — it's folded into Clerk Office/the courtroom, same channel, no
# codename of its own.
EXEMPT_CODES = {
    # SOUTH hub = transport-district
    "miniflat", "2bedroom", "3bedroom", "industrial", "mine", "dam",
    # CENTRAL hub = hotel-reception
    "estate", "duplex", "penthouse", "fctminister", "secretariat", "council",
    "cityhall", "cos", "president", "vp", "villa", "cbn",
    # NORTH hub = line-houses
    "rural",
}

# Where each exempt codename's parent channel actually sits on the spine, by
# zone (A = North, B = Central, C = South) — needed for the hub drop-off.
EXEMPT_ZONES = {
    "miniflat": "C", "2bedroom": "C", "3bedroom": "C",
    "industrial": "C", "mine": "C", "dam": "C",
    "estate": "B", "duplex": "B", "penthouse": "B", "fctminister": "B",
    "secretariat": "B", "council": "B", "cityhall": "B",
    "cos": "B", "president": "B", "vp": "B", "villa": "B", "cbn": "B",
    "rural": "A",
}

ZONE_HUB = {
    "A": "line-houses",        # NORTH exception drop-off
    "B": "hotel-reception",    # CENTRAL exception drop-off
    "C": "transport-district", # SOUTH exception drop-off
}


def codename_dest(codename):
    """codename (e.g. '!bus rural') -> (category_code, location_code, parent_name)
    or None if unknown."""
    if codename not in CODENAMES:
        return None
    parent, cmd = CODENAMES[codename]
    for cat_code, cat in locmod.LOCATIONS.get("Abuja", {}).items():
        for loc_code, loc in cat["locations"].items():
            if loc["name"] == parent:
                return (cat_code, loc_code, parent)
    return None


def codename_dest_by_code(location_code):
    """location_code -> (category_code, location_code, parent_name) or None."""
    for cat_code, cat in locmod.LOCATIONS.get("Abuja", {}).items():
        loc = cat["locations"].get(location_code)
        if loc is not None:
            return (cat_code, location_code, loc["name"])
    return None
