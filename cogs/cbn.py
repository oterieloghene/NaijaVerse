"""
cogs/cbn.py

The Central Bank commands.

  CBN Governor/Deputy (vault)      !print <amount>            prints in batches of ₦100,000,000,
                                                                1 minute per batch, added to the
                                                                vault as each batch completes
  CBN Governor/Deputy (vault)      !cb-with <amount> <national treasury | state>
                                                                moves money from the vault into
                                                                the National Treasury or a state's
                                                                bank account (bank_revenue)
  CBN Governor only (vault)        !disburse <state>-treasury <amount>
                                                                moves money from the National
                                                                Treasury into a state's Treasury
  Bank Manager / Executive Director !load-cash <amount>        loads cash into the current
                                                                state's ATM (no cash, no !with),
                                                                drawn from that state's Bank PLC

Only one !print job can run at a time, bot-wide. If the bot restarts mid-print, the job is
simply lost (nothing is owed back) — run !print again for the remainder.
"""

import asyncio

import discord
from discord.ext import commands

import bank_config as cfg
import bank_database as bank
import bank_messages as bank_msgs
import cbn_database as cbn
import cbn_messages as msgs
from bank_database import BankError
from location_permissions import channel_code
from locations import LOCATIONS

NO_PINGS = discord.AllowedMentions.none()

USAGE = {
    "print": "Usage: `!print <amount>` (multiples of ₦100,000,000), in the vault.",
    "cb-with": "Usage: `!cb-with <amount> <national treasury | Delta | Lagos | Abuja>`, in the vault.",
    "load-cash": "Usage: `!load-cash <amount>`, inside the state whose ATM you're loading.",
    "disburse": "Usage: `!disburse <state>-treasury <amount>`, in the vault, e.g. `!disburse delta-treasury 50000000`.",
}


def state_of_channel(channel):
    category = getattr(channel, "category", None)
    if category is None:
        return None
    for state in LOCATIONS:
        if state.casefold() in category.name.casefold():
            return state
    return None


