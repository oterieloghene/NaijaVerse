"""
bank_database.py

All the banking data and the rules that protect it: accounts, daily limits, and the money moves
(transfer, withdraw, deposit). Nothing here talks to Discord, so the ATM commands and the phone
both call the very same functions and behave identically.

Tables (created automatically by init_tables(), safe to run on every start):
    bank_accounts       one row per account: personal (a player), org (a business/organisation),
                        treasury and bank_revenue (one of each per state, created on first use)
    bank_daily_usage    how much an account has transferred / withdrawn / deposited today (WAT)
    bank_transactions   every completed transaction, with its reference number

Every money move runs inside ONE database transaction with the accounts locked, so two things
happening at once can never overspend an account or skip a limit.

Cash (players.cash_balance) and bank balance (bank_accounts.balance) are separate, as before.
"""

import asyncpg

import bank_config as cfg
import database

_PERSONAL, _ORG, _TREASURY, _REVENUE = "personal", "org", "treasury", "bank_revenue"

# An account plus its owner's current name and Discord id (for personal accounts).
ACCOUNT_SELECT = """
    SELECT a.*, COALESCE(p.character_name, a.name) AS display_name, p.discord_id AS discord_id
    FROM bank_accounts a
    LEFT JOIN players p ON p.player_id = a.player_id
"""


class BankError(Exception):
    """A problem the player should be told about. .message is safe to show."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def init_tables():
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_accounts (
                account_id      SERIAL PRIMARY KEY,
                account_number  TEXT NOT NULL UNIQUE,
                account_type    TEXT NOT NULL CHECK (account_type IN ('personal', 'org', 'treasury', 'bank_revenue')),
                player_id       INTEGER UNIQUE REFERENCES players(player_id) ON DELETE CASCADE,
                name            TEXT NOT NULL DEFAULT '',
                state           TEXT NOT NULL,
                tier            TEXT NOT NULL DEFAULT 'bronze',
                balance         NUMERIC(20, 2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
                opened_by       BIGINT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS bank_accounts_system_uq
            ON bank_accounts (state, account_type)
            WHERE account_type IN ('treasury', 'bank_revenue');
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_daily_usage (
                account_id   INTEGER NOT NULL REFERENCES bank_accounts(account_id) ON DELETE CASCADE,
                day          DATE NOT NULL,
                transferred  NUMERIC(20, 2) NOT NULL DEFAULT 0,
                withdrawn    NUMERIC(20, 2) NOT NULL DEFAULT 0,
                deposited    NUMERIC(20, 2) NOT NULL DEFAULT 0,
                PRIMARY KEY (account_id, day)
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_transactions (
                id            SERIAL PRIMARY KEY,
                ref           TEXT NOT NULL UNIQUE,
                kind          TEXT NOT NULL,
                from_account  INTEGER REFERENCES bank_accounts(account_id) ON DELETE SET NULL,
                to_account    INTEGER REFERENCES bank_accounts(account_id) ON DELETE SET NULL,
                from_name     TEXT,
                to_name       TEXT,
                amount        NUMERIC(20, 2) NOT NULL,
                fee           NUMERIC(20, 2) NOT NULL DEFAULT 0,
                tax           NUMERIC(20, 2) NOT NULL DEFAULT 0,
                narration     TEXT NOT NULL DEFAULT '',
                state         TEXT NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )


# ---------------------------------------------------------------------------
# Reading accounts
# ---------------------------------------------------------------------------

async def get_account(account_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow(ACCOUNT_SELECT + " WHERE a.account_id = $1;", account_id)


async def get_account_by_player(player_id):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow(ACCOUNT_SELECT + " WHERE a.player_id = $1;", player_id)


async def get_account_by_number(account_number):
    async with database.get_pool().acquire() as conn:
        return await conn.fetchrow(ACCOUNT_SELECT + " WHERE a.account_number = $1;", str(account_number))


async def get_usage(account_id):
    """Today's totals for an account: {'transferred', 'withdrawn', 'deposited'} (zeros if none)."""
    async with database.get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT transferred, withdrawn, deposited FROM bank_daily_usage WHERE account_id = $1 AND day = $2;",
            account_id, cfg.today_wat(),
        )
    zero = cfg.to_money(0)
    if row is None:
        return {"transferred": zero, "withdrawn": zero, "deposited": zero}
    return dict(row)


# ---------------------------------------------------------------------------
# Opening accounts and changing tiers
# ---------------------------------------------------------------------------

