"""
oil_config.py

All the numbers and route data for the tanker/trailer oil supply chain,
now covering all three states (Delta, Lagos, Abuja) so interstate trips can
actually continue past the destination's Immigration Office.

Mirrors the shape of keke_config.py: vehicle & ownership constants, route/stop
data, storage caps, and movement timing all live here. The rules live in
oil_database.py, the commands and channel moves in cogs/oil.py.

Route model
-----------
Each state's REAL location order is its own passenger-transport spine —
keke_config.STOP_CODES (Delta), danfo_config.STOP_CODES (Lagos), or
bus_config.STOP_CODES (Abuja) — copied here VERBATIM. These are never
reordered; they reflect actual physical geography and were confirmed
correct as given. immigration-office is the interstate entry/exit point
for every state, wherever it happens to sit in that state's own spine
(Lagos: far end; Abuja/Delta: near the front) — that's just geography.

industrial-district (refinery) hangs off "commercial-district" as an
exempt/off-spine stop in all three states (same hub, same pattern keke/
danfo/bus already use). For Delta, oil-well continues one further step
past industrial-district. Crucially: in Delta and Abuja, commercial-district
IS the spine's own endpoint, so the spur is just a straight extension. In
LAGOS, commercial-district is NOT the endpoint — Ghetto (administrative-
block, bed-sitter, line-houses, immigration-office) continues past it — so
industrial-district is a genuine dead-end branch off that point, not an
inline stop on the Mainland->Ghetto route. build_path() below handles this
properly: a trip between two "main" stops never detours through the spur,
and a trip touching the spur walks main-line to the branch point, then
onto the spur (or the reverse).
"""

# ---------------------------------------------------------------------------
# Vehicle & ownership
# ---------------------------------------------------------------------------

TRAILER_COST = 25_000_000        # NGN, from the State Treasury
TANKER_COST = 40_000_000         # NGN, from the State Treasury

TRAILER_CARGO_CAPACITY = 200     # barrels of crude per trip
TANKER_CARGO_CAPACITY = 5_000    # litres of fuel OR kg of gas per trip (never both)

# Vehicle's own fuel tank (separate from its cargo). Proposed defaults —
# flag if these should change.
TRAILER_TANK_CAPACITY_L = 200
TANKER_TANK_CAPACITY_L = 500
TRAILER_FUEL_PER_TRIP = 20       # flat burn, oil-well <-> refinery (short haul)
TANKER_FUEL_PER_KM = 1.0         # intrastate leg burn rate
TRAILER_FUEL_PER_INTERSTATE_TRIP = 100  # flat burn for the interstate leg
TANKER_FUEL_PER_INTERSTATE_TRIP = 100   # flat burn for the interstate leg

FUEL_LEDGER = "{S} Oil Fleet Fuel Ledger"   # treasury/narration label, per state

# ---------------------------------------------------------------------------
# Drilling (oil well, Delta only)
# ---------------------------------------------------------------------------

DRILL_YIELD_BARRELS = 50         # per `!drill crude` job
DRILL_DURATION_SECONDS = 5 * 60  # test time: drilling itself takes 5 min, not instant
DRILL_COST = 300_000             # NGN, equipment/operating cost per call (debited on start)

WELL_STOCKPILE_CAP = 200         # barrels sitting at the oil well

# ---------------------------------------------------------------------------
# Refining (refinery, every state)
# ---------------------------------------------------------------------------

REFINE_RATE_BARRELS_PER_MIN = 2  # test pace
REFINE_COST = 150_000            # NGN, equipment/operating cost per `!refine crude` call

REFINERY_CRUDE_CAP = 200         # barrels waiting to be refined
REFINERY_FUEL_CAP = 4_000        # litres of refined fuel in reserve
REFINERY_GAS_CAP = 4_000         # kg of refined gas in reserve

BARREL_TO_FUEL_L = 20            # 1 barrel -> 20L fuel
BARREL_TO_GAS_KG = 30            # 1 barrel -> 30kg gas

