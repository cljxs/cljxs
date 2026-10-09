#!/usr/bin/env python3
"""
store-report.py — how the shop's own listings are doing, from Etsy.

    store-report.py setup VintageLoomTreasures   find the shop's id, once
    store-report.py fetch                        snapshot every active listing today
    store-report.py show                         the report: today against the last snapshot
    store-report.py pins                         a Pinterest pin for every listing, and its board
    store-report.py audit                        what to fix in each listing's title, tags and description

Nothing in the system looked at the store itself. Scout measures other
people's listings and Emily makes new ones; which of OURS get looked at, and
which get saved, went unrecorded - so what to make more of was a guess. This
reads the shop's active listings once a day and keeps each day's numbers, so
the change since yesterday is a subtraction rather than a memory.

It uses Scout's Etsy key through etsy-probe.py (the one Etsy client), and the
listing arithmetic in market-scan.py (favourites per view, listing age),
which was built against real Etsy payloads. Nothing is estimated: a number
Etsy did not send is shown as missing, not as zero.

Snapshots: agents/emily/state/store/<Eastern date>.json, one a day; a second
fetch on the same day replaces the first. Fury's daily briefing reads the
latest two through summary() here, so the sums exist once.

Standard library only.
"""

import importlib.util
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import et_time  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
STORE = ROOT / "agents" / "emily" / "state" / "store"
SHOP = STORE / "shop.json"

PAGE = 100                  # Etsy's largest page for a shop's listings
QUIET_AFTER_DAYS = 7        # a listing this old with no views is worth a look


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ep():
    return _load("etsy_probe", "etsy-probe.py")


def _ms():
    return _load("market_scan", "market-scan.py")


# ------------------------------------------------------------------ fetching

def pick_shop(results, name):
    """The shop in a findShops answer whose name is exactly `name`, ignoring
    case - or None. Etsy's search is loose; the owner's shop is not."""
    want = (name or "").strip().lower()
    for s in results or []:
        if str(s.get("shop_name") or "").strip().lower() == want:
            return s
    return None


def snapshot_row(listing):
    """One listing, reduced to what the report reads."""
    ms = _ms()
    return {
        "listing_id": listing.get("listing_id"),
        "title": str(listing.get("title") or "")[:140],
        "views": listing.get("views") if isinstance(listing.get("views"), int) else None,
        "favs": listing.get("num_favorers") if isinstance(listing.get("num_favorers"), int) else None,
        "price": ms.price_of(listing),
        "age_days": round(ms.age_days(listing), 1) if ms.age_days(listing) is not None else None,
        "url": listing.get("url"),
        # Kept for the pins: a pin is written from the listing's own words.
        "tags": [str(t) for t in (listing.get("tags") or []) if str(t).strip()][:13],
        "description": str(listing.get("description") or "")[:600],
    }


def cmd_setup(args):
    if not args:
        print("usage: store-report.py setup <exact Etsy shop name>", file=sys.stderr)
        return 2
    name = " ".join(args)
    ep = _ep()
    key, err = ep.api_key()
    if err:
        print(err, file=sys.stderr)
        return 2
    data, _h, err = ep.call(f"/shops?{urllib.parse.urlencode({'shop_name': name})}", key)
    if err:
        print(f"Etsy refused the lookup: {err}", file=sys.stderr)
        return 1
    shop = pick_shop((data or {}).get("results"), name)
    if not shop:
        near = [s.get("shop_name") for s in (data or {}).get("results") or []][:5]
        print(f"no shop named exactly {name!r}."
              + (f" Etsy suggested: {', '.join(map(str, near))}" if near else ""), file=sys.stderr)
        return 1
    STORE.mkdir(parents=True, exist_ok=True)
    SHOP.write_text(json.dumps({"shop_id": shop["shop_id"], "shop_name": shop["shop_name"]}) + "\n")
    print(f"found {shop['shop_name']} (shop {shop['shop_id']}). Now: store-report.py fetch")
    return 0


