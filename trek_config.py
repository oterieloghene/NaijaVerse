"""
trek_config.py

On-foot travel (!walk / !trek) — Delta only for now.

There is no existing "every parent location, in geographic order" list for a
state — keke_config.STOP_CODES only covers the 17 stops a *keke* actually
stops at. A pedestrian isn't limited to the keke route, so a trekker needs
the FULL walking order: those same 17 stops, with the 16 "exempt" locations
(the ones a keke jumps over) slotted in next to the hub keke_config already
says they're closest to (EXEMPT_ZONES / ZONE_HUB).

This does NOT reuse keke's hub-dropoff behaviour — a trekker always walks
all the way to their real destination, never a substitute hub. The zone/hub
data is only borrowed here to work out physical ORDER, i.e. how many
locations lie between two points and which channels lie on that path.

Source of truth for names/order: locations_codenames.csv (via keke_config),
so this never drifts out of sync with the approved codename list.
"""

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


def _build_full_walk_order():
    """
    The 17 keke stops, in order, with each zone's exempt (off-spine)
    locations inserted right after that zone's hub stop, in the order they
    appear in locations_codenames.csv.
    """
    zone_words = {zone: [] for zone in kc.ZONE_HUB}
    for cmd in kc.CODENAMES:                      # dict preserves CSV row order
        word = cmd[len("!keke "):] if cmd.startswith("!keke ") else cmd
        if word in kc.EXEMPT_CODES:
            zone_words[kc.EXEMPT_ZONES[word]].append(word)

    order = []
    for stop_code in kc.STOP_CODES:
        order.append(stop_code)
        for zone, hub_code in kc.ZONE_HUB.items():
            if stop_code == hub_code:
                for word in zone_words[zone]:
                    dest = kc.codename_dest(f"!keke {word}")
                    if dest is not None:
                        order.append(dest[1])     # location_code
    return order


FULL_WALK_ORDER = _build_full_walk_order()
_WALK_INDEX = {code: i for i, code in enumerate(FULL_WALK_ORDER)}

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
