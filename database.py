"""
database.py

Core database layer for the RP bot's player system.
Uses PostgreSQL (via asyncpg) so it works with Render's free Postgres,
Supabase, Neon, or any other Postgres provider — just point DATABASE_URL
at whichever one you're using.

Covers:
    - players            (identity, location, job, cash, employment_state)
    - inventory          (player_id -> item, quantity)
    - player_vehicles    (player_id -> vehicle, capped at 2 per player)
    - player_stats       (health/energy/hunger/thirst/hygiene/breathe/happiness)
    - guest_passes       (Governor's House guests: which governor/deputy issued the pass)
    - settings           (key/value config the bot needs, e.g. arrival-terminal channel per state)

Nothing here talks to Discord directly — this is pure data access so it
can be imported by any cog.
"""

import os
from decimal import Decimal

import asyncpg

from player_rules import format_nin

DATABASE_URL = os.environ.get("DATABASE_URL")

# Stat config: name -> (default, min, max, decay_amount, decay_interval_minutes, is_timer_based)
# decay_amount/decay_interval only matter for timer-based stats.
# Health is NOT timer-based — it's derived/adjusted by other systems.
STAT_CONFIG = {
    "energy":    {"default": 100, "min": 0, "max": 100, "decay_amount": 1, "decay_interval": 15, "timer_based": True},
    "hunger":    {"default": 100, "min": 0, "max": 100, "decay_amount": 1, "decay_interval": 20, "timer_based": True},
    "thirst":    {"default": 100, "min": 0, "max": 100, "decay_amount": 1, "decay_interval": 15, "timer_based": True},
    "hygiene":   {"default": 100, "min": 0, "max": 100, "decay_amount": 1, "decay_interval": 30, "timer_based": True},
    "breathe":   {"default": 100, "min": 0, "max": 100, "decay_amount": 1, "decay_interval": 45, "timer_based": True},
    "happiness": {"default": 100, "min": 0, "max": 100, "decay_amount": 1, "decay_interval": 60, "timer_based": True},
    "health":    {"default": 100, "min": 0, "max": 100, "decay_amount": 0, "decay_interval": 0,  "timer_based": False},
}

MAX_VEHICLES_PER_PLAYER = 2

_pool: asyncpg.Pool | None = None


