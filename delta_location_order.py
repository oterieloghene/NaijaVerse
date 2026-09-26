"""
delta_location_order.py

The single source of truth for the REAL physical north-to-south order of
every parent location in Delta — not just the 17 places a keke actually
stops at (keke_config.STOP_CODES), but all 33, including the ones a keke
jumps over.

This does NOT come from locations.py's category order — that's just the
order categories happened to be typed in the file, and it does NOT track
geography. Proof: property_and_development (rental-desk) is defined near
the END of the category dict, but rental-desk is physically the FIRST stop
of Central zone B on the real spine. Same story for university
(administrative-block), defined dead last, despite being stop #1.

It also does NOT come from keke_config.ZONE_HUB — that's only where a keke
arbitrarily drops a passenger closest to an exempt destination, not where
that destination actually is.

The real order is locations_codenames.csv's own row order — the CSV rows
are laid out geographically, north to south, exactly like STOP_CODES but
with every exempt location slotted into its real position instead of
omitted. Rural District leads (row 2, right after row 1's header), Mid-Class
Residential (miniflat/2bedroom/3bedroom) sits between Banking Hall and Help
Desk — the START of zone C, not the end — and Industrial District/Oil Well
close out the whole spine after Commercial District. This list is just that
CSV order, converted from codename to location code.

Used by trek_config.py to build the walking path between two Delta parent
locations. If Delta's geography ever needs correcting again, THIS is the
file to fix — nothing else should be hand-deriving location order.
"""

DELTA_LOCATION_ORDER = [
    # --- Zone A: North -------------------------------------------------
    "rural-district",
    "administrative-block",
    "bed-sitter",
    "line-houses",
    "immigration-office",

    # --- Zone B: Central -------------------------------------------------
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

    # --- Zone C: South -------------------------------------------------
    "mini-flat",                    # Mid-Class Residential — starts zone C
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
    "industrial-district",          # closes out the whole spine
    "oil-well",
]

DELTA_LOCATION_INDEX = {code: i for i, code in enumerate(DELTA_LOCATION_ORDER)}
