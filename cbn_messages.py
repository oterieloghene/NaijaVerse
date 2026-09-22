"""
cbn_messages.py

The vault-channel messages for !print, !cb-with and !load-cash.
"""

import discord

import bank_config as cfg

GOLD, BLUE = 0xF1C40F, 0x3498DB


def print_batch_embed(governor_mention, new_vault_balance):
    lines = [
        f"🖨️ {cfg.money(cfg.PRINT_CHUNK)} printed",
        f"Printed by: {governor_mention}",
        f"New vault balance: {cfg.money(new_vault_balance)}",
    ]
    return discord.Embed(title="Printing batch completed ✅", description="\n".join(lines), colour=GOLD)


def cb_with_embed(governor_mention, result):
    lines = [
        f"Destination: {result['destination_name']}",
        f"Amount: {cfg.money(result['amount'])}",
        f"Moved by: {governor_mention}",
        f"New vault balance: {cfg.money(result['new_vault_balance'])}",
        f"New destination balance: {cfg.money(result['new_destination_balance'])}",
    ]
    return discord.Embed(title="🏦 Vault withdrawal", description="\n".join(lines), colour=BLUE)


def load_cash_embed(state, manager_mention, new_balance):
    lines = [f"State: {state}", f"Loaded by: {manager_mention}", f"New ATM cash balance: {cfg.money(new_balance)}"]
    return discord.Embed(title="💵 ATM cash loaded", description="\n".join(lines), colour=GOLD)


ATM_OUT_OF_CASH = "🏧 This ATM is out of cash."
