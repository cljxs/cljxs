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
import re
import struct
import sys
import time
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

# THE MOST A REQUEST MAY WRITE. OpenRouter prices every request at its WORST
# case before running it - input plus max_tokens of output - and refuses with
# 402 if that does not fit what the key has left, even when the real cost
# would (its docs, 2026-10-04). Unset, the worst case of a premium image model
# is the model's whole output allowance: on 2026-10-04 every drawing was
# refused with $0.64 of a $1.00 day still unspent. One square image is about
# 1,100-1,300 output tokens on Gemini's image models (believed, from their
# docs); these leave room for that and a little text, and no more.
IMAGE_MAX_TOKENS = 3000
PROOF_MAX_TOKENS = 1000


def post(req, timeout=120):
    """urlopen, with OpenRouter's own reason kept when it refuses. A bare
    "HTTP Error 402: Payment Required" said nothing about WHICH limit; the
    body says how much the request was priced at and how much is left."""
    import urllib.error
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
            msg = (body.get("error") or {}).get("message") or ""
        except Exception:
            msg = ""
        raise RuntimeError(f"HTTP {e.code}: {msg or e.reason}"[:300]) from None
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

# Written into every placeholder, and refused by draft. A placeholder is flat
# geometric art on a plain background, so every check for "is this artwork"
# passes it - and on 2026-10-02 one went to Printify as a nurse tee.
PLACEHOLDER_MARK = b"emily-assets placeholder - not artwork"


def is_placeholder(path):
    try:
        return PLACEHOLDER_MARK in Path(path).read_bytes()[:4096]
    except OSError:
        return False


def write_png(path, width, height, rows, mark=None):
    """rows: iterable of bytes objects, each width*3 long (RGB)."""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    if mark:
        png += chunk(b"tEXt", b"Comment\x00" + mark)
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
    return write_png(path, size, size, rows, mark=PLACEHOLDER_MARK)


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
    "no border. "
    "Any words must be spelled exactly and read naturally. Never draw a "
    "date, a timestamp or a file name."
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
    mod, entry = _catalogue_entry(product)
    return bool(entry) and mod.is_all_over(entry)


def shape(product):
    """The aspect ratio to ask the model for, or None to leave it the model's.

    The first all-over tote came back 1408x768 - the model's own choice,
    because nothing asked for anything else - for a face that is square.
    emily-printify.py knows the face from Printify's measured print area;
    this only passes its answer on.
    """
    mod, entry = _catalogue_entry(product)
    return mod.aspect_for(entry) if entry else None


