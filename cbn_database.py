"""
cbn_database.py

The CBN vault: the raw cash pool !print adds to, and !cb-with pays out of into either the
National Treasury or a state's bank account (bank_revenue). One row, id = 1.

This intentionally reuses bank_database's private _system_account() / _national_treasury()
inside the SAME transaction as the vault debit, so a cb-with is one atomic move — the vault
never drops without the destination account being credited, or vice versa.
"""

import bank_config as cfg
import bank_database as bank
import database
from bank_database import BankError


async def init_tables():
    async with database.get_pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cbn_vault (
                id       INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                balance  NUMERIC(20, 2) NOT NULL DEFAULT 0 CHECK (balance >= 0)
            );
            """
        )


async def get_vault_balance():
    async with database.get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM cbn_vault WHERE id = 1;")
        return row["balance"] if row else cfg.to_money(0)


async def add_to_vault(amount):
    """+= amount to the vault (used once per completed ₦100,000,000 batch by !print). Returns the new balance."""
    amount = cfg.to_money(amount)
    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT balance FROM cbn_vault WHERE id = 1 FOR UPDATE;")
            new_balance = (row["balance"] if row else cfg.to_money(0)) + amount
            await conn.execute(
                "INSERT INTO cbn_vault (id, balance) VALUES (1, $1) ON CONFLICT (id) DO UPDATE SET balance = $1;",
                new_balance,
            )
            return new_balance


async def cb_withdraw(destination, amount):
    """
    Move `amount` out of the vault into an account.
    destination: cfg.NATIONAL_TREASURY_STATE for the National Treasury, or a state name for
    that state's bank account (bank_revenue).

    Returns a dict with both:
      - the vault-specific fields cogs/cbn.py uses for the vault-channel embed
        ("destination_name", "new_vault_balance", "new_destination_balance")
      - the standard announce_transaction() shape (kind, ref, created_at, state, amount, fee,
        tax, narration, sender, receiver) so it also posts to the destination's transaction log
        and (if the destination were a personal account, which it never is here) DMs.
    """
    amount = cfg.to_money(amount)
    if amount <= 0:
        raise BankError("The amount must be more than zero.")

    async with database.get_pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT balance FROM cbn_vault WHERE id = 1 FOR UPDATE;")
            vault_balance = row["balance"] if row else cfg.to_money(0)
            if vault_balance < amount:
                raise BankError(f"The vault only has {cfg.money(vault_balance)}.")

            if destination == cfg.NATIONAL_TREASURY_STATE:
                account = await bank._national_treasury(conn)
            else:
                account = await bank._system_account(conn, destination, bank._REVENUE)

            new_destination_balance = account["balance"] + amount
            await conn.execute("UPDATE bank_accounts SET balance = balance + $1 WHERE account_id = $2;",
                               amount, account["account_id"])
            new_vault_balance = vault_balance - amount
            await conn.execute(
                "INSERT INTO cbn_vault (id, balance) VALUES (1, $1) ON CONFLICT (id) DO UPDATE SET balance = $1;",
                new_vault_balance,
            )
            zero = cfg.to_money(0)
            tx = await bank._insert_tx(conn, "cb-with", None, account["account_id"], "CBN Vault",
                                       account["name"], amount, zero, zero, "", destination)
            return {
                "destination_name": account["name"],
                "amount": amount,
                "new_vault_balance": new_vault_balance,
                "new_destination_balance": new_destination_balance,
                # announce_transaction() shape
                "kind": "cb-with", "ref": tx["ref"], "created_at": tx["created_at"], "state": destination,
                "fee": zero, "tax": zero, "total": amount, "narration": "",
                "sender": None, "receiver": bank._party(account, new_destination_balance),
                "from_label": "CBN Vault", "to_label": account["name"],
            }
