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
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
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

ROLES = {
    "bull": ("Bull", "You make the strongest honest case for buying more of this stock or keeping it. You still "
             "say hold or sell when the facts are against it - a bull who never changes his mind is no use."),
    "bear": ("Bear", "You make the strongest honest case for selling or trimming this stock, or for not buying "
             "it. You still say hold or buy when the facts are against you."),
    "risk": ("Risk manager", "You judge what this trade would do to the whole book: how much of it would sit in "
             "one stock or one theme, how much cash is left, how it overlaps with the other holdings, and "
             "whether the news is real enough to act on. You prefer doing nothing to acting on noise."),
}
VOTES = ("buy", "hold", "sell")


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
               "stock; a sell sells half, or all of it when Warren's check says Skip.")
    return "\n".join(out)


# ---------------------------------------------------------------- the panel

def panel_question(role, sym, rc, lines, event, others=None):
    name, brief = ROLES[role]
    sells = "\n".join(f"  sell {i}. I would sell if {r}" for i, r in enumerate(lines["sell"], 1)) or "  (none saved)"
    q = (f"You sit on a three-member panel that runs a PAPER trading book for a beginner investor - practice money, "
         f"no real trades. Your role: {name}. {brief}\n\n"
         f"WHAT HAPPENED\n{event}\n\n"
         f"WARREN'S CHECKLIST RECAP (computed by code from SEC filings - these are the only figures you may use)\n{rc}\n\n"
         f"THE STOCK'S OWN LINES, from Warren's check\n  What would make me buy: {lines['buy'] or '(none saved)'}\n"
         f"  What would make me sell:\n{sells}\n\n")
    if others:
        q += ("ROUND 2. The other two members answered round 1 like this:\n"
              + "\n".join(f"  {ROLES[r][0]}: {v['vote']} - {v['why']}" for r, v in others.items())
              + "\nRead their points, then give your final vote. Change your mind only if their points are better "
                "than yours. Only a vote all three agree on is traded.\n\n")
    else:
        q += "ROUND 1. Answer on your own; you will see the others' answers in round 2.\n\n"
    q += ('Answer with JSON only: {"vote": "buy", "hold" or "sell", "line": "buy" if the news meets the buy line, '
          '"sell N" for the sell line it hits, or "none", "confidence": 1 to 5, "why": "two or three plain '
          'sentences for a beginner"}. Use no number in "why" that is not in the recap or in what happened.')
    return q


def _numbers(text):
    return {x.rstrip(".,") for x in re.findall(r"\d[\d.,]*", text or "")}


def read_vote(text, allowed, n_sell):
    """The vote code can stand behind, or None: valid JSON, buy/hold/sell, a
    line that exists, a reason with no number the recap or the news lacks."""
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
    try:
        conf = int(d.get("confidence"))
    except (TypeError, ValueError):
        conf = None
    return {"vote": vote, "line": line, "confidence": conf if conf in range(1, 6) else None, "why": why}


def meet(sym, event, check, book, ask):
    """Two rounds, three members. ({round 1}, {round 2}, outcome, note)."""
    rc, lines = recap(check, book, sym), lines_of(check)
    allowed = rc + "\n" + event
    rounds = []
    for r in (1, 2):
        got = {}
        for role in ROLES:
            others = {k: v for k, v in rounds[0].items() if k != role} if r == 2 else None
            ok, text = ask(panel_question(role, sym, rc, lines, event, others))
            v = read_vote(text, allowed, len(lines["sell"])) if ok else None
            got[role] = v or {"vote": "hold", "line": "none", "confidence": None, "failed": True,
                              "why": "(no answer code could stand behind - counted as hold)"}
        rounds.append(got)
    final = [v["vote"] for v in rounds[1].values()]
    if len(set(final)) == 1 and not any(v.get("failed") for v in rounds[1].values()):
        return rounds[0], rounds[1], final[0], "all three agree"
    tally = {x: final.count(x) for x in VOTES if final.count(x)}
    return rounds[0], rounds[1], "hold", ("split " + ", ".join(f"{n} {x}" for x, n in tally.items())
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


def decide(sym, kind, event, alerts, book, now, ask, build=None, new_report=None):
    """Warren's check first, then the panel, then code's trade. The
    decision, or None when there was no check to be had (tried again)."""
    check, how = check_for(sym, now, value(book) * BUY_PCT / 100, build, new_report)
    if not check:
        return None, how
    count("meetings", now, bump=True)
    r1, r2, outcome, note = meet(sym, event, check, book, ask)
    did = f"{now.strftime('%Y%m%d%H%M')}-{sym}"
    trade, result = act(book, sym, outcome, check, now, did)
    d = {"id": did, "utc": iso(now), "symbol": sym, "kind": kind, "event": event, "alerts": alerts,
         "check": {"when_utc": check.get("when_utc"), "decision": check.get("decision"), "how": how},
         "lines": lines_of(check), "round1": r1, "round2": r2, "outcome": outcome, "note": note,
         "result": result, "trade": trade}
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


def panel_ask(q):
    return _wake().ask("warren-book", q, PANEL_TIMEOUT)


def _wake():
    return _load("wake_warren_book", "wake.py")


def tick(now=None, ask=None, news=None, build=None, new_report=None, price_fn=None):
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
        d, why = decide(sym, "news", news_event(alerts), [{k: a.get(k) for k in ("id", "title", "importance", "signal",
                                                                                 "link", "publisher")}
                                                         for a in alerts], book, now, ask or panel_ask, build, new_report)
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
            "not a fund, a preferred share or a partnership (MLP).\n\n"
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
                                [], book, now, panel or panel_ask, build, new_report)
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
