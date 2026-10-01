#!/usr/bin/env python3
"""
belfort-trade.py — apply a buy or sell to the paper portfolio, in code.

Belfort decides WHAT to trade. This does the arithmetic, because arithmetic
is not a judgement call and a model doing it approximately is how $3,731.90
disappeared: three positions were closed by setting shares to 0 and the
proceeds were never added to cash. The account read -41% while the actual
trading loss was under 4%.

    belfort-trade.py buy  MRVL 8 --price 237.47 --reason "TYPE: the fact"
    belfort-trade.py sell MU   2 --price 927.60 --reason "stop loss -10%"
    belfort-trade.py show
    belfort-trade.py exits [--apply]   # the sell rules, in code; --apply sells what is due
    belfort-trade.py mark        # end of cycle: re-mark, bump cycle_count, stamp the file
    belfort-trade.py shadow      # the no-AI book's turn: same rules, top candidate, no judgement
    belfort-trade.py stats [--json]    # both books against each other and QQQ

Every trade is validated against the rules before it is applied:
  * you cannot spend cash you do not have
  * you cannot sell shares you do not hold
  * no single position may exceed 25% of portfolio value
  * at least 5% of the portfolio must stay in cash after a buy
  * no more than 40% in one cluster of related names (CLUSTERS)
  * no more than 8 names at once
  * the market regime (regime()): no buys when it is unfavourable, half-size
    ones when it is mixed
  * Belfort's buys cite a headline from news.json by its id (--headline)

A refused trade changes nothing and says why.

THE SELL RULES are code too (exit_check). They lived only in Belfort's
instructions until 2026-10-01, and the "+8%: protect it" rule could not be
followed at all: nothing remembered how high a position had been. The
fetcher now keeps each name's daily closes, and the highest close since
entry is that memory. belfort-cycle.sh runs `exits --apply` before Belfort
wakes; he is told what was sold and judges only a broken thesis.
Standard library only.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
STATE = ROOT / "agents" / "belfort" / "state" / "portfolio.json"
# The no-AI book: the same rules and exits, the top candidate instead of a
# judgement. The only way to learn whether the judgement adds anything.
SHADOW = ROOT / "agents" / "belfort" / "state" / "shadow.json"
EQUITY = ROOT / "agents" / "belfort" / "state" / "equity.jsonl"
QUOTES = ROOT / "agents" / "belfort" / "data" / "quotes.json"
NEWS = ROOT / "agents" / "belfort" / "data" / "news.json"
CANDIDATES = ROOT / "agents" / "belfort" / "data" / "candidates.json"

MAX_POSITION_PCT = 25.0
MIN_CASH_PCT = 5.0          # the owner lowered it from 15% on 2026-10-01

# The exit rules, in percent from cost.
STOP_LOSS = -10.0           # close, no exceptions, no averaging down
TAKE_PROFIT = 25.0          # close
PROTECT_AT = 8.0            # once a position has been up this much...
TRAIL = 10.0                # ...its stop follows the highest price, this far below,
                            #    and never below what was paid
DEAD_DAYS, DEAD_BAND = 5, 2.0   # 5+ trading days within +/-2%: dead money

# Names that move together, so a cap on one name is not a cap on one bet:
# NVDA, AMD and MU at 20% each is 60% in one trade. A name not listed is its
# own cluster.
CLUSTERS = {
    "semiconductors": ["NVDA", "AMD", "AVGO", "MU", "TSM", "SMCI", "ARM", "QCOM", "INTC", "MRVL"],
    "software and security": ["PLTR", "NET", "DDOG", "SNOW", "CRWD", "ZS", "PANW", "MDB"],
    "mega-cap": ["META", "GOOGL", "AMZN", "MSFT", "AAPL"],
    "crypto and high-beta": ["COIN", "HOOD", "TSLA"],
    "consumer internet": ["SHOP", "UBER", "ABNB", "RBLX"],
}
CLUSTER_CAP_PCT = 40.0
MAX_POSITIONS = 8

# The market regime (regime()). Mixed: a new buy may bring the position to
# this much of the portfolio - half the usual ~20%. Unfavourable: none.
NEUTRAL_POSITION_PCT = 10.0
BREADTH_MIN = 0.5           # share of the 30 above their own 50-day average

HEADLINE_MAX_AGE_DAYS = 7
SHADOW_POSITION_PCT = 20.0


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load(path=None):
    path = path or STATE
    try:
        p = json.loads(path.read_text())
    except Exception as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    p.setdefault("positions", [])
    p.setdefault("trades", [])
    p.setdefault("cash", 0.0)
    p.setdefault("starting_cash", 10000)
    return p


def save(p, path=None):
    p["last_cycle_utc"] = now()
    (path or STATE).write_text(json.dumps(p, indent=2, sort_keys=True) + "\n")


def quotes_doc():
    try:
        return json.loads(QUOTES.read_text())
    except Exception:
        return {}


def regime(doc=None):
    """(state, why) - "favorable", "neutral", "unfavorable", or "unknown"
    (no QQQ data yet: treated as neutral). From QQQ against its own 50-day
    average and that average's direction over 10 days, and breadth: the share
    of the 30 names above their own 50-day average."""
    doc = doc if doc is not None else quotes_doc()
    qqq = (doc.get("benchmarks") or {}).get("QQQ") or {}
    closes = [float(c) for _, c in qqq.get("history") or []]
    if len(closes) < 60 or qqq.get("price") is None:
        return "unknown", "no QQQ history yet - treated as mixed"
    sma_now, sma_then = sum(closes[-50:]) / 50, sum(closes[-60:-10]) / 50
    above, rising = float(qqq["price"]) > sma_now, sma_now >= sma_then
    names = [q for q in (doc.get("quotes") or {}).values() if q.get("sma50") and q.get("price")]
    breadth = (sum(1 for q in names if float(q["price"]) > float(q["sma50"])) / len(names)) if names else None
    why = (f"QQQ {'above' if above else 'below'} its {'rising' if rising else 'falling'} 50-day average"
           + (f"; {breadth:.0%} of the names above theirs" if breadth is not None else ""))
    if (not above and not rising) or (breadth is not None and breadth < BREADTH_MIN):
        return "unfavorable", why
    if not above or not rising:
        return "neutral", why
    return "favorable", why


def cluster_of(symbol):
    sym = symbol.upper()
    return next((name for name, members in CLUSTERS.items() if sym in members), sym)


def headline(hid, symbol, now_ts=None):
    """The news.json headline a buy cites, checked: it exists, it is from
    this name's own feed, and it is under HEADLINE_MAX_AGE_DAYS old. Raises
    ValueError saying which."""
    from email.utils import parsedate_to_datetime
    try:
        items = json.loads(NEWS.read_text())["headlines"]
    except Exception:
        raise ValueError("no news.json to cite a headline from")
    h = next((x for x in items if x.get("id") == hid), None)
    if not h:
        raise ValueError(f"no headline {hid!r} in news.json - cite one by its \"id\"")
    if str(h.get("symbol", "")).upper() != symbol.upper():
        raise ValueError(f"headline {hid} is from {h.get('symbol')}'s news, not {symbol.upper()}'s")
    try:
        age = (now_ts or datetime.now(timezone.utc).timestamp()) - parsedate_to_datetime(h["published"]).timestamp()
    except Exception:
        raise ValueError(f"headline {hid} has no readable date")
    if age > HEADLINE_MAX_AGE_DAYS * 86400:
        raise ValueError(f"headline {hid} is {age / 86400:.0f} days old - a catalyst is news, "
                         f"under {HEADLINE_MAX_AGE_DAYS} days")
    return {k: h.get(k) for k in ("id", "title", "published", "publisher", "symbol")}


def quote(symbol):
    try:
        return json.loads(QUOTES.read_text())["quotes"][symbol.upper()]
    except Exception:
        return None


def exit_check(pos, q):
    """(rule, reason, stop_price) for one open position against its quote;
    rule is None when nothing is due. stop_price is where it is sold: the
    -10% stop, or - once it has been up 8% - TRAIL below its highest price,
    never below what was paid."""
    cb = float(pos.get("cost_basis") or 0)
    if not q or not cb or q.get("price") is None:
        return None, "no quote", None
    px = float(q["price"])
    entry_day = str(pos.get("entry_utc") or "")[:10]
    # No known entry date: the closes say nothing about this position, and a
    # high from before it was bought must never sell it.
    history = (q.get("history") or []) if entry_day else []
    peak = max([float(c) for d, c in history if d >= entry_day] + [px])
    # Rounded before comparing: 90 against a cost of 100 is -9.999999999999998%
    # in floating point, and a stop at exactly -10% would never fire.
    pnl = round((px / cb - 1) * 100, 6)
    peak_pnl = round((peak / cb - 1) * 100, 6)
    stop = round(cb * (1 + STOP_LOSS / 100), 2)
    if peak_pnl >= PROTECT_AT:
        stop = round(max(cb, peak * (1 - TRAIL / 100)), 2)
    held = sum(1 for d, _ in history if d > entry_day)

    if pnl <= STOP_LOSS:
        return "stop", f"STOP LOSS: {pnl:+.1f}% from cost ${cb:,.2f}", stop
    if pnl >= TAKE_PROFIT:
        return "take", f"TAKE PROFIT: {pnl:+.1f}% from cost ${cb:,.2f}", stop
    if peak_pnl >= PROTECT_AT and px <= stop:
        return "protect", (f"PROTECT GAIN: was up {peak_pnl:+.1f}% (high ${peak:,.2f}), now {pnl:+.1f}%, "
                           f"at or under its stop ${stop:,.2f}"), stop
    if held >= DEAD_DAYS and abs(pnl) <= DEAD_BAND:
        return "dead", f"DEAD MONEY: {pnl:+.1f}% after {held} trading days", stop
    return None, f"{pnl:+.1f}%, stop ${stop:,.2f}", stop


def entry_of(p, pos):
    """The position with its entry date: its own, or its latest BUY's."""
    if pos.get("entry_utc"):
        return pos
    buys = [t.get("utc") for t in p.get("trades", [])
            if str(t.get("side", "")).upper() == "BUY" and str(t.get("symbol", "")).upper() == pos["symbol"].upper()]
    return dict(pos, entry_utc=max(buys)) if any(buys) else pos


