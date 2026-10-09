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
    belfort-trade.py stats [--json]    # both books against each other, QQQ and SPY
    belfort-trade.py calls       # file state/calls.txt: his score and reason per candidate

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
import re
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
EARNINGS = ROOT / "agents" / "belfort" / "data" / "earnings.json"
BARS = ROOT / "agents" / "belfort" / "data" / "bars.json"
REPORTS = ROOT / "agents" / "belfort" / "reports"
# His call on each candidate, every wake (owner, 2026-10-06: "I want it to say
# for each stock why it passed"). He writes CALLS_IN; `calls` files it in CALLS.
CALLS_IN = ROOT / "agents" / "belfort" / "state" / "calls.txt"
CALLS = ROOT / "agents" / "belfort" / "state" / "calls.jsonl"
# The candidates as they stood at wake, copied by belfort-cycle.sh. The
# fetcher rewrites candidates.json every 10 minutes, so without this a name
# that screened in after he wrote his calls would fail a cycle that did its job.
CALLS_OWED = ROOT / "agents" / "belfort" / "state" / ".candidates-at-wake.json"
CALL_MIN_WORDS = 6

MAX_POSITION_PCT = 25.0
MIN_CASH_PCT = 5.0          # the owner lowered it from 15% on 2026-10-01

# The exit rules, in percent from cost.
STOP_LOSS = -10.0           # close, no exceptions, no averaging down
TAKE_PROFIT = 25.0          # sell half - or all of a 1-share position - once
# Owner, 2026-10-09: half, not all. O'Neil's 20-25% rule says take most or
# part there and keep the rest on a stop; Minervini and Qullamaggie sell part
# into strength. The other half stays on the protect stop below, which keeps
# rising with each new high - so a big run is no longer capped at +25%.
PROTECT_AT = 8.0            # once a position has been up this much...
TRAIL = 10.0                # ...its stop follows the highest price, this far below,
                            #    and never below what was paid
# Dead money: 8+ trading days within +/-2% AND behind QQQ over the same days.
# Was 5 days and no market comparison; a swing trade often goes sideways
# for a week before it moves, and flat in a flat market is not dead.
DEAD_DAYS, DEAD_BAND = 8, 2.0

# Stops and sizes from each name's own volatility (ATR, from the fetcher):
# the stop sits ATR_MULT average days' moves under the price paid, never
# nearer than STOP_MIN_PCT or further than STOP_MAX_PCT; a new buy may lose
# at most RISK_PER_TRADE_PCT of the portfolio if that stop is hit. A flat
# -10% was a coin-flip on SMCI and a big loss on MSFT. Positions bought
# before this keep STOP_LOSS and TRAIL.
ATR_MULT = 2.0
STOP_MIN_PCT, STOP_MAX_PCT = 5.0, 15.0
RISK_PER_TRADE_PCT = 1.0

# No new buy within this many trading days of the name's earnings report:
# an overnight earnings gap goes straight through any stop.
EARNINGS_BLACKOUT_DAYS = 5

# Names that move together, so a cap on one name is not a cap on one bet:
# NVDA, AMD and MU at 20% each is 60% in one trade. A name not listed is its
# own cluster.
CLUSTERS = {
    "semiconductors": ["NVDA", "AMD", "AVGO", "MU", "TSM", "SMCI", "ARM", "QCOM", "INTC", "MRVL"],
    "software and security": ["PLTR", "NET", "DDOG", "SNOW", "CRWD", "ZS", "PANW", "MDB"],
    "mega-cap": ["META", "GOOGL", "AMZN", "MSFT", "AAPL"],
    "crypto and high-beta": ["COIN", "HOOD", "TSLA"],
    "consumer internet": ["SHOP", "UBER", "ABNB", "RBLX"],
    # Added 2026-10-06 with the names in belfort-fetch.py's UNIVERSE.
    "healthcare": ["LLY", "ISRG", "VRTX", "ABBV", "HIMS"],
    "financials": ["JPM", "GS", "V", "MA"],
    "industrials and energy": ["GE", "CAT", "ETN", "XOM"],
    "consumer and retail": ["COST", "NFLX", "WMT", "NKE"],
    # Owner, 2026-10-08. SpaceX listed 2026-06-12 as SPCX; 82 trading days by
    # the day it was added, past the 60 the signals need.
    "space": ["SPCX", "RKLB", "ASTS", "LUNR", "PL", "RDW"],
}
CLUSTER_CAP_PCT = 40.0
MAX_POSITIONS = 8

