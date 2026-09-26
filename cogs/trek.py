"""
cogs/trek.py

!walk <codename> / !trek <codename> — on-foot travel, Delta only for now.

Rules
-----
- Must be typed in the channel matching the player's CURRENT parent location
  (players.current_sub_location) — you trek FROM where you actually are.
- Destination is one of the approved code names (locations_codenames.csv,
  via trek_config.resolve_codename / keke_config), resolved to its REAL
  location — unlike keke, there is no hub-substitution for exempt channels;
  a trekker walks all the way there.
- Full role-gating check (locations.has_access) plus the Governor's House
  guest-pass check (locations.guest_pass_needed + database.has_guest_pass),
  same as every other access check in the game.
- Travel takes trek_config.HOP_SECONDS (70s) per location hop on the walking
  path (trek_config.path_between). Every ANNOUNCE_EVERY_N_HOPS (2) hops, a
  silent, no-ping "<name> just walked past here..." notice is posted in that
  intermediate channel — the player still passes every location in between,
  only the notice is skipped on the in-between hop.
- Same permission handling as keke boarding: the departure channel AND every
  room inside it the player could otherwise write in are locked (personal
  Send Messages: deny) for the whole walk, and lifted on arrival. Locks are
  recorded in trek_database so a bot restart mid-walk doesn't leave anyone
  muted forever.
- On arrival, current_sub_location is updated and channel permissions are
  re-synced, same as keke drop-off.

Reused verbatim from keke (cogs/keke.py) so the two systems feel consistent:
  "invalid destination ❌"
  "Not accessible❗You need gate pass 🪪"
"""

import asyncio
import logging

import discord
from discord.ext import commands

import database
import location_permissions as perms
import locations
import trek_config as tc
import trek_database as tdb

log = logging.getLogger("nvv.trek")

MSG_INVALID_DEST = "invalid destination ❌"
MSG_GATE_PASS = "Not accessible❗You need gate pass 🪪"
MSG_NO_PLAYER_RECORD = "Not authorised! Meet @immigrationofficer for help❗"
MSG_WRONG_STATE = "🚧 Trekking hasn't been set up outside Delta yet."
MSG_NOT_AT_ORIGIN = "You have to `!trek`/`!walk` from the location you're currently in."
MSG_ALREADY_THERE = "You're already there."
MSG_ALREADY_TREKKING = "You're already on a trek — wait until you arrive."


def _fmt_eta(seconds):
    minutes, secs = divmod(seconds, 60)
    if minutes and secs:
        return f"{minutes}m{secs}s"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


async def _send_silent(channel, content):
    """Post without pinging or notifying — same convention as keke's status blocks."""
    quiet = discord.AllowedMentions.none()
    try:
        return await channel.send(content, silent=True, allowed_mentions=quiet)
    except TypeError:
        return await channel.send(content, allowed_mentions=quiet)


async def _fetch_member(channel, member_id):
    """The guild Member (not just a User) for `member_id` in channel's guild."""
    if channel is None:
        return None
    guild = channel.guild
    member = guild.get_member(member_id)
    if member is None:
        try:
            member = await guild.fetch_member(member_id)
        except discord.HTTPException:
            member = None
    return member


async def _unlock_write(channel, member):
    """
    Remove a personal send-deny WE placed on a channel. Mirrors keke's
    _unlock_write: drop the overwrite entirely once nothing else is in it.
    Returns True if the lock is gone or was never there.
    """
    if channel is None or member is None:
        return False
    try:
        overwrite = channel.overwrites_for(member)
        if overwrite.send_messages is not False:
            return True
        overwrite.send_messages = None
        if overwrite.is_empty():
            await channel.set_permissions(member, overwrite=None, reason="Trek arrival")
        else:
            await channel.set_permissions(member, overwrite=overwrite, reason="Trek arrival")
        return True
    except discord.HTTPException:
        log.exception("trek: could not lift lock on #%s for %s", channel, member)
        return False


