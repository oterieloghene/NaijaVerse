"""
danfo_database.py

The Lagos State danfo fleet: one row per unit (zone, fuel in the tank, who
bought it), the fuel burned per trip, and the two money moves — the purchase
out of the State Treasury and the passenger fare from the player's cash at
hand into the State Treasury at arrival. Mirrors keke_database.py exactly;
only the table names, config values and the can't-afford wording differ.

Tables (created automatically by init_tables(), safe to run on every start):
    danfos           one row per owned unit: zone, fuel litres, purchaser.
    danfo_locks      the "can't write here while riding" lock placed on a
                     passenger's departure channel. Riders live only in
                     memory, so a restart mid-ride would leave the lock
                     behind forever; this table lets the bot find and lift
                     them on startup.
    danfo_fuel_log   the fuel each unit burns per trip. Refueling is
                     DEFERRED, so this log never moves money — it only
                     records litres against the tank.

The money moves intentionally reuse bank_database's private _system_account() /
_insert_tx() inside the SAME transaction as the danfo row / player cash update,
so a purchase or a fare is one atomic move — the treasury never moves without
the danfo row or the player's cash moving with it.
"""

import bank_config as cfg
import bank_database as bank
import database
import danfo_config as dc
from bank_database import BankError


async def init_tables():
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS danfos (
                danfo_id      SERIAL PRIMARY KEY,
                state         TEXT NOT NULL,
                zone          TEXT NOT NULL CHECK (zone IN ('A', 'B', 'C')),
                fuel_liters   NUMERIC(6, 3) NOT NULL DEFAULT 70 CHECK (fuel_liters >= 0),
                purchased_by  BIGINT,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS danfo_locks (
                member_id     BIGINT NOT NULL,
                channel_id    BIGINT NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (member_id, channel_id)
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS danfo_fuel_log (
                log_id        SERIAL PRIMARY KEY,
                danfo_id      INTEGER NOT NULL REFERENCES danfos(danfo_id) ON DELETE CASCADE,
                liters        NUMERIC(6, 3) NOT NULL CHECK (liters >= 0),
                narration     TEXT NOT NULL DEFAULT '',
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS danfo_parked (
                danfo_id      INTEGER PRIMARY KEY REFERENCES danfos(danfo_id) ON DELETE CASCADE,
                channel_id    BIGINT NOT NULL,
                message_id    BIGINT NOT NULL,
                stop_code     TEXT,
                direction     SMALLINT
            );
            """
        )
        await conn.execute("ALTER TABLE danfo_parked ADD COLUMN IF NOT EXISTS stop_code TEXT;")
        await conn.execute("ALTER TABLE danfo_parked ADD COLUMN IF NOT EXISTS direction SMALLINT;")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS danfo_zone_state (
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

async def count_danfos(state):
    async with database.get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) AS n FROM danfos WHERE state = $1;", state)
        return row["n"]


async def get_danfos(state):
    async with database.get_pool().acquire() as conn:
        return await conn.fetch("SELECT * FROM danfos WHERE state = $1 ORDER BY danfo_id;", state)


async def get_danfo(danfo_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM danfos WHERE danfo_id = $1;", danfo_id)


# ---------------------------------------------------------------------------
# !danfo-start / !danfo-stop: per-zone on/off switch for the Lagos Commissioner
# of Commerce. Persisted so a stopped zone STAYS stopped across a redeploy.
# ---------------------------------------------------------------------------

async def get_active_zones(state):
    """Zones ('A'/'B'/'C') currently allowed to run danfos for `state`. A zone
    with no row yet has never been stopped, so it defaults to active."""
    async with database.get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT zone, active FROM danfo_zone_state WHERE state = $1;", state
        )
    stopped = {r["zone"] for r in rows if not r["active"]}
    return {z for z in dc.ZONES if z not in stopped}


async def set_zone_active(state, zone, active):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO danfo_zone_state (state, zone, active)
            VALUES ($1, $2, $3)
            ON CONFLICT (state, zone) DO UPDATE SET active = EXCLUDED.active;
            """,
            state, zone, active,
        )


# ---------------------------------------------------------------------------
# !buy-danfo (zone): the State Treasury pays for a new unit (full tank).
# ---------------------------------------------------------------------------

async def buy_danfo(state, zone, buyer_id):
    """
    Buy one danfo for `zone` ('A'/'B'/'C') out of the state's treasury.
    Returns {"danfo_id", "zone", "cost", "new_treasury_balance", "ref"}.
    """
    if zone not in dc.ZONES:
        raise BankError("That is not a danfo zone. Use A, B or C.")
    if await count_danfos(state) >= dc.MAX_DANFOS:
        raise BankError(f"{state} can only own {dc.MAX_DANFOS} danfos.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            treasury = await bank._system_account(conn, state, bank._TREASURY)
            if treasury["balance"] < dc.DANFO_COST:
                raise BankError(
                    f"The {state} Treasury only has {cfg.money(treasury['balance'])} — "
                    f"it needs {cfg.money(dc.DANFO_COST)} for a danfo."
                )

            new_treasury_balance = treasury["balance"] - dc.DANFO_COST
            await conn.execute(
                "UPDATE bank_accounts SET balance = balance - $1 WHERE account_id = $2;",
                dc.DANFO_COST, treasury["account_id"],
            )
            tx = await bank._insert_tx(conn, "danfo_purchase", treasury["account_id"], None,
                                       treasury["name"], None,
                                       dc.DANFO_COST, cfg.to_money(0), cfg.to_money(0),
                                       f"Danfo purchase (zone {zone})", state)

            row = await conn.fetchrow(
                """
                INSERT INTO danfos (state, zone, fuel_liters, purchased_by)
                VALUES ($1, $2, $3, $4)
                RETURNING danfo_id;
                """,
                state, zone, dc.TANK_CAPACITY_L, buyer_id,
            )
            return {
                "danfo_id": row["danfo_id"],
                "zone": zone,
                "cost": dc.DANFO_COST,
                "new_treasury_balance": new_treasury_balance,
                "ref": tx["ref"],
                "kind": "danfo_purchase", "amount": dc.DANFO_COST,
                "created_at": tx["created_at"], "state": state,
                "sender": bank._party(treasury, new_treasury_balance), "receiver": None,
                "from_label": treasury["name"], "to_label": f"{state} Danfo (zone {zone})",
            }


async def add_lock(member_id, channel_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO danfo_locks (member_id, channel_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING;",
            member_id, channel_id,
        )


async def remove_lock(member_id, channel_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM danfo_locks WHERE member_id = $1 AND channel_id = $2;",
            member_id, channel_id,
        )


async def get_locks():
    async with database.get_pool().acquire() as conn:
        return await conn.fetch("SELECT member_id, channel_id FROM danfo_locks;")


# ---------------------------------------------------------------------------
# The "DANFO PARKED" block: one per danfo, kept until it is back in service.
# ---------------------------------------------------------------------------

async def set_parked(danfo_id, channel_id, message_id, stop_code, direction=1):
    """`stop_code` is the stop the danfo is parked at and `direction` (+1 towards
    the route end, -1 back) the way it was heading: it re-enters the road there,
    that way, when it is put back into service."""
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO danfo_parked (danfo_id, channel_id, message_id, stop_code, direction)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (danfo_id) DO UPDATE
                SET channel_id = EXCLUDED.channel_id,
                    message_id = EXCLUDED.message_id,
                    stop_code  = EXCLUDED.stop_code,
                    direction  = EXCLUDED.direction;
            """,
            danfo_id, channel_id, message_id, stop_code, direction,
        )


async def get_parked():
    async with database.get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT danfo_id, channel_id, message_id, stop_code, direction FROM danfo_parked;"
        )


async def pop_parked(danfo_id):
    """Remove and return the parked-block record for `danfo_id` (None if none)."""
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow(
            "DELETE FROM danfo_parked WHERE danfo_id = $1 "
            "RETURNING danfo_id, channel_id, message_id, stop_code, direction;",
            danfo_id,
        )


async def reset_fuel(state):
    """Refill every danfo of `state` to a full tank. Used on each bot start
    while refuelling is deferred (see cogs/danfo.py RESET_FUEL_ON_DEPLOY)."""
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE danfos SET fuel_liters = $1 WHERE state = $2;",
            dc.TANK_CAPACITY_L, state,
        )


# ---------------------------------------------------------------------------
# Fuel: 1 L per km out of the 70 L tank. Refueling is deferred — nothing buys
# fuel, the tank only ever drains.
# ---------------------------------------------------------------------------

async def burn_fuel(danfo_id, km, narration=""):
    """
    Drain `km * FUEL_PER_KM` litres from the unit's tank and log the burn.
    Returns {"liters", "new_fuel"}.
    """
    liters = cfg.to_money(km * dc.FUEL_PER_KM)
    if liters <= 0:
        raise BankError("No fuel to burn on zero distance.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            danfo = await conn.fetchrow(
                "SELECT * FROM danfos WHERE danfo_id = $1 FOR UPDATE;", danfo_id
            )
            if danfo is None:
                raise BankError("That danfo does not exist.")
            if danfo["fuel_liters"] < liters:
                raise BankError(
                    f"The danfo only has {danfo['fuel_liters']:.2f} L in the tank — "
                    f"the run needs {liters:.2f} L."
                )
            new_fuel = danfo["fuel_liters"] - liters
            await conn.execute(
                "UPDATE danfos SET fuel_liters = $1 WHERE danfo_id = $2;",
                new_fuel, danfo_id,
            )
            await conn.execute(
                "INSERT INTO danfo_fuel_log (danfo_id, liters, narration) VALUES ($1, $2, $3);",
                danfo_id, liters, (narration or dc.FUEL_LEDGER)[:100],
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
                    "Ah ah, Oga/Madam, owo e o to o, nitori naa, waka o! 😂🚶🏾"
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
            tx = await bank._insert_tx(conn, "danfo_fare", None, treasury["account_id"],
                                       player["character_name"] or player_name,
                                       treasury["name"],
                                       fare, cfg.to_money(0), cfg.to_money(0),
                                       "Danfo fare", state)
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
                "kind": "danfo_fare",
                "created_at": tx["created_at"],
                "state": state,
                "amount": fare,
                "fee": cfg.to_money(0),
                "tax": cfg.to_money(0),
                "total": fare,
                "narration": "Danfo fare",
                "sender": player_party,
                "receiver": bank._party(treasury, new_treasury_balance),
                "from_label": player["character_name"] or player_name,
                "to_label": treasury["name"],
            }
