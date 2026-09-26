"""
housing_database.py

Storage for the housing system (cogs/housing.py):

    housing_assignments   one row per (player, state) — max one house per
                           state is enforced by a UNIQUE constraint, so a
                           player can hold at most one assignment per state
                           but one in each state at once.
    housing_threads       private thread ids for an assignment, one row per
                           room (parlour, bathroom, kitchen, bedroom, ...).
                           Deleted automatically when its assignment is
                           deleted (ON DELETE CASCADE).
    housing_shared_threads one row per (state, house_type, shared_key) —
                           the Face Me I Face You / line-house Bathroom /
                           Swimming Pool threads shared by every resident of
                           that house type in that state. Created once,
                           reused, never deleted by an individual eviction.

Nothing here talks to Discord directly — see housing_threads.py for that.
"""

import database


async def init_tables():
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS housing_assignments (
                assignment_id  SERIAL PRIMARY KEY,
                player_id      INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
                state          TEXT NOT NULL,
                house_type     TEXT NOT NULL,
                assigned_by    BIGINT,
                assigned_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                rent_due_at    TIMESTAMPTZ,
                UNIQUE (player_id, state)
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS housing_threads (
                assignment_id  INTEGER NOT NULL REFERENCES housing_assignments(assignment_id) ON DELETE CASCADE,
                room_key       TEXT NOT NULL,
                thread_id      BIGINT NOT NULL,
                PRIMARY KEY (assignment_id, room_key)
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS housing_shared_threads (
                state          TEXT NOT NULL,
                house_type     TEXT NOT NULL,
                shared_key     TEXT NOT NULL,
                thread_id      BIGINT NOT NULL,
                PRIMARY KEY (state, house_type, shared_key)
            );
            """
        )


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------

async def get_assignment(player_id, state):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM housing_assignments WHERE player_id = $1 AND state = $2;", player_id, state)


async def get_assignments_for_player(player_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM housing_assignments WHERE player_id = $1;", player_id)


async def create_assignment(player_id, state, house_type, assigned_by, rent_due_at):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO housing_assignments (player_id, state, house_type, assigned_by, rent_due_at)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING assignment_id;
            """,
            player_id, state, house_type, assigned_by, rent_due_at,
        )


async def delete_assignment(assignment_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute("DELETE FROM housing_assignments WHERE assignment_id = $1;", assignment_id)


async def get_assignments_by_type(state, house_type):
    """Every resident currently housed under a type in a state, with their name and rent due date."""
    async with database.get_pool().acquire() as conn:
        return await conn.fetch(
            """
            SELECT a.assignment_id, a.rent_due_at, p.player_id, p.discord_id, p.character_name
            FROM housing_assignments a
            JOIN players p ON p.player_id = a.player_id
            WHERE a.state = $1 AND a.house_type = $2
            ORDER BY a.assigned_at;
            """,
            state, house_type,
        )


async def anyone_home(state, house_type, exclude_player_id=None):
    """True if any OTHER resident of this house type/state is currently standing in it."""
    async with database.get_pool().acquire() as conn:
        return await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM housing_assignments a
                JOIN players p ON p.player_id = a.player_id
                WHERE a.state = $1 AND a.house_type = $2
                  AND p.current_state = $1 AND p.current_sub_location = $2
                  AND a.player_id != $3
            );
            """,
            state, house_type, exclude_player_id or -1,
        )


# ---------------------------------------------------------------------------
# Private threads
# ---------------------------------------------------------------------------

async def add_private_thread(assignment_id, room_key, thread_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO housing_threads (assignment_id, room_key, thread_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (assignment_id, room_key) DO UPDATE SET thread_id = EXCLUDED.thread_id;
            """,
            assignment_id, room_key, thread_id,
        )


async def get_private_threads(assignment_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT room_key, thread_id FROM housing_threads WHERE assignment_id = $1;", assignment_id)


# ---------------------------------------------------------------------------
# Shared threads
# ---------------------------------------------------------------------------

async def get_shared_thread(state, house_type, shared_key):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchval(
            """
            SELECT thread_id FROM housing_shared_threads
            WHERE state = $1 AND house_type = $2 AND shared_key = $3;
            """,
            state, house_type, shared_key,
        )


async def set_shared_thread(state, house_type, shared_key, thread_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO housing_shared_threads (state, house_type, shared_key, thread_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (state, house_type, shared_key) DO UPDATE SET thread_id = EXCLUDED.thread_id;
            """,
            state, house_type, shared_key, thread_id,
        )


# ---------------------------------------------------------------------------
# Anti-tag listener support
# ---------------------------------------------------------------------------

async def is_house_thread(thread_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM housing_threads WHERE thread_id = $1
                UNION ALL
                SELECT 1 FROM housing_shared_threads WHERE thread_id = $1
            );
            """,
            thread_id,
        )
