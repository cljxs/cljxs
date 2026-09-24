#!/usr/bin/env python3
"""
emily-assets.py — turns a prompt into a REAL image file for Emily.

Two modes, chosen automatically:

  * OPENROUTER_API_KEY set -> generates real art through the same OpenRouter
    key the rest of the ecosystem already uses. No new provider, no new account.
  * no key -> writes a deterministic geometric placeholder at the right
    dimensions, clearly marked as a placeholder.

It never writes a blank or zero-byte file. A blank deliverable is a failed
build, so the placeholder path exists precisely so a build can still complete
before the external accounts are connected.

Standard library only.
"""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import struct
import sys
import urllib.request
import zlib
from pathlib import Path

def read_env_file(path):
    """{key: value} from a credentials.env, or {} if it cannot be read.

    THE one parser for this format. preflight grew a second one to show which
    model draws the art, and the two disagreed immediately: this accepts
    `KEY = value` with spaces around the equals and that one did not, so a
    setting that worked perfectly would have been reported as absent.
    """
    out = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v:
            out[k] = v
    return out


def load_credentials():
    """credentials.env is a file, not an environment. Nothing was loading it,
    so the key was never visible and every build silently fell back to a
    placeholder. Read it here so the file actually does something."""
    here = Path(__file__).resolve().parent.parent
    for cand in (here / "agents" / "emily" / "state" / "credentials.env",
                 Path(os.environ.get("EMILY_CREDENTIALS", "/nonexistent"))):
        if not cand.is_file():
            continue
        for k, v in read_env_file(cand).items():
            if k not in os.environ:
                os.environ[k] = v


OR_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_IMAGE_MODEL = "google/gemini-2.5-flash-image"


def image_model(override=None):
    """Which model draws the art.

    Read HERE, not at import. It used to be a module constant, and
    load_credentials() runs inside main() - so EMILY_IMAGE_MODEL set in
    credentials.env was read before the file that sets it had been loaded, and
    silently did nothing. A documented knob that is wired to nothing is worse
    than no knob, because it is believed.
    """
    return (override or os.environ.get("EMILY_IMAGE_MODEL") or "").strip() \
        or DEFAULT_IMAGE_MODEL


# ---------------------------------------------------------------- PNG writer

def write_png(path, width, height, rows):
    """rows: iterable of bytes objects, each width*3 long (RGB)."""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    Path(path).write_bytes(png)
    return len(png)