# The market regime (regime()). Mixed: a new buy may bring the position to
# this much of the portfolio - half the usual ~20%. Unfavourable: none.
NEUTRAL_POSITION_PCT = 10.0
BREADTH_MIN = 0.5           # share of the names above their own 50-day average

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
    of the names above their own 50-day average."""
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
    try:
        items = json.loads(NEWS.read_text())["headlines"]
    except Exception:
        raise ValueError("no news.json to cite a headline from")
    h = next((x for x in items if x.get("id") == hid), None)
    if not h:
        raise ValueError(f"no headline {hid!r} in news.json - cite one by its \"id\"")
    if str(h.get("symbol", "")).upper() != symbol.upper():
        raise ValueError(f"headline {hid} is from {h.get('symbol')}'s news, not {symbol.upper()}'s")
    age = headline_age(h, now_ts)
    if age is None:
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


def risk_pct(q):
    """How far under the price a new position's stop goes, in percent:
    ATR_MULT days' average range, kept between STOP_MIN_PCT and STOP_MAX_PCT.
    None when the quote has no ATR yet."""
    if not q or not q.get("atr14") or not q.get("price"):
        return None
    raw = ATR_MULT * float(q["atr14"]) / float(q["price"]) * 100
    return round(min(STOP_MAX_PCT, max(STOP_MIN_PCT, raw)), 2)


def since(q_or_hist, day):
    """The first daily close on or after `day`, from a quote's history."""
    hist = q_or_hist.get("history") if isinstance(q_or_hist, dict) else q_or_hist
    return next((float(c) for d, c in hist or [] if d >= day), None)


def exit_check(pos, q, qqq=None):
    """(rule, reason, stop_price) for one open position against its quote;
    rule is None when nothing is due. The stop is the position's own
    distance (risk_pct, set from volatility when bought; -10% for older
    positions) under what was paid - or, once it has been up 8%, that far
    under its highest price, never below what was paid. qqq is QQQ's quote,
    for the dead-money comparison."""
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
    dist = float(pos.get("risk_pct") or -STOP_LOSS)
    trail = float(pos.get("risk_pct") or TRAIL)
    stop = round(cb * (1 - dist / 100), 2)
    if peak_pnl >= PROTECT_AT:
        stop = round(max(cb, peak * (1 - trail / 100)), 2)
    held = sum(1 for d, _ in history if d > entry_day)

    if pnl <= -dist:
        return "stop", f"STOP LOSS: {pnl:+.1f}% from cost ${cb:,.2f} (its stop: -{dist:g}%)", stop
    if pnl >= TAKE_PROFIT and not pos.get("trimmed"):
        if float(pos.get("shares") or 0) >= 2:
            return "trim", (f"TAKE PROFIT (half): {pnl:+.1f}% from cost ${cb:,.2f} - the rest rides "
                            f"its stop"), stop
        return "take", f"TAKE PROFIT: {pnl:+.1f}% from cost ${cb:,.2f}", stop
    if peak_pnl >= PROTECT_AT and px <= stop:
        return "protect", (f"PROTECT GAIN: was up {peak_pnl:+.1f}% (high ${peak:,.2f}), now {pnl:+.1f}%, "
                           f"at or under its stop ${stop:,.2f}"), stop
    if held >= DEAD_DAYS and abs(pnl) <= DEAD_BAND:
        base = since(qqq, entry_day) if qqq else None
        mkt = round((float(qqq["price"]) / base - 1) * 100, 6) if base and qqq.get("price") else None
        if mkt is not None and pnl < mkt:
            return "dead", (f"DEAD MONEY: {pnl:+.1f}% after {held} trading days, "
                            f"while QQQ moved {mkt:+.1f}%"), stop
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
        rule, reason, _ = exit_check(entry_of(p, pos), quote(pos["symbol"]), benchmark("QQQ"))
        if rule:
            out.append((pos["symbol"], rule, reason))
    return out


def apply_exits(p):
    """Sell what exits_due() says, for either book - the one place a due exit
    becomes a sale. A trim sells half (rounded down) and marks the position,
    so it is never trimmed twice and its last sale can say what selling it all
    at the trim would have made."""
    did = []
    for sym, rule, reason in exits_due(p):
        pos = position(p, sym)
        held = int(float(pos.get("shares") or 0))
        if rule == "trim":
            half = held // 2
            if sell(p, sym, half, None, reason) == 0:
                px = float(p["trades"][-1]["price"])
                pos["trimmed"] = {"utc": now(), "price": px, "shares_before": held, "shares_sold": half,
                                  "realised": float(p["trades"][-1]["realised_pnl"])}
                did.append(f"sold {half} of {held} {sym}: {reason}")
        elif sell(p, sym, "all", None, reason) == 0:
            did.append(f"sold {sym}: {reason}")
    return did


def benchmark(symbol):
    return (quotes_doc().get("benchmarks") or {}).get(symbol)


def headline_age(h, now_ts=None):
    """Seconds since a news.json headline was published, or None if its date
    cannot be read. The one reading of a headline's age: a buy's catalyst
    must be under HEADLINE_MAX_AGE_DAYS by it, and the Markets page counts
    the headlines that could still be one."""
    from email.utils import parsedate_to_datetime
    try:
        return (now_ts or datetime.now(timezone.utc).timestamp()) - parsedate_to_datetime(h["published"]).timestamp()
    except Exception:
        return None


