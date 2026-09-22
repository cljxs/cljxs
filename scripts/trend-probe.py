#!/usr/bin/env python3
"""
trend-probe.py — what is the rest of the world searching for?

    trend-probe.py suggest fall sticker   what Google completes the phrase to
    trend-probe.py expand fall sticker    the phrase's whole query space, a-z
    trend-probe.py trending               today's breakout searches (US)

THE OTHER HALF OF THE PICTURE. etsy-probe.py says what is already being SOLD.
This says what is being SEARCHED FOR. Neither is enough alone: 'fall sticker'
has 109,099 active Etsy listings whose top results have 1, 14 and 0 favourites
- enormous supply meeting demand that has already been met. You cannot see
that from either source by itself. market-scan.py joins them.

No key, no account, no quota - these are the endpoints a browser's address
bar uses.

WHAT WORKS AND WHAT DOES NOT. Checked from this droplet, against real
responses, rather than assumed:

  suggestqueries.google.com/complete/search    200, works, no key
  trends.google.com/trending/rss?geo=US        200, works, no key
  trends.google.com/trends/api/explore         429 HTML - dead from a
                                               datacenter IP, do not build
                                               on it

That last one is the interesting failure. It does not return an error in
JSON; it returns an HTML page titled 'Error 429 (Too Many Requests)'. A
parser that assumed JSON would either crash or, worse, quietly return nothing
and read as 'no trend data for this phrase'. tests/fixtures/ holds that page
so the refusal stays tested.

Standard library only.
"""

import importlib.util
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# THE trademark list, imported. Emily will need the same one before she
# generates art, and two copies would mean the one she reads is the one that
# never got Pikmin added to it.
_ip = importlib.util.spec_from_file_location("ip_check", SCRIPTS / "ip-check.py")
ipc = importlib.util.module_from_spec(_ip)
_ip.loader.exec_module(ipc)

SUGGEST = "https://suggestqueries.google.com/complete/search"
TRENDING = "https://trends.google.com/trending/rss"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Between requests in an expansion. Suggest has no published limit and no
# rate-limit headers to read, so the only safe number is one that looks like
# a person typing. 26 letters at this pace is about 9 seconds.
POLITE = 0.35

HT = "{https://trends.google.com/trending/rss}"

# What a suggestion tells us about the person typing it. A completion is
# evidence of demand even when it is demand we cannot serve - so these
# LABEL rather than delete. Dropping 'fall stickers png' silently would hide
# the fact that a printable version of the phrase is wanted at all.
INTENT = [
    # 'target' is BOTH a shop and an ordinary word: this list once labelled
    # 'target practice sticker' as someone shopping at Target and deleted a
    # perfectly good product idea. A retailer query puts the shop last -
    # 'fall stickers target' - so that is where it is matched, and an
    # ambiguous word that is not last is left alone. Missing one retailer
    # query costs nothing; killing a real idea costs the idea.
    (re.compile(r"\b(near me|amazon|walmart|hobby lobby|michaels|"
                r"dollar tree|temu|shein|aliexpress|ebay)\b", re.I),
     "offsite", "shopping for a shop that is not Etsy"),
    (re.compile(r"\b(target|kohls|costco|cvs|walgreens)\s*$", re.I),
     "offsite", "shopping for a shop that is not Etsy"),
    # 'crochet pattern', 'goodnotes' and 'clipart' all came back inside the
    # BUYING bucket on the first real run. A crochet pattern is a document,
    # GoodNotes is a tablet app, and clipart is an image licence - none of
    # them is a thing to print and post.
    (re.compile(r"\b(png|svg|printable|print at home|digital|download|"
                r"copy and paste|cricut file|clip ?art|cut file|"
                r"sublimation|goodnotes|notability|procreate|"
                r"(crochet|knit(ting)?|sewing|quilt(ing)?) pattern)\b", re.I),
     "digital", "wants a file, not a physical product"),
    (re.compile(r"\b(free|diy|how to|tutorial|ideas|template)\b", re.I),
     "research", "looking for information, not buying"),
]


