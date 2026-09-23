"""Keke rules — 21st-pass spec (keke_notes.md) locked in tests.

Covers: the 17-stop effective spine order, zone boundaries (A = stops
0-3, B = stops 4-8, C = stops 9-16), route stop spans and one-way km,
fuel/tank constants, the ₦100-per-km fare with its ₦100 minimum, and the
exempt-codename hub drop-off table.
"""
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

import keke_config as kc
from cogs import keke as cog

# Effective spine, north -> south (notes 1.7a, 21st pass):
# Administrative Office, Bed-Sitter, Line Houses, Immigration Office,
# Rental Desk, Police Station, Clerk Office, Hotel Reception, Banking Hall,
# Help Desk, Hospital Lobby, School Building, Broadcasting Station,
# Market District, Automotive District, Transport District, Commercial District
EXPECTED_SPINE = [
    "administrative-office",
    "bed-sitter",
    "line-houses",
    "immigration-office",
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
    "automotive-district",
    "transport-district",
    "commercial-district",
]


# ---------------------------------------------------------------------------
# Spine order + geometry
# ---------------------------------------------------------------------------

def test_effective_spine_order():
    assert kc.STOP_CODES == EXPECTED_SPINE, (
        "spine order drifted from the 21st-pass note"
    )
    assert len(kc.STOP_CODES) == 17


def test_spine_geometry():
    assert kc.SEGMENTS == 16
    assert kc.TOTAL_KM == 30.0
    assert kc.KM_PER_SEGMENT == 30.0 / 16 == 1.875


def test_fuel_and_tank():
    assert kc.TANK_CAPACITY_L == 30
    assert kc.FUEL_PER_KM == 0.25
    # A full C<->A round trip (60 km) burns exactly HALF the tank.
    assert kc.RANGE_KM == kc.TANK_CAPACITY_L / kc.FUEL_PER_KM == 120.0
    round_trip_ca_km = 2 * kc.ONE_WAY_KM["CA"]
    assert round_trip_ca_km * kc.FUEL_PER_KM == 15.0 == kc.TANK_CAPACITY_L / 2


# ---------------------------------------------------------------------------
# Zone boundaries (notes 2)
# ---------------------------------------------------------------------------

def test_zone_boundaries_by_spine_position():
    expected = ["A"] * 4 + ["B"] * 5 + ["C"] * 8
    assert [cog._stop_zone(code) for code in kc.STOP_CODES] == expected


def test_zone_names():
    assert kc.ZONES == {"A": "North", "B": "Central", "C": "South"}


# ---------------------------------------------------------------------------
# Routes (notes 1.7a)
# ---------------------------------------------------------------------------

def test_route_stops_match_spine_slices():
    # Each loop covers its two zones in full, so the spans share B's five stops.
    assert kc.ROUTES["AB"]["stop_codes"] == EXPECTED_SPINE[0:9]   # A(4) + B(5)
    assert kc.ROUTES["BC"]["stop_codes"] == EXPECTED_SPINE[4:17]  # B(5) + C(8)
    assert kc.ROUTES["CA"]["stop_codes"] == EXPECTED_SPINE        # all 17


def test_route_one_way_km_equals_segments_times_1_875():
    for name, route in kc.ROUTES.items():
        segments = len(route["stop_codes"]) - 1
        assert route["one_way_km"] == segments * kc.KM_PER_SEGMENT, name
    assert kc.ONE_WAY_KM == {"AB": 15.0, "BC": 22.5, "CA": 30.0}
    assert kc.ROUTES["AB"]["one_way_km"] == 15.0
    assert kc.ROUTES["BC"]["one_way_km"] == 22.5
    assert kc.ROUTES["CA"]["one_way_km"] == 30.0


def test_zone_route_map():
    assert cog.ZONE_ROUTE == {"A": "AB", "B": "BC", "C": "CA"}


# ---------------------------------------------------------------------------
# Fares (notes 1.5 / Q9)
# ---------------------------------------------------------------------------

def test_fare_minimum_floor():
    # "If the distance the player travels is less than 1km, it's still the same 100"
    assert kc.fare_for(0) == 100
    assert kc.fare_for(0.1) == 100
    assert kc.fare_for(0.9) == 100


