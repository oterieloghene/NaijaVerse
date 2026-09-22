"""
bank_messages.py

Every message the bank sends, in one place: the transaction-log entry, the DM alerts, the
account-opened / tier-changed messages, the balance card and the phone's transfer screens.
To change how a message looks, edit it here.

Also holds announce_transaction(), which posts a finished transaction to the state's
transaction-log and DMs the people involved. The ATM commands and the phone both use it.
"""

import discord

import bank_config as cfg
from location_permissions import state_location_channels

SEP = "━━━━━━━━━━━━━━━━━━"

RED, GREEN, BLUE, GREY = 0xE74C3C, 0x2ECC71, 0x3498DB, 0x95A5A6


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def bank_name(state):
    return f"{state.upper()} BANK PLC"


def tier_label(tier_key):
    tier = cfg.TIERS[tier_key]
    return f"{tier['emoji']} {tier['name']}"


def _pct(rate):
    return format((rate * 100).normalize(), "f")


def _log_time(created_at):
    return created_at.astimezone(cfg.WAT).strftime("%d %b %Y · %H:%M WAT")


def _dm_time(created_at):
    return created_at.astimezone(cfg.WAT).strftime("%d %b %Y, %H:%M")


def _embed(title, lines, colour):
    return discord.Embed(title=title, description="\n".join(lines), colour=colour)


def _limits_lines(tier_key):
    tier = cfg.TIERS[tier_key]
    cap = "Unlimited" if tier["max_balance"] is None else cfg.money(tier["max_balance"])
    return [
        f"Max Balance: {cap}",
        f"Daily Transfer: {cfg.money(tier['daily_transfer'])}",
        f"Daily Withdrawal: {cfg.money(tier['daily_withdrawal'])}",
        f"Daily Deposit: {cfg.money(tier['daily_deposit'])}",
    ]


# ---------------------------------------------------------------------------
# Transaction log (posted in the transaction-log of the account's state)
# ---------------------------------------------------------------------------

def log_embed(result):
    kind = result["kind"]
    if kind == "transfer":
        title = f"🔄 TRANSFER · Ref {result['ref']}"
        lines = [
            SEP,
            f"From: {result['sender']['display_name']}",
            f"To: {result['receiver']['display_name']}",
            f"Amount: {cfg.money(result['amount'])}",
            f"Fee {_pct(cfg.TRANSFER_FEE_RATE)}%: {cfg.money(result['fee'])}",
            f"Tax {_pct(cfg.TRANSFER_TAX_RATE)}%: {cfg.money(result['tax'])}",
            f"Narration: {result['narration'] or '—'}",
        ]
    elif kind == "withdrawal":
        title = f"🏧 WITHDRAWAL · Ref {result['ref']}"
        lines = [SEP, f"Account: {result['sender']['display_name']}", f"Amount: {cfg.money(result['amount'])}"]
    else:
        title = f"💵 DEPOSIT · Ref {result['ref']}"
        lines = [SEP, f"Account: {result['receiver']['display_name']}", f"Amount: {cfg.money(result['amount'])}"]
    lines += [SEP, _log_time(result["created_at"])]
    return _embed(title, lines, BLUE)


# ---------------------------------------------------------------------------
# DM alerts
# ---------------------------------------------------------------------------

def debit_alert(result):
    """For the person whose account was charged (sender / withdrawer)."""
    kind = result["kind"]
    lines = [SEP, f"Amount: {cfg.money(result['amount'])}"]
    if kind == "transfer":
        lines += [
            f"Fee: {cfg.money(result['fee'])}",
            f"Tax: {cfg.money(result['tax'])}",
            f"Total: {cfg.money(result['total'])}",
            f"To: {result['receiver']['display_name']}",
        ]
    else:
        lines.append("Type: Cash withdrawal")
    lines += [f"Balance: {cfg.money(result['sender']['balance'])}", SEP,
              f"Ref {result['ref']} · {_dm_time(result['created_at'])}"]
    return _embed("🔴 DEBIT ALERT", lines, RED)


def credit_alert(result):
    """For the person whose account was credited (receiver / depositor)."""
    lines = [SEP, f"Amount: {cfg.money(result['amount'])}"]
    if result["kind"] == "transfer":
        lines.append(f"From: {result['sender']['display_name']}")
    else:
        lines.append("Type: Cash deposit")
    lines += [f"Balance: {cfg.money(result['receiver']['balance'])}", SEP,
              f"Ref {result['ref']} · {_dm_time(result['created_at'])}"]
    return _embed("🟢 CREDIT ALERT", lines, GREEN)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def account_opened_dm(account, staff_name):
    """DM to the new customer. `account` is a bank_accounts row (with display_name)."""
    state = account["state"]
    lines = [SEP, f"Welcome, {account['display_name']}!", "Account Opened.",
             f"Account No.: `{account['account_number']}`", f"Tier: {tier_label(account['tier'])}", SEP,
             *_limits_lines(account["tier"]), SEP, f"Opened by {staff_name} · {state}"]
    return _embed(f"🏦 {bank_name(state)}", lines, cfg.TIERS[account["tier"]]["colour"])


