"""
bot.py

Minimal starting point for the RP bot. Wires together:
  - discord.py bot
  - database.py  (Postgres connection + schema)
  - decay.py     (background stat decay loop)
  - keep_alive.py (Flask pinger so Render Web Service stays reachable)

Set these environment variables on Render:
  - DISCORD_TOKEN   -> your bot's token from the Discord Developer Portal
  - DATABASE_URL    -> your Render Postgres "Internal Database URL"
"""

import os
import discord
from discord.ext import commands

from keep_alive import keep_alive
import database
from decay import start_decay_loop

TOKEN = os.environ.get("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True  # needed for prefix commands like !register

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")

    # Set up the database connection + tables (safe to call every startup)
    await database.init_db()
    print("Database ready.")

    # Start the background stat-decay loop
    start_decay_loop(bot)
    print("Decay loop running.")


if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
