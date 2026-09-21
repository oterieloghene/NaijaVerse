"""
cogs/onboarding.py

Join flow (uses Discord's built-in Onboarding questions):
  1. Discord Onboarding asks the new member for a destination and a gender.
     Each answer hands out a role: "<State> Arrival" and Male / Female.
  2. The bot sees the role change, creates the player record (placed at the
     Immigration Office — the parent location a new arrival stands in, which
     also gives them access to its Refugee Camp sub-location per their
     roles), and arrival-terminal announces them (tagging the Immigration Officer role) (a non-physical channel
     that only ever carries the welcome message, not a place a player is
     "at").
  3. An Immigration Officer, in #front-desk, runs  !name @player <Full Name> <age>
     -> the player's server nickname becomes their full name, and they get
        "<State> Indigene", Illiterate, Jobless, Homeless, Single.
  4. Then  !immigrate @player
     -> player gets an ID (NIN0001DL / LA / FCT), and "<State> Arrival" is swapped
        for the state role ("Delta" / "Lagos" / "Abuja").
  5. If a member leaves the server, everything about them is deleted.

Write/see access (see location_permissions.py): only the player's current location is writable, and
channels they shouldn't see (other states, failed role rules) are hidden. It is set up when the
player arrives and re-synced whenever their roles change.

The bot never creates Discord roles — every role used here must already exist.

No setup commands: each state's arrival-terminal is found automatically (the channel
named "arrival-terminal" that the "<State> Arrival" role can see).
"""

import asyncpg
import discord
from discord.ext import commands

import database
from location_permissions import (
    audit_member,
    clear_member_permissions,
    sync_member_permissions,
)
from player_rules import (
    DEFAULT_NEW_ROLES,
    arrival_role,
    find_arrival_terminal,
    find_roles,
    indigene_role,
    is_front_desk,
    onboarding_actions,
    parse_name_and_age,
)

IMMIGRATION_STAFF_ROLES = {"immigration officer", "chief immigration officer"}
IMMIGRATION_OFFICER_ROLE = "Immigration Officer"    # tagged in the arrival welcome message

USAGE = {
    "name": "`!name @player <Full Name> <age>`",
    "immigrate": "`!immigrate @player`",
    "checklocks": "`!checklocks @player`",
}


async def announce_in_arrival_terminal(guild, state, text, mention=None, role=None):
    """Post in the given state's arrival-terminal (found automatically). Only `mention` / `role` get pinged."""
    channel = find_arrival_terminal(guild.text_channels, state)
    if channel is None:
        print(f"[onboarding] Couldn't find the arrival-terminal for {state}; skipped: {text}")
        return
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions(
            users=[mention] if mention else False, roles=[role] if role else False))
    except discord.HTTPException as exc:
        print(f"[onboarding] Couldn't post in arrival-terminal for {state}: {exc}")


def front_desk_staff_only():
    """!name and !immigrate: only in #front-desk, and only for Immigration Officers."""
    async def predicate(ctx):
        if not is_front_desk(ctx.channel.name):
            raise commands.CheckFailure("This command can only be used in #front-desk.")
        member = ctx.author
        if member.guild_permissions.administrator:
            return True
        if any(r.name.casefold() in IMMIGRATION_STAFF_ROLES for r in member.roles):
            return True
        raise commands.CheckFailure("Only Immigration Officers can use this command.")
    return commands.check(predicate)


