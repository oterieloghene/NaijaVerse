"""
phone_database.py

The phone's battery. One row per player; the level is stored together with the time it was last
settled, and the drain since then is worked out when the phone is used. So there is no
background loop: the battery keeps draining in real time even while the bot is asleep.

    level = await get_battery(player_id)          # 0-100 (float), idle drain already applied
    level = await spend(player_id, "transfer")    # after a SUCCESSFUL action: charges its cost
    await set_battery(player_id, 100)             # admin / future charging

The cost of each action and the idle drain rate are in phone_config.py.
"""

import database
import phone_config as cfg

_IDLE_PER_MINUTE = cfg.IDLE_DRAIN_PERCENT / cfg.IDLE_DRAIN_EVERY_MINUTES


async def init_tables():
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS phones (
                player_id           INTEGER PRIMARY KEY REFERENCES players(player_id) ON DELETE CASCADE,
                battery             DOUBLE PRECISION NOT NULL DEFAULT 100,
                battery_updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )


async def _settle(player_id, extra_cost):
    """Apply idle drain (+ an extra cost) up to now, save it, and return the new level."""
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO phones (player_id) VALUES ($1) ON CONFLICT DO NOTHING;", player_id
        )
        return await conn.fetchval(
            """
            UPDATE phones
            SET battery = GREATEST(0::float8, battery - $2::float8
                                   - EXTRACT(EPOCH FROM (NOW() - battery_updated_at))::float8 / 60.0 * $3::float8),
                battery_updated_at = NOW()
            WHERE player_id = $1
            RETURNING battery;
            """,
            player_id, float(extra_cost), float(_IDLE_PER_MINUTE),
        )


async def get_battery(player_id):
    return await _settle(player_id, 0.0)


async def spend(player_id, action):
    """Charge the battery cost of a successful action (see ACTION_COST). Returns the new level."""
    return await _settle(player_id, cfg.ACTION_COST.get(action, 0.0))


async def set_battery(player_id, percent):
    percent = max(0.0, min(100.0, float(percent)))
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO phones (player_id, battery, battery_updated_at) VALUES ($1, $2, NOW())
            ON CONFLICT (player_id) DO UPDATE SET battery = EXCLUDED.battery, battery_updated_at = NOW();
            """,
            player_id, percent,
        )


def is_dead(level):
    """A phone shows and works from 1% up; below that it counts as 0% (dead)."""
    return int(level) <= 0