async def open_personal_account(player_id, character_name, state, opened_by_discord_id):
    """Open a Bronze account for a player. Raises BankError if they already have one."""
    already = BankError("That player already has a bank account.")
    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            if await conn.fetchval("SELECT 1 FROM bank_accounts WHERE player_id = $1;", player_id):
                raise already
            for _ in range(20):
                number = cfg.new_account_number()
                row = await conn.fetchrow(
                    """
                    INSERT INTO bank_accounts (account_number, account_type, player_id, name, state, tier, opened_by)
                    VALUES ($1, 'personal', $2, $3, $4, $5, $6)
                    ON CONFLICT DO NOTHING
                    RETURNING account_id;
                    """,
                    number, player_id, character_name, state, cfg.DEFAULT_TIER, opened_by_discord_id,
                )
                if row:
                    await conn.execute("UPDATE players SET bank_account = $1 WHERE player_id = $2;", number, player_id)
                    return await conn.fetchrow(ACCOUNT_SELECT + " WHERE a.account_id = $1;", row["account_id"])
                if await conn.fetchval("SELECT 1 FROM bank_accounts WHERE player_id = $1;", player_id):
                    raise already                       # someone opened it a split second ago
            raise BankError("Couldn't find a free account number. Please try again.")


async def create_org_account(state, name):
    """A business / organisation account (what 'Send to Business' pays into)."""
    async with database.get_pool().acquire() as conn:
        for _ in range(20):
            row = await conn.fetchrow(
                """
                INSERT INTO bank_accounts (account_number, account_type, name, state, tier)
                VALUES ($1, 'org', $2, $3, 'platinum')
                ON CONFLICT DO NOTHING
                RETURNING account_id;
                """,
                cfg.new_account_number(), name, state,
            )
            if row:
                return await conn.fetchrow(ACCOUNT_SELECT + " WHERE a.account_id = $1;", row["account_id"])
    raise BankError("Couldn't find a free account number. Please try again.")


async def set_tier(account_id, tier):
    """Returns (old_tier, updated_account_row). `tier` must be a key of bank_config.TIERS."""
    if tier not in cfg.TIERS:
        raise BankError("Unknown tier.")
    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchval("SELECT tier FROM bank_accounts WHERE account_id = $1 FOR UPDATE;", account_id)
            if old is None:
                raise BankError("That account doesn't exist.")
            await conn.execute("UPDATE bank_accounts SET tier = $1 WHERE account_id = $2;", tier, account_id)
            return old, await conn.fetchrow(ACCOUNT_SELECT + " WHERE a.account_id = $1;", account_id)


# ---------------------------------------------------------------------------
# Internals for money moves
# ---------------------------------------------------------------------------

async def _system_account(conn, state, kind):
    """The state's treasury / bank_revenue account, created the first time it's needed."""
    query = "SELECT * FROM bank_accounts WHERE state = $1 AND account_type = $2;"
    row = await conn.fetchrow(query, state, kind)
    if row:
        return row
    title = "Treasury" if kind == _TREASURY else "Bank Revenue"
    for _ in range(20):
        row = await conn.fetchrow(
            """
            INSERT INTO bank_accounts (account_number, account_type, name, state, tier)
            VALUES ($1, $2, $3, $4, 'platinum')
            ON CONFLICT DO NOTHING
            RETURNING *;
            """,
            cfg.new_account_number(), kind, f"{state} {title}", state,
        )
        if row:
            return row
        row = await conn.fetchrow(query, state, kind)
        if row:
            return row
    raise BankError("Couldn't set up the bank's accounts. Please try again.")


async def _lock(conn, account_ids):
    """Lock accounts (always in id order, so two moves can't deadlock). {account_id: row}."""
    rows = await conn.fetch(
        ACCOUNT_SELECT + " WHERE a.account_id = ANY($1::int[]) ORDER BY a.account_id FOR UPDATE OF a;",
        sorted(set(account_ids)),
    )
    return {r["account_id"]: r for r in rows}


async def _usage_row(conn, account_id, day):
    await conn.execute(
        "INSERT INTO bank_daily_usage (account_id, day) VALUES ($1, $2) ON CONFLICT DO NOTHING;",
        account_id, day,
    )
    return await conn.fetchrow(
        "SELECT * FROM bank_daily_usage WHERE account_id = $1 AND day = $2 FOR UPDATE;", account_id, day
    )


async def _insert_tx(conn, kind, from_acc, to_acc, from_name, to_name, amount, fee, tax, narration, state):
    for _ in range(20):
        row = await conn.fetchrow(
            """
            INSERT INTO bank_transactions
                (ref, kind, from_account, to_account, from_name, to_name, amount, fee, tax, narration, state)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (ref) DO NOTHING
            RETURNING ref, created_at;
            """,
            cfg.new_tx_ref(), kind, from_acc, to_acc, from_name, to_name, amount, fee, tax, narration, state,
        )
        if row:
            return row
    raise BankError("Couldn't record the transaction. Nothing was charged.")


