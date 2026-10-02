#!/usr/bin/env python3
"""
niche-scan.py - keep Scout's market data fresh, and find new niches, every
morning, with no model involved.

    python3 scripts/niche-scan.py run            scan the most out-of-date niches (the timer)
    python3 scripts/niche-scan.py run --n 2      fewer
    python3 scripts/niche-scan.py list           every niche, how old its scan is, what it sold
    python3 scripts/niche-scan.py blanks         selling blanks Emily lacks, and the closest Printify product

WHY (2026-10-02). The owner wants Scout working on "a well selling niche",
with no preference which. Two things stood in the way:

  * Scans only happened when somebody typed market-scan.py by hand, and
    Scout may only propose from a scan under 14 days old - so on 2026-09-30
    Scout had nothing to propose and said so. Nothing was keeping the data
    fresh.
  * Nobody chose WHICH niches to measure. A person had to think of a seed.

So: a starter list of evergreen niches across the products Emily can make,
scanned oldest-first a few a day so none goes stale; and every scan's best
SELLING phrases contribute the tags their top listings use, which become
new niches to measure. The data decides what sells; nothing here is a
guess about taste.

WHAT IT DOES NOT DO. It never reads a listing's images or copies a title -
only the counts market-scan.py measures and the tags sellers label their
work with. A niche ("dog mom mug") is not a design. Emily's designs are her
own; this only says where people are buying.

Budget: one scan is 27 Google Suggest queries, 12 Etsy searches and up to
50 Etsy review counts. RUN_N scans a day is a few hundred Etsy calls,
against a daily allowance in the thousands.

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
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
STATE = ROOT / "agents" / "scout" / "state"
NICHES = STATE / "niches.json"
SCANS = STATE / "scans"
# THE SIGNAL (owner, 2026-10-02): when the sellers who sell print on a blank
# Emily is not set up for, say so, and say the closest thing Printify has.
GAPS = STATE / "blank-gaps.json"
BLUEPRINTS = ROOT / "agents" / "emily" / "state" / "printify-blueprints.json"
BLUEPRINTS_DAYS = 7      # Printify's catalogue changes slowly; one read a week

RUN_N = 6                 # scans a morning: MAX_NICHES at 6/day is ~13 days, inside Scout's 14
FRESH_DAYS = 7            # a niche scanned this recently is not re-scanned
MAX_NICHES = 80           # the list stops growing here; old ones keep refreshing
DISCOVER_PER_RUN = 4      # new niches added from tags per run, at most

# Evergreen, gift-driven niches on products Emily can make (emily-printify's
# product words). Starting points, not conclusions: each is measured, and a
# niche nobody is buying in drops to the bottom of Scout's list by itself.
STARTER = [
    "water bottle sticker", "laptop sticker", "funny sticker", "mental health sticker",
    "bookish sticker", "frog sticker", "mushroom sticker", "cat sticker",
    "dog mom mug", "cat lover mug", "teacher gift mug", "nurse gift mug",
    "coffee lover mug", "funny coffee mug", "book lover mug",
    "book lover tote", "plant lady tote", "teacher tote bag", "library tote bag",
    "dog mom sweatshirt", "cat mom shirt", "funny dad shirt", "hiking shirt",
    "fishing shirt", "gardening shirt", "bookish sweatshirt", "nurse shirt",
    "botanical print", "minimalist mountain print", "bird lover gift",
    "camping mug", "retro sunset sticker",
]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def log(msg):
    print(f"[niche-scan {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def slug(phrase):
    """market-scan.py's file name for a seed - one rule, imported."""
    return _load("market_scan_slug", "market-scan.py").slug(phrase)


def load():
    try:
        d = json.loads(NICHES.read_text())
        if isinstance(d.get("niches"), dict):
            return d
    except Exception:
        pass
    return {"niches": {}}


def save(d):
    NICHES.parent.mkdir(parents=True, exist_ok=True)
    tmp = NICHES.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1) + "\n")
    tmp.replace(NICHES)


def blocked(phrase):
    """Somebody else's property - ip-check.py's list, the one Scout's gate uses."""
    tier, _what, _why = _load("ip_check_n", "ip-check.py").risky(phrase)
    return tier == "blocked"


