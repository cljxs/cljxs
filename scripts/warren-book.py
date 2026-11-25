#!/usr/bin/env python3
"""
warren-book.py - Warren's paper book: an AI-run copy of the owner's
portfolio that trades on its own, judged by Warren's checklist and his news.
PAPER MONEY ONLY - it never places a real order and touches no account.

    warren-book.py tick                the timer after warren-news: news on the book's own stocks, then
                                       the panel on every High or Major headline, then any trade
    warren-book.py ideas               weekly: Opus looks for new stocks, Warren checks each, the panel meets
    warren-book.py board               everything the Paper book tab draws, as JSON
    warren-book.py check               does the book balance? (exit 1 if not)
    warren-book.py watch add|remove SYM [--why TEXT]
    warren-book.py seen                the owner has looked at the trades (the village badge)

THE OWNER'S BRIEF (2026-11-23): "giving Warren all my current investment
positions and letting a AI run a paper trading book simulating my positions.
I would want it to run warrens tool on anything it wants to buy or sell ...
make sure it notes 'what would make me buy' and 'what would make me sell'
... read the high/important impact news that warren already fetches ... and
judge it for a buy/sell or hold." Then: "set up 3 different AI's to debate
over selling and buying. They should all read the news and form their own
opinion and come to an agreement if possible ... allow them to trade on the
news but make sure it checks warrens tool for a recap of the numbers."

HIS CHOICES: it starts as a copy of his Warren holdings plus $500 of paper
cash and then runs on its own (his real trades do not change it); it may buy
any US stock; at most 20% of the book in one stock; three Sonnets on the
panel; only a unanimous vote trades (a 2-1 split is a hold); new ideas from
Warren's checked stocks, a watchlist he adds to, and a weekly search of its
own; trades on its tab and as a village badge.

WHAT DECIDES WHAT:
  * code - every number: the book, the fills, the size of each trade, the
    20% limit, cash, the daily caps, the recap the panel reads;
  * Warren's checklist (warren.py build) - nothing is bought or sold
    without a check: the saved one if under CHECK_FRESH_DAYS old and the
    company has filed nothing since, else a fresh one. Its "what would make
    me buy" (buy-more test) and "what would make me sell" (sell rules) go
    into every decision, and a check that says Skip blocks a buy;
  * the panel - buy, hold or sell, nothing else: a Bull, a Bear and a Risk
    manager each answer alone, then read the other two and vote again.
    All three the same: that is the decision. Anything less: hold.
    An answer code cannot read, or with a number that is not in the recap or
    the headline, counts as hold.

SIZE (code, the owner's 20%): a buy adds BUY_PCT of the book's value, never
past MAX_PCT in one stock and never more than the cash; a stock that started
over MAX_PCT is kept but not added to. A sell sells half - all of it when
Warren's fresh check says Skip, or when half would leave a crumb.

Standard library only.
"""

import argparse
import email.utils
import importlib.util
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


W = _load("warren_for_book", "warren.py")
et_time = W.et_time

BOOK_DIR = W.STATE / "book"
BOOK = BOOK_DIR / "book.json"
DECISIONS = BOOK_DIR / "decisions.json"
WATCHLIST = BOOK_DIR / "watchlist.json"
COUNTS = BOOK_DIR / "counts.json"
IDEAS_LOG = BOOK_DIR / "ideas.json"
# Its own news on the stocks Warren's watcher does not cover (the book's,
# not the owner's): the same watcher (warren.watch_news), its own files.
BOOK_ALERTS = BOOK_DIR / "alerts.json"
BOOK_NEWS_SEEN = BOOK_DIR / "news-seen.json"

START_CASH = 500.0
MAX_PCT = 20.0                # of the book's value in one stock (owner, 2026-11-23)
BUY_PCT = 10.0                # what one buy adds, of the book's value
MIN_TRADE = 5.0               # dollars; anything smaller is not worth a line in the ledger
TRADES_PER_DAY = 3
MEETINGS_PER_DAY = 4          # each is six Sonnet questions
CHECKS_PER_DAY = 3            # fresh Warren checks the book may start a day - each is one of Warren's Opus runs
CHECK_FRESH_DAYS = 30
IMPORTANT = 4                 # High (4) and Major (5) headlines call the panel
ALERT_MAX_AGE_H = 48          # an older alert is not news any more
IDEAS_PER_WEEK = 3
PANEL_TIMEOUT = 300
DECISIONS_KEPT = 300
HISTORY_KEPT = 1000

# The owner (2026-11-24), on the first meetings: "the three ai models are
# basically just repeating the same things." They were: each role was told
# to give way "when the facts are against" it, so all three drifted to the
# middle, the Risk manager judged the company instead of the book, and round
# 2 never answered anyone. Now each role has one job it cannot hand back.
# The owner (2026-11-25), on the next ones: "too dull with each other". So
# code now hands each seat its own evidence (`evidence`), round 2 is a
# cross-examination - one pointed question each - and round 3 answers it,
# answers the others by name, and casts the vote that counts.
ROLES = {
    "bull": ("Bull", "Your job is the case FOR acting: what this news makes possible - the upside, and which part "
             "of the buy line it meets. Argue it as strongly as the recap honestly allows. If you vote hold, "
             "say exactly what is missing before you would buy."),
    "bear": ("Bear", "Your job is the case AGAINST: give the worst honest reading of this news, tie it to Warren's "
             "Red and Yellow ratings and the sell lines, and say what it would cost the book if you are right. "
             "Vote sell (when the book holds the stock) or hold - never buy - unless you can say precisely why the "
             "danger is not real. \"One report is not enough\" is not by itself such a reason."),
    "risk": ("Risk manager", "You do not judge whether this is a good company - the other two do that. You judge only "
             "the book: how much would sit in this one stock and in its theme, how much cash would be left, and "
             "how it overlaps with the other holdings (the recap lists them with their sectors). Vote on those "
             "grounds alone, and say which of them decides your vote."),
}
NOT_YOURS = ("Code sets the size of any trade and places it: never suggest amounts, steps, timing, limit orders or "
             "any other kind of order.")
# A second model on the panel (owner, 2026-11-24): the Bear sits on a model
# from another family, through OpenRouter, so the panel is not one model
# arguing with itself. The Agent Room already uses this one on the droplet.
# Its spend has a cap of its own (tasks/limits.json warren_book_daily_ai) and
# leaves warren_book_leave_for_others of the shared day; when it cannot be
# asked - over the cap, no key, a model OpenRouter no longer has - Sonnet
# takes the seat for that meeting and the debate says so.
SEAT_MODELS = {"bear": "openai/gpt-5.4-mini"}
OR_MAX_TOKENS = 2000
OR_SYSTEM = ("You answer as one member of a three-member investment panel for a beginner's paper trading book. "
             "Answer with the JSON object asked for and nothing else.")
MODELS_CACHE = BOOK_DIR / "models.json"
LIMITS = ROOT / "tasks" / "limits.json"
SPEND = BOOK_DIR / "spend.json"
VOTES = ("buy", "hold", "sell")
# The owner (2026-11-24): "make sure they know they can buy partial stocks.
# They don't have to buy a whole share." Code buys dollar amounts already;
# this tells the panel and the idea search, so neither passes on a stock
# because one share costs more than the book.
FRACTIONAL = ("Fractional shares are allowed: a buy is a dollar amount, not whole shares, so a stock whose single "
              "share costs more than the whole book can still be bought. Never count a high share price against a stock.")


# ---------------------------------------------------------------- small parts

def iso(t):
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(s):
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def pct(part, whole):
    return round(100 * part / whole, 1) if whole else 0.0


def count(kind, now=None, bump=False):
    """Today's count of meetings, checks or trades (Eastern day - a 22:00
    ET run is still today, though the UTC day has turned)."""
    day = et_time.day(et_time.to_eastern(now) if now else None)
    d = W.read_json(COUNTS, {}) or {}
    if d.get("day") != day:
        d = {"day": day}
    n = int(d.get(kind) or 0)
    if bump:
        d[kind] = n + 1
        W.write_json(COUNTS, d)
    return n + (1 if bump else 0)


