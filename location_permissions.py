"""
location_permissions.py

Who can SEE and WRITE where, on top of Discord's own role permissions.

Discord can only OR roles together ("Police Officer" OR "Lagos Employee"), and it has no idea
which state a player is in. locations.py has the full rules (AND/OR, per state), so the bot
applies them with personal (per-member) overwrites on the location channels:

  WRITE  Only the player's current parent location (players.current_sub_location) and the
         rooms inside it their roles allow. Everything else is read-only.
         -> personal "Send Messages: allow" on those channels.

  HIDE   Channels the player must not see are hidden with a personal "View Channel: deny":
           - every channel of a state they are NOT in (HIDE_OTHER_STATES), e.g. a Delta
             house resident who moved to Lagos can't still see Delta's channels;
           - inside their own state, channels whose full rules from locations.py they fail
             (HIDE_FAILED_ACCESS), e.g. has the job role but not "<State> Employee";
           - physical sub-locations whose parent the player isn't currently at
             (HIDE_NOT_AT_PARENT), e.g. banking-office/atm/deposit stay hidden until the
             player's current_sub_location is banking-hall, even if their roles would
             otherwise let them in. Parent locations are unaffected (always role-gated
             only). Only non_physical sub-locations are exempt from this gate (e.g.
             transaction-log, treasury) - those stay visible by role alone. Voice
             sub-locations (e.g. council-voice) are NOT exempt: they're gated like any
             other physical sub-location.
         A hide is only added where the player would otherwise be able to see the channel,
         so the number of overwrites stays small.

  HISTORY  Parent locations also get a personal "Read Message History: allow" for any
           player who passes the parent's own role rules (locations.py), regardless of
           current_sub_location - so someone who can see banking-hall but isn't there yet
           can still read what's already been posted; they still can't write there. Only
           parent locations get this grant, not sub-locations.

One-time Discord setup: deny Send Messages for @everyone and the state roles on every location
channel. Viewing stays role-gated exactly as you have it now. Personal overwrites beat role
overwrites, so the bot's allow/deny wins.

Always read-only, for everyone: locations flagged non_physical or voice_channel (arrival-terminal,
airport, council-voice, ...). The bot never grants write access to them (they can still be hidden).

Finding channels: the category name contains the state (e.g. "Delta Border & Entry") and the
channel name, minus any emoji/decoration in front, equals the location code. Voice channels
may drop the "-voice" suffix (the council voice channel is literally called "council").

Positions are two columns: current_state (Delta / Lagos / Abuja, changes when travelling) and
current_sub_location (the parent location inside that state, e.g. "banking-hall"). The rooms
inside a parent (the ATM, offices) are never stored — they open and close with it.

Every sync covers ALL three states, so nothing is left behind when a player travels or leaves.

Never touched: a member overwrite that explicitly DENIES send_messages (treated as a staff mute).
Members with Administrator (and the server owner) ignore channel permissions, so they're skipped.
The bot owns member-level "View Channel: deny" overwrites on location channels.
"""

import asyncio
import logging
import re

import discord

import database
import housing_threads

log = logging.getLogger("nvv.permissions")
from locations import LOCATIONS, STATES, guest_pass_needed, has_access

REASON = "Location permissions"

HIDE_OTHER_STATES = True     # can't see channels of a state you're not in
HIDE_FAILED_ACCESS = True    # in your own state, hide channels that fail the full role rules
HIDE_NOT_AT_PARENT = True    # hide physical sub-locations until you're at their parent
GRANT_HISTORY_AT_PARENT = True  # let players read a parent's backlog once they pass its role rules

_LEADING_DECORATION = re.compile(r"^[^a-z0-9]+")
_locks: dict[int, asyncio.Lock] = {}


# ---------------------------------------------------------------------------
# Pure helpers (no Discord calls)
# ---------------------------------------------------------------------------

def channel_code(channel_name):
    """'🏦┃banking-hall' -> 'banking-hall' (decoration in front is ignored)."""
    return _LEADING_DECORATION.sub("", channel_name.casefold())


def find_parent_location(state, location_code):
    """The parent-location dict for a code in a state, or None."""
    for category in LOCATIONS.get(state, {}).values():
        location = category["locations"].get(location_code)
        if location is not None:
            return location
    return None


def location_nodes(state):
    """{code: node} for every location and sub-location in a state."""
    nodes = {}
    for category in LOCATIONS.get(state, {}).values():
        for code, location in category["locations"].items():
            nodes[code] = location
            nodes.update(location["sub_locations"])
    return nodes


def _always_read_only(node):
    return node["non_physical"] or node["voice_channel"]


