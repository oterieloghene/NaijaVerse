"""
housing_threads.py

All the Discord-side work for house threads: creating a house's private and
shared threads on !assignhouse, deleting/untagging on !evicthouse, and
archiving-on-leave / unarchiving-on-arrival so 500 players' worth of houses
don't blow past Discord's 1,000-active-thread server cap.

sync_house_locks() is called from location_permissions.sync_member_permissions
(not the other way around, to avoid a circular import) every time a player's
role/location sync runs, so house threads stay in step with the same
current_state / current_sub_location the rest of the location system uses.

Private threads: archived+locked while the resident isn't standing in that
house, unarchived+unlocked while they are — only that one resident matters.

Shared threads (Face Me I Face You, line-house Bathroom, Swimming Pool):
archived+locked only when NO resident of that house type/state is currently
home; stay unarchived if even one is.

Discord requires Boost Level 2 for a server to create private threads at
all — if that's not on, thread creation will fail with an HTTPException,
which the caller (cogs/housing.py) surfaces to the officer.
"""

import discord

import housing_config as hcfg
import housing_database as hdb

AUTO_ARCHIVE = 10080  # 7 days — the longest Discord allows


async def _get_thread(guild, thread_id):
    thread = guild.get_thread(thread_id)
    if thread is not None:
        return thread
    try:
        channel = await guild.fetch_channel(thread_id)
    except discord.NotFound:
        return None
    return channel if isinstance(channel, discord.Thread) else None


async def _create_thread(house_channel, name):
    return await house_channel.create_thread(
        name=name, type=discord.ChannelType.private_thread, invitable=False,
        auto_archive_duration=AUTO_ARCHIVE, reason="Housing assignment",
    )


async def _identify(thread, display_name):
    message = await thread.send(f"🔑 Assigned to {display_name}.")
    try:
        await message.pin()
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------

async def create_house(house_channel, member, display_name, state, house_type, assignment_id):
    """
    Create/reuse every thread a new assignment needs, tag the resident into
    all of them, and record the ids. Returns {room_key: Thread} for the
    private rooms (so the caller can post the receipt into the parlour).
    """
    rooms = hcfg.HOUSE_ROOMS[house_type]
    private_threads = {}

    for room_key, room_label in rooms["private"]:
        name = hcfg.private_thread_name(room_key, room_label, display_name)
        thread = await _create_thread(house_channel, name)
        await thread.add_user(member)
        if room_key not in hcfg.NAMED_ROOM_KEYS:
            await _identify(thread, display_name)
        await hdb.add_private_thread(assignment_id, room_key, thread.id)
        private_threads[room_key] = thread

    for shared_key in rooms["shared"]:
        thread_id = await hdb.get_shared_thread(state, house_type, shared_key)
        thread = await _get_thread(house_channel.guild, thread_id) if thread_id else None
        if thread is None:
            thread = await _create_thread(house_channel, hcfg.shared_thread_name(shared_key))
            await hdb.set_shared_thread(state, house_type, shared_key, thread.id)
        await thread.add_user(member)

    return private_threads


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------

async def delete_private_threads(guild, assignment_id):
    for row in await hdb.get_private_threads(assignment_id):
        thread = await _get_thread(guild, row["thread_id"])
        if thread is not None:
            try:
                await thread.delete()
            except discord.HTTPException:
                pass


async def untag_shared_threads(guild, member, state, house_type):
    for shared_key in hcfg.HOUSE_ROOMS[house_type]["shared"]:
        thread_id = await hdb.get_shared_thread(state, house_type, shared_key)
        if not thread_id:
            continue
        thread = await _get_thread(guild, thread_id)
        if thread is not None:
            try:
                await thread.remove_user(member)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Archive-on-leave / unarchive-on-arrival
# ---------------------------------------------------------------------------

async def _set_locked(guild, thread_id, locked):
    thread = await _get_thread(guild, thread_id)
    if thread is None:
        return
    wanted_archived, wanted_locked = locked, locked
    if thread.archived == wanted_archived and thread.locked == wanted_locked:
        return
    try:
        await thread.edit(archived=wanted_archived, locked=wanted_locked, reason="Housing sync")
    except discord.HTTPException:
        pass


async def sync_house_locks(guild, player):
    """Call whenever a player's current_state/current_sub_location/roles are synced."""
    for row in await hdb.get_assignments_for_player(player["player_id"]):
        state, house_type = row["state"], row["house_type"]
        rooms = hcfg.HOUSE_ROOMS.get(house_type)
        if rooms is None:
            continue
        home = player["current_state"] == state and player["current_sub_location"] == house_type

        for prow in await hdb.get_private_threads(row["assignment_id"]):
            await _set_locked(guild, prow["thread_id"], locked=not home)

        for shared_key in rooms["shared"]:
            thread_id = await hdb.get_shared_thread(state, house_type, shared_key)
            if not thread_id:
                continue
            anyone_home = home or await hdb.anyone_home(state, house_type, player["player_id"])
            await _set_locked(guild, thread_id, locked=not anyone_home)