def exits_due(p):
    """[(symbol, rule, reason)] for every open position whose exit is due."""
    out = []
    for pos in p["positions"]:
        if float(pos.get("shares") or 0) <= 0:
            continue
        rule, reason, _ = exit_check(entry_of(p, pos), quote(pos["symbol"]))
        if rule:
            out.append((pos["symbol"], rule, reason))
    return out


def last_price(symbol, fallback=None):
    """Prices come from the fetcher's file, never from an argument alone, so a
    trade cannot be booked at a price nobody observed."""
    try:
        q = json.loads(QUOTES.read_text())["quotes"][symbol.upper()]
        return float(q["price"])
    except Exception:
        return fallback


def book_gap(p):
    """Cash is a closed system:  starting - bought + sold == cash.

    Returns (gap, bought, sold, expected, actual). belfort-verify.py imports
    this rather than restating the formula - two copies that disagree would be
    worse than no check at all, because one of them would keep saying fine.
    """
    bought = sold = 0.0
    unknown = []
    for t in p.get("trades", []):
        side = str(t.get("side", "")).upper()
        notional = t.get("notional")
        notional = float(notional) if notional is not None else (
            float(t.get("shares") or 0) * float(t.get("price") or 0))
        if side == "BUY":
            bought += notional
        elif side == "SELL":
            sold += notional
        else:
            unknown.append(t)
    start = float(p.get("starting_cash") or 10000.0)
    expected = start - bought + sold
    actual = float(p.get("cash") or 0)
    return actual - expected, bought, sold, expected, actual, unknown


