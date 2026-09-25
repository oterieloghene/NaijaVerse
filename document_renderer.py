"""
document_renderer.py

Generic engine that turns a document template + values into a finished PNG. It knows nothing about
NINs: everything document-specific (template file, field boxes, fonts, colours) comes from
document_config.DOCUMENTS, so a Resident Permit or Driver's Licence only needs a new config entry.

Only needs Pillow (and `qrcode`, imported when a QR code is drawn). The master template is only ever
read, never written.

    png_bytes = render_document("nin", values, portrait_bytes, qr_data)
"""

import io
import logging
import random
import statistics

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFont, ImageOps

import document_config as cfg

log = logging.getLogger(__name__)

CORNER_RADIUS_PAD = 2        # measured radius is nudged up a hair so no grey canvas is left in the corners
MAX_SOURCE_PIXELS = 40_000_000


# ---------------------------------------------------------------------------
# Template: find the card, crop it, measure its corners
# ---------------------------------------------------------------------------

def _corner_average(img, x, y, size=8):
    patch = img.crop((x, y, x + size, y + size)).resize((1, 1), Image.BOX)
    return patch.getpixel((0, 0))


def _background_colour(img):
    """Colour of the empty canvas around the card: the median of the four corner patches."""
    w, h = img.size
    corners = [_corner_average(img, 0, 0), _corner_average(img, w - 8, 0),
               _corner_average(img, 0, h - 8), _corner_average(img, w - 8, h - 8)]
    return tuple(int(statistics.median(c[i] for c in corners)) for i in range(3))


def _detect_card_box(img, threshold=60, min_coverage=0.30):
    """
    Bounding box (left, top, right, bottom) of the card on its canvas, or None if it can't be
    told apart from the canvas. A column/row counts as 'card' when at least 30% of its pixels
    differ clearly from the canvas colour, which ignores the four rounded corners (they hold canvas)
    and any speckle, while still including the full straight edges.
    """
    rgb = img.convert("RGB")
    w, h = rgb.size
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, _background_colour(rgb)))
    r, g, b = diff.split()
    total = ImageChops.add(ImageChops.add(r, g), b)
    mask = total.point(lambda v: 255 if v > threshold else 0)

    cols = mask.resize((w, 1), Image.BOX).tobytes()
    rows = mask.resize((1, h), Image.BOX).tobytes()
    limit = 255 * min_coverage
    xs = [i for i, v in enumerate(cols) if v > limit]
    ys = [i for i, v in enumerate(rows) if v > limit]
    if not xs or not ys:
        return None

    box = (xs[0], ys[0], xs[-1] + 1, ys[-1] + 1)
    if (box[2] - box[0]) < 0.5 * w or (box[3] - box[1]) < 0.5 * h:
        return None
    return box


def crop_card_template(img, manual_crop=None):
    """
    Cut the empty canvas away from around the card without touching the card itself.
    Returns (cropped_image, box_used). Uses manual_crop (left, top, right, bottom in template
    pixels) when given; otherwise detects the card, and if detection is unreliable keeps the whole
    image (and says so in the log) rather than risk cutting into the card.
    """
    w, h = img.size
    if manual_crop:
        left, top, right, bottom = manual_crop
        box = (max(0, left), max(0, top), min(w, right), min(h, bottom))
    else:
        box = _detect_card_box(img)
        if box is None:
            log.warning("Couldn't detect the card area automatically; using the whole template. "
                        "Set manual_crop in document_config.DOCUMENTS to fix this.")
            box = (0, 0, w, h)
    return img.crop(box), box


def _detect_corner_radius(card, default=70):
    """
    Radius of the card's rounded corners, measured on the cropped card: walk diagonally in from
    each corner until the pixel stops looking like the canvas. On a circular corner of radius r
    that happens at t = r * (1 - 1/sqrt(2)) along the diagonal.
    """
    w, h = card.size
    reach = min(w, h) // 3
    hits = []
    for cx, cy, dx, dy in ((0, 0, 1, 1), (w - 1, 0, -1, 1), (0, h - 1, 1, -1), (w - 1, h - 1, -1, -1)):
        bg = card.getpixel((cx, cy))
        for t in range(reach):
            px = card.getpixel((cx + dx * t, cy + dy * t))
            if sum(abs(a - b) for a, b in zip(px, bg)) > 60:
                hits.append(t)
                break
    if len(hits) < 4:
        return default
    radius = statistics.median(hits) / (1 - 2 ** -0.5)
    if not (8 <= radius <= 0.25 * min(w, h)):
        return default
    return int(round(radius)) + CORNER_RADIUS_PAD


