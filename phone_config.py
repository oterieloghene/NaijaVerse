"""
phone_config.py

Every setting for the in-game phone: the image, where the battery is drawn, how the battery
drains, and the list of apps. Change numbers here; nothing else needs touching.

Files this needs (relative to the project folder, next to bot.py):
    assets/templates/naijaverse_phone_template.png   <- the cropped phone picture
    assets/fonts/WorkSans-Bold.ttf                   <- already there for the NIN card

All boxes are (left, top, right, bottom) in pixels of the CROPPED phone picture (856 x 1310).
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "assets" / "templates" / "naijaverse_phone_template.png"
FONT_PATH = BASE_DIR / "assets" / "fonts" / "WorkSans-Bold.ttf"

# ---------------------------------------------------------------------------
# Battery drawn on the picture (top right of the status bar)
# ---------------------------------------------------------------------------

# The original battery icon is painted over this area, then a fresh one is drawn.
BATTERY_ERASE_BOX = (676, 80, 754, 124)
# The new battery body (the little cap on the right is added automatically).
BATTERY_BODY_BOX = (680, 88, 738, 116)
LOW_BATTERY_PERCENT = 20          # at or below this the fill turns red

# ---------------------------------------------------------------------------
# Battery drain
# ---------------------------------------------------------------------------

IDLE_DRAIN_PERCENT = 1.0          # lose this much ...
IDLE_DRAIN_EVERY_MINUTES = 15     # ... every this many minutes, even when the phone isn't used

# Extra drain for each SUCCESSFUL action on the phone (percent points). Failed actions cost nothing.
ACTION_COST = {
    "balance": 1.0,               # checking the bank balance
    "transfer": 2.0,              # a completed transfer
}

# ---------------------------------------------------------------------------
# Apps (same order as the icons on the picture: 3 per row, 4 rows)
# key, button label, button emoji
# ---------------------------------------------------------------------------

APPS = [
    ("bus", "Bus", "🚌"), ("taxi", "Taxi", "🚕"), ("dispatch", "Dispatch", "📦"),
    ("mechanic", "Mechanic", "🔧"), ("bank", "Bank", "🏦"), ("emergency", "Emergency", "🚨"),
    ("flight", "Flight", "✈️"), ("hotel", "Hotel", "🏨"), ("job", "Job", "💼"),
    ("smart", "Smart", "📲"), ("shopping", "Shopping", "🛍️"), ("contacts", "Contacts", "👤"),
]
APPS_PER_ROW = 3

# The public "takes out their phone" message disappears after this many seconds if nobody opens it.
PICKUP_TIMEOUT_SECONDS = 90
# How long the private phone screens keep working before the buttons stop responding.
PHONE_TIMEOUT_SECONDS = 900