def _exempt_from_parent_gate(node):
    """
    Sub-locations that skip HIDE_NOT_AT_PARENT entirely (visible by role alone, no
    "arrive to reveal" gate). Only non_physical ones qualify now - voice_channel subs
    are gated like any other physical sub-location.
    """
    return node["non_physical"]


def sub_parent_map(state):
    """
    {sub_code: parent_code} for every sub-location in a state that HIDE_NOT_AT_PARENT
    applies to - i.e. every sub-location except the non_physical ones (see
    _exempt_from_parent_gate). Voice sub-locations ARE included: they're hidden until
    the player's current_sub_location is the parent, same as any other physical sub.
    """
    parents = {}
    for category in LOCATIONS.get(state, {}).values():
        for parent_code, location in category["locations"].items():
            for sub_code, sub in location["sub_locations"].items():
                if not _exempt_from_parent_gate(sub):
                    parents[sub_code] = parent_code
    return parents


def parent_codes(state):
    """Codes that are top-level (parent) locations in a state, not sub-locations."""
    codes = set()
    for category in LOCATIONS.get(state, {}).values():
        codes.update(category["locations"])
    return codes


def expected_codes(state):
    """Every location/sub-location code in a state (text and voice)."""
    return set(location_nodes(state))


def _voice_codes(state):
    return {code for code, node in location_nodes(state).items() if node["voice_channel"]}


def _in_state(channel, state):
    category = channel.category
    return category is not None and state.casefold() in category.name.casefold()


def state_location_channels(guild, state):
    """
    {location_code: channel} for one state, text and voice channels together.
    Category name must contain the state; the channel name (minus decoration) must be a
    known code. Text channels match text codes; voice channels match voice codes, with or
    without the "-voice" suffix.
    """
    all_codes = expected_codes(state)
    voice = _voice_codes(state)
    text = all_codes - voice
    found = {}

    for channel in guild.text_channels:
        code = channel_code(channel.name)
        if _in_state(channel, state) and code in text and code not in found:
            found[code] = channel

    for channel in guild.voice_channels:
        if not _in_state(channel, state):
            continue
        name = channel_code(channel.name)
        code = name if name in voice else (name + "-voice" if name + "-voice" in voice else None)
        if code and code not in found:
            found[code] = channel
    return found


async def may_see(role_names, player_id, state, node):
    """Does the player pass the FULL rules for this location (roles, plus guest pass if needed)?"""
    if not has_access(role_names, node["access"]):
        return False
    pass_type = guest_pass_needed(role_names, node["access"])
    if pass_type and not await database.has_guest_pass(player_id, state, pass_type):
        return False   # only in through a guest role, and no pass on record
    return True


async def writable_codes(member, player):
    """
    Location codes this player may write in right now: their current parent location plus
    its rooms they have access to. Non-physical and voice locations are never included.
    """
    state = player["current_state"]
    here = player["current_sub_location"]
    parent = find_parent_location(state, here) if state and here else None
    if parent is None:
        return set()

    role_names = [r.name for r in member.roles]
    nodes = [(here, parent), *parent["sub_locations"].items()]

    allowed = set()
    for code, node in nodes:
        if _always_read_only(node):
            continue
        if await may_see(role_names, player["player_id"], state, node):
            allowed.add(code)
    return allowed


# ---------------------------------------------------------------------------
# Applying permissions
# ---------------------------------------------------------------------------

async def _apply(channel, member, write, hide, history=False):
    """
    Make the member's personal overwrite on a channel match the wanted state.
      write    True -> personal Send Messages + Send Messages in Threads allow; False -> remove them
      hide     True -> personal View Channel deny (only if they'd otherwise see it); False -> remove it
      history  True -> personal Read Message History allow; False -> remove it
    Returns True if anything changed.
    """
    overwrite = channel.overwrites_for(member)
    changed = False

    if overwrite.send_messages is not False:          # explicit member-level deny (mute): leave alone
        wanted = True if write else None
        if overwrite.send_messages != wanted:
            overwrite.send_messages = wanted
            changed = True

    if overwrite.send_messages_in_threads is not False:  # explicit member-level deny (mute): leave alone
        wanted = True if write else None
        if overwrite.send_messages_in_threads != wanted:
            overwrite.send_messages_in_threads = wanted
            changed = True

    if overwrite.read_message_history is not False:   # explicit member-level deny: leave alone
        wanted = True if history else None
        if overwrite.read_message_history != wanted:
            overwrite.read_message_history = wanted
            changed = True

    if hide:
        if overwrite.view_channel is not False and channel.permissions_for(member).view_channel:
            overwrite.view_channel = False
            changed = True
    elif overwrite.view_channel is False:
        overwrite.view_channel = None
        changed = True

    if not changed:
        return False
    if overwrite.is_empty():
        await channel.set_permissions(member, overwrite=None, reason=REASON)
    else:
        await channel.set_permissions(member, overwrite=overwrite, reason=REASON)
    return True


