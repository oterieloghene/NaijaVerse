"""
lagos_location_order.py

The single source of truth for the REAL physical order of every parent
location in Lagos — not just the 17 places a danfo actually stops at
(danfo_config.STOP_CODES), but all 33, including the ones a danfo jumps
over. Mirrors delta_location_order.py's role for trek exactly.

Order confirmed by the user (2026-09-27): Lagos reads Island, then
Mainland, then Ghetto — Island at the top of the spine, Mainland in the
middle, Ghetto at the bottom (see the geography note above
danfo_config.ROUTES). Within each zone, the real stops and their exempt
destinations sit in the same relative order as Delta's matching content
block (Island's content = Delta Central's block, Mainland's content =
Delta South's block with oil-well -> sea-port, Ghetto's content = Delta
North's block) — only the top-to-bottom sequence of the three blocks
differs from Delta.

Used by trek_config.py to build the walking path between two Lagos parent
locations. If Lagos's geography ever needs correcting again, THIS is the
file to fix — nothing else should be hand-deriving location order.
"""

LAGOS_LOCATION_ORDER = [
    # --- Lagos Island (top) ---------------------------------------------
    "rental-desk",
    "police-station",
    "clerk-office",                # Judiciary
    "hotel-reception",
    "private-estate",              # High-Class Residential
    "luxury-duplex",
    "penthouse",
    "governor-penthouse",          # Governor's House
    "governor-office",             # State Government
    "deputy-governor-office",
    "chief-of-staff",
    "state-secretariat",
    "council",
    "city-hall",
    "banking-hall",

    # --- Lagos Mainland (middle) -----------------------------------------
    "mini-flat",                    # Mid-Class Residential — starts Mainland
    "two-bedroom-flat",
    "three-bedroom-flat",
    "help-desk",
    "hospital-lobby",
    "school-building",
    "broadcasting-station",
    "market-district",
    "automotive-district",
    "transport-district",
    "commercial-district",
    "industrial-district",
    "sea-port",                     # closes out Mainland (Lagos's own business stop)

    # --- Lagos Ghetto (bottom) -------------------------------------------
    "rural-district",
    "administrative-block",
    "bed-sitter",
    "line-houses",
    "immigration-office",
]

assert len(LAGOS_LOCATION_ORDER) == 33

LAGOS_LOCATION_INDEX = {code: i for i, code in enumerate(LAGOS_LOCATION_ORDER)}
