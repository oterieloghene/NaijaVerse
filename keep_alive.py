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

Usage (in your bot.py, before bot.run(...)):

    from keep_alive import keep_alive
    keep_alive()
"""

import os
from threading import Thread
from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive."


def _run():
    port = int(os.environ.get("PORT", 8080))  # Render provides PORT
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = Thread(target=_run)
    t.daemon = True
    t.start()
