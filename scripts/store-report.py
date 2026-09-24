#!/usr/bin/env python3
"""
store-report.py — how the shop's own listings are doing, from Etsy.

    store-report.py setup VintageLoomTreasures   find the shop's id, once
    store-report.py fetch                        snapshot every active listing today
    store-report.py show                         the report: today against the last snapshot

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
    if s["quiet"]:
        out.append(f"No views after {QUIET_AFTER_DAYS}+ days - title, first photo or price "
                   f"worth a look: " + "; ".join(short(t, 36) for t in s["quiet"]))
    return out


def cmd_show(_args):
    print("\n".join(lines(latest_summary())))
    return 0


def main():
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    fn = {"setup": cmd_setup, "fetch": cmd_fetch, "show": cmd_show}.get(cmd)
    if not fn:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