def test_fare_rounds_up():
    # Distance is CEILED to the next whole km, THEN multiplied by ₦100
    # (notes Q9: 3.75 km -> ceil 4 -> ₦400, NOT ₦375).
    assert kc.fare_for(1.0) == 100
    assert kc.fare_for(1.01) == 200
    assert kc.fare_for(1.5) == 200
    assert kc.fare_for(1.875) == 200   # one spine segment
    assert kc.fare_for(2.0) == 200
    assert kc.fare_for(2.01) == 300
    assert kc.fare_for(3.75) == 400    # the note's own example


def test_fare_full_route_oneways():
    assert kc.fare_for(15.0) == 1500   # A<->B one way
    assert kc.fare_for(22.5) == 2300   # B<->C one way (ceil 23)
    assert kc.fare_for(30.0) == 3000   # C<->A one way


def test_fare_never_below_minimum_even_for_small_positive_km():
    for km in (0.001, 0.4, 0.999):
        assert kc.fare_for(km) == kc.MIN_FARE, km


# ---------------------------------------------------------------------------
# Codenames / exempt hub drop-off (notes 1.6, 1.8, 21st pass)
# ---------------------------------------------------------------------------

def test_exempt_codes_are_known_codenames():
    assert len(kc.EXEMPT_CODES) == 16
    for word in kc.EXEMPT_CODES:
        assert kc.codename_dest(f"!keke {word}") is not None, word


def test_exempt_zones_cover_all_exempt_codes():
    assert set(kc.EXEMPT_ZONES) == kc.EXEMPT_CODES
    for word, zone in kc.EXEMPT_ZONES.items():
        assert zone in ("A", "B", "C"), word


def test_exempt_zone_matches_parent_stop_zone():
    """Every exempt codename's parent channel is NOT a spine stop (it is
    jumped over), so its configured zone is the authoritative zone and must
    match the zone of its hub drop-off."""
    for word in kc.EXEMPT_CODES:
        cat, loc, _parent = kc.codename_dest(f"!keke {word}")
        assert loc not in kc.STOP_INDEX, word
        assert kc.EXEMPT_ZONES[word] == cog._stop_zone(kc.ZONE_HUB[kc.EXEMPT_ZONES[word]]), word


def test_zone_hubs():
    assert kc.ZONE_HUB == {
        "A": "line-houses",          # north exception drop-off
        "B": "hotel-reception",      # central exception drop-off
        "C": "transport-district",   # south exception drop-off
    }
    for zone, hub in kc.ZONE_HUB.items():
        assert hub in kc.STOP_INDEX, hub
        assert cog._stop_zone(hub) == zone, hub


def test_effective_destination_exempt_goes_to_hub():
    for word in kc.EXEMPT_CODES:
        zone = kc.EXEMPT_ZONES[word]
        hub = kc.ZONE_HUB[zone]
        cat, loc, parent, hub_dropoff = cog._effective_destination(word)
        assert hub_dropoff is True, word
        assert loc == hub, word
        assert cog._stop_zone(hub) == zone, word


def test_effective_destination_nonexempt_stays_on_parent():
    # !keke university -> Administrative Office (stop, north end)
    cat, loc, parent, hub_dropoff = cog._effective_destination("university")
    assert hub_dropoff is False
    assert loc == "administrative-office"
    assert parent == "Administrative Office"


def test_effective_destination_unknown_word():
    assert cog._effective_destination("not-a-real-destination") is None


def test_hub_fares():
    # Fare to a hub = fare for the km from boarding to the hub stop;
    # e.g. a CA keke boarding at administrative-office to the south hub
    # (transport-district, index 15) travels 15 segments = 28.125 km ->
    # ceil 29 -> ₦2900 under the whole-km rounding rule.
    segments = kc.STOP_INDEX["transport-district"] - kc.STOP_INDEX["administrative-office"]
    km = segments * kc.KM_PER_SEGMENT
    assert km == 28.125
    assert kc.fare_for(km) == max(kc.MIN_FARE, math.ceil(km) * kc.FARE_PER_KM) == 2900