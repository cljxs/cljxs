#!/usr/bin/env python3
"""
attention.py - what is waiting for the owner on each Command Deck tab.

    attention.py            JSON: {tab: {n, tone, why}} for the Deck's nav

Owner, 2026-10-16: "Can we add some type of notification to these
categories when they need my attention?" Each count is read from the script
that owns that page - the same function the page itself draws from - so a
badge and the page it opens can never disagree:

    shop       TikTok kits with AI scenes to check, today's kit not yet
               posted, listings waiting for a decision   (tiktok-kit.py,
               store-board.py)
    stan       problems waiting for approve or reject    (stan.py deck)
    agentroom  plans waiting for an approval, held runs  (agentroom.py deck)
    bolt       a book that does not balance, or no prices for half an hour
               (bolt.py)
    warren     news alerts on stocks the owner holds    (warren.py news_board)

tone is "todo" (a decision is yours to make) or "alert" (something is
wrong). A source that cannot be read says so in `error` and counts nothing:
a badge must never be invented. Free - no model, no network.
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SCRIPTS = Path(__file__).resolve().parent
PRICES_LATE = timedelta(minutes=30)      # bolt.py STALE_MINUTES: a 10-minute tick missed three times


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def shop(today=None):
    tk = _load("tiktok_kit_att", "tiktok-kit.py")
    kits = tk.read_kits()
    day = today or tk.et_time.day()
    review = [k for k in kits if k.get("status") == "needs_review"]
    unposted = [k for k in kits if k.get("day") == day and k.get("status") == "ready"]
    cards = _load("store_board_att", "store-board.py").board()["cards"]
    listings = [c for c in cards if not c["published"] and not c["approved_utc"]]
    why = []
    if review:
        why.append(plural(len(review), "TikTok kit") + " with AI scenes to check")
    if unposted:
        why.append("today's TikTok kit is ready to post")
    if listings:
        why.append(plural(len(listings), "listing") + " to review")
    return {"n": len(review) + len(unposted) + len(listings), "tone": "todo", "why": why}


def stan():
    pending = _load("stan_att", "stan.py").deck()["pending"]
    return {"n": len(pending), "tone": "todo",
            "why": [plural(len(pending), "problem") + " to approve or reject"] if pending else []}


def agentroom():
    ar = _load("agentroom_att", "agentroom.py")
    conn = ar.connect()
    try:
        c = ar.deck(conn)["counts"]
    finally:
        conn.close()
    why = []
    if c["waiting"]:
        why.append(plural(c["waiting"], "plan") + " waiting for your approval")
    if c["held"]:
        why.append(plural(c["held"], "run") + " held")
    return {"n": c["waiting"] + c["held"], "tone": "todo", "why": why}


def bolt(now=None):
    b = _load("bolt_att", "bolt.py")
    now = now or datetime.now(timezone.utc)
    why = []
    for label, path in (("Bolt's book", b.BOOK), ("the news book", b.NEWS_BOOK)):
        if path.is_file() and b.problems(b.load_book(now, path)):
            why.append(f"{label} does not balance")
    if b.BOOK.is_file():
        last = b.parse_iso(b.load_book(now, b.BOOK).get("last_tick_utc"))
        if last and now - last > PRICES_LATE:
            why.append(f"no prices since {last.strftime('%H:%M')} UTC")
    return {"n": len(why), "tone": "alert", "why": why}


def warren():
    """News alerts on what the owner holds, not yet dismissed (warren.py
    news_board). A sell alert is an alert; buy-more ones are a decision."""
    b = _load("warren_att", "warren.py").news_board()
    why = []
    if b["sell"]:
        why.append(plural(b["sell"], "headline") + " that may be a reason to sell")
    if b["buy"]:
        why.append(plural(b["buy"], "headline") + " that may be a reason to buy more")
    if b.get("watch"):
        why.append(plural(b["watch"], "headline") + " about what your sell or buy-more rules watch for")
    if b.get("important"):
        why.insert(0, plural(b["important"], "headline") + " rated High or Major")
    return {"n": b["unseen"], "tone": "alert" if b["sell"] or b.get("important") else "todo", "why": why}


SOURCES = {"shop": shop, "stan": stan, "agentroom": agentroom, "bolt": bolt, "warren": warren}


def attention():
    out = {}
    for tab, fn in SOURCES.items():
        try:
            out[tab] = fn()
        except Exception as exc:
            out[tab] = {"n": 0, "tone": "todo", "why": [], "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return out


if __name__ == "__main__":
    print(json.dumps(attention()))
    sys.exit(0)
