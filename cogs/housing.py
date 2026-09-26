"""
cogs/housing.py

The housing system, run by Housing Officers from Rental Desk's staff-office:

  !assignhouse @resident <house_type>
      Resident must be at #rental-desk in the officer's state, and must not
      already hold a house in that state. Swaps their "Homeless" role for the
      house type's prerequisite role (document_config.HOUSE_PREREQUISITE_ROLES),
      creates the house's threads (housing_config.HOUSE_ROOMS), starts a
      7-day rent clock, and posts a tenancy receipt to the new house's
      parlour/bedsitter thread and to staff-office.
  !evicthouse @resident <house_type>
      Deletes the resident's private threads, untags them from any shared
      threads, strips the house role + "{State} Resident", re-grants
      "Homeless", and moves them to immigration-office so they aren't
      stranded with nowhere to stand.
  !housinginfo <house_type>
      Every resident currently housed under that type in the officer's
      state, with their rent due date (flagged if overdue — rent is never
      auto-enforced, just surfaced for an officer to act on).

Only ordinary house types are handled here — governor-penthouse and
president-villa stay on the existing !permit-only flow (housing_config.py).

Also enforces: no tagging other players in ANY house thread (private or
shared) — Discord has no permission for this, so it's a message listener
that deletes pings on sight.
"""

import datetime as dt

import discord
from discord.ext import commands

import database
import document_config as cfg
import housing_config as hcfg
import housing_database as hdb
import housing_messages
import housing_threads
from location_permissions import state_location_channels, sync_member_permissions
from permit_card import generate_address
from player_rules import find_roles

NO_PINGS = discord.AllowedMentions.none()
STATES = ["Lagos", "Delta", "Abuja"]


def _channel_state(channel):
    category = getattr(channel, "category", None)
    name = category.name.casefold() if category else ""
    return next((s for s in STATES if s.casefold() in name), None)


def housing_officer_only():
    async def predicate(ctx):
        if "staff-office" not in ctx.channel.name.casefold():
            raise commands.CheckFailure("This command can only be used in the Rental Desk's Staff Office.")
        if ctx.author.guild_permissions.administrator:
            return True
        state = _channel_state(ctx.channel)
        role_names = {r.name.casefold() for r in ctx.author.roles}
        if state and "housing officer" in role_names and f"{state} employee".casefold() in role_names:
            return True
        raise commands.CheckFailure("Only Housing Officers can use this command.")
    return commands.check(predicate)


def _valid_types_text():
    return ", ".join(f"`{c}`" for c in hcfg.HOUSE_ROOMS)


