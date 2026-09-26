"""
trek_config.py

On-foot travel (!walk / !trek) — Delta only for now.

Walking order comes from delta_location_order.py — the real, manually
confirmed north-to-south geography of all 33 Delta parent locations. It does
NOT come from keke_config.ZONE_HUB: that's only where a keke arbitrarily
drops a passenger closest to an exempt destination, not where that
destination actually is (see delta_location_order.py for why that matters
and how each zone's real order was worked out).

This does NOT reuse keke's hub-dropoff behaviour either way — a trekker
always walks all the way to their real destination, never a substitute hub.

Source of truth for codenames: locations_codenames.csv (via keke_config).
"""

import delta_location_order as dlo
import keke_config as kc

STATE = "Delta"

# Longer than a keke's dwell (60s): 70s per location hop while trekking.
HOP_SECONDS = 70

# Post a "just walked past here" notice every 2 hops (not every hop — the
# trekker still passes through every location in between, they just aren't
# announced individually).
ANNOUNCE_EVERY_N_HOPS = 2

WALKED_PAST_TEMPLATE = "{name} just walked past here..."
ARRIVED_TEMPLATE = "🚶 {mention} has arrived at {destination}."
DEPARTED_TEMPLATE = "🚶 {name} set out trekking to {destination}. ETA: {eta}."

FULL_WALK_ORDER = dlo.DELTA_LOCATION_ORDER
_WALK_INDEX = dlo.DELTA_LOCATION_INDEX

assert len(FULL_WALK_ORDER) == len(kc.CODENAMES), (
    "trek walk order missing/duplicating a codename destination"
)


def path_between(from_code, to_code):
    """
    Ordered list of parent-location codes from from_code to to_code
    (inclusive), walking FULL_WALK_ORDER — no skipping, no doubling back.
    Returns None if either code isn't a known Delta parent location.
    """
    if from_code not in _WALK_INDEX or to_code not in _WALK_INDEX:
        return None
    start, end = _WALK_INDEX[from_code], _WALK_INDEX[to_code]
    step = 1 if end >= start else -1
    return [FULL_WALK_ORDER[i] for i in range(start, end + step, step)]


def resolve_codename(word):
    """
    Bare word typed after !walk/!trek -> (category_code, location_code,
    parent_name), or None if unknown. Reuses keke's codename table directly —
    the same approved code names, same real destination (no hub swap).
    """
    return kc.codename_dest(f"!keke {word}")