# ---------------------------------------------------------------- the book

def seed(now, holds):
    """The owner's Warren holdings - same shares, same average price - plus
    START_CASH. A holding with no average price cannot be turned into
    shares, so it is left out and said so."""
    positions, notes = [], []
    for h in holds:
        sym, amt, avg = h.get("symbol"), float(h.get("amount") or 0), h.get("avg_price")
        if not avg or amt <= 0:
            notes.append(f"{sym}: no average price paid in Warren, so its shares are unknown - left out")
            continue
        positions.append({"symbol": sym, "shares": round(amt / avg, 6), "cost": round(amt, 2),
                          "opened_utc": iso(now), "from": "your holdings"})
    return {"mode": "PAPER", "started_utc": iso(now), "start_cash": START_CASH, "cash": START_CASH,
            "start_positions": [{k: p[k] for k in ("symbol", "shares", "cost")} for p in positions],
            "positions": positions, "trades": [], "marks": {}, "history": [], "seed_notes": notes}


def load_book():
    return W.read_json(BOOK)


def ensure_book(now):
    b = load_book()
    if b is None:
        b = seed(now, W.holdings())
        W.write_json(BOOK, b)
    return b


def held(book, sym):
    return next((p for p in book["positions"] if p["symbol"] == sym), None)


def mark_of(book, sym):
    p = held(book, sym)
    m = (book.get("marks") or {}).get(sym)
    return m if m else (p["cost"] / p["shares"] if p and p["shares"] else None)


def value(book):
    return round(book["cash"] + sum(p["shares"] * (mark_of(book, p["symbol"]) or 0) for p in book["positions"]), 2)


def baseline(book):
    """What the book would be worth had it never traded: the starting
    positions at today's prices plus the starting cash - the fair test of
    whether the panel's trades helped."""
    marks = book.get("marks") or {}
    total = book["start_cash"]
    for p in book.get("start_positions") or []:
        m = marks.get(p["symbol"]) or (p["cost"] / p["shares"] if p["shares"] else 0)
        total += p["shares"] * m
    return round(total, 2)


def buy(book, sym, spent, price, when, decision_id, why):
    shares = round(spent / price, 6)
    p = held(book, sym)
    if p:
        p["shares"] = round(p["shares"] + shares, 6)
        p["cost"] = round(p["cost"] + spent, 2)
    else:
        book["positions"].append({"symbol": sym, "shares": shares, "cost": round(spent, 2),
                                  "opened_utc": when, "from": "bought by the panel"})
    book["cash"] = round(book["cash"] - spent, 2)
    t = {"utc": when, "symbol": sym, "side": "buy", "shares": shares, "price": price, "notional": round(spent, 2),
         "decision": decision_id, "why": why, "seen": False}
    book["trades"].append(t)
    return t


def sell(book, sym, shares, price, when, decision_id, why):
    p = held(book, sym)
    shares = min(shares, p["shares"])
    cost_part = round(p["cost"] * shares / p["shares"], 2)
    proceeds = round(shares * price, 2)
    p["shares"] = round(p["shares"] - shares, 6)
    p["cost"] = round(p["cost"] - cost_part, 2)
    if p["shares"] <= 1e-6:
        book["positions"] = [x for x in book["positions"] if x is not p]
    book["cash"] = round(book["cash"] + proceeds, 2)
    t = {"utc": when, "symbol": sym, "side": "sell", "shares": round(shares, 6), "price": price,
         "notional": proceeds, "cost": cost_part, "gain": round(proceeds - cost_part, 2),
         "gain_pct": pct(proceeds - cost_part, cost_part), "decision": decision_id, "why": why, "seen": False}
    book["trades"].append(t)
    return t


def problems(book):
    """[] when cash and shares are exactly what the start and the trades
    leave - the ledger is the record, the positions are its sum."""
    out = []
    cash = book["start_cash"]
    shares = {p["symbol"]: p["shares"] for p in book.get("start_positions") or []}
    for t in book["trades"]:
        sign = 1 if t["side"] == "buy" else -1
        cash -= sign * t["notional"]
        shares[t["symbol"]] = shares.get(t["symbol"], 0) + sign * t["shares"]
    if abs(cash - book["cash"]) > 0.02:
        out.append(f"cash is ${book['cash']:,.2f} but the start and the trades leave ${cash:,.2f}")
    if book["cash"] < -0.005:
        out.append(f"cash is below zero (${book['cash']:,.2f})")
    have = {}
    for p in book["positions"]:
        if p["symbol"] in have:
            out.append(f"{p['symbol']} is held twice")
        have[p["symbol"]] = p["shares"]
    for s in set(shares) | set(have):
        if abs(shares.get(s, 0) - have.get(s, 0)) > 1e-4:
            out.append(f"{s}: the trades leave {shares.get(s, 0):.6f} shares, the book holds {have.get(s, 0):.6f}")
    return out


def size_buy(book, sym):
    """(dollars, why-not). BUY_PCT of the book, never past MAX_PCT in one
    stock, never more than the cash."""
    v = value(book)
    p = held(book, sym)
    now_in = p["shares"] * (mark_of(book, sym) or 0) if p else 0.0
    if now_in >= v * MAX_PCT / 100:
        return 0.0, f"{sym} is already {pct(now_in, v)}% of the book - the limit is {MAX_PCT:g}%"
    spend = min(v * BUY_PCT / 100, v * MAX_PCT / 100 - now_in, book["cash"])
    if spend < MIN_TRADE:
        return 0.0, f"only ${book['cash']:,.2f} of cash - too little to buy with" if book["cash"] < MIN_TRADE \
            else "the 20% limit leaves too little room"
    return round(spend, 2), ""


def size_sell(book, sym, all_of_it):
    """Shares to sell: half, or all of it when Warren says Skip or half
    would leave a crumb."""
    p = held(book, sym)
    half = p["shares"] / 2
    if all_of_it or half * (mark_of(book, sym) or 0) < MIN_TRADE:
        return p["shares"]
    return round(half, 6)


# ------------------------------------------------- Warren's checklist first

def check_for(sym, now, amount, build=None, new_report=None):
    """(check, how) - the saved check when it is under CHECK_FRESH_DAYS old
    and the company has filed nothing since, else a fresh one. (None, why)
    when there is none to be had - then nothing is traded."""
    old = W.latest(sym)
    t = parse((old or {}).get("when_utc"))
    if old and t and (now - t).days <= CHECK_FRESH_DAYS:
        try:
            if not (new_report or W.new_report)(old):
                return old, f"Warren's check of {old['when_utc'][:10]}"
        except Exception:
            return old, f"Warren's check of {old['when_utc'][:10]} (the SEC could not be asked about new filings)"
    if count("checks", now) >= CHECKS_PER_DAY:
        return None, f"the book's {CHECKS_PER_DAY} fresh checks today are used - asked again next run"
    if W.runs_today() >= W.RUNS_PER_DAY:
        return None, f"Warren's {W.RUNS_PER_DAY} Opus runs today are used - asked again next run"
    inputs = dict((old or {}).get("inputs") or {"months": 12, "goal": "growth", "hold_drop": "yes"})
    inputs.update(amount=float(max(round(amount), 1)), why="Warren's paper book")
    count("checks", now, bump=True)
    try:
        new = (build or W.build)(sym, inputs)
    except Exception as e:
        return None, f"Warren's check of {sym} failed ({str(e)[:120]})"
    new["changes"] = W.changes(old, new)
    new["by"] = "paper book"
    W.save(new)
    return new, f"a fresh Warren check ({new['when_utc'][:10]})"


def lines_of(check):
    """What would make me buy / what would make me sell, from the check."""
    ai = (check or {}).get("ai") or {}
    risk = ((check or {}).get("steps") or {}).get("risk") or {}
    return {"buy": risk.get("buy_more") or ai.get("buy_more") or "",
            "sell": list(ai.get("sell_rules") or [])}