def cmd_fetch(_args):
    try:
        shop = json.loads(SHOP.read_text())
    except Exception:
        print("no shop set up yet:  store-report.py setup <your Etsy shop name>", file=sys.stderr)
        return 2
    ep = _ep()
    key, err = ep.api_key()
    if err:
        print(err, file=sys.stderr)
        return 2
    rows, offset = [], 0
    while True:
        data, _h, err = ep.call(f"/shops/{shop['shop_id']}/listings/active"
                                f"?limit={PAGE}&offset={offset}", key)
        if err:
            print(f"Etsy refused the listings: {err}", file=sys.stderr)
            return 1
        page = (data or {}).get("results") or []
        rows += page
        offset += len(page)
        if len(page) < PAGE or offset >= int((data or {}).get("count") or 0):
            break
    listings = [snapshot_row(r) for r in rows]
    day = et_time.day()
    STORE.mkdir(parents=True, exist_ok=True)
    (STORE / f"{day}.json").write_text(json.dumps({
        "day": day, "taken_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "shop_id": shop["shop_id"], "shop_name": shop["shop_name"], "listings": listings,
    }, indent=1) + "\n")
    # Said out loud: the report ranks on views, and if Etsy stops sending
    # them for this endpoint the ranking would be silently empty.
    missing = sum(1 for r in listings if r["views"] is None)
    print(f"{len(listings)} active listing(s) saved for {day}"
          + (f" - {missing} came back without a view count" if missing else ""))
    return 0


# ------------------------------------------------------------------ the report

def snapshots():
    """Every saved day, oldest first."""
    out = []
    for f in sorted(STORE.glob("????-??-??.json")) if STORE.is_dir() else []:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("listings"), list):
            out.append(d)
    return out


def _gain(now, before):
    if not isinstance(now, int):
        return None
    if before is None:
        return None
    return now - before if isinstance(before, int) else None


def summary(latest, previous=None):
    """The report as data, so the briefing and the command line print the
    same numbers. `previous` is the last snapshot before `latest`, if any."""
    before = {r.get("listing_id"): r for r in (previous or {}).get("listings") or []}
    rows = []
    for r in latest.get("listings") or []:
        was = before.get(r.get("listing_id")) or {}
        views, favs, age = r.get("views"), r.get("favs"), r.get("age_days")
        rows.append({
            "title": r.get("title") or "?",
            "views": views, "favs": favs, "age_days": age, "price": r.get("price"),
            "views_gained": _gain(views, was.get("views")) if was else None,
            "favs_gained": _gain(favs, was.get("favs")) if was else None,
            "views_per_day": round(views / age, 1) if isinstance(views, int) and age else None,
            "new": not was and previous is not None,
        })
    rows.sort(key=lambda x: (-(x["views_gained"] if x["views_gained"] is not None else -1),
                             -(x["views"] or 0)))
    total = lambda k: sum(r[k] for r in rows if isinstance(r[k], int))
    quiet = [r for r in rows if r["views"] == 0 and (r["age_days"] or 0) >= QUIET_AFTER_DAYS]
    saved = [r for r in rows if isinstance(r["favs"], int) and r["favs"] > 0]
    return {
        "day": latest.get("day"), "since": (previous or {}).get("day"),
        "listings": len(rows), "views": total("views"), "favs": total("favs"),
        "views_gained": total("views_gained") if previous else None,
        "favs_gained": total("favs_gained") if previous else None,
        "rows": rows,
        "quiet": [r["title"] for r in quiet],
        "most_saved": sorted(saved, key=lambda r: -r["favs"])[:3],
        "audit": audit_line(audit(latest)),
    }


def latest_summary():
    days = snapshots()
    if not days:
        return None
    return summary(days[-1], days[-2] if len(days) > 1 else None)