def placeholder(path, prompt, size):
    """A deterministic geometric design derived from the brief. Real pixels,
    never blank, and obviously a placeholder rather than fake finished art."""
    h = hashlib.sha256(prompt.encode()).digest()
    pal = [(h[i] // 2 + 60, h[i + 1] // 2 + 60, h[i + 2] // 2 + 60) for i in (0, 3, 6, 9)]
    bg = (18 + h[12] % 24, 18 + h[13] % 24, 26 + h[14] % 24)
    bands = 5 + h[15] % 5
    rows = []
    for y in range(size):
        row = bytearray()
        band = (y * bands) // size
        for x in range(size):
            cx, cy = x - size / 2, y - size / 2
            r = (cx * cx + cy * cy) ** .5
            ring = int(r / (size / 14)) % 2
            inside = r < size * 0.34
            if inside and ring:
                c = pal[band % len(pal)]
            elif inside:
                c = pal[(band + 2) % len(pal)]
            elif abs(cy) < size * 0.008:
                c = pal[(band + 1) % len(pal)]
            else:
                c = bg
            row += bytes(c)
        rows.append(row)
    return write_png(path, size, size, rows)


# ------------------------------------------------------------- OpenRouter

# Appended to every prompt that draws a print file.
#
# Emily asked for "Maple leaf pocket sticker" and got exactly that: a
# PHOTOGRAPH of a maple leaf sticker lying on a wooden desk next to a ruler,
# which then went to Printify as the file to print. The model was not wrong -
# that is what the words describe. Nobody had told it the output is a print
# file rather than a picture of a product.
#
# It lives in code and not in Emily's instructions because it is the same
# sentence every time and does not need judgement. Her judgement is the idea;
# this is the format, and a format restated by a model every cycle is a
# format that eventually comes back missing a clause.
PRINT_DIRECTION = (
    "Flat 2D vector-style illustration intended as a print file. "
    "Solid plain background in one even colour, filling the frame, seen "
    "straight on. "
    "NOT a photograph and NOT a product mockup: no desk, no table, no wood "
    "grain, no ruler, no hand, no packaging, no printed sticker, no shelf, "
    "no room, no perspective, no drop shadow, no reflection, no watermark, "
    "no border."
)

# ...except on a product that prints its whole surface, where PRINT_DIRECTION
# asks for the wrong picture.
#
# "Solid plain background in one even colour, filling the frame" describes a
# subject sitting on a field, which is right for a sticker or a chest print
# and is exactly what an all-over tote must not be: printed edge to edge, the
# even field becomes most of the bag and the subject a blob near the middle.
#
# The refusals that come after are the same ones, because the failure they
# guard is the same - a model asked for a print file returning a photograph
# of the product.
ALL_OVER_DIRECTION = (
    "Flat 2D vector-style illustration intended as an all-over print file: "
    "the design covers the whole square, edge to edge, with no background "
    "field and no empty margin. A repeating pattern drawn from a few solid "
    "colours, even in density across the frame, seen straight on. "
    "NOT a photograph and NOT a product mockup: no desk, no table, no wood "
    "grain, no ruler, no hand, no packaging, no shelf, no room, no bag, no "
    "tote, no garment, no perspective, no drop shadow, no reflection, no "
    "watermark, no border."
)


def all_over(product):
    """Does this product print its whole surface?

    Asked of emily-printify.py rather than answered here. It reads the
    blueprint title Printify gave the catalogue entry, and it is the same
    call that decides whether the background gets cut out. Two pieces of
    code deciding that a tote is an all-over print would eventually
    disagree, and the prompt is the half nobody would notice had drifted -
    the art would just quietly get worse.

    Imported lazily and inside a try: drawing art must not stop because the
    catalogue is missing. Unknown means "not all-over", which is the safe
    way round - a normal print direction on an AOP product is a worse
    picture, an AOP direction on a sticker is a file with no background to
    cut out and a refused build.
    """
    if not (product or "").strip():
        return False
    try:
        spec = importlib.util.spec_from_file_location(
            "emily_printify_ao", Path(__file__).resolve().parent / "emily-printify.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _key, entry = mod.resolve(mod.read_catalog(), product)
        return bool(entry) and mod.is_all_over(entry)
    except Exception:
        return False


def market_notes(evidence):
    """Design constraints that follow from the numbers, not from taste.

    A model asked to be creative will be creative in whatever direction it
    happened to start in. These are the parts of the brief that are FACTS
    about the market, so they belong in code:

      - 436,862 competing listings is not trivia. It means the design is
        first seen as a 170-pixel thumbnail in a grid of forty, and a
        delicate illustration that reads beautifully at full size is
        invisible there. That is a drawing instruction.
      - a 4.99 median price and a 28.00 median price are different products.
        One is an impulse buy that must read in a second; the other is
        looked at before it is bought.
      - the market's own tags are the only description of its look that is
        not somebody's opinion.
    """
    if not isinstance(evidence, dict):
        return []
    out = []
    supply = evidence.get("supply")
    if isinstance(supply, int) and supply > 0:
        if supply >= 50000:
            out.append(f"This design competes with {supply:,} other listings. "
                       f"It will first be seen as a small thumbnail in a grid, "
                       f"so it must read instantly at that size: bold shapes, "
                       f"high contrast, few elements, no fine detail that "
                       f"disappears when shrunk.")
        else:
            out.append(f"About {supply:,} listings compete with this - a "
                       f"narrow market, so it can afford to be specific and "
                       f"characterful rather than broadly safe.")
    price = evidence.get("typical_price")
    if isinstance(price, (int, float)) and price > 0:
        if price < 12:
            out.append(f"Typical price in this market is {price:.2f}: an "
                       f"impulse buy. One clear idea, understood in a second.")
        else:
            out.append(f"Typical price in this market is {price:.2f}: it will "
                       f"be looked at before it is bought, so it can reward "
                       f"a second look.")
    tags = [t for t in (evidence.get("tags") or []) if isinstance(t, str)]
    if tags:
        out.append("Sellers in this market describe it as: "
                   + ", ".join(tags[:8]) + ". Fit that world without copying "
                   "any individual listing.")
    return out


def prior_designs(build_root, phrase, limit=12):
    """What has already been made for this phrase.

    Emily builds one design at a time with no memory of the last one, and an
    image model handed the same brief twice draws the same picture twice. A
    shop wins on many DISTINCT designs in one coherent style, so the designs
    already made are part of the brief - as things to differ from.
    """
    root = Path(build_root)
    if not root.is_dir():
        return []
    seen = []
    for meta in sorted(root.glob("*/build.json")):
        try:
            d = json.loads(meta.read_text())
        except Exception:
            continue                      # a broken build is not a crash here
        if not isinstance(d, dict):
            continue
        ev = d.get("evidence") or {}
        same = str(ev.get("phrase") or "").strip().lower() == \
            str(phrase or "").strip().lower()
        title = str(d.get("idea") or d.get("title") or "").strip()
        if same and title and title not in seen:
            seen.append(title)
    return seen[-limit:]


def compose(idea, brief="", product="", evidence=None, already=()):
    """THE art prompt. One place.

    It used to be built in two: emily-new-build.py appended 'Flat vector
    illustration for a <product>, clean edges, no text' and emily-assets.py
    appended PRINT_DIRECTION, which says the same thing in different words.
    Two pieces of code deciding one thing always drift, and these two were
    already disagreeing about whether text was allowed.
    """
    parts = [str(idea or "").strip()]
    if brief and brief.strip():
        parts.append(brief.strip())
    if product and product.strip():
        parts.append(f"Artwork for a {product.strip()}.")
    parts += market_notes(evidence)
    if already:
        parts.append("The shop already sells these, so this must be visibly "
                     "different from all of them - a different subject and a "
                     "different composition, in the same style: "
                     + "; ".join(already) + ".")
    return "\n\n".join(p for p in parts if p)


def directed(prompt, product=""):
    """The art direction on the end, once, however the prompt already reads.

    Which direction depends on the product, because an all-over print wants
    the opposite of a plain background.
    """
    return (f"{prompt.strip()}\n\n"
            f"{ALL_OVER_DIRECTION if all_over(product) else PRINT_DIRECTION}")


def generate(path, prompt, key, model=None, product=""):
    """Draw one image. Returns (bytes written, usage).

    `usage.include` asks OpenRouter to price the call and hand the number back
    in the response. An image's cost cannot be worked out from the published
    rate alone - image_output is dollars per output TOKEN, and how many tokens
    an image is depends on the model, the size and the quality. So rather than
    estimate it and be wrong in a direction nobody can check, the run reports
    what it actually cost.
    """
    body = json.dumps({
        "model": model or image_model(),
        "messages": [{"role": "user", "content": directed(prompt, product)}],
        "modalities": ["image", "text"],
        "usage": {"include": True},
    }).encode()
    req = urllib.request.Request(OR_URL, data=body, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cljxs/cljxs",
        "X-Title": "emily-assets",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())

    usage = data.get("usage") or {}
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    for img in (msg.get("images") or []):
        url = ((img.get("image_url") or {}).get("url")) or img.get("url") or ""
        if url.startswith("data:"):
            Path(path).write_bytes(base64.b64decode(url.split(",", 1)[1]))
            return Path(path).stat().st_size, usage
        if url.startswith("http"):
            with urllib.request.urlopen(url, timeout=120) as im:
                Path(path).write_bytes(im.read())
            return Path(path).stat().st_size, usage
    raise RuntimeError("no image returned; response keys: " + ",".join(msg.keys()))


def cost_of(usage):
    """What the call cost in dollars, or None if the provider did not say.

    None is not zero. A zero here would read as "this model is free", which is
    the kind of number that gets repeated.
    """
    for key in ("cost", "total_cost"):
        value = (usage or {}).get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def safe_name(model):
    """A model slug as a filename: openai/gpt-5-image -> openai-gpt-5-image."""
    return "".join(c if c.isalnum() or c in "-." else "-" for c in model).strip("-")


def compare(prompt, models, key, out_dir):
    """Draw one prompt with several models, side by side.

    "Are the ChatGPT ones better" is not answerable in the abstract - it
    depends on the prompt, the garment and the taste of the person selling
    them. So this puts the same brief through each model and writes them into
    a build folder, because the Deck's gallery already renders a folder of
    images with a lightbox. The filename is the model, so what you are looking
    at is never a guess.

    Nothing here is a product: no listing, no price, no Printify. It is a
    folder of pictures to look at, and the Remove button throws it away.
    """
    if not key:
        print(json.dumps({"ok": False, "error": "no OPENROUTER_API_KEY - "
                          "comparing needs real generations"}), file=sys.stderr)
        return 2

    wanted = [m.strip() for m in models.split(",") if m.strip()]
    if not wanted:
        print(json.dumps({"ok": False, "error": "no models given"}), file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "build.json").write_text(json.dumps({
        "status": "comparison",
        "art_mode": "generated",
        "idea": f"Model comparison - {prompt[:60]}",
        "prompt": prompt,
        "models": wanted,
    }, indent=1) + "\n")
    (out_dir / "listing.json").write_text(json.dumps({
        "title": f"Model comparison: {prompt[:48]}",
        "description": "Not a product. One prompt drawn by several models, "
                       "for choosing between them.",
    }, indent=1) + "\n")

    results = []
    for model in wanted:
        path = out_dir / f"{safe_name(model)}.png"
        try:
            n, usage = generate(path, prompt, key, model)
            cost = cost_of(usage)
            results.append({"model": model, "ok": True, "file": path.name,
                            "bytes": n, "cost_usd": cost, "usage": usage})
            shown = f"${cost:.4f}" if cost is not None else "cost not reported"
            print(f"  {model:<38} {n:>9,} bytes  {shown:>18}")
        except Exception as exc:
            # One model refusing or timing out must not cost you the others.
            results.append({"model": model, "ok": False, "error": str(exc)[:200]})
            print(f"  {model:<38} FAILED  {str(exc)[:90]}", file=sys.stderr)

    (out_dir / "comparison.json").write_text(json.dumps(
        {"prompt": prompt, "results": results}, indent=1) + "\n")
    drawn = [r for r in results if r["ok"]]
    priced = [r["cost_usd"] for r in drawn if r["cost_usd"] is not None]
    if priced:
        print(f"\n  this comparison cost ${sum(priced):.4f}"
              + ("" if len(priced) == len(drawn)
                 else f" (only {len(priced)} of {len(drawn)} reported a cost)"))
    print(f"\n{len(drawn)} of {len(wanted)} drew something. Look at them in the "
          f"Command Deck -\nthe filename under each is the model. Throw the "
          f"whole comparison away with Remove.")
    return 0 if drawn else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--placeholder-only", action="store_true")
    ap.add_argument("--model", default="",
                    help="draw with this model instead of the configured one")
    ap.add_argument("--product", default="",
                    help="the catalogue product this art is for. An all-over "
                         "print gets the opposite art direction - edge to "
                         "edge, no background field.")
    ap.add_argument("--compare", default="",
                    help="comma-separated models: draw the SAME prompt with "
                         "each, into a build folder the Deck already shows")
    a = ap.parse_args()

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    load_credentials()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()

    if a.compare:
        return compare(a.prompt, a.compare, key, Path(a.out))

    if key and not a.placeholder_only:
        try:
            model = image_model(a.model)
            n, usage = generate(a.out, a.prompt, key, model, a.product)
            print(json.dumps({"ok": True, "mode": "generated", "model": model,
                              "path": a.out, "bytes": n,
                              "cost_usd": cost_of(usage)}))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "mode": "generate-failed",
                              "error": str(exc)[:200]}), file=sys.stderr)

    n = placeholder(a.out, a.prompt, a.size)
    print(json.dumps({"ok": True, "mode": "placeholder", "path": a.out, "bytes": n,
                      "note": "geometric placeholder - set OPENROUTER_API_KEY for real art"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
