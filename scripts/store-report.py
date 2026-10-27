#!/usr/bin/env python3
"""
store-report.py — how the shop's own listings are doing, from Etsy.

    store-report.py setup VintageLoomTreasures   find the shop's id, once
    store-report.py fetch                        snapshot every active listing today
    store-report.py show                         the report: today against the last snapshot
    store-report.py pins                         a Pinterest pin for every listing, and its board
    store-report.py audit                        what to fix in each listing's title, tags and description,
                                                 and what to write instead
    store-report.py check '{"listing_id": ...}'  the audit of a rewrite, as JSON (the Fix listings box)

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

import html
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


def plain(text):
    """Etsy's API hands text back HTML-escaped: "Dad&#39;s Boss T-Shirt",
    "&quot;It&#39;s Fine&quot;" (2026-10-12, the Deck and the audit both
    showing it). The shop shows an apostrophe; so does everything here."""
    return html.unescape(str(text or ""))


def snapshot_row(listing):
    """One listing, reduced to what the report reads."""
    ms = _ms()
    return {
        "listing_id": listing.get("listing_id"),
        "title": plain(listing.get("title"))[:140],
        "views": listing.get("views") if isinstance(listing.get("views"), int) else None,
        "favs": listing.get("num_favorers") if isinstance(listing.get("num_favorers"), int) else None,
        "price": ms.price_of(listing),
        "age_days": round(ms.age_days(listing), 1) if ms.age_days(listing) is not None else None,
        "url": listing.get("url"),
        # Kept for the pins: a pin is written from the listing's own words.
        "tags": [plain(t) for t in (listing.get("tags") or []) if str(t).strip()][:13],
        "description": plain(listing.get("description"))[:600],
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
            # Days saved before plain() existed carry the escapes.
            for r in d["listings"]:
                if isinstance(r, dict):
                    for k in ("title", "description"):
                        if isinstance(r.get(k), str):
                            r[k] = plain(r[k])
                    if isinstance(r.get("tags"), list):
                        r["tags"] = [plain(t) for t in r["tags"]]
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
# ADVICE: a title shorter than what search shows wastes the part people see.
# It was 60, which flagged 54-59-character titles as short (2026-10-12) -
# while Etsy's own advice is now shorter, plainer titles.
TITLE_SHORT = TITLE_SEEN
ONE_WORD_TAGS_MAX = 3     # ADVICE: shoppers buy on long phrases
# ADVICE: title and tags say different things only when most tags miss the
# title. Two or three that do are how tags are meant to work - a synonym, a
# style, an occasion. Flagging any one of them made 26 of 27 listings "need
# fixes" (2026-10-12), nearly all for "gift for her" and "eco friendly".
STRAY_SHARE = 0.5
# Who it is for and when it is given: searches the title is not meant to
# carry, so never counted as a tag straying from it.
OCCASION_RE = re.compile(
    r"\b(gifts?|lovers?|owners?|for (?:her|him|them|mom|dad|teens?|kids|men|women|coworkers?|"
    r"hikers?|teachers?|friends?)|birthday|christmas|xmas|halloween|thanksgiving|valentines?|"
    r"easter|wedding|bridal|shower|anniversary|graduation|retirement|mothers?|fathers?|"
    r"mom|dad|teacher)\b", re.I)
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
    stray = [t for t in tags if not ({_stem(w) for w in _keywords(t)} & title_words)
             and not OCCASION_RE.search(t)]
    if tags and len(stray) > STRAY_SHARE * len(tags):
        out.append(("ADVICE", f"{len(stray)} of {len(tags)} tags share no word with the title "
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


# ------------------------------------------------------- write this instead
#
# The audit says what is wrong; the Shop page's Fix listings tab also offers
# what to write instead, in a box the owner edits and copies into Etsy
# (owner, 2026-10-27: "give me a text box to fix it ... Write this instead").
# Built from the listing's own title and tags - no model, so nothing is
# claimed that the listing does not already say - and audited again: a
# suggestion is offered only when it leaves fewer issues than there are now.

TITLE_FILL = 60           # a short title is filled with tag phrases to about here
# Words a search phrase does not start or end on ("you frog sticker").
_EDGE = _STOP | {"you", "i", "me", "we", "us", "its", "it's", "i'm", "im", "so", "just", "very"}
SEPARATORS = re.compile(r"\s*[|,;:–—]\s*|\s+-\s+")


def _cap(phrase):
    """Title case, leaving words that already carry capitals (POV, iPad) alone."""
    return " ".join(w if any(c.isupper() for c in w[1:]) else w[:1].upper() + w[1:]
                    for w in str(phrase).split())


def _phrases(text, longest=TAG_MAX):
    """Two- and three-word search phrases from text, in reading order, within
    one part of a title (never across a | or a comma), no stop word at
    either end, short enough to be a tag."""
    out = []
    for part in SEPARATORS.split(str(text or "")):
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*[A-Za-z0-9]|[A-Za-z0-9]", part)
        for n in (3, 2):
            for i in range(len(words) - n + 1):
                chunk = [w.lower() for w in words[i:i + n]]
                if chunk[0] in _EDGE or chunk[-1] in _EDGE:
                    continue
                p = " ".join(chunk)
                if len(p) <= longest and p not in out:
                    out.append(p)
    return out


def _stray(tag, title):
    return not ({_stem(w) for w in _keywords(tag)} & {_stem(w) for w in _keywords(title)}) \
        and not OCCASION_RE.search(tag)


def suggest_tags(title, tags):
    """The listing's tags, fixed: none too long, none twice, one that search
    shows in the title's first 40 characters, and every empty slot filled
    with a phrase from the title."""
    keep, seen = [], set()
    for t in tags or []:
        t = " ".join(str(t).split())
        words = t.split()
        while words and len(" ".join(words)) > TAG_MAX:
            words.pop()
        t = " ".join(words)
        k = _tag_key(t)
        if t and k not in seen:
            seen.add(k)
            keep.append(t)
    shown = title[:TITLE_SEEN].lower()
    if not any(t.lower() in shown for t in keep):
        lead = next((p for p in _phrases(title[:TITLE_SEEN]) if p in shown
                     and _tag_key(p) not in seen), None)
        if lead:
            if len(keep) >= TAG_SLOTS:
                # Out goes a tag that says nothing the title says; failing
                # that, the last one.
                out = next((t for t in reversed(keep) if _stray(t, title)), keep[-1])
                keep.remove(out)
                seen.discard(_tag_key(out))
            keep.insert(0, lead)
            seen.add(_tag_key(lead))
    for p in _phrases(title):
        if len(keep) >= TAG_SLOTS:
            break
        # "coffee mug" says nothing "mom coffee mug" did not.
        inside = any(set(_words(p)) <= set(_words(k)) for k in keep)
        if _tag_key(p) not in seen and not inside:
            seen.add(_tag_key(p))
            keep.append(p)
    return keep[:TAG_SLOTS]


def suggest_title(title, tags):
    """A short title, or one whose tags say other things, lengthened with the
    tag phrases it does not say yet. A title that is neither is left alone."""
    head = " ".join(str(title or "").split())
    stray = [t for t in tags if _stray(t, head)]
    if len(head) >= TITLE_SHORT and len(stray) <= STRAY_SHARE * max(len(tags), 1):
        return head
    goal = TITLE_FILL if len(head) < TITLE_SHORT else min(TITLE_MAX, len(head) + 45)
    added = 0
    for t in sorted(tags, key=lambda t: not _stray(t, head)):
        if len(head) >= goal:
            break
        new = {_stem(w) for w in _keywords(t)} - {_stem(w) for w in _keywords(head)}
        if not new or t.lower() in head.lower():
            continue
        cand = head + (", " if added else " | ") + _cap(t)
        # Never a word three times - the audit's own "repeats" rule.
        if len(cand) <= TITLE_MAX and max(_words(cand).count(w) for w in _keywords(cand)) < 3:
            head, added = cand, added + 1
    return head


def suggest(r):
    """{"title", "tags", "opening", "left"} for one snapshot row - each None
    when the listing's own is as good - or None when nothing would improve.
    "left" is what the audit would still say with every suggestion taken."""
    title = str(r.get("title") or "")
    tags = [str(t).strip() for t in (r.get("tags") or []) if str(t).strip()]
    desc = str(r.get("description") or "")
    now = audit_listing(r)
    if not now:
        return None
    new_tags = suggest_tags(title, tags)
    new_title = suggest_title(title, new_tags)
    opening = None
    if desc and not ({_stem(w) for w in _keywords(desc[:DESCRIPTION_OPENING])}
                     & {_stem(w) for w in _keywords(new_title)}):
        opening = _cap(SEPARATORS.split(new_title)[0]) + "."
    left = audit_listing({"title": new_title, "tags": new_tags,
                          "description": (opening + " " + desc) if opening else desc})
    # Better is fewer issues - or as many, with more of the 13 slots used: 7
    # tags where there were none still leaves "uses 7 of 13", and is better.
    score = lambda issues, t: len(issues) + (TAG_SLOTS - min(len(t), TAG_SLOTS)) / TAG_SLOTS
    if score(left, new_tags) >= score(now, tags):
        return None
    return {"title": new_title if new_title != title else None,
            "tags": new_tags if new_tags != tags else None,
            "opening": opening,
            "left": [list(x) for x in left]}


def check(row, title=None, tags=None, opening=None):
    """What the audit says about a listing as the owner is rewriting it."""
    desc = str(row.get("description") or "")
    return audit_listing({"title": row.get("title") if title is None else title,
                          "tags": row.get("tags") if tags is None else tags,
                          "description": (opening.strip() + " " + desc) if opening else desc})


def audit(latest):
    """Every listing in a snapshot with what to fix, most to fix first."""
    rows = []
    for r in (latest or {}).get("listings") or []:
        rows.append({"listing_id": r.get("listing_id"), "title": r.get("title") or "?",
                     "views": r.get("views"), "favs": r.get("favs"), "url": r.get("url"),
                     "tags": r.get("tags") or [],
                     "issues": audit_listing(r), "suggest": suggest(r)})
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
        sg = r.get("suggest")
        if sg and sg["title"]:
            print(f"  WRITE  title: {sg['title']}")
        if sg and sg["tags"]:
            print(f"  WRITE  tags:  {', '.join(sg['tags'])}")
        if sg and sg["opening"]:
            print(f"  WRITE  start the description with: {sg['opening']}")
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


def cmd_check(args):
    """store-report.py check '{"listing_id": .., "title": .., "tags": [..], "opening": ..}'
    - the audit of a listing as the owner is rewriting it, as JSON. The Fix
    listings box asks this as they type, so its verdict is the audit's own."""
    try:
        want = json.loads(args[0])
        lid = int(want["listing_id"])
    except (IndexError, ValueError, KeyError, TypeError):
        print("usage: store-report.py check '{\"listing_id\": 123, \"title\": \"...\"}'",
              file=sys.stderr)
        return 2
    snaps = snapshots()
    row = next((r for r in (snaps[-1].get("listings") if snaps else []) or []
                if r.get("listing_id") == lid), None)
    if row is None:
        print(f"listing {lid} is not in the latest snapshot", file=sys.stderr)
        return 1
    tags = want.get("tags")
    if tags is not None:
        tags = [" ".join(str(t).split()) for t in tags if str(t).strip()]
    title = want.get("title")
    issues = check(row, None if title is None else " ".join(str(title).split()),
                   tags, want.get("opening") or None)
    print(json.dumps({"listing_id": lid, "issues": [list(x) for x in issues]}))
    return 0


def main():
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    fn = {"setup": cmd_setup, "fetch": cmd_fetch, "show": cmd_show,
          "pins": cmd_pins, "audit": cmd_audit, "check": cmd_check}.get(cmd)
    if not fn:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