async def init_db():
    """Create the connection pool and ensure all tables exist. Call once on bot startup."""
    global _pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set.")

    _pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                player_id           SERIAL PRIMARY KEY,
                discord_id          BIGINT UNIQUE NOT NULL,
                character_name      TEXT NOT NULL,
                age                 INTEGER,
                gender              TEXT,
                state_of_origin     TEXT,
                current_state       TEXT,
                current_sub_location TEXT,
                residence           TEXT,
                occupation          TEXT,
                bank_account        TEXT,
                cash_balance        NUMERIC NOT NULL DEFAULT 0,
                employment_state    TEXT,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                id          SERIAL PRIMARY KEY,
                player_id   INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
                item_name   TEXT NOT NULL,
                quantity    INTEGER NOT NULL DEFAULT 0,
                UNIQUE (player_id, item_name)
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_vehicles (
                id            SERIAL PRIMARY KEY,
                player_id     INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
                vehicle_name  TEXT NOT NULL,
                acquired_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_stats (
                player_id       INTEGER PRIMARY KEY REFERENCES players(player_id) ON DELETE CASCADE,
                energy          NUMERIC NOT NULL DEFAULT 100,
                hunger          NUMERIC NOT NULL DEFAULT 100,
                thirst          NUMERIC NOT NULL DEFAULT 100,
                hygiene         NUMERIC NOT NULL DEFAULT 100,
                breathe         NUMERIC NOT NULL DEFAULT 100,
                happiness       NUMERIC NOT NULL DEFAULT 100,
                health          NUMERIC NOT NULL DEFAULT 100,
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # Location is stored in two columns:
        #   current_state        -> the state location: Delta / Lagos / Abuja (changes when travelling)
        #   current_sub_location -> the parent location inside that state, e.g. "banking-hall"
        # (the sub-locations INSIDE a parent, like the ATM, are role-gated and never stored).
        # If an earlier version renamed this column to current_location, rename it back.
        await conn.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'players' AND column_name = 'current_location'
                ) AND NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'players' AND column_name = 'current_sub_location'
                ) THEN
                    ALTER TABLE players RENAME COLUMN current_location TO current_sub_location;
                END IF;
            END $$;
            """
        )

        # Onboarding / immigration additions (safe to run on an existing database)
        await conn.execute(
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS immigration_status TEXT NOT NULL DEFAULT 'arrived';"
        )
        await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS nin TEXT UNIQUE;")
        await conn.execute("CREATE SEQUENCE IF NOT EXISTS nin_seq START 1;")   # no longer used for NINs
        # The number part of the NIN (NIN0007DL -> 7), shared across all states and unique, so a
        # number is free again as soon as its player is gone.
        await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS nin_number INTEGER UNIQUE;")
        await conn.execute(
            """
            UPDATE players SET nin_number = substring(nin from '^NIN([0-9]+)')::int
            WHERE nin IS NOT NULL AND nin_number IS NULL;
            """
        )

        # A player's portrait (shared by every document: NIN card, later permits, licences, ...).
        # Stored as image bytes because Render's disk is wiped on every restart.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_portraits (
                player_id   INTEGER PRIMARY KEY REFERENCES players(player_id) ON DELETE CASCADE,
                image       BYTEA NOT NULL,
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # One row per NIN card. document_number is permanent: a re-rendered card reuses it.
        # due_at = when the card should reach parcel-pickup; sent_at = when it did (NULL = not yet).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nin_cards (
                player_id            INTEGER PRIMARY KEY REFERENCES players(player_id) ON DELETE CASCADE,
                document_number      TEXT NOT NULL UNIQUE,
                full_name            TEXT NOT NULL,
                nin                  TEXT NOT NULL,
                date_of_birth        DATE NOT NULL,
                sex                  TEXT NOT NULL DEFAULT '',
                nationality          TEXT NOT NULL,
                state_of_origin      TEXT NOT NULL,
                date_of_registration DATE NOT NULL,
                issue_state          TEXT NOT NULL,
                created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                due_at               TIMESTAMPTZ NOT NULL,
                sent_at              TIMESTAMPTZ
            );
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS nin_cards_due_idx ON nin_cards (due_at) WHERE sent_at IS NULL;"
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            );
            """
        )

        # Governor's House guest passes. The Discord role stays a plain
        # "State Visitor/Guest" / "State Guest"; who issued it lives here.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guest_passes (
                id               SERIAL PRIMARY KEY,
                guest_player_id  INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
                host_player_id   INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
                state            TEXT NOT NULL,
                pass_type        TEXT NOT NULL CHECK (pass_type IN ('visitor', 'guest')),
                host_code        TEXT NOT NULL,
                issued_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (guest_player_id, host_player_id, state, pass_type)
            );
            """
        )


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db() first.")
    return _pool


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

async def create_player(discord_id: int, character_name: str, **fields) -> int:
    """Register a new player. Extra fields (age, gender, state_of_origin, etc.) are optional."""
    allowed = {
        "age", "gender", "state_of_origin", "current_state", "current_sub_location",
        "residence", "occupation", "bank_account", "cash_balance", "employment_state",
    }
    columns = ["discord_id", "character_name"] + [k for k in fields if k in allowed]
    values = [discord_id, character_name] + [fields[k] for k in fields if k in allowed]
    placeholders = ", ".join(f"${i+1}" for i in range(len(values)))

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO players ({", ".join(columns)})
            VALUES ({placeholders})
            RETURNING player_id;
            """,
            *values,
        )
        player_id = row["player_id"]

        # Give every new player a stats row with defaults
        await conn.execute(
            "INSERT INTO player_stats (player_id) VALUES ($1) ON CONFLICT DO NOTHING;",
            player_id,
        )
        return player_id


async def get_player_by_discord_id(discord_id: int):
    async with get_pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM players WHERE discord_id = $1;", discord_id)


async def get_player(player_id: int):
    async with get_pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM players WHERE player_id = $1;", player_id)