def cmd_check(a):
    """Does the book balance? Exit 0 yes, 1 no. Meant for scripts."""
    p = load()
    gap, bought, sold, expected, actual, unknown = book_gap(p)
    for t in unknown:
        print(f"trade with no recognisable side: {t!r}", file=sys.stderr)
    if abs(gap) <= 0.01 and not unknown:
        print(f"book balances: ${float(p['starting_cash']):,.2f} - ${bought:,.2f} "
              f"+ ${sold:,.2f} = ${actual:,.2f}")
        return 0
    print(f"BOOK DOES NOT BALANCE: expected ${expected:,.2f}, cash reads "
          f"${actual:,.2f} - ${gap:+,.2f} "
          f"{'invented' if gap > 0 else 'destroyed'}", file=sys.stderr)
    return 1


def position(p, symbol):
    for pos in p["positions"]:
        if pos.get("symbol", "").upper() == symbol.upper():
            return pos
    return None


def market_value(p):
    total = float(p["cash"])
    for pos in p["positions"]:
        sh = float(pos.get("shares") or 0)
        if sh <= 0:
            continue
        px = last_price(pos["symbol"], pos.get("cost_basis"))
        total += sh * float(px or 0)
    return total


def cmd_show(a):
    print(summary(load()))
    return 0


