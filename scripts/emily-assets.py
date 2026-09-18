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

def generate(path, prompt, key, model=None):
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
        "messages": [{"role": "user", "content": prompt}],
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
            n, usage = generate(a.out, a.prompt, key, model)
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
