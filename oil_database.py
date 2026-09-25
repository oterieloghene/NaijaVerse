"""
oil_database.py

The tanker/trailer oil supply chain: fleet ownership, the oil well and
refinery stockpiles (per state), refining jobs (not instant — resolved by a
background tick, same due_at pattern as nin_cards/residence_permits in
database.py), vehicle fuel, and interstate transfer requests.

Tables (created by init_tables(), safe to run on every start):
    oil_vehicles          one row per owned trailer/tanker: state, type,
                           name, fuel_liters, cargo.
    oil_vehicle_parked    the "parked here, facing this way" record — sibling
                           to keke_parked, not merged with it.
    oil_vehicle_fuel_log  fuel burned per trip (mirrors keke_fuel_log).
    oil_state_stock       one row per state: well barrels, refinery crude,
                           refinery fuel/gas reserve, NNPC pump fuel/gas, and
                           the last drill timestamp (for the cooldown).
    oil_refine_jobs       an in-progress `!refine crude` batch: qty, state,
                           due_at. A tick calls complete_due_refine_jobs()
                           to land the output once due_at has passed.
    oil_interstate_requests  the request -> quote -> (manual payment) ->
                           dispatch trail for cross-state product transfers.

Money moves (vehicle purchase, drill/refine equipment cost) reuse
bank_database's private _system_account()/_insert_tx() inside the SAME
transaction as the row change, same pattern as keke_database.buy_keke.
"""

import bank_config as cfg
import bank_database as bank
import database
import oil_config as oc
from bank_database import BankError