def trading_days_until(day, today=None):
    """Weekdays from today (0) to `day`, or None if it has passed."""
    from datetime import date, timedelta
    if today is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import et_time      # the Eastern date; after 8pm ET, UTC is already tomorrow
        today = et_time.eastern_now().date()
    target = date.fromisoformat(day)
    if target < today:
        return None
    n, d = 0, today
    while d < target:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def earnings_soon(symbol, today=None):
    """(date, trading days away) when the name reports within the blackout,
    else None. No earnings.json yet: None - a missing calendar must not stop
    every buy, and says so in `show`."""
    try:
        day = json.loads(EARNINGS.read_text())["dates"].get(symbol.upper())
    except Exception:
        return None
    if not day:
        return None
    n = trading_days_until(day, today)
    return (day, n) if n is not None and n <= EARNINGS_BLACKOUT_DAYS else None


def size(p, symbol, state=None):
    """(shares, why) - the most of `symbol` a buy may take now: the
    risk budget at its stop, the position cap (smaller when the regime is
    mixed), cash above the floor, and the cluster's room. 0 with the reason
    when none."""
    sym = symbol.upper()
    q = quote(sym)
    if not q or not q.get("price"):
        return 0, f"no price for {sym}"
    px = float(q["price"])
    state, why = state or regime()
    if state == "unfavorable":
        return 0, f"the regime is unfavourable ({why})"
    soon = earnings_soon(sym)
    if soon:
        return 0, f"{sym} reports earnings {soon[0]}, {soon[1]} trading day(s) away"
    held = position(p, sym)
    names = [x for x in p["positions"] if float(x.get("shares") or 0) > 0]
    # buy() refuses a 9th name; size() said "12 shares" all the same until
    # 2026-10-06, when the Markets page began quoting it as the reason a
    # stock is or is not held.
    if not (held and float(held.get("shares") or 0) > 0) and len(names) >= MAX_POSITIONS:
        return 0, f"{len(names)} names held, the most is {MAX_POSITIONS}"
    mv = market_value(p)
    cap = MAX_POSITION_PCT if state == "favorable" else NEUTRAL_POSITION_PCT
    held_value = float(held["shares"]) * px if held else 0.0
    group = cluster_of(sym)
    in_group = sum(float(x["shares"]) * float(last_price(x["symbol"], x.get("cost_basis")) or 0)
                   for x in p["positions"] if float(x.get("shares") or 0) > 0 and cluster_of(x["symbol"]) == group)
    limits = {"position cap": mv * cap / 100 - held_value,
              "cash floor": float(p["cash"]) - mv * MIN_CASH_PCT / 100,
              f"{group} cluster": mv * CLUSTER_CAP_PCT / 100 - in_group}
    r = risk_pct(q)
    if r:
        limits[f"1% risk at a -{r:g}% stop"] = mv * RISK_PER_TRADE_PCT / 100 / (r / 100)
    name, room = min(limits.items(), key=lambda kv: kv[1])
    shares = max(0, int(room // px))
    if not shares:
        return 0, f"no room left under the {name}"
    return shares, f"{shares} shares of {sym} at ${px:,.2f}, held to the {name}" + (f"; stop -{r:g}%" if r else "")


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
    soon = earnings_soon(sym)
    if soon:
        print(f"REFUSED: {sym} reports earnings on {soon[0]}, {soon[1]} trading day(s) away - no new "
              f"buys within {EARNINGS_BLACKOUT_DAYS}.", file=sys.stderr)
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

    r = risk_pct(quote(sym))
    if r and mv and cost * r / 100 > mv * RISK_PER_TRADE_PCT / 100 + 0.01:
        print(f"REFUSED: {shares} {sym} would lose ${cost * r / 100:,.2f} at its stop (-{r:g}%, from its "
              f"volatility) - the most a buy may risk is {RISK_PER_TRADE_PCT:g}% of the portfolio, "
              f"${mv * RISK_PER_TRADE_PCT / 100:,.2f}: {int(mv * RISK_PER_TRADE_PCT / r // float(px))} shares. "
              f"`belfort-trade.py size {sym}` gives the most allowed.", file=sys.stderr)
        return 1

    p["cash"] = round(float(p["cash"]) - cost, 2)
    if existing and float(existing.get("shares") or 0) > 0:
        # A new average cost is a new position: it may trim again, and its
        # half-versus-all comparison no longer means anything.
        existing.pop("trimmed", None)
        old_sh = float(existing["shares"])
        existing["cost_basis"] = round(
            (old_sh * float(existing["cost_basis"]) + cost) / (old_sh + shares), 2)
        existing["shares"] = old_sh + shares
    else:
        p["positions"] = [x for x in p["positions"] if x.get("symbol", "").upper() != sym]
        p["positions"].append({"symbol": sym, "shares": shares, "cost_basis": round(float(px), 2),
                               "entry_utc": now(), "reason": reason or "", "score": score,
                               "catalyst": cited, "regime": state, "risk_pct": r})
    p["trades"].append({"cycle": p.get("cycle_count", 0), "side": "BUY", "symbol": sym,
                        "shares": shares, "price": round(float(px), 2),
                        "notional": round(cost, 2), "utc": now(), "reason": reason or "",
                        "catalyst": cited, "regime": state, "score": score})
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
    trim = pos.get("trimmed") if pos["shares"] <= 0 else None
    if pos["shares"] <= 0:
        # A closed position leaves the book. Leaving zero-share rows in
        # positions is what filled the dashboard with $0.00 lines.
        p["positions"] = [x for x in p["positions"] if x.get("symbol", "").upper() != sym]

    p["trades"].append({"cycle": p.get("cycle_count", 0), "side": "SELL", "symbol": sym,
                        "shares": shares, "price": round(float(px), 2),
                        "notional": round(proceeds, 2), "realised_pnl": round(realised, 2),
                        "utc": now(), "reason": reason or ""})
    if trim:
        # Half at the trim, the rest here - against all of it at the trim.
        p["trades"][-1]["trim_test"] = {
            "actual": round(float(trim["realised"]) + realised, 2),
            "all_at_trim": round((float(trim["price"]) - cb) * float(trim["shares_before"]), 2)}
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
    candidate (candidates.json is ranked by MACD histogram over price) it does not hold
    that every rule allows, sized like Belfort's. Returns what it did."""
    did = apply_exits(p)
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
        # Belfort's usual ~20%, or less: whatever size() allows
        shares = min(int(mv * pct / 100 // px), size(p, sym, (state, why))[0])
        if shares > 0 and buy(p, sym, shares, None, "SHADOW: top candidate by MACD histogram over price",
                              0, None, (state, why)) == 0:
            return did + [f"bought {shares} {sym} ({state})"]
    return did + ["no buy: no candidate the rules allow"]


def cmd_shadow(a):
    p = load_shadow()
    if a.exits_only:
        did = apply_exits(p) or ["nothing due"]
        save(p, SHADOW)
        log_equity(p, "shadow")
        print("shadow book, midday exits: " + "; ".join(did))
        return 0
    did = shadow_turn(p)
    p["cycle_count"] = int(p.get("cycle_count") or 0) + 1
    p["market_value"] = round(market_value(p), 2)
    save(p, SHADOW)
    log_equity(p, "shadow")
    print("shadow book: " + "; ".join(did) + f" - value ${p['market_value']:,.2f}")
    return 0


RULE_NAMES = (("STOP LOSS", "stop loss"), ("TAKE PROFIT", "take profit"),
              ("PROTECT GAIN", "protect a gain"), ("DEAD MONEY", "dead money"))


def catalyst_type(reason):
    """"ANALYST" from "ANALYST: Piper Sandler to Neutral" - the TYPE his
    instructions make him write first. "untagged" when there is none."""
    m = re.match(r"\s*([A-Z][A-Z/&\- ]{1,24}):", reason or "")
    return m.group(1).strip() if m else "untagged"


def closed_trades(p):
    """Every sale that realised a profit or loss, with the buy that opened
    its position: what it was bought on, the score, the regime. The one
    reading of a closed trade - stats and belfort-learn.py's record both use
    it. A position added to keeps its first buy."""
    lots, held, out = {}, {}, []
    for t in p.get("trades", []):
        sym, side = str(t.get("symbol", "")).upper(), str(t.get("side", "")).upper()
        if side == "BUY":
            lots.setdefault(sym, t)
            held[sym] = held.get(sym, 0) + float(t.get("shares") or 0)
            continue
        if side != "SELL" or t.get("realised_pnl") is None:
            continue
        pnl = float(t["realised_pnl"])
        cost = float(t.get("notional") or 0) - pnl
        rule = next((name for prefix, name in RULE_NAMES if str(t.get("reason", "")).startswith(prefix)),
                    "judgement")
        entry = lots.get(sym) or {}
        out.append({"symbol": t.get("symbol"), "pnl": pnl, "pct": pnl / cost * 100 if cost else 0.0,
                    "rule": rule, "sold_utc": t.get("utc"), "bought_utc": entry.get("utc"),
                    "catalyst_type": catalyst_type(entry.get("reason")), "score": entry.get("score") or None,
                    "regime": entry.get("regime"), "cluster": cluster_of(sym)})
        held[sym] = held.get(sym, 0) - float(t.get("shares") or 0)
        if held[sym] <= 0:
            lots.pop(sym, None)
    return out


def book_stats(p, book, doc=None):
    """The numbers a strategy is judged by, for one book."""
    doc = doc if doc is not None else quotes_doc()
    closed = closed_trades(p)
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
    qqq_pct, qqq_from = bench_since(doc, "QQQ", since)
    spy_pct, spy_from = bench_since(doc, "SPY", since)
    return {
        "book": book, "since": since, "value": round(value, 2),
        "return_pct": round((value / start - 1) * 100, 2),
        "qqq_return_pct": qqq_pct, "qqq_from": qqq_from,
        "spy_return_pct": spy_pct, "spy_from": spy_from,
        "closed": len(closed), "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_win_pct": round(sum(c["pct"] for c in wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(c["pct"] for c in losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(c["pnl"] for c in wins) / -sum(c["pnl"] for c in losses), 2) if losses else None,
        "expectancy_pct": round(sum(c["pct"] for c in closed) / len(closed), 2) if closed else None,
        "max_drawdown_pct": round(drawdown, 2),
        "exits_by_rule": {name: sum(1 for c in closed if c["rule"] == name)
                          for name in [n for _, n in RULE_NAMES] + ["judgement"]},
        "open": len([x for x in p["positions"] if float(x.get("shares") or 0) > 0]),
        "trim_test": trim_test(p),
    }


def trim_test(p):
    """Half at +25% and the rest on the stop, against all of it at +25%, over
    every trimmed position that has closed. The owner's rule (2026-10-09)
    stays only if this says it earns more on his own trades."""
    rows = [t["trim_test"] for t in p.get("trades", []) if t.get("trim_test")]
    if not rows:
        return None
    actual = round(sum(r["actual"] for r in rows), 2)
    all_at = round(sum(r["all_at_trim"] for r in rows), 2)
    return {"positions": len(rows), "actual": actual, "all_at_trim": all_at,
            "difference": round(actual - all_at, 2)}


def bench_since(doc, symbol, since):
    """(percent, from day): a benchmark from the first close on or after
    `since` to its price now. QQQ is the tech market his first 30 names came
    from; SPY, added 2026-10-06 with the non-tech names, the whole market."""
    b = (doc.get("benchmarks") or {}).get(symbol) or {}
    hist = [(d, float(c)) for d, c in b.get("history") or []]
    if not since or not hist:
        return None, None
    day, base = next(((d, c) for d, c in hist if d >= since), hist[0])
    return (round((float(b["price"]) / base - 1) * 100, 2) if base and b.get("price") else None), day


def book_view(p):
    """What a page shows of one book: its cash, its positions with their
    stops, its last trades - each book's own, never added to the other."""
    rows = []
    for pos in p["positions"]:
        sh = float(pos.get("shares") or 0)
        if sh <= 0:
            continue
        px = float(last_price(pos["symbol"], pos.get("cost_basis")) or 0)
        cb = float(pos.get("cost_basis") or 0)
        rows.append({"symbol": pos["symbol"], "shares": sh, "entry": cb, "last": round(px, 2),
                     "value": round(sh * px, 2), "pnl_pct": round((px / cb - 1) * 100, 2) if cb else None,
                     "stop": exit_check(entry_of(p, pos), quote(pos["symbol"]))[2],
                     "cluster": cluster_of(pos["symbol"])})
    return {"cash": round(float(p["cash"]), 2), "starting_cash": float(p.get("starting_cash") or 10000),
            "positions": rows,
            "trades": [{k: t.get(k) for k in ("utc", "side", "symbol", "shares", "price", "notional",
                                               "realised_pnl", "reason")} for t in p.get("trades", [])[-10:][::-1]]}


def same_days(books):
    """Belfort and the shadow book over the SAME days - the shadow book's,
    which started later. Their own totals cover different stretches of the
    market and cannot be compared. None until both have been running."""
    shadow = next((b for b in books if b["book"] == "shadow"), None)
    if not shadow:
        return None
    start = load(SHADOW).get("created_utc") or ""
    try:
        rows = [json.loads(line) for line in EQUITY.read_text().splitlines() if line.strip()]
    except OSError:
        rows = []
    first = next((r["value"] for r in rows if r.get("book") == "belfort" and str(r.get("utc", "")) >= start), None)
    now_value = next(b["value"] for b in books if b["book"] == "belfort")
    return {"since": start[:10], "shadow_pct": shadow["return_pct"], "qqq_pct": shadow["qqq_return_pct"],
            "spy_pct": shadow["spy_return_pct"],
            "belfort_pct": round((now_value / first - 1) * 100, 2) if first else None}


def cmd_stats(a):
    books = [book_stats(load(), "belfort")]
    if SHADOW.exists():
        books.append(book_stats(load(SHADOW), "shadow"))
    state, why = regime()
    if a.json:
        for b in books:
            b.update(book_view(load(SHADOW) if b["book"] == "shadow" else load()))
        print(json.dumps({"regime": state, "regime_why": why, "books": books,
                          "same_days": same_days(books)}, indent=1))
        return 0
    fmt = lambda v, suffix="%": "-" if v is None else f"{v:+.2f}{suffix}" if suffix == "%" else f"{v}{suffix}"
    print(f"regime: {state} - {why}\n")
    for b in books:
        name = "BELFORT (AI)" if b["book"] == "belfort" else "SHADOW (no AI)"
        print(f"{name}  since {b['since']}\n"
              f"  value ${b['value']:,.2f}  return {fmt(b['return_pct'])}   over the same time: QQQ "
              f"{fmt(b['qqq_return_pct'])}, SPY {fmt(b['spy_return_pct'])}"
              + (f" (from {b['qqq_from']})" if b["qqq_from"] and b["qqq_from"] > b["since"] else "")
              + f"\n  max drawdown {b['max_drawdown_pct']:.2f}%   open positions {b['open']}\n"
              f"  closed trades {b['closed']}   win rate {fmt(b['win_rate'], '%') if b['win_rate'] is None else str(b['win_rate']) + '%'}"
              f"   avg win {fmt(b['avg_win_pct'])}   avg loss {fmt(b['avg_loss_pct'])}\n"
              f"  profit factor {b['profit_factor'] if b['profit_factor'] is not None else '-'}"
              f"   expectancy {fmt(b['expectancy_pct'])} a trade\n"
              f"  sells by rule: " + ", ".join(f"{k} {v}" for k, v in b["exits_by_rule"].items()) + "\n")
    return 0


def _doc(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _change(closes, back):
    """Percent from the close `back` trading days ago to the last one."""
    if len(closes) <= back or not closes[-1 - back]:
        return None
    return round((closes[-1] / closes[-1 - back] - 1) * 100, 2)


def board():
    """Everything the Markets page draws (owner, 2026-10-06), worked out
    here so nothing on the page is arithmetic in JavaScript: the showdown
    between his book, the no-AI book, QQQ and SPY; what he holds and what passed
    the screens; per name, the price, the candles and lines from bars.json,
    the gauges, his own exit rules as "what would change his mind", the
    news, and his reports."""
    doc = quotes_doc()
    quotes = doc.get("quotes") or {}
    charts = _doc(BARS).get("bars") or {}
    news = _doc(NEWS).get("headlines") or []
    p = load()
    books = [book_stats(p, "belfort", doc)]
    if SHADOW.exists():
        books.append(book_stats(load(SHADOW), "shadow", doc))
    same = same_days(books)
    state, why = regime(doc)
    qqq = (doc.get("benchmarks") or {}).get("QQQ")

    held = {pos["symbol"].upper(): entry_of(p, pos) for pos in p["positions"]
            if float(pos.get("shares") or 0) > 0}
    calls = {}                      # wake -> {symbol: call}, the later line winning
    for c in calls_log():
        calls.setdefault(c.get("wake"), {})[c.get("symbol")] = {
            k: c.get(k) for k in ("symbol", "score", "reason", "wake", "utc")}
    latest = {}
    for wake in sorted(calls, key=lambda w: report_order(Path(f"{w}.md"))):
        latest.update(calls[wake])
    cands = [c.get("symbol") for c in _doc(CANDIDATES).get("candidates") or []]
    order = list(held) + [c for c in cands if c and c not in held]

    def ticker(sym):
        q = quotes.get(sym) or {}
        ch = charts.get(sym) or {}
        closes = [float(c) for _, c in q.get("history") or []]
        pos = held.get(sym)
        flips, row = [], {}
        if pos:
            rule, why_now, stop = exit_check(pos, q, qqq)
            cb = float(pos.get("cost_basis") or 0)
            verdict = "SELL" if rule else "HOLD"
            if rule:
                flips.append(["SELL", f"{why_now} - due at his next check"])
            if stop is not None:
                flips.append(["SELL", f"if it closes at or under ${stop:,.2f} - its stop"])
            if cb and not pos.get("trimmed"):
                part = "half" if float(pos.get("shares") or 0) >= 2 else "all"
                flips.append(["SELL", f"{part} at ${cb * (1 + TAKE_PROFIT / 100):,.2f} - take profit "
                                      f"(+{TAKE_PROFIT:g}% from cost)"])
            row = {"shares": float(pos["shares"]), "entry": cb, "stop": stop, "score": pos.get("score"),
                   "pnl_pct": round((float(q["price"]) / cb - 1) * 100, 2) if cb and q.get("price") else None,
                   "catalyst": (pos.get("catalyst") or {}).get("title") if isinstance(pos.get("catalyst"), dict) else None}
        else:
            verdict = "WATCH"
            room, blocked = size(p, sym, (state, why))
            fresh = [n for n in news if str(n.get("symbol", "")).upper() == sym
                     and (headline_age(n) or 10 ** 9) <= HEADLINE_MAX_AGE_DAYS * 86400]
            if not room:
                row["why_not"] = f"His rules block a buy: {blocked}."
            elif not fresh:
                row["why_not"] = (f"Room for {room} shares, but no headline under {HEADLINE_MAX_AGE_DAYS} days "
                                  f"old in its feed - a buy must cite one.")
            else:
                row["why_not"] = (f"Room for {room} shares and {len(fresh)} fresh headline(s) - a buy needs one "
                                  f"he scores 7+ of 10.")
            flips.append(["BUY", "if a headline from its own news names a specific event he scores 7+ of 10"])
            if state == "unfavorable":
                flips.append(["WAIT", f"no buys while the market is unfavorable ({why})"])
            soon = earnings_soon(sym)
            if soon:
                flips.append(["WAIT", f"reports earnings {soon[0]}, {soon[1]} trading days away - no buys this close"])
            r = risk_pct(q)
            if r:
                flips.append(["RISK", f"a buy's stop would sit {r:g}% under the price"])
        price, s20, s50 = q.get("price"), q.get("sma20"), q.get("sma50")
        return {
            "symbol": sym, "verdict": verdict, "held": bool(pos), **row, "call": latest.get(sym),
            "price": price, "change_pct": q.get("change_pct"),
            "change_5d": _change(closes, 5), "change_30d": _change(closes, 30),
            "spark": [round(c, 2) for c in closes[-30:]],
            "candles": ch.get("bars") or [], "sma20_line": ch.get("sma20") or [], "sma50_line": ch.get("sma50") or [],
            "sma20": s20, "sma50": s50, "rsi14": q.get("rsi14"),
            "macd_hist": q.get("macd_hist"), "macd_hist_line": ch.get("macd_hist") or [],
            "volume": ch.get("volume"), "volume_avg20": ch.get("volume_avg20"), "volume_ratio": ch.get("volume_ratio"),
            "vs_sma20_pct": round((price / s20 - 1) * 100, 2) if price and s20 else None,
            "vs_sma50_pct": round((price / s50 - 1) * 100, 2) if price and s50 else None,
            "mechanical_score": q.get("mechanical_score"), "cluster": cluster_of(sym),
            "flips": flips,
            "news": [{k: n.get(k) for k in ("title", "publisher", "published")}
                     for n in news if str(n.get("symbol", "")).upper() == sym][:8],
        }

    if same and same.get("belfort_pct") is not None:
        basis = f"same days, since {same['since']}"
        race = [("Belfort (AI)", same["belfort_pct"]), ("No-AI book", same["shadow_pct"]), ("QQQ", same["qqq_pct"]),
                ("SPY", same["spy_pct"])]
    else:
        mine = books[0]
        basis = f"since {mine['since'] or 'the start'}"
        race = [("Belfort (AI)", mine["return_pct"]), ("QQQ", mine["qqq_return_pct"]), ("SPY", mine["spy_return_pct"])]
        if len(books) > 1:
            race.insert(1, ("No-AI book", books[1]["return_pct"]))
    vals = [v for _, v in race if v is not None]
    reports = sorted(REPORTS.glob("*.md"), key=report_order, reverse=True) if REPORTS.is_dir() else []
    return {
        "asof_utc": doc.get("asof_utc"), "regime": state, "regime_why": why,
        "showdown": {"basis": basis, "rows": [{"name": n, "pct": v} for n, v in race],
                     "spread": round(max(vals) - min(vals), 2) if len(vals) > 1 else None},
        "books": [{k: b[k] for k in ("book", "value", "return_pct", "open")} for b in books],
        "account": account(p, quotes),
        "watch": [ticker(s) for s in order if s in quotes],
        "report": {"name": reports[0].name, "text": reports[0].read_text(errors="replace")} if reports else None,
        "history": [f.name for f in reports[:30]],
        "calls": {f.stem: sorted(calls[f.stem].values(), key=lambda c: (-(c["score"] or 0), c["symbol"]))
                  for f in reports[:30] if f.stem in calls},
    }


# ------------------------------------------------------------------ calls

def cycle_started():
    """When this wake began, written by belfort-cycle.sh. 0 when absent."""
    try:
        return int((STATE.parent / ".cycle-started").read_text().strip())
    except Exception:
        return 0


def wake_name(started=None):
    """This wake's name, "2026-10-06-open": its report's name without .md,
    from et_time, so a call and its report can never be filed apart."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import et_time
    name, _ = et_time.expected_report(STATE.parent.parent, "belfort", started=started or None)
    return Path(name).stem if name else None


def calls_log():
    out = []
    try:
        text = CALLS.read_text()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def calls_owed():
    """The candidates owed a call: those at wake if belfort-cycle.sh copied
    them, else candidates.json as it is now."""
    started = cycle_started()
    src = CALLS_OWED if CALLS_OWED.exists() and (not started or CALLS_OWED.stat().st_mtime >= started) \
        else CANDIDATES
    return [str(c.get("symbol")).upper() for c in _doc(src).get("candidates") or [] if c.get("symbol")]


def calls_missing(since=None):
    """Candidates with no call from this wake, leaving out what he holds -
    a name he bought this wake is his call on it. The one rule: signoff.py
    and belfort-verify.py ask this function."""
    since = cycle_started() if since is None else since
    held = {x["symbol"].upper() for x in load()["positions"] if float(x.get("shares") or 0) > 0}
    done = {c.get("symbol") for c in calls_log() if not since or int(c.get("ts") or 0) >= since}
    return [s for s in calls_owed() if s not in held and s not in done]


def parse_calls(text, known):
    """`SYMBOL | score | reason` per line -> ({symbol: (score, reason)}, problems)."""
    got, bad = {}, []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*").strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split("|", 2)]
        if len(parts) < 3:
            bad.append(f"{line[:60]!r}: write it as SYMBOL | score | reason")
            continue
        sym, score, reason = parts[0].upper(), parts[1].split("/")[0].strip(), parts[2]
        if sym not in known:
            bad.append(f"{sym}: not one of the names in quotes.json")
        elif sym in got:
            bad.append(f"{sym}: two lines - give each name one")
        elif not score.isdigit() or not 0 <= int(score) <= 10:
            bad.append(f"{sym}: score {parts[1]!r} - a whole number 0 to 10")
        elif len(reason.split()) < CALL_MIN_WORDS:
            bad.append(f"{sym}: {reason!r} is not a reason - say what you weighed for THIS name "
                       f"(the headline and why it does or does not reach 7, or what your rules block)")
        else:
            got[sym] = (int(score), reason)
    return got, bad


def cmd_calls(a):
    owed = calls_missing()
    if a.owed:
        print(", ".join(owed) if owed else "none")
        return 0
    started = cycle_started()
    fresh = CALLS_IN.exists() and not (started and CALLS_IN.stat().st_mtime < started)
    if not owed and not fresh:
        # Nothing to file and nothing owed. An error here would send him back
        # to write a file no one asked for.
        print("No candidate is owed a call this wake - nothing to file.")
        return 0
    if not CALLS_IN.exists():
        print(f"state/calls.txt does not exist. Write one line per candidate: SYMBOL | score | reason. "
              f"Owed this wake: {', '.join(owed) or 'none'}", file=sys.stderr)
        return 1
    if started and CALLS_IN.stat().st_mtime < started:
        print("state/calls.txt is from an earlier wake - nothing recorded. Rewrite it with this "
              f"wake's calls. Owed: {', '.join(owed) or 'none'}", file=sys.stderr)
        return 1
    doc = quotes_doc()
    quotes, known = doc.get("quotes") or {}, set(doc.get("quotes") or {})
    spy = ((doc.get("benchmarks") or {}).get("SPY") or {}).get("price")
    got, bad = parse_calls(CALLS_IN.read_text(errors="replace"), known)
    if bad:
        print("nothing recorded:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    held = {x["symbol"].upper() for x in load()["positions"] if float(x.get("shares") or 0) > 0}
    now_ = datetime.now(timezone.utc)
    wake = wake_name(started)
    CALLS.parent.mkdir(parents=True, exist_ok=True)
    with CALLS.open("a") as f:
        for sym, (score, reason) in got.items():
            f.write(json.dumps({"ts": int(now_.timestamp()), "utc": now_.strftime("%Y-%m-%d %H:%M:%S"),
                                "wake": wake, "symbol": sym, "score": score, "reason": reason,
                                "held": sym in held,
                                # what belfort-learn.py measures a pass from, and SPY beside it
                                "price": (quotes.get(sym) or {}).get("price"), "spy": spy}) + "\n")
    left = calls_missing()
    print(f"recorded {len(got)} call(s) for {wake}." + (
        f" Still owed: {', '.join(left)} - add a line for each and run this again." if left
        else " Every candidate has a call."))
    return 0


def account(p, quotes):
    """His account in dollars, for the top of the Markets page (owner,
    2026-10-07: "add the money value at the top"). Worked out here, so the
    page only prints it: the value and cash are the same market_value() and
    cash every other reading uses, and today's move is each holding's shares
    times its change since yesterday's close."""
    value, start, cash = market_value(p), float(p.get("starting_cash") or 10000), float(p["cash"])
    today = 0.0
    for pos in p["positions"]:
        q = quotes.get(pos["symbol"].upper()) or {}
        if float(pos.get("shares") or 0) > 0 and q.get("price") is not None and q.get("prev_close"):
            today += float(pos["shares"]) * (float(q["price"]) - float(q["prev_close"]))
    return {"value": round(value, 2), "starting_cash": start, "pnl": round(value - start, 2),
            "pnl_pct": round((value / start - 1) * 100, 2) if start else None, "cash": round(cash, 2),
            "invested": round(value - cash, 2), "today": round(today, 2),
            "today_pct": round(today / (value - today) * 100, 2) if value - today else None}


def report_order(f):
    """Newest first means by day, then by wake: "2026-10-05-close" comes after
    "-open", though by name it sorts before it. The wake order is
    et_time's, the one place the slots are named."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import et_time
    wakes = [name for _, name in et_time.SLOTS["belfort"]]
    m = re.match(r"(\d{4}-\d{2}-\d{2})-([a-z]+)", f.name)
    if not m:
        return (f.name[:10], -1, f.name)
    return (m.group(1), wakes.index(m.group(2)) if m.group(2) in wakes else -1, f.name)


def cmd_board(a):
    print(json.dumps(board(), separators=(",", ":")))
    return 0


def cmd_size(a):
    shares, why = size(load(), a.symbol)
    print(why if shares else f"0 shares of {a.symbol.upper()}: {why}")
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
    if not a.apply:
        for sym, rule, reason in due:
            print(f"DUE {sym}: {reason}")
        return 0
    did = apply_exits(p)
    save(p)
    for line in did:
        print(line)
    return 0 if len(did) == len(due) else 1


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
    try:
        dates = json.loads(EARNINGS.read_text())["dates"]
        soon = sorted((d, s) for s, d in dates.items() if earnings_soon(s))
        lines.append("earnings  " + (", ".join(f"{s} {d}" for d, s in soon) + " - no new buys in these"
                                     if soon else f"none within {EARNINGS_BLACKOUT_DAYS} trading days"))
    except Exception:
        lines.append("earnings  no calendar yet (data/earnings.json) - the blackout is not being checked")
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

    sh = sub.add_parser("shadow")
    sh.add_argument("--exits-only", action="store_true", help="the midday check: sell what is due, buy nothing")
    sh.set_defaults(fn=cmd_shadow)
    z = sub.add_parser("size", help="the most of a name a buy may take now, and what limits it")
    z.add_argument("symbol")
    z.set_defaults(fn=cmd_size)
    sub.add_parser("board", help="JSON for the Markets page").set_defaults(fn=cmd_board)
    c = sub.add_parser("calls", help="file state/calls.txt: his score and reason for each candidate")
    c.add_argument("--owed", action="store_true", help="only list the names still owed a call this wake")
    c.set_defaults(fn=cmd_calls)
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