def fetch(url, timeout=20):
    """(text, error). Never raises - callers decide what a failure means."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as r:
            return r.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        body = e.read(400).decode("utf-8", "replace")
        return None, f"HTTP {e.code}: {looks_like(body)}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def looks_like(body):
    """Name what came back, for an error message worth reading.

    The 429 from Trends is an HTML page, not JSON. Saying 'an HTML error
    page' beats pasting 400 bytes of stylesheet into the log.
    """
    head = (body or "").lstrip()[:200].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        m = re.search(r"<title>([^<]{0,120})</title>", body, re.I)
        return f"an HTML page ({m.group(1)})" if m else "an HTML page"
    return (body or "")[:200] or "an empty response"


def parse_suggest(text):
    """(suggestions, relevance, error) from a Suggest payload.

    THE ARITY DIFFERS BY CLIENT. client=chrome returns five elements,
    client=firefox four. So the metadata is taken from the END of the array
    and the suggestions from index 1 - never from a fixed position, which is
    the assumption that would break the day a client is added.
    """
    try:
        data = json.loads(text)
    except Exception:
        return [], [], f"not JSON - {looks_like(text)}"
    if not isinstance(data, list) or len(data) < 2:
        return [], [], "JSON, but not the shape Suggest returns"
    words = [w for w in (data[1] or []) if isinstance(w, str)]
    meta = data[-1] if isinstance(data[-1], dict) else {}
    rel = meta.get("google:suggestrelevance") or []
    rel = [r for r in rel if isinstance(r, (int, float))]
    return words, rel[:len(words)], None


def filler_from(rel):
    """How many trailing relevance scores are just rank, not signal.

    A real payload for 'fall sticker' scores its suggestions

        1250, 601, 600, 561, 560, 559, 558, 557, 556, 555, 554, 553, ...

    The tail is a consecutive descending run: Google has stopped reporting
    how popular each one is and is only reporting the order it put them in.
    Treating 554 as a measurement would invent a difference between the 11th
    and 12th suggestion that the payload does not contain.

    Returns the count of scores in that trailing run.
    """
    if len(rel) < 3:
        return 0
    n = 1
    while n < len(rel) and rel[-n - 1] == rel[-n] + 1:
        n += 1
    return n if n >= 3 else 0


def intent_of(phrase):
    """(label, why) or (None, None) - what this searcher actually wants."""
    for pattern, label, why in INTENT:
        if pattern.search(phrase):
            return label, why
    return None, None


RETAILERS = re.compile(
    r"\s*\b(near me|on amazon|at amazon|amazon|at walmart|walmart|"
    r"hobby lobby|michaels|dollar tree|temu|shein|aliexpress|ebay|target|"
    r"kohls|costco|cvs|walgreens)\b\s*$", re.I)


def stem(word):
    """Crudest possible singular. 'stickers' -> 'sticker', and that is all
    this needs to do - it is comparing a phrase to its own seed, not parsing
    English."""
    w = word.lower().strip()
    for suffix in ("ies", "es", "s"):
        if len(w) > len(suffix) + 2 and w.endswith(suffix):
            return w[:-len(suffix)] + ("y" if suffix == "ies" else "")
    return w


# Words that mean the same product to a buyer. WITHOUT this, drift detection
# is actively harmful: expanding 'cozy fall sweatshirt' returns pullover,
# sweater, hoodie and crewneck - the four best results in the run - and a
# literal head-noun match would have discarded all four as off-topic.
#
# Kept deliberately small and only for things actually sold here. 'label' is
# NOT a synonym for sticker, however much it looks like one: it pulls in
# 'fall risk label' and 'fall records label', which are a hospital sign and
# a music company.
SYNONYMS = [
    {"sticker", "decal"},
    {"sweatshirt", "hoodie", "crewneck", "pullover", "sweater", "jumper"},
    {"shirt", "tee", "tshirt", "t-shirt", "top"},
    {"mug", "tumbler", "cup", "glass"},
    {"tote", "bag", "totebag"},
    {"hat", "cap", "beanie"},
    {"poster", "print", "wallart"},
    {"magnet", "magnets"},
    {"pin", "button", "badge"},
]


def family(word):
    """Every word that means the same product as this one."""
    w = stem(word)
    out = {w}
    for group in SYNONYMS:
        if w in {stem(g) for g in group}:
            out |= {stem(g) for g in group}
    return out


def drifted(seed, phrase):
    """Has the completion wandered off the thing we asked about?

    Expanding 'cozy fall sweatshirt' returned 'cozy fall desserts', and the
    first run counted it as someone trying to buy a physical thing. It is
    not: Google completed 'cozy fall', not 'cozy fall sweatshirt'. The seed's
    LAST word is what is being sold, so a completion that has lost it is
    about something else.
    """
    words = (seed or "").split()
    if not words:
        return False
    head = family(words[-1])
    return not (head & {stem(w) for w in re.findall(r"[a-z'-]+", phrase.lower())})


def without_retailer(phrase):
    """'fall nail stickers amazon' -> 'fall nail stickers', or None.

    An offsite query is the STRONGEST demand signal in the whole sweep and
    the first version threw it away. Somebody typing 'fall window stickers
    near me' has decided to buy, has a product in mind, and has not thought
    of Etsy. Strip the shop and what is left is a validated product phrase.
    """
    out = RETAILERS.sub("", phrase or "").strip()
    return out if out and out != (phrase or "").strip() else None


def retail_demand(found):
    """{product phrase: {shops it was hunted in}} across a whole expansion.

    A phrase hunted in three different shops is three separate people who
    wanted it enough to guess at a stockist.
    """
    out = {}
    for phrase in found:
        m = RETAILERS.search(phrase)
        bare = without_retailer(phrase)
        if m and bare:
            out.setdefault(bare, set()).add(m.group(1).lower())
    return out


def bucket(seed, phrase):
    """The one group this completion belongs in, worst news first."""
    tier, what, _why = ipc.risky(phrase)
    if tier == "blocked":
        return "blocked", what
    if drifted(seed, phrase):
        return "drift", None
    label, _why = intent_of(phrase)
    if label:
        return label, None
    if tier == "check":
        return "check", what
    return "buying", None


def suggest(phrase, client="chrome"):
    """(suggestions, relevance, error) for one phrase."""
    q = urllib.parse.urlencode({"client": client, "q": phrase, "hl": "en"})
    text, err = fetch(f"{SUGGEST}?{q}")
    if err:
        return [], [], err
    return parse_suggest(text)


def expand(phrase, letters="abcdefghijklmnopqrstuvwxyz", pause=POLITE,
           on_step=None):
    """Every completion of '<phrase> a' .. '<phrase> z', plus the bare phrase.

    Suggest returns ten-ish completions for any one query, which is a keyhole
    view. Asking once per letter widens it to the phrase's whole neighbourhood
    - the standard trick, and the only way to get breadth out of an endpoint
    that has no breadth parameter.

    Returns {suggestion: best_rank}, where rank 0 is the top of some list.
    """
    found, errors = {}, []
    for i, seed in enumerate([phrase] + [f"{phrase} {c}" for c in letters]):
        words, _rel, err = suggest(seed)
        if err:
            errors.append(f"{seed!r}: {err}")
        for rank, w in enumerate(words):
            w = w.strip().lower()
            if w and (w not in found or rank < found[w]):
                found[w] = rank
        if on_step:
            on_step(i, seed, len(found))
        if pause:
            time.sleep(pause)
    return found, errors


def trending(geo="US"):
    """(items, error). Today's breakout searches, as (phrase, traffic)."""
    text, err = fetch(f"{TRENDING}?{urllib.parse.urlencode({'geo': geo})}")
    if err:
        return [], err
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [], f"not RSS - {looks_like(text)}"
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        traffic = (item.findtext(f"{HT}approx_traffic") or "").strip()
        heads = [(h.text or "").strip()
                 for h in item.iter(f"{HT}news_item_title")]
        if title:
            out.append((title, traffic, heads))
    return out, None


