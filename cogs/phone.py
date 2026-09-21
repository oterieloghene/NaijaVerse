"""
cogs/phone.py

The in-game phone.

  !phone   Your !phone message is deleted and a small public message appears ("X takes out their
           phone") with an Open button. Only you can press it. Pressing it removes that public
           message and shows your phone PRIVATELY (only you can see it): the phone picture with your
           battery percentage, and one button per app in the same 3-per-row order as the icons.

  Only the Bank app is wired for now. It stays silent (does nothing) if you have no bank account:
      Check Balance · Send to Business · Send to Person   (Pay Utility Bill and Recharge BRT Card
      are placed but not wired yet)

  !setbattery @player <0-100>   (admins) set someone's battery; handy until charging exists.

Battery: it drains slowly all the time and a little more for each successful action. The number on
the picture is fixed while the phone is open; it is redrawn from the real level each time you open
the phone or press Back to the home screen. All rates are in phone_config.py.

Money moves (transfers) use bank_database.transfer(), the same function as the ATM's !transfer,
so limits, fees, tax, the transaction-log and DM alerts all behave identically.

The bot needs Manage Messages in the channels where players use !phone (to delete the command).
"""

import asyncio
import io
import re

import discord
from discord.ext import commands

import bank_config as bcfg
import bank_database as bank
import bank_messages as msgs
import database
import phone_config as cfg
import phone_database as phone_db
from bank_database import BankError
from phone_renderer import render_phone

NO_PINGS = discord.AllowedMentions.none()
PHONE_COLOUR = 0x0B121B


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

async def _phone_screen(level):
    """(embed, file) for the phone home screen at this battery level."""
    png = await asyncio.to_thread(render_phone, level)
    embed = discord.Embed(colour=PHONE_COLOUR)
    embed.set_image(url="attachment://phone.png")
    return embed, discord.File(io.BytesIO(png), filename="phone.png")


async def _show(interaction, embed, view):
    """Replace what the person sees. Edits the phone message; falls back to a new private one."""
    if interaction.message is not None:
        await interaction.response.edit_message(embed=embed, view=view, attachments=[])
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def _player_and_account(interaction):
    player = await database.get_player_by_discord_id(interaction.user.id)
    account = await bank.get_account_by_player(player["player_id"]) if player else None
    return player, account


async def _battery_ok(interaction, player):
    """True if the phone has charge. If it's dead, tells the person (privately) and returns False."""
    level = await phone_db.get_battery(player["player_id"])
    if phone_db.is_dead(level):
        msg = "🪫 Your phone's battery is dead."
        if interaction.response.is_done():        # already deferred by the caller
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False
    return True


def _bank_menu_embed(account):
    return discord.Embed(
        title=f"🏦 {msgs.bank_name(account['state'])}",
        description="What would you like to do?",
        colour=bcfg.TIERS[account["tier"]]["colour"],
    )