def scan_of(seed):
    """(scanned_at datetime or None, doc or None) - the scan file is the record
    of when a niche was measured, so there is no second date to drift."""
    try:
        doc = json.loads((SCANS / f"{slug(seed)}.json").read_text())
        when = datetime.strptime(doc["scanned_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return when, doc
    except Exception:
        return None, None


def niches(d=None, now=None):
    """[(seed, source, age_days or None)], oldest first, never-scanned first."""
    d = d or load()
    now = now or datetime.now(timezone.utc)
    seeds = dict.fromkeys(STARTER, "starter")
    seeds.update({k: v.get("source", "found") for k, v in d["niches"].items()})
    out = []
    for seed, source in seeds.items():
        when, _ = scan_of(seed)
        out.append((seed, source, None if when is None else (now - when).total_seconds() / 86400))
    out.sort(key=lambda x: (x[2] is not None, -(x[2] or 0)))
    return out


def due(d=None, n=RUN_N, now=None, force=False):
    """The niches to scan now, oldest first. force: scanned this week too -
    for when the measuring itself changed and the recent scans are wrong."""
    return [s for s, _src, age in niches(d, now) if (force or age is None or age >= FRESH_DAYS)
            and not blocked(s)][:n]


def product_of(seed):
    try:
        return _load("emily_printify_n", "emily-printify.py").product_word(seed)
    except Exception:
        return None


def same(phrase):
    """One niche however it is pluralised: 'cat stickers' is 'cat sticker'
    (the first droplet run added both)."""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
                    for w in str(phrase or "").lower().split())


def discover(d, now=None):
    """New niches from the tags of phrases that are SELLING: the words sellers
    who are making sales use for their own work. A tag with no product word
    takes its scan's product ("dog mom gift" in a mug scan -> "dog mom mug")."""
    known = {same(s) for s, _src, _age in niches(d, now)}
    added = []
    for f in sorted(SCANS.glob("*.json")) if SCANS.is_dir() else []:
        try:
            doc = json.loads(f.read_text())
        except Exception:
            continue
        product = product_of(doc.get("seed") or "")
        for row in doc.get("rows") or []:
            if not row.get("selling") or row.get("selling") * 2 < (row.get("sales_n") or 0):
                continue                       # fewer than half its top listings sold
            for tag in row.get("tags") or []:
                tag = re.sub(r"\s+", " ", str(tag).lower()).strip()
                if not re.fullmatch(r"[a-z][a-z ']{2,40}", tag) or not 2 <= len(tag.split()) <= 4:
                    continue
                seed = tag if product_of(tag) else (f"{tag.removesuffix(' gift').strip()} {product}"
                                                    if product else None)
                if not seed or same(seed) in known or blocked(seed):
                    continue
                if len(known) >= MAX_NICHES or len(added) >= DISCOVER_PER_RUN:
                    return added
                known.add(same(seed))
                d["niches"][seed] = {"source": f"tag on {row.get('phrase')}",
                                     "added": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")}
                added.append(seed)
    return added


def scan(seed):
    """market-scan.py scan <seed> --save, in its own process: its output is
    the journal's record, and one niche failing never stops the rest."""
    r = subprocess.run([sys.executable, str(SCRIPTS / "market-scan.py"), "scan", *seed.split(), "--save"],
                       capture_output=True, text=True, timeout=900)
    tail = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
    return r.returncode, tail


def cmd_run(a):
    d = load()
    todo = due(d, a.n, force=a.force)
    if not todo:
        log("every niche was scanned within the last week - nothing due")
    for seed in todo:
        log(f"scanning '{seed}'")
        code, tail = scan(seed)
        for line in [l for l in tail if l.strip().startswith(("sales:", "saved"))] or tail[-3:]:
            log(f"  {line.strip()}")
        if code:
            log(f"  '{seed}' FAILED (exit {code})")
    added = discover(d)
    for seed in added:
        log(f"new niche from what sells: '{seed}' ({d['niches'][seed]['source']})")
    save(d)
    for g in gaps():
        for line in gap_lines(g):
            log(line)
    return 0


def printify():
    return _load("emily_printify_gap", "emily-printify.py")


def blueprints(now=None):
    """Printify's whole catalogue (id, title, brand, model), cached for a week.
    [] when Printify cannot be asked - the warning still says what is
    missing, it just cannot name the nearest product."""
    now = now or datetime.now(timezone.utc)
    try:
        d = json.loads(BLUEPRINTS.read_text())
        age = (now - datetime.strptime(d["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)).total_seconds() / 86400
        if age < BLUEPRINTS_DAYS:
            return d["blueprints"]
    except Exception:
        pass
    try:
        raw = printify().call("/catalog/blueprints.json")
    except (Exception, SystemExit) as exc:
        log(f"Printify catalogue not read ({type(exc).__name__}) - warnings will not name the closest product")
        return []
    rows = [{k: b.get(k) for k in ("id", "title", "brand", "model")} for b in raw or [] if isinstance(b, dict)]
    BLUEPRINTS.parent.mkdir(parents=True, exist_ok=True)
    BLUEPRINTS.write_text(json.dumps({"fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                      "blueprints": rows}, indent=1) + "\n")
    return rows


def catalogue():
    """Emily's chosen products as [{key, blueprint_id, blueprint_title, brand, model}]."""
    try:
        p = printify()
        return [dict(e, key=k) for k, e in p.products(p.read_catalog())]
    except (Exception, SystemExit):
        return []


def gaps(bps=None):
    """[warning] - one per scanned phrase whose selling listings name a blank
    Emily does not have, with the closest Printify products. Written to
    blank-gaps.json for the Deck and Emily, and logged."""
    bl = _load("blanks_gap", "blanks.py")
    have = catalogue()
    out, seen = [], set()
    for f in sorted(SCANS.glob("*.json")) if SCANS.is_dir() else []:
        try:
            doc = json.loads(f.read_text())
        except Exception:
            continue
        for row in doc.get("rows") or []:
            top = row.get("blanks") or []
            kind = bl.family(product_of(row.get("phrase") or doc.get("seed") or ""))
            g = bl.gap(top, kind, have)
            if not g or (g["blank"], kind) in seen:
                continue
            seen.add((g["blank"], kind))
            if bps is None:
                bps = blueprints()
            g.update(phrase=row.get("phrase"), sold=row.get("selling"), sold_of=row.get("sales_n"),
                     closest=bl.closest(top[0], kind, bps))
            out.append(g)
    GAPS.parent.mkdir(parents=True, exist_ok=True)
    GAPS.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "gaps": out}, indent=1) + "\n")
    return out


def gap_lines(g):
    have = ", ".join(g["have"]) if g["have"] else f"no {g['kind'] or 'product'} at all"
    lines = [f"WARNING: '{g['phrase']}' sells on {g['blank']} ({g['selling']} of its "
             f"{g['sold']} selling listings name it). Emily has: {have}."]
    for c in g.get("closest") or []:
        lines.append(f"  closest on Printify: blueprint {c['id']} {c['title']} "
                     f"({c.get('brand') or '?'} {c.get('model') or ''}) - {c['why']}")
    if g.get("closest"):
        lines.append(f"  set it up once: python3 scripts/emily-printify.py providers {g['closest'][0]['id']}"
                     f"  then  emily-printify.py pick --product {g['kind']} --blueprint "
                     f"{g['closest'][0]['id']} --provider <id>")
    return lines


def cmd_blanks(_a):
    found = gaps()
    if not found:
        print("No gaps: wherever selling listings name a blank, Emily has it (or none was named yet).")
        return 0
    for g in found:
        print("\n".join(gap_lines(g)) + "\n")
    return 0


def cmd_list(_a):
    d = load()
    rows = niches(d)
    print(f"{len(rows)} niches ({sum(1 for r in rows if r[1] == 'starter')} starter, "
          f"{sum(1 for r in rows if r[1] != 'starter')} found from what sells)\n")
    for seed, source, age in rows:
        _when, doc = scan_of(seed)
        best = max((r for r in (doc or {}).get("rows") or [] if r.get("sales_n")),
                   key=lambda r: (r.get("selling") or 0, r.get("reviews") or 0), default=None)
        sold = (f"best: {best['phrase']} - {best['selling']}/{best['sales_n']} sold, "
                f"{best['reviews']} reviews" if best else "no sales measured yet")
        if best and best.get("blanks"):
            sold += f", on {best['blanks'][0]['blank']}"
        when = "never" if age is None else f"{age:.0f}d ago"
        print(f"  {seed:<30} {when:>9}  {sold}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Keep Scout's niche data fresh and find new niches")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--n", type=int, default=RUN_N)
    r.add_argument("--force", action="store_true", help="re-scan even niches measured this week")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("blanks", help="selling blanks Emily is not set up for, and the closest Printify product").set_defaults(fn=cmd_blanks)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
