"""
nin_card.py

Everything NIN-card specific: the card's data, document numbers, date of birth, scheduling after
!immigrate, and generate_nin_card(). Drawing itself is done by document_renderer.py using the
coordinates in document_config.py.

How a card is produced
  1. !immigrate issues the NIN, then calls schedule_nin_card(): one row is stored in nin_cards with
     a permanent document number, the card's data, and a "due at" time (default now + 20 minutes).
  2. cogs/nin_delivery.py checks once a minute for cards that are due, calls generate_nin_card()
     and posts the PNG in that state's parcel-pickup.
  3. generate_nin_card() only READS the stored data, so re-rendering a card always gives the same
     document number and details.

Test without Discord or a database:   python nin_card.py      (writes dummy_nin_card.png)

Pillow and qrcode are needed; the database is only imported inside the functions that use it.
"""

import asyncio
import datetime as dt
import random
import secrets

import document_config as cfg
from document_renderer import render_document

LAGOS_TZ = dt.timezone(dt.timedelta(hours=1))      # WAT, no daylight saving
DOCUMENT_NUMBER_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no I, L, O, 0, 1: hard to misread
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def new_document_number():
    """e.g. NV-7K3M9Q2X. Random, so numbers can't be guessed from each other."""
    return "NV-" + "".join(secrets.choice(DOCUMENT_NUMBER_ALPHABET) for _ in range(8))


def format_date(d):
    """dt.date -> '07 MAR 1998'."""
    return f"{d.day:02d} {MONTHS[d.month - 1]} {d.year}"


def _years_ago(d, years):
    try:
        return d.replace(year=d.year - years)
    except ValueError:                              # 29 Feb
        return d.replace(year=d.year - years, day=28)


def birth_date_from_age(age, today=None, rng=random):
    """
    A date of birth that makes the character exactly `age` years old today (day and month picked at
    random). Called once at registration; the result is stored and never recomputed.
    """
    today = today or dt.datetime.now(LAGOS_TZ).date()
    latest = _years_ago(today, age)                                  # turned `age` today
    earliest = _years_ago(today, age + 1) + dt.timedelta(days=1)     # turns age+1 tomorrow
    return earliest + dt.timedelta(days=rng.randint(0, (latest - earliest).days))


def verification_data(document_number):
    """What the QR code holds: a verification URL if NIN_VERIFY_BASE_URL is set, else the plain number."""
    if cfg.NIN_VERIFY_BASE_URL:
        return f"{cfg.NIN_VERIFY_BASE_URL}/verify/{document_number}"
    return f"NAIJAVERSE-NIN:{document_number}"


def card_values(data):
    """Stored card data -> the text for each field on the card."""
    return {
        "full_name": data["full_name"],
        "nin": data["nin"],
        "date_of_birth": format_date(data["date_of_birth"]),
        "sex": cfg.SEX_LABELS.get(data["sex"], data["sex"] or "—"),
        "nationality": data["nationality"],
        "state_of_origin": data["state_of_origin"],
        "date_of_registration": format_date(data["date_of_registration"]),
        "document_number": data["document_number"],
        "signature": data["full_name"],
    }


async def generate_nin_card(player_data, portrait_bytes=None):
    """
    Render a player's NIN card and return it as PNG bytes (send with discord.File(io.BytesIO(...))).

    player_data     a nin_cards row (or dict) with: full_name, nin, date_of_birth, sex, nationality,
                    state_of_origin, date_of_registration, document_number (and player_id)
    portrait_bytes  the portrait image. None -> the stored portrait, else the default silhouette.
                    (The Discord avatar fallback is added by the caller, which has the member.)

    Uses the existing document number; never creates a new one.
    """
    data = dict(player_data)
    if portrait_bytes is None and data.get("player_id"):
        import database
        portrait_bytes = await database.get_portrait(data["player_id"])
    # drawing takes a moment: keep it off the bot's event loop
    return await asyncio.to_thread(
        render_document, "nin", card_values(data), portrait_bytes, verification_data(data["document_number"]))


async def schedule_nin_card(player, nin, state):
    """
    Called by !immigrate right after the NIN is issued. Stores the card's data with a permanent
    document number and the time it becomes due. Does nothing (returns the existing record) if this
    player already has one, so the document number never changes. Returns the nin_cards row.
    """
    import asyncpg
    import database

    today = dt.datetime.now(LAGOS_TZ).date()
    for _ in range(10):                             # retry in the (very unlikely) event of a number clash
        try:
            return await database.create_nin_card(
                player_id=player["player_id"],
                document_number=new_document_number(),
                full_name=player["character_name"],
                nin=nin,
                date_of_birth=birth_date_from_age(player["age"] or 18, today),
                sex=player["gender"] or "",
                nationality=cfg.NATIONALITY,
                state_of_origin=player["state_of_origin"] or state,
                date_of_registration=today,
                issue_state=state,
                due_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=cfg.NIN_CARD_DELAY_MINUTES),
            )
        except asyncpg.UniqueViolationError:
            continue
    raise RuntimeError("Couldn't find a free document number.")


def sample_card_data():
    """Dummy player for testing the layout (no database needed)."""
    return {
        "player_id": None,
        "full_name": "Eloghene Uyoyo",
        "nin": "NIN0001DL",
        "date_of_birth": dt.date(1998, 3, 7),
        "sex": "Male",
        "nationality": cfg.NATIONALITY,
        "state_of_origin": "Delta",
        "date_of_registration": dt.date(2026, 9, 21),
        "document_number": "NV-7K3M9Q2X",
    }


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "dummy_nin_card.png"
    png = asyncio.run(generate_nin_card(sample_card_data()))
    with open(out, "wb") as f:
        f.write(png)
    print(f"Wrote {out} ({len(png) / 1e6:.1f} MB)")
