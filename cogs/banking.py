"""
cogs/banking.py

The bank's commands. All the rules live in bank_database.py; all the wording lives in
bank_messages.py; the tiers, limits, fee and tax live in bank_config.py.

  Bank Staff (banking-office)   !open-account @player        opens a Bronze account (DM to the player)
  Accountant and above          !upgrade-tier @player <tier>  changes a tier up OR down
  Bank Staff (deposit)          !dep @player <amount>         player's cash -> their bank balance
  ATM                           !bal                          check your balance
                                !transfer <@player | account number> <amount> [narration]
                                !with <amount>                bank balance -> your cash
  Admins                        !open-org-account <state> <name>   makes a business/organisation account

Amounts accept 50000, 50,000, ₦50,000, 50k, 1.5m.
Every transaction is posted to the transaction-log of the state the sender's account was opened in,
and the people involved get a DM alert.

The bot needs Send Messages + Embed Links in every state's transaction-log.
"""

import re

import discord
from discord.ext import commands

import bank_config as cfg
import bank_database as bank
import bank_messages as msgs
import database
from bank_database import BankError
from location_permissions import channel_code
from locations import LOCATIONS

NO_PINGS = discord.AllowedMentions.none()

USAGE = {
    "open-account": "Usage: `!open-account @player`",
    "upgrade-tier": "Usage: `!upgrade-tier @player <bronze|gold|diamond|platinum>`",
    "transfer": "Usage: `!transfer <@player or 10-digit account number> <amount> [narration]`",
    "with": "Usage: `!with <amount>`",
    "dep": "Usage: `!dep @player <amount>`",
    "open-org-account": "Usage: `!open-org-account <Delta|Lagos|Abuja> <organisation name>`",
}


def state_of_channel(channel):
    """The state a channel belongs to, from its category name (e.g. '🏛️ Delta – Bank Plc' -> 'Delta')."""
    category = getattr(channel, "category", None)
    if category is None:
        return None
    for state in LOCATIONS:
        if state.casefold() in category.name.casefold():
            return state
    return None