def _lock_for(member_id):
    return _locks.setdefault(member_id, asyncio.Lock())


async def sync_member_permissions(member):
    """
    Bring a member's write/hide overwrites in line with their current state, location and
    roles, across ALL three states. Safe to call any time (API calls only where something
    differs). Does nothing for members without a player record, and skips administrators.
    Call it whenever roles, current_state or current_sub_location change.
    """
    if member.guild_permissions.administrator:
        return

    async with _lock_for(member.id):
        player = await database.get_player_by_discord_id(member.id)
        if not player or not player["current_state"]:
            return

        here_state = player["current_state"]
        here_sub_location = player["current_sub_location"]
        role_names = [r.name for r in member.roles]
        writable = await writable_codes(member, player)

        for state in STATES:
            nodes = location_nodes(state)
            parents = sub_parent_map(state) if state == here_state else None
            top_level = parent_codes(state) if state == here_state else None
            for code, channel in state_location_channels(member.guild, state).items():
                if state == here_state:
                    write = code in writable
                    role_access = await may_see(role_names, player["player_id"], state, nodes[code])
                    fails_role = HIDE_FAILED_ACCESS and not role_access
                    not_at_parent = (
                        HIDE_NOT_AT_PARENT
                        and parents.get(code) is not None
                        and here_sub_location != parents[code]
                    )
                    hide = fails_role or not_at_parent
                    history = GRANT_HISTORY_AT_PARENT and code in top_level and role_access
                else:
                    write = False
                    hide = HIDE_OTHER_STATES
                    history = False
                try:
                    await _apply(channel, member, write, hide, history)
                except discord.HTTPException as exc:
                    print(f"[permissions] Couldn't update #{channel.name} for {member}: {exc}")

        try:
            await housing_threads.sync_house_locks(member.guild, player)
        except Exception:
            # Was `except discord.HTTPException` only — anything else (bad
            # house_type lookup, a DB error, etc.) used to propagate silently
            # up to the caller instead of showing up anywhere. Log it with a
            # full traceback so a failed thread sync is visible.
            log.exception("Couldn't sync house threads for %s", member)


async def clear_member_permissions(member):
    """Remove every personal write/hide overwrite the bot may have set (e.g. on leaving the server)."""
    async with _lock_for(member.id):
        for state in STATES:
            for channel in state_location_channels(member.guild, state).values():
                try:
                    await _apply(channel, member, write=False, hide=False, history=False)
                except discord.HTTPException as exc:
                    print(f"[permissions] Couldn't clear #{channel.name} for {member}: {exc}")
    _locks.pop(member.id, None)


# ---------------------------------------------------------------------------
# Audit (for confirming the setup works)
# ---------------------------------------------------------------------------

async def audit_member(member, player):
    """
    What Discord itself says this member can do right now. Returns a dict:
        writable        codes (in their current state) where they can send messages
        locked          number of their state's text channels where they can't
        leaked          "State/code" for channels of OTHER states they can still see (should be empty)
        rule_failures   codes in their own state they can see but fail locations.py rules for
                        (should be empty when HIDE_FAILED_ACCESS is on)
        hidden_wrongly  codes in their own state they PASS the rules for but can't see (Discord's
                        role permissions don't show it, or a personal hide is in the way)
        missing         {state: [codes with no matching channel in the server]}
    """
    here_state = player["current_state"]
    here_sub_location = player["current_sub_location"]
    role_names = [r.name for r in member.roles]
    result = {"writable": [], "locked": 0, "leaked": [], "rule_failures": [],
              "hidden_wrongly": [], "missing": {}}

    for state in STATES:
        channels = state_location_channels(member.guild, state)
        nodes = location_nodes(state)
        parents = sub_parent_map(state) if state == here_state else None
        result["missing"][state] = sorted(set(nodes) - set(channels))

        for code, channel in sorted(channels.items()):
            perms = channel.permissions_for(member)
            if state != here_state:
                if perms.view_channel:
                    result["leaked"].append(f"{state}/{code}")
                continue
            if nodes[code]["voice_channel"]:
                pass                                   # voice channels aren't counted as writable/locked
            elif perms.send_messages:
                result["writable"].append(code)
            else:
                result["locked"] += 1
            allowed = await may_see(role_names, player["player_id"], state, nodes[code])
            if allowed and HIDE_NOT_AT_PARENT and parents.get(code) is not None \
                    and here_sub_location != parents[code]:
                allowed = False                         # correctly hidden: not at its parent yet
            if perms.view_channel and not allowed:
                result["rule_failures"].append(code)
            elif allowed and not perms.view_channel:
                result["hidden_wrongly"].append(code)
    return result
