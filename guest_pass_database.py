"""
guest_pass_database.py

Storage for cogs/estate.py.

    guest_pass_requests   one row per application, moving through:
        pending  -> officer hasn't decided yet
        denied   -> officer said no
        ready    -> approved, code issued, waiting for the visitor to redeem
        revoked  -> officer cancelled a still-unredeemed "ready" request
        redeemed -> visitor entered the code, pass is currently active
        ended    -> visitor left early (no cooldown)
        expired  -> bot auto-expired it on timeout (owner->visitor cooldown applies)

    guest_pass_cooldowns   one row per (owner, visitor) pair — only written
        when a pass auto-expires; checked before a new application from that
        owner naming that visitor is allowed.
"""

import database


async def init_tables():
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guest_pass_requests (
                request_id        SERIAL PRIMARY KEY,
                state              TEXT NOT NULL,
                house_type         TEXT NOT NULL,
                assignment_id      INTEGER NOT NULL REFERENCES housing_assignments(assignment_id) ON DELETE CASCADE,
                owner_player_id    INTEGER NOT NULL,
                visitor_player_id  INTEGER NOT NULL,
                visitor_discord_id BIGINT NOT NULL,
                thread_keys        TEXT[] NOT NULL,
                requested_minutes  INTEGER NOT NULL,
                granted_minutes    INTEGER,
                status             TEXT NOT NULL DEFAULT 'pending',
                code               TEXT,
                request_channel_id BIGINT,
                request_message_id BIGINT,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                decided_by         BIGINT,
                redeemed_at        TIMESTAMPTZ,
                expires_at         TIMESTAMPTZ
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guest_pass_cooldowns (
                owner_player_id   INTEGER NOT NULL,
                visitor_player_id INTEGER NOT NULL,
                until             TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (owner_player_id, visitor_player_id)
            );
            """
        )


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

async def create_request(state, house_type, assignment_id, owner_player_id, visitor_player_id,
                          visitor_discord_id, thread_keys, requested_minutes):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO guest_pass_requests
                (state, house_type, assignment_id, owner_player_id, visitor_player_id,
                 visitor_discord_id, thread_keys, requested_minutes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING request_id;
            """,
            state, house_type, assignment_id, owner_player_id, visitor_player_id,
            visitor_discord_id, thread_keys, requested_minutes,
        )


async def set_message_ref(request_id, channel_id, message_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE guest_pass_requests SET request_channel_id = $2, request_message_id = $3 "
            "WHERE request_id = $1;",
            request_id, channel_id, message_id,
        )


async def get_request(request_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM guest_pass_requests WHERE request_id = $1;", request_id)


async def approve(request_id, granted_minutes, code, decided_by):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE guest_pass_requests
            SET status = 'ready', granted_minutes = $2, code = $3, decided_by = $4
            WHERE request_id = $1 AND status = 'pending';
            """,
            request_id, granted_minutes, code, decided_by,
        )


async def deny(request_id, decided_by):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE guest_pass_requests SET status = 'denied', decided_by = $2 "
            "WHERE request_id = $1 AND status = 'pending';",
            request_id, decided_by,
        )


async def revoke_ready(request_id, decided_by):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE guest_pass_requests SET status = 'revoked', decided_by = $2 "
            "WHERE request_id = $1 AND status = 'ready';",
            request_id, decided_by,
        )


async def code_in_use(code):
    async with database.get_pool().acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT 1 FROM guest_pass_requests WHERE code = $1 AND status = 'ready' LIMIT 1;", code))


# ---------------------------------------------------------------------------
# Redemption / lifecycle
# ---------------------------------------------------------------------------

async def find_ready_by_code(visitor_discord_id, code):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM guest_pass_requests "
            "WHERE visitor_discord_id = $1 AND code = $2 AND status = 'ready';",
            visitor_discord_id, code,
        )


async def redeem(request_id, expires_at):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE guest_pass_requests SET status = 'redeemed', redeemed_at = NOW(), expires_at = $2 "
            "WHERE request_id = $1;",
            request_id, expires_at,
        )


async def end_early(request_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute("UPDATE guest_pass_requests SET status = 'ended' WHERE request_id = $1;", request_id)


async def expire(request_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute("UPDATE guest_pass_requests SET status = 'expired' WHERE request_id = $1;", request_id)


async def get_due_for_expiry():
    async with database.get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM guest_pass_requests WHERE status = 'redeemed' AND expires_at <= NOW();")


async def get_active_for_visitor(visitor_discord_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM guest_pass_requests WHERE visitor_discord_id = $1 AND status = 'redeemed';",
            visitor_discord_id,
        )


# ---------------------------------------------------------------------------
# Cooldowns
# ---------------------------------------------------------------------------

async def get_cooldown_until(owner_player_id, visitor_player_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchval(
            "SELECT until FROM guest_pass_cooldowns WHERE owner_player_id = $1 AND visitor_player_id = $2;",
            owner_player_id, visitor_player_id,
        )


async def set_cooldown(owner_player_id, visitor_player_id, until):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO guest_pass_cooldowns (owner_player_id, visitor_player_id, until)
            VALUES ($1, $2, $3)
            ON CONFLICT (owner_player_id, visitor_player_id) DO UPDATE SET until = EXCLUDED.until;
            """,
            owner_player_id, visitor_player_id, until,
        )
