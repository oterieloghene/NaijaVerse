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
    # An unhandled error would stop this loop for good, so failures are caught and reported once
    # per tick instead: one bad row or a database hiccup only skips that minute's update.
    try:
        player_ids = await database.get_all_player_ids_for_decay()
    except Exception as exc:
        print(f"[decay] couldn't load players: {exc!r}")
        return

    failures, first_error = 0, None
    for player_id in player_ids:
        for stat_name, cfg in database.STAT_CONFIG.items():
            if not cfg["timer_based"]:
                continue  # skip health — it's derived, not timer-decayed

            decrement = cfg["decay_amount"] * (TICK_MINUTES / cfg["decay_interval"])
            try:
                await database.adjust_stat(player_id, stat_name, -decrement)
            except Exception as exc:
                failures += 1
                first_error = first_error or exc

    if failures:
        print(f"[decay] {failures} stat update(s) failed this tick; first error: {first_error!r}")


def start_decay_loop(bot):
    """Call once from bot.py's on_ready or setup_hook, after database.init_db()."""
    if not _decay_tick.is_running():
        _decay_tick.start()
