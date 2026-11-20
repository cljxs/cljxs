#!/usr/bin/env python3
"""
belfort-quick.py - Belfort Quick: short swing trades on Belfort's stocks, by
plain code. Paper money, its own $10,000, no model.

    belfort-quick.py tick              what the 10-minute timer runs: exits, and buys at the buy checks
    belfort-quick.py show              the book, in words
    belfort-quick.py board             everything the Markets page's Quick tab draws, as JSON
    belfort-quick.py check             does the book balance? (exit 1 if not)
    belfort-quick.py replay [--json]   these rules over two years of Yahoo daily prices (read-only)

THE OWNER'S BRIEF (2026-11-06): a stock trader like Bolt Quick - a 5% target
and Bolt Quick's stop - but with its own entry and exit rules, not Belfort's
market check, for short swings. Chosen: Belfort's 79 stocks, up to 10
trading days a trade, 15/20/25/30% of the account for a score of 7/8/9/10,
and a light daily brake on the market instead of the 50-day check.

THE SETUP IT BUYS: a pause inside an uptrend, on the day it turns back up.
The score (out of 10, buys at 7 or more):

    +3  uptrend: price above its 50-day average, the 20-day above the 50-day
    +2  pullback: within 3% of its 20-day average
    +2  turning up: up on the day
    +1  volume: today at 1.2x its usual pace or more
    +1  room: RSI(14) 40-65
    +1  stronger than the market: up more than SPY over 5 days

NEVER: earnings within 5 trading days (belfort-trade.earnings_soon - one
calendar for both); up 7%+ on the day; a stock whose usual daily move (ATR)
is under 1.5% or over 6% of its price; SPY down 1.5%+ on the day; a third
name from one of Belfort's clusters; a name already held; more than 4 held.

EXITS (Bolt Quick's): stop 2.5% under the price paid, trailing 2.5% under
the best price since - up 2.5%, it is at breakeven; all of it sold at +5%;
sold after 10 trading days; sold the trading day before an earnings report.
Down 15% from where it last started counting: no buys for a trading day.

BUYS once a day, at the 15:30 ET check (the owner's choice after the
replay: three checks a day, one buy each, made +18.8% with a 20% fall from
a high on the way; one buy a day made +24.1% and never fell more than
7.6%). Exits every 10 minutes on Belfort's prices (belfort-fetch.py,
data/quotes.json and bars.json).

THE REPLAY uses daily candles, which cannot say whether a day's high or
low came first: a day that touched both the stop and the target is counted
a loss. Buys are at a day's close (the 15:30 check), BUYS_PER_DAY a day.
Earnings dates in the past are not on file, so the replay cannot apply that
rule - live, it does.

Standard library only.
"""

import importlib.util
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import et_time  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
AGENT = ROOT / "agents" / "belfort"
BOOK = AGENT / "state" / "quick-portfolio.json"
EQUITY = AGENT / "state" / "quick-equity.jsonl"
QUOTES = AGENT / "data" / "quotes.json"
BARS = AGENT / "data" / "bars.json"
REPLAY_DIR = AGENT / "data" / "quick-replay"

STARTING_CASH = 10000.0
MIN_SCORE = 7
SIZE_BY_SCORE = {7: 15.0, 8: 20.0, 9: 25.0, 10: 30.0}     # % of the account, the owner's "bolder"
MAX_POSITIONS = 4
PER_CLUSTER = 2
CASH_FLOOR_PCT = 5.0
STOP_PCT = 2.5
TRAIL_PCT = 2.5
TARGET_PCT = 5.0
MAX_HOLD_DAYS = 10            # trading days
CHASE_PCT = 7.0
ATR_RANGE = (1.5, 6.0)        # usual daily move, % of price
PULLBACK_PCT = 3.0
VOLUME_PACE = 1.2
RSI_RANGE = (40.0, 65.0)
SPY_BRAKE_PCT = -1.5
HALT_PCT = 15.0
BUY_CHECKS_ET = ((15, 30),)
BUYS_PER_DAY = 1
OPEN_ET, CLOSE_ET = (9, 30), (16, 0)
SLIPPAGE_PCT = 0.05           # each way, in the replay: stocks trade without a fee here


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_cache = {}


