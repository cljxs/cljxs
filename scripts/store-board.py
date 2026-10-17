#!/usr/bin/env python3
"""
store-board.py — every build Emily made, what it would actually earn, and
what is wrong with it, so each can be approved, sent back or killed.

    store-board.py show [--json]            the board
    store-board.py approve <build> [--undo] you have looked at it and it can go up
    store-board.py revise <build> [--note "less text, bigger fern"] [--keep-art]

Killing a build is emily-build.py remove, which already owns what removing
one means (archive, never delete, and refuse while a Printify product points
at it). The Deck's Kill button calls that, not anything here.

THE MONEY. Per sale, from the one fee table in emily-printify.py (fees_of,
net_of): the price, what Etsy takes, what Printify charges to make it and
post it, and what is left. Prices are the catalogue's per-variant prices
when they are saved; otherwise the live range `emily-printify.py status`
read back from Printify. Nothing is worked out in the page - it prints what
this hands it.

THE CONCERNS. Worst first:
    LOSS     a variant that loses money on every sale
    FIX      something that stops it selling or printing right: no cost
             recorded, the price still the placeholder, the art missing or
             the placeholder, a listing Etsy would refuse (store-report.py's
             own audit, so the board and `store-report.py audit` agree)
    CHECK    worth a look: a thin margin, art that may print soft, the
             audit's advice
A build with none is GOOD.

APPROVE marks it on build.json. It does not publish: nothing here can - the
Publish button in Printify is the owner's, on purpose (emily-printify.py has
no publish command either). The board then shows the Printify link.

REVISE sends it back: --keep-art drafts the same art again through today's
placement (free); without it the art is drawn again (one image call), with
your note added to the brief it is drawn from. It is emily-finish.py's
--redraft / --redo, which refuse to touch anything live on Etsy.

Standard library only.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
BUILDS = ROOT / "agents" / "emily" / "builds"

THIN_MARGIN = 0.20          # ADVICE: under a fifth of the price is little room for a coupon
PRINT_DPI = 300             # Printify's print areas are measured at 300 DPI
SOFT_DPI = 150              # ADVICE: below this a print starts to look soft
MIN_ART_SIDE = 1000         # without a measured area, smaller than this is soft anywhere
NOTE_MAX = 300
NOTE_MARK = "\n\nOwner's note: "
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
RANK = {"LOSS": 0, "FIX": 1, "CHECK": 2}


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MODS = {}


def _mod(key, file):
    if key not in _MODS:
        _MODS[key] = _load(key, file)
    return _MODS[key]


def _ep():
    return _mod("emily_printify_board", "emily-printify.py")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _json(path):
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def money(x):
    return None if x is None else round(float(x), 2)


# ------------------------------------------------------------------ the money

def breakdown(price, cost, fees):
    """One sale, in parts that add up: price + shipping charged = etsy +
    making + postage + kept. `kept` is net_of(), the one formula; Etsy's
    share is what is left of the revenue after the rest, so the parts cannot
    disagree with it."""
    ep = _ep()
    kept = ep.net_of(price, cost, fees, fees["ship_cost"], fees["ship_charged"])
    revenue = price + fees["ship_charged"]
    etsy = revenue - kept - cost - fees["ship_cost"]
    return {"price": money(price), "ship_charged": money(fees["ship_charged"]),
            "etsy": money(etsy), "making": money(cost), "postage": money(fees["ship_cost"]),
            "kept": money(kept), "margin": round(kept / price, 3) if price else None}


def variant_rows(entry, build):
    """[(label, price, cost)] to work the money out on.

    Per variant where the catalogue has both its price and its cost. Where
    prices were never saved there (the owner sets them in Printify), the
    live range from `emily-printify.py status`: the low price against the
    cheapest variant to make, the high against the dearest. Returns
    (rows, source) - source says which, for the board to print."""
    ep = _ep()
    costs = ep.costs_of(entry or {})
    prices = {str(k): v for k, v in ((entry or {}).get("prices") or {}).items()
              if isinstance(v, (int, float))}
    ids = [str(v) for v in (entry or {}).get("variant_ids") or []]
    titles = (entry or {}).get("variant_titles") or []
    rows = []
    for i, vid in enumerate(ids):
        if vid in prices and vid in costs:
            rows.append((titles[i] if i < len(titles) else vid, prices[vid] / 100, costs[vid]))
    if rows:
        return rows, "catalogue"
    lo, hi = build.get("price_low"), build.get("price_high")
    if costs and isinstance(lo, (int, float)):
        c = sorted(costs.values())
        rows = [("lowest price", float(lo), c[0])]
        if isinstance(hi, (int, float)) and (hi != lo or c[-1] != c[0]):
            rows.append(("highest price", float(hi), c[-1]))
        return rows, "printify"
    return [], None


def economics(entry, build, fees):
    """The money for one build: the worst and best sale, or why there is none."""
    rows, source = variant_rows(entry, build)
    if not rows:
        return {"source": None, "variants": 0}
    worked = [dict(breakdown(p, c, fees), label=str(label)[:40]) for label, p, c in rows]
    worked.sort(key=lambda r: r["kept"])
    return {"source": source, "variants": len(worked), "worst": worked[0],
            "best": worked[-1], "losing": [r["label"] for r in worked if r["kept"] < 0]}


# ------------------------------------------------------------------ the concerns

def art_concerns(d, entry):
    """[(level, message)] about design.png: missing, the placeholder, or too
    small for what it prints on."""
    design = Path(d) / "design.png"
    if not design.is_file():
        return [("FIX", "no design.png - there is no artwork")]
    if _mod("emily_assets_board", "emily-assets.py").is_placeholder(design):
        return [("FIX", "the art is the placeholder - the real drawing failed")]
    try:
        w, h = _mod("knockout_board", "knockout.py").size(design)
    except Exception:
        return [("FIX", "design.png is not a readable PNG")]
    sizes = [tuple(v) for v in ((entry or {}).get("print_sizes") or {}).values()
             if isinstance(v, (list, tuple)) and len(v) == 2 and min(v) > 0]
    if sizes:
        aw, ah = max(sizes)
        dpi = round(PRINT_DPI * min(w / aw, h / ah))
        if dpi < SOFT_DPI:
            return [("CHECK", f"art is {w}x{h} px - about {dpi} DPI across a "
                              f"{aw / PRINT_DPI:.0f}x{ah / PRINT_DPI:.0f} in print area; "
                              f"under {SOFT_DPI} prints soft")]
    elif max(w, h) < MIN_ART_SIDE:
        return [("CHECK", f"art is only {w}x{h} px - small for any print")]
    return []


def money_concerns(econ, entry, key, fees, product):
    out = []
    if not entry:
        return [("FIX", f"no catalogue entry for '{product}' - it cannot be priced or drafted")]
    ep = _ep()
    if not ep.costs_of(entry):
        out.append(("FIX", f"production cost not recorded - emily-printify.py costs --product {key}"))
    if not econ.get("source"):
        if ep.costs_of(entry):
            out.append(("FIX", "no price yet - save prices, or set them in Printify and run "
                               "emily-printify.py status"))
        return out
    worst = econ["worst"]
    if econ.get("losing"):
        out.append(("LOSS", f"loses ${-worst['kept']:.2f} a sale on {worst['label']} "
                            f"(${worst['price']:.2f}; at that cost it breaks even at "
                            f"${ep.floor_price(worst['making'], fees, fees['ship_cost'], fees['ship_charged']):.2f})"))
    elif worst["margin"] is not None and worst["margin"] < THIN_MARGIN:
        out.append(("CHECK", f"thin: keeps ${worst['kept']:.2f} of ${worst['price']:.2f} "
                             f"({worst['margin'] * 100:.0f}%) on {worst['label']}"))
    placeholder = ep.PLACEHOLDER_CENTS / 100
    if worst["price"] == placeholder:
        out.append(("FIX", f"priced at the ${placeholder:.2f} placeholder - set the real price"))
    return out


def listing_concerns(listing, read_error):
    if read_error:
        return [("FIX", f"listing.json will not parse ({read_error})")]
    if not listing:
        return [("CHECK", "no listing.json yet - no title, tags or description")]
    row = {"title": listing.get("title"), "tags": listing.get("tags") or [],
           "description": listing.get("description")}
    level = {"LIMIT": "FIX", "ADVICE": "CHECK"}
    return [(level.get(k, "CHECK"), m) for k, m in
            _mod("store_report_board", "store-report.py").audit_listing(row)]


def verdict(concerns):
    if not concerns:
        return "GOOD"
    return min((c[0] for c in concerns), key=lambda k: RANK.get(k, 9))


# ------------------------------------------------------------------ the board

def card(d, cat, fees):
    """Everything the board shows about one build."""
    build = _json(d / "build.json")
    order = _json(d / "order.json")
    read_error = None
    try:
        listing = json.loads((d / "listing.json").read_text())
        listing = listing if isinstance(listing, dict) else {}
    except FileNotFoundError:
        listing = {}
    except Exception as exc:
        listing, read_error = {}, f"{type(exc).__name__}"
    product = (listing.get("product_type") or order.get("product") or build.get("product") or "")
    ep = _ep()
    try:
        key, entry = ep.resolve(cat, product) if product else (None, None)
        ambiguous = None
    except ep.Ambiguous as exc:
        key, entry, ambiguous = None, None, exc.keys
    econ = economics(entry, build, fees)
    concerns = []
    if ambiguous:
        concerns.append(("FIX", f"'{product}' matches {', '.join(ambiguous)} - name one in listing.json"))
    else:
        concerns += money_concerns(econ, entry, key, fees, product or "?")
    concerns += art_concerns(d, entry)
    concerns += listing_concerns(listing, read_error)
    if isinstance(build.get("draft_blocked"), dict):
        why = build["draft_blocked"].get("reason") or ""
        concerns.append(("FIX", f"no Printify draft: {str(why)[:200]}"))
    concerns.sort(key=lambda c: RANK.get(c[0], 9))
    rev = build.get("revision") if isinstance(build.get("revision"), dict) else None
    try:
        art_v = int((d / "design.png").stat().st_mtime * 1000)
    except OSError:
        art_v = None
    return {
        "slug": d.name,
        "title": listing.get("title") or order.get("name") or build.get("idea") or d.name,
        "product": product or None, "catalogue": key,
        "status": build.get("status") or "no_record",
        "published": build.get("published") is True,
        "approved_utc": build.get("approved_utc"),
        "printify_url": build.get("printify_url"), "etsy_url": build.get("etsy_url"),
        "tags": len(listing.get("tags") or []),
        "money": econ,
        "concerns": [{"level": k, "text": m} for k, m in concerns],
        "verdict": "LIVE" if build.get("published") is True else verdict(concerns),
        "revision": rev,
        # The design's timestamp, for the page's image URL (?v=): a cached
        # copy is then always the current art, and a refresh re-downloads
        # nothing - the 13 MB-every-20-seconds lesson in emily.js.
        "art_v": art_v,
    }


def board():
    ep = _ep()
    cat = ep.read_catalog()
    fees = ep.fees_of(cat)
    cards = []
    for d in sorted(BUILDS.iterdir()) if BUILDS.is_dir() else []:
        if d.is_dir() and SLUG_RE.match(d.name):
            cards.append(card(d, cat, fees))
    order = {"LOSS": 0, "FIX": 1, "CHECK": 2, "GOOD": 3, "LIVE": 4}
    cards.sort(key=lambda c: (order.get(c["verdict"], 9), c["slug"]))
    notes = []
    if not fees["ship_cost"]:
        notes.append("Postage is recorded as $0, so every number here assumes Printify "
                     "posts it free. It does not: emily-printify.py fees --set ship_cost=4.59")
    counts = {}
    for c in cards:
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
    return {"cards": cards, "counts": counts, "fees": fees, "notes": notes,
            "approved": sum(1 for c in cards if c["approved_utc"] and not c["published"])}


# ------------------------------------------------------------------ actions

def find(slug):
    if not SLUG_RE.match(slug or ""):
        raise ValueError(f"{slug!r} is not a build name")
    d = BUILDS / slug
    if not d.is_dir():
        raise FileNotFoundError(f"no build called {slug} - store-board.py show")
    return d


def write_build(d, build):
    tmp = d / "build.json.tmp"
    tmp.write_text(json.dumps(build, indent=1) + "\n")
    tmp.replace(d / "build.json")


def approve(slug, undo=False):
    d = find(slug)
    path = d / "build.json"
    build = _json(path)
    if path.exists() and not build:
        raise ValueError("build.json will not parse - not overwriting it")
    if undo:
        build.pop("approved_utc", None)
    else:
        build["approved_utc"] = _now()
    write_build(d, build)
    return build


def with_note(brief, note):
    """The brief with the owner's note on the end - replacing an earlier
    note, so three revisions do not stack three notes."""
    base = str(brief or "").split(NOTE_MARK)[0].rstrip()
    note = re.sub(r"\s+", " ", str(note or "")).strip()[:NOTE_MAX]
    return f"{base}{NOTE_MARK}{note}" if note else base


def revise(slug, note=None, keep_art=False, runner=None):
    """Send a build back. Records the request on build.json first, so the
    board says 'revising' while it runs, then the outcome when it ends."""
    d = find(slug)
    build = _json(d / "build.json")
    if build.get("published") is True:
        raise ValueError(f"{slug} is live on Etsy - not touching it")
    if note:
        order = _json(d / "order.json")
        listing = _json(d / "listing.json")
        if not order:
            order = {"product": listing.get("product_type") or build.get("product") or "",
                     "name": build.get("idea") or listing.get("title") or slug,
                     "brief": str(listing.get("description") or "")[:600]}
        order["brief"] = with_note(order.get("brief"), note)
        (d / "order.json").write_text(json.dumps(order, indent=1) + "\n")
    build["revision"] = {"requested_utc": _now(), "note": (note or "")[:NOTE_MAX] or None,
                         "keep_art": bool(keep_art), "finished_utc": None, "ok": None,
                         "said": None}
    build.pop("approved_utc", None)
    write_build(d, build)
    cmd = [sys.executable, str(SCRIPTS / "emily-finish.py"),
           "--redraft" if keep_art else "--redo", slug]
    r = (runner or (lambda c: subprocess.run(c, capture_output=True, text=True, timeout=1500)))(cmd)
    text = ((r.stdout or "") + (r.stderr or "")).strip()
    (d / "revision.log").write_text(text + "\n")
    build = _json(d / "build.json")          # emily-finish rewrote it
    rev = build.get("revision") if isinstance(build.get("revision"), dict) else {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # emily-finish exits 0 when the draft is refused - it records why on the
    # build instead - so the outcome is read off the build, not the exit code.
    blocked = build.get("draft_blocked") if isinstance(build.get("draft_blocked"), dict) else None
    ok = r.returncode == 0 and not blocked
    said = (blocked or {}).get("reason") or (lines[-1] if lines else "")
    rev.update({"finished_utc": _now(), "ok": ok, "said": str(said)[:300]})
    build["revision"] = rev
    write_build(d, build)
    return ok, text


# ------------------------------------------------------------------ commands

def cmd_show(a):
    b = board()
    if a.json:
        print(json.dumps(b))
        return 0
    if not b["cards"]:
        print("no builds yet")
        return 0
    for c in b["cards"]:
        m = c["money"]
        w = m.get("worst") or {}
        cash = ("not priced" if not m.get("source")
                else f"loses ${-w['kept']:.2f} on ${w['price']:.2f}" if w["kept"] < 0
                else f"keeps ${w['kept']:.2f} of ${w['price']:.2f}")
        print(f"{c['verdict']:<6}{c['slug']:<40}{cash}"
              + ("   APPROVED" if c["approved_utc"] and not c["published"] else ""))
        for x in c["concerns"]:
            print(f"        {x['level']:<6}{x['text']}")
    for n in b["notes"]:
        print(f"\n{n}")
    return 0


def cmd_approve(a):
    try:
        approve(a.build, a.undo)
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{a.build}: {'approval taken back' if a.undo else 'approved'}"
          + ("" if a.undo else " - publish it from Printify when ready"))
    return 0


def cmd_revise(a):
    try:
        ok, text = revise(a.build, a.note, a.keep_art)
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(text)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="the board")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)
    p = sub.add_parser("approve", help="mark a build approved")
    p.add_argument("build")
    p.add_argument("--undo", action="store_true")
    p.set_defaults(fn=cmd_approve)
    r = sub.add_parser("revise", help="send a build back")
    r.add_argument("build")
    r.add_argument("--note", help="what to change - added to the art's brief")
    r.add_argument("--keep-art", action="store_true", help="redraft the same art (free)")
    r.set_defaults(fn=cmd_revise)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