def cmd_buy(a):
    p = load()
    try:
        cited = headline(a.headline, a.symbol) if a.headline else None
    except ValueError as exc:
        print(f"REFUSED: {exc}.", file=sys.stderr)
        return 1
    if not cited:
        print("REFUSED: a buy cites its catalyst: --headline ID, the \"id\" of a headline in "
              "data/news.json from this name's own news.", file=sys.stderr)
        return 1
    code = buy(p, a.symbol, int(a.shares), a.price, a.reason, a.score, cited)
    if code == 0:
        save(p)
    return code


def buy(p, symbol, shares, price=None, reason="", score=0, cited=None, state=None):
    """Apply one purchase to p in memory, if every rule allows it; the
    caller saves. 0, or 1 refused with the reason printed. The one place
    the buying rules live - Belfort's book and the shadow book both use it."""
    sym = symbol.upper()
    px = price if price is not None else last_price(sym)
    if px is None:
        print(f"no price for {sym} in quotes.json and none given - refusing.", file=sys.stderr)
        return 1
    if shares <= 0:
        print("shares must be positive.", file=sys.stderr)
        return 1
    state, why = state or regime()
    if state == "unfavorable":
        print(f"REFUSED: the market regime is unfavourable ({why}) - no new buys.", file=sys.stderr)
        return 1

    cost = shares * float(px)
    if cost > float(p["cash"]):
        print(f"REFUSED: that costs ${cost:,.2f} and cash is ${float(p['cash']):,.2f}.", file=sys.stderr)
        return 1

    mv = market_value(p)
    existing = position(p, sym)
    held = [x for x in p["positions"] if float(x.get("shares") or 0) > 0]
    if not existing and len(held) >= MAX_POSITIONS:
        print(f"REFUSED: {len(held)} names held, the most is {MAX_POSITIONS}.", file=sys.stderr)
        return 1
    held_value = float(existing["shares"]) * float(px) if existing and float(existing.get("shares") or 0) > 0 else 0
    cap = MAX_POSITION_PCT if state == "favorable" else NEUTRAL_POSITION_PCT
    if mv and (held_value + cost) / mv * 100 > cap:
        print(f"REFUSED: {sym} would be "
              f"{(held_value + cost) / mv * 100:.1f}% of the portfolio, cap is {cap:.0f}%"
              + ("" if state == "favorable" else f" while the regime is {state} ({why})") + ".",
              file=sys.stderr)
        return 1
    if mv and (float(p["cash"]) - cost) / mv * 100 < MIN_CASH_PCT:
        print(f"REFUSED: that would leave "
              f"{(float(p['cash']) - cost) / mv * 100:.1f}% cash, minimum is {MIN_CASH_PCT:.0f}%.",
              file=sys.stderr)
        return 1
    group = cluster_of(sym)
    in_group = sum(float(x["shares"]) * float(last_price(x["symbol"], x.get("cost_basis")) or 0)
                   for x in held if cluster_of(x["symbol"]) == group)
    if mv and (in_group + cost) / mv * 100 > CLUSTER_CAP_PCT:
        print(f"REFUSED: {group} would be {(in_group + cost) / mv * 100:.1f}% of the portfolio, "
              f"cap is {CLUSTER_CAP_PCT:.0f}% for one cluster.", file=sys.stderr)
        return 1

    p["cash"] = round(float(p["cash"]) - cost, 2)
    if existing and float(existing.get("shares") or 0) > 0:
        old_sh = float(existing["shares"])
        existing["cost_basis"] = round(
            (old_sh * float(existing["cost_basis"]) + cost) / (old_sh + shares), 2)
        existing["shares"] = old_sh + shares
    else:
        p["positions"] = [x for x in p["positions"] if x.get("symbol", "").upper() != sym]
        p["positions"].append({"symbol": sym, "shares": shares, "cost_basis": round(float(px), 2),
                               "entry_utc": now(), "reason": reason or "", "score": score,
                               "catalyst": cited, "regime": state})
    p["trades"].append({"cycle": p.get("cycle_count", 0), "side": "BUY", "symbol": sym,
                        "shares": shares, "price": round(float(px), 2),
                        "notional": round(cost, 2), "utc": now(), "reason": reason or "",
                        "catalyst": cited, "regime": state})
    print(f"BOUGHT {shares} {sym} @ ${float(px):,.2f} = ${cost:,.2f}   cash now ${float(p['cash']):,.2f}")
    return 0