def _rounded_mask(size, radius, scale=4):
    """Anti-aliased rounded-rectangle mask (255 inside)."""
    w, h = size
    big = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * scale - 1, h * scale - 1), radius=radius * scale, fill=255)
    return big.resize((w, h), Image.LANCZOS)


_template_cache = {}


def _template_path(spec):
    for name in spec["template_files"]:
        path = cfg.TEMPLATES_DIR / name
        if path.exists():
            return path
    raise FileNotFoundError(
        f"No template found. Put it at {cfg.TEMPLATES_DIR / spec['template_files'][0]} "
        f"(accepted names: {', '.join(spec['template_files'])})")


def _load_template(doc_type):
    """(cropped card image, corner radius). Cached; reloaded if the file changes. Never written back."""
    spec = cfg.DOCUMENTS[doc_type]
    path = _template_path(spec)
    stamp = (str(path), path.stat().st_mtime_ns)
    cached = _template_cache.get(doc_type)
    if cached and cached[0] == stamp:
        return cached[1], cached[2]

    with Image.open(path) as src:
        card, box = crop_card_template(src.convert("RGB"), spec.get("manual_crop"))
    radius = spec.get("corner_radius")
    if radius is None:
        radius = _detect_corner_radius(card)
    log.info("Template %s: card box %s, size %s, corner radius %s", path.name, box, card.size, radius)
    _template_cache[doc_type] = (stamp, card, radius)
    return card, radius


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

_font_cache = {}