def account_opened_channel(customer_name, tier_key, staff_mention):
    """The confirmation in the banking-office (shows no account number or balance)."""
    lines = [f"Customer: {customer_name}", f"Tier: {tier_label(tier_key)} account.", f"Opened by {staff_mention}"]
    return _embed("✅ Account created", lines, GREEN)


def tier_changed_dm(account, old_tier, staff_name):
    new_tier = account["tier"]
    up = list(cfg.TIERS).index(new_tier) > list(cfg.TIERS).index(old_tier)
    title = f"🏦 {bank_name(account['state'])}"
    lines = [SEP, f"Account {'Upgraded' if up else 'Changed'}", f"Account No.: `{account['account_number']}`",
             f"Tier: {tier_label(old_tier)} → {tier_label(new_tier)}", SEP,
             *_limits_lines(new_tier), SEP, f"Updated by {staff_name} · {account['state']}"]
    return _embed(title, lines, cfg.TIERS[new_tier]["colour"])


def tier_changed_channel(customer_name, old_tier, new_tier, staff_mention):
    up = list(cfg.TIERS).index(new_tier) > list(cfg.TIERS).index(old_tier)
    lines = [f"Customer: {customer_name}", f"Tier: {tier_label(old_tier)} → {tier_label(new_tier)}",
             f"Updated by {staff_mention}"]
    return _embed("✅ Account upgraded" if up else "✅ Account tier changed", lines, GREEN)


def balance_embed(account, usage):
    """Balance card for !bal and the phone. `usage` is bank_database.get_usage()."""
    tier = cfg.TIERS[account["tier"]]

    def left(limit_key, used_key):
        return cfg.money(max(tier[limit_key] - usage[used_key], cfg.to_money(0)))

    lines = [SEP, f"Account No.: `{account['account_number']}`", f"Tier: {tier_label(account['tier'])}", SEP,
             f"Balance: {cfg.money(account['balance'])}", SEP, "Left today",
             f"Transfer: {left('daily_transfer', 'transferred')}",
             f"Withdrawal: {left('daily_withdrawal', 'withdrawn')}",
             f"Deposit: {left('daily_deposit', 'deposited')}"]
    return _embed(f"🏦 {bank_name(account['state'])}", lines, cfg.TIERS[account["tier"]]["colour"])


# ---------------------------------------------------------------------------
# Phone transfer screens
# ---------------------------------------------------------------------------

def transfer_confirm_embed(preview, receiver_number):
    lines = [
        SEP,
        f"To: {preview['receiver']['display_name']}",
        f"Account No.: `{receiver_number}`",
        f"Amount: {cfg.money(preview['amount'])}",
        f"Fee {_pct(cfg.TRANSFER_FEE_RATE)}%: {cfg.money(preview['fee'])}",
        f"Tax {_pct(cfg.TRANSFER_TAX_RATE)}%: {cfg.money(preview['tax'])}",
        f"Total: {cfg.money(preview['total'])}",
        f"Narration: {preview['narration'] or '—'}",
        SEP,
        "Check the details, then confirm.",
    ]
    return _embed("📤 Confirm transfer", lines, BLUE)


def transfer_success_embed(result):
    lines = [
        SEP,
        f"To: {result['receiver']['display_name']}",
        f"Amount: {cfg.money(result['amount'])}",
        f"Fee: {cfg.money(result['fee'])}",
        f"Tax: {cfg.money(result['tax'])}",
        f"Total: {cfg.money(result['total'])}",
        f"Balance: {cfg.money(result['sender']['balance'])}",
        SEP,
        f"Ref {result['ref']} · {_dm_time(result['created_at'])}",
    ]
    return _embed("✅ Transfer successful", lines, GREEN)


def error_embed(message):
    return _embed("⚠️ Couldn't complete that", [message], RED)


# ---------------------------------------------------------------------------
# Org account receipts (posted to the account's own receipt channel, set at !create-org-account)
# ---------------------------------------------------------------------------

_RECEIPT_LINE = "▬" * 24