def recap(check, book, sym):
    """What the panel reads about the stock and the book - written by code
    from Warren's saved check, so every number in it is Warren's."""
    n = check.get("numbers") or {}
    m = W.money
    steps = check.get("steps") or {}
    out = [f"{sym} - {check.get('company') or sym} ({check.get('sector') or 'sector unknown'}). "
           f"Warren's check of {str(check.get('when_utc'))[:10]}: {check.get('decision')} - {check.get('decision_why') or ''}"]
    out.append("Step ratings: " + ", ".join(f"{k} {v.get('rating')}" for k, v in steps.items()))
    for r in check.get("decision_reasons") or []:
        out.append(f"Red: {r}")
    figs = [("price", n.get("price"), lambda v: f"${v:,.2f}"),
            ("forward P/E", n.get("pe_forward"), lambda v: f"{v:g}"),
            ("P/S", n.get("ps"), lambda v: f"{v:g}"),
            ("free cash flow, 12 months", n.get("fcf_ttm"), m),
            ("revenue, 12 months", n.get("revenue_ttm"), m),
            ("profit margin", n.get("margin_ttm"), lambda v: f"{v:g}%"),
            ("FCF yield", n.get("fcf_yield"), lambda v: f"{v:g}%"),
            ("leverage (net debt / EBITDA)", n.get("leverage"), lambda v: f"{v:g}x"),
            ("1-year price change", n.get("change_1y"), lambda v: f"{v:+g}%")]
    out.append("Key figures: " + "; ".join(f"{k} {f(v)}" for k, v, f in figs if v is not None))
    for c in check.get("caution") or []:
        out.append(f"Caution: {c}")
    v = value(book)
    p = held(book, sym)
    if p:
        pv = p["shares"] * (mark_of(book, sym) or 0)
        out.append(f"The paper book is worth {m(v)} with {m(book['cash'])} cash. It holds {p['shares']:.4f} shares "
                   f"of {sym} worth {m(pv)} ({pct(pv, v)}% of the book), bought for {m(p['cost'])}.")
    else:
        out.append(f"The paper book is worth {m(v)} with {m(book['cash'])} cash. It does not hold {sym}.")
    others = sorted(((x["symbol"], x["shares"] * (mark_of(book, x["symbol"]) or 0)) for x in book["positions"]
                     if x["symbol"] != sym), key=lambda t: -t[1])
    if others:
        out.append("Its other holdings: " + ", ".join(
            f"{s} {pct(val, v)}% ({(W.latest(s) or {}).get('sector') or 'sector unknown'})" for s, val in others))
    out.append(f"Code sizes any trade: a buy adds about {BUY_PCT:g}% of the book, never past {MAX_PCT:g}% in one "
               "stock; a sell sells half, or all of it when Warren's check says Skip. " + FRACTIONAL)
    return "\n".join(out)


# ------------------------------------------------------------ the evidence
# The owner (2026-11-25), on a debate where all three argued from the same
# short recap: "they are being too dull with each other and could use access
# to some more tools for more informed and better debates." Code now gathers
# what a careful investor would look at - every figure from a source code
# reads, nothing from a model - and each seat gets its own share:
#   all three: the article itself, the stock's news of the last two weeks and
#     how the price moved after each, the book's own record on the stock,
#     the market's mood (Belfort's market check) and its sector, the chart;
#   the Bull: what analysts expect (targets, ratings, forecasts), the next
#     earnings date, and where it beats its rivals;
#   the Bear: Warren's risks, every Red and Yellow and why, figures the second
#     source disputed, insider selling, short interest, where it trails rivals;
#   the Risk manager: how closely it moves with each holding, the book's
#     sectors, and cash.
# A piece that cannot be had says so; the debate goes on without it.

INSIDER_URL = "https://api.nasdaq.com/api/company/{sym}/insider-trades?limit=15&type=ALL&sortColumn=lastDate&sortOrder=DESC"
SHORT_URL = "https://api.nasdaq.com/api/quote/{sym}/short-interest?assetClass=stocks"
TARGET_URL = "https://api.nasdaq.com/api/analyst/{sym}/targetprice"
EARNINGS_DATE_URL = "https://api.nasdaq.com/api/analyst/{sym}/earnings-date"
CHARTS = BOOK_DIR / "charts"
HISTORY_DAYS = 14
CORR_DAYS = 120
ARTICLE_CHARS = 2500
BOILER = re.compile(r"broker-dealer|financial advice|cookies|subscribe|sign up|newsletter|all rights reserved|"
                    r"get in touch|terms of (use|service)|privacy policy", re.I)
SECTOR_NAMES = {"finance": "financials", "healthcare": "health care", "health care": "health care",
                "telecommunications": "communication services", "basic materials": "materials",
                "communication services": "communication services", "technology": "technology",
                "consumer discretionary": "consumer discretionary", "consumer staples": "consumer staples",
                "energy": "energy", "industrials": "industrials", "utilities": "utilities", "real estate": "real estate",
                "materials": "materials", "financials": "financials"}


def article_text(url, get=None):
    """(the article's own paragraphs, why not). Scripts, pictures and the
    site's own boilerplate are dropped; ARTICLE_CHARS at most."""
    if not url or not str(url).startswith("http"):
        return "", "no link to the article"
    if url.split("/")[2].endswith("news.google.com"):
        return "", "a Google News link - the article itself cannot be opened from it"
    try:
        raw = (get or W.http_get)(url)
    except Exception as e:
        return "", f"the page could not be opened ({str(e)[:80]})"
    html = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    html = re.sub(r"(?is)<(script|style|svg|noscript)\b.*?</\1>", " ", html)
    paras = []
    for chunk in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", html):
        t = re.sub(r"\s+", " ", unescape(re.sub(r"(?s)<[^>]+>", " ", chunk))).strip()
        if len(t) >= 60 and not BOILER.search(t) and t not in paras:
            paras.append(t)
    text = ""
    for t in paras:
        if len(text) + len(t) > ARTICLE_CHARS:
            break
        text += t + "\n"
    return text.strip(), "" if text else "no article text on the page (a paywall, or a page built by script)"


def closes_of(payload):
    """[(YYYY-MM-DD, close)] from a Yahoo chart."""
    res = payload["chart"]["result"][0]
    q = (res.get("indicators") or {}).get("quote", [{}])[0]
    return [(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), float(c))
            for t, c in zip(res.get("timestamp") or [], q.get("close") or []) if c is not None]


def chart(sym, now, get=None):
    """A year of daily closes, fetched once a day per stock."""
    path = CHARTS / f"{sym}.json"
    day = now.strftime("%Y-%m-%d")
    have = W.read_json(path, {}) or {}
    if have.get("day") == day and have.get("closes"):
        return [tuple(x) for x in have["closes"]]
    closes = closes_of(W.get_json(W.CHART_URL.format(sym=sym), get))
    W.write_json(path, {"day": day, "closes": closes})
    return closes


def _when(a):
    for k in ("published", "found_utc"):
        try:
            t = email.utils.parsedate_to_datetime(a[k]) if k == "published" else parse(a[k])
            if t:
                return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return None


def news_history(sym, now, closes):
    """The stock's alerts of the last HISTORY_DAYS, newest first, each with
    how the price moved on its day and since."""
    seen, out = set(), []
    for path in (W.ALERTS, BOOK_ALERTS):
        for a in (W.read_json(path, {}) or {}).get("alerts") or []:
            t = _when(a)
            if a.get("symbol") != sym or not t or now - t > timedelta(days=HISTORY_DAYS) or a["title"] in seen:
                continue
            seen.add(a["title"])
            out.append((t, a))
    lines = []
    for t, a in sorted(out, key=lambda x: x[0], reverse=True)[:8]:
        line = f"{t.strftime('%b %d')} [{W.IMPORTANCE.get(a.get('importance'), '?')}] {a['title']} - marked {a['signal']}"
        day = t.strftime("%Y-%m-%d")
        i = next((k for k, (d, _) in enumerate(closes) if d >= day), None)
        if i is not None and i > 0:
            before, on, last = closes[i - 1][1], closes[i][1], closes[-1][1]
            line += (f"; the stock moved {100 * (on / before - 1):+.1f}% that day and "
                     f"{100 * (last / before - 1):+.1f}% since")
        lines.append(line)
    return lines