class CBN(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._printing = False   # bot-wide: only one !print job at a time

    async def cog_load(self):
        await cbn.init_tables()

    # --- helpers --------------------------------------------------------------------------

    def _is_cbn(self, member):
        return cfg.member_has_role(member, cfg.CBN_ROLE_NAMES)

    async def _only_in_vault(self, ctx):
        if channel_code(ctx.channel.name) == cfg.VAULT_CHANNEL:
            return True
        await ctx.send("You can only use this in the vault.", delete_after=10)
        return False

    # --- commands --------------------------------------------------------------------------

    @commands.command(name="print")
    @commands.guild_only()
    async def print_money(self, ctx, amount: str):
        """!print <amount>  (vault, CBN Governor/Deputy)"""
        if not self._is_cbn(ctx.author):
            await ctx.send("Only the CBN Governor or CBN Deputy can print money.")
            return
        if not await self._only_in_vault(ctx):
            return
        if self._printing:
            await ctx.send("A print job is already running. Wait for it to finish before starting another.")
            return
        try:
            value = cfg.parse_amount(amount)
        except ValueError as err:
            await ctx.send(str(err))
            return
        if value % cfg.PRINT_CHUNK != 0:
            await ctx.send(f"Printing works in batches of {cfg.money(cfg.PRINT_CHUNK)}. "
                           f"Use a multiple of that, e.g. {cfg.money(cfg.PRINT_CHUNK)} or "
                           f"{cfg.money(cfg.PRINT_CHUNK * 2)}.")
            return

        batches = int(value / cfg.PRINT_CHUNK)
        minutes = batches * cfg.PRINT_SECONDS_PER_CHUNK / 60
        await ctx.send(f"🖨️ Printing {cfg.money(value)} — {batches} batch(es), "
                       f"about {minutes:.0f} minute(s) total.")

        self._printing = True
        last_message = None
        try:
            for _ in range(batches):
                await asyncio.sleep(cfg.PRINT_SECONDS_PER_CHUNK)
                new_balance = await cbn.add_to_vault(cfg.PRINT_CHUNK)
                embed = msgs.print_batch_embed(ctx.author.mention, new_balance)
                if last_message is not None:
                    try:
                        await last_message.delete()
                    except discord.HTTPException:
                        pass
                last_message = await ctx.send(embed=embed, allowed_mentions=NO_PINGS)
        finally:
            self._printing = False

    @commands.command(name="cb-with")
    @commands.guild_only()
    async def cb_with(self, ctx, amount: str, *, destination: str):
        """!cb-with <amount> <national treasury | state>  (vault, CBN Governor/Deputy)"""
        if not self._is_cbn(ctx.author):
            await ctx.send("Only the CBN Governor or CBN Deputy can move vault funds.")
            return
        if not await self._only_in_vault(ctx):
            return
        try:
            value = cfg.parse_amount(amount)
        except ValueError as err:
            await ctx.send(str(err))
            return

        dest = destination.strip().casefold()
        if dest in ("national treasury", "national-treasury", "national"):
            target = cfg.NATIONAL_TREASURY_STATE
        else:
            match = next((s for s in LOCATIONS if s.casefold() == dest), None)
            if match is None:
                await ctx.send("Destination must be `national treasury` or one of: " + ", ".join(LOCATIONS) + ".")
                return
            target = match

        try:
            result = await cbn.cb_withdraw(target, value)
        except BankError as err:
            await ctx.send(err.message)
            return
        await ctx.send(embed=msgs.cb_with_embed(ctx.author.mention, result), allowed_mentions=NO_PINGS)
        if target == cfg.NATIONAL_TREASURY_STATE:
            await bank_msgs.post_treasury_credit(self.bot, ctx.guild, cfg.NATIONAL_TREASURY_STATE, result)
        else:
            await bank_msgs.announce_transaction(self.bot, ctx.guild, result)

    @commands.command(name="load-cash")
    @commands.guild_only()
    async def load_cash(self, ctx, amount: str):
        """!load-cash <amount>  (Bank Manager / Executive Director): loads cash into this state's
        ATM, drawn from that state's own bank account (Bank PLC)."""
        if not cfg.member_has_role(ctx.author, cfg.BANK_MANAGER_ROLE_NAMES):
            await ctx.send("Only a Bank Manager or Executive Director can load ATM cash.")
            return
        state = state_of_channel(ctx.channel)
        if state is None:
            await ctx.send("Use this inside a state's channels so I know which state's ATM to load.")
            return
        try:
            value = cfg.parse_amount(amount)
        except ValueError as err:
            await ctx.send(str(err))
            return
        try:
            result = await bank.load_cash(state, value)
        except BankError as err:
            await ctx.send(err.message)
            return
        await ctx.send(embed=msgs.load_cash_embed(state, ctx.author.mention, result["new_cash_balance"]),
                       allowed_mentions=NO_PINGS)
        await bank_msgs.announce_transaction(self.bot, ctx.guild, result)

    @commands.command(name="disburse")
    @commands.guild_only()
    async def disburse(self, ctx, destination: str, amount: str):
        """!disburse <state>-treasury <amount>  (vault, CBN Governor only): moves money from the
        National Treasury into a state's Treasury account. E.g. !disburse delta-treasury 50000000"""
        if not cfg.member_has_role(ctx.author, cfg.CBN_GOVERNOR_ROLE_NAMES):
            await ctx.send("Only the CBN Governor can disburse treasury funds.")
            return
        if not await self._only_in_vault(ctx):
            return

        dest = destination.strip().casefold()
        if not dest.endswith("-treasury"):
            await ctx.send(USAGE["disburse"])
            return
        state_token = dest[: -len("-treasury")]
        match = next((s for s in LOCATIONS if s.casefold() == state_token), None)
        if match is None:
            await ctx.send("State must be one of: " + ", ".join(f"{s.casefold()}-treasury" for s in LOCATIONS) + ".")
            return
        try:
            value = cfg.parse_amount(amount)
        except ValueError as err:
            await ctx.send(str(err))
            return

        try:
            result = await bank.disburse_to_state_treasury(match, value)
        except BankError as err:
            await ctx.send(err.message)
            return
        await ctx.send(f"✅ {cfg.money(value)} disbursed from the National Treasury to **{match} Treasury** "
                       f"· Ref {result['ref']}", allowed_mentions=NO_PINGS)
        await bank_msgs.post_treasury_credit(self.bot, ctx.guild, match, result)
        await bank_msgs.post_treasury_debit(self.bot, ctx.guild, cfg.NATIONAL_TREASURY_STATE, result)

    # --- errors -------------------------------------------------------------------------

    async def cog_command_error(self, ctx, error):
        name = ctx.command.name if ctx.command else ""
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(USAGE.get(name, "Missing something in that command."))
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Use this in the server.")
        else:
            print(f"[cbn] command error in {name}: {error!r}")
            await ctx.send("Something went wrong. Please try again.")


async def setup(bot):
    await bot.add_cog(CBN(bot))