async def update_player_field(player_id: int, field: str, value):
    allowed = {
        "character_name", "age", "gender", "state_of_origin", "current_state",
        "current_sub_location", "residence", "occupation", "bank_account",
        "cash_balance", "employment_state",
    }
    if field not in allowed:
        raise ValueError(f"Cannot update field: {field}")

    async with get_pool().acquire() as conn:
        await conn.execute(f"UPDATE players SET {field} = $1 WHERE player_id = $2;", value, player_id)


async def adjust_cash(player_id: int, amount):
    """amount can be negative to deduct."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE players SET cash_balance = cash_balance + $1 WHERE player_id = $2;",
            amount, player_id,
        )


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

async def add_item(player_id: int, item_name: str, quantity: int = 1):
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO inventory (player_id, item_name, quantity)
            VALUES ($1, $2, $3)
            ON CONFLICT (player_id, item_name)
            DO UPDATE SET quantity = inventory.quantity + EXCLUDED.quantity;
            """,
            player_id, item_name, quantity,
        )


async def remove_item(player_id: int, item_name: str, quantity: int = 1) -> bool:
    """Returns False if the player doesn't have enough of the item."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT quantity FROM inventory WHERE player_id = $1 AND item_name = $2;",
            player_id, item_name,
        )
        if not row or row["quantity"] < quantity:
            return False

        new_qty = row["quantity"] - quantity
        if new_qty == 0:
            await conn.execute(
                "DELETE FROM inventory WHERE player_id = $1 AND item_name = $2;",
                player_id, item_name,
            )
        else:
            await conn.execute(
                "UPDATE inventory SET quantity = $1 WHERE player_id = $2 AND item_name = $3;",
                new_qty, player_id, item_name,
            )
        return True


async def get_inventory(player_id: int):
    async with get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT item_name, quantity FROM inventory WHERE player_id = $1 ORDER BY item_name;",
            player_id,
        )


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------

async def add_vehicle(player_id: int, vehicle_name: str) -> bool:
    """Returns False if the player already owns the max number of vehicles."""
    async with get_pool().acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM player_vehicles WHERE player_id = $1;", player_id
        )
        if count >= MAX_VEHICLES_PER_PLAYER:
            return False

        await conn.execute(
            "INSERT INTO player_vehicles (player_id, vehicle_name) VALUES ($1, $2);",
            player_id, vehicle_name,
        )
        return True


async def remove_vehicle(player_id: int, vehicle_id: int):
    async with get_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM player_vehicles WHERE id = $1 AND player_id = $2;",
            vehicle_id, player_id,
        )


async def get_vehicles(player_id: int):
    async with get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT id, vehicle_name, acquired_at FROM player_vehicles WHERE player_id = $1;",
            player_id,
        )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

async def get_stats(player_id: int):
    async with get_pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM player_stats WHERE player_id = $1;", player_id)


async def set_stat(player_id: int, stat_name: str, value):
    if stat_name not in STAT_CONFIG:
        raise ValueError(f"Unknown stat: {stat_name}")

    cfg = STAT_CONFIG[stat_name]
    clamped = max(cfg["min"], min(cfg["max"], value))

    async with get_pool().acquire() as conn:
        await conn.execute(
            f"UPDATE player_stats SET {stat_name} = $1, updated_at = NOW() WHERE player_id = $2;",
            clamped, player_id,
        )
    return clamped


async def adjust_stat(player_id: int, stat_name: str, delta):
    """delta can be negative. Clamped to the stat's min/max."""
    if stat_name not in STAT_CONFIG:
        raise ValueError(f"Unknown stat: {stat_name}")

    cfg = STAT_CONFIG[stat_name]
    delta = Decimal(str(delta))     # the stat columns are NUMERIC (returned as Decimal), which can't be added to a float
    async with get_pool().acquire() as conn:
        current = await conn.fetchval(
            f"SELECT {stat_name} FROM player_stats WHERE player_id = $1;", player_id
        )
        new_value = max(Decimal(cfg["min"]), min(Decimal(cfg["max"]), current + delta))
        await conn.execute(
            f"UPDATE player_stats SET {stat_name} = $1, updated_at = NOW() WHERE player_id = $2;",
            new_value, player_id,
        )
    return new_value