def record_on(sym, book):
    """The panel's last meetings on this stock and how the price did since."""
    now_price = (book.get("marks") or {}).get(sym)
    out = []
    for d in [d for d in (W.read_json(DECISIONS, {}) or {}).get("decisions") or [] if d["symbol"] == sym][-3:]:
        line = f"{d['utc'][:10]}: the panel's verdict was {d['outcome']} ({d['note']}); {d['result']}"
        if d.get("price") and now_price:
            line += f"; the price was ${d['price']:,.2f} then and is ${now_price:,.2f} now ({100 * (now_price / d['price'] - 1):+.1f}%)"
        out.append(line)
    return out


def market_mood(sector):
    """Belfort's market check for both indexes, and the stock's sector fund."""
    bt = _load("book_belfort_trade", "belfort-trade.py")
    doc = bt.quotes_doc()
    out = [f"{i}: {r['state']} - {r['why']}" for i, r in bt.regimes(doc).items()]
    name = SECTOR_NAMES.get(str(sector or "").strip().lower())
    fund = next((f for f, n in bt.SECTOR_FUNDS.items() if n == name), None)
    q = (doc.get("sectors") or {}).get(fund) or {}
    if fund and q.get("price") and q.get("sma50"):
        hist = [c for _, c in q.get("history") or []]
        line = (f"its sector, {name} ({fund}): ${q['price']:,.2f}, "
                f"{abs(100 * (q['price'] / q['sma50'] - 1)):.1f}% {'above' if q['price'] > q['sma50'] else 'below'} its 50-day average")
        if len(hist) > 21:
            line += f", {100 * (hist[-1] / hist[-22] - 1):+.1f}% over the last month"
        out.append(line)
    return out


def _n(x):
    try:
        return float(str(x).replace(",", "").replace("$", "").replace("(", "-").replace(")", ""))
    except (TypeError, ValueError):
        return None


def insiders(payload):
    """The company's own people buying and selling, from Nasdaq's record of
    their SEC Form 4 filings."""
    d = (payload or {}).get("data") or {}
    trades = {r["insiderTrade"]: r for r in (d.get("numberOfTrades") or {}).get("rows") or []}
    shares = {r["insiderTrade"]: r for r in (d.get("numberOfSharesTraded") or {}).get("rows") or []}
    out = []
    for k, span in (("months3", "last 3 months"), ("months12", "last 12 months")):
        b = (trades.get("Number of Open Market Buys") or {}).get(k)
        s_ = (trades.get("Number of Sells") or {}).get(k)
        net = (shares.get("Net Activity") or {}).get(k)
        if b is not None and s_ is not None:
            out.append(f"insiders, {span}: {b} open-market buys, {s_} sells, net {_n(net):+,.0f} shares"
                       if net is not None else f"insiders, {span}: {b} open-market buys, {s_} sells")
    rows = ((d.get("transactionTable") or {}).get("table") or {}).get("rows") or []
    real = [r for r in rows if "Option" not in r["transactionType"] and "Non Open Market" not in r["transactionType"]]
    for r in real[:4]:
        planned = " - a pre-planned (10b5-1) sale, a weaker signal than a chosen one" if r["transactionType"] == "Automatic Sell" else ""
        out.append(f"{r['lastDate']}: {r['insider'].title()} ({r['relation']}) {r['transactionType'].lower()} "
                   f"{r['sharesTraded']} shares at {r['lastPrice']}{planned}")
    return out


def short_interest(payload, n=None):
    """How many shares are sold short - investors betting on a fall - and
    how that has moved."""
    rows = (((payload or {}).get("data") or {}).get("shortInterestTable") or {}).get("rows") or []
    if not rows:
        return []
    last = rows[0]
    si = _n(last["interest"])
    line = f"short interest {last['interest']} shares on {last['settlementDate']}, {float(last['daysToCover']):.1f} days to cover"
    shares = (n or {}).get("market_cap") / (n or {}).get("price") if (n or {}).get("market_cap") and (n or {}).get("price") else None
    if shares and si:
        line += f", about {100 * si / shares:.1f}% of its shares"
    day = lambda r: datetime.strptime(r["settlementDate"], "%m/%d/%Y")
    for back, label in ((28, "a month"), (170, "six months")):
        old = next((r for r in rows if (day(last) - day(r)).days >= back), None)
        if old and _n(old["interest"]):
            line += f"; {100 * (si / _n(old['interest']) - 1):+.1f}% vs {label} earlier ({old['settlementDate']})"
    return [line]


def analysts(target, earnings_date=None, price=None):
    """What the analysts who follow it expect, from Nasdaq."""
    d = (target or {}).get("data") or {}
    c = d.get("consensusOverview") or {}
    out = []
    if c.get("priceTarget"):
        line = (f"analysts' average price target ${c['priceTarget']:,.2f} (low ${c['lowPriceTarget']:,.2f}, "
                f"high ${c['highPriceTarget']:,.2f}); {c.get('buy', 0)} buy, {c.get('hold', 0)} hold, {c.get('sell', 0)} sell")
        if price:
            line += f"; the average target is {100 * (c['priceTarget'] / price - 1):+.1f}% from today's price"
        hist = d.get("historicalConsensus") or []
        if len(hist) >= 4 and hist[-4].get("y"):
            line += f"; three months ago the average target was ${hist[-4]['y']:,.2f}"
        out.append(line)
    text = str(((earnings_date or {}).get("data") or {}).get("announcement") or "")
    m = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", text + " " + str(((earnings_date or {}).get("data") or {}).get("reportText") or ""))
    out.append(f"next earnings report: {m.group(1)}" if m else "next earnings report: no date given yet")
    return out


def correlation(a, b):
    """How closely two stocks' daily moves go together, -1 to 1, over the
    days both traded (the last CORR_DAYS)."""
    da, db = dict(a), dict(b)
    days = sorted(set(da) & set(db))[-(CORR_DAYS + 1):]
    if len(days) < 30:
        return None
    ra = [da[days[i]] / da[days[i - 1]] - 1 for i in range(1, len(days))]
    rb = [db[days[i]] / db[days[i - 1]] - 1 for i in range(1, len(days))]
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return round(cov / math.sqrt(va * vb), 2) if va and vb else None


def together(c):
    return ("move closely together" if c >= 0.7 else "often move together" if c >= 0.4
            else "loosely related" if c >= 0.1 else "barely related, or move opposite ways")


def rival_lines(check, sym):
    """(where it leads its rivals, where it trails) from the check's rows."""
    lead, trail = [], []
    for st in (check.get("steps") or {}).values():
        for r in st.get("rows") or []:
            vs = r.get("vs")
            if not vs:
                continue
            line = (f"{vs['label']}: " + ", ".join(f"{c['symbol']} {c['text']}" for c in vs["cols"])
                    + (f"; rivals' average {vs['average']}" if vs.get("average") else "") + f". {vs['note']}")
            spec = next((x for x in W.RIVAL_ROWS if r["check"].startswith(x[0])), None)
            best, worst = W.BEST.get(spec[0], ("", "")) if spec else ("", "")
            if worst and f"is the {worst}" in vs["note"]:
                trail.append(line)
            elif best and f"is the {best}" in vs["note"]:
                lead.append(line)
            else:
                lead.append(line)
                trail.append(line)
    return lead, trail