def short(title, n=46):
    t = re.sub(r"\s+", " ", str(title)).strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def lines(s):
    """The report as text - the briefing prints these too."""
    if not s:
        return ["No store snapshot yet. Once: store-report.py setup <shop name>, "
                "then store-report.py fetch."]
    head = f"{s['listings']} listing(s), {s['views']:,} views, {s['favs']:,} favourites"
    if s["since"]:
        head += (f" - since {s['since']}: +{s['views_gained']:,} views, "
                 f"+{s['favs_gained']:,} favourites")
    out = [head]
    for r in s["rows"]:
        gain = f" (+{r['views_gained']})" if isinstance(r["views_gained"], int) and r["views_gained"] else ""
        views = "?" if r["views"] is None else f"{r['views']:,}"
        favs = "?" if r["favs"] is None else f"{r['favs']}"
        age = f"{r['age_days']:.0f}d" if r["age_days"] is not None else "?"
        out.append(f"  {short(r['title']):<47} {views:>6} views{gain:<7} {favs:>3} fav  {age:>4}"
                   + ("  NEW" if r["new"] else ""))
    if s["most_saved"]:
        out.append("Most favourited: " + "; ".join(f"{short(r['title'], 36)} ({r['favs']})"
                                                     for r in s["most_saved"]))
    if s.get("audit"):
        out.append(s["audit"])
    if s["quiet"]:
        out.append(f"No views after {QUIET_AFTER_DAYS}+ days - title, first photo or price "
                   f"worth a look: " + "; ".join(short(t, 36) for t in s["quiet"]))
    return out


# ------------------------------------------------------------------ the audit
#
# What each listing's own words do for Etsy search, checked the same way every
# time. Two kinds of rule, and each says which it is:
#
#   LIMIT   Etsy's own limits: 13 tags, 20 characters a tag, 140 a title.
#   ADVICE  From sellers and tool makers who measured it (researched
#           2026-09-25): Etsy shows roughly the first 40 characters of a
#           title in search; over 70% of purchases come from searches of three
#           or more words; the opening of the description is the snippet
#           Google shows. Advice is a reason to look, not a verdict.
#
# The audit reads the saved snapshot, so it costs nothing and calls nobody.
# Fixing is the owner's: nothing here can edit a listing on Etsy.

TAG_SLOTS = 13            # LIMIT
TAG_MAX = 20              # LIMIT, characters
TITLE_MAX = 140           # LIMIT, characters
TITLE_SEEN = 40           # ADVICE: what a search result shows of a title
TITLE_SHORT = 60          # ADVICE: a title this short leaves search phrases unused
ONE_WORD_TAGS_MAX = 3     # ADVICE: shoppers buy on long phrases
DESCRIPTION_MIN_WORDS = 50
DESCRIPTION_OPENING = 160  # ADVICE: characters Google shows as the snippet

_STOP = {"a", "an", "and", "the", "for", "of", "to", "in", "on", "with", "by", "or",
         "your", "my", "our", "is", "it", "this", "that", "at", "from", "as", "be"}


def _words(text):
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def _keywords(text):
    return {w for w in _words(text) if w not in _STOP and len(w) > 1}