async def init_tables():
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oil_vehicles (
                vehicle_id    SERIAL PRIMARY KEY,
                state         TEXT NOT NULL,
                vehicle_type  TEXT NOT NULL CHECK (vehicle_type IN ('trailer', 'tanker')),
                name          TEXT NOT NULL,
                fuel_liters   NUMERIC(8, 3) NOT NULL DEFAULT 0 CHECK (fuel_liters >= 0),
                cargo_type    TEXT NOT NULL DEFAULT 'none'
                              CHECK (cargo_type IN ('none', 'crude', 'fuel', 'gas')),
                cargo_amount  NUMERIC(10, 3) NOT NULL DEFAULT 0 CHECK (cargo_amount >= 0),
                purchased_by  BIGINT,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oil_vehicle_parked (
                vehicle_id    INTEGER PRIMARY KEY REFERENCES oil_vehicles(vehicle_id) ON DELETE CASCADE,
                channel_id    BIGINT NOT NULL,
                message_id    BIGINT NOT NULL,
                stop_code     TEXT,
                direction     SMALLINT
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oil_vehicle_fuel_log (
                log_id        SERIAL PRIMARY KEY,
                vehicle_id    INTEGER NOT NULL REFERENCES oil_vehicles(vehicle_id) ON DELETE CASCADE,
                liters        NUMERIC(8, 3) NOT NULL CHECK (liters >= 0),
                narration     TEXT NOT NULL DEFAULT '',
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oil_state_stock (
                state           TEXT PRIMARY KEY,
                well_barrels    NUMERIC(10, 3) NOT NULL DEFAULT 0 CHECK (well_barrels >= 0),
                refinery_crude  NUMERIC(10, 3) NOT NULL DEFAULT 0 CHECK (refinery_crude >= 0),
                refinery_fuel   NUMERIC(10, 3) NOT NULL DEFAULT 0 CHECK (refinery_fuel >= 0),
                refinery_gas    NUMERIC(10, 3) NOT NULL DEFAULT 0 CHECK (refinery_gas >= 0),
                nnpc_fuel       NUMERIC(10, 3) NOT NULL DEFAULT 0 CHECK (nnpc_fuel >= 0),
                nnpc_gas        NUMERIC(10, 3) NOT NULL DEFAULT 0 CHECK (nnpc_gas >= 0),
                last_drill_at   TIMESTAMPTZ
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oil_refine_jobs (
                job_id        SERIAL PRIMARY KEY,
                state         TEXT NOT NULL,
                barrels       NUMERIC(10, 3) NOT NULL CHECK (barrels > 0),
                started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                due_at        TIMESTAMPTZ NOT NULL,
                completed_at  TIMESTAMPTZ
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oil_interstate_requests (
                request_id        SERIAL PRIMARY KEY,
                requesting_state  TEXT NOT NULL,
                owning_state      TEXT NOT NULL,
                product           TEXT NOT NULL CHECK (product IN ('fuel', 'gas', 'tanker')),
                qty               NUMERIC(10, 3),
                status            TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending', 'quoted', 'paid', 'dispatched', 'cancelled')),
                quoted_price      NUMERIC(14, 2),
                requested_by      BIGINT,
                quoted_by         BIGINT,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )


# ---------------------------------------------------------------------------
# Fleet queries & purchase
# ---------------------------------------------------------------------------

async def count_vehicles(state, vehicle_type):
    async with database.get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM oil_vehicles WHERE state = $1 AND vehicle_type = $2;",
            state, vehicle_type,
        )
        return row["n"]


async def get_vehicles(state):
    async with database.get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM oil_vehicles WHERE state = $1 ORDER BY vehicle_type, vehicle_id;", state
        )


async def get_vehicle(vehicle_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow("SELECT * FROM oil_vehicles WHERE vehicle_id = $1;", vehicle_id)


async def buy_vehicle(state, vehicle_type, buyer_id):
    """
    Buy one trailer or tanker out of the state's treasury. Auto-names it
    "<State> <Type> <N>" (N = count of that type already owned, +1).
    Returns {"vehicle_id", "name", "cost", "new_treasury_balance", "ref"}.
    """
    if vehicle_type not in ("trailer", "tanker"):
        raise BankError("Vehicle type must be 'trailer' or 'tanker'.")

    cost = oc.TRAILER_COST if vehicle_type == "trailer" else oc.TANKER_COST
    tank = oc.TRAILER_TANK_CAPACITY_L if vehicle_type == "trailer" else oc.TANKER_TANK_CAPACITY_L

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            treasury = await bank._system_account(conn, state, bank._TREASURY)
            if treasury["balance"] < cost:
                raise BankError(
                    f"The {state} Treasury only has {cfg.money(treasury['balance'])} — "
                    f"it needs {cfg.money(cost)} for a {vehicle_type}."
                )

            existing = await conn.fetchval(
                "SELECT COUNT(*) FROM oil_vehicles WHERE state = $1 AND vehicle_type = $2;",
                state, vehicle_type,
            )
            name = f"{state} {vehicle_type.capitalize()} {existing + 1}"

            new_treasury_balance = treasury["balance"] - cost
            await conn.execute(
                "UPDATE bank_accounts SET balance = balance - $1 WHERE account_id = $2;",
                cost, treasury["account_id"],
            )
            tx = await bank._insert_tx(conn, f"{vehicle_type}_purchase", treasury["account_id"], None,
                                       treasury["name"], None,
                                       cost, cfg.to_money(0), cfg.to_money(0),
                                       f"{name} purchase", state)

            row = await conn.fetchrow(
                """
                INSERT INTO oil_vehicles (state, vehicle_type, name, fuel_liters, purchased_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING vehicle_id;
                """,
                state, vehicle_type, name, tank, buyer_id,
            )
            return {
                "vehicle_id": row["vehicle_id"],
                "name": name,
                "cost": cost,
                "new_treasury_balance": new_treasury_balance,
                "ref": tx["ref"],
            }


# ---------------------------------------------------------------------------
# Parked state — sibling to keke_parked, same shape, own table.
# ---------------------------------------------------------------------------

async def set_parked(vehicle_id, channel_id, message_id, stop_code, direction=1):
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO oil_vehicle_parked (vehicle_id, channel_id, message_id, stop_code, direction)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (vehicle_id) DO UPDATE
                SET channel_id = EXCLUDED.channel_id,
                    message_id = EXCLUDED.message_id,
                    stop_code  = EXCLUDED.stop_code,
                    direction  = EXCLUDED.direction;
            """,
            vehicle_id, channel_id, message_id, stop_code, direction,
        )


async def get_parked():
    async with database.get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT vehicle_id, channel_id, message_id, stop_code, direction FROM oil_vehicle_parked;"
        )


async def pop_parked(vehicle_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow(
            "DELETE FROM oil_vehicle_parked WHERE vehicle_id = $1 "
            "RETURNING vehicle_id, channel_id, message_id, stop_code, direction;",
            vehicle_id,
        )


# ---------------------------------------------------------------------------
# Vehicle fuel — burn on trips, refuel at NNPC (litres only, no money).
# ---------------------------------------------------------------------------

async def burn_fuel(vehicle_id, liters, narration=""):
    """Drain `liters` from the vehicle's tank and log the burn."""
    liters = cfg.to_money(liters)
    if liters <= 0:
        raise BankError("No fuel to burn on zero distance.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            vehicle = await conn.fetchrow(
                "SELECT * FROM oil_vehicles WHERE vehicle_id = $1 FOR UPDATE;", vehicle_id
            )
            if vehicle is None:
                raise BankError("That vehicle does not exist.")
            if vehicle["fuel_liters"] < liters:
                raise BankError(
                    f"{vehicle['name']} only has {vehicle['fuel_liters']:.2f} L in the tank — "
                    f"the run needs {liters:.2f} L."
                )
            new_fuel = vehicle["fuel_liters"] - liters
            await conn.execute(
                "UPDATE oil_vehicles SET fuel_liters = $1 WHERE vehicle_id = $2;",
                new_fuel, vehicle_id,
            )
            await conn.execute(
                "INSERT INTO oil_vehicle_fuel_log (vehicle_id, liters, narration) VALUES ($1, $2, $3);",
                vehicle_id, liters, (narration or "Oil fleet trip")[:100],
            )
            return {"liters": liters, "new_fuel": new_fuel}


async def refuel_vehicle(vehicle_id, station_state, liters):
    """
    Move `liters` from `station_state`'s NNPC fuel stock into the vehicle's
    tank. No money involved. Fails if the station doesn't have enough fuel,
    or the tank can't hold it all.
    """
    liters = cfg.to_money(liters)
    if liters <= 0:
        raise BankError("Enter a litre amount greater than zero.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            vehicle = await conn.fetchrow(
                "SELECT * FROM oil_vehicles WHERE vehicle_id = $1 FOR UPDATE;", vehicle_id
            )
            if vehicle is None:
                raise BankError("That vehicle does not exist.")
            tank_cap = (oc.TRAILER_TANK_CAPACITY_L if vehicle["vehicle_type"] == "trailer"
                       else oc.TANKER_TANK_CAPACITY_L)
            if vehicle["fuel_liters"] + liters > tank_cap:
                raise BankError(
                    f"{vehicle['name']}'s tank only has room for "
                    f"{tank_cap - vehicle['fuel_liters']:.2f} more litres."
                )

            stock = await _get_stock(conn, station_state)
            if stock["nnpc_fuel"] < liters:
                raise BankError(
                    f"{station_state}'s NNPC station only has {stock['nnpc_fuel']:.2f} L of fuel."
                )

            await conn.execute(
                "UPDATE oil_state_stock SET nnpc_fuel = nnpc_fuel - $1 WHERE state = $2;",
                liters, station_state,
            )
            new_fuel = vehicle["fuel_liters"] + liters
            await conn.execute(
                "UPDATE oil_vehicles SET fuel_liters = $1 WHERE vehicle_id = $2;",
                new_fuel, vehicle_id,
            )
            return {"liters": liters, "new_fuel": new_fuel}


# ---------------------------------------------------------------------------
# Per-state stock (well, refinery reserve, NNPC pump)
# ---------------------------------------------------------------------------

async def _get_stock(conn, state):
    """Row for `state`, creating it (all zeros) on first use. Must be called
    with a connection already inside a transaction if the caller will lock it."""
    row = await conn.fetchrow("SELECT * FROM oil_state_stock WHERE state = $1;", state)
    if row is None:
        row = await conn.fetchrow(
            "INSERT INTO oil_state_stock (state) VALUES ($1) RETURNING *;", state
        )
    return row


async def get_stock(state):
    async with database.get_pool().acquire() as conn:
        return await _get_stock(conn, state)


# ---------------------------------------------------------------------------
# Drilling — per-use, not automatic. Debits treasury for equipment cost.
# ---------------------------------------------------------------------------

async def drill_crude(state, driller_id):
    """
    +DRILL_YIELD_BARRELS at the well (capped), debiting DRILL_COST from the
    treasury. Raises if the cooldown hasn't elapsed or the well is full.
    Returns {"barrels_added", "new_well_barrels", "new_treasury_balance", "ref"}.
    """
    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            await _get_stock(conn, state)
            stock = await conn.fetchrow(
                "SELECT * FROM oil_state_stock WHERE state = $1 FOR UPDATE;", state
            )
            # Cooldown check done in SQL, against the DB's own clock:
            cooldown_row = await conn.fetchrow(
                "SELECT (last_drill_at + $2 * INTERVAL '1 second') AS ready_at "
                "FROM oil_state_stock WHERE state = $1;",
                state, oc.DRILL_COOLDOWN_SECONDS,
            )
            ready_at = cooldown_row["ready_at"] if cooldown_row else None
            if ready_at is not None:
                still_waiting = await conn.fetchval("SELECT $1::timestamptz > NOW();", ready_at)
                if still_waiting:
                    raise BankError(f"The well is still cooling down — try again shortly.")

            if stock["well_barrels"] >= oc.WELL_STOCKPILE_CAP:
                raise BankError(f"The well is already full ({oc.WELL_STOCKPILE_CAP} barrels) — load a trailer first.")

            treasury = await bank._system_account(conn, state, bank._TREASURY)
            if treasury["balance"] < oc.DRILL_COST:
                raise BankError(
                    f"The {state} Treasury only has {cfg.money(treasury['balance'])} — "
                    f"drilling needs {cfg.money(oc.DRILL_COST)}."
                )

            added = min(oc.DRILL_YIELD_BARRELS, oc.WELL_STOCKPILE_CAP - stock["well_barrels"])
            new_well_barrels = stock["well_barrels"] + added

            new_treasury_balance = treasury["balance"] - oc.DRILL_COST
            await conn.execute(
                "UPDATE bank_accounts SET balance = balance - $1 WHERE account_id = $2;",
                oc.DRILL_COST, treasury["account_id"],
            )
            tx = await bank._insert_tx(conn, "drill_crude", treasury["account_id"], None,
                                       treasury["name"], None,
                                       oc.DRILL_COST, cfg.to_money(0), cfg.to_money(0),
                                       "Drilling equipment cost", state)

            await conn.execute(
                "UPDATE oil_state_stock SET well_barrels = $1, last_drill_at = NOW() WHERE state = $2;",
                new_well_barrels, state,
            )
            return {
                "barrels_added": added,
                "new_well_barrels": new_well_barrels,
                "new_treasury_balance": new_treasury_balance,
                "ref": tx["ref"],
            }


# ---------------------------------------------------------------------------
# Refining — NOT instant. Starts a job; a tick resolves it via
# complete_due_refine_jobs() once due_at has passed. Debits treasury up
# front, on start (equipment cost, separate from the time cost).
# ---------------------------------------------------------------------------

async def refine_crude(state, qty, refiner_id):
    """
    Start refining `qty` barrels sitting in the refinery's crude stockpile.
    Debits REFINE_COST immediately; deducts `qty` from refinery_crude
    immediately (it's committed to the job); output lands only when the job
    completes. Returns {"job_id", "due_at", "new_treasury_balance", "ref"}.
    """
    qty = cfg.to_money(qty)
    if qty <= 0:
        raise BankError("Enter a barrel amount greater than zero.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            stock = await conn.fetchrow(
                "SELECT * FROM oil_state_stock WHERE state = $1 FOR UPDATE;", state
            )
            if stock is None or stock["refinery_crude"] < qty:
                have = stock["refinery_crude"] if stock else 0
                raise BankError(f"The refinery only has {have:.2f} barrels of crude on hand.")

            fuel_yield = float(qty) * oc.BARREL_TO_FUEL_L
            gas_yield = float(qty) * oc.BARREL_TO_GAS_KG
            fuel_room = oc.REFINERY_FUEL_CAP - float(stock["refinery_fuel"])
            gas_room = oc.REFINERY_GAS_CAP - float(stock["refinery_gas"])
            if fuel_yield > fuel_room or gas_yield > gas_room:
                raise BankError(
                    f"That batch would overflow the reserve — room for {fuel_room:.2f} L fuel "
                    f"and {gas_room:.2f} kg gas, but {qty:.2f} barrels yields "
                    f"{fuel_yield:.2f} L fuel and {gas_yield:.2f} kg gas. Refine a smaller amount "
                    f"or offload the reserve first."
                )

            treasury = await bank._system_account(conn, state, bank._TREASURY)
            if treasury["balance"] < oc.REFINE_COST:
                raise BankError(
                    f"The {state} Treasury only has {cfg.money(treasury['balance'])} — "
                    f"refining needs {cfg.money(oc.REFINE_COST)}."
                )

            new_treasury_balance = treasury["balance"] - oc.REFINE_COST
            await conn.execute(
                "UPDATE bank_accounts SET balance = balance - $1 WHERE account_id = $2;",
                oc.REFINE_COST, treasury["account_id"],
            )
            tx = await bank._insert_tx(conn, "refine_crude", treasury["account_id"], None,
                                       treasury["name"], None,
                                       oc.REFINE_COST, cfg.to_money(0), cfg.to_money(0),
                                       "Refining equipment cost", state)

            await conn.execute(
                "UPDATE oil_state_stock SET refinery_crude = refinery_crude - $1 WHERE state = $2;",
                qty, state,
            )

            minutes = float(qty) / oc.REFINE_RATE_BARRELS_PER_MIN
            job = await conn.fetchrow(
                """
                INSERT INTO oil_refine_jobs (state, barrels, due_at)
                VALUES ($1, $2, NOW() + $3 * INTERVAL '1 minute')
                RETURNING job_id, due_at;
                """,
                state, qty, minutes,
            )
            return {
                "job_id": job["job_id"],
                "due_at": job["due_at"],
                "new_treasury_balance": new_treasury_balance,
                "ref": tx["ref"],
            }


async def get_due_refine_jobs(limit=10):
    async with database.get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM oil_refine_jobs WHERE completed_at IS NULL AND due_at <= NOW() "
            "ORDER BY due_at LIMIT $1;",
            limit,
        )


async def complete_refine_job(job_id):
    """
    Land a due job's output in the refinery reserve, capped at
    REFINERY_FUEL_CAP/REFINERY_GAS_CAP (excess is lost — the batch was
    already committed at start, this just stops the reserve overflowing).
    Returns {"fuel_added", "gas_added"} or None if already completed.
    """
    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow(
                "SELECT * FROM oil_refine_jobs WHERE job_id = $1 AND completed_at IS NULL FOR UPDATE;",
                job_id,
            )
            if job is None:
                return None

            stock = await conn.fetchrow(
                "SELECT * FROM oil_state_stock WHERE state = $1 FOR UPDATE;", job["state"]
            )
            fuel_yield = float(job["barrels"]) * oc.BARREL_TO_FUEL_L
            gas_yield = float(job["barrels"]) * oc.BARREL_TO_GAS_KG
            fuel_added = min(fuel_yield, oc.REFINERY_FUEL_CAP - float(stock["refinery_fuel"]))
            gas_added = min(gas_yield, oc.REFINERY_GAS_CAP - float(stock["refinery_gas"]))
            fuel_added = max(fuel_added, 0)
            gas_added = max(gas_added, 0)

            await conn.execute(
                "UPDATE oil_state_stock SET refinery_fuel = refinery_fuel + $1, "
                "refinery_gas = refinery_gas + $2 WHERE state = $3;",
                fuel_added, gas_added, job["state"],
            )
            await conn.execute(
                "UPDATE oil_refine_jobs SET completed_at = NOW() WHERE job_id = $1;", job_id
            )
            return {"fuel_added": fuel_added, "gas_added": gas_added}


# ---------------------------------------------------------------------------
# Loading / offloading — all manual (Commissioner of Petroleum).
# ---------------------------------------------------------------------------

async def load_crude(vehicle_id, state, qty):
    """Well stockpile -> trailer cargo."""
    qty = cfg.to_money(qty)
    if qty <= 0:
        raise BankError("Enter a barrel amount greater than zero.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            vehicle = await conn.fetchrow(
                "SELECT * FROM oil_vehicles WHERE vehicle_id = $1 FOR UPDATE;", vehicle_id
            )
            if vehicle is None or vehicle["vehicle_type"] != "trailer":
                raise BankError("That is not a trailer.")
            if vehicle["cargo_type"] not in ("none", "crude"):
                raise BankError(f"{vehicle['name']} is already carrying {vehicle['cargo_type']}.")
            if vehicle["cargo_amount"] + qty > oc.TRAILER_CARGO_CAPACITY:
                raise BankError(
                    f"{vehicle['name']} can only hold "
                    f"{oc.TRAILER_CARGO_CAPACITY - vehicle['cargo_amount']:.2f} more barrels."
                )

            stock = await conn.fetchrow(
                "SELECT * FROM oil_state_stock WHERE state = $1 FOR UPDATE;", state
            )
            if stock is None or stock["well_barrels"] < qty:
                have = stock["well_barrels"] if stock else 0
                raise BankError(f"The well only has {have:.2f} barrels on hand.")

            await conn.execute(
                "UPDATE oil_state_stock SET well_barrels = well_barrels - $1 WHERE state = $2;",
                qty, state,
            )
            await conn.execute(
                "UPDATE oil_vehicles SET cargo_type = 'crude', cargo_amount = cargo_amount + $1 "
                "WHERE vehicle_id = $2;",
                qty, vehicle_id,
            )
            return {"loaded": qty}


async def offload_crude(vehicle_id, state, qty):
    """Trailer cargo -> refinery crude stockpile."""
    qty = cfg.to_money(qty)
    if qty <= 0:
        raise BankError("Enter a barrel amount greater than zero.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            vehicle = await conn.fetchrow(
                "SELECT * FROM oil_vehicles WHERE vehicle_id = $1 FOR UPDATE;", vehicle_id
            )
            if vehicle is None or vehicle["cargo_type"] != "crude" or vehicle["cargo_amount"] < qty:
                raise BankError("That trailer isn't carrying enough crude.")

            stock = await conn.fetchrow(
                "SELECT * FROM oil_state_stock WHERE state = $1 FOR UPDATE;", state
            )
            room = oc.REFINERY_CRUDE_CAP - (stock["refinery_crude"] if stock else 0)
            if qty > room:
                raise BankError(f"The refinery only has room for {room:.2f} more barrels.")

            new_cargo = vehicle["cargo_amount"] - qty
            await conn.execute(
                "UPDATE oil_vehicles SET cargo_amount = $1, "
                "cargo_type = CASE WHEN $1 = 0 THEN 'none' ELSE cargo_type END "
                "WHERE vehicle_id = $2;",
                new_cargo, vehicle_id,
            )
            await conn.execute(
                "UPDATE oil_state_stock SET refinery_crude = refinery_crude + $1 WHERE state = $2;",
                qty, state,
            )
            return {"offloaded": qty}


async def load_product(vehicle_id, state, product, qty):
    """Refinery reserve (fuel or gas) -> tanker cargo. One product at a time."""
    if product not in ("fuel", "gas"):
        raise BankError("Product must be 'fuel' or 'gas'.")
    qty = cfg.to_money(qty)
    if qty <= 0:
        raise BankError("Enter an amount greater than zero.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            vehicle = await conn.fetchrow(
                "SELECT * FROM oil_vehicles WHERE vehicle_id = $1 FOR UPDATE;", vehicle_id
            )
            if vehicle is None or vehicle["vehicle_type"] != "tanker":
                raise BankError("That is not a tanker.")
            if vehicle["cargo_type"] not in ("none", product):
                raise BankError(f"{vehicle['name']} is already carrying {vehicle['cargo_type']}.")
            if vehicle["cargo_amount"] + qty > oc.TANKER_CARGO_CAPACITY:
                raise BankError(
                    f"{vehicle['name']} can only hold "
                    f"{oc.TANKER_CARGO_CAPACITY - vehicle['cargo_amount']:.2f} more {product}."
                )

            column = "refinery_fuel" if product == "fuel" else "refinery_gas"
            stock = await conn.fetchrow(
                "SELECT * FROM oil_state_stock WHERE state = $1 FOR UPDATE;", state
            )
            if stock is None or stock[column] < qty:
                have = stock[column] if stock else 0
                raise BankError(f"The refinery reserve only has {have:.2f} {product}.")

            await conn.execute(
                f"UPDATE oil_state_stock SET {column} = {column} - $1 WHERE state = $2;",
                qty, state,
            )
            await conn.execute(
                "UPDATE oil_vehicles SET cargo_type = $1, cargo_amount = cargo_amount + $2 "
                "WHERE vehicle_id = $3;",
                product, qty, vehicle_id,
            )
            return {"loaded": qty}


async def offload_product(vehicle_id, state, qty):
    """Tanker cargo -> the given state's NNPC pump stock. Fuel only for
    now — gas has no NNPC delivery path yet."""
    qty = cfg.to_money(qty)
    if qty <= 0:
        raise BankError("Enter an amount greater than zero.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            vehicle = await conn.fetchrow(
                "SELECT * FROM oil_vehicles WHERE vehicle_id = $1 FOR UPDATE;", vehicle_id
            )
            if vehicle is None or vehicle["cargo_type"] != "fuel" or vehicle["cargo_amount"] < qty:
                if vehicle is not None and vehicle["cargo_type"] == "gas":
                    raise BankError("Gas can't be offloaded to an NNPC station yet — fuel only for now.")
                raise BankError("That tanker isn't carrying enough fuel.")

            new_cargo = vehicle["cargo_amount"] - qty
            await conn.execute(
                "UPDATE oil_vehicles SET cargo_amount = $1, "
                "cargo_type = CASE WHEN $1 = 0 THEN 'none' ELSE cargo_type END "
                "WHERE vehicle_id = $2;",
                new_cargo, vehicle_id,
            )
            await _get_stock(conn, state)  # ensure the row exists
            await conn.execute(
                "UPDATE oil_state_stock SET nnpc_fuel = nnpc_fuel + $1 WHERE state = $2;",
                qty, state,
            )
            return {"offloaded": qty}


# ---------------------------------------------------------------------------
# Interstate requests — request -> quote -> (manual payment) -> dispatch.
# Payment itself happens through the existing bank transfer, not here.
# ---------------------------------------------------------------------------

async def create_request(requesting_state, owning_state, product, qty, requested_by):
    async with database.get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO oil_interstate_requests
                (requesting_state, owning_state, product, qty, requested_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *;
            """,
            requesting_state, owning_state, product, qty, requested_by,
        )
        return row


async def get_request(request_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM oil_interstate_requests WHERE request_id = $1;", request_id
        )


async def quote_request(request_id, price, quoted_by):
    async with database.get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM oil_interstate_requests WHERE request_id = $1 FOR UPDATE;", request_id
        )
        if row is None:
            raise BankError("That request does not exist.")
        if row["status"] != "pending":
            raise BankError(f"That request is already {row['status']}.")
        return await conn.fetchrow(
            """
            UPDATE oil_interstate_requests
            SET status = 'quoted', quoted_price = $1, quoted_by = $2
            WHERE request_id = $3
            RETURNING *;
            """,
            price, quoted_by, request_id,
        )


async def mark_paid(request_id):
    """Called once the requesting state's Commissioner of Commerce confirms
    the manual bank payment has gone through. Unlocks dispatch."""
    async with database.get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM oil_interstate_requests WHERE request_id = $1 FOR UPDATE;", request_id
        )
        if row is None:
            raise BankError("That request does not exist.")
        if row["status"] != "quoted":
            raise BankError("That request hasn't been quoted yet.")
        return await conn.fetchrow(
            "UPDATE oil_interstate_requests SET status = 'paid' WHERE request_id = $1 RETURNING *;",
            request_id,
        )


async def mark_dispatched(request_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow(
            "UPDATE oil_interstate_requests SET status = 'dispatched' WHERE request_id = $1 RETURNING *;",
            request_id,
        )
