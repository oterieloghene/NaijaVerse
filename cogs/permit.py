"""
cogs/permit.py

Residence permits: !permit registers a player's address and grants "State Resident"; the physical
card follows 20 minutes later, same mechanism as the NIN card (cogs/nin_delivery.py).

  !permit @player <house type>
      e.g.  !permit @Ada two-bedroom-flat
      Run by a Housing Officer (or admin). The address is fully auto-generated — a random house
      number and street name for that house type's category, e.g. "No 7, First Pipeline,
      Low-Cost Housing, Delta" — and the name/NIN on the card come straight from the player's NIN
      record, so the two documents always agree. Grants "State Resident" only; running it again
      for the same player (a different house type) keeps the permit number, updates everything
      else, and resets the 14-day expiry.
  !permitinfo [@player]   shows what's on file
  !permithelp             lists the valid house type codes
  !permitcard [@player]   (admin) posts a card here now — no player = dummy card for layout testing

The bot needs Send Messages + Attach Files in every state's parcel-pickup, and Manage Roles with
its role above "State Resident".
"""

import io

import discord
from discord.ext import commands, tasks

import database
import document_config as cfg
from location_permissions import state_location_channels, sync_member_permissions
from permit_card import generate_permit_card, register_permit, sample_card_data

NO_PINGS = discord.AllowedMentions.none()
HOUSING_STAFF_ROLES = {"housing officer"}


def housing_staff_only():
    """!permit: Housing Officers (or admins) only."""
    async def predicate(ctx):
        member = ctx.author
        if member.guild_permissions.administrator:
            return True
        if any(r.name.casefold() in HOUSING_STAFF_ROLES for r in member.roles):
            return True
        raise commands.CheckFailure("Only Housing Officers can use this command.")
    return commands.check(predicate)


class Permit(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._warned = set()

    async def cog_load(self):
        self.deliver_due_permits.start()

    async def cog_unload(self):
        self.deliver_due_permits.cancel()

    # --- delivery loop (identical shape to nin_delivery's) ---------------------------------

    @tasks.loop(seconds=60)
    async def deliver_due_permits(self):
        try:
            due = await database.get_due_residence_permits()
        except Exception as exc:
            print(f"[permit] couldn't check for due permits: {exc}")
            return
        for row in due:
            try:
                await self._deliver(row)
            except Exception as exc:
                print(f"[permit] couldn't deliver permit {row['permit_number']}: {exc}")

    @deliver_due_permits.before_loop
    async def _wait_until_ready(self):
        await self.bot.wait_until_ready()

    def _find_member(self, discord_id):
        for guild in self.bot.guilds:
            member = guild.get_member(discord_id)
            if member:
                return guild, member
        return None, None

    async def _deliver(self, row):
        guild, member = self._find_member(row["discord_id"])
        if member is None:
            return

        channel = state_location_channels(guild, row["issue_state"]).get(cfg.DELIVERY_CHANNEL)
        if channel is None:
            if row["permit_number"] not in self._warned:
                self._warned.add(row["permit_number"])
                print(f"[permit] No '{cfg.DELIVERY_CHANNEL}' channel found for {row['issue_state']}; "
                      f"permit {row['permit_number']} will be sent once it exists.")
            return

        player = await database.get_player_by_discord_id(member.id)
        gender = (player or {}).get("gender") or "Male"
        portrait = await database.get_effective_portrait(row["player_id"], gender)
        png = await generate_permit_card(row, portrait_bytes=portrait)

        await channel.send(
            f"📦 **Residence Permit ready for pickup**: {member.mention} · {row['residence_type']} · "
            f"Permit No. {row['permit_number']}",
            file=discord.File(io.BytesIO(png), filename=f"{row['permit_number']}.png"),
            allowed_mentions=NO_PINGS,
        )
        await database.mark_residence_permit_sent(row["player_id"])
        self._warned.discard(row["permit_number"])

    # --- commands ----------------------------------------------------------------------------

    @commands.command(name="permit")
    @commands.guild_only()
    @housing_staff_only()
    async def permit(self, ctx, member: discord.Member, house_type: str):
        target = await database.get_player_by_discord_id(member.id)
        if not target or not target["current_state"]:
            await ctx.send(f"{member.mention} hasn't arrived yet.", allowed_mentions=NO_PINGS)
            return

        try:
            row, house_label = await register_permit(dict(target), house_type, target["current_state"])
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        role = discord.utils.find(lambda r: r.name.lower() == cfg.STATE_RESIDENT_ROLE.lower(), ctx.guild.roles)
        problem = None
        try:
            if role:
                await member.add_roles(role, reason="Residence permit registered")
            else:
                problem = f"find the role \"{cfg.STATE_RESIDENT_ROLE}\" on this server"
        except discord.HTTPException:
            problem = "assign the role (is my role above it?)"

        await sync_member_permissions(member)   # write access follows the role change immediately

        lines = [f"Registered {member.mention}: **{house_label}**, {row['address']}",
                 f"Permit No. **{row['permit_number']}** · ready for pickup in about "
                 f"{cfg.PERMIT_CARD_DELAY_MINUTES} minutes at parcel-pickup.",
                 f"Expires {row['expiry_date']:%d %b %Y}."]
        if problem:
            lines.append(f"⚠️ Couldn't {problem}.")
        await ctx.send("\n".join(lines), allowed_mentions=NO_PINGS)

    @commands.command(name="permitinfo")
    @commands.guild_only()
    async def permit_info(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        player = await database.get_player_by_discord_id(member.id)
        row = await database.get_residence_permit_by_player(player["player_id"]) if player else None
        if row is None:
            await ctx.send(f"{member.mention} has no residence permit on file.", allowed_mentions=NO_PINGS)
            return
        await ctx.send(
            f"**{row['full_name']}** — {row['residence_type']}\n{row['address']}\n"
            f"Permit No. {row['permit_number']} · issued {row['date_of_issuance']:%d %b %Y} · "
            f"expires {row['expiry_date']:%d %b %Y}",
            allowed_mentions=NO_PINGS,
        )

    @commands.command(name="permithelp")
    @commands.guild_only()
    async def permit_help(self, ctx):
        lines = ["**House types for `!permit @player <code>`:**"]
        for code, (category_key, label) in cfg.HOUSE_TYPES.items():
            category = cfg.HOUSE_CATEGORY_LABELS[category_key]
            lines.append(f"`{code}` — {label} ({category})")
        await ctx.send("\n".join(lines))

    @commands.command(name="permitcard")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def permit_card_preview(self, ctx, member: discord.Member = None):
        """!permitcard [@player]: post a card here (no player = dummy card for layout testing)."""
        if member is None:
            data, portrait = sample_card_data(), None
        else:
            player = await database.get_player_by_discord_id(member.id)
            data = await database.get_residence_permit_by_player(player["player_id"]) if player else None
            if data is None:
                await ctx.send(f"{member.mention} has no residence permit yet.", allowed_mentions=NO_PINGS)
                return
            portrait = await database.get_effective_portrait(player["player_id"], player.get("gender") or "Male")

        async with ctx.typing():
            png = await generate_permit_card(data, portrait_bytes=portrait)
        await ctx.send(file=discord.File(io.BytesIO(png), filename="permit_card.png"))

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("Only admins can do that.")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send(str(error) or "You can't use that command.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `!permit @player <house type>`. See `!permithelp`.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("I couldn't find that player: mention them with @.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use this in the server.")
        else:
            print(f"[permit] command error: {error!r}")
            await ctx.send("Something went wrong. Please try again.")


async def setup(bot):
    await bot.add_cog(Permit(bot))
