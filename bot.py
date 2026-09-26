"""
bot.py

Minimal starting point for the RP bot. Wires together:
  - discord.py bot
  - database.py  (Postgres connection + schema)
  - decay.py     (background stat decay loop)
  - keep_alive.py (Flask pinger so Render Web Service stays reachable)
  - cogs/onboarding.py (arrival via Discord Onboarding roles, !name, !immigrate)
  - cogs/nin_delivery.py (NIN cards sent to parcel-pickup after 20 minutes, !portrait, !nincard,
    !splitportraits for the stock-portrait archive)
  - cogs/permit.py (Residence Permits: !permit @player <house type>, sent to parcel-pickup after 20 minutes)
  - cogs/passport.py (International Passport: !passport @player, issued immediately, no role granted)
  - cogs/banking.py (bank accounts, tiers, !open-account, !upgrade-tier, !bal, !transfer, !with, !dep)
  - cogs/phone.py (!phone, with the Bank app)
  - cogs/keke.py (keke transport: !keke <codename>, !buy-keke, route loop engine)
  - cogs/danfo.py (danfo transport: !danfo <codename>, !buy-danfo, route loop engine)

Set these environment variables on Render:
  - DISCORD_TOKEN   -> your bot's token from the Discord Developer Portal
  - DATABASE_URL    -> your Render Postgres "Internal Database URL"

In the Discord Developer Portal (Bot tab) turn ON "Server Members Intent" —
the bot needs it to see people joining/leaving and to assign roles.
"""

import logging
import os
import time

import discord
from discord.ext import commands

from keep_alive import keep_alive
import database
from decay import start_decay_loop

TOKEN = os.environ.get("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True  # needed for prefix commands like !name
intents.members = True          # needed for join/leave events and role assignment


class RPBot(commands.Bot):
    async def setup_hook(self):
        # Runs once, before the bot connects: DB first, then the cogs that use it.
        await database.init_db()
        print("Database ready.")
        await self.load_extension("cogs.onboarding")
        try:
            await self.load_extension("cogs.nin_delivery")
        except Exception as exc:        # e.g. Pillow not installed yet: the rest of the bot still runs
            print(f"NIN card delivery NOT loaded: {exc!r}")
        try:
            await self.load_extension("cogs.permit")
        except Exception as exc:
            print(f"Residence permit delivery NOT loaded: {exc!r}")
        try:
            await self.load_extension("cogs.passport")
        except Exception as exc:
            print(f"Passport issuance NOT loaded: {exc!r}")
        # banking must load before the phone and the keke: both use the bank tables
        for extension in ("cogs.banking", "cogs.phone", "cogs.cbn", "cogs.keke", "cogs.danfo", "cogs.oil", "cogs.housing", "cogs.trek", "cogs.estate"):
            try:
                await self.load_extension(extension)
            except Exception as exc:
                print(f"{extension} NOT loaded: {exc!r}")


bot = RPBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")

    # Start the background stat-decay loop (start_decay_loop ignores repeat calls)
    start_decay_loop(bot)
    print("Decay loop running.")


if __name__ == "__main__":
    keep_alive(bot)
    try:
        bot.run(TOKEN)
    except Exception:
        # If the bot dies after startup (e.g. Discord's global rate limit
        # blocks it hard), DON'T let the process exit. Render treats an exit
        # as a failed deploy and immediately restarts the container, which
        # replays the exact startup burst that caused the crash in the first
        # place — a redeploy loop that makes the rate limit worse each time.
        # keep_alive's Flask server runs in its own daemon thread and is
        # unaffected by this crash, so it keeps answering Render's health
        # check; staying alive (instead of exiting) means Render sees a
        # healthy service and leaves it alone instead of kicking off another
        # deploy. This does NOT reconnect the bot — that needs a manual
        # restart on Render once the underlying issue is fixed.
        logging.exception("Bot crashed after startup — staying up with no reconnect so Render doesn't redeploy-loop.")
        while True:
            time.sleep(3600)