def trade():
    """belfort-trade.py: the watchlist, the clusters and the earnings calendar."""
    if "t" not in _cache:
        _cache["t"] = _load("belfort_trade_quick", "belfort-trade.py")
    return _cache["t"]


def ind():
    """belfort-fetch.py: the SMA, RSI and ATR arithmetic Belfort's quotes use."""
    if "f" not in _cache:
        _cache["f"] = _load("belfort_fetch_quick", "belfort-fetch.py")
    return _cache["f"]


def iso(t):
    return t.strftime("%Y-%m-%d %H:%M:%S")


def pct(a, b):
    return (a / b - 1) * 100 if a is not None and b else None


# ---------------------------------------------------------------- the rules

def features(closes, highs, lows, vols, spy_closes, session=1.0):
    """What the score and the filters read, from daily bars with today last
    (today's close is the price now). `session` is the share of today's
    trading done, so a 10:00 volume is judged against the pace by then."""
    f = ind()
    price, prev = closes[-1], closes[-2]
    a = f.atr(highs, lows, closes)
    prior = [v for v in vols[-21:-1] if v]
    avg = sum(prior) / len(prior) if prior else None
    return {
        "price": price, "change_pct": pct(price, prev),
        "sma20": f.sma(closes, 20), "sma50": f.sma(closes, 50), "rsi14": f.rsi(closes, 14),
        "atr_pct": a / price * 100 if a and price else None,
        "volume_pace": vols[-1] / (avg * max(session, 0.1)) if avg and vols[-1] else None,
        "vs_spy_5d": (pct(price, closes[-6]) - pct(spy_closes[-1], spy_closes[-6]))
        if len(closes) > 5 and len(spy_closes) > 5 else None,
        "spy_change_pct": pct(spy_closes[-1], spy_closes[-2]) if len(spy_closes) > 1 else None,
    }


def score(x):
    """(score, [the points it got])."""
    p, s20, s50 = x["price"], x["sma20"], x["sma50"]
    rules = [
        (3, "uptrend: price over its 50-day, 20-day over 50-day", bool(s20 and s50 and p > s50 and s20 > s50)),
        (2, f"pullback: within {PULLBACK_PCT:g}% of its 20-day", bool(s20 and abs(p / s20 - 1) * 100 <= PULLBACK_PCT)),
        (2, "turning up: up on the day", bool(x["change_pct"] is not None and x["change_pct"] > 0)),
        (1, f"volume {VOLUME_PACE:g}x its usual pace", bool(x["volume_pace"] and x["volume_pace"] >= VOLUME_PACE)),
        (1, f"RSI {RSI_RANGE[0]:g}-{RSI_RANGE[1]:g}", bool(x["rsi14"] is not None and RSI_RANGE[0] <= x["rsi14"] <= RSI_RANGE[1])),
        (1, "beating SPY over 5 days", bool(x["vs_spy_5d"] is not None and x["vs_spy_5d"] > 0)),
    ]
    return sum(n for n, _, ok in rules if ok), [w for _, w, ok in rules if ok]


def refusal(x):
    """Why this stock may not be bought on its own numbers, or None."""
    if x["change_pct"] is not None and x["change_pct"] >= CHASE_PCT:
        return f"up {x['change_pct']:.1f}% today - not chasing it"
    a = x["atr_pct"]
    if a is None:
        return "no usual daily move on file"
    if not ATR_RANGE[0] <= a <= ATR_RANGE[1]:
        return f"moves {a:.1f}% a day - outside {ATR_RANGE[0]:g}-{ATR_RANGE[1]:g}% for a 2.5% stop and a 5% target"
    return None


def market_brake(spy_change):
    if spy_change is not None and spy_change <= SPY_BRAKE_PCT:
        return f"SPY {spy_change:+.1f}% today - no new buys into a selloff (brake at {SPY_BRAKE_PCT:g}%)"
    return None


def stop_for(pos):
    """2.5% under the price paid, or 2.5% under the best price since -
    whichever is higher. It only rises."""
    return max(pos["entry_price"] * (1 - STOP_PCT / 100), pos["best"] * (1 - TRAIL_PCT / 100))


def target_for(pos):
    return pos["entry_price"] * (1 + TARGET_PCT / 100)


