"""
trek_database.py

Persistence for the !walk/!trek departure-channel lock (see cogs/trek.py).

Mirrors keke_database's keke_locks table exactly: a trekker's departure
channel is locked (personal Send Messages: deny) for the whole walk. Treks
live only in memory, so a bot restart mid-walk would otherwise leave that
deny in place forever — this table lets the bot find and lift them on
startup, the same way keke does for keke_locks.
"""

import database


async def init_tables():
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trek_locks (
                member_id     BIGINT NOT NULL,
                channel_id    BIGINT NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (member_id, channel_id)
            );
            """
        )


async def add_lock(member_id, channel_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO trek_locks (member_id, channel_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING;",
            member_id, channel_id,
        )


async def remove_lock(member_id, channel_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM trek_locks WHERE member_id = $1 AND channel_id = $2;",
            member_id, channel_id,
        )


async def get_locks():
    async with database.get_pool().acquire() as conn:
        return await conn.fetch("SELECT member_id, channel_id FROM trek_locks;")
