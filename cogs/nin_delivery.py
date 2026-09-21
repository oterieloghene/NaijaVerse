"""
cogs/nin_delivery.py

Delivers NIN cards and lets players set their portrait.

  - Every minute, looks for cards whose 20 minutes are up (nin_cards.due_at, set by !immigrate),
    draws them and posts them in the parcel-pickup channel of the state they were registered in.
    The due time lives in the database, so a restart or a sleeping Render service only delays a
    card, it never loses one.
  - !portrait        (with an image attached) sets your portrait for your ID documents
    !portrait remove  goes back to using your Discord avatar
  - !nincard [@player]   (admins) post a card in the current channel: with a player, that player's
    real card (same document number); with nobody, a dummy card for checking the layout.

The bot needs Send Messages + Attach Files in every state's parcel-pickup.
"""

import asyncio
import io

import discord
from discord.ext import commands, tasks

import database
import document_config as cfg
from document_renderer import prepare_stored_portrait
from location_permissions import state_location_channels
from nin_card import generate_nin_card, sample_card_data

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
NO_PINGS = discord.AllowedMentions.none()


async def _avatar_bytes(member):
    """Discord avatar as PNG bytes, or None if it can't be fetched."""
    try:
        return await member.display_avatar.replace(size=512, static_format="png").read()
    except discord.HTTPException:
        return None


class NinDelivery(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._warned = set()     # so a missing channel is logged once per card, not every minute

    async def cog_load(self):
        self.deliver_due_cards.start()

    async def cog_unload(self):
        self.deliver_due_cards.cancel()

    # --- delivery loop ---------------------------------------------------------------

    @tasks.loop(seconds=60)
    async def deliver_due_cards(self):
        try:
            due = await database.get_due_nin_cards()
        except Exception as exc:                        # database hiccup: try again next minute
            print(f"[nin] couldn't check for due cards: {exc}")
            return
        for row in due:
            try:
                await self._deliver(row)
            except Exception as exc:
                print(f"[nin] couldn't deliver card {row['document_number']}: {exc}")

    @deliver_due_cards.before_loop
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
            return          # not in the server right now; if they left, their records are already gone

        channel = state_location_channels(guild, row["issue_state"]).get(cfg.DELIVERY_CHANNEL)
        if channel is None:
            if row["document_number"] not in self._warned:
                self._warned.add(row["document_number"])
                print(f"[nin] No '{cfg.DELIVERY_CHANNEL}' channel found for {row['issue_state']}; "
                      f"card {row['document_number']} will be sent once it exists.")
            return

        portrait = await database.get_portrait(row["player_id"]) or await _avatar_bytes(member)
        png = await generate_nin_card(row, portrait_bytes=portrait)

        await channel.send(
            f"📦 **NIN card ready for pickup**: {member.mention} · {row['nin']} · "
            f"Document No. {row['document_number']}",
            file=discord.File(io.BytesIO(png), filename=f"{row['document_number']}.png"),
            allowed_mentions=NO_PINGS,
        )
        await database.mark_nin_card_sent(row["player_id"])
        self._warned.discard(row["document_number"])

    # --- commands ---------------------------------------------------------------------

    @commands.command(name="portrait")
    @commands.guild_only()
    async def portrait(self, ctx, action: str = None):
        """!portrait (attach an image) | !portrait remove"""
        player = await database.get_player_by_discord_id(ctx.author.id)
        if not player:
            await ctx.send("You haven't arrived yet.")
            return

        if action and action.lower() in ("remove", "reset", "clear"):
            removed = await database.delete_portrait(player["player_id"])
            await ctx.send("Portrait removed. Your Discord avatar will be used." if removed
                           else "You don't have a portrait saved.")
            return

        if not ctx.message.attachments:
            await ctx.send("Attach your character's portrait image to the message: `!portrait`. "
                           "A head-and-shoulders picture works best. `!portrait remove` goes back to your avatar.")
            return

        attachment = ctx.message.attachments[0]
        if attachment.size > MAX_UPLOAD_BYTES:
            await ctx.send("That image is too big (limit 8 MB).")
            return
        try:
            stored = await asyncio.to_thread(prepare_stored_portrait, await attachment.read())
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        await database.set_portrait(player["player_id"], stored)
        await ctx.send("Portrait saved. It will appear on your ID documents.")

    @commands.command(name="nincard")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def nincard(self, ctx, member: discord.Member = None):
        """!nincard [@player]: post a card here (no player = dummy card for layout testing)"""
        if member is None:
            data, portrait = sample_card_data(), None
        else:
            player = await database.get_player_by_discord_id(member.id)
            data = await database.get_nin_card_by_player(player["player_id"]) if player else None
            if data is None:
                await ctx.send(f"{member.mention} has no NIN card record yet (they need to be immigrated).",
                               allowed_mentions=NO_PINGS)
                return
            portrait = await database.get_portrait(player["player_id"]) or await _avatar_bytes(member)

        async with ctx.typing():
            png = await generate_nin_card(data, portrait_bytes=portrait)
        await ctx.send(file=discord.File(io.BytesIO(png), filename="nin_card.png"))

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("Only admins can do that.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("I couldn't find that player. Usage: `!nincard @player`")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use this in the server.")
        else:
            print(f"[nin] command error in {ctx.command}: {error!r}")
            await ctx.send("Something went wrong. Please try again.")


async def setup(bot):
    await bot.add_cog(NinDelivery(bot))