def _catalogue_entry(product):
    """(emily-printify module, catalogue entry) for a product word.

    (None, None) when there is no product, no catalogue or no match. Drawing
    art must never stop because the catalogue is missing; the prompt just
    goes without what the catalogue would have added.
    """
    if not (product or "").strip():
        return None, None
    try:
        spec = importlib.util.spec_from_file_location(
            "emily_printify_ao", Path(__file__).resolve().parent / "emily-printify.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _key, entry = mod.resolve(mod.read_catalog(), product)
        return (mod, entry) if entry else (None, None)
    except Exception:
        return None, None


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


def on_product(product):
    """What the art is printed on, said so it is not drawn.

    This line used to read "Artwork for a mug." and on 2026-10-09 the model
    drew exactly that: two enamel mugs with handles and rims, which Printify
    then wrapped round a real mug - a mug printed with pictures of mugs.
    "For a mug" names the product as the subject; "printed ON a mug" names it
    as the surface. PRINT_DIRECTION's "not a product mockup" was there all
    along and did not stop it, because a drawn mug is not obviously a mockup.
    """
    p = product.strip()
    return (f"This artwork will be printed ON a {p}. Draw only the design that "
            f"goes on it - never the {p} itself, and no {p}, cup, handle, rim, "
            f"shirt, bag or other product shown as an object.")


def compose(idea, brief="", product="", evidence=None, already=()):
    """THE art prompt. One place.

    It used to be built in two: emily-new-build.py appended 'Flat vector
    illustration for a <product>, clean edges, no text' and emily-assets.py
    appended PRINT_DIRECTION, which says the same thing in different words.
    Two pieces of code deciding one thing always drift, and these two were
    already disagreeing about whether text was allowed.
    """
    # The idea's title is the product's working name. Handed over bare, the
    # image model drew it: a mug read "TEACHER'S COFFEE PLOT MUG" (2026-10-03),
    # every word spelled right and nothing anyone would buy. So it is labelled.
    parts = [f"Working name of this product, to tell you what it is - NEVER "
             f"text on the design: {str(idea or '').strip()}" if str(idea or "").strip() else "",
             "If the design has words, they are a short phrase a buyer would want "
             "to wear or use - never the product's name or a description of it."]
    if brief and brief.strip():
        parts.append(brief.strip())
    if product and product.strip():
        parts.append(on_product(product))
    parts += market_notes(evidence)
    if already:
        parts.append("The shop already sells these, so this must be visibly "
                     "different from all of them - a different subject and a "
                     "different composition, in the same style: "
                     + "; ".join(already) + ".")
    return "\n\n".join(p for p in parts if p)


def _size_of(path):
    """[width, height] of what came back, or None if it is not a readable PNG."""
    try:
        spec = importlib.util.spec_from_file_location(
            "knockout_sz", Path(__file__).resolve().parent / "knockout.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return list(mod.size(path))
    except Exception:
        return None


def ink_direction(product):
    """The garment colours this design prints on, and what that asks of it.

    "WILD & FREE ON THE TRAIL" (2026-10-05) was drawn in pale cream and
    printed on eight tee colours, five of them light: the words vanished on
    White, Ivory and Butter. Nothing told the model what it was printing on.
    The colours are the catalogue's own - the ones the owner picked - read
    from the variant titles. Empty for anything that is not worn."""
    mod, entry = _catalogue_entry(product)
    if not entry or not mod.is_garment(entry):
        return ""
    colours = []
    for t in entry.get("variant_titles") or []:
        c = mod.colour_of(t)
        if c and c not in colours:
            colours.append(c)
    if not colours:
        return ""
    return (f"This is printed on garments in these colours: {', '.join(colours)}. "
            f"Every element - above all any words - must be clearly readable on "
            f"every one of them: use dark, saturated ink with strong contrast. "
            f"No white, cream, beige or pale lettering.")


def directed(prompt, product=""):
    """The art direction on the end, once, however the prompt already reads.

    Which direction depends on the product, because an all-over print wants
    the opposite of a plain background.
    """
    ink = ink_direction(product)
    return (f"{prompt.strip()}\n\n"
            f"{ALL_OVER_DIRECTION if all_over(product) else PRINT_DIRECTION}"
            + (f"\n\n{ink}" if ink else ""))


def generate(path, prompt, key, model=None, product="", direction=None,
             aspect=None):
    """Draw one image. Returns (bytes written, usage).

    `usage.include` asks OpenRouter to price the call and hand the number back
    in the response. An image's cost cannot be worked out from the published
    rate alone - image_output is dollars per output TOKEN, and how many tokens
    an image is depends on the model, the size and the quality. So rather than
    estimate it and be wrong in a direction nobody can check, the run reports
    what it actually cost.

    `direction` and `aspect` are for art that is not a print file - the shop
    banner. Left out, a product's art gets its print direction and its face's
    shape exactly as before; the request is still built here, once.
    """
    content = (f"{prompt.strip()}\n\n{direction}" if direction is not None
               else directed(prompt, product))
    request = {
        "model": model or image_model(),
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
        "max_tokens": IMAGE_MAX_TOKENS,
        "usage": {"include": True},
    }
    ratio = aspect or shape(product)
    if ratio:
        # Asked for, not assumed. main() reports the shape that actually
        # came back, and draft covers the face whatever it is - a model that
        # ignores this costs some cropping, not a blank tote.
        request["image_config"] = {"aspect_ratio": ratio}
    body = json.dumps(request).encode()
    req = urllib.request.Request(OR_URL, data=body, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cljxs/cljxs",
        "X-Title": "emily-assets",
    })
    data = post(req)

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


# THE PROOFREAD. An image model draws letters, it does not spell them: the
# first bookish sweatshirt (2026-10-02) said "I'M JUST HE HER FOR THE BOOKISH
# MERCH" and an emblem carried that day's date. Nothing read the words back, so
# both went to Printify. A model that reads images now does, and code - not
# that model - decides what counts as a problem.
DEFAULT_PROOF_MODEL = "google/gemini-2.5-flash"
PROOF_TRIES = 3
# No redraw starts after this many seconds. emily-new-build gives the whole art
# step 180, and a step killed half way leaves no art at all.
REDRAW_BUDGET_S = 100

PROOF_ASK = (
    "This is a print file for a product. Read every piece of text drawn in it, "
    "exactly as drawn, letter for letter - do not correct anything. Then check "
    "it as a customer would. Reply with JSON only, nothing else:\n"
    '{"lines": [each line of text as drawn], "errors": [each misspelled word, '
    "garbled or doubled or missing word, or broken letter, quoted as drawn "
    'with what it should be], "product": null}\n'
    'If there is no text, "lines" and "errors" are []. '
    'Set "product" to the product\'s name (e.g. "mug", "t-shirt", "tote bag") '
    "if the picture SHOWS a product as an object - a mug with its handle and "
    "rim, a shirt with sleeves, a bag, a framed print, a sticker lying on "
    "something - instead of being flat artwork to print onto one. A small "
    "icon drawn as part of a flat design is artwork, not a product: null."
)

# Words a proofreader writes when it means "no product". read_proof keeps the
# JSON as sent, and a model asked for null answers "none" often enough.
NO_PRODUCT = ("", "null", "none", "no", "false", "n/a")

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_RE = re.compile(
    r"\b(?:19|20)\d{2}\s?[-/.]\s?\d{1,2}\s?[-/.]\s?\d{1,2}\b"        # 2026-10-02
    r"|\b\d{1,2}\s?[-/.]\s?\d{1,2}\s?[-/.]\s?(?:19|20)?\d{2}\b"       # 10/02/2026
    r"|\b" + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?\b"                # Oct 2
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"(?=\W|$)",          # 2 October
    re.I)


def proof_model(override=None):
    return (override or os.environ.get("EMILY_PROOF_MODEL") or "").strip() \
        or DEFAULT_PROOF_MODEL


def read_proof(content):
    """The JSON object in a proofreading reply, or None. Models wrap JSON in
    prose or a ```json fence often enough that only the braces are trusted."""
    text = content if isinstance(content, str) else ""
    if isinstance(content, list):                       # content parts
        text = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        d = json.loads(text[a:b + 1])
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _name_words(s):
    return [w for w in re.findall(r"[a-z0-9]+", str(s or "").lower().replace("'", ""))
            if len(w) > 2 and w not in ("the", "and", "for", "with")]


def prints_name(lines, name):
    """Does the drawn text spell out the product's own name? Three quarters of
    its words is enough: the mug printed all four of 'Teacher's Coffee Plot
    Mug', and a design that merely shares a word with its name is fine."""
    want = _name_words(name)
    have = set(_name_words(" ".join(lines)))
    return len(want) >= 2 and sum(w in have for w in want) >= 0.75 * len(want)


def proof_problems(reading, name=None):
    """[problem] from what the proofreader read. A year alone is allowed -
    "EST. 2026" is a style - a calendar date never is. Nor is the product's
    own name, spelled however well, nor a picture of the product itself (the
    mug of mugs, 2026-10-09)."""
    lines = [str(x) for x in (reading or {}).get("lines") or [] if str(x).strip()]
    out = [f"prints a date: {m.group(0)!r}" for m in DATE_RE.finditer(" / ".join(lines))]
    if name and prints_name(lines, name):
        out.append(f"prints the product's own name ({name!r}) instead of a design")
    out += [f"text error: {e}" for e in (reading or {}).get("errors") or [] if str(e).strip()]
    shown = (reading or {}).get("product")
    if shown and str(shown).strip().lower() not in NO_PRODUCT:
        out.append(f"draws a {str(shown).strip()} - a picture of the product, not artwork "
                   f"to print on it")
    return out


def proof(path, key, model=None, name=None):
    """(problems, lines) for the text drawn in an image. Raises if the
    proofreader cannot be asked or its answer cannot be read - an unread
    proof is not a passed one, and the caller says which happened."""
    data = base64.b64encode(Path(path).read_bytes()).decode()
    body = json.dumps({
        "model": proof_model(model),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROOF_ASK},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}]}],
        "max_tokens": PROOF_MAX_TOKENS,
    }).encode()
    req = urllib.request.Request(OR_URL, data=body, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cljxs/cljxs",
        "X-Title": "emily-proof",
    })
    data = post(req)
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    reading = read_proof(msg.get("content"))
    if reading is None:
        raise RuntimeError("the proofreader's reply was not JSON: "
                           + str(msg.get("content"))[:120])
    return proof_problems(reading, name), [str(x) for x in reading.get("lines") or []]


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