def cmd_suggest(words):
    phrase = " ".join(words)
    got, rel, err = suggest(phrase)
    if err:
        print(f"suggest failed - {err}", file=sys.stderr)
        return 1
    if not got:
        print(f"'{phrase}' -> nothing. Google does not complete it, which "
              f"usually means\n  almost nobody types it.")
        return 0
    filler = filler_from(rel)
    real = len(rel) - filler
    print(f"'{phrase}' -> {len(got)} completions\n")
    for i, w in enumerate(got):
        score = rel[i] if i < len(rel) else None
        if score is None:
            shown = ""
        elif i >= real:
            shown = f"{score:>6} (rank only)"
        else:
            shown = f"{score:>6}"
        label, _why = intent_of(w)
        tag = f"  [{label}]" if label else ""
        print(f"  {i + 1:>2}. {w:<44}{shown}{tag}")
    if filler:
        print(f"\n  The last {filler} scores are a consecutive descending run "
              f"- that is\n  Google reporting ORDER, not popularity. Only the "
              f"top {real} carry a\n  measurement worth comparing.")
    buyable = [w for w in got if intent_of(w)[0] is None]
    print(f"\n  {len(buyable)} of {len(got)} look like someone trying to BUY "
          f"a physical thing.")
    return 0


def cmd_expand(words):
    phrase = " ".join(words)
    print(f"expanding '{phrase}' across 26 letters, ~{POLITE:.2f}s apart "
          f"(about {27 * POLITE:.0f}s)...", flush=True)

    def step(i, seed, total):
        if i % 6 == 0 or i == 26:
            print(f"  {i + 1:>2}/27  {seed:<30} {total} found so far",
                  flush=True)

    found, errors = expand(phrase, on_step=step)
    if errors:
        print(f"\n  {len(errors)} of 27 queries failed:", file=sys.stderr)
        for e in errors[:3]:
            print(f"    {e}", file=sys.stderr)
    if not found:
        print("\nnothing came back at all. Suggest may be blocking this IP.")
        return 1

    groups, marks = {}, {}
    for w, rank in sorted(found.items(), key=lambda kv: (kv[1], kv[0])):
        name, what = bucket(phrase, w)
        groups.setdefault(name, []).append((w, rank))
        if what:
            marks[w] = what

    print(f"\n{len(found)} distinct searches around '{phrase}'\n")
    ORDER = [
        ("blocked", "SOMEBODY ELSE'S PROPERTY - do not make these"),
        ("buying", "wants to buy one"),
        ("check", "a word that is also a property - a human decides"),
        ("digital", "wants a file, not a physical product"),
        ("offsite", "shopping for a shop that is not Etsy"),
        ("research", "looking for information, not buying"),
        ("drift", f"not about a {phrase.split()[-1]} at all - Google "
                  f"completed a shorter phrase"),
    ]
    for name, why in ORDER:
        rows = groups.get(name) or []
        if not rows:
            continue
        print(f"  {name.upper()} ({len(rows)}) - {why}")
        for w, _rank in rows[:18]:
            note = f"   <- {marks[w]}" if w in marks else ""
            print(f"      {w}{note}")
        if len(rows) > 18:
            print(f"      ... and {len(rows) - 18} more")
        print()

    # The bit the first version threw away.
    hunted = retail_demand(found)
    multi = sorted(((p_, shops) for p_, shops in hunted.items() if len(shops) > 1),
                   key=lambda kv: (-len(kv[1]), kv[0]))
    if multi:
        print(f"  HUNTED IN MORE THAN ONE SHOP ({len(multi)}) - the strongest "
              f"signal here")
        print(f"  Someone typing 'X near me' has decided to buy, knows what "
              f"they want,\n  and has not thought of Etsy. Two different "
              f"shops means two such people.")
        for p_, shops in multi[:12]:
            print(f"      {p_:<40} {len(shops)} shops: "
                  f"{', '.join(sorted(shops))}")
        print()

    buying = len(groups.get("buying") or [])
    print(f"  {buying} of {len(found)} are someone trying to buy a physical "
          f"thing on Etsy.")
    print("  Nothing here is a sales figure. It is what people TYPE - the "
          "supply side\n  comes from etsy-probe.py, and market-scan.py is "
          "what puts the two together.")
    return 0


def cmd_trending(words):
    geo = (words[0] if words else "US").upper()
    items, err = trending(geo)
    if err:
        print(f"trending failed - {err}", file=sys.stderr)
        return 1
    print(f"today's breakout searches in {geo} ({len(items)})\n")
    for phrase, traffic, heads in items:
        print(f"  {phrase:<32} {traffic:>8}")
        if heads:
            print(f"      {heads[0][:96]}")
    print("\n  These are NEWS spikes, not product categories. Most days none "
          "of them is\n  a thing to print on a shirt, and the ones that are "
          "have a shelf life of\n  about a week. Read it for the occasional "
          "hit, not as a work queue.")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    rest = sys.argv[2:]
    if cmd == "trending":
        return cmd_trending(rest)
    if cmd in ("suggest", "expand") and rest:
        return (cmd_suggest if cmd == "suggest" else cmd_expand)(rest)
    print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
