"""
permit_card.py

Everything Residence Permit specific, the same way nin_card.py is for the NIN. A permit's card is
drawn from one of three templates (assets/templates/naijaverse_permit_<state>.jpg), chosen by the
player's current_state; the field layout is identical across all three (document_config.py).

How it works
  !permit @player <house type>   (Immigration Officers / admins — see cogs/permit.py)
    - checks the player already holds the role(s) that house type requires (document_config's
      HOUSE_PREREQUISITE_ROLES / GOVERNMENT_RESIDENCES) — !permit never grants those, it only
      verifies them, so refuses with a clear message if they're missing
    - the address is auto-generated: a random house number and street for ordinary house types
      ("No 7, First Pipeline, Low-Cost Housing, Delta"); a fixed official address for the two
      government residences ("Government House, Delta" / "Aso Rock Presidential Villa, Abuja")
    - grants "{State} Resident" (or, for the President/VP, "Abuja Resident") — nothing else; the
      two government residences grant nothing new, since holding them already implies it
    - the player's name, NIN and portrait come from their NIN record, so the documents always agree
    - stores the permit with a permanent permit number (re-running it for the same player keeps
      the number, updates everything else, and resets the 14-day expiry) and schedules the
      physical card for 20 minutes later, same mechanism as the NIN card

Test without Discord or a database:   python permit_card.py      (writes dummy_permit_card.png)
"""

import asyncio
import datetime as dt
import random
import secrets

import document_config as cfg
from document_renderer import render_document
from nin_card import LAGOS_TZ, format_date

PERMIT_NUMBER_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def new_permit_number():
    return "RP-" + "".join(secrets.choice(PERMIT_NUMBER_ALPHABET) for _ in range(8))


def _role_names(member_or_player_roles):
    return {r.casefold() for r in member_or_player_roles}


def check_prerequisite(location_code, state, member_role_names):
    """
    Does this player already hold what `location_code` requires? Returns (ok, missing_description,
    house_label, role_to_grant, address_or_None). address_or_None is the fixed official address for
    a government residence, or None for an ordinary house type (its address is generated separately).
    Raises ValueError for an unrecognised code.
    """
    gov = cfg.GOVERNMENT_RESIDENCES.get(location_code)
    if gov is not None:
        if gov.get("state_only") and state != gov["state_only"]:
            raise ValueError(f"`{location_code}` only applies in {gov['state_only']}.")
        shared = gov["shared_role"]
        holder_role = next((r for r in gov["holders"] if r.format(S=state).casefold() in member_role_names), None)
        if shared.casefold() not in member_role_names or holder_role is None:
            wanted = " or ".join(r.format(S=state) for r in gov["holders"])
            return False, f'"{shared}" and ({wanted})', None, None, None
        label, grant_template = gov["holders"][holder_role]
        role = grant_template.format(S=state) if grant_template else None
        return True, None, label, role, gov["address"].format(S=state)

    entry = cfg.HOUSE_TYPES.get(location_code)
    if entry is None:
        valid = ", ".join(f"`{c}`" for c in cfg.HOUSE_TYPES)
        raise ValueError(f"`{location_code}` isn't a residential location. Choose one of: {valid}")
    category_key, house_label = entry
    required = [r.format(S=state) for r in cfg.HOUSE_PREREQUISITE_ROLES.get(location_code, [])]
    missing = [r for r in required if r.casefold() not in member_role_names]
    if missing:
        return False, " and ".join(f'"{r}"' for r in missing), None, None, None
    role = f"{state} Resident" if location_code in cfg.HOUSE_GRANTS_STATE_RESIDENT else None
    return True, None, house_label, role, None


def generate_address(location_code, state, rng=random):
    """"No <n>, <Street>, <Category>, <State>" for an ordinary house type."""
    category_key, _ = cfg.HOUSE_TYPES[location_code]
    category_label = cfg.HOUSE_CATEGORY_LABELS[category_key]
    street = rng.choice(cfg.HOUSE_STREET_NAMES[category_key])
    house_no = rng.randint(1, 48)
    return f"No {house_no}, {street}, {category_label}, {state}"


