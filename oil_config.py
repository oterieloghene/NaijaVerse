"""
oil_config.py

All the numbers and route data for the tanker/trailer oil supply chain
(Delta oil well -> refinery -> NNPC fuel station / interstate).

Mirrors the shape of keke_config.py: vehicle & ownership constants, route/stop
data, storage caps, and movement timing all live here. The rules live in
oil_database.py, the commands and channel moves in cogs/oil.py.

Delta only for now (oil well and the real stop-by-stop route). Lagos and
Abuja have no intrastate stop-order system built yet, so interstate arrivals
there currently stop at Immigration Office with no further routing.
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
# Delta route — real stop-by-stop order, no exempt-skipping.
# Exempt only applies to keke passengers; freight stops at every real
# location. This is keke's 17-stop spine (see keke_config.STOP_CODES) with
# industrial-district (refinery) and oil-well appended as real stops at the
# southern end, in the confirmed order:
#   ... -> commercial-district -> industrial-district -> oil-well
# ---------------------------------------------------------------------------

OIL_STOP_CODES = [
    # North terminus
    "administrative-block",
    "bed-sitter",
    "line-houses",
    "immigration-office",         # interstate exit/entry point
    "rental-desk",
    "police-station",
    "clerk-office",
    "hotel-reception",
    "banking-hall",
    "help-desk",
    "hospital-lobby",
    "school-building",
    "broadcasting-station",
    "market-district",
    "automotive-district",        # NNPC Fuel Station is here — fleet parks here
    "transport-district",
    "commercial-district",
    "industrial-district",        # Refinery is here
    "oil-well",                   # South terminus — crude source
]

assert len(OIL_STOP_CODES) == 19
OIL_STOP_INDEX = {code: i for i, code in enumerate(OIL_STOP_CODES)}

NNPC_STOP = "automotive-district"
REFINERY_STOP = "industrial-district"
OIL_WELL_STOP = "oil-well"
IMMIGRATION_STOP = "immigration-office"

# Vehicles route/park at the PARENT district stop above (that's what's
# stored as stop_code), but their actual messages — parked/departing/
# in-transit/arrival — should post in the specific sub-location channel
# inside that district, not the district channel itself.
MESSAGE_STOP_OVERRIDE = {
    NNPC_STOP: "nnpc-fuel-station",
    REFINERY_STOP: "refinery",
}

# Distance per segment. Reusing keke's flat figure (1.875km/segment) across
# the whole line, including the two new segments (commercial-district <->
# industrial-district, industrial-district <-> oil-well), since no separate
# distance was given for those. Flag if these two should get their own km.
KM_PER_SEGMENT = 1.875

# ---------------------------------------------------------------------------
# Movement timing — reusing keke's per-segment pace.
# ---------------------------------------------------------------------------

MOVE_SECONDS = 2                 # drive between two adjacent real stops
STOP_SECONDS = 60                # dwell at each real stop
TRANSIT_MESSAGE_EVERY_N_STOPS = 2   # in-transit message posts after every 2 real stops passed

# ---------------------------------------------------------------------------
# Interstate — Delta-first. Placeholder fixed times for the interstate leg
# itself (Immigration Office -> destination state's Immigration Office).
# Proposed, not yet locked in.
# ---------------------------------------------------------------------------

INTERSTATE_LEG_SECONDS = {
    ("Delta", "Lagos"): 15 * 60,
    ("Delta", "Abuja"): 15 * 60,
}