# ---------------------------------------------------------------------------
# Per-state routes. main_stops is each state's REAL spine, copied verbatim
# from that state's own transport config. spur_chain starts at the branch
# point (always "commercial-district") and continues outward; spur_chain[0]
# is always the branch point itself, shared with main_stops.
# ---------------------------------------------------------------------------

STATE_ROUTES = {
    "Delta": {
        # Same order as keke_config.STOP_CODES.
        "main_stops": [
            "administrative-block", "bed-sitter", "line-houses", "immigration-office",
            "rental-desk", "police-station", "clerk-office", "hotel-reception", "banking-hall",
            "help-desk", "hospital-lobby", "school-building", "broadcasting-station", "market-district",
            "automotive-district", "transport-district", "commercial-district",
        ],
        "spur_chain": ["commercial-district", "industrial-district", "oil-well"],
        "nnpc_stop": "automotive-district",
        "refinery_stop": "industrial-district",
        "oil_well_stop": "oil-well",
        "immigration_stop": "immigration-office",
    },
    "Lagos": {
        # Same order as danfo_config.STOP_CODES — Island -> Mainland -> Ghetto.
        # NOTE: commercial-district sits mid-spine here, NOT at the end.
        "main_stops": [
            "rental-desk", "police-station", "clerk-office", "hotel-reception", "banking-hall",
            "help-desk", "hospital-lobby", "school-building", "broadcasting-station", "market-district",
            "automotive-district", "transport-district", "commercial-district",
            "administrative-block", "bed-sitter", "line-houses", "immigration-office",
        ],
        "spur_chain": ["commercial-district", "industrial-district"],
        "nnpc_stop": "automotive-district",
        "refinery_stop": "industrial-district",
        "oil_well_stop": None,
        "immigration_stop": "immigration-office",
    },
    "Abuja": {
        # Same order as bus_config.STOP_CODES — North -> Central -> South.
        "main_stops": [
            "administrative-block", "bed-sitter", "line-houses", "immigration-office",
            "rental-desk", "police-station", "clerk-office", "hotel-reception", "banking-hall",
            "help-desk", "hospital-lobby", "school-building", "broadcasting-station", "market-district",
            "automotive-district", "transport-district", "commercial-district",
        ],
        "spur_chain": ["commercial-district", "industrial-district"],
        "nnpc_stop": "automotive-district",
        "refinery_stop": "industrial-district",
        "oil_well_stop": None,
        "immigration_stop": "immigration-office",
    },
}

for _state, _route in STATE_ROUTES.items():
    assert len(_route["main_stops"]) == 17, _state
    assert _route["spur_chain"][0] == "commercial-district", _state
del _state, _route

# Vehicles route/park at the PARENT district stop (that's what's stored as
# stop_code), but their actual messages — parked/departing/in-transit/
# arrival — should post in the specific sub-location channel inside that
# district, not the district channel itself. Same override in every state.
MESSAGE_STOP_OVERRIDE = {
    "automotive-district": "nnpc-fuel-station",
    "industrial-district": "refinery",
}

# Distance per segment. Reusing keke's flat figure (1.875km/segment)
# everywhere, including the industrial-district/oil-well spur segments,
# since no separate distance was given for those.
KM_PER_SEGMENT = 1.875

# ---------------------------------------------------------------------------
# Movement timing.
# ---------------------------------------------------------------------------

MOVE_SECONDS = 120               # drive between two adjacent real stops (trailer AND tanker)
TRANSIT_MESSAGE_EVERY_N_STOPS = 2   # in-transit message posts after every 2 real stops passed

# ---------------------------------------------------------------------------
# Interstate — the fixed border-crossing leg itself (Immigration Office ->
# destination state's Immigration Office). Symmetric per state pair.
# Placeholder timing, not yet locked in.
# ---------------------------------------------------------------------------

INTERSTATE_LEG_SECONDS = {
    frozenset({"Delta", "Lagos"}): 15 * 60,
    frozenset({"Delta", "Abuja"}): 15 * 60,
    frozenset({"Lagos", "Abuja"}): 15 * 60,
}


def interstate_leg_seconds(state_a, state_b):
    return INTERSTATE_LEG_SECONDS[frozenset({state_a, state_b})]