def cmd_sell(a):
    p = load()
    code = sell(p, a.symbol, a.shares, a.price, a.reason)
    if code == 0:
        save(p)
    return code


def sell(p, symbol, shares_arg="all", price=None, reason=""):
    """Apply one sale to p in memory; the caller saves. 0, or 1 if refused."""
    sym = symbol.upper()
    pos = position(p, sym)
    held = float(pos.get("shares") or 0) if pos else 0
    if held <= 0:
        print(f"REFUSED: no open position in {sym}.", file=sys.stderr)
        return 1

    shares = int(held) if shares_arg in (None, "all") else int(shares_arg)
    if shares <= 0 or shares > held:
        print(f"REFUSED: you hold {held:.0f} {sym}, cannot sell {shares}.", file=sys.stderr)
        return 1

    px = price if price is not None else last_price(sym)
    if px is None:
        print(f"no price for {sym} in quotes.json and none given - refusing.", file=sys.stderr)
        return 1

    proceeds = shares * float(px)
    cb = float(pos.get("cost_basis") or 0)
    realised = (float(px) - cb) * shares

    # THE BUG THIS SCRIPT EXISTS FOR: the proceeds go back into cash.
    p["cash"] = round(float(p["cash"]) + proceeds, 2)
    pos["shares"] = held - shares
    if pos["shares"] <= 0:
        # A closed position leaves the book. Leaving zero-share rows in
        # positions is what filled the dashboard with $0.00 lines.
        p["positions"] = [x for x in p["positions"] if x.get("symbol", "").upper() != sym]

    p["trades"].append({"cycle": p.get("cycle_count", 0), "side": "SELL", "symbol": sym,
                        "shares": shares, "price": round(float(px), 2),
                        "notional": round(proceeds, 2), "realised_pnl": round(realised, 2),
                        "utc": now(), "reason": reason or ""})
    print(f"SOLD {shares} {sym} @ ${float(px):,.2f} = ${proceeds:,.2f}  "
          f"realised {realised:+,.2f}   cash now ${float(p['cash']):,.2f}")
    return 0


def log_equity(p, book):
    """One line a cycle per book in state/equity.jsonl - the value history a
    drawdown is measured from."""
    with open(EQUITY, "a") as f:
        f.write(json.dumps({"utc": now(), "book": book, "value": round(market_value(p), 2)}) + "\n")


def load_shadow():
    if not SHADOW.exists():
        SHADOW.write_text(json.dumps({
            "mode": "paper-shadow", "currency": "USD",
            "note": "The no-AI book: Belfort's rules, caps and exits; the top mechanical "
                    "candidate instead of a judgement. Compared in `belfort-trade.py stats`.",
            "starting_cash": 10000.0, "cash": 10000.0, "positions": [], "trades": [],
            "cycle_count": 0, "created_utc": now(), "last_cycle_utc": None}, indent=2) + "\n")
    return load(SHADOW)