class Housing(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await hdb.init_tables()

    # --- commands ------------------------------------------------------------------------------

    @commands.command(name="assignhouse")
    @commands.guild_only()
    @housing_officer_only()
    async def assign_house(self, ctx, member: discord.Member, house_type: str):
        house_type = house_type.lower()
        if house_type not in hcfg.HOUSE_ROOMS:
            await ctx.send(f"`{house_type}` isn't a house type. Choose one of: {_valid_types_text()}")
            return

        state = _channel_state(ctx.channel)

        if not ctx.author.guild_permissions.administrator:
            officer = await database.get_player_by_discord_id(ctx.author.id)
            if not officer or officer["current_state"] != state or officer["current_sub_location"] != "rental-desk":
                await ctx.send("You need to be at the Rental Desk yourself to assign a house.")
                return

        target = await database.get_player_by_discord_id(member.id)
        if not target or target["current_state"] != state or target["current_sub_location"] != "rental-desk":
            await ctx.send(f"{member.mention} needs to be at the Rental Desk before a house can be assigned.",
                            allowed_mentions=NO_PINGS)
            return

        if await hdb.get_assignment(target["player_id"], state) is not None:
            await ctx.send(f"{member.mention} already has a house in {state} — evict the current one first "
                            f"with `!evicthouse`.", allowed_mentions=NO_PINGS)
            return

        house_channel = state_location_channels(ctx.guild, state).get(house_type)
        if house_channel is None:
            await ctx.send(f"⚠️ Couldn't find the `{house_type}` channel for {state}.")
            return

        now = dt.datetime.now(dt.timezone.utc)
        rent_due = now + dt.timedelta(days=hcfg.RENT_DAYS)
        assignment_id = await hdb.create_assignment(target["player_id"], state, house_type, ctx.author.id, rent_due)

        problems = []
        homeless_roles, _ = find_roles(ctx.guild.roles, ["Homeless"])
        wanted_roles, missing = find_roles(ctx.guild.roles, cfg.HOUSE_PREREQUISITE_ROLES.get(house_type, []))
        try:
            if homeless_roles and homeless_roles[0] in member.roles:
                await member.remove_roles(homeless_roles[0], reason="Assigned a house")
            if wanted_roles:
                await member.add_roles(*wanted_roles, reason="Assigned a house")
        except discord.HTTPException:
            problems.append("update roles (is my role above them?)")
        if missing:
            problems.append(f'find the role "{missing[0]}"')

        try:
            private_threads = await housing_threads.create_house(
                house_channel, member, member.display_name, state, house_type, assignment_id)
        except discord.HTTPException as exc:
            await ctx.send(f"⚠️ House threads couldn't be fully created ({exc}). Is the server at Boost Level 2?")
            private_threads = {}

        await sync_member_permissions(member)

        house_label = cfg.HOUSE_TYPES[house_type][1]
        address = generate_address(house_type, state)
        receipt = housing_messages.receipt_embed(
            member.display_name, house_label, state, address, rent_due, ctx.author.display_name, now)
        home_thread = private_threads.get("parlour") or private_threads.get("room")
        if home_thread is not None:
            try:
                await home_thread.send(embed=receipt)
            except discord.HTTPException:
                pass
        await ctx.send(embed=receipt)

        lines = [f"🏠 {member.mention} has been assigned a **{house_label}** in {state}."]
        if problems:
            lines.append(f"⚠️ Couldn't {', '.join(problems)}.")
        await ctx.send("\n".join(lines), allowed_mentions=NO_PINGS)

    @commands.command(name="evicthouse")
    @commands.guild_only()
    @housing_officer_only()
    async def evict_house(self, ctx, member: discord.Member, house_type: str):
        house_type = house_type.lower()
        if house_type not in hcfg.HOUSE_ROOMS:
            await ctx.send(f"`{house_type}` isn't a house type. Choose one of: {_valid_types_text()}")
            return

        state = _channel_state(ctx.channel)
        target = await database.get_player_by_discord_id(member.id)
        if not target:
            await ctx.send(f"{member.mention} hasn't arrived yet.", allowed_mentions=NO_PINGS)
            return

        assignment = await hdb.get_assignment(target["player_id"], state)
        if assignment is None or assignment["house_type"] != house_type:
            await ctx.send(f"{member.mention} doesn't hold a {house_type} in {state}.", allowed_mentions=NO_PINGS)
            return

        await housing_threads.delete_private_threads(ctx.guild, assignment["assignment_id"])
        await housing_threads.untag_shared_threads(ctx.guild, member, state, house_type)
        await hdb.delete_assignment(assignment["assignment_id"])

        problems = []
        remove_names = [*cfg.HOUSE_PREREQUISITE_ROLES.get(house_type, []), f"{state} Resident"]
        roles_to_remove, _ = find_roles(ctx.guild.roles, remove_names)
        homeless_roles, missing_homeless = find_roles(ctx.guild.roles, ["Homeless"])
        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Evicted")
            if homeless_roles:
                await member.add_roles(homeless_roles[0], reason="Evicted")
        except discord.HTTPException:
            problems.append("update roles (is my role above them?)")
        if missing_homeless:
            problems.append('find the role "Homeless"')

        # Only stranded if they were actually standing in the house being
        # evicted — if they're at rental-desk, banking-hall, wherever else,
        # leave their location alone entirely.
        if target["current_sub_location"] == house_type:
            await database.update_player_field(target["player_id"], "current_sub_location", "immigration-office")
        await sync_member_permissions(member)

        house_label = cfg.HOUSE_TYPES[house_type][1]
        lines = [f"📦 {member.mention} has been evicted from **{house_label}** in {state}."]
        if problems:
            lines.append(f"⚠️ Couldn't {', '.join(problems)}.")
        await ctx.send("\n".join(lines), allowed_mentions=NO_PINGS)

    @commands.command(name="housinginfo")
    @commands.guild_only()
    @housing_officer_only()
    async def housing_info(self, ctx, house_type: str):
        house_type = house_type.lower()
        if house_type not in hcfg.HOUSE_ROOMS:
            await ctx.send(f"`{house_type}` isn't a house type. Choose one of: {_valid_types_text()}")
            return

        state = _channel_state(ctx.channel)
        rows = await hdb.get_assignments_by_type(state, house_type)
        house_label = cfg.HOUSE_TYPES[house_type][1]
        if not rows:
            await ctx.send(f"No one is currently housed under **{house_label}** in {state}.")
            return

        now = dt.datetime.now(dt.timezone.utc)
        lines = [f"🏘️ **{house_label} — {state}**"]
        for row in rows:
            due = row["rent_due_at"]
            flag = " ⚠️ OVERDUE" if due and due < now else ""
            due_text = f"{due:%d %b %Y}" if due else "—"
            lines.append(f"• {row['character_name']} — rent due {due_text}{flag}")
        await ctx.send("\n".join(lines))

    # --- anti-tag listener -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not isinstance(message.channel, discord.Thread):
            return
        if not message.mentions and not message.role_mentions:
            return
        if not await hdb.is_house_thread(message.channel.id):
            return
        try:
            await message.delete()
        except discord.HTTPException:
            return
        try:
            await message.channel.send(
                f"{message.author.mention} tagging others isn't allowed in house threads.",
                delete_after=5, allowed_mentions=NO_PINGS,
            )
        except discord.HTTPException:
            pass


    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            await ctx.send(str(error) or "You can't use that command.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Usage: `!{ctx.command.name} @player <house_type>`.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("I couldn't find that player: mention them with @.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use this in the server.")
        else:
            print(f"[housing] command error: {error!r}")
            await ctx.send("Something went wrong. Please try again.")


async def setup(bot):
    await bot.add_cog(Housing(bot))