def compare(prompt, models, key, out_dir, product=""):
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
            # The product travels, so a tote comparison is drawn as a tote -
            # all-over direction, the face's shape - and the models are being
            # compared on the picture that would actually be printed.
            n, usage = generate(path, prompt, key, model, product)
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


def cmd_proof(paths):
    """emily-assets.py proof <png> ... - read the words in existing art, the
    same proofread drafts get. The way to check the proofreader on a real
    file, which is the only kind of check that has ever caught a parser bug."""
    load_credentials()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("no OPENROUTER_API_KEY - nothing can be proofread", file=sys.stderr)
        return 2
    worst = 0
    for p in paths:
        try:
            problems, lines = proof(p, key)
        except Exception as exc:
            print(f"{p}: NOT PROOFREAD - {type(exc).__name__}: {str(exc)[:160]}")
            worst = 2
            continue
        print(f"{p}: reads {' / '.join(lines) or '(no text)'}")
        for x in problems:
            print(f"  PROBLEM: {x}")
        worst = max(worst, 1 if problems else 0)
    print(f"proofread by {proof_model()}")
    return worst


def main():
    if sys.argv[1:2] == ["proof"]:
        return cmd_proof(sys.argv[2:])
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
    ap.add_argument("--name", default="",
                    help="the product's working name, which the art must not print")
    ap.add_argument("--compare", default="",
                    help="comma-separated models: draw the SAME prompt with "
                         "each, into a build folder the Deck already shows")
    a = ap.parse_args()

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    load_credentials()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()

    if a.compare:
        return compare(a.prompt, a.compare, key, Path(a.out), a.product)

    if key and not a.placeholder_only:
        try:
            model = image_model(a.model)
            prompt, problems, lines, note = a.prompt, [], [], None
            began = time.monotonic()
            for attempt in range(1, PROOF_TRIES + 1):
                if attempt > 1 and time.monotonic() - began > REDRAW_BUDGET_S:
                    note = f"out of time after {attempt - 1} drawing(s): " + "; ".join(problems)
                    break
                n, usage = generate(a.out, prompt, key, model, a.product)
                try:
                    problems, lines = proof(a.out, key, name=a.name or None)
                except Exception as exc:
                    note = f"text not proofread: {str(exc)[:120]}"
                    break
                if not problems:
                    break
                # Drawn again with what went wrong said plainly.
                prompt = (f"{a.prompt}\n\nThe last attempt was wrong: "
                          + "; ".join(problems) + ". Spell every word exactly; no dates.")
            print(json.dumps({"ok": True, "mode": "generated", "model": model,
                              "path": a.out, "bytes": n,
                              "asked_shape": shape(a.product),
                              "got_size": _size_of(a.out),
                              "cost_usd": cost_of(usage),
                              "attempts": attempt, "text": lines,
                              "proof": note or (problems or "clean")}))
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
