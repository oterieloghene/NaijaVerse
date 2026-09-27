"""
abuja_location_order.py

The single source of truth for the REAL physical order of every parent
location in Abuja — not just the 17 places a korope actually stops at
(bus_config.STOP_CODES), but all 36, including the ones a korope jumps
over. Mirrors delta_location_order.py's role for trek exactly.

Zoning is IDENTICAL to Delta's — North, then Central, then South, same
physical order, no reordering (unlike Lagos). Within each zone, the real
stops and exempt destinations are in the same relative order as
locations_codenames_abuja.csv (which the CSV itself follows for the same
reason Delta's does — it's already laid out geographically): North and
South are unchanged from Delta's own content (South's two state-specific
businesses, Mining Ground and Kanji Dam, replace Delta's single Oil Well).
Central differs the most — Abuja has no Governor, so Delta's
governor-penthouse/governor-office/deputy-governor-office are absent;
in their place: Aso Rock's four locations, FCT Minister, and the Central
Bank, all confirmed by the user. Supreme Court is NOT a separate location —
it shares Clerk Office's channel, so it never appears here.

Used by trek_config.py to build the walking path between two Abuja parent
locations.
"""

ABUJA_LOCATION_ORDER = [
    # --- Abuja North (5) --------------------------------------------------
    "rural-district",
    "administrative-block",
    "bed-sitter",
    "line-houses",
    "immigration-office",

    # --- Abuja Central (17) ------------------------------------------------
    "rental-desk",
    "police-station",
    "clerk-office",              # Judiciary — Supreme Court shares this channel
    "hotel-reception",
    "private-estate",            # High-Class Residential
    "luxury-duplex",
    "penthouse",
    "fct-minister",              # replaces Delta's Governor Penthouse/Office
    "state-secretariat",
    "council",
    "city-hall",
    "chief-of-staff",            # Aso Rock's, not state_government's (Abuja has none there)
    "president-office",          # Aso Rock
    "vice-president-office",     # Aso Rock
    "president-villa",           # Aso Rock
    "cbn-lobby",                 # Central Bank of Nigeria
    "banking-hall",              # turnaround for the A<->B loop

    # --- Abuja South (14) ---------------------------------------------------
    "mini-flat",                  # Mid-Class Residential — starts South
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
    "mining-ground",              # Abuja's two state-specific businesses,
    "kanji-dam",                  # replacing Delta's single Oil Well
]

assert len(ABUJA_LOCATION_ORDER) == 36

ABUJA_LOCATION_INDEX = {code: i for i, code in enumerate(ABUJA_LOCATION_ORDER)}
