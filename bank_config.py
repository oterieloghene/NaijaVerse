"""
bank_config.py

Every setting for banking: the tiers and their limits, the transfer fee and tax, which channels
each command works in, and who may change a tier. Plus the small pure helpers (amount parsing,
fee maths, account numbers) so they can be tested without Discord or a database.

To change a limit or a rate, change it here. Nothing else needs touching.
"""

import datetime as dt
import re
import secrets
from decimal import ROUND_HALF_UP, Decimal

CURRENCY = "₦"
WAT = dt.timezone(dt.timedelta(hours=1))     # Nigeria time, no daylight saving. "Today" resets at midnight WAT.

# ---------------------------------------------------------------------------
# Tiers. None = no limit. Order matters: it is the order tiers are listed in.
# ---------------------------------------------------------------------------

TIERS = {
    "bronze": {
        "name": "Bronze", "emoji": "🥉", "colour": 0xCD7F32,
        "max_balance": Decimal("3000000"),
        "daily_transfer": Decimal("500000"),
        "daily_withdrawal": Decimal("200000"),
        "daily_deposit": Decimal("200000"),
    },
    "gold": {
        "name": "Gold", "emoji": "🏅", "colour": 0xFFD700,
        "max_balance": Decimal("20000000"),
        "daily_transfer": Decimal("1000000"),
        "daily_withdrawal": Decimal("500000"),
        "daily_deposit": Decimal("500000"),
    },
    "diamond": {
        "name": "Diamond", "emoji": "💎", "colour": 0x5DADE2,
        "max_balance": Decimal("50000000"),
        "daily_transfer": Decimal("5000000"),
        "daily_withdrawal": Decimal("1000000"),
        "daily_deposit": Decimal("1000000"),
    },
    "platinum": {
        "name": "Platinum", "emoji": "💠", "colour": 0xB0C4DE,
        "max_balance": None,
        "daily_transfer": Decimal("20000000"),
        "daily_withdrawal": Decimal("5000000"),
        "daily_deposit": Decimal("1000000"),
    },
}
DEFAULT_TIER = "bronze"

# ---------------------------------------------------------------------------
# Fee and tax on transfers (charged ON TOP of the amount).
# The fee goes to the state's bank_revenue account, the tax to the state's treasury account.
# ---------------------------------------------------------------------------

TRANSFER_FEE_RATE = Decimal("0.01")      # 1%
TRANSFER_TAX_RATE = Decimal("0.005")     # 0.5%

# ---------------------------------------------------------------------------
# Where each command works (channel codes, i.e. the channel name without its emoji decoration)
# ---------------------------------------------------------------------------

ATM_CHANNEL = "atm"                      # !bal  !transfer  !with
OPEN_ACCOUNT_CHANNEL = "banking-office"  # !open-account
DEPOSIT_CHANNEL = "deposit"              # !dep
LOG_CHANNEL = "transaction-log"          # every transaction is posted here (in the account's state)

# Who may use !upgrade-tier. Matched against the END of each role name, case-insensitive, so
# "Accountant" also matches "Delta Accountant". Add or remove names when you settle the hierarchy.
UPGRADE_ROLE_NAMES = ("Accountant", "Bank Manager", "Executive Director")

# Role sets for the new commands. Same end-of-name, case-insensitive matching as above.
# "State Bank Manager" is the plain "Bank Manager" role — no separate role needed.
BANK_MANAGER_ROLE_NAMES = ("Bank Manager", "Executive Director")   # !view-balances, !bank-debit, !bank-credit
AUDIT_ROLE_NAMES = ("Auditor", "Bank Manager")                     # !statement, !send-statement
CBN_ROLE_NAMES = ("CBN Governor", "CBN Deputy")                    # !print, !cb-with
CBN_GOVERNOR_ROLE_NAMES = ("CBN Governor",)                        # !disburse (Governor only, not Deputy)

# A single row, not tied to any state: account_type = 'national_treasury'.
NATIONAL_TREASURY_STATE = "National"

# --- CBN vault -------------------------------------------------------------------------------
VAULT_CHANNEL = "vault"                  # !print  !cb-with  (and where the batch/withdrawal posts go)
PRINT_CHUNK = Decimal("100000000")       # ₦100,000,000 per batch
PRINT_SECONDS_PER_CHUNK = 60             # 1 minute per ₦100,000,000

MAX_AMOUNT = Decimal("1000000000000")    # sanity ceiling for a single typed amount


def member_has_role(member, names):
    """True if `member` is an admin, or has a role whose name ends with one of `names` (case-insensitive)."""
    if member.guild_permissions.administrator:
        return True
    keys = [n.casefold() for n in names]
    return any(role.name.casefold().endswith(k) for role in member.roles for k in keys)

ACCOUNT_NUMBER_LENGTH = 10
TX_REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no I, L, O, 0, 1: hard to misread


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def to_money(value):
    """Anything number-like -> Decimal rounded to 2 decimal places (kobo)."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fee_and_tax(amount):
    """(fee, tax) for a transfer of `amount`."""
    amount = to_money(amount)
    return to_money(amount * TRANSFER_FEE_RATE), to_money(amount * TRANSFER_TAX_RATE)


_AMOUNT_RE = re.compile(r"^(\d+(?:\.\d{1,2})?)([km]?)$")


def parse_amount(text):
    """
    '100000', '100,000', '₦100,000', '100k', '1.5m' -> Decimal.
    Raises ValueError with a player-friendly message if it isn't a sensible positive amount.
    """
    cleaned = str(text).strip().lower().replace(",", "").replace(CURRENCY.lower(), "").replace(" ", "")
    match = _AMOUNT_RE.match(cleaned)
    if not match:
        raise ValueError("That isn't a valid amount. Use numbers only, e.g. `50000`, `50,000` or `50k`.")
    number, suffix = Decimal(match.group(1)), match.group(2)
    if suffix == "k":
        number *= 1000
    elif suffix == "m":
        number *= 1000000
    amount = to_money(number)
    if amount <= 0:
        raise ValueError("The amount must be more than zero.")
    if amount > MAX_AMOUNT:
        raise ValueError("That amount is too large.")
    return amount


def new_account_number():
    """A random 10-digit account number, e.g. '0123456789' (leading zeros allowed)."""
    return "".join(secrets.choice("0123456789") for _ in range(ACCOUNT_NUMBER_LENGTH))


def new_tx_ref():
    """e.g. 'TX-8K3M2Q'."""
    return "TX-" + "".join(secrets.choice(TX_REF_ALPHABET) for _ in range(6))


def find_tier(text):
    """'Gold', 'gold', ' DIAMOND ' -> 'gold' / 'diamond'; None if it isn't a tier."""
    key = str(text).strip().casefold()
    return key if key in TIERS else None


def today_wat():
    return dt.datetime.now(WAT).date()


def money(value):
    """Decimal -> '₦100,000' (whole naira) or '₦1,250.50' (when there are kobo)."""
    value = to_money(value)
    if value == value.to_integral_value():
        return f"{CURRENCY}{int(value):,}"
    return f"{CURRENCY}{value:,.2f}"
