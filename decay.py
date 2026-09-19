"""
decay.py

Runs a background loop (using discord.ext.tasks) that decays timer-based
player stats (energy, hunger, thirst, hygiene, breathe, happiness).

Health is intentionally excluded — it's derived/adjusted elsewhere
(other systems, events), not decayed on a timer.

Each stat decays proportionally to its own configured interval, so you
don't need a separate scheduled job per stat:

    decrement = decay_amount * (TICK_MINUTES / decay_interval_minutes)

Usage (in your bot.py):

    from decay import start_decay_loop
    start_decay_loop(bot)
"""

from discord.ext import tasks
import database

TICK_MINUTES = 1  # how often this loop runs


@tasks.loop(minutes=TICK_MINUTES)
async def _decay_tick():
    player_ids = await database.get_all_player_ids_for_decay()

    for player_id in player_ids:
        for stat_name, cfg in database.STAT_CONFIG.items():
            if not cfg["timer_based"]:
                continue  # skip health — it's derived, not timer-decayed

            decrement = cfg["decay_amount"] * (TICK_MINUTES / cfg["decay_interval"])
            await database.adjust_stat(player_id, stat_name, -decrement)


def start_decay_loop(bot):
    """Call once from bot.py's on_ready or setup_hook, after database.init_db()."""
    if not _decay_tick.is_running():
        _decay_tick.start()