def org_receipt_embed(result):
    """The receipt dropped in an org account's receipt channel for every payment INTO it."""
    receiver = result["receiver"]
    sender = result["sender"]
    lines = [
        _RECEIPT_LINE,
        f"Sender: {sender['display_name']}",
        f"Account no: `{sender['account_number']}`",
        f"Amount: {cfg.money(result['amount'])}",
        f"Narration: {result['narration'] or '—'}",
        _RECEIPT_LINE,
        f"Ref: {result['ref']} · {result['created_at'].astimezone(cfg.WAT).strftime('%d %b %Y, %H:%M WAT')}",
    ]
    return _embed(f"🧾 {receiver['display_name']}", lines, GREEN)


# ---------------------------------------------------------------------------
# !statement / !send-statement
# ---------------------------------------------------------------------------

def statement_embed(account, transactions):
    """The account's last N transactions (bank_database.recent_transactions rows)."""
    lines = [SEP, f"Account No.: `{account['account_number']}`", f"Balance: {cfg.money(account['balance'])}", SEP]
    if not transactions:
        lines.append("No transactions yet.")
    for tx in transactions:
        direction = "OUT" if tx["from_account"] == account["account_id"] else "IN"
        other = tx["to_name"] if direction == "OUT" else tx["from_name"]
        arrow = "🔴" if direction == "OUT" else "🟢"
        when = tx["created_at"].astimezone(cfg.WAT).strftime("%d %b, %H:%M")
        lines.append(f"{arrow} {tx['kind'].title()} · {cfg.money(tx['amount'])} "
                     f"{'to' if direction == 'OUT' else 'from'} {other or '—'} · {when} · Ref {tx['ref']}")
    return _embed(f"📜 Statement — {account['display_name']}", lines, BLUE)


# ---------------------------------------------------------------------------
# !view-balances
# ---------------------------------------------------------------------------

def _balance_lines(accounts, limit=20):
    if not accounts:
        return ["None."]
    lines = [f"{a['display_name']} (`{a['account_number']}`): {cfg.money(a['balance'])}" for a in accounts[:limit]]
    if len(accounts) > limit:
        lines.append(f"…and {len(accounts) - limit} more.")
    return lines


def state_balances_embed(state, data):
    lines = [f"**Customers ({len(data['customers'])})**", *_balance_lines(data["customers"]), "",
             f"**Organisations ({len(data['orgs'])})**", *_balance_lines(data["orgs"]), "",
             "**State bank account**",
             cfg.money(data["state_bank"]["balance"]) if data["state_bank"] else cfg.money(0), "",
             "**State treasury**",
             cfg.money(data["treasury"]["balance"]) if data["treasury"] else cfg.money(0)]
    return _embed(f"📊 {state} — Balances", lines, BLUE)


# ---------------------------------------------------------------------------
# Sending a finished transaction out: transaction-log + DMs
# ---------------------------------------------------------------------------

async def _dm(bot, discord_id, embed):
    if not discord_id:
        return
    try:
        user = bot.get_user(discord_id) or await bot.fetch_user(discord_id)
        await user.send(embed=embed)
    except discord.HTTPException:
        pass                                    # DMs closed: the transaction-log still has it


async def announce_transaction(bot, guild, result):
    """
    Post the transaction in the transaction-log of the account's state, then DM the people
    involved. Never raises: the money has already moved, so a Discord hiccup must not look like
    a failed transaction.
    """
    try:
        channel = state_location_channels(guild, result["state"]).get(cfg.LOG_CHANNEL)
        if channel is None:
            print(f"[bank] No '{cfg.LOG_CHANNEL}' channel found for {result['state']}; "
                  f"{result['ref']} was not logged.")
        else:
            await channel.send(embed=log_embed(result))
    except Exception as exc:
        print(f"[bank] couldn't post {result['ref']} to the log: {exc!r}")

    try:
        if result["sender"]:
            await _dm(bot, result["sender"]["discord_id"], debit_alert(result))
        if result["receiver"]:
            await _dm(bot, result["receiver"]["discord_id"], credit_alert(result))
    except Exception as exc:
        print(f"[bank] couldn't send alerts for {result['ref']}: {exc!r}")

    receiver = result.get("receiver")
    if receiver and receiver.get("account_type") == "org" and receiver.get("receipt_channel_id"):
        try:
            channel = guild.get_channel(receiver["receipt_channel_id"]) \
                or await bot.fetch_channel(receiver["receipt_channel_id"])
            await channel.send(embed=org_receipt_embed(result))
        except Exception as exc:
            print(f"[bank] couldn't post receipt for {result['ref']}: {exc!r}")
