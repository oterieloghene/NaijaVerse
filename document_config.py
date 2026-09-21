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
}
