"""
keke_database.py

The Delta State keke fleet: one row per unit (zone, fuel in the tank, who bought
it), the fuel burned per trip, and the two money moves — the purchase out of the
State Treasury and the passenger fare from the player's cash at hand into the
State Treasury at arrival.

Tables (created automatically by init_tables(), safe to run on every start):
    kekes           one row per owned unit: zone, fuel litres, purchaser.
    keke_locks      the "can't write here while riding" lock placed on a
                    passenger's departure channel. Riders live only in memory,
                    so a restart mid-ride would leave the lock behind forever;
                    this table lets the bot find and lift them on startup.
    keke_fuel_log   the fuel each unit burns per trip. Refueling is DEFERRED by
                    the user, so this log never moves money — it only records
                    litres against the tank.

The money moves intentionally reuse bank_database's private _system_account() /
_insert_tx() inside the SAME transaction as the keke row / player cash update,
so a purchase or a fare is one atomic move — the treasury never moves without
the keke row or the player's cash moving with it.
"""

import bank_config as cfg
import bank_database as bank
import database
import keke_config as kc
from bank_database import BankError


async def init_tables():
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kekes (
                keke_id       SERIAL PRIMARY KEY,
                state         TEXT NOT NULL,
                zone          TEXT NOT NULL CHECK (zone IN ('A', 'B', 'C')),
                fuel_liters   NUMERIC(6, 3) NOT NULL DEFAULT 30 CHECK (fuel_liters >= 0),
                purchased_by  BIGINT,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keke_locks (
                member_id     BIGINT NOT NULL,
                channel_id    BIGINT NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (member_id, channel_id)
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keke_fuel_log (
                log_id        SERIAL PRIMARY KEY,
                keke_id       INTEGER NOT NULL REFERENCES kekes(keke_id) ON DELETE CASCADE,
                liters        NUMERIC(6, 3) NOT NULL CHECK (liters >= 0),
                narration     TEXT NOT NULL DEFAULT '',
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keke_parked (
                keke_id       INTEGER PRIMARY KEY REFERENCES kekes(keke_id) ON DELETE CASCADE,
                channel_id    BIGINT NOT NULL,
                message_id    BIGINT NOT NULL
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keke_zone_state (
                state         TEXT NOT NULL,
                zone          TEXT NOT NULL CHECK (zone IN ('A', 'B', 'C')),
                active        BOOLEAN NOT NULL DEFAULT TRUE,
                PRIMARY KEY (state, zone)
            );
            """
        )


# ---------------------------------------------------------------------------
# Fleet queries
# ---------------------------------------------------------------------------

async def count_kekes(state):
    async with database.get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) AS n FROM kekes WHERE state = $1;", state)
        return row["n"]


async def get_kekes(state):
    async with database.get_pool().acquire() as conn:
        return await conn.fetch("SELECT * FROM kekes WHERE state = $1 ORDER BY keke_id;", state)


async def get_keke(keke_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM kekes WHERE keke_id = $1;", keke_id)


# ---------------------------------------------------------------------------
# !keke-start / !keke-stop: per-zone on/off switch for the Delta Commissioner
# of Commerce. Persisted (not just held in the cog's memory) so a stopped
# zone STAYS stopped across a redeploy instead of the engine silently
# respawning it the next time it loads every row from `kekes` on boot.
# ---------------------------------------------------------------------------

async def get_active_zones(state):
    """Zones ('A'/'B'/'C') currently allowed to run kekes for `state`. A zone
    with no row yet has never been stopped, so it defaults to active."""
    async with database.get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT zone, active FROM keke_zone_state WHERE state = $1;", state
        )
    stopped = {r["zone"] for r in rows if not r["active"]}
    return {z for z in kc.ZONES if z not in stopped}


async def set_zone_active(state, zone, active):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO keke_zone_state (state, zone, active)
            VALUES ($1, $2, $3)
            ON CONFLICT (state, zone) DO UPDATE SET active = EXCLUDED.active;
            """,
            state, zone, active,
        )


# ---------------------------------------------------------------------------
# !buy-keke (zone): the State Treasury pays for a new unit (full tank).
# ---------------------------------------------------------------------------

async def buy_keke(state, zone, buyer_id):
    """
    Buy one keke for `zone` ('A'/'B'/'C') out of the state's treasury.
    Returns {"keke_id", "zone", "cost", "new_treasury_balance", "ref"}.
    """
    if zone not in kc.ZONES:
        raise BankError("That is not a keke zone. Use A, B or C.")
    if await count_kekes(state) >= kc.MAX_KEKES:
        raise BankError(f"{state} can only own {kc.MAX_KEKES} kekes.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            treasury = await bank._system_account(conn, state, bank._TREASURY)
            if treasury["balance"] < kc.KEKE_COST:
                raise BankError(
                    f"The {state} Treasury only has {cfg.money(treasury['balance'])} — "
                    f"it needs {cfg.money(kc.KEKE_COST)} for a keke."
                )

            new_treasury_balance = treasury["balance"] - kc.KEKE_COST
            await conn.execute(
                "UPDATE bank_accounts SET balance = balance - $1 WHERE account_id = $2;",
                kc.KEKE_COST, treasury["account_id"],
            )
            tx = await bank._insert_tx(conn, "keke_purchase", treasury["account_id"], None,
                                       treasury["name"], None,
                                       kc.KEKE_COST, cfg.to_money(0), cfg.to_money(0),
                                       f"Keke purchase (zone {zone})", state)

            row = await conn.fetchrow(
                """
                INSERT INTO kekes (state, zone, fuel_liters, purchased_by)
                VALUES ($1, $2, $3, $4)
                RETURNING keke_id;
                """,
                state, zone, kc.TANK_CAPACITY_L, buyer_id,
            )
            return {
                "keke_id": row["keke_id"],
                "zone": zone,
                "cost": kc.KEKE_COST,
                "new_treasury_balance": new_treasury_balance,
                "ref": tx["ref"],
                "kind": "keke_purchase", "amount": kc.KEKE_COST,
                "created_at": tx["created_at"], "state": state,
                "sender": bank._party(treasury, new_treasury_balance), "receiver": None,
                "from_label": treasury["name"], "to_label": f"{state} Keke (zone {zone})",
            }


async def add_lock(member_id, channel_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO keke_locks (member_id, channel_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING;",
            member_id, channel_id,
        )


async def remove_lock(member_id, channel_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM keke_locks WHERE member_id = $1 AND channel_id = $2;",
            member_id, channel_id,
        )


async def get_locks():
    async with database.get_pool().acquire() as conn:
        return await conn.fetch("SELECT member_id, channel_id FROM keke_locks;")


# ---------------------------------------------------------------------------
# The "KEKE PARKED" block: one per keke, kept until the keke is back in service.
# ---------------------------------------------------------------------------

async def set_parked(keke_id, channel_id, message_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO keke_parked (keke_id, channel_id, message_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (keke_id) DO UPDATE
                SET channel_id = EXCLUDED.channel_id, message_id = EXCLUDED.message_id;
            """,
            keke_id, channel_id, message_id,
        )


async def get_parked():
    async with database.get_pool().acquire() as conn:
        return await conn.fetch("SELECT keke_id, channel_id, message_id FROM keke_parked;")


async def pop_parked(keke_id):
    """Remove and return the parked-block record for `keke_id` (None if none)."""
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow(
            "DELETE FROM keke_parked WHERE keke_id = $1 "
            "RETURNING keke_id, channel_id, message_id;",
            keke_id,
        )


async def reset_fuel(state):
    """Refill every keke of `state` to a full tank. Used on each bot start while
    refuelling is deferred (see cogs/keke.py RESET_FUEL_ON_DEPLOY)."""
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE kekes SET fuel_liters = $1 WHERE state = $2;",
            kc.TANK_CAPACITY_L, state,
        )


# ---------------------------------------------------------------------------
# Fuel: 0.25 L per km out of the 30 L tank. Refueling is deferred — nothing
# buys fuel, the tank only ever drains.
# ---------------------------------------------------------------------------

async def burn_fuel(keke_id, km, narration=""):
    """
    Drain `km * FUEL_PER_KM` litres from the unit's tank and log the burn.
    Returns {"liters", "new_fuel"}.
    """
    liters = cfg.to_money(km * kc.FUEL_PER_KM)
    if liters <= 0:
        raise BankError("No fuel to burn on zero distance.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            keke = await conn.fetchrow(
                "SELECT * FROM kekes WHERE keke_id = $1 FOR UPDATE;", keke_id
            )
            if keke is None:
                raise BankError("That keke does not exist.")
            if keke["fuel_liters"] < liters:
                raise BankError(
                    f"The keke only has {keke['fuel_liters']:.2f} L in the tank — "
                    f"the run needs {liters:.2f} L."
                )
            new_fuel = keke["fuel_liters"] - liters
            await conn.execute(
                "UPDATE kekes SET fuel_liters = $1 WHERE keke_id = $2;",
                new_fuel, keke_id,
            )
            await conn.execute(
                "INSERT INTO keke_fuel_log (keke_id, liters, narration) VALUES ($1, $2, $3);",
                keke_id, liters, (narration or kc.FUEL_LEDGER)[:100],
            )
            return {"liters": liters, "new_fuel": new_fuel}


# ---------------------------------------------------------------------------
# Fare at arrival: the player's CASH AT HAND -> the State Treasury.
# ---------------------------------------------------------------------------

async def credit_fare(state, player_id, player_name, fare):
    """
    Deduct `fare` from the player's cash at hand and credit the state's
    treasury, one atomic move. Returns {"fare", "new_cash",
    "new_treasury_balance", "ref"}.
    """
    fare = cfg.to_money(fare)
    if fare <= 0:
        raise BankError("The amount must be more than zero.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            player = await conn.fetchrow(
                "SELECT player_id, discord_id, character_name, cash_balance FROM players WHERE player_id = $1 FOR UPDATE;",
                player_id,
            )
            if player is None:
                raise BankError("No such player.")
            if player["cash_balance"] < fare:
                raise BankError(
                    f"Oga/Madam, your money nor reach, use leg waka. 😂🚶🏾"
                )

            new_cash = player["cash_balance"] - fare
            await conn.execute(
                "UPDATE players SET cash_balance = $1 WHERE player_id = $2;",
                new_cash, player_id,
            )

            treasury = await bank._system_account(conn, state, bank._TREASURY)
            new_treasury_balance = treasury["balance"] + fare
            await conn.execute(
                "UPDATE bank_accounts SET balance = balance + $1 WHERE account_id = $2;",
                fare, treasury["account_id"],
            )
            tx = await bank._insert_tx(conn, "keke_fare", None, treasury["account_id"],
                                       player["character_name"] or player_name,
                                       treasury["name"],
                                       fare, cfg.to_money(0), cfg.to_money(0),
                                       "Keke fare", state)
            player_party = {
                "account_id": None,
                "account_number": None,
                "account_type": "player",
                "display_name": player["character_name"] or player_name,
                "discord_id": player["discord_id"],
                "tier": "bronze",
                "balance": new_cash,
                "receipt_channel_id": None,
            }
            return {
                "fare": fare,
                "new_cash": new_cash,
                "new_treasury_balance": new_treasury_balance,
                "ref": tx["ref"],
                "kind": "keke_fare",
                "created_at": tx["created_at"],
                "state": state,
                "amount": fare,
                "fee": cfg.to_money(0),
                "tax": cfg.to_money(0),
                "total": fare,
                "narration": "Keke fare",
                "sender": player_party,
                "receiver": bank._party(treasury, new_treasury_balance),
                "from_label": player["character_name"] or player_name,
                "to_label": treasury["name"],
            }