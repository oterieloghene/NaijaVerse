"""
bus_database.py

The Abuja State korope fleet: one row per unit (zone, fuel in the tank, who
bought it), the fuel burned per trip, and the two money moves — the purchase
out of the State Treasury and the passenger fare from the player's cash at
hand into the State Treasury at arrival. Mirrors keke_database.py /
danfo_database.py exactly; only the table names, config values and the
can't-afford wording differ.

Tables (created automatically by init_tables(), safe to run on every start):
    koropes          one row per owned unit: zone, fuel litres, purchaser.
    korope_locks     the "can't write here while riding" lock placed on a
                     passenger's departure channel. Riders live only in
                     memory, so a restart mid-ride would leave the lock
                     behind forever; this table lets the bot find and lift
                     them on startup.
    korope_fuel_log  the fuel each unit burns per trip. Refueling is
                     DEFERRED, so this log never moves money — it only
                     records litres against the tank.

The money moves intentionally reuse bank_database's private _system_account() /
_insert_tx() inside the SAME transaction as the korope row / player cash update,
so a purchase or a fare is one atomic move — the treasury never moves without
the korope row or the player's cash moving with it.
"""

import bank_config as cfg
import bank_database as bank
import bus_config as bc
import database
from bank_database import BankError


async def init_tables():
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS koropes (
                korope_id     SERIAL PRIMARY KEY,
                state         TEXT NOT NULL,
                zone          TEXT NOT NULL CHECK (zone IN ('A', 'B', 'C')),
                fuel_liters   NUMERIC(6, 3) NOT NULL DEFAULT 50 CHECK (fuel_liters >= 0),
                purchased_by  BIGINT,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS korope_locks (
                member_id     BIGINT NOT NULL,
                channel_id    BIGINT NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (member_id, channel_id)
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS korope_fuel_log (
                log_id        SERIAL PRIMARY KEY,
                korope_id     INTEGER NOT NULL REFERENCES koropes(korope_id) ON DELETE CASCADE,
                liters        NUMERIC(6, 3) NOT NULL CHECK (liters >= 0),
                narration     TEXT NOT NULL DEFAULT '',
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS korope_parked (
                korope_id     INTEGER PRIMARY KEY REFERENCES koropes(korope_id) ON DELETE CASCADE,
                channel_id    BIGINT NOT NULL,
                message_id    BIGINT NOT NULL,
                stop_code     TEXT,
                direction     SMALLINT
            );
            """
        )
        await conn.execute("ALTER TABLE korope_parked ADD COLUMN IF NOT EXISTS stop_code TEXT;")
        await conn.execute("ALTER TABLE korope_parked ADD COLUMN IF NOT EXISTS direction SMALLINT;")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS korope_zone_state (
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

async def count_koropes(state):
    async with database.get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) AS n FROM koropes WHERE state = $1;", state)
        return row["n"]


async def get_koropes(state):
    async with database.get_pool().acquire() as conn:
        return await conn.fetch("SELECT * FROM koropes WHERE state = $1 ORDER BY korope_id;", state)


async def get_korope(korope_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM koropes WHERE korope_id = $1;", korope_id)


# ---------------------------------------------------------------------------
# !bus-start / !bus-stop: per-zone on/off switch for the Abuja Commissioner
# of Commerce. Persisted so a stopped zone STAYS stopped across a redeploy.
# ---------------------------------------------------------------------------

async def get_active_zones(state):
    """Zones ('A'/'B'/'C') currently allowed to run koropes for `state`. A zone
    with no row yet has never been stopped, so it defaults to active."""
    async with database.get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT zone, active FROM korope_zone_state WHERE state = $1;", state
        )
    stopped = {r["zone"] for r in rows if not r["active"]}
    return {z for z in bc.ZONES if z not in stopped}


async def set_zone_active(state, zone, active):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO korope_zone_state (state, zone, active)
            VALUES ($1, $2, $3)
            ON CONFLICT (state, zone) DO UPDATE SET active = EXCLUDED.active;
            """,
            state, zone, active,
        )


# ---------------------------------------------------------------------------
# !buy-bus (zone): the State Treasury pays for a new unit (full tank).
# ---------------------------------------------------------------------------

async def buy_korope(state, zone, buyer_id):
    """
    Buy one korope for `zone` ('A'/'B'/'C') out of the state's treasury.
    Returns {"korope_id", "zone", "cost", "new_treasury_balance", "ref"}.
    """
    if zone not in bc.ZONES:
        raise BankError("That is not a korope zone. Use A, B or C.")
    if await count_koropes(state) >= bc.MAX_BUSES:
        raise BankError(f"{state} can only own {bc.MAX_BUSES} koropes.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            treasury = await bank._system_account(conn, state, bank._TREASURY)
            if treasury["balance"] < bc.BUS_COST:
                raise BankError(
                    f"The {state} Treasury only has {cfg.money(treasury['balance'])} — "
                    f"it needs {cfg.money(bc.BUS_COST)} for a korope."
                )

            new_treasury_balance = treasury["balance"] - bc.BUS_COST
            await conn.execute(
                "UPDATE bank_accounts SET balance = balance - $1 WHERE account_id = $2;",
                bc.BUS_COST, treasury["account_id"],
            )
            tx = await bank._insert_tx(conn, "korope_purchase", treasury["account_id"], None,
                                       treasury["name"], None,
                                       bc.BUS_COST, cfg.to_money(0), cfg.to_money(0),
                                       f"Korope purchase (zone {zone})", state)

            row = await conn.fetchrow(
                """
                INSERT INTO koropes (state, zone, fuel_liters, purchased_by)
                VALUES ($1, $2, $3, $4)
                RETURNING korope_id;
                """,
                state, zone, bc.TANK_CAPACITY_L, buyer_id,
            )
            return {
                "korope_id": row["korope_id"],
                "zone": zone,
                "cost": bc.BUS_COST,
                "new_treasury_balance": new_treasury_balance,
                "ref": tx["ref"],
                "kind": "korope_purchase", "amount": bc.BUS_COST,
                "created_at": tx["created_at"], "state": state,
                "sender": bank._party(treasury, new_treasury_balance), "receiver": None,
                "from_label": treasury["name"], "to_label": f"{state} Korope (zone {zone})",
            }


async def add_lock(member_id, channel_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO korope_locks (member_id, channel_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING;",
            member_id, channel_id,
        )


async def remove_lock(member_id, channel_id):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM korope_locks WHERE member_id = $1 AND channel_id = $2;",
            member_id, channel_id,
        )


async def get_locks():
    async with database.get_pool().acquire() as conn:
        return await conn.fetch("SELECT member_id, channel_id FROM korope_locks;")


# ---------------------------------------------------------------------------
# The "KOROPE PARKED" block: one per korope, kept until it is back in service.
# ---------------------------------------------------------------------------

async def set_parked(korope_id, channel_id, message_id, stop_code, direction=1):
    """`stop_code` is the stop the korope is parked at and `direction` (+1 towards
    the route end, -1 back) the way it was heading: it re-enters the road there,
    that way, when it is put back into service."""
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO korope_parked (korope_id, channel_id, message_id, stop_code, direction)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (korope_id) DO UPDATE
                SET channel_id = EXCLUDED.channel_id,
                    message_id = EXCLUDED.message_id,
                    stop_code  = EXCLUDED.stop_code,
                    direction  = EXCLUDED.direction;
            """,
            korope_id, channel_id, message_id, stop_code, direction,
        )


async def get_parked():
    async with database.get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT korope_id, channel_id, message_id, stop_code, direction FROM korope_parked;"
        )


async def pop_parked(korope_id):
    """Remove and return the parked-block record for `korope_id` (None if none)."""
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow(
            "DELETE FROM korope_parked WHERE korope_id = $1 "
            "RETURNING korope_id, channel_id, message_id, stop_code, direction;",
            korope_id,
        )


async def reset_fuel(state):
    """Refill every korope of `state` to a full tank. Used on each bot start
    while refuelling is deferred (see cogs/bus.py RESET_FUEL_ON_DEPLOY)."""
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE koropes SET fuel_liters = $1 WHERE state = $2;",
            bc.TANK_CAPACITY_L, state,
        )


# ---------------------------------------------------------------------------
# Fuel: 0.75 L per km out of the 50 L tank. Refueling is deferred — nothing
# buys fuel, the tank only ever drains.
# ---------------------------------------------------------------------------

async def burn_fuel(korope_id, km, narration=""):
    """
    Drain `km * FUEL_PER_KM` litres from the unit's tank and log the burn.
    Returns {"liters", "new_fuel"}.
    """
    liters = cfg.to_money(km * bc.FUEL_PER_KM)
    if liters <= 0:
        raise BankError("No fuel to burn on zero distance.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            korope = await conn.fetchrow(
                "SELECT * FROM koropes WHERE korope_id = $1 FOR UPDATE;", korope_id
            )
            if korope is None:
                raise BankError("That korope does not exist.")
            if korope["fuel_liters"] < liters:
                raise BankError(
                    f"The korope only has {korope['fuel_liters']:.2f} L in the tank — "
                    f"the run needs {liters:.2f} L."
                )
            new_fuel = korope["fuel_liters"] - liters
            await conn.execute(
                "UPDATE koropes SET fuel_liters = $1 WHERE korope_id = $2;",
                new_fuel, korope_id,
            )
            await conn.execute(
                "INSERT INTO korope_fuel_log (korope_id, liters, narration) VALUES ($1, $2, $3);",
                korope_id, liters, (narration or bc.FUEL_LEDGER)[:100],
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
                    "Ah ah, Oga/Madam, kudi naka no reach, waka o! 😂🚶🏾"
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
            tx = await bank._insert_tx(conn, "korope_fare", None, treasury["account_id"],
                                       player["character_name"] or player_name,
                                       treasury["name"],
                                       fare, cfg.to_money(0), cfg.to_money(0),
                                       "Korope fare", state)
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
                "kind": "korope_fare",
                "created_at": tx["created_at"],
                "state": state,
                "amount": fare,
                "fee": cfg.to_money(0),
                "tax": cfg.to_money(0),
                "total": fare,
                "narration": "Korope fare",
                "sender": player_party,
                "receiver": bank._party(treasury, new_treasury_balance),
                "from_label": player["character_name"] or player_name,
                "to_label": treasury["name"],
            }