def derive_status(stats_row) -> str:
    """
    Compute healthy / sick / critical from the current stat values.
    Placeholder thresholds — tune these once the rest of the game's
    balance is worked out.
    """
    health = stats_row["health"]
    core_stats = [stats_row["hunger"], stats_row["thirst"], stats_row["hygiene"], stats_row["breathe"]]

    if health <= 20 or any(s <= 10 for s in core_stats):
        return "critical"
    if health <= 50 or any(s <= 30 for s in core_stats):
        return "sick"
    return "healthy"


async def get_all_player_ids_for_decay():
    """Used by the decay tick loop to know which players to update."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT player_id FROM player_stats;")
        return [r["player_id"] for r in rows]


# ---------------------------------------------------------------------------
# Guest passes (Governor's House)
# ---------------------------------------------------------------------------

def make_host_code(host_role: str, host_player_id: int) -> str:
    """host_role: 'governor' or 'deputy'. e.g. GOV-0042 / DEP-0042."""
    prefix = "GOV" if host_role == "governor" else "DEP"
    return f"{prefix}-{host_player_id:04d}"


async def grant_guest_pass(guest_player_id: int, host_player_id: int, state: str,
                           pass_type: str, host_role: str) -> str:
    """Record that a governor/deputy gave someone access. Returns the host code."""
    code = make_host_code(host_role, host_player_id)
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO guest_passes (guest_player_id, host_player_id, state, pass_type, host_code)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (guest_player_id, host_player_id, state, pass_type) DO NOTHING;
            """,
            guest_player_id, host_player_id, state, pass_type, code,
        )
    return code


async def revoke_guest_pass(guest_player_id: int, host_player_id: int, state: str, pass_type: str):
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            DELETE FROM guest_passes
            WHERE guest_player_id = $1 AND host_player_id = $2 AND state = $3 AND pass_type = $4;
            """,
            guest_player_id, host_player_id, state, pass_type,
        )


async def revoke_all_guest_passes_by_host(host_player_id: int, state: str):
    """E.g. when a governor/deputy leaves office — everyone they invited loses access."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM guest_passes WHERE host_player_id = $1 AND state = $2;",
            host_player_id, state,
        )


async def get_guest_passes(guest_player_id: int, state: str):
    async with get_pool().acquire() as conn:
        return await conn.fetch(
            """
            SELECT host_player_id, pass_type, host_code, issued_at
            FROM guest_passes WHERE guest_player_id = $1 AND state = $2;
            """,
            guest_player_id, state,
        )


async def has_guest_pass(guest_player_id: int, state: str, pass_type: str) -> bool:
    async with get_pool().acquire() as conn:
        return bool(await conn.fetchval(
            """
            SELECT 1 FROM guest_passes
            WHERE guest_player_id = $1 AND state = $2 AND pass_type = $3 LIMIT 1;
            """,
            guest_player_id, state, pass_type,
        ))


# ---------------------------------------------------------------------------
# Onboarding & immigration
# immigration_status: 'arrived' (picked destination) -> 'named' (!name) -> 'immigrated' (!immigrate)
# ---------------------------------------------------------------------------

async def delete_player_by_discord_id(discord_id: int):
    """
    Wipe a player completely (inventory, vehicles, stats and guest passes go with
    them via ON DELETE CASCADE). Returns the deleted row, or None if they had none.
    """
    async with get_pool().acquire() as conn:
        return await conn.fetchrow(
            "DELETE FROM players WHERE discord_id = $1 RETURNING *;", discord_id
        )


async def record_name(player_id: int, full_name: str, age: int, state: str):
    """Set by !name. New arrivals start as Indigene of their state, jobless and homeless."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE players
            SET character_name = $1, age = $2, state_of_origin = $3,
                occupation = 'Jobless', residence = 'Homeless',
                immigration_status = 'named'
            WHERE player_id = $4;
            """,
            full_name, age, state, player_id,
        )