def check_detail(check):
    """What else Warren's check knows: (for the Bull, for the Bear, for all)."""
    n, ai = check.get("numbers") or {}, check.get("ai") or {}
    steps = check.get("steps") or {}
    bull, bear, both = [], [], []
    if n.get("eps_ntm") is not None:
        bull.append(f"analysts' expected earnings per share over the next 12 months: ${n['eps_ntm']}"
                    + (f"; expected yearly earnings growth {n['eps_growth']:+g}%" if n.get("eps_growth") is not None else ""))
    for k, label in (("business", "the business"), ("customers", "its customers")):
        if ai.get(k):
            both.append(f"{label}: {ai[k]}")
    for r in ai.get("risks") or []:
        bear.append(f"risk: {r}")
    if ai.get("debt_note"):
        bear.append(f"debt: {ai['debt_note']}")
    for st in steps.values():
        for r in st.get("rows") or []:
            if r.get("rating") in ("Red", "Yellow") and r.get("why"):
                bear.append(f"{r['check']}: {r['rating']} - {r['why']}")
    for q, a in (steps.get("price") or {}).get("chart") or []:
        both.append(f"{q} {a}")
    ss = n.get("second_source") or {}
    for f in (ss.get("figures") or {}).values():
        if f.get("status") in ("disagree", "settled", "explained"):
            bear.append(f"figure checked against Nasdaq - {f['label']}: " + ((f.get("settled") or {}).get("says")
                        or f"Warren {W.money(f.get('ours'))}, Nasdaq {W.money(f.get('theirs'))}, not settled"))
    for a in n.get("alarms") or []:
        bear.append(f"Warren's alarm: {a}")
    lead, trail = rival_lines(check, check.get("symbol"))
    bull += [f"against rivals - {x}" for x in lead]
    bear += [f"against rivals - {x}" for x in trail]
    return bull, bear, both


def evidence(sym, check, book, alerts, now, get=None):
    """{"shared", "bull", "bear", "risk", "missing"} - text blocks, all
    written by code from the sources named in each line."""
    missing = []

    def piece(name, fn):
        try:
            return fn()
        except Exception as e:
            missing.append(f"{name} ({str(e)[:80]})")
            return []

    n = check.get("numbers") or {}
    price = (book.get("marks") or {}).get(sym) or n.get("price")
    closes = piece("the price history", lambda: chart(sym, now, get))
    texts = []
    for a in alerts[:2]:
        t, why = article_text(a.get("link"), get)
        texts.append(f"{a['title']}:\n{t}" if t else f"{a['title']}: (article text not available - {why})")
    shared = {"THE ARTICLE ITSELF (as the publisher wrote it - not checked by Warren)": texts,
              f"THE STOCK'S NEWS, LAST {HISTORY_DAYS} DAYS, AND HOW THE PRICE MOVED AFTER EACH":
                  piece("the news history", lambda: news_history(sym, now, closes)),
              "THE PANEL'S OWN RECORD ON THIS STOCK": record_on(sym, book) or ["no earlier meetings on this stock"],
              "THE MARKET'S MOOD (Belfort's market check)": piece("the market's mood", lambda: market_mood(
                  check.get("sector") or n.get("nasdaq_sector"))),
              }
    bull_d, bear_d, both_d = check_detail(check)
    shared["MORE FROM WARREN'S CHECK"] = both_d
    bull = {"WHAT ANALYSTS EXPECT": piece("analysts' targets", lambda: analysts(
                W.get_json(TARGET_URL.format(sym=sym), get),
                piece("the earnings date", lambda: W.get_json(EARNINGS_DATE_URL.format(sym=sym), get)) or None, price)),
            "FOR THE CASE TO BUY": bull_d}
    bear = {"INSIDERS (company officers and directors, from their SEC filings via Nasdaq)":
                piece("insider trades", lambda: insiders(W.get_json(INSIDER_URL.format(sym=sym), get))),
            "SHORT SELLERS (investors betting the price falls)":
                piece("short interest", lambda: short_interest(W.get_json(SHORT_URL.format(sym=sym), get), n)),
            "FOR THE CASE AGAINST": bear_d}
    corr = []
    for p in book["positions"]:
        if p["symbol"] == sym:
            continue
        c = piece(f"{p['symbol']}'s price history", lambda: correlation(closes, chart(p["symbol"], now, get)) if closes else None)
        if isinstance(c, float):
            corr.append(f"{p['symbol']}: {c:+.2f} - {together(c)}")
    v = value(book)
    sectors = {}
    for p in book["positions"]:
        sec = (W.latest(p["symbol"]) or {}).get("sector") or "sector unknown"
        sectors[sec] = sectors.get(sec, 0) + p["shares"] * (mark_of(book, p["symbol"]) or 0)
    risk = {f"HOW CLOSELY {sym} MOVES WITH EACH HOLDING (daily moves, last {CORR_DAYS} trading days; 1 = in step, "
            "0 = unrelated)": corr or ["not available"],
            "THE BOOK BY SECTOR": [f"{k}: {pct(x, v)}%" for k, x in sorted(sectors.items(), key=lambda t: -t[1])]
            + [f"cash: {pct(book['cash'], v)}%"]}
    block = lambda d: "\n".join(f"{k}:\n" + "\n".join(f"  - {x}" for x in (vals or ["nothing found"]))
                                for k, vals in d.items())
    return {"shared": block(shared), "bull": block(bull), "bear": block(bear), "risk": block(risk),
            "missing": missing}


# ---------------------------------------------------------------- the panel

# THE CROSS-EXAMINATION (owner, 2026-11-25): round 2 used to end in "I
# agree with your strongest point" all round. Now:
#   round 1 - each argues alone, from the shared evidence and its own;
#   round 2 - each puts ONE pointed question to ONE other member, about the
#             weakest part of their case; nobody votes;
#   round 3 - each answers the questions put to it, answers the others'
#             strongest points by name, and votes. Only round 3 counts.
STAGES = {1: "ROUND 1", 2: "ROUND 2 - CROSS-EXAMINATION", 3: "ROUND 3 - ANSWERS AND THE FINAL VOTE"}


def panel_question(role, sym, rc, lines, event, ev=None, stage=1, r1=None, qs=None):
    name, brief = ROLES[role]
    ev = ev or {}
    sells = "\n".join(f"  sell {i}. I would sell if {r}" for i, r in enumerate(lines["sell"], 1)) or "  (none saved)"
    q = (f"You sit on a three-member panel that runs a PAPER trading book for a beginner investor - practice money, "
         f"no real trades. Your role: {name}. {brief} {NOT_YOURS}\n\n"
         f"WHAT HAPPENED\n{event}\n\n"
         f"WARREN'S CHECKLIST RECAP (computed by code from SEC filings)\n{rc}\n\n"
         f"THE STOCK'S OWN LINES, from Warren's check\n  What would make me buy: {lines['buy'] or '(none saved)'}\n"
         f"  What would make me sell:\n{sells}\n\n")
    if ev.get("shared"):
        q += f"EVIDENCE ALL THREE OF YOU HAVE\n{ev['shared']}\n\n"
    if ev.get(role):
        q += (f"YOUR OWN EVIDENCE, for your side - the other two were given different evidence for theirs\n"
              f"{ev[role]}\n\n")
    q += "Every figure you use must come from what is above. Use the evidence: name the line you rely on.\n\n"
    if stage == 1:
        q += f"{STAGES[1]}. Answer on your own; you will see the others' answers next.\n\n"
        q += ('Answer with JSON only: {"vote": "buy", "hold" or "sell", "line": "buy" if the news meets the buy line, '
              '"sell N" for the sell line it hits, or "none", "confidence": 1 to 5, "why": "two or three plain '
              'sentences for a beginner"}.')
        return q
    others = [r for r in ROLES if r != role]
    q += "ROUND 1 - what each member said:\n" + "\n".join(
        f"  {ROLES[r][0]}: {v['vote']} - {v['why']}" for r, v in (r1 or {}).items()) + "\n\n"
    if stage == 2:
        q += (f"{STAGES[2]}. Do not vote now. Put ONE pointed question to ONE other member - "
              f"{' or '.join(ROLES[r][0] for r in others)} - aimed at the weakest part of their case: the question "
              "whose honest answer would most change the decision. No speeches; one question.\n\n"
              'Answer with JSON only: {"to": "' + '" or "'.join(ROLES[r][0] for r in others)
              + '", "question": "your one question"}.')
        return q
    asked = [(r, x) for r, x in (qs or {}).items() if not x.get("failed")]
    q += "ROUND 2 - the questions:\n" + ("\n".join(f"  {ROLES[r][0]} asked {ROLES[x['to']][0]}: {x['question']}"
                                                  for r, x in asked) or "  (none)") + "\n\n"
    mine = [(r, x) for r, x in asked if x["to"] == role]
    names = " and ".join(ROLES[r][0] for r in others)
    q += (f"{STAGES[3]}. " + ("Answer the question(s) put to you plainly in \"replies\" - say whether the answer "
                              "changes your mind. " if mine else "")
          + f"Answer the strongest point each of {names} made, by name, in \"answers\". Then give your final vote, "
          "still in your own role. Change your mind only if their points are better than yours. Only a vote all "
          "three agree on is traded.\n\n")
    q += ('Answer with JSON only: {' + ('"replies": "your answer to each question put to you", ' if mine else '')
          + '"answers": "one sentence to each of the other two, starting with their role name", '
          '"vote": "buy", "hold" or "sell", "line": "buy", "sell N" or "none", "confidence": 1 to 5, '
          '"why": "two or three plain sentences for a beginner"}.')
    return q


