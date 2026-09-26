"""
cogs/passport.py

International Passport: !passport @player generates the data page immediately and posts it right
there in the channel — no 20-minute delay like the NIN card or residence permit, and no role is
granted (a passport doesn't change what a player can access, unlike immigration or housing).

  !passport @player   Chief Immigration Officer only, and only in #chief-marshal-office.
                      The player must already have a NIN (name/DOB/sex/state of origin and the
                      portrait all come from that record, so every document agrees). Reissuing
                      keeps the same passport number and refreshes the dates from today.
  !passportcard [@player]   (admin) posts a card here now — no player = dummy card for layout testing
"""

import io

import discord
from discord.ext import commands

import database
from passport_card import generate_passport_card, issue_passport, sample_card_data
from player_rules import is_chief_marshal_office

NO_PINGS = discord.AllowedMentions.none()
CHIEF_IMMIGRATION_ROLE = "chief immigration officer"


def chief_marshal_office_only():
    """!passport: the Chief Immigration Officer (or admin) only, and only in #chief-marshal-office."""
    async def predicate(ctx):
        if not is_chief_marshal_office(ctx.channel.name):
            raise commands.CheckFailure("This command can only be used in #chief-marshal-office.")
        member = ctx.author
        if member.guild_permissions.administrator:
            return True
        if any(r.name.casefold() == CHIEF_IMMIGRATION_ROLE for r in member.roles):
            return True
        raise commands.CheckFailure("Only the Chief Immigration Officer can use this command.")
    return commands.check(predicate)


class Passport(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="passport")
    @commands.guild_only()
    @chief_marshal_office_only()
    async def passport(self, ctx, member: discord.Member):
        target = await database.get_player_by_discord_id(member.id)
        if not target or not target["current_state"]:
            await ctx.send(f"{member.mention} hasn't arrived yet.", allowed_mentions=NO_PINGS)
            return

        try:
            row = await issue_passport(dict(target))
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        portrait = await database.get_effective_portrait(target["player_id"], target.get("gender") or "Male")
        async with ctx.typing():
            png = await generate_passport_card(row, portrait_bytes=portrait)

        await ctx.send(
            f"🛂 **International Passport issued**: {member.mention} · Passport No. {row['passport_no']} · "
            f"expires {row['date_of_expiry']:%d %b %Y}",
            file=discord.File(io.BytesIO(png), filename=f"{row['passport_no']}.png"),
            allowed_mentions=NO_PINGS,
        )

    @commands.command(name="passportcard")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def passport_card_preview(self, ctx, member: discord.Member = None):
        """!passportcard [@player]: post a card here (no player = dummy card for layout testing)."""
        if member is None:
            data, portrait = sample_card_data(), None
        else:
            player = await database.get_player_by_discord_id(member.id)
            data = await database.get_passport_by_player(player["player_id"]) if player else None
            if data is None:
                await ctx.send(f"{member.mention} has no passport yet.", allowed_mentions=NO_PINGS)
                return
            portrait = await database.get_effective_portrait(player["player_id"], player.get("gender") or "Male")

        async with ctx.typing():
            png = await generate_passport_card(data, portrait_bytes=portrait)
        await ctx.send(file=discord.File(io.BytesIO(png), filename="passport.png"))

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("Only admins can do that.")
        elif isinstance(error, commands.CheckFailure):
            await ctx.send(str(error) or "You can't use that command.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `!passport @player`")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("I couldn't find that player: mention them with @.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use this in the server.")
        else:
            print(f"[passport] command error: {error!r}")
            await ctx.send("Something went wrong. Please try again.")


async def setup(bot):
    await bot.add_cog(Passport(bot))