def permit_verification_data(permit_number):
    if cfg.NIN_VERIFY_BASE_URL:
        return f"{cfg.NIN_VERIFY_BASE_URL}/verify-permit/{permit_number}"
    return f"NAIJAVERSE-PERMIT:{permit_number}"


def card_values(data):
    return {
        "state_banner": cfg.STATE_BANNER.get(data["issue_state"], data["issue_state"]),
        "full_name": data["full_name"],
        "permit_number": data["permit_number"],
        "nin": data["nin"] or "—",
        "nationality": cfg.NATIONALITY,
        "residence_type": data["residence_type"],
        "address": data["address"],
        "lga": data["lga"],
        "date_of_issuance": format_date(data["date_of_issuance"]),
        "expiry_date": format_date(data["expiry_date"]),
        "issuing_authority": "NaijaVerse Immigration Service",
        "signature": data["full_name"],
    }


async def generate_permit_card(player_data, portrait_bytes=None):
    """Render a permit and return PNG bytes. Uses the template that matches the permit's issue_state."""
    data = dict(player_data)
    if portrait_bytes is None and data.get("player_id"):
        import database
        portrait_bytes = await database.get_portrait(data["player_id"])
    doc_type = cfg.STATE_PERMIT_DOC.get(data["issue_state"], "permit_delta")
    return await asyncio.to_thread(
        render_document, doc_type, card_values(data), portrait_bytes, permit_verification_data(data["permit_number"]))


async def register_permit(target_player, location_code, state, member_role_names):
    """
    Called by !permit. Checks the prerequisite role(s) for `location_code`, looks up the target's
    NIN record (name/NIN come from there), builds the address, and stores/updates the permit —
    keeping the permit number if they already have one. Returns (row, house_label, role_to_grant).
    Raises ValueError for an unknown house type, a missing prerequisite, or no NIN on file.
    """
    import database

    ok, missing, house_label, role, fixed_address = check_prerequisite(
        location_code, state, _role_names(member_role_names))
    if not ok:
        raise ValueError(f"This player needs {missing} before a permit can be issued for `{location_code}`.")

    nin_record = await database.get_nin_card_by_player(target_player["player_id"])
    if nin_record is None:
        raise ValueError("This player needs to be immigrated (have a NIN) before registering a residence.")

    address = fixed_address if fixed_address else generate_address(location_code, state)
    today = dt.datetime.now(LAGOS_TZ).date()

    row = await database.upsert_residence_permit(
        player_id=target_player["player_id"],
        permit_number=new_permit_number(),          # only used if this player has no permit yet
        full_name=nin_record["full_name"],
        nin=nin_record["nin"],
        residence_type=house_label,
        address=address,
        lga=f"{state} Central",                      # placeholder until real LGA data exists per area
        issue_state=state,
        date_of_issuance=today,
        expiry_date=today + dt.timedelta(days=cfg.PERMIT_EXPIRY_DAYS),
        due_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=cfg.PERMIT_CARD_DELAY_MINUTES),
    )
    return row, house_label, role


def sample_card_data():
    today = dt.date(2026, 9, 22)
    return {
        "player_id": None,
        "full_name": "Eloghene Uyoyo",
        "permit_number": "RP-7K3M9Q2X",
        "nin": "NIN0001DL",
        "residence_type": "Two Bedroom Flat",
        "address": "No 7, First Pipeline, Mid-Class Residential, Delta",
        "lga": "Delta Central",
        "issue_state": "Delta",
        "date_of_issuance": today,
        "expiry_date": today + dt.timedelta(days=cfg.PERMIT_EXPIRY_DAYS),
    }


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "dummy_permit_card.png"
    png = asyncio.run(generate_permit_card(sample_card_data()))
    with open(out, "wb") as f:
        f.write(png)
    print(f"Wrote {out} ({len(png) / 1e6:.1f} MB)")
