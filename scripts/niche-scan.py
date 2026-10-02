#!/usr/bin/env python3
"""
niche-scan.py - keep Scout's market data fresh, and find new niches, every
morning, with no model involved.

    python3 scripts/niche-scan.py run            scan the most out-of-date niches (the timer)
    python3 scripts/niche-scan.py run --n 2      fewer
    python3 scripts/niche-scan.py list           every niche, how old its scan is, what it sold

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


def due(d=None, n=RUN_N, now=None):
    return [s for s, _src, age in niches(d, now) if (age is None or age >= FRESH_DAYS)
            and not blocked(s)][:n]


def product_of(seed):
    try:
        return _load("emily_printify_n", "emily-printify.py").product_word(seed)
    except Exception:
        return None


def discover(d, now=None):
    """New niches from the tags of phrases that are SELLING: the words sellers
    who are making sales use for their own work. A tag with no product word
    takes its scan's product ("dog mom gift" in a mug scan -> "dog mom mug")."""
    known = {s.lower() for s, _src, _age in niches(d, now)}
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
                if not seed or seed in known or blocked(seed):
                    continue
                if len(known) >= MAX_NICHES or len(added) >= DISCOVER_PER_RUN:
                    return added
                known.add(seed)
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
    todo = due(d, a.n)
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
        when = "never" if age is None else f"{age:.0f}d ago"
        print(f"  {seed:<30} {when:>9}  {sold}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Keep Scout's niche data fresh and find new niches")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--n", type=int, default=RUN_N)
    r.set_defaults(fn=cmd_run)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
