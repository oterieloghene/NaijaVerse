"""
phone_renderer.py

Draws the phone picture with a live battery percentage, the same way document_renderer.py draws
the NIN card: open the template, paint over the old battery, draw the new one.

    png_bytes = render_phone(87)

Only needs Pillow. Test without Discord or a database:   python phone_renderer.py
(writes phone_100.png, phone_87.png, phone_15.png, phone_0.png)
"""

import io

from PIL import Image, ImageChops, ImageDraw, ImageFont

import phone_config as cfg

SCALE = 4                     # the battery is drawn 4x bigger, then shrunk, so its edges are smooth
_template = None


def _load_template():
    global _template
    if _template is None:
        _template = Image.open(cfg.TEMPLATE_PATH).convert("RGB")
    return _template.copy()


def _average(img, box):
    patch = img.crop(box).resize((1, 1), Image.BOX)
    return patch.getpixel((0, 0))


def _erase_old_battery(img):
    """Paint over the old icon with the dark status-bar colour (smooth top-to-bottom blend)."""
    left, top, right, bottom = cfg.BATTERY_ERASE_BOX
    above = _average(img, (left, top - 10, right, top - 4))
    below = _average(img, (left, bottom + 4, right, bottom + 10))
    draw = ImageDraw.Draw(img)
    height = max(bottom - top - 1, 1)
    for i, y in enumerate(range(top, bottom)):
        t = i / height
        colour = tuple(round(above[c] + (below[c] - above[c]) * t) for c in range(3))
        draw.line([(left, y), (right - 1, y)], fill=colour)


def _battery_layer(percent):
    """The new battery as a transparent RGBA picture (body + cap), plus where to paste it."""
    left, top, right, bottom = cfg.BATTERY_BODY_BOX
    cap_w = 4
    pad = 3                                           # room around the icon inside its own picture
    w, h = right - left, bottom - top
    lw, lh = (w + cap_w + pad * 2) * SCALE, (h + pad * 2) * SCALE
    s = SCALE

    body = (pad * s, pad * s, (pad + w) * s, (pad + h) * s)
    radius = 8 * s
    layer = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # empty battery: soft grey body with a thin light outline, and the little cap
    draw.rounded_rectangle(body, radius=radius, fill=(255, 255, 255, 70), outline=(255, 255, 255, 120), width=s)
    cap = ((pad + w + 1) * s, (pad + h * 0.32) * s, (pad + w + cap_w) * s, (pad + h * 0.68) * s)
    draw.rounded_rectangle(cap, radius=2 * s, fill=(255, 255, 255, 120))

    # the charge: a rounded bar inside the body, as wide as the percentage
    inset = 2 * s
    inner = (body[0] + inset, body[1] + inset, body[2] - inset, body[3] - inset)
    fill_w = (inner[2] - inner[0]) * max(0, min(percent, 100)) / 100
    fill_mask = Image.new("L", layer.size, 0)
    if fill_w >= 1:
        fill_colour = (255, 59, 48, 255) if percent <= cfg.LOW_BATTERY_PERCENT else (255, 255, 255, 255)
        fill_box = (inner[0], inner[1], inner[0] + fill_w, inner[3])
        ImageDraw.Draw(fill_mask).rounded_rectangle(fill_box, radius=max(radius - inset, 1), fill=255)
        layer.paste(Image.new("RGBA", layer.size, fill_colour), mask=fill_mask)

    # the number: dark over the charge, white over the empty part, so it is readable at any level
    label = f"{int(percent)}"
    size = 19 * s
    font = ImageFont.truetype(str(cfg.FONT_PATH), size)
    text_mask = Image.new("L", layer.size, 0)
    tdraw = ImageDraw.Draw(text_mask)
    while size > 8 * s and tdraw.textlength(label, font=font) > (w - 6) * s:
        size -= s
        font = ImageFont.truetype(str(cfg.FONT_PATH), size)
    cx, cy = (body[0] + body[2]) / 2, (body[1] + body[3]) / 2
    tdraw.text((cx, cy), label, font=font, fill=255, anchor="mm")

    on_charge = ImageChops.multiply(text_mask, fill_mask)
    on_empty = ImageChops.multiply(text_mask, ImageChops.invert(fill_mask))
    dark = (20, 20, 24, 255) if percent > cfg.LOW_BATTERY_PERCENT else (255, 255, 255, 255)
    layer.paste(Image.new("RGBA", layer.size, dark), mask=on_charge)
    layer.paste(Image.new("RGBA", layer.size, (255, 255, 255, 255)), mask=on_empty)

    small = layer.resize((lw // s, lh // s), Image.LANCZOS)
    return small, (left - pad, top - pad)


def render_phone(battery_percent):
    """The phone picture with the given battery percentage (0-100) drawn on it. Returns PNG bytes."""
    percent = max(0, min(100, int(battery_percent)))
    img = _load_template()
    _erase_old_battery(img)
    layer, position = _battery_layer(percent)
    img = img.convert("RGBA")
    img.alpha_composite(layer, position)
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


if __name__ == "__main__":
    for level in (100, 87, 15, 0):
        with open(f"phone_{level}.png", "wb") as f:
            f.write(render_phone(level))
    print("Wrote phone_100.png, phone_87.png, phone_15.png, phone_0.png")
