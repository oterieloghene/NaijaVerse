"""
trek_config.py

On-foot travel (!walk / !trek) — one state at a time, per the player's
current_state. Every state trek supports needs two things registered below:
  1. its real, manually confirmed full location order (all parent locations,
     not just the ones its transport system stops at), and
  2. how to resolve a bare codename to a real destination in that state.

Walking order does NOT come from a transport system's hub-dropoff config
(keke_config.ZONE_HUB / danfo_config.ZONE_HUB): those are only where a
keke/danfo arbitrarily drops a passenger closest to an exempt destination,
not where that destination actually is. See delta_location_order.py and
lagos_location_order.py for the real order and how each was worked out.

This does NOT reuse a transport system's hub-dropoff behaviour either way —
a trekker always walks all the way to their real destination, never a
substitute hub. There is no exempt-channel handling in trek at all.

Full role-gating (locations.has_access) plus the guest-pass check runs the
same as everywhere else in the game — trek does not relax or bypass access
in any state. Trek always operates within a single state; a player can only
!trek/!walk to a destination in the state they are currently in.

Source of truth for codenames: locations_codenames.csv (Delta, via
keke_config) and locations_codenames_lagos.csv (Lagos, via danfo_config).
"""

import danfo_config as dc
import delta_location_order as dlo
import keke_config as kc
import lagos_location_order as llo

# Longer than a keke/danfo's dwell (60s): 70s per location hop while trekking.
HOP_SECONDS = 70

# Post a "just walked past here" notice every 2 hops (not every hop — the
# trekker still passes through every location in between, they just aren't
# announced individually).
ANNOUNCE_EVERY_N_HOPS = 2

WALKED_PAST_TEMPLATE = "{name} just walked past here..."
ARRIVED_TEMPLATE = "🚶 {mention} has arrived at {destination}."
DEPARTED_TEMPLATE = "🚶 {name} set out trekking to {destination}. ETA: {eta}."

# Per-state registry. Adding a new state to trek means adding one entry here
# (its own <state>_location_order.py, plus how to resolve its codenames) —
# nothing else in this module or cogs/trek.py should need to change.
STATE_WALK_ORDERS = {
    "Delta": dlo.DELTA_LOCATION_ORDER,
    "Lagos": llo.LAGOS_LOCATION_ORDER,
}
STATE_WALK_INDEX = {
    state: {code: i for i, code in enumerate(order)}
    for state, order in STATE_WALK_ORDERS.items()
}

# Each state's codename table lives in its own transport config, keyed by
# its own command prefix (Delta's !keke, Lagos's !danfo) — trek just reuses
# whichever table matches the player's current state.
_STATE_CODENAME_RESOLVER = {
    "Delta": lambda word: kc.codename_dest(f"!keke {word}"),
    "Lagos": lambda word: dc.codename_dest(f"!danfo {word}"),
}

assert len(dlo.DELTA_LOCATION_ORDER) == len(kc.CODENAMES), (
    "Delta trek walk order missing/duplicating a codename destination"
)
assert len(llo.LAGOS_LOCATION_ORDER) == len(dc.CODENAMES), (
    "Lagos trek walk order missing/duplicating a codename destination"
)


def supported(state):
    """Whether trek has a walk order + codename table registered for `state`."""
    return state in STATE_WALK_ORDERS


def path_between(state, from_code, to_code):
    """
    Ordered list of parent-location codes from from_code to to_code
    (inclusive), walking STATE_WALK_ORDERS[state] — no skipping, no doubling
    back. Returns None if `state` isn't registered or either code isn't a
    known parent location in that state.
    """
    index = STATE_WALK_INDEX.get(state)
    order = STATE_WALK_ORDERS.get(state)
    if index is None or from_code not in index or to_code not in index:
        return None
    start, end = index[from_code], index[to_code]
    step = 1 if end >= start else -1
    return [order[i] for i in range(start, end + step, step)]


def resolve_codename(state, word):
    """
    Bare word typed after !walk/!trek -> (category_code, location_code,
    parent_name), or None if `state` isn't registered or the word is
    unknown. Reuses that state's transport codename table directly — the
    same approved code names, same real destination (no hub swap).
    """
    resolver = _STATE_CODENAME_RESOLVER.get(state)
    if resolver is None:
        return None
    return resolver(word)