def _font(path, size):
    key = (str(path), size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


def fit_text(text, font_path, box_w, box_h, max_size, min_size, pad_x):
    """
    Largest font size (max_size down to min_size) at which the text fits inside the box, so long
    values shrink instead of overflowing. If it still doesn't fit at min_size, it's cut with "…".
    Returns (font, text_to_draw).
    """
    avail_w = box_w - 2 * pad_x
    avail_h = box_h - 4
    size = max_size
    while size >= min_size:
        font = _font(font_path, size)
        left, top, right, bottom = font.getbbox(text, anchor="ls")
        if (right - left) <= avail_w and (bottom - top) <= avail_h:
            return font, text
        size -= 2

    font = _font(font_path, min_size)
    while len(text) > 1 and font.getlength(text + "…") > avail_w:
        text = text[:-1]
    return font, text.rstrip() + "…"


def _draw_text_field(draw, text, box, style):
    left, top, right, bottom = box
    text = str(text).strip()
    if style.get("upper"):
        text = text.upper()
    if not text:
        return
    font, text = fit_text(text, cfg.FONTS[style["font"]], right - left, bottom - top,
                          style["max_size"], style["min_size"], style.get("pad_x", 20))

    cy = (top + bottom) / 2
    if style.get("vcenter", "caps") == "ink":
        l, t, r, b = font.getbbox(text, anchor="ls")
        baseline = cy - (t + b) / 2
    else:
        cap_top = font.getbbox("H", anchor="ls")[1]     # negative: distance from baseline up to cap height
        baseline = cy - cap_top / 2

    if style.get("align") == "center":
        draw.text(((left + right) / 2, baseline), text, font=font, fill=style["color"], anchor="ms")
    else:
        draw.text((left + style.get("pad_x", 20), baseline), text, font=font, fill=style["color"], anchor="ls")


# ---------------------------------------------------------------------------
# Portrait
# ---------------------------------------------------------------------------

def prepare_stored_portrait(data):
    """
    Validate and shrink an uploaded image for storage (JPEG, longest side <= 1000px, upright).
    Raises ValueError if the bytes aren't a usable image.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_SOURCE_PIXELS:
                raise ValueError("That image is too large.")
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                flat = Image.new("RGB", img.size, "white")
                flat.paste(img, mask=img.split()[-1])
                img = flat
            else:
                img = img.convert("RGB")
            img.thumbnail((1000, 1000), Image.LANCZOS)
            out = io.BytesIO()
            img.save(out, "JPEG", quality=90)
            return out.getvalue()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("That doesn't look like a usable image.") from exc


def default_portrait(size):
    """Plain silhouette used when a player has no portrait and no avatar could be fetched."""
    w, h = size
    s = 2
    img = Image.new("RGB", (w * s, h * s), "#D9E1DC")
    d = ImageDraw.Draw(img)
    fg = "#A9B7B0"
    d.ellipse((w * s * 0.33, h * s * 0.20, w * s * 0.67, h * s * 0.20 + w * s * 0.34), fill=fg)
    d.ellipse((w * s * 0.12, h * s * 0.62, w * s * 0.88, h * s * 1.25), fill=fg)
    return img.resize((w, h), Image.LANCZOS)


def _portrait_image(data, size, centering):
    if data:
        try:
            with Image.open(io.BytesIO(data)) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                # cover-crop to the box: scales without stretching, cuts the overflow
                return ImageOps.fit(img, size, Image.LANCZOS, centering=centering)
        except Exception:
            log.warning("Portrait couldn't be read; using the default portrait.")
    return default_portrait(size)


# ---------------------------------------------------------------------------
# QR code
# ---------------------------------------------------------------------------

def make_date_stamp_image(asset_path, date_text, size, date_color, angle=-8,
                          date_pos=(0.40, 0.724), date_size_frac=0.052):
    """
    Take the pre-made circular stamp artwork at `asset_path` (transparent PNG), stamp `date_text`
    onto its blank date line, size it to fit `size` (keeping it circular — never stretched), rotate
    it a few degrees like a hand-stamped mark, and return an RGBA image sized to fit `size` without
    spilling outside its box. `date_pos` is the date line's position as a fraction of the artwork's
    own width/height (left edge of the text, vertically centred on the line); tune it here if the
    stamp artwork changes. `date_size_frac` is the date's font size as a fraction of the stamp's
    diameter.
    """
    with Image.open(asset_path) as src:
        art = src.convert("RGBA")

    diameter = min(size)   # keep the artwork circular — sized to whichever dimension is tighter
    art = art.resize((diameter, diameter), Image.LANCZOS)

    if date_text:
        d = ImageDraw.Draw(art)
        font_size = max(10, int(diameter * date_size_frac))
        font = _font(cfg.FONTS["bold"], font_size)
        x, y = date_pos[0] * diameter, date_pos[1] * diameter
        d.text((x, y), date_text.upper(), font=font, fill=ImageColor.getrgb(date_color), anchor="lm")

    rotated = art.rotate(angle, expand=True, resample=Image.BICUBIC)
    rotated.thumbnail(size, Image.LANCZOS)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.alpha_composite(rotated, ((size[0] - rotated.width) // 2, (size[1] - rotated.height) // 2))
    return out


def make_qr_image(data, size, dark="#000000", light="#FFFFFF", quiet_modules=2):
    """
    Square QR image, exactly `size` px, with whole-pixel modules (crisp, no blurring) centred in
    a light quiet zone. Error correction M.
    """
    import qrcode

    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=1, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    unit = size // (n + 2 * quiet_modules)
    if unit < 2:
        raise ValueError("QR code is too dense for its box; use a shorter verification URL.")
    offset = (size - unit * n) // 2
    img = Image.new("RGB", (size, size), light)
    d = ImageDraw.Draw(img)
    for y, row in enumerate(matrix):
        for x, filled in enumerate(row):
            if filled:
                x0, y0 = offset + x * unit, offset + y * unit
                d.rectangle((x0, y0, x0 + unit - 1, y0 + unit - 1), fill=dark)
    return img


# ---------------------------------------------------------------------------
# Ink stamp (e.g. an issuing office's stamp, with a date)
# ---------------------------------------------------------------------------

def make_stamp_image(lines, size, color="#8B1E1E", angle=-6, border=5):
    """
    A bold, worn "ink stamp" oval with the given lines of bold uppercase text, rotated a few
    degrees like a hand-stamped mark. `lines` is a list of strings, e.g.
    ["DELTA IMMIGRATION OFFICE", "ISSUED 22 SEP 2026"]. Returns an RGBA image sized to fit `size`
    once rotated (so it can be pasted straight onto the card without spilling outside its box).
    """
    scale = 4
    w, h = size
    # draw oversized and un-rotated first, so the border and text stay crisp after rotation/shrink
    draw_w, draw_h = w * scale, h * scale
    stamp = Image.new("RGBA", (draw_w, draw_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(stamp)
    ink = ImageColor.getrgb(color)
    pad = 5 * scale
    outer = (pad, pad, draw_w - pad, draw_h - pad)
    d.ellipse(outer, outline=ink, width=border * scale)
    inset = pad + 9 * scale
    d.ellipse((inset, inset, draw_w - inset, draw_h - inset), outline=ink, width=max(2, border * scale // 2))

    # text sits inside the ellipse: each line's available width is that ellipse's chord at its
    # own vertical offset from centre, so lines further from the middle get less width, not more
    rx, ry = (outer[2] - outer[0]) / 2, (outer[3] - outer[1]) / 2
    cx, cy0 = draw_w / 2, draw_h / 2
    text_h = ry * 1.5   # block of text spans this tall, centred vertically
    line_h = text_h / max(len(lines), 1)
    size_px = max(10, int(line_h * 0.68))
    font = _font(cfg.FONTS["bold"], size_px)
    safety = 0.74   # keep clear of the inner ring, not just the outer edge
    line_positions = []
    for i, line in enumerate(lines):
        cy = cy0 - text_h / 2 + line_h * (i + 0.5)
        dy = cy - cy0
        chord = 2 * rx * (1 - (dy / ry) ** 2) ** 0.5 * safety if abs(dy) < ry else 0
        line_positions.append((cy, chord))
    # one font size for every line, sized to the tightest line so nothing pokes past the ring
    tightest = min(chord for _, chord in line_positions)
    while size_px > 8 and max(font.getlength(l.upper()) for l in lines) > tightest:
        size_px -= 2
        font = _font(cfg.FONTS["bold"], size_px)
    for text, (cy, _) in zip(lines, line_positions):
        d.text((cx, cy), text.upper(), font=font, fill=ink, anchor="mm")
    # a small decorative rule between two lines, like a classic office stamp
    if len(lines) > 1:
        rule_y = cy0 - text_h / 2 + line_h
        half = rx * (1 - ((rule_y - cy0) / ry) ** 2) ** 0.5 * 0.4
        d.line((cx - half, rule_y, cx + half, rule_y), fill=ink, width=max(1, scale))

    # a bold worn look: knock out a noisy speckle so it doesn't look computer-perfect
    speckle = Image.new("L", stamp.size, 0)
    ds = ImageDraw.Draw(speckle)
    rng = random.Random(sum(map(ord, "".join(lines))))
    for _ in range(int(draw_w * draw_h * 0.001)):
        x, y = rng.randint(0, draw_w - 1), rng.randint(0, draw_h - 1)
        r = rng.randint(1, scale)
        ds.ellipse((x, y, x + r, y + r), fill=rng.randint(70, 170))
    alpha = stamp.split()[3]
    alpha = ImageChops.subtract(alpha, speckle)
    stamp.putalpha(alpha)

    rotated = stamp.rotate(angle, expand=True, resample=Image.BICUBIC)
    rotated.thumbnail((w, h), Image.LANCZOS)
    # centre the (now smaller, due to rotation padding) stamp inside the exact requested size
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.alpha_composite(rotated, ((w - rotated.width) // 2, (h - rotated.height) // 2))
    return out




def render_document(doc_type, values, portrait_bytes=None, qr_data=None):
    """
    Draw one document and return it as PNG bytes (transparent rounded corners).
      values          {field name: text} for the text fields in the document's config
      portrait_bytes  image bytes for the portrait field; None -> default portrait
      qr_data         text for the QR field; None -> QR field left blank
    """
    spec = cfg.DOCUMENTS[doc_type]
    card, radius = _load_template(doc_type)
    canvas = card.copy()                       # the cached template is never drawn on
    draw = ImageDraw.Draw(canvas)

    for name, box in spec["fields"].items():
        kind = spec["image_fields"].get(name)
        left, top, right, bottom = box
        size = (right - left, bottom - top)

        if kind == "portrait":
            photo = _portrait_image(portrait_bytes, size, spec.get("portrait_centering", (0.5, 0.3)))
            canvas.paste(photo, (left, top), _rounded_mask(size, spec.get("portrait_corner_radius", 0)))
        elif kind == "qr":
            if qr_data:
                side = min(size)
                qr = make_qr_image(qr_data, side, dark=spec.get("qr_dark", "#000000"))
                canvas.paste(qr, (left + (size[0] - side) // 2, top + (size[1] - side) // 2))
        elif kind == "stamp":
            lines = values.get(name)
            if lines:
                stamp_style = spec["text_styles"].get(name, {})
                stamp = make_stamp_image(lines, size, color=stamp_style.get("color", "#8B1E1E"),
                                         angle=stamp_style.get("angle", -9))
                canvas.paste(stamp, (left, top), stamp)
        elif kind == "date_stamp":
            stamp_style = spec["text_styles"].get(name, {})
            asset_path = spec["stamp_assets"][name]
            date_text = values.get(name, "")
            stamp = make_date_stamp_image(
                asset_path, date_text, size,
                date_color=stamp_style.get("color", "#000000"),
                angle=stamp_style.get("angle", -8),
                date_pos=stamp_style.get("date_pos", (0.40, 0.724)),
                date_size_frac=stamp_style.get("date_size_frac", 0.052),
            )
            canvas.paste(stamp, (left, top), stamp)
        elif name in values and values[name] not in (None, ""):
            _draw_text_field(draw, values[name], box, spec["text_styles"][name])

    out = canvas.convert("RGBA")
    out.putalpha(_rounded_mask(out.size, radius))
    buf = io.BytesIO()
    out.save(buf, "PNG", compress_level=6)
    return buf.getvalue()