def shadow_turn(p):
    """The shadow book's cycle: the exits, then at most one buy - the first
    candidate (candidates.json is ranked by MACD histogram) it does not hold
    that every rule allows, sized like Belfort's. Returns what it did."""
    did = [f"sold {sym}: {reason}" for sym, _, reason in exits_due(p)
           if sell(p, sym, "all", None, reason) == 0]
    state, why = regime()
    if state == "unfavorable":
        return did + [f"no buy: regime unfavourable ({why})"]
    try:
        ranked = json.loads(CANDIDATES.read_text())["candidates"]
    except Exception:
        ranked = []
    mv = market_value(p)
    pct = SHADOW_POSITION_PCT if state == "favorable" else NEUTRAL_POSITION_PCT
    for c in ranked:
        sym, px = c["symbol"], float(c["price"])
        if position(p, sym) or px <= 0:
            continue
        room = float(p["cash"]) - mv * MIN_CASH_PCT / 100
        shares = int(min(mv * pct / 100, room) // px)
        if shares > 0 and buy(p, sym, shares, None, "SHADOW: top candidate by MACD histogram",
                              0, None, (state, why)) == 0:
            return did + [f"bought {shares} {sym} ({state})"]
    return did + ["no buy: no candidate the rules allow"]


def cmd_shadow(a):
    p = load_shadow()
    did = shadow_turn(p)
    p["cycle_count"] = int(p.get("cycle_count") or 0) + 1
    p["market_value"] = round(market_value(p), 2)
    save(p, SHADOW)
    log_equity(p, "shadow")
    print("shadow book: " + "; ".join(did) + f" - value ${p['market_value']:,.2f}")
    return 0


RULE_NAMES = (("STOP LOSS", "stop loss"), ("TAKE PROFIT", "take profit"),
              ("PROTECT GAIN", "protect a gain"), ("DEAD MONEY", "dead money"))


def book_stats(p, book, doc=None):
    """The numbers a strategy is judged by, for one book."""
    doc = doc if doc is not None else quotes_doc()
    closed = []
    for t in p.get("trades", []):
        if str(t.get("side", "")).upper() != "SELL" or t.get("realised_pnl") is None:
            continue
        pnl = float(t["realised_pnl"])
        cost = float(t.get("notional") or 0) - pnl
        rule = next((name for prefix, name in RULE_NAMES if str(t.get("reason", "")).startswith(prefix)),
                    "judgement")
        closed.append({"symbol": t.get("symbol"), "pnl": pnl, "pct": pnl / cost * 100 if cost else 0.0,
                       "rule": rule})
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] < 0]
    start = float(p.get("starting_cash") or 10000)
    value = market_value(p)
    values = []
    try:
        values = [json.loads(line)["value"] for line in EQUITY.read_text().splitlines()
                  if line.strip() and json.loads(line).get("book") == book]
    except (OSError, ValueError):
        pass
    peak, drawdown = start, 0.0
    for v in values + [value]:
        peak = max(peak, v)
        drawdown = max(drawdown, (peak - v) / peak * 100)
    since = str(p.get("created_utc") or (p["trades"][0].get("utc") if p.get("trades") else "") or "")[:10]
    qqq = (doc.get("benchmarks") or {}).get("QQQ") or {}
    hist = [(d, float(c)) for d, c in qqq.get("history") or []]
    base = next((c for d, c in hist if d >= since), hist[0][1] if hist else None) if since else None
    return {
        "book": book, "since": since, "value": round(value, 2),
        "return_pct": round((value / start - 1) * 100, 2),
        "qqq_return_pct": round((float(qqq["price"]) / base - 1) * 100, 2) if base and qqq.get("price") else None,
        "qqq_from": next((d for d, _ in hist if d >= since), hist[0][0] if hist else None) if since else None,
        "closed": len(closed), "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_win_pct": round(sum(c["pct"] for c in wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(c["pct"] for c in losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(c["pnl"] for c in wins) / -sum(c["pnl"] for c in losses), 2) if losses else None,
        "expectancy_pct": round(sum(c["pct"] for c in closed) / len(closed), 2) if closed else None,
        "max_drawdown_pct": round(drawdown, 2),
        "exits_by_rule": {name: sum(1 for c in closed if c["rule"] == name)
                          for name in [n for _, n in RULE_NAMES] + ["judgement"]},
        "open": len([x for x in p["positions"] if float(x.get("shares") or 0) > 0]),
    }


def cmd_stats(a):
    books = [book_stats(load(), "belfort")]
    if SHADOW.exists():
        books.append(book_stats(load(SHADOW), "shadow"))
    state, why = regime()
    if a.json:
        print(json.dumps({"regime": state, "regime_why": why, "books": books}, indent=1))
        return 0
    fmt = lambda v, suffix="%": "-" if v is None else f"{v:+.2f}{suffix}" if suffix == "%" else f"{v}{suffix}"
    print(f"regime: {state} - {why}\n")
    for b in books:
        name = "BELFORT (AI)" if b["book"] == "belfort" else "SHADOW (no AI)"
        print(f"{name}  since {b['since']}\n"
              f"  value ${b['value']:,.2f}  return {fmt(b['return_pct'])}   QQQ over the same time "
              f"{fmt(b['qqq_return_pct'])}" + (f" (from {b['qqq_from']})" if b["qqq_from"] and b["qqq_from"] > b["since"] else "")
              + f"\n  max drawdown {b['max_drawdown_pct']:.2f}%   open positions {b['open']}\n"
              f"  closed trades {b['closed']}   win rate {fmt(b['win_rate'], '%') if b['win_rate'] is None else str(b['win_rate']) + '%'}"
              f"   avg win {fmt(b['avg_win_pct'])}   avg loss {fmt(b['avg_loss_pct'])}\n"
              f"  profit factor {b['profit_factor'] if b['profit_factor'] is not None else '-'}"
              f"   expectancy {fmt(b['expectancy_pct'])} a trade\n"
              f"  sells by rule: " + ", ".join(f"{k} {v}" for k, v in b["exits_by_rule"].items()) + "\n")
    return 0


def cmd_exits(a):
    """The sell rules, checked in code. Without --apply it says what is due;
    with it, it sells each one, with the rule as the trade's reason."""
    p = load()
    due = exits_due(p)
    if not due:
        held = [x for x in p["positions"] if float(x.get("shares") or 0) > 0]
        print("exits: nothing due" + "".join(
            f"\n  {x['symbol']}: {exit_check(entry_of(p, x), quote(x['symbol']))[1]}" for x in held))
        return 0
    for sym, rule, reason in due:
        if not a.apply:
            print(f"DUE {sym}: {reason}")
        elif sell(p, sym, "all", None, reason) != 0:
            return 1
    if a.apply:
        save(p)
    return 0


def summary(p):
    mv = market_value(p)
    start = float(p["starting_cash"])
    lines = [f"cash      ${float(p['cash']):,.2f}",
             f"value     ${mv:,.2f}   ({(mv / start - 1) * 100:+.2f}% from ${start:,.0f})",
             f"cycles    {p.get('cycle_count', 0)}"]
    open_pos = [x for x in p["positions"] if float(x.get("shares") or 0) > 0]
    lines.append(f"\npositions ({len(open_pos)} open)")
    for pos in open_pos:
        sh = float(pos["shares"])
        px = float(last_price(pos["symbol"], pos.get("cost_basis")) or 0)
        cb = float(pos.get("cost_basis") or 0)
        pnl = (px / cb - 1) * 100 if cb else 0
        stop = exit_check(entry_of(p, pos), quote(pos["symbol"]))[2]
        lines.append(f"  {pos['symbol']:<6} {sh:>6.0f} sh  entry ${cb:>8,.2f}  "
                     f"last ${px:>8,.2f}  value ${sh * px:>9,.2f}  {pnl:+.2f}%"
                     + (f"  stop ${stop:,.2f}" if stop else ""))
    groups = {}
    for pos in open_pos:
        px = float(last_price(pos["symbol"], pos.get("cost_basis")) or 0)
        groups[cluster_of(pos["symbol"])] = groups.get(cluster_of(pos["symbol"]), 0) + float(pos["shares"]) * px
    if groups and mv:
        lines.append("clusters  " + ", ".join(f"{g} {v / mv * 100:.0f}%" for g, v in
                                              sorted(groups.items(), key=lambda kv: -kv[1]))
                     + f"  (cap {CLUSTER_CAP_PCT:.0f}% each)")
    state, why = regime()
    lines.append(f"regime    {state} - {why}"
                 + {"favorable": "", "unfavorable": ": no new buys"}.get(state, f": new buys up to {NEUTRAL_POSITION_PCT:.0f}%"))
    lines.append(f"\ntrades    {len(p['trades'])}")
    for t in p["trades"][-8:]:
        lines.append(f"  cycle {t.get('cycle', '?')}  {t.get('side', '?'):<4} "
                     f"{t.get('symbol', '?'):<6} {t.get('shares', 0):>4} @ "
                     f"${float(t.get('price') or 0):>8,.2f}  ${float(t.get('notional') or 0):>9,.2f}")
    return "\n".join(lines)


def cmd_mark(a):
    """End of cycle. Re-marks every position against quotes.json, bumps
    cycle_count and stamps last_cycle_utc.

    Every cycle ends here, including a cycle that traded nothing. That is the
    point: a pass is a real outcome and the file has to say when it happened.
    It is also what tells the service the cycle ran - a portfolio.json whose
    mtime never moved is indistinguishable from an agent that quit early."""
    p = load()
    for pos in p["positions"]:
        sh = float(pos.get("shares") or 0)
        if sh <= 0:
            continue
        px = float(last_price(pos["symbol"], pos.get("cost_basis")) or 0)
        cb = float(pos.get("cost_basis") or 0)
        pos["last_price"] = round(px, 2)
        pos["market_value"] = round(sh * px, 2)
        pos["unrealised_pnl"] = round((px - cb) * sh, 2)
        pos["unrealised_pct"] = round((px / cb - 1) * 100, 2) if cb else 0.0
    p["positions"] = [x for x in p["positions"] if float(x.get("shares") or 0) > 0]
    p["cycle_count"] = int(p.get("cycle_count") or 0) + 1
    p["market_value"] = round(market_value(p), 2)
    save(p)
    log_equity(p, "belfort")
    print(summary(p))
    return 0


def cmd_reset(a):
    """Start the paper account over at starting_cash, keeping the old file.

    This exists because the account once read -41% when the real trading loss
    was under 4%: three positions were closed by zeroing shares without ever
    crediting the proceeds, and $3,731.90 left the book. There is no honest way
    to reconstruct those sales - the prices were never recorded - so the choice
    is a clean restart or a number nobody can stand behind."""
    if not a.confirm:
        print("reset needs --confirm. It archives the current portfolio and "
              "starts over at starting_cash.", file=sys.stderr)
        return 1
    p = load()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # Archives live in their own folder: the dashboard falls back to any
    # *portfolio*.json in state/ when portfolio.json is missing, and an old
    # book is the last thing that should stand in for the current one.
    adir = STATE.parent / "archive"
    adir.mkdir(exist_ok=True)
    archive = adir / f"portfolio.{stamp}.json"
    archive.write_text(STATE.read_text())
    fresh = {"mode": p.get("mode", "paper"),
             "note": p.get("note", "Simulated money. No real brokerage is connected to this file."),
             "currency": p.get("currency", "USD"),
             "starting_cash": float(a.cash if a.cash is not None else p["starting_cash"]),
             "cash": float(a.cash if a.cash is not None else p["starting_cash"]),
             "positions": [], "trades": [], "cycle_count": 0,
             "created_utc": now(), "last_cycle_utc": None,
             "reset_from": f"archive/{archive.name}",
             "reset_reason": a.reason or "accounting repair"}
    STATE.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
    print(f"archived to {archive}")
    print(f"portfolio reset to ${fresh['cash']:,.2f} cash, no positions, no trades.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Belfort's paper portfolio, with the arithmetic in code")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show").set_defaults(fn=cmd_show)
    sub.add_parser("mark").set_defaults(fn=cmd_mark)
    sub.add_parser("check").set_defaults(fn=cmd_check)

    r = sub.add_parser("reset")
    r.add_argument("--confirm", action="store_true")
    r.add_argument("--cash", type=float)
    r.add_argument("--reason", default="")
    r.set_defaults(fn=cmd_reset)

    b = sub.add_parser("buy")
    b.add_argument("symbol"); b.add_argument("shares")
    b.add_argument("--price", type=float); b.add_argument("--reason", default="")
    b.add_argument("--score", type=int, default=0)
    b.add_argument("--headline", help="the id of the news.json headline that is the catalyst")
    b.set_defaults(fn=cmd_buy)

    sub.add_parser("shadow").set_defaults(fn=cmd_shadow)
    st = sub.add_parser("stats")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_stats)

    e = sub.add_parser("exits")
    e.add_argument("--apply", action="store_true", help="sell every position whose exit is due")
    e.set_defaults(fn=cmd_exits)

    s = sub.add_parser("sell")
    s.add_argument("symbol"); s.add_argument("shares", nargs="?", default="all")
    s.add_argument("--price", type=float); s.add_argument("--reason", default="")
    s.set_defaults(fn=cmd_sell)

    a = ap.parse_args()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