class OwnedView(discord.ui.View):
    """A view only its owner can press."""

    def __init__(self, owner_id, timeout=cfg.PHONE_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your phone.", ephemeral=True)
            return False
        return True


# ---------------------------------------------------------------------------
# Public "takes out their phone" message
# ---------------------------------------------------------------------------

class PickupView(OwnedView):
    def __init__(self, owner_id):
        super().__init__(owner_id, timeout=cfg.PICKUP_TIMEOUT_SECONDS)
        self.message = None

    @discord.ui.button(label="Open phone", emoji="📱", style=discord.ButtonStyle.primary)
    async def open_phone(self, interaction, button):
        await interaction.response.defer(ephemeral=True)   # acknowledge within 3s before any DB/render work
        player = await database.get_player_by_discord_id(interaction.user.id)
        if not player:
            return
        level = await phone_db.get_battery(player["player_id"])
        embed, file = await _phone_screen(level)
        await interaction.followup.send(embed=embed, file=file, view=HomeView(interaction.user.id),
                                        ephemeral=True)
        self.stop()
        try:
            await interaction.message.delete()           # the public message goes away
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Home screen: the phone picture + one button per app
# ---------------------------------------------------------------------------

class HomeView(OwnedView):
    def __init__(self, owner_id):
        super().__init__(owner_id)
        for index, (key, label, emoji) in enumerate(cfg.APPS):
            button = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
                                       row=index // cfg.APPS_PER_ROW)
            button.callback = self._callback_for(key)
            self.add_item(button)

    def _callback_for(self, key):
        async def callback(interaction):
            if key == "bank":
                await open_bank(interaction)
            else:
                await interaction.response.defer()       # app not built yet: silently do nothing
        return callback


async def show_home(interaction):
    await interaction.response.defer()                    # acknowledge within 3s before any DB/render work
    player = await database.get_player_by_discord_id(interaction.user.id)
    if not player:
        return
    level = await phone_db.get_battery(player["player_id"])
    embed, file = await _phone_screen(level)
    await interaction.edit_original_response(embed=embed, attachments=[file], view=HomeView(interaction.user.id))


# ---------------------------------------------------------------------------
# Bank app
# ---------------------------------------------------------------------------

async def open_bank(interaction):
    await interaction.response.defer()                    # acknowledge within 3s before any DB work
    player, account = await _player_and_account(interaction)
    if account is None:                                    # no account: the app doesn't respond at all
        return
    if not await _battery_ok(interaction, player):
        return
    await interaction.edit_original_response(embed=_bank_menu_embed(account),
                                             view=BankMenuView(interaction.user.id), attachments=[])


async def show_bank_menu(interaction):
    player, account = await _player_and_account(interaction)
    if account is None:
        await interaction.response.defer()
        return
    await _show(interaction, _bank_menu_embed(account), BankMenuView(interaction.user.id))


class BankMenuView(OwnedView):
    @discord.ui.button(label="Check Balance", emoji="💰", style=discord.ButtonStyle.primary, row=0)
    async def balance(self, interaction, button):
        player, account = await _player_and_account(interaction)
        if account is None:
            await interaction.response.defer()
            return
        if not await _battery_ok(interaction, player):
            return
        usage = await bank.get_usage(account["account_id"])
        await _show(interaction, msgs.balance_embed(account, usage), BackToBankView(self.owner_id))
        await phone_db.spend(player["player_id"], "balance")

    @discord.ui.button(label="Pay Utility Bill", emoji="💡", style=discord.ButtonStyle.secondary, row=0)
    async def utility(self, interaction, button):
        await interaction.response.defer()               # not wired yet

    @discord.ui.button(label="Recharge BRT Card", emoji="🚌", style=discord.ButtonStyle.secondary, row=0)
    async def brt(self, interaction, button):
        await interaction.response.defer()               # not wired yet

    @discord.ui.button(label="Send to Business", emoji="🏢", style=discord.ButtonStyle.success, row=1)
    async def send_business(self, interaction, button):
        await self._open_send(interaction, "org")

    @discord.ui.button(label="Send to Person", emoji="👤", style=discord.ButtonStyle.success, row=1)
    async def send_person(self, interaction, button):
        await self._open_send(interaction, "personal")

    @discord.ui.button(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction, button):
        await show_home(interaction)

    async def _open_send(self, interaction, kind):
        player, account = await _player_and_account(interaction)
        if account is None:
            await interaction.response.defer()
            return
        if not await _battery_ok(interaction, player):
            return
        await interaction.response.send_modal(SendModal(self.owner_id, kind))


class BackToBankView(OwnedView):
    @discord.ui.button(label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await show_bank_menu(interaction)


class SendModal(discord.ui.Modal):
    def __init__(self, owner_id, kind):
        super().__init__(title="Send to Business" if kind == "org" else "Send to Person")
        self.owner_id = owner_id
        self.kind = kind
        self.number = discord.ui.TextInput(label="Account number", placeholder="0123456789",
                                           min_length=bcfg.ACCOUNT_NUMBER_LENGTH,
                                           max_length=bcfg.ACCOUNT_NUMBER_LENGTH)
        self.amount = discord.ui.TextInput(label="Amount (₦)", placeholder="50000", max_length=20)
        self.narration = discord.ui.TextInput(label="Narration (optional)", required=False, max_length=100)
        for item in (self.number, self.amount, self.narration):
            self.add_item(item)

    async def on_submit(self, interaction):
        back = BackToBankView(self.owner_id)
        number = self.number.value.strip()
        if not re.fullmatch(r"\d{%d}" % bcfg.ACCOUNT_NUMBER_LENGTH, number):
            await _show(interaction, msgs.error_embed("An account number is 10 digits."), back)
            return
        try:
            amount = bcfg.parse_amount(self.amount.value)
        except ValueError as err:
            await _show(interaction, msgs.error_embed(str(err)), back)
            return

        player, account = await _player_and_account(interaction)
        if account is None:
            await interaction.response.defer()
            return
        narration = (self.narration.value or "").strip()
        try:
            preview = await bank.transfer(account["account_id"], number, amount, narration,
                                          expect_type=self.kind, dry_run=True)
        except BankError as err:
            await _show(interaction, msgs.error_embed(err.message), back)
            return
        view = ConfirmView(self.owner_id, account["account_id"], number, amount, narration, self.kind)
        await _show(interaction, msgs.transfer_confirm_embed(preview, number), view)


class ConfirmView(OwnedView):
    def __init__(self, owner_id, account_id, number, amount, narration, kind):
        super().__init__(owner_id)
        self.account_id, self.number, self.amount = account_id, number, amount
        self.narration, self.kind = narration, kind
        self._used = False

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        if self._used:
            await interaction.response.defer()
            return
        self._used = True
        self.stop()
        back = BackToBankView(self.owner_id)

        player = await database.get_player_by_discord_id(interaction.user.id)
        if not player:
            await interaction.response.defer()
            return
        if not await _battery_ok(interaction, player):
            return
        try:
            result = await bank.transfer(self.account_id, self.number, self.amount, self.narration,
                                         expect_type=self.kind)
        except BankError as err:
            await _show(interaction, msgs.error_embed(err.message), back)
            return

        await _show(interaction, msgs.transfer_success_embed(result), back)
        await phone_db.spend(player["player_id"], "transfer")
        await msgs.announce_transaction(interaction.client, interaction.guild, result)

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button):
        self._used = True
        self.stop()
        await show_bank_menu(interaction)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

class Phone(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await phone_db.init_tables()

    @commands.command(name="phone")
    @commands.guild_only()
    async def phone(self, ctx):
        """!phone: take out your phone"""
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass                                          # missing Manage Messages: the command just stays

        player = await database.get_player_by_discord_id(ctx.author.id)
        if not player:
            await ctx.send("You haven't arrived yet.", delete_after=8)
            return
        view = PickupView(ctx.author.id)
        view.message = await ctx.send(
            embed=discord.Embed(description=f"📱 **{ctx.author.display_name}** takes out their phone…",
                                colour=PHONE_COLOUR),
            view=view, allowed_mentions=NO_PINGS,
        )

    @commands.command(name="setbattery")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def setbattery(self, ctx, member: discord.Member, percent: int):
        """!setbattery @player <0-100>"""
        player = await database.get_player_by_discord_id(member.id)
        if not player:
            await ctx.send("That person hasn't arrived yet.")
            return
        await phone_db.set_battery(player["player_id"], percent)
        await ctx.send(f"🔋 {member.display_name}'s battery is now {max(0, min(100, percent))}%.",
                       allowed_mentions=NO_PINGS)

    async def cog_command_error(self, ctx, error):
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send("Usage: `!setbattery @player <0-100>`")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("Only admins can do that.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use this in the server.")
        else:
            print(f"[phone] command error in {ctx.command}: {error!r}")


async def setup(bot):
    await bot.add_cog(Phone(bot))
