"""
housing_messages.py

Every message the housing system sends, in one place — same pattern as
bank_messages.py. Currently just the tenancy receipt, posted by !assignhouse
to the resident's new house thread and to staff-office.
"""

import discord

SEP = "\u2501" * 18
GOLD = 0xC49A3A


def _time(when):
    return when.strftime("%d %b %Y, %H:%M")


def _date(when):
    return when.strftime("%d %b %Y")


def receipt_embed(resident_name, house_label, state, address, rent_due_at, issued_by, issued_at):
    lines = [
        SEP,
        f"Resident: {resident_name}",
        f"House Type: {house_label}",
        f"State: {state}",
        f"Address: {address}",
        SEP,
        "Rent: Weekly",
        f"Next Due: {_date(rent_due_at)}",
        SEP,
        f"Issued by: {issued_by} · {_time(issued_at)}",
    ]
    return discord.Embed(title="🏠 TENANCY RECEIPT", description="\n".join(lines), colour=GOLD)
