"""
bot.py

Minimal starting point for the RP bot. Wires together:
  - discord.py bot
  - database.py  (Postgres connection + schema)
  - decay.py     (background stat decay loop)
  - keep_alive.py (Flask pinger so Render Web Service stays reachable)
  - cogs/onboarding.py (arrival via Discord Onboarding roles, !name, !immigrate)
  - cogs/nin_delivery.py (NIN cards sent to parcel-pickup after 20 minutes, !portrait, !nincard)

Set these environment variables on Render:
  - DISCORD_TOKEN   -> your bot's token from the Discord Developer Portal
  - DATABASE_URL    -> your Render Postgres "Internal Database URL"

In the Discord Developer Portal (Bot tab) turn ON "Server Members Intent" —
the bot needs it to see people joining/leaving and to assign roles.
"""

import os
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


bot = RPBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")

    # Start the background stat-decay loop (start_decay_loop ignores repeat calls)
    start_decay_loop(bot)
    print("Decay loop running.")


if __name__ == "__main__":
    keep_alive(bot)
    bot.run(TOKEN)