def lowest_free_number(used) -> int:
    """Smallest positive integer not in `used` (1 if nothing is used)."""
    taken = set(used)
    number = 1
    while number in taken:
        number += 1
    return number


NIN_LOCK_ID = 7_310_001   # arbitrary app-wide key for the advisory lock below


async def assign_nin(player_id: int, state: str):
    """
    Set by !immigrate. Issues the lowest NIN number nobody currently holds (across all states),
    so the number of a player who left the server is handed out again, e.g. NIN0001DL.
    Marks the player immigrated. Returns the NIN, or None if the player wasn't in the 'named' state.
    """
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # One issuer at a time, so two officers can't be given the same free number.
            await conn.execute("SELECT pg_advisory_xact_lock($1);", NIN_LOCK_ID)
            rows = await conn.fetch("SELECT nin_number FROM players WHERE nin_number IS NOT NULL;")
            number = lowest_free_number(r["nin_number"] for r in rows)
            nin = format_nin(number, state)
            result = await conn.execute(
                """
                UPDATE players SET nin = $1, nin_number = $2, immigration_status = 'immigrated'
                WHERE player_id = $3 AND immigration_status = 'named';
                """,
                nin, number, player_id,
            )
            if result != "UPDATE 1":
                return None
            return nin


# ---------------------------------------------------------------------------
# Portraits and NIN cards
# ---------------------------------------------------------------------------

async def set_portrait(player_id: int, image: bytes):
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO player_portraits (player_id, image) VALUES ($1, $2)
            ON CONFLICT (player_id) DO UPDATE SET image = EXCLUDED.image, updated_at = NOW();
            """,
            player_id, image,
        )


async def get_portrait(player_id: int):
    """The stored portrait bytes, or None."""
    async with get_pool().acquire() as conn:
        return await conn.fetchval("SELECT image FROM player_portraits WHERE player_id = $1;", player_id)


async def delete_portrait(player_id: int) -> bool:
    async with get_pool().acquire() as conn:
        result = await conn.execute("DELETE FROM player_portraits WHERE player_id = $1;", player_id)
        return result == "DELETE 1"


async def create_nin_card(player_id, document_number, full_name, nin, date_of_birth, sex, nationality,
                          state_of_origin, date_of_registration, issue_state, due_at):
    """
    Store a player's NIN card record. If they already have one it is left untouched (so the
    document number never changes) and the existing record is returned.
    """
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO nin_cards (player_id, document_number, full_name, nin, date_of_birth, sex,
                                   nationality, state_of_origin, date_of_registration, issue_state, due_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (player_id) DO NOTHING
            RETURNING *;
            """,
            player_id, document_number, full_name, nin, date_of_birth, sex, nationality,
            state_of_origin, date_of_registration, issue_state, due_at,
        )
        if row is None:
            row = await conn.fetchrow("SELECT * FROM nin_cards WHERE player_id = $1;", player_id)
        return row


async def get_nin_card_by_player(player_id: int):
    async with get_pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM nin_cards WHERE player_id = $1;", player_id)


async def get_nin_card_by_document(document_number: str):
    async with get_pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM nin_cards WHERE document_number = $1;", document_number)


async def get_due_nin_cards(limit: int = 10):
    """Cards whose time has come and that haven't been sent yet, oldest first, with the player's discord_id."""
    async with get_pool().acquire() as conn:
        return await conn.fetch(
            """
            SELECT c.*, p.discord_id
            FROM nin_cards c JOIN players p ON p.player_id = c.player_id
            WHERE c.sent_at IS NULL AND c.due_at <= NOW()
            ORDER BY c.due_at
            LIMIT $1;
            """,
            limit,
        )


async def mark_nin_card_sent(player_id: int):
    async with get_pool().acquire() as conn:
        await conn.execute("UPDATE nin_cards SET sent_at = NOW() WHERE player_id = $1;", player_id)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

async def set_setting(key: str, value: str):
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
            """,
            key, value,
        )


async def get_setting(key: str):
    async with get_pool().acquire() as conn:
        return await conn.fetchval("SELECT value FROM settings WHERE key = $1;", key)