def _party(row, balance_after):
    return {
        "account_id": row["account_id"],
        "account_number": row["account_number"],
        "display_name": row["display_name"],
        "discord_id": row["discord_id"],
        "tier": row["tier"],
        "balance": balance_after,
    }


def _need_personal(row):
    if row is None or row["account_type"] != _PERSONAL:
        raise BankError("You don't have a bank account.")


# ---------------------------------------------------------------------------
# Money moves
# ---------------------------------------------------------------------------

async def transfer(sender_account_id, receiver_number, amount, narration="", *, expect_type=None, dry_run=False):
    """
    Move `amount` from the sender to the account with number `receiver_number`. The sender also
    pays the fee and tax on top; the fee goes to the sender's-state bank_revenue account and the
    tax to that state's treasury.

    expect_type   None, 'personal' or 'org': insist the receiver is that kind of account
                  (the phone's Send to Person / Send to Business).
    dry_run       do every check and return the breakdown, but don't move any money
                  (used for the phone's confirmation screen).

    Returns a dict: ref, kind, amount, fee, tax, total, narration, state, created_at,
    sender and receiver ({account_id, account_number, display_name, discord_id, tier, balance
    (AFTER the move)}). Raises BankError with a player-friendly message if it can't be done.
    """
    amount = cfg.to_money(amount)
    if amount <= 0:
        raise BankError("The amount must be more than zero.")
    fee, tax = cfg.fee_and_tax(amount)
    total = amount + fee + tax
    narration = (narration or "").strip()[:100]
    day = cfg.today_wat()

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            sender = await conn.fetchrow(ACCOUNT_SELECT + " WHERE a.account_id = $1;", sender_account_id)
            _need_personal(sender)
            receiver = await conn.fetchrow(ACCOUNT_SELECT + " WHERE a.account_number = $1;", str(receiver_number))
            if receiver is None:
                raise BankError("No account exists with that number.")
            if receiver["account_id"] == sender["account_id"]:
                raise BankError("You can't send money to your own account.")
            if receiver["account_type"] not in (_PERSONAL, _ORG):
                raise BankError("You can't send money to that account.")
            if expect_type and receiver["account_type"] != expect_type:
                raise BankError("That isn't a business account." if expect_type == _ORG
                                else "That isn't a personal account.")

            revenue = await _system_account(conn, sender["state"], _REVENUE)
            treasury = await _system_account(conn, sender["state"], _TREASURY)
            locked = await _lock(conn, [sender["account_id"], receiver["account_id"],
                                        revenue["account_id"], treasury["account_id"]])
            sender, receiver = locked[sender["account_id"]], locked[receiver["account_id"]]

            # 1. today's transfer limit (fees don't count, only the amount sent)
            usage = await _usage_row(conn, sender["account_id"], day)
            limit = cfg.TIERS[sender["tier"]]["daily_transfer"]
            if usage["transferred"] + amount > limit:
                left = max(limit - usage["transferred"], cfg.to_money(0))
                raise BankError(f"That's over your daily transfer limit of {cfg.money(limit)}. "
                                f"You can still send {cfg.money(left)} today.")

            # 2. enough money for amount + fee + tax
            if sender["balance"] < total:
                short = total - sender["balance"]
                raise BankError(f"Insufficient funds. You need {cfg.money(total)} (amount + fee + tax) "
                                f"but have {cfg.money(sender['balance'])}. You're short by {cfg.money(short)}.")

            # 3. the receiver's account must be able to hold it
            cap = cfg.TIERS[receiver["tier"]]["max_balance"]
            if cap is not None and receiver["balance"] + amount > cap:
                raise BankError("The receiving account can't accept that amount right now.")

            result = {
                "kind": "transfer", "amount": amount, "fee": fee, "tax": tax, "total": total,
                "narration": narration, "state": sender["state"],
                "sender": _party(sender, sender["balance"] - total),
                "receiver": _party(receiver, receiver["balance"] + amount),
            }
            if dry_run:
                result.update(ref=None, created_at=None)
                return result

            await conn.execute("UPDATE bank_accounts SET balance = balance - $1 WHERE account_id = $2;",
                               total, sender["account_id"])
            await conn.execute("UPDATE bank_accounts SET balance = balance + $1 WHERE account_id = $2;",
                               amount, receiver["account_id"])
            if fee > 0:
                await conn.execute("UPDATE bank_accounts SET balance = balance + $1 WHERE account_id = $2;",
                                   fee, revenue["account_id"])
            if tax > 0:
                await conn.execute("UPDATE bank_accounts SET balance = balance + $1 WHERE account_id = $2;",
                                   tax, treasury["account_id"])
            await conn.execute(
                "UPDATE bank_daily_usage SET transferred = transferred + $1 WHERE account_id = $2 AND day = $3;",
                amount, sender["account_id"], day,
            )
            tx = await _insert_tx(conn, "transfer", sender["account_id"], receiver["account_id"],
                                  sender["display_name"], receiver["display_name"],
                                  amount, fee, tax, narration, sender["state"])
            result.update(ref=tx["ref"], created_at=tx["created_at"])
            return result


