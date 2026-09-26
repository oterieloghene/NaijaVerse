"""
passport_card.py

International Passport: !passport (Chief Immigration Officer, in #chief-marshal-office only — see
cogs/passport.py) generates the card immediately, no delay and no role granted. Only the left
(data) page is filled; the right Travel Records page is never touched, per the brief.

Name/date of birth/sex/state of origin and the portrait all come from the player's NIN record, so
every document agrees. Surname/given names are split from the character's full name (last word =
surname, the rest = given names — the convention used throughout NaijaVerse's naming).

Test without Discord or a database:   python passport_card.py      (writes dummy_passport.png)
"""

import asyncio
import datetime as dt
import secrets

import document_config as cfg
from document_renderer import render_document
from nin_card import LAGOS_TZ, format_date

PASSPORT_NUMBER_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def new_passport_number():
    return "NP-" + "".join(secrets.choice(PASSPORT_NUMBER_ALPHABET) for _ in range(8))


def split_name(full_name):
    """'Eloghene Uyoyo' -> ('Uyoyo', 'Eloghene'). A one-word name has no given names."""
    parts = full_name.split()
    if len(parts) < 2:
        return full_name, ""
    return parts[-1], " ".join(parts[:-1])


def card_values(data):
    surname, given_names = split_name(data["full_name"])
    return {
        "surname": surname,
        "given_names": given_names,
        "nationality": cfg.NATIONALITY,
        "date_of_birth": format_date(data["date_of_birth"]),
        "sex": cfg.SEX_LABELS.get(data["sex"], data["sex"] or "—"),
        "state_of_birth": data["state_of_origin"],
        "date_of_issue": format_date(data["date_of_issue"]),
        "passport_no": data["passport_no"],
        "date_of_expiry": format_date(data["date_of_expiry"]),
        "authority": cfg.PASSPORT_ISSUING_AUTHORITY,
        "signature": data["full_name"],
    }


async def generate_passport_card(player_data, portrait_bytes=None):
    """Render a passport data page and return PNG bytes."""
    data = dict(player_data)
    if portrait_bytes is None and data.get("player_id"):
        import database
        portrait_bytes = await database.get_portrait(data["player_id"])
    return await asyncio.to_thread(render_document, "passport", card_values(data), portrait_bytes, None)


async def issue_passport(target_player):
    """
    Called by !passport. Pulls name/DOB/sex/state of origin from the target's NIN record (raises
    ValueError if they don't have one yet). Reissuing keeps the same passport number and extends
    a fresh expiry date from today; everything else refreshes too. Returns the stored row.
    """
    import database

    nin_record = await database.get_nin_card_by_player(target_player["player_id"])
    if nin_record is None:
        raise ValueError("This player needs to be immigrated (have a NIN) before a passport can be issued.")

    today = dt.datetime.now(LAGOS_TZ).date()
    return await database.upsert_passport(
        player_id=target_player["player_id"],
        passport_no=new_passport_number(),   # only used if this player has no passport yet
        full_name=nin_record["full_name"],
        date_of_birth=nin_record["date_of_birth"],
        sex=nin_record["sex"],
        state_of_origin=nin_record["state_of_origin"],
        date_of_issue=today,
        date_of_expiry=today + dt.timedelta(days=cfg.PASSPORT_VALIDITY_DAYS),
    )


def sample_card_data():
    today = dt.date(2026, 9, 26)
    return {
        "player_id": None,
        "full_name": "Eloghene Uyoyo",
        "date_of_birth": dt.date(1998, 3, 7),
        "sex": "Male",
        "state_of_origin": "Delta",
        "date_of_issue": today,
        "passport_no": "NP-7K3M9Q2X",
        "date_of_expiry": today + dt.timedelta(days=cfg.PASSPORT_VALIDITY_DAYS),
    }


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "dummy_passport.png"
    png = asyncio.run(generate_passport_card(sample_card_data()))
    with open(out, "wb") as f:
        f.write(png)
    print(f"Wrote {out} ({len(png) / 1e6:.1f} MB)")
