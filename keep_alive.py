"""
keep_alive.py

Render Web Services require the app to bind to a port and free services
spin down after ~15 minutes with no inbound traffic. This runs a tiny
Flask server in a background thread so:

  1. Render sees an open port and treats the service as healthy.
  2. An external uptime pinger (e.g. UptimeRobot, cron-job.org — both
     free) can hit this URL every 5-10 minutes to keep the service awake.

This does NOT keep the service awake by itself — you still need to point
a free external pinger at your Render URL (e.g. https://yourapp.onrender.com/).
Render will not let a service reliably ping itself to stay awake.

It also serves the NIN verification page the card's QR code points to:

    <NIN_VERIFY_BASE_URL>/verify/<document number>

The web server runs in its own thread, so database lookups are handed to the bot's event loop.

Usage (in your bot.py, before bot.run(...)):

    from keep_alive import keep_alive
    keep_alive(bot)
"""

import asyncio
import os
import re
from threading import Thread

from flask import Flask
from markupsafe import escape

app = Flask(__name__)
_bot = None
_DOCUMENT_NUMBER = re.compile(r"^NV-[A-Z0-9]{8}$")


@app.route("/")
def home():
    return "Bot is alive."


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NaijaVerse NIN verification</title>
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f2418;font-family:system-ui,sans-serif;color:#f2ecd8}}
.card{{background:#16341f;border:2px solid #c9a24b;border-radius:16px;padding:28px 32px;max-width:420px;width:88%}}
h1{{margin:0 0 4px;font-size:1.1rem;color:#c9a24b;letter-spacing:.08em}}
.status{{font-size:1.6rem;font-weight:700;margin:6px 0 18px;color:{colour}}}
dt{{font-size:.75rem;letter-spacing:.08em;color:#c9a24b;margin-top:12px}}
dd{{margin:2px 0 0;font-size:1.05rem;font-weight:600}}
</style></head><body><div class="card"><h1>NAIJAVERSE NATIONAL IDENTITY SYSTEM</h1>
<div class="status">{status}</div>{body}</div></body></html>"""


def _fetch_card(document_number):
    import database
    loop = _bot.loop                    # raises if the bot hasn't started yet -> "try again" page
    future = asyncio.run_coroutine_threadsafe(database.get_nin_card_by_document(document_number), loop)
    return future.result(timeout=8)


@app.route("/verify/<document_number>")
def verify(document_number):
    document_number = document_number.strip().upper()
    if not _DOCUMENT_NUMBER.match(document_number):
        return _PAGE.format(colour="#e07a6b", status="NOT FOUND",
                            body="<p>No NIN card matches that document number.</p>"), 404
    try:
        card = _fetch_card(document_number)
    except Exception:
        return _PAGE.format(colour="#e0b45b", status="TRY AGAIN",
                            body="<p>The system is starting up. Please try again in a moment.</p>"), 503
    if card is None:
        return _PAGE.format(colour="#e07a6b", status="NOT FOUND",
                            body="<p>No NIN card matches that document number.</p>"), 404

    from nin_card import format_date
    rows = [("FULL NAME", card["full_name"]), ("NIN", card["nin"]),
            ("STATE OF ORIGIN", card["state_of_origin"]),
            ("DATE OF REGISTRATION", format_date(card["date_of_registration"])),
            ("DOCUMENT NUMBER", card["document_number"])]
    body = "<dl>" + "".join(f"<dt>{label}</dt><dd>{escape(str(value))}</dd>" for label, value in rows) + "</dl>"
    return _PAGE.format(colour="#7fd08f", status="VALID NIN CARD", body=body)


def _run():
    port = int(os.environ.get("PORT", 8080))  # Render provides PORT
    app.run(host="0.0.0.0", port=port)


def keep_alive(bot=None):
    """Start the web server. Pass the bot so /verify can read the database through its event loop."""
    global _bot
    _bot = bot
    t = Thread(target=_run)
    t.daemon = True
    t.start()