async def withdraw(account_id, amount):
    """Bank balance -> the player's cash (used at the ATM). Same result shape as transfer()."""
    amount = cfg.to_money(amount)
    if amount <= 0:
        raise BankError("The amount must be more than zero.")
    day = cfg.today_wat()

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            locked = await _lock(conn, [account_id])
            acc = locked.get(account_id)
            _need_personal(acc)

            usage = await _usage_row(conn, account_id, day)
            limit = cfg.TIERS[acc["tier"]]["daily_withdrawal"]
            if usage["withdrawn"] + amount > limit:
                left = max(limit - usage["withdrawn"], cfg.to_money(0))
                raise BankError(f"That's over your daily withdrawal limit of {cfg.money(limit)}. "
                                f"You can still withdraw {cfg.money(left)} today.")
            if acc["balance"] < amount:
                raise BankError(f"Insufficient funds. Your balance is {cfg.money(acc['balance'])}.")

            await conn.execute("UPDATE bank_accounts SET balance = balance - $1 WHERE account_id = $2;", amount, account_id)
            await conn.execute("UPDATE players SET cash_balance = cash_balance + $1 WHERE player_id = $2;",
                               amount, acc["player_id"])
            await conn.execute(
                "UPDATE bank_daily_usage SET withdrawn = withdrawn + $1 WHERE account_id = $2 AND day = $3;",
                amount, account_id, day,
            )
            zero = cfg.to_money(0)
            tx = await _insert_tx(conn, "withdrawal", account_id, None, acc["display_name"], None,
                                  amount, zero, zero, "", acc["state"])
            return {
                "kind": "withdrawal", "ref": tx["ref"], "created_at": tx["created_at"], "state": acc["state"],
                "amount": amount, "fee": zero, "tax": zero, "total": amount, "narration": "",
                "sender": _party(acc, acc["balance"] - amount), "receiver": None,
            }


async def deposit(account_id, amount):
    """The player's cash -> their bank balance (Bank Staff use this with !dep)."""
    amount = cfg.to_money(amount)
    if amount <= 0:
        raise BankError("The amount must be more than zero.")
    day = cfg.today_wat()

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            locked = await _lock(conn, [account_id])
            acc = locked.get(account_id)
            _need_personal(acc)

            cash = await conn.fetchval("SELECT cash_balance FROM players WHERE player_id = $1 FOR UPDATE;",
                                       acc["player_id"])
            if cash is None or cash < amount:
                raise BankError(f"{acc['display_name']} only has {cfg.money(cash or 0)} in cash.")

            usage = await _usage_row(conn, account_id, day)
            limit = cfg.TIERS[acc["tier"]]["daily_deposit"]
            if usage["deposited"] + amount > limit:
                left = max(limit - usage["deposited"], cfg.to_money(0))
                raise BankError(f"That's over the account's daily deposit limit of {cfg.money(limit)}. "
                                f"It can still take {cfg.money(left)} today.")
            cap = cfg.TIERS[acc["tier"]]["max_balance"]
            if cap is not None and acc["balance"] + amount > cap:
                raise BankError(f"That would go over the account's maximum balance of {cfg.money(cap)}.")

            await conn.execute("UPDATE players SET cash_balance = cash_balance - $1 WHERE player_id = $2;",
                               amount, acc["player_id"])
            await conn.execute("UPDATE bank_accounts SET balance = balance + $1 WHERE account_id = $2;", amount, account_id)
            await conn.execute(
                "UPDATE bank_daily_usage SET deposited = deposited + $1 WHERE account_id = $2 AND day = $3;",
                amount, account_id, day,
            )
            zero = cfg.to_money(0)
            tx = await _insert_tx(conn, "deposit", None, account_id, None, acc["display_name"],
                                  amount, zero, zero, "", acc["state"])
            return {
                "kind": "deposit", "ref": tx["ref"], "created_at": tx["created_at"], "state": acc["state"],
                "amount": amount, "fee": zero, "tax": zero, "total": amount, "narration": "",
                "sender": None, "receiver": _party(acc, acc["balance"] + amount),
            }