class Banking(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await bank.init_tables()

    # --- small helpers ------------------------------------------------------------------

    async def _only_in(self, ctx, code, place):
        """True if the command was used in the right channel; otherwise says so and returns False."""
        if channel_code(ctx.channel.name) == code:
            return True
        await ctx.send(f"You can only use this at the {place}.", delete_after=10)
        return False

    async def _my_account(self, ctx):
        """(player, account) for the person using the command, or None after telling them why."""
        player = await database.get_player_by_discord_id(ctx.author.id)
        if not player:
            await ctx.send("You haven't arrived yet.")
            return None
        account = await bank.get_account_by_player(player["player_id"])
        if account is None:
            await ctx.send("You don't have a bank account yet. Visit the banking office to open one.")
            return None
        return player, account

    def _may_change_tier(self, member):
        if member.guild_permissions.administrator:
            return True
        names = [n.casefold() for n in cfg.UPGRADE_ROLE_NAMES]
        return any(role.name.casefold().endswith(n) for role in member.roles for n in names)

    # --- staff commands -----------------------------------------------------------------

    @commands.command(name="open-account")
    @commands.guild_only()
    async def open_account(self, ctx, member: discord.Member):
        """!open-account @player  (banking-office)"""
        if not await self._only_in(ctx, cfg.OPEN_ACCOUNT_CHANNEL, "banking office"):
            return
        state = state_of_channel(ctx.channel)
        if state is None:
            await ctx.send("I can't tell which state this banking office belongs to.")
            return
        player = await database.get_player_by_discord_id(member.id)
        if not player:
            await ctx.send("That person hasn't arrived yet, so they can't open an account.")
            return

        try:
            account = await bank.open_personal_account(player["player_id"], player["character_name"],
                                                       state, ctx.author.id)
        except BankError as err:
            await ctx.send(err.message)
            return

        await ctx.send(embed=msgs.account_opened_channel(member.display_name, account["tier"], ctx.author.mention),
                       allowed_mentions=NO_PINGS)
        try:
            await member.send(embed=msgs.account_opened_dm(account, ctx.author.name))
        except discord.HTTPException:
            await ctx.send("⚠️ I couldn't DM them the account details (their DMs are closed).")

    @commands.command(name="upgrade-tier")
    @commands.guild_only()
    async def upgrade_tier(self, ctx, member: discord.Member, tier: str):
        """!upgrade-tier @player <tier>  (Accountant and above; works up or down)"""
        if not self._may_change_tier(ctx.author):
            await ctx.send("Only an Accountant or above can change account tiers.")
            return
        key = cfg.find_tier(tier)
        if key is None:
            await ctx.send("Tiers are: " + ", ".join(t["name"] for t in cfg.TIERS.values()) + ".")
            return
        player = await database.get_player_by_discord_id(member.id)
        account = await bank.get_account_by_player(player["player_id"]) if player else None
        if account is None:
            await ctx.send("That player doesn't have a bank account.")
            return
        if account["tier"] == key:
            await ctx.send(f"They're already on {msgs.tier_label(key)}.")
            return

        try:
            old, updated = await bank.set_tier(account["account_id"], key)
        except BankError as err:
            await ctx.send(err.message)
            return

        await ctx.send(embed=msgs.tier_changed_channel(member.display_name, old, key, ctx.author.mention),
                       allowed_mentions=NO_PINGS)
        try:
            await member.send(embed=msgs.tier_changed_dm(updated, old, ctx.author.name))
        except discord.HTTPException:
            pass

    @commands.command(name="dep")
    @commands.guild_only()
    async def dep(self, ctx, member: discord.Member, amount: str):
        """!dep @player <amount>  (deposit channel): the player's cash goes into their account"""
        if not await self._only_in(ctx, cfg.DEPOSIT_CHANNEL, "deposit desk"):
            return
        try:
            value = cfg.parse_amount(amount)
        except ValueError as err:
            await ctx.send(str(err))
            return
        player = await database.get_player_by_discord_id(member.id)
        account = await bank.get_account_by_player(player["player_id"]) if player else None
        if account is None:
            await ctx.send("That player doesn't have a bank account.")
            return

        try:
            result = await bank.deposit(account["account_id"], value)
        except BankError as err:
            await ctx.send(err.message)
            return
        await ctx.send(f"✅ {cfg.money(value)} deposited into **{member.display_name}**'s account · Ref {result['ref']}",
                       allowed_mentions=NO_PINGS)
        await msgs.announce_transaction(self.bot, ctx.guild, result)

    @commands.command(name="open-org-account")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def open_org_account(self, ctx, state: str, *, name: str):
        """!open-org-account <state> <name>: a business / organisation account for 'Send to Business'"""
        match = next((s for s in LOCATIONS if s.casefold() == state.casefold()), None)
        if match is None:
            await ctx.send("State must be one of: " + ", ".join(LOCATIONS) + ".")
            return
        try:
            account = await bank.create_org_account(match, name.strip())
        except BankError as err:
            await ctx.send(err.message)
            return
        await ctx.send(f"✅ **{account['display_name']}** ({match}) account number: `{account['account_number']}`")

    # --- ATM commands -------------------------------------------------------------------

    @commands.command(name="bal")
    @commands.guild_only()
    async def bal(self, ctx):
        """!bal  (ATM): check your balance"""
        if not await self._only_in(ctx, cfg.ATM_CHANNEL, "ATM"):
            return
        mine = await self._my_account(ctx)
        if mine is None:
            return
        _, account = mine
        usage = await bank.get_usage(account["account_id"])
        await ctx.send(embed=msgs.balance_embed(account, usage))

    @commands.command(name="transfer")
    @commands.guild_only()
    async def transfer(self, ctx, target: str, amount: str, *, narration: str = ""):
        """!transfer <@player | account number> <amount> [narration]  (ATM). No confirmation step."""
        if not await self._only_in(ctx, cfg.ATM_CHANNEL, "ATM"):
            return
        mine = await self._my_account(ctx)
        if mine is None:
            return
        _, account = mine

        mention = re.fullmatch(r"<@!?(\d+)>", target.strip())
        if mention:
            other = await database.get_player_by_discord_id(int(mention.group(1)))
            other_account = await bank.get_account_by_player(other["player_id"]) if other else None
            if other_account is None:
                await ctx.send("That player doesn't have a bank account.")
                return
            number = other_account["account_number"]
        elif re.fullmatch(r"\d{%d}" % cfg.ACCOUNT_NUMBER_LENGTH, target.strip()):
            number = target.strip()
        else:
            await ctx.send(USAGE["transfer"])
            return
        try:
            value = cfg.parse_amount(amount)
        except ValueError as err:
            await ctx.send(str(err))
            return

        try:
            result = await bank.transfer(account["account_id"], number, value, narration)
        except BankError as err:
            await ctx.send(err.message)
            return
        await ctx.send(f"✅ {cfg.money(value)} sent to **{result['receiver']['display_name']}** · "
                       f"Ref {result['ref']}. Your alert is in your DMs.", allowed_mentions=NO_PINGS)
        await msgs.announce_transaction(self.bot, ctx.guild, result)

    @commands.command(name="with")
    @commands.guild_only()
    async def withdraw(self, ctx, amount: str):
        """!with <amount>  (ATM): take cash out of your account"""
        if not await self._only_in(ctx, cfg.ATM_CHANNEL, "ATM"):
            return
        mine = await self._my_account(ctx)
        if mine is None:
            return
        _, account = mine
        try:
            value = cfg.parse_amount(amount)
        except ValueError as err:
            await ctx.send(str(err))
            return

        try:
            result = await bank.withdraw(account["account_id"], value)
        except BankError as err:
            await ctx.send(err.message)
            return
        await ctx.send(f"✅ You withdrew {cfg.money(value)} in cash · Ref {result['ref']}")
        await msgs.announce_transaction(self.bot, ctx.guild, result)

    # --- errors -------------------------------------------------------------------------

    async def cog_command_error(self, ctx, error):
        name = ctx.command.name if ctx.command else ""
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(USAGE.get(name, "Missing something in that command."))
        elif isinstance(error, commands.BadArgument):
            await ctx.send("I couldn't find that player. " + USAGE.get(name, ""))
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("Only admins can do that.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use this in the server.")
        else:
            print(f"[bank] command error in {name}: {error!r}")
            await ctx.send("Something went wrong. Please try again.")


async def setup(bot):
    await bot.add_cog(Banking(bot))
