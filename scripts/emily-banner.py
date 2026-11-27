#!/usr/bin/env python3
"""
emily-banner.py — the shop's Etsy banner, drawn by Emily's image model.

    python3 scripts/emily-banner.py                     draw it
    python3 scripts/emily-banner.py "more sage green"   ...with a nudge added

Writes agents/emily/shop/banner.png (cropped to Etsy's 4:1) and keeps the
model's uncropped original beside it as banner-raw.png. Open it on a tablet at
the Command Deck's address + /api/emily/shop/banner.png, save the image, and
upload it in Shop Manager > Edit shop > Banner.

WHY THE CROP IS CODE. Etsy's big banner is 4:1 (3360x840 recommended, 1200x300
minimum). The image model's widest shape is 21:9, so it is asked for that and
the middle 4:1 band is kept. A model asked for "4:1" in words returns whatever
it likes; a crop is exact.

WHY NO WORDS AND NO BAGS. Etsy prints the shop name beside the banner, and an
image model spells badly, so text would only add misspellings. And a drawn tote
is not one of the shop's totes: a banner that shows products must show the real
ones. This draws the shop's THEME - vintage loom and botanical - and lets the
listings show the goods.

Standard library only; the PNG work is knockout.py's.
"""

import importlib.util
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
OUT_DIR = ROOT / "agents" / "emily" / "shop"

ETSY_RATIO = 4.0            # width / height of Etsy's big banner
ETSY_MIN = (1200, 300)      # below this Etsy refuses the upload
ASK = "21:9"                # the widest shape the image model offers

# The shop's theme, from its own logo: a hand loom in a laurel wreath, brown
# ink on cream, sage leaves.
PROMPT = (
    "A wide, calm decorative banner for a vintage-themed shop called "
    "VintageLoom Treasures. Vintage botanical engraving style: fern fronds, "
    "laurel sprigs, wildflowers and woven-textile texture, like an antique "
    "herbarium plate crossed with a heritage weaving pattern. Warm cream "
    "paper background with fine sepia-brown line work and muted sage green "
    "leaves, a touch of faded teal. Detail spread evenly across the full "
    "width with a quieter centre, so it still reads when a phone crops the "
    "edges."
)

DIRECTION = (
    "This is a website banner, not a product. Flat illustration, seen "
    "straight on, filling the whole frame edge to edge. NO text, no letters, "
    "no words, no numbers, no logo, no signature, no watermark. No bags, no "
    "clothing, no products, no people, no hands, no mockup, no photograph, "
    "no border, no frame."
)


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def crop_box(w, h, ratio=ETSY_RATIO):
    """(x, y, width, height) of the largest centred band at `ratio`.

    Wider than the ratio keeps full height and trims the sides; taller keeps
    full width and trims top and bottom.
    """
    if w / h >= ratio:
        cw, ch = int(round(h * ratio)), h
    else:
        cw, ch = w, int(round(w / ratio))
    return (w - cw) // 2, (h - ch) // 2, cw, ch


def crop(px, w, box):
    """RGBA pixels of `box` out of a w-wide RGBA image."""
    x0, y0, cw, ch = box
    out = bytearray()
    for y in range(y0, y0 + ch):
        start = (y * w + x0) * 4
        out += px[start:start + cw * 4]
    return out


def main():
    extra = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
    ea = _load("emily_assets", "emily-assets.py")
    ko = _load("knockout", "knockout.py")

    ea.load_credentials()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("no OPENROUTER_API_KEY in agents/emily/state/credentials.env - "
              "the banner needs the image model.", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw, out = OUT_DIR / "banner-raw.png", OUT_DIR / "banner.png"
    prompt = PROMPT + (f"\n\n{extra}" if extra else "")
    print(f"drawing the banner ({ASK}, then cropped to 4:1) ...")
    try:
        n, usage = ea.generate(raw, prompt, key, direction=DIRECTION, aspect=ASK)
    except Exception as exc:
        print(f"the image model failed: {exc}", file=sys.stderr)
        return 1
    cost = ea.cost_of(usage)

    try:
        w, h, px = ko.decode(raw)
    except ko.PngError as exc:
        print(f"the model's image could not be read: {exc}\n"
              f"  The original is at {raw}.", file=sys.stderr)
        return 1
    box = crop_box(w, h)
    ko.encode(out, box[2], box[3], crop(px, w, box))

    print(f"  model returned {w}x{h}; kept the centre {box[2]}x{box[3]}")
    if box[2] < ETSY_MIN[0] or box[3] < ETSY_MIN[1]:
        print(f"  WARNING: Etsy needs at least {ETSY_MIN[0]}x{ETSY_MIN[1]}. "
              f"Run it again, or try another model.")
    print(f"  cost: ${cost:.4f}" if cost is not None else "  cost: not reported")
    print(f"\nwrote {out}\n"
          f"Open it on the iPad at your Command Deck's address followed by\n"
          f"  /api/emily/shop/banner.png\n"
          f"save the image, then Etsy > Shop Manager > Edit shop > Banner.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
