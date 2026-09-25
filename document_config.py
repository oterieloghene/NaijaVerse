"""
document_config.py

Every setting for generated documents (NIN card today; Resident Permit, Driver's Licence,
Student ID, ... later) lives here. To add a document: copy the NIN entry in DOCUMENTS, point it
at its own template, and give it its own field coordinates.

Where things go (relative to the project folder, next to bot.py):
    assets/templates/naijaverse_nin_template.png   <- your NIN template (.jpg / .jpeg also accepted)
    assets/fonts/WorkSans-Bold.ttf                 <- field text
    assets/fonts/NothingYouCouldDo-Regular.ttf     <- signature

Environment variables (all optional):
    NIN_VERIFY_BASE_URL     public URL of the bot's web server, e.g. https://yourapp.onrender.com
                            -> QR codes point to <base>/verify/<document number>
                            (unset: the QR just holds the document number as text)
    NIN_CARD_DELAY_MINUTES  minutes between !immigrate and the card reaching parcel-pickup (default 20)

COORDINATES
All boxes are (left, top, right, bottom) in pixels of the CROPPED card, i.e. after the empty grey
canvas around the card has been cut away (the template is 2400x1792, the card 2160x1480, so
cropped x = template x - 120 and cropped y = template y - 156). The numbers below were measured
from your template: they are the blank white areas inside each field. Nudge any box by a few
pixels here and run `python nin_card.py` to see the result in dummy_nin_card.png.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
TEMPLATES_DIR = ASSETS_DIR / "templates"

FONTS = {
    "bold": FONTS_DIR / "WorkSans-Bold.ttf",
    "signature": FONTS_DIR / "NothingYouCouldDo-Regular.ttf",
    "display": FONTS_DIR / "Gloock-Regular.ttf",   # bold serif for prominent headings (state banner)
}

NIN_CARD_DELAY_MINUTES = int(os.environ.get("NIN_CARD_DELAY_MINUTES", "20"))
NIN_VERIFY_BASE_URL = os.environ.get("NIN_VERIFY_BASE_URL", "").strip().rstrip("/")

NATIONALITY = "Nigerian"
SEX_LABELS = {"Male": "MALE", "Female": "FEMALE"}     # gender stored in the players table -> card text
DELIVERY_CHANNEL = "parcel-pickup"                    # location code of the channel cards are posted in

# ---------------------------------------------------------------------------
# NIN card
# ---------------------------------------------------------------------------

NIN_CARD_FIELDS = {
    "portrait":             (127, 455, 657, 1121),
    "full_name":            (726, 465, 2060, 531),
    "nin":                  (726, 586, 2060, 650),
    "date_of_birth":        (726, 706, 1378, 770),
    "sex":                  (1431, 706, 2060, 770),
    "nationality":          (726, 825, 1615, 890),
    "state_of_origin":      (726, 945, 1615, 1010),
    "date_of_registration": (726, 1065, 1615, 1129),
    "signature":            (127, 1215, 1605, 1401),
    "qr_code":              (1689, 890, 2050, 1251),
    "document_number":      (1679, 1339, 2059, 1403),
}

INK = "#12261B"          # dark green-black for field text
SIGNATURE_INK = "#1B2A5E"  # blue-black "pen"

# Text look per field. Text shrinks from max_size down to min_size to fit its box, and is cut
# with "…" only if it still doesn't fit at min_size. align: "left" | "center".
# vcenter: "caps" centres capital letters in the box (best for uppercase data);
#          "ink" centres the actual drawn text (best for handwriting).
NIN_CARD_TEXT = {
    "full_name":            dict(font="bold", max_size=44, min_size=20, color=INK, align="left", pad_x=24, upper=True),
    "nin":                  dict(font="bold", max_size=42, min_size=20, color=INK, align="left", pad_x=24, upper=True),
    "date_of_birth":        dict(font="bold", max_size=40, min_size=20, color=INK, align="left", pad_x=24, upper=True),
    "sex":                  dict(font="bold", max_size=40, min_size=20, color=INK, align="left", pad_x=24, upper=True),
    "nationality":          dict(font="bold", max_size=40, min_size=20, color=INK, align="left", pad_x=24, upper=True),
    "state_of_origin":      dict(font="bold", max_size=40, min_size=20, color=INK, align="left", pad_x=24, upper=True),
    "date_of_registration": dict(font="bold", max_size=40, min_size=20, color=INK, align="left", pad_x=24, upper=True),
    "document_number":      dict(font="bold", max_size=34, min_size=16, color=INK, align="center", pad_x=14, upper=True),
    "signature":            dict(font="signature", max_size=130, min_size=40, color=SIGNATURE_INK, align="left",
                                 pad_x=48, upper=False, vcenter="ink"),
}

# ---------------------------------------------------------------------------
# Registry: one entry per document type
# ---------------------------------------------------------------------------

PERMIT_CARD_DELAY_MINUTES = int(os.environ.get("PERMIT_CARD_DELAY_MINUTES", "20"))
PERMIT_EXPIRY_DAYS = int(os.environ.get("PERMIT_EXPIRY_DAYS", "14"))

# Residential location code -> (category key, card label). Governor/President tier use a
# per-holder label instead (see GOVERNMENT_RESIDENCES below), so they aren't listed here.
HOUSE_TYPES = {
    "line-houses":         ("low_cost_housing",       "Line Houses"),
    "bed-sitter":          ("low_cost_housing",       "Bed-Sitter"),
    "mini-flat":           ("mid_class_residential",  "Mini Flat"),
    "two-bedroom-flat":    ("mid_class_residential",  "Two Bedroom Flat"),
    "three-bedroom-flat":  ("mid_class_residential",  "Three Bedroom Flat"),
    "private-estate":      ("high_class_residential", "Private Estate"),
    "luxury-duplex":       ("high_class_residential", "Luxury Duplex"),
    "penthouse":           ("high_class_residential", "Penthouse"),
    "governor-penthouse":  ("high_class_residential", None),   # label depends on who holds it
    "president-villa":     ("high_class_residential", None),
}

# Role a player must ALREADY hold before Immigration will issue a permit for that house type.
# This is a prerequisite check only — it doesn't grant the role, and it has no effect on the
# existing channel-access rules in locations.py / location_permissions.py.
# "{S}" is replaced with the player's current state (e.g. "Delta").
HOUSE_PREREQUISITE_ROLES = {
    "line-houses":        ["Line Houses Resident"],
    "bed-sitter":         ["Bed Sitter Resident"],
    "mini-flat":          ["Miniflat Resident"],
    "two-bedroom-flat":   ["Two Bedroom Flat Resident"],
    "three-bedroom-flat": ["Three Bedroom Flat Resident"],
    "private-estate":     ["Private Estate Resident"],
    "luxury-duplex":      ["Luxury Duplex Resident"],
    "penthouse":          ["Penthouse Resident"],
    # governor-penthouse / president-villa have their own two-role check — see GOVERNMENT_RESIDENCES.
}

# Role !permit grants on success, for every code in HOUSE_TYPES except governor-penthouse and
# president-villa (those grant nothing new — see GOVERNMENT_RESIDENCES).
HOUSE_GRANTS_STATE_RESIDENT = {
    "line-houses", "bed-sitter", "mini-flat", "two-bedroom-flat", "three-bedroom-flat",
    "private-estate", "luxury-duplex", "penthouse",
}

# governor-penthouse / president-villa: two roles are checked together (the literal "State
# Resident" or "Federal Resident" role, plus one of two holder-specific roles), and the card label
# and the granted role depend on WHICH of the two holder roles the player has.
# Each entry: shared_role, {holder_role (with "{S}" for the player's state): (card_label, role_granted_or_None)}
GOVERNMENT_RESIDENCES = {
    "governor-penthouse": {
        "shared_role": "State Resident",
        "holders": {
            "{S} Governor":         ("Governor Penthouse", "{S} Resident"),
            "{S} Deputy Governor":  ("Deputy Governor Residence", "{S} Resident"),
        },
        "address": "Government House, {S}",
    },
    "president-villa": {
        "shared_role": "Federal Resident",
        "holders": {
            "President":       ("President Villa", "Abuja Resident"),
            "Vice President":  ("Vice President Residence", "Abuja Resident"),
        },
        "address": "Aso Rock Presidential Villa, Abuja",
        "state_only": "Abuja",
    },
}

# Category key -> label printed after the address (matches locations.py's category display names).
HOUSE_CATEGORY_LABELS = {
    "low_cost_housing": "Low-Cost Housing",
    "mid_class_residential": "Mid-Class Residential",
    "high_class_residential": "High-Class Residential",
}
# Category key -> street/estate names used to build the address, e.g. "No 3, First Pipeline".
# Add as many as you like; a random one is picked per registration. (Not used by the two
# government residences above — they use a fixed official address instead.)
HOUSE_STREET_NAMES = {
    "low_cost_housing": ["First Pipeline", "Second Pipeline", "Third Pipeline", "Unity Close", "Peace Avenue"],
    "mid_class_residential": ["Freedom Way", "Garden Estate Road", "Palm Grove Street", "Harmony Crescent"],
    "high_class_residential": ["Ocean View Drive", "Victoria Crescent", "Royal Palm Avenue", "Emerald Hills Road"],
}

# Text printed in each state's permit banner and used for the LGA field placeholder.
STATE_BANNER = {
    "Delta": "DELTA STATE",
    "Lagos": "LAGOS STATE",
    "Abuja": "FEDERAL CAPITAL TERRITORY",
}
# Which template a state's permit uses (keys into DOCUMENTS below).
STATE_PERMIT_DOC = {"Delta": "permit_delta", "Lagos": "permit_lagos", "Abuja": "permit_abuja"}

# All three templates share one layout, just a different colour skin, so one set of coordinates
# and one set of text styles covers all of them. Measured by pixel-scanning the cropped card
# (1685x1143) — see manual_crop below — not by eye, so these should already be accurate; nudge
# and re-run `python permit_card.py` if anything still looks off.
PERMIT_CARD_FIELDS = {
    "portrait":             (102, 402, 450, 853),
    "state_banner":         (544, 83, 1592, 170),
    "full_name":            (544, 432, 1591, 480),
    "permit_number":        (544, 528, 873, 575),
    "nin":                  (925, 528, 1231, 572),
    "nationality":          (1283, 528, 1587, 569),
    "residence_type":       (542, 617, 1591, 666),
    "address":              (542, 710, 1591, 762),
    "lga":                  (537, 806, 874, 863),
    "date_of_issuance":     (921, 809, 1233, 855),
    "expiry_date":          (1283, 810, 1586, 854),
    "issuing_authority":    (70, 955, 545, 1028),
    "signature":            (735, 955, 1175, 1028),
    "qr_code":              (1439, 944, 1591, 1094),
}

PERMIT_INK = "#17233B"
PERMIT_CARD_TEXT = {
    "state_banner":      dict(font="display", max_size=68, min_size=30, color="#8B1E1E", align="center", pad_x=10, upper=True, vcenter="caps"),
    "full_name":         dict(font="bold", max_size=32, min_size=14, color=PERMIT_INK, align="left", pad_x=16, upper=True),
    "permit_number":     dict(font="bold", max_size=26, min_size=12, color=PERMIT_INK, align="left", pad_x=14, upper=True),
    "nin":               dict(font="bold", max_size=26, min_size=12, color=PERMIT_INK, align="left", pad_x=14, upper=True),
    "nationality":       dict(font="bold", max_size=26, min_size=12, color=PERMIT_INK, align="left", pad_x=14, upper=True),
    "residence_type":    dict(font="bold", max_size=28, min_size=13, color=PERMIT_INK, align="left", pad_x=16, upper=True),
    "address":           dict(font="bold", max_size=26, min_size=12, color=PERMIT_INK, align="left", pad_x=16, upper=True),
    "lga":               dict(font="bold", max_size=26, min_size=12, color=PERMIT_INK, align="left", pad_x=14, upper=True),
    "date_of_issuance":  dict(font="bold", max_size=24, min_size=12, color=PERMIT_INK, align="left", pad_x=14, upper=True),
    "expiry_date":       dict(font="bold", max_size=24, min_size=12, color=PERMIT_INK, align="left", pad_x=14, upper=True),
    "issuing_authority": dict(color="#8B1E1E", angle=-9),   # drawn as a stamp, not text — see image_fields below
    "signature":         dict(font="signature", max_size=56, min_size=20, color="#1B2A5E", align="left", pad_x=10, upper=False, vcenter="ink"),
}


def _permit_entry(template_file):
    return {
        "template_files": [template_file],
        # Detection doesn't work on this mockup's soft gradient background, so this is fixed:
        # measured directly from the template with a cool-vs-warm colour split, not eyeballed.
        "manual_crop": (359, 320, 2044, 1463),
        "corner_radius": None,
        "fields": PERMIT_CARD_FIELDS,
        "text_styles": PERMIT_CARD_TEXT,
        "image_fields": {"portrait": "portrait", "qr_code": "qr", "issuing_authority": "stamp"},
        "portrait_corner_radius": 10,
        "portrait_centering": (0.5, 0.30),
        "qr_dark": PERMIT_INK,
    }


DOCUMENTS = {
    "nin": {
        # first file that exists wins
        "template_files": ["naijaverse_nin_template.png", "naijaverse_nin_template.jpg",
                           "naijaverse_nin_template.jpeg"],
        # Set to (left, top, right, bottom) in TEMPLATE pixels to skip auto-detection of the card
        # area, e.g. (120, 156, 2280, 1636). None = detect automatically.
        "manual_crop": None,
        # Corner radius of the card in pixels. None = measured from the template.
        "corner_radius": None,
        "fields": NIN_CARD_FIELDS,
        "text_styles": NIN_CARD_TEXT,
        # which fields are images rather than text
        "image_fields": {"portrait": "portrait", "qr_code": "qr"},
        "portrait_corner_radius": 14,
        "portrait_centering": (0.5, 0.30),   # crop focus: 0.30 = biased towards the top, keeps faces
        "qr_dark": "#0E1F16",
    },
    "permit_delta": _permit_entry("naijaverse_permit_delta.jpg"),
    "permit_abuja": _permit_entry("naijaverse_permit_abuja.jpg"),
    "permit_lagos": _permit_entry("naijaverse_permit_lagos.jpg"),
}