def _numbers(text):
    return {x.rstrip(".,") for x in re.findall(r"\d[\d.,]*", text or "")}


def read_question(text, allowed, role):
    """{"to": role key, "question"} code can stand behind, or None."""
    t = (text or "").strip()
    a, b = t.find("{"), t.rfind("}")
    try:
        d = json.loads(t[a:b + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    to = str(d.get("to") or "").strip().lower()
    key = next((r for r, (nm, _) in ROLES.items() if nm.lower() == to and r != role), None)
    question = str(d.get("question") or "").strip()
    if not key or not 10 <= len(question) <= 400:
        return None
    if _numbers(question) - _numbers(allowed) - {str(i) for i in range(1, 6)}:
        return None
    return {"to": key, "question": question}


def read_vote(text, allowed, n_sell, answer=(), replies=False):
    """The vote code can stand behind, or None: valid JSON, buy/hold/sell, a
    line that exists, a reason with no number the recap or the news lacks -
    and in round 2 (`answer` = the others' role names) a reply that names
    each of them."""
    t = (text or "").strip()
    a, b = t.find("{"), t.rfind("}")
    try:
        d = json.loads(t[a:b + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    vote, line, why = d.get("vote"), str(d.get("line") or "none").strip().lower(), str(d.get("why") or "").strip()
    if vote not in VOTES or not why or len(why) > 700:
        return None
    m = re.fullmatch(r"sell (\d+)", line)
    if not (line in ("buy", "none") or (m and 1 <= int(m.group(1)) <= n_sell)):
        return None
    # Every figure must be the recap's or the headline's; a small whole
    # number is allowed on its own, for "sell rule 2" or "3 of the steps".
    if _numbers(why) - _numbers(allowed) - {str(i) for i in range(1, 6)}:
        return None
    said = str(d.get("answers") or "").strip()[:700]
    if answer and not all(name.lower() in said.lower() for name in answer):
        return None
    if _numbers(said) - _numbers(allowed) - {str(i) for i in range(1, 6)}:
        return None
    try:
        conf = int(d.get("confidence"))
    except (TypeError, ValueError):
        conf = None
    out = {"vote": vote, "line": line, "confidence": conf if conf in range(1, 6) else None, "why": why}
    if answer:
        out["answers"] = said
    if replies:
        r = str(d.get("replies") or "").strip()[:900]
        if not r or _numbers(r) - _numbers(allowed) - {str(i) for i in range(1, 6)}:
            return None
        out["replies"] = r
    return out


def meet(sym, event, check, book, ask, ev=None):
    """Three rounds, three members: (round 1, the questions, the final
    votes, outcome, note)."""
    rc, lines = recap(check, book, sym), lines_of(check)
    ev = ev or {}
    allowed = "\n".join([rc, event] + [ev.get(k) or "" for k in ("shared", "bull", "bear", "risk")])
    n_sell = len(lines["sell"])

    def put(role, q):
        said = ask(q, role)
        ok, text, who = (tuple(said) + ("",))[:3]
        return (text if ok else None), who

    r1 = {}
    for role in ROLES:
        text, who = put(role, panel_question(role, sym, rc, lines, event, ev, 1))
        r1[role] = (read_vote(text, allowed, n_sell) if text else None) or {
            "vote": "hold", "line": "none", "confidence": None, "failed": True,
            "why": "(no answer code could stand behind - counted as hold)"}
        if who:
            r1[role]["model"] = who
    qs = {}
    for role in ROLES:
        text, who = put(role, panel_question(role, sym, rc, lines, event, ev, 2, r1))
        qs[role] = (read_question(text, allowed, role) if text else None) or {
            "failed": True, "question": "(no question code could stand behind)"}
        if who:
            qs[role]["model"] = who
    final = {}
    for role in ROLES:
        others = tuple(ROLES[o][0] for o in ROLES if o != role)
        put_to = any(x.get("to") == role for x in qs.values() if not x.get("failed"))
        text, who = put(role, panel_question(role, sym, rc, lines, event, ev, 3, r1, qs))
        final[role] = (read_vote(text, allowed, n_sell, others, put_to) if text else None) or {
            "vote": "hold", "line": "none", "confidence": None, "failed": True,
            "why": "(no answer code could stand behind - counted as hold)"}
        if who:
            final[role]["model"] = who
    votes = [v["vote"] for v in final.values()]
    if len(set(votes)) == 1 and not any(v.get("failed") for v in final.values()):
        return r1, qs, final, votes[0], "all three agree"
    tally = {x: votes.count(x) for x in VOTES if votes.count(x)}
    return r1, qs, final, "hold", ("split " + ", ".join(f"{n} {x}" for x, n in tally.items())
                                   + " - no trade (only a unanimous vote trades)")


def act(book, sym, outcome, check, now, did):
    """(trade or None, what happened). Code decides the size; Warren's
    check can still block a buy."""
    if outcome == "hold":
        return None, "held"
    if count("trades", now) >= TRADES_PER_DAY:
        return None, f"no trade: the {TRADES_PER_DAY} trades a day are used"
    price = mark_of(book, sym) if held(book, sym) else None
    price = (book.get("marks") or {}).get(sym) or price
    if not price:
        return None, f"no trade: no price for {sym}"
    skip = check.get("decision") == "Skip"
    when = iso(now)
    if outcome == "buy":
        if skip:
            return None, "no trade: Warren's check says Skip, so the book does not buy it"
        spend, why_not = size_buy(book, sym)
        if not spend:
            return None, f"no trade: {why_not}"
        t = buy(book, sym, spend, price, when, did, "the panel voted buy, all three")
    else:
        if not held(book, sym):
            return None, f"no trade: the book does not hold {sym}"
        t = sell(book, sym, size_sell(book, sym, skip), price, when, did,
                 "the panel voted sell, all three" + (" - all of it, Warren's check says Skip" if skip else ""))
    count("trades", now, bump=True)
    return t, f"{'bought' if t['side'] == 'buy' else 'sold'} {t['shares']:.4f} shares at ${t['price']:,.2f} ({W.money(t['notional'])})"


def record(decision):
    d = (W.read_json(DECISIONS, {}) or {}).get("decisions") or []
    d.append(decision)
    W.write_json(DECISIONS, {"decisions": d[-DECISIONS_KEPT:]})


def decide(sym, kind, event, alerts, book, now, ask, build=None, new_report=None, get=None):
    """Warren's check first, then the evidence, then the panel, then code's
    trade. The decision, or None when there was no check to be had (tried
    again)."""
    check, how = check_for(sym, now, value(book) * BUY_PCT / 100, build, new_report)
    if not check:
        return None, how
    count("meetings", now, bump=True)
    ev = evidence(sym, check, book, alerts, now, get)
    r1, qs, final, outcome, note = meet(sym, event, check, book, ask, ev)
    did = f"{now.strftime('%Y%m%d%H%M')}-{sym}"
    price = (book.get("marks") or {}).get(sym)
    trade, result = act(book, sym, outcome, check, now, did)
    d = {"id": did, "utc": iso(now), "symbol": sym, "kind": kind, "event": event,
         "alerts": [{k: a.get(k) for k in ("id", "title", "importance", "signal", "link", "publisher")} for a in alerts],
         "check": {"when_utc": check.get("when_utc"), "decision": check.get("decision"), "how": how},
         "lines": lines_of(check), "evidence": ev, "price": price, "round1": r1, "questions": qs, "final": final,
         "outcome": outcome, "note": note, "result": result, "trade": trade}
    record(d)
    return d, ""


# ------------------------------------------------------------------ the run

def watchlist():
    return (W.read_json(WATCHLIST, {}) or {}).get("stocks") or []


def watched(book):
    return sorted({p["symbol"] for p in book["positions"]} | {x["symbol"] for x in watchlist()})


def refresh_marks(book, price_fn=None):
    syms = watched(book)
    got = (price_fn or W.prices)(syms) if syms else {}
    marks = book.setdefault("marks", {})
    for s in syms:
        p = (got.get(s) or {}).get("price")
        if not p:
            p = ((W.latest(s) or {}).get("numbers") or {}).get("price")
        if p:
            marks[s] = p
    return book


def important_alerts(book, now):
    """High and Major alerts on the book's stocks from Warren's watcher and
    the book's own, not yet put to the panel, at most ALERT_MAX_AGE_H old."""
    handled = set(book.get("handled") or [])
    mine = set(watched(book))
    out = []
    for path in (W.ALERTS, BOOK_ALERTS):
        for a in (W.read_json(path, {}) or {}).get("alerts") or []:
            t = parse(a.get("found_utc"))
            key = f"{a['id']}:{a['symbol']}"
            if (a.get("importance") or 0) >= IMPORTANT and a["symbol"] in mine and key not in handled \
                    and t and now - t <= timedelta(hours=ALERT_MAX_AGE_H):
                out.append(dict(a, key=key))
    seen, uniq = set(), []
    for a in out:
        if a["key"] not in seen:
            seen.add(a["key"])
            uniq.append(a)
    return uniq


def news_event(alerts):
    return "\n".join(f"[{W.IMPORTANCE.get(a.get('importance'), '?')}] {a['title']} ({a.get('publisher') or '?'}, "
                     f"{a.get('published') or '?'}) - Warren's news reader marked it {a['signal']}: {a['why']}"
                     for a in alerts)


def panel_ask(q, role=None, now=None, or_call=None, prices=None, key=None, budget_read=None, sonnet=None):
    """(answered, text, which model). Bull and Risk manager: Sonnet on the
    plan. A seat in SEAT_MODELS: that model through OpenRouter - or Sonnet
    standing in, said so, when it cannot be asked."""
    sonnet = sonnet or (lambda q: _wake().ask("warren-book", q, PANEL_TIMEOUT))
    model = SEAT_MODELS.get(role)
    if not model:
        return tuple(sonnet(q)) + ("Sonnet",)
    ok, text, why = or_ask(model, q, now, or_call, prices, key, budget_read)
    if ok:
        return True, text, model
    ok, text = sonnet(q)
    return ok, text, f"Sonnet (standing in: {why})"


def or_spent(day):
    return float((W.read_json(SPEND, {}) or {}).get(day) or 0.0)


def or_ask(model, q, now=None, or_call=None, prices=None, key=None, budget_read=None):
    """(answered, text, why not). One OpenRouter question, its worst case
    checked first against the book's own day and the shared one."""
    now = now or datetime.now(timezone.utc)
    orc = _load("book_or_chat", "or_chat.py")
    day = now.strftime("%Y-%m-%d")             # OpenRouter counts its day in UTC (budget.py)
    try:
        prices = prices if prices is not None else orc.catalogue(MODELS_CACHE)
    except Exception as e:
        return False, "", f"OpenRouter's price list could not be read ({str(e)[:80]})"
    if model not in prices:
        return False, "", f"{model} is not a model OpenRouter has - change SEAT_MODELS in warren-book.py"
    worst = orc.worst_case(prices, model, OR_SYSTEM + q, OR_MAX_TOKENS)
    caps = W.read_json(LIMITS, {}) or {}
    cap, keep = float(caps.get("warren_book_daily_ai", 0)), float(caps.get("warren_book_leave_for_others", 0.5))
    if worst is None or or_spent(day) + worst > cap + 1e-9:
        return False, "", f"the book's ${cap:.2f} a day on OpenRouter is used"
    try:
        left = (budget_read or _load("book_budget", "budget.py").read)().get("ai_left_today")
    except Exception:
        left = None
    if not isinstance(left, (int, float)) or left - worst < keep:
        return False, "", "the shared OpenRouter day is too low (or could not be read)"
    key = key if key is not None else _load("book_preflight", "preflight.py").openrouter_key()
    if not key:
        return False, "", "no OpenRouter key found"
    try:
        data = (or_call or (lambda: orc.openrouter_call(key, model, OR_SYSTEM, q, OR_MAX_TOKENS, title="warren-book",
                                                       max_price=orc.ceiling(prices, model))))()
    except Exception as e:
        return False, "", f"OpenRouter: {str(e)[:100]}"
    text, _, usage = orc.reply_text(data)
    cost = orc.ea().cost_of(usage)
    d = W.read_json(SPEND, {}) or {}
    d[day] = round(float(d.get(day) or 0.0) + (cost if cost is not None else worst), 6)     # unknown counts as the worst
    W.write_json(SPEND, dict(sorted(d.items())[-60:]))
    return (True, text, "") if text else (False, "", "an empty reply")


def _wake():
    return _load("wake_warren_book", "wake.py")


def tick(now=None, ask=None, news=None, build=None, new_report=None, price_fn=None, get=None):
    """One run of the timer. Returns what it did, in words and counts."""
    now = now or datetime.now(timezone.utc)
    book = ensure_book(now)
    refresh_marks(book, price_fn)
    status = {"run_utc": iso(now), "meetings": 0, "trades": 0, "notes": []}
    own = {h["symbol"] for h in W.holdings()}
    extra = [s for s in watched(book) if s not in own]
    if extra:
        try:
            status["news"] = (news or W.watch_news)(now, symbols=extra, alerts_path=BOOK_ALERTS,
                                                     seen_path=BOOK_NEWS_SEEN)
        except Exception as e:
            status["notes"].append(f"the book's own news watch failed ({str(e)[:100]})")
    by_sym = {}
    for a in important_alerts(book, now):
        by_sym.setdefault(a["symbol"], []).append(a)
    for sym, alerts in sorted(by_sym.items(), key=lambda kv: -max(a.get("importance") or 0 for a in kv[1])):
        if count("meetings", now) >= MEETINGS_PER_DAY:
            status["notes"].append(f"{MEETINGS_PER_DAY} meetings already today - {sym} waits for the next run")
            continue
        d, why = decide(sym, "news", news_event(alerts), alerts, book, now, ask or panel_ask, build, new_report, get)
        if not d:
            status["notes"].append(f"{sym}: {why}")
            continue
        book.setdefault("handled", []).extend(a["key"] for a in alerts)
        book["handled"] = book["handled"][-2000:]
        status["meetings"] += 1
        status["trades"] += bool(d["trade"])
        W.write_json(BOOK, book)
    snapshot(book, now)
    book["last"] = status
    W.write_json(BOOK, book)
    return status


def snapshot(book, now):
    h = book.setdefault("history", [])
    h.append({"utc": iso(now), "value": value(book), "baseline": baseline(book)})
    book["history"] = h[-HISTORY_KEPT:]


# ------------------------------------------------------------------ ideas

def ideas_question(book):
    have = ", ".join(watched(book)) or "nothing yet"
    return ("You look for new stocks for a beginner's PAPER trading book (practice money). Search the web for "
            f"up to {IDEAS_PER_WEEK} US-listed stocks worth putting through a full checklist this week. Judge them in "
            "this order: strong and growing free cash flow; revenue growing over the last three years; debt under "
            "control and borrowing that pays for growth, not losses; a profit margin earned by the business "
            "itself; a price that is reasonable against its rivals. Any size and any risk level is fine, but "
            "not a fund, a preferred share or a partnership (MLP). " + FRACTIONAL + "\n\n"
            f"Leave out what the book already holds or watches: {have}.\n\n"
            'Answer with JSON only: {"ideas": [{"symbol": "TICKER", "why": "one or two plain sentences, no '
            'numbers"}]}. Warren will check every figure himself; give no figures.')


def read_ideas(text, tickers, exclude):
    t = (text or "").strip()
    a, b = t.find("{"), t.rfind("}")
    try:
        d = json.loads(t[a:b + 1])
    except (ValueError, TypeError):
        return [], ["no readable JSON"]
    out, problems = [], []
    for x in (d.get("ideas") or [])[:10] if isinstance(d, dict) else []:
        if len(out) >= IDEAS_PER_WEEK:
            break
        sym = str((x or {}).get("symbol") or "").upper().strip()
        why = str((x or {}).get("why") or "").strip()[:300]
        if not W.SYMBOL_RE.match(sym) or W.cik_for(sym, tickers)[0] is None:
            problems.append(f"{sym or '?'} is not a ticker the SEC lists")
        elif sym in exclude or sym in {o["symbol"] for o in out}:
            problems.append(f"{sym} is already held or watched")
        elif not why or re.search(r"\d", why):
            problems.append(f"{sym}: the reason is empty or has numbers")
        else:
            out.append({"symbol": sym, "why": why})
    return out, problems


def ideas(now=None, ask=None, panel=None, build=None, new_report=None, get=None, price_fn=None):
    now = now or datetime.now(timezone.utc)
    book = ensure_book(now)
    log = (W.read_json(IDEAS_LOG, {}) or {}).get("runs") or []
    run = {"utc": iso(now), "ideas": [], "problems": [], "note": ""}
    if W.runs_today() >= W.RUNS_PER_DAY:
        run["note"] = f"Warren's {W.RUNS_PER_DAY} Opus runs today are used - the search waits for next week"
    else:
        W.runs_today(bump=True)
        ok, text = (ask or (lambda q: _wake().ask("warren-ideas", q, W.ASK_TIMEOUT, tools=("WebSearch",))))(
            ideas_question(book))
        if not ok:
            run["note"] = f"Opus could not answer ({str(text)[:120]})"
        else:
            found, run["problems"] = read_ideas(text, W.sec_tickers(get), set(watched(book)))
            for x in found:
                add_watch(x["symbol"], x["why"], "ideas", now)
                refresh_marks(book, price_fn)
                d, why = decide(x["symbol"], "idea", f"Opus suggested {x['symbol']} for the book this week: {x['why']}",
                                [], book, now, panel or panel_ask, build, new_report, get)
                run["ideas"].append({"symbol": x["symbol"], "why": x["why"],
                                     "decision": d["id"] if d else None, "result": d["result"] if d else why})
                W.write_json(BOOK, book)
    snapshot(book, now)
    W.write_json(BOOK, book)
    log.append(run)
    W.write_json(IDEAS_LOG, {"runs": log[-100:]})
    return run


# ------------------------------------------------------------------ the page

def add_watch(sym, why, by, now=None):
    sym = sym.upper()
    if not W.SYMBOL_RE.match(sym):
        raise SystemExit("a ticker is letters, like KMI")
    st = watchlist()
    if not any(x["symbol"] == sym for x in st):
        st.append({"symbol": sym, "why": (why or "").strip()[:300], "by": by,
                   "added_utc": iso(now or datetime.now(timezone.utc))})
        W.write_json(WATCHLIST, {"stocks": st})
    return st


def remove_watch(sym):
    st = [x for x in watchlist() if x["symbol"] != sym.upper()]
    W.write_json(WATCHLIST, {"stocks": st})
    return st


def board():
    book = load_book()
    if book is None:
        return {"started": False, "start_cash": START_CASH, "max_pct": MAX_PCT, "watchlist": watchlist()}
    v = value(book)
    rows = []
    for p in sorted(book["positions"], key=lambda p: -p["shares"] * (mark_of(book, p["symbol"]) or 0)):
        m = mark_of(book, p["symbol"])
        pv = p["shares"] * (m or 0)
        c = W.latest(p["symbol"]) or {}
        rows.append({"symbol": p["symbol"], "shares": p["shares"], "price": m, "value": round(pv, 2),
                     "cost": p["cost"], "gain": round(pv - p["cost"], 2), "gain_pct": pct(pv - p["cost"], p["cost"]),
                     "weight": pct(pv, v), "over_limit": pct(pv, v) > MAX_PCT, "from": p.get("from"),
                     "decision": c.get("decision"), "checked": (c.get("when_utc") or "")[:10], "lines": lines_of(c)})
    watch = []
    for x in watchlist():
        c = W.latest(x["symbol"]) or {}
        watch.append(dict(x, decision=c.get("decision"), checked=(c.get("when_utc") or "")[:10], lines=lines_of(c),
                          held=bool(held(book, x["symbol"]))))
    trades = list(reversed(book["trades"]))
    decisions = list(reversed((W.read_json(DECISIONS, {}) or {}).get("decisions") or []))[:40]
    base = baseline(book)
    return {"started": True, "mode": "PAPER", "started_utc": book["started_utc"], "start_cash": book["start_cash"],
            "cash": book["cash"], "value": v, "baseline": base, "vs_baseline": round(v - base, 2),
            "max_pct": MAX_PCT, "buy_pct": BUY_PCT, "positions": rows, "watchlist": watch, "trades": trades[:60],
            "unseen": sum(not t.get("seen") for t in book["trades"]), "decisions": decisions,
            "history": book.get("history", [])[-200:], "last": book.get("last"),
            "ideas": ((W.read_json(IDEAS_LOG, {}) or {}).get("runs") or [])[-5:][::-1],
            "seed_notes": book.get("seed_notes") or [], "problems": problems(book),
            "today": {k: count(k) for k in ("meetings", "checks", "trades")},
            "caps": {"meetings": MEETINGS_PER_DAY, "checks": CHECKS_PER_DAY, "trades": TRADES_PER_DAY}}


def mark_seen():
    book = load_book()
    if book:
        for t in book["trades"]:
            t["seen"] = True
        W.write_json(BOOK, book)
    return {"ok": True}


def unseen_trades():
    """For the village badge (scripts/attention.py)."""
    book = load_book() or {"trades": []}
    return [t for t in book["trades"] if not t.get("seen")]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick")
    sub.add_parser("ideas")
    sub.add_parser("board")
    sub.add_parser("check")
    sub.add_parser("seen")
    w = sub.add_parser("watch")
    w.add_argument("action", choices=("add", "remove"))
    w.add_argument("symbol")
    w.add_argument("--why", default="")
    a = ap.parse_args(argv)
    if a.cmd == "tick":
        print(json.dumps(tick()))
    elif a.cmd == "ideas":
        print(json.dumps(ideas()))
    elif a.cmd == "board":
        print(json.dumps(board()))
    elif a.cmd == "seen":
        print(json.dumps(mark_seen()))
    elif a.cmd == "watch":
        st = add_watch(a.symbol, a.why, "owner") if a.action == "add" else remove_watch(a.symbol)
        print(json.dumps({"watchlist": st}))
    elif a.cmd == "check":
        book = load_book()
        p = problems(book) if book else ["the book has not started yet"]
        print("\n".join(p) or "the book balances")
        return 1 if p and book else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