def size_for(book, eq, sc, price):
    """Whole shares for a buy at this score: its share of the account, never
    taking cash under the floor."""
    share = SIZE_BY_SCORE.get(min(sc, 10), 0.0)
    room = book["cash"] - eq * CASH_FLOOR_PCT / 100
    money = max(0.0, min(eq * share / 100, room))
    return int(money // price) if price > 0 else 0


# ---------------------------------------------------------------- the book

def seed(now):
    return {"mode": "PAPER", "starting_cash": STARTING_CASH, "cash": STARTING_CASH, "positions": [],
            "trades": [], "halt": {"active": False, "since_day": None, "reference": STARTING_CASH},
            "started_utc": iso(now), "checks_done": [], "marks": {}}


def load_book(now=None, path=None):
    path = path or BOOK
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return seed(now or datetime.now(timezone.utc))


def equity(book, marks=None):
    marks = marks if marks is not None else book.get("marks") or {}
    return book["cash"] + sum(p["shares"] * (marks.get(p["symbol"]) or p["entry_price"]) for p in book["positions"])


def buy(book, sym, shares, price, sc, why, day, when):
    cost = shares * price
    book["cash"] -= cost
    book["positions"].append({"symbol": sym, "shares": shares, "entry_price": price, "cost": cost,
                              "best": price, "opened_day": day, "opened_utc": when, "score": sc,
                              "stop": price * (1 - STOP_PCT / 100)})
    t = {"utc": when, "day": day, "symbol": sym, "side": "buy", "shares": shares, "price": price,
         "notional": cost, "score": sc, "reason": why}
    book["trades"].append(t)
    return t


def sell(book, pos, price, why, day, when):
    value = pos["shares"] * price
    book["cash"] += value
    book["positions"] = [p for p in book["positions"] if p is not pos]
    pnl = value - pos["cost"]
    t = {"utc": when, "day": day, "symbol": pos["symbol"], "side": "sell", "shares": pos["shares"], "price": price,
         "notional": value, "reason": why, "entry_price": pos["entry_price"], "opened_utc": pos["opened_utc"],
         "pnl": pnl, "pnl_pct": pnl / pos["cost"] * 100, "best_pct": pct(pos["best"], pos["entry_price"])}
    book["trades"].append(t)
    return t


def problems(book):
    """[] when cash and holdings are exactly what the trades leave."""
    out, cash, held = [], book["starting_cash"], {}
    for t in book["trades"]:
        sign = 1 if t["side"] == "buy" else -1
        cash -= sign * t["notional"]
        held[t["symbol"]] = held.get(t["symbol"], 0) + sign * t["shares"]
    if abs(cash - book["cash"]) > 0.01:
        out.append(f"cash is ${book['cash']:,.2f} but the trades leave ${cash:,.2f}")
    have = {}
    for p in book["positions"]:
        if p["symbol"] in have:
            out.append(f"{p['symbol']} is held twice")
        have[p["symbol"]] = p["shares"]
    for s in set(held) | set(have):
        if held.get(s, 0) != have.get(s, 0):
            out.append(f"{s}: the trades leave {held.get(s, 0)}, the book holds {have.get(s, 0)}")
    return out


def update_halt(book, eq, day):
    """'halted', 'resumed' or None - down HALT_PCT from where it last started
    counting: no buys for the rest of that trading day."""
    h = book["halt"]
    if h.get("active"):
        if day != h.get("since_day"):
            h.update(active=False, since_day=None, reference=eq)
            return "resumed"
        return None
    if eq < h["reference"] * (1 - HALT_PCT / 100):
        h.update(active=True, since_day=day)
        return "halted"
    return None


def candidates(rows, book, held_clusters, cluster_of):
    """[(score, points, symbol, x)] that may be bought, best first: rows is
    {symbol: features}."""
    held = {p["symbol"] for p in book["positions"]}
    out = []
    for sym, x in rows.items():
        if sym in held or refusal(x) or held_clusters.get(cluster_of(sym), 0) >= PER_CLUSTER:
            continue
        sc, pts = score(x)
        if sc >= MIN_SCORE:
            out.append((sc, pts, sym, x))
    return sorted(out, key=lambda c: (-c[0], -(c[3]["vs_spy_5d"] or 0)))


# ---------------------------------------------------------------- the replay

def replay_bars(symbols, days=730, get=None, sleep=time.sleep, now=None):
    """{symbol: {"dates", "o", "h", "l", "c", "v"}} from Yahoo, kept 12 hours
    in REPLAY_DIR."""
    now = now or datetime.now(timezone.utc)
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    rng = "2y" if days > 365 else "1y"
    out = {}
    for sym in symbols:
        path = REPLAY_DIR / f"{sym}-{rng}.json"
        if path.is_file() and now.timestamp() - path.stat().st_mtime < 12 * 3600:
            raw = json.loads(path.read_text())
        else:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
            try:
                raw = (get or _http_json)(url)
            except Exception as exc:
                print(f"replay: {sym} not fetched - {exc}", file=sys.stderr)
                continue
            path.write_text(json.dumps(raw))
            sleep(0.6)
        bars = parse_chart(raw)
        if bars:
            out[sym] = bars
    return out


def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; belfort-quick/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def parse_chart(raw):
    """A Yahoo chart reply as daily bars; rows with a missing price dropped."""
    try:
        res = raw["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return None
    out = {k: [] for k in ("dates", "o", "h", "l", "c", "v")}
    for ts, o, h, l, c, v in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"]):
        if None in (o, h, l, c):
            continue
        out["dates"].append(datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"))
        for k, val in (("o", o), ("h", h), ("l", l), ("c", c), ("v", v or 0)):
            out[k].append(float(val))
    return out if len(out["c"]) > 60 else None


def day_exit(pos, o, h, l, c, held):
    """(price, why) when a held stock goes on a day with these open, high,
    low and close - or None, and its best price moves up to the high. A
    daily bar does not say whether the low or the high came first, so a day
    that touched both is counted a loss."""
    stop, target = stop_for(pos), target_for(pos)
    if o <= stop:
        return o, "stop (gapped through at the open)"
    if o >= target:
        return o, "target (gapped past at the open)"
    if l <= stop:
        return stop, ("trailing stop" if stop > pos["entry_price"] * (1 - STOP_PCT / 100) + 1e-9 else "stop")
    if h >= target:
        return target, f"target +{TARGET_PCT:g}%"
    if held >= MAX_HOLD_DAYS:
        return c, f"time limit ({MAX_HOLD_DAYS} trading days)"
    pos["best"] = max(pos["best"], h)
    return None


def replay(bars, spy, cluster_of, buys_per_day=None, warmup=60):
    """The rules over daily bars. Exits each day before buys; buys at the
    close. Returns {"book", "curve": [(day, equity)], "trades"}."""
    buys_per_day = BUYS_PER_DAY if buys_per_day is None else buys_per_day
    days = spy["dates"][warmup:]
    idx = {s: {d: i for i, d in enumerate(b["dates"])} for s, b in bars.items()}
    spy_i = {d: i for i, d in enumerate(spy["dates"])}
    book = seed(datetime(2000, 1, 1, tzinfo=timezone.utc))
    held_days = {}
    curve = []
    slip = SLIPPAGE_PCT / 100
    for day in days:
        when = f"{day} 20:00:00"
        # exits, on the day's open, low, high and close
        for pos in list(book["positions"]):
            b, i = bars[pos["symbol"]], idx[pos["symbol"]].get(day)
            if i is None:
                continue
            held_days[pos["symbol"]] = held_days.get(pos["symbol"], 0) + 1
            out = day_exit(pos, b["o"][i], b["h"][i], b["l"][i], b["c"][i], held_days[pos["symbol"]])
            if out:
                sell(book, pos, out[0] * (1 - slip), out[1], day, when)
        marks = {s: bars[s]["c"][idx[s][day]] for s in bars if day in idx[s]}
        eq = equity(book, marks)
        curve.append((day, eq))
        update_halt(book, eq, day)
        j = spy_i[day]
        spy_closes = spy["c"][:j + 1]
        if book["halt"]["active"] or market_brake(pct(spy_closes[-1], spy_closes[-2])):
            continue
        rows = {}
        for s, b in bars.items():
            i = idx[s].get(day)
            if i is None or i < 55:
                continue
            rows[s] = features(b["c"][:i + 1], b["h"][:i + 1], b["l"][:i + 1], b["v"][:i + 1], spy_closes)
        held_clusters = {}
        for p in book["positions"]:
            held_clusters[cluster_of(p["symbol"])] = held_clusters.get(cluster_of(p["symbol"]), 0) + 1
        bought = 0
        for sc, pts, sym, x in candidates(rows, book, held_clusters, cluster_of):
            if bought >= buys_per_day or len(book["positions"]) >= MAX_POSITIONS:
                break
            if cluster_of(sym) and held_clusters.get(cluster_of(sym), 0) >= PER_CLUSTER:
                continue
            px = x["price"] * (1 + slip)
            n = size_for(book, eq, sc, px)
            if n <= 0:
                continue
            buy(book, sym, n, px, sc, f"scored {sc}/10: " + "; ".join(pts), day, when)
            held_days[sym] = 0
            held_clusters[cluster_of(sym)] = held_clusters.get(cluster_of(sym), 0) + 1
            bought += 1
    return {"book": book, "curve": curve}


def replay_summary(result, spy):
    curve, book = result["curve"], result["book"]
    start, end = curve[0][1], curve[-1][1]
    mid = curve[len(curve) // 2]
    sells = [t for t in book["trades"] if t["side"] == "sell"]
    wins = [t for t in sells if t["pnl"] > 0]
    s0, s1 = spy["c"][spy["dates"].index(curve[0][0])], spy["c"][-1]
    sm = spy["c"][spy["dates"].index(mid[0])]
    reasons = {}
    for t in sells:
        k = t["reason"].split(" (")[0].split(" +")[0]
        reasons[k] = reasons.get(k, 0) + 1
    return {"from": curve[0][0], "to": curve[-1][0], "profit": round(end - start, 2),
            "return_pct": round(pct(end, start), 2), "first_half": round(mid[1] - start, 2),
            "second_half": round(end - mid[1], 2), "spy_pct": round(pct(s1, s0), 2),
            "spy_first_half_pct": round(pct(sm, s0), 2), "spy_second_half_pct": round(pct(s1, sm), 2),
            "sales": len(sells), "won": len(wins),
            "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else None,
            "avg_loss_pct": round(sum(t["pnl_pct"] for t in sells if t["pnl"] <= 0)
                                  / max(1, len(sells) - len(wins)), 2) if sells else None,
            "worst_drop_pct": round(min(pct(v, max(x for _, x in curve[:k + 1])) for k, (_, v) in enumerate(curve)), 2),
            "exits": reasons, "balances": not problems(book)}


def cmd_replay(args):
    t = trade()
    bars = replay_bars(t.universe() + ["SPY"])
    spy = bars.pop("SPY", None)
    if not spy:
        print("replay: no SPY prices - nothing to measure against", file=sys.stderr)
        return 1
    r = replay_summary(replay(bars, spy, t.cluster_of), spy)
    if "--json" in args:
        print(json.dumps(r, indent=1))
        return 0
    print(f"Belfort Quick replayed on {len(bars)} stocks' daily prices, {r['from']} to {r['to']}, from $10,000.")
    print("A day that touched both the stop and the target counts as a loss; earnings are not on file for the")
    print("past, so that rule is not applied here. A guide, not a promise.")
    print(f"  profit ${r['profit']:+,.2f} ({r['return_pct']:+.1f}%) - SPY {r['spy_pct']:+.1f}% over the same days")
    print(f"  first half ${r['first_half']:+,.2f} (SPY {r['spy_first_half_pct']:+.1f}%), second half "
          f"${r['second_half']:+,.2f} (SPY {r['spy_second_half_pct']:+.1f}%)")
    print(f"  {r['sales']} trades closed, {r['won']} won; average win {r['avg_win_pct']}%, average loss "
          f"{r['avg_loss_pct']}%; worst drop from a high {r['worst_drop_pct']}%")
    print("  exits: " + ", ".join(f"{k} {v}" for k, v in sorted(r["exits"].items(), key=lambda kv: -kv[1])))
    return 0


# ---------------------------------------------------------------- live

def trading_days_held(opened_day, today):
    """Weekdays after the buy's day, up to and including today."""
    d, n = datetime.strptime(opened_day, "%Y-%m-%d").date(), 0
    while d < today:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


def session_share(now_et):
    o = now_et.replace(hour=OPEN_ET[0], minute=OPEN_ET[1], second=0, microsecond=0)
    return min(1.0, max(0.0, (now_et - o).total_seconds() / (6.5 * 3600)))


def due_check(now_et, done):
    """The buy check this tick is in (its "HH:MM" name), or None: a check
    is due from its time until the next one, once a day."""
    hm = (now_et.hour, now_et.minute)
    due = [c for c in BUY_CHECKS_ET if hm >= c and hm < CLOSE_ET]
    if not due:
        return None
    name = f"{now_et:%Y-%m-%d} {due[-1][0]:02d}:{due[-1][1]:02d}"
    return None if name in done else name


def live_rows(bars_doc, quotes_doc, session):
    """{symbol: features} from Belfort's own files: bars.json's daily candles
    (today's, as far as it has gone, last) and quotes.json's prices now."""
    spy_hist = [c for _, c in ((quotes_doc.get("benchmarks") or {}).get("SPY") or {}).get("history") or []]
    spy_now = ((quotes_doc.get("benchmarks") or {}).get("SPY") or {}).get("price")
    if spy_hist and spy_now:
        spy_hist = spy_hist[:-1] + [float(spy_now)]
    rows = {}
    for sym, ch in (bars_doc.get("bars") or {}).items():
        b = ch.get("bars") or []
        q = (quotes_doc.get("quotes") or {}).get(sym) or {}
        if len(b) < 55 or not q.get("price") or q.get("stale"):
            continue
        closes = [x[4] for x in b[:-1]] + [float(q["price"])]
        rows[sym] = features(closes, [x[2] for x in b], [x[3] for x in b], [x[5] or 0 for x in b], spy_hist, session)
    return rows, (pct(spy_hist[-1], spy_hist[-2]) if len(spy_hist) > 1 else None)


def tick(now=None, quotes_doc=None, bars_doc=None):
    """One tick: exits on every held stock, then - at a buy check - at most
    one buy. Returns what happened, as lines."""
    now = now or datetime.now(timezone.utc)
    now_et = et_time.to_eastern(now)
    day = et_time.day(now_et)
    t = trade()
    quotes_doc = quotes_doc if quotes_doc is not None else _read(QUOTES)
    bars_doc = bars_doc if bars_doc is not None else _read(BARS)
    book = load_book(now)
    marks = {s: float(q["price"]) for s, q in (quotes_doc.get("quotes") or {}).items() if q.get("price")}
    when, lines = iso(now), []
    for pos in list(book["positions"]):
        px = marks.get(pos["symbol"])
        if px is None:
            continue
        pos["best"] = max(pos["best"], px)
        pos["stop"] = stop_for(pos)
        held = trading_days_held(pos["opened_day"], now_et.date())
        soon = t.earnings_soon(pos["symbol"], now_et.date())
        why = ("trailing stop" if pos["stop"] > pos["entry_price"] * (1 - STOP_PCT / 100) + 1e-9 else "stop loss") \
            + f" (-{STOP_PCT:g}%)" if px <= pos["stop"] else \
            f"target +{TARGET_PCT:g}%" if px >= target_for(pos) else \
            f"time limit ({MAX_HOLD_DAYS} trading days)" if held >= MAX_HOLD_DAYS else \
            f"earnings {soon[0]} - out the day before" if soon and soon[1] <= 1 else None
        if why:
            s = sell(book, pos, px, why, day, when)
            lines.append(f"sold {s['shares']} {s['symbol']} at ${px:,.2f} - {why}: {s['pnl_pct']:+.1f}%")
    book["marks"] = marks
    eq = equity(book, marks)
    h = update_halt(book, eq, day)
    if h:
        lines.append(f"{h} - down {HALT_PCT:g}%: no buys for the rest of {day}" if h == "halted" else
                     f"{h}: counting from ${eq:,.2f}")
    check = due_check(now_et, set(book.get("checks_done") or []))
    if check:
        book["checks_done"] = (book.get("checks_done") or [])[-30:] + [check]
        rows, spy_change = live_rows(bars_doc, quotes_doc, session_share(now_et))
        held_clusters = {}
        for p in book["positions"]:
            held_clusters[t.cluster_of(p["symbol"])] = held_clusters.get(t.cluster_of(p["symbol"]), 0) + 1
        why_not = ("halted" if book["halt"]["active"] else market_brake(spy_change)
                   or (f"{len(book['positions'])} held, the most is {MAX_POSITIONS}"
                       if len(book["positions"]) >= MAX_POSITIONS else None))
        action = None
        if not why_not:
            for sc, pts, sym, x in candidates(rows, book, held_clusters, t.cluster_of):
                soon = t.earnings_soon(sym, now_et.date())
                if soon:
                    continue
                n = size_for(book, eq, sc, x["price"])
                if n <= 0:
                    continue
                b = buy(book, sym, n, x["price"], sc, f"scored {sc}/10: " + "; ".join(pts), day, when)
                action = f"bought {n} {sym} at ${x['price']:,.2f} (${b['notional']:,.2f}), scored {sc}/10"
                break
        lines.append(f"check {check[-5:]} ET: " + (action or f"no buy - {why_not or 'nothing scored 7+ that may be bought'}"))
    bad = problems(book)
    if bad:
        return ["THE QUICK BOOK DOES NOT BALANCE - nothing saved: " + "; ".join(bad)]
    book["last_tick_utc"] = when
    BOOK.parent.mkdir(parents=True, exist_ok=True)
    BOOK.write_text(json.dumps(book, indent=1) + "\n")
    with EQUITY.open("a") as fh:
        fh.write(json.dumps({"t": when, "equity": round(eq, 2), "cash": round(book["cash"], 2)}) + "\n")
    return lines


def _read(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def board(now=None):
    """Everything the Quick tab shows, worked out here."""
    now = now or datetime.now(timezone.utc)
    book = load_book(now)
    eq = equity(book)
    sells = [t for t in book["trades"] if t["side"] == "sell"]
    return {"equity": eq, "cash": book["cash"], "pnl": eq - book["starting_cash"],
            "pnl_pct": pct(eq, book["starting_cash"]), "started_utc": book.get("started_utc"),
            "positions": [dict(p, price=book["marks"].get(p["symbol"]),
                               pnl_pct=pct(book["marks"].get(p["symbol"]), p["entry_price"]),
                               target=target_for(p)) for p in book["positions"]],
            "trades": list(reversed(book["trades"][-30:])),
            "closed": {"n": len(sells), "won": sum(1 for t in sells if t["pnl"] > 0),
                       "realized": sum(t["pnl"] for t in sells)},
            "halted": bool(book["halt"].get("active")), "balances": not problems(book),
            "last_tick_utc": book.get("last_tick_utc"),
            "rules": {"min_score": MIN_SCORE, "size_by_score": {str(k): v for k, v in SIZE_BY_SCORE.items()},
                      "max_positions": MAX_POSITIONS, "per_cluster": PER_CLUSTER, "stop_pct": STOP_PCT,
                      "trail_pct": TRAIL_PCT, "target_pct": TARGET_PCT, "max_hold_days": MAX_HOLD_DAYS,
                      "chase_pct": CHASE_PCT, "atr_range": list(ATR_RANGE), "spy_brake_pct": SPY_BRAKE_PCT,
                      "buy_checks": [f"{h}:{m:02d}" for h, m in BUY_CHECKS_ET]}}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "show"
    if cmd in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if cmd == "replay":
        return cmd_replay(argv[1:])
    if cmd == "tick":
        lines = tick()
        print("\n".join(f"belfort quick: {x}" for x in lines) or "belfort quick: nothing due")
        return 1 if lines and "DOES NOT BALANCE" in lines[0] else 0
    if cmd == "board":
        print(json.dumps(board()))
        return 0
    if cmd == "check":
        bad = problems(load_book())
        print("belfort quick: " + ("; ".join(bad) if bad else "the book balances"))
        return 1 if bad else 0
    b = board()
    print(f"Belfort Quick (paper): ${b['equity']:,.2f} ({b['pnl_pct']:+.2f}%), cash ${b['cash']:,.2f}, "
          f"{len(b['positions'])} held")
    for p in b["positions"]:
        print(f"  {p['symbol']:<5} {p['shares']} at ${p['entry_price']:,.2f}, stop ${p['stop']:,.2f}, "
              f"target ${p['target']:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