class Trek(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._trekking = set()  # member_ids currently mid-trek

    async def cog_load(self):
        await tdb.init_tables()

    def _channel_map(self, guild):
        return perms.state_location_channels(guild, tc.STATE)

    async def _gate_check(self, member, cat, loc, player):
        node = locations.get_location(tc.STATE, cat, loc)
        if node is None:
            return False
        role_names = [r.name for r in member.roles]
        if not locations.has_access(role_names, node["access"]):
            return False
        pass_type = locations.guest_pass_needed(role_names, node["access"])
        if pass_type and not await database.has_guest_pass(player["player_id"], tc.STATE, pass_type):
            return False
        return True

    # ------------------------------------------------------------------
    # departure locking — same approach as keke's _lock_ride/_release_lock
    # ------------------------------------------------------------------

    async def _lock_write(self, member, channel):
        """Personal Send Messages: deny. Returns True if WE placed the deny
        (a pre-existing staff mute is left alone and never tracked/lifted)."""
        try:
            overwrite = channel.overwrites_for(member)
            if overwrite.send_messages is False:
                return False
            overwrite.send_messages = False
            await channel.set_permissions(member, overwrite=overwrite, reason="Trek departure")
        except discord.HTTPException:
            log.exception("trek: could not lock #%s for %s", channel, member)
            return False
        try:
            await tdb.add_lock(member.id, channel.id)
        except Exception:
            log.exception("trek: could not record lock for %s", member.id)
        return True

    async def _lock_departure(self, member, channel, player, channel_map):
        """Lock the departure channel AND every room inside it the player
        could write in — same rule as keke boarding. Returns the channels we
        actually locked."""
        targets = [channel]
        try:
            writable = await perms.writable_codes(member, player)
        except Exception:
            log.exception("trek: could not work out rooms to lock for %s", member.id)
            writable = set()
        for code in sorted(writable):
            room = channel_map.get(code)
            if room is not None and room.id != channel.id:
                targets.append(room)
        locked = []
        for target in targets:
            if await self._lock_write(member, target):
                locked.append(target)
        return locked

    async def _release_locks(self, member, locks):
        """Lift every lock this trek placed. A lock that can't be lifted
        stays on record so the startup sweep will retry it."""
        for channel in list(locks):
            if await _unlock_write(channel, member):
                locks.remove(channel)
                try:
                    await tdb.remove_lock(member.id, channel.id)
                except Exception:
                    log.exception("trek: could not clear lock record for %s", member.id)

    async def _clear_stale_locks(self):
        """Treks live only in memory, so after a restart nobody is walking:
        lift every departure lock still on record so nobody stays muted."""
        try:
            rows = await tdb.get_locks()
        except Exception:
            log.exception("trek: could not read locks")
            return
        to_sync = {}
        for row in rows:
            channel = self.bot.get_channel(row["channel_id"])
            member = await _fetch_member(channel, row["member_id"])
            done = channel is None or member is None or await _unlock_write(channel, member)
            if not done:
                continue
            try:
                await tdb.remove_lock(row["member_id"], row["channel_id"])
            except Exception:
                log.exception("trek: could not clear lock record")
            if member is not None:
                to_sync[member.id] = member
        for member in to_sync.values():
            try:
                await perms.sync_member_permissions(member)
            except Exception:
                log.exception("trek: permission sync failed for %s", member.id)

    @commands.Cog.listener()
    async def on_ready(self):
        await self._clear_stale_locks()

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @commands.command(name="walk", aliases=["trek"], help="Trek on foot to a location, e.g. !trek police")
    async def walk(self, ctx, codename: str = None):
        if codename is None:
            await ctx.reply(MSG_INVALID_DEST)
            return
        codename = codename.strip().lower()

        player = await database.get_player_by_discord_id(ctx.author.id)
        if player is None:
            await ctx.reply(MSG_NO_PLAYER_RECORD)
            return
        if player["current_state"] != tc.STATE:
            await ctx.reply(MSG_WRONG_STATE)
            return
        if ctx.author.id in self._trekking:
            await ctx.reply(MSG_ALREADY_TREKKING)
            return

        origin = player["current_sub_location"]
        here_code = perms.channel_code(ctx.channel.name)
        if not origin or here_code != origin:
            await ctx.reply(MSG_NOT_AT_ORIGIN)
            return

        dest = tc.resolve_codename(codename)
        if dest is None:
            log.info("trek: unknown codename %r", codename)
            await ctx.reply(MSG_INVALID_DEST)
            return
        cat, loc_code, parent_name = dest

        if loc_code == origin:
            await ctx.reply(MSG_ALREADY_THERE)
            return

        if not await self._gate_check(ctx.author, cat, loc_code, player):
            await ctx.reply(MSG_GATE_PASS)
            return

        path = tc.path_between(origin, loc_code)
        if path is None:
            log.warning("trek: no walk-order path from %r to %r", origin, loc_code)
            await ctx.reply(MSG_INVALID_DEST)
            return

        hops = len(path) - 1
        eta = hops * tc.HOP_SECONDS
        channel_map = self._channel_map(ctx.guild)

        self._trekking.add(ctx.author.id)
        await ctx.reply(
            tc.DEPARTED_TEMPLATE.format(
                name=player["character_name"], destination=parent_name, eta=_fmt_eta(eta)
            )
        )
        locks = await self._lock_departure(ctx.author, ctx.channel, player, channel_map)
        asyncio.create_task(
            self._run_trek(ctx.author, player, path, parent_name, channel_map, locks)
        )

    async def _run_trek(self, member, player, path, destination_name, channel_map, locks):
        try:
            last = len(path) - 1
            for i in range(1, len(path) + 1):
                await asyncio.sleep(tc.HOP_SECONDS)
                if i > last:
                    break
                stop_code = path[i]
                is_final = i == last
                if is_final:
                    await self._arrive(member, player, stop_code, destination_name, channel_map, locks)
                elif i % tc.ANNOUNCE_EVERY_N_HOPS == 0:
                    channel = channel_map.get(stop_code)
                    if channel is not None:
                        try:
                            await _send_silent(
                                channel, tc.WALKED_PAST_TEMPLATE.format(name=player["character_name"])
                            )
                        except discord.HTTPException:
                            log.exception("trek: could not post walked-past notice at %s", stop_code)
        except Exception:
            log.exception("trek: trip crashed for %s", member.id)
            # Whatever went wrong, don't leave the player permanently muted.
            await self._release_locks(member, locks)
        finally:
            self._trekking.discard(member.id)

    async def _arrive(self, member, player, dest_code, destination_name, channel_map, locks):
        # Lift the departure lock BEFORE syncing: sync_member_permissions
        # never clears an explicit member-level send-deny (same rule as keke).
        await self._release_locks(member, locks)
        try:
            await database.update_player_field(player["player_id"], "current_sub_location", dest_code)
        except Exception:
            log.exception("trek: could not update location for %s", player["player_id"])
            return
        try:
            await perms.sync_member_permissions(member)
        except Exception:
            log.exception("trek: permission sync failed for %s", member.id)
        channel = channel_map.get(dest_code)
        if channel is not None:
            try:
                await channel.send(
                    tc.ARRIVED_TEMPLATE.format(mention=member.mention, destination=destination_name)
                )
            except discord.HTTPException:
                log.exception("trek: could not post arrival message at %s", dest_code)


async def setup(bot):
    await bot.add_cog(Trek(bot))