class Onboarding(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- arriving (driven by Discord's Onboarding questions) --------------------

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if after.bot:
            return
        before_names = [r.name for r in before.roles]
        after_names = [r.name for r in after.roles]
        if before_names == after_names:
            return

        created = False
        actions = None
        player = None
        for _ in range(2):   # 2nd pass only if a parallel update created the record first
            player = await database.get_player_by_discord_id(after.id)
            actions = onboarding_actions(before_names, after_names, player)
            if not actions["create"]:
                break
            state, gender = actions["create"]
            try:
                await database.create_player(
                    after.id, after.display_name,
                    gender=gender, current_state=state, current_sub_location="immigration-office",
                )
            except asyncpg.UniqueViolationError:
                continue
            created = True
            officers, _ = find_roles(after.guild.roles, [IMMIGRATION_OFFICER_ROLE])
            officer_role = officers[0] if officers else None
            text = f"{after.mention} just arrived. Welcome to {state}."
            if officer_role:
                text += f" {officer_role.mention} will be with you shortly."
            await announce_in_arrival_terminal(after.guild, state, text, mention=after, role=officer_role)
            break

        if actions["create"] and not created:
            return

        if actions["set_gender"] and player:
            await database.update_player_field(player["player_id"], "gender", actions["set_gender"])

        # Destination/gender are locked once chosen: undo later changes to the answers.
        try:
            remove, _ = find_roles(after.guild.roles, actions["remove_roles"])
            add, _ = find_roles(after.guild.roles, actions["add_roles"])
            if remove:
                await after.remove_roles(*remove, reason="Onboarding choice is locked")
            if add:
                await after.add_roles(*add, reason="Onboarding choice is locked")
        except discord.HTTPException as exc:
            print(f"[onboarding] Couldn't revert onboarding change for {after}: {exc}")

        # Write access follows location + roles: runs on arrival (gives the Immigration Office
        # channels) and again whenever roles change (!name, !immigrate, officer roles, ...).
        await sync_member_permissions(after)

    # --- leaving -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if member.bot:
            return
        row = await database.delete_player_by_discord_id(member.id)
        if row:
            await clear_member_permissions(member)      # all three states, wherever they had been
        if row and row["current_state"]:
            await announce_in_arrival_terminal(
                member.guild, row["current_state"],
                f"{member.mention} just left Nigeria. Goodbye, you will be missed.", mention=member)

    # --- immigration officer commands ------------------------------------------

    @commands.command(name="name")
    @commands.guild_only()
    @front_desk_staff_only()
    async def name_player(self, ctx, member: discord.Member, *, details: str):
        """!name @player <Full Name> <age>"""
        try:
            full_name, age = parse_name_and_age(details)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        player = await database.get_player_by_discord_id(member.id)
        if not player:
            await ctx.send(f"{member.mention} hasn't arrived yet.")
            return
        if player["immigration_status"] == "immigrated":
            await ctx.send(f"{member.mention} is already immigrated and can't be renamed.")
            return

        state = player["current_state"]
        roles, missing = find_roles(ctx.guild.roles, [indigene_role(state), *DEFAULT_NEW_ROLES])
        if missing:
            await ctx.send(f"These roles don't exist yet, so I did nothing: {', '.join(missing)}")
            return

        await database.record_name(player["player_id"], full_name, age, state)

        problems = []
        try:
            await member.add_roles(*roles, reason=f"Named by {ctx.author}")
        except discord.HTTPException:
            problems.append("assign the roles (is my role above theirs?)")
        try:
            await member.edit(nick=full_name, reason=f"Named by {ctx.author}")
        except discord.HTTPException:
            problems.append("change their nickname (I need Manage Nicknames and a role above theirs; I can never rename the server owner)")
        if problems:
            await ctx.send(f"Saved the name, but I couldn't {' or '.join(problems)}. Run the command again once fixed.")
            return
        await ctx.send(f"{member.mention} is now **{full_name}**, age {age}. Next: `!immigrate {member.mention}`")

    @commands.command(name="immigrate")
    @commands.guild_only()
    @front_desk_staff_only()
    async def immigrate_player(self, ctx, member: discord.Member):
        """!immigrate @player"""
        player = await database.get_player_by_discord_id(member.id)
        if not player:
            await ctx.send(f"{member.mention} hasn't arrived yet.")
            return
        if player["immigration_status"] == "immigrated":
            await ctx.send(f"{member.mention} is already immigrated ({player['nin']}).")
            return
        if player["immigration_status"] != "named":
            await ctx.send(f"{member.mention} needs a name first: {USAGE['name']}")
            return

        state = player["current_state"]
        state_roles, missing = find_roles(ctx.guild.roles, [state])
        if missing:
            await ctx.send(f"The **{state}** role doesn't exist yet, so I did nothing.")
            return

        nin = await database.assign_nin(player["player_id"], state)
        if nin is None:
            await ctx.send("Couldn't issue an ID right now. Please try again.")
            return

        # The physical card follows later: it's stored now and posted in this state's parcel-pickup
        # once its delay is up (see cogs/nin_delivery.py). Never allowed to break registration.
        card_note = None
        try:
            from nin_card import schedule_nin_card
            from document_config import NIN_CARD_DELAY_MINUTES
            if await schedule_nin_card(player, nin, state):
                card_note = f"Ready for pickup in about {NIN_CARD_DELAY_MINUTES} minutes."
        except Exception as exc:
            print(f"[nin] couldn't schedule the card for {member}: {exc!r}")

        arrival, _ = find_roles(ctx.guild.roles, [arrival_role(state)])
        try:
            await member.add_roles(*state_roles, reason=f"Immigrated by {ctx.author}")
            await member.remove_roles(*arrival, reason=f"Immigrated by {ctx.author}")
        except discord.HTTPException:
            await ctx.send(f"ID issued ({nin}) but I couldn't swap the roles (is my role above them?). Please fix it manually.")
            return

        embed = discord.Embed(title="Immigration complete")
        embed.add_field(name="Name", value=player["character_name"])
        embed.add_field(name="Age", value=str(player["age"]))
        embed.add_field(name="State", value=state)
        embed.add_field(name="NIN", value=nin, inline=False)
        if card_note:
            embed.add_field(name="NIN card", value=card_note, inline=False)
        await ctx.send(f"Welcome to {state}, {member.mention}.", embed=embed)

    # --- admin check ---------------------------------------------------------------

    @commands.command(name="checklocks")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def check_locks(self, ctx, member: discord.Member):
        """!checklocks @player — where can this player write / see right now?"""
        player = await database.get_player_by_discord_id(member.id)
        if not player or not player["current_state"]:
            await ctx.send(f"{member.mention} hasn't arrived yet.")
            return

        report = await audit_member(member, player)
        state = player["current_state"]
        lines = [
            f"**{member.display_name}** ({state}) is at `{player['current_sub_location']}`",
            f"Writable ({len(report['writable'])}): " + (", ".join(f"`{c}`" for c in report["writable"]) or "none"),
            f"Read-only: {report['locked']} other text channels in {state}",
            "Visible in other states: " + (", ".join(f"`{c}`" for c in report["leaked"]) or "none ✅"),
            "Visible here but failing the role rules: " + (", ".join(f"`{c}`" for c in report["rule_failures"]) or "none ✅"),
            "Should be visible but isn't: " + (", ".join(f"`{c}`" for c in report["hidden_wrongly"]) or "none ✅"),
        ]
        own_missing = report["missing"].get(state, [])
        if own_missing:
            lines.append(f"No matching channel in the server for {state}: " + ", ".join(f"`{c}`" for c in own_missing))
        others = {s: len(m) for s, m in report["missing"].items() if s != state and m}
        if others:
            lines.append("Channels not found in other states: " + ", ".join(f"{s} {n}" for s, n in others.items()))
        if member.guild_permissions.administrator:
            lines.append("⚠️ This member is an Administrator, so Discord ignores channel permissions for them. Test with a non-admin account.")
        await ctx.send("\n".join(lines)[:1900], allowed_mentions=discord.AllowedMentions.none())

    # --- errors ------------------------------------------------------------------

    async def cog_command_error(self, ctx, error):
        command = ctx.command.name if ctx.command else ""
        if isinstance(error, commands.CheckFailure):
            await ctx.send(str(error) or "You can't use that command.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("I couldn't find that player — mention them with @.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send(f"Usage: {USAGE.get(command, '`!' + command + '`')}")
        else:
            print(f"[onboarding] Unhandled error in !{command}: {error!r}")
            await ctx.send("Something went wrong. Please try again.")


async def setup(bot):
    await bot.add_cog(Onboarding(bot))