def _stem(word):
    """Plural folded to singular, crudely: tote/totes, gift/gifts, bag/bags.
    Enough to catch two tags that are one search."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _tag_key(tag):
    return " ".join(sorted(_stem(w) for w in _words(tag)))


def audit_listing(r):
    """[(kind, message)] for one snapshot row, worst first. Empty is clean."""
    title = str(r.get("title") or "")
    tags = [str(t).strip() for t in (r.get("tags") or []) if str(t).strip()]
    desc = str(r.get("description") or "")
    out = []

    # tags
    if len(tags) < TAG_SLOTS:
        out.append(("LIMIT", f"uses {len(tags)} of {TAG_SLOTS} tags - every empty slot is a "
                             f"search this listing cannot be found for"))
    for t in tags:
        if len(t) > TAG_MAX:
            out.append(("LIMIT", f"tag {t!r} is {len(t)} characters; Etsy's limit is {TAG_MAX}"))
    seen = {}
    for t in tags:
        k = _tag_key(t)
        if k in seen:
            same = "the same tag twice" if t.lower() == seen[k].lower() else "one search, two slots"
            out.append(("ADVICE", f"{seen[k]!r} and {t!r} are {same} - replace one"))
        else:
            seen[k] = t
    single = [t for t in tags if len(_words(t)) == 1]
    if len(single) > ONE_WORD_TAGS_MAX:
        out.append(("ADVICE", f"{len(single)} one-word tags ({', '.join(single)}) - shoppers "
                              f"buy on phrases of three or more words"))
    title_words = {_stem(w) for w in _keywords(title)}
    stray = [t for t in tags if not ({_stem(w) for w in _keywords(t)} & title_words)]
    if stray:
        out.append(("ADVICE", f"{len(stray)} tag(s) share no word with the title "
                              f"({', '.join(stray[:4])}{'...' if len(stray) > 4 else ''}) - "
                              f"title and tags work best saying the same thing"))

    # title
    if len(title) > TITLE_MAX:
        out.append(("LIMIT", f"title is {len(title)} characters; Etsy's limit is {TITLE_MAX}"))
    if len(title) < TITLE_SHORT:
        out.append(("ADVICE", f"title uses {len(title)} of {TITLE_MAX} characters - room for "
                              f"the phrases a shopper would type"))
    seen_part = title[:TITLE_SEEN].lower()
    if tags and not any(t.lower() in seen_part for t in tags):
        out.append(("ADVICE", f"none of the tags appears in the first {TITLE_SEEN} characters "
                              f"of the title, which is what search shows"))
    counts = {}
    for w in _keywords(title):
        counts[w] = sum(1 for x in _words(title) if x == w)
    repeated = sorted(w for w, n in counts.items() if n >= 3)
    if repeated:
        out.append(("ADVICE", f"title repeats {', '.join(repeated)} - once is enough for search"))
    shouting = [w for w in re.findall(r"[A-Za-z]{4,}", title) if w.isupper()]
    if len(shouting) >= 2:
        out.append(("ADVICE", f"title shouts ({' '.join(shouting[:4])}) - reads as spam"))

    # description
    if len(desc) < 600 and len(_words(desc)) < DESCRIPTION_MIN_WORDS:
        out.append(("ADVICE", f"description is {len(_words(desc))} words - thin for a shopper "
                              f"deciding, and for Google"))
    if desc and not ({_stem(w) for w in _keywords(desc[:DESCRIPTION_OPENING])} & title_words):
        out.append(("ADVICE", "the description's opening shares no word with the title - "
                              "it is the snippet Google shows"))
    return sorted(out, key=lambda x: x[0] != "LIMIT")


def audit(latest):
    """Every listing in a snapshot with what to fix, most to fix first."""
    rows = []
    for r in (latest or {}).get("listings") or []:
        rows.append({"listing_id": r.get("listing_id"), "title": r.get("title") or "?",
                     "views": r.get("views"), "favs": r.get("favs"), "url": r.get("url"),
                     "issues": audit_listing(r)})
    rows.sort(key=lambda x: (-len(x["issues"]), x["views"] if isinstance(x["views"], int) else 0))
    return rows


def audit_line(rows):
    """One line for the briefing: how many listings have something to fix."""
    if not rows:
        return None
    n = sum(1 for r in rows if r["issues"])
    if not n:
        return "Listing audit: every listing's title, tags and description pass."
    return (f"Listing audit: {n} of {len(rows)} listings have title or tag fixes "
            f"(store-report.py audit).")


def cmd_audit(args):
    snaps = snapshots()
    if not snaps:
        print("No store snapshot yet: store-report.py fetch", file=sys.stderr)
        return 2
    rows = audit(snaps[-1])
    if "--json" in args:
        print(json.dumps({"day": snaps[-1].get("day"), "listings": rows}, indent=1))
        return 0
    print(f"Listing audit, from the {snaps[-1].get('day')} snapshot. LIMIT is Etsy's rule; "
          f"ADVICE is what sellers measured.\n")
    for r in rows:
        views = "?" if r["views"] is None else r["views"]
        favs = "?" if r["favs"] is None else r["favs"]
        print(f"{short(r['title'], 70)}   ({views} views, {favs} fav)")
        if r["listing_id"]:
            print(f"  listing {r['listing_id']}")
        for kind, msg in r["issues"] or [("OK", "nothing to fix")]:
            print(f"  {kind:<6} {msg}")
        print()
    print(audit_line(rows))
    return 0


# ------------------------------------------------------------------ pinterest
#
# Pinterest boards are searched like Etsy is, so each is named for a phrase a
# shopper types. A listing goes to the first board whose words it carries,
# read from its title and tags; anything else falls to the catch-all for its
# product. The list lives here, once - rename a board here and every pin
# follows.
BOARDS = [
    ("Bookish Tote Bags & Library Gifts",
     "Tote bags for readers, library runs and book markets - vintage-inspired "
     "designs from VintageLoom Treasures.",
     r"\b(book|books|bookish|library|librarian|reading|reader)\b"),
    ("Coastal & Nautical Tote Bags",
     "Wave, sea and coastal pattern totes with a vintage seaside feel.",
     r"\b(wave|waves|coastal|seaside|ocean|sea|nautical|beach|tide)\b"),
    ("Vintage Botanical Tote Bags",
     "Botanical, floral and leaf-pattern tote bags inspired by antique "
     "herbarium plates.",
     r"\b(botanical|floral|flower|flowers|leaf|leaves|fern|wildflower|garden)\b"),
    ("Heritage Pattern & Map Tote Bags",
     "Old maps, town landmarks, compasses and heritage patterns on everyday "
     "canvas totes.",
     r"\b(map|landmark|town|city|compass|heritage|geometric)\b"),
]
TOTE_BOARD = ("Vintage-Inspired Tote Bags",
              "Every-day canvas totes with vintage-inspired all-over patterns.")
OTHER_BOARD = ("Vintage Stickers & Gifts",
               "Stickers, apparel and small gifts in a vintage style.")
SHOP_LINE = "VintageLoom Treasures on Etsy"


def board_for(row):
    """(board name, description) for one listing, by its own words."""
    text = " ".join([str(row.get("title") or "")] + list(row.get("tags") or [])).lower()
    if not re.search(r"\btote", text):
        return OTHER_BOARD
    for name, desc, words in BOARDS:
        if re.search(words, text):
            return name, desc
    return TOTE_BOARD


def first_sentence(text, limit=220):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    return t if len(t) <= limit else t[:limit - 1].rsplit(" ", 1)[0] + "…"


def pin_for(row):
    """{board, title, description, link} - Pinterest allows 100 characters of
    title and 500 of description, and both are cut here, not by Pinterest."""
    title = re.split(r"\s+\|\s+", str(row.get("title") or "").strip())[0][:100]
    lead = first_sentence(row.get("description")) or title
    if not re.search(r"[.!?…]$", lead):
        lead += "."
    tags = [t for t in (row.get("tags") or []) if t.lower() not in title.lower()][:6]
    desc = lead + (f" {', '.join(tags).capitalize()}." if tags else "") + f" {SHOP_LINE}."
    board, _ = board_for(row)
    return {"board": board, "title": title, "description": desc[:500], "link": row.get("url")}


def cmd_pins(_args):
    days = snapshots()
    if not days:
        print("No store snapshot yet: store-report.py fetch", file=sys.stderr)
        return 2
    rows = [r for r in days[-1].get("listings") or [] if r.get("url")]
    if rows and not any("tags" in r for r in rows):
        print("This snapshot was taken before pins were added - run "
              "store-report.py fetch once more, then pins.", file=sys.stderr)
        return 2
    used = {}
    for r in rows:
        pin = pin_for(r)
        used.setdefault(pin["board"], []).append(pin)
    print("BOARDS TO CREATE (name, then description):\n")
    for name, desc in [b[:2] for b in BOARDS] + [TOTE_BOARD, OTHER_BOARD]:
        if name in used:
            print(f"  {name}\n    {desc}\n")
    print("PINS (one per listing):")
    n = 0
    for board, pins in used.items():
        for pin in pins:
            n += 1
            print(f"\n#{n}  board: {board}\n    link: {pin['link']}"
                  f"\n    title: {pin['title']}\n    description: {pin['description']}")
    return 0


def cmd_show(_args):
    print("\n".join(lines(latest_summary())))
    return 0


def main():
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    fn = {"setup": cmd_setup, "fetch": cmd_fetch, "show": cmd_show,
          "pins": cmd_pins, "audit": cmd_audit}.get(cmd)
    if not fn:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
