#!/usr/bin/env python3
"""
bolt.py — Bolt, the crypto momentum trader. Paper money, plain code, no model.

    bolt.py tick         what the timer runs every 10 minutes
    bolt.py show         the book, in words
    bolt.py check        does the book balance? (exit 1 if not)
    bolt.py board        everything the Bolt screen draws, as JSON

NO MODEL IS CALLED, ON PURPOSE (owner, 2026-10-13). Every rule Bolt trades by
is a formula - the score, the stops, the sizes, the breakers - and a model
handed formulas eventually reports doing arithmetic it did not do. Belfort
showed it: a book that read -41% when the loss was under 4%, because closes
zeroed the shares without crediting the cash; sell rules that were only
followed once they moved into code; a cycle that stopped one step short.
Fury went the same way. So Bolt is a script on a timer: free, the same
answer every time, and never a number it did not compute.

EVERY TICK (10 minutes): one CoinGecko call for all ten coins, the 20- and
50-hour averages from the seven days of hourly prices that call returns,
then the exits, at this tick's prices:

    stop loss     -8% on the price paid
    trailing stop 8% under the best price since he bought - it only rises
    time limit    14 days open, whatever it is doing
    (no fixed target: a winner runs until the trailing stop takes it)
    BTC dump      BTC down more than 5% on the day: every coin but BTC goes

EVERY HOUR AND A HALF (00:00, 01:30, 03:00 ... UTC): at most one new position, the best
scorer of 7 or more on the 10-point rubric below, if nothing stops him -
the dump switch, the drawdown halt, or the 5% of the account he always keeps
in cash. There is no limit on how many coins he holds (owner, 2026-10-13);
one of each, and the cash floor, are the only bounds.

A FILL IS THE PRICE AT THE TICK, NOT THE STOP. A stop at -4% checked every
ten minutes can still be crossed by more than that between two looks; the
book records what the sale would really have fetched, and 0.1% of fee each
way, so the paper account errs on the side it would lose money on.

BOLT + NEWS, A SECOND BOOK (owner, 2026-10-16: "Would it be worth
implement something for bolt to trade more like belforts research model" -
Belfort's no-AI shadow was doing much worse than Belfort). $10,000 more,
the same rules and the same exits at the same ticks, with one difference:
before it buys the coin code picked, that coin's last 48 hours of headlines
(CoinDesk, Cointelegraph, The Block, Decrypt) go to Haiku on the owner's
Claude plan as one question - is there a specific, citable catalyst, and is
there bad news? Code reads the answer: it buys only on a score of 7+ that
cites one of the headlines it was shown, with no bad news. No headlines, no
question and no buy. At most NEWS_WAKES_PER_DAY questions an Eastern day.
Exits never wait for a model. The plain book carries on exactly as before,
so the two side by side say whether news helps crypto the way it helps
Belfort.

THE HALT RESUMES ITSELF (owner: "it's just paper trading"). Down 15% from
where he last started counting, he opens nothing for 24 hours; then he starts
again and counts from the equity he restarts at.

Standard library only.
"""

import email.utils
import importlib.util
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import et_time  # noqa: E402
import remember  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
AGENT = ROOT / "agents" / "bolt"
MARKET = AGENT / "data" / "market.json"
BOOK = AGENT / "state" / "portfolio.json"
EQUITY = AGENT / "state" / "equity.jsonl"
REPORTS = AGENT / "reports"

API = "https://api.coingecko.com/api/v3/coins/markets"
UA = "bolt/1.0 (paper trading; github.com/cljxs/cljxs)"

# The universe: ticker -> CoinGecko id, in the order the screen lists them.
UNIVERSE = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
    "XRP": "ripple", "ADA": "cardano", "AVAX": "avalanche-2", "LINK": "chainlink",
    "DOT": "polkadot", "LTC": "litecoin",
}
# ...and a coin is only bought while it is big and busy enough. On the day
# this was written (fixture, 2026-10-13) LTC, AVAX and DOT were under the $5B
# and LINK, LTC, AVAX and DOT under the $500M: they are watched, and refused.
MIN_MARKET_CAP = 5e9
MIN_VOLUME_24H = 5e8

STARTING_CASH = 10000.0
FEE_PCT = 0.1                 # each way: a typical exchange's taker fee
TARGET_PCT = 15.0             # of the account, per new position (owner, 2026-10-13: was 25)
CASH_FLOOR_PCT = 5.0          # always kept in cash, after the fee (owner, 2026-10-13: was 25)
MIN_TRADE = 100.0             # a position smaller than this is not worth the fees

# SWING EXITS (owner, 2026-10-19: "the bot needs to be almost swing trading
# like belfort" - a +15% target with a 24-hour dead-money rule wanted a move
# that takes weeks inside a day). Replayed through run() over 83 days of
# hourly prices, the old rules closed 118 of 166 trades as dead money before
# the target could come; quick +5/-2.5 trades lost money in both halves; this
# made the most and traded a third as often. The stop is a floor that only
# rises: 8% under the entry, or 8% under the best price since, whichever is
# higher.
STOP_LOSS_PCT = 8.0
TRAIL_PCT = 8.0
MAX_HOLD_DAYS = 14.0
BTC_DUMP_PCT = -5.0           # BTC's 24h change below this: close every altcoin
MAX_DRAWDOWN_PCT = 15.0
HALT_HOURS = 24.0

# Owner, 2026-10-13: every 1.5 hours, not 4 - it costs nothing, being code.
# 5,400 seconds divides a day exactly, so the checks fall at the same 16
# times every day: 00:00, 01:30, 03:00 ... 22:30 UTC.
CYCLE_HOURS = 1.5
MA_FAST, MA_SLOW = 20, 50     # hours
MIN_SCORE = 7
CHASE_PCT = 20.0              # up this much on the day: no buying the top
STALE_MINUTES = 30            # the screen calls prices older than this stale

# Bolt + news (see the docstring).
NEWS_BOOK = AGENT / "state" / "news-portfolio.json"
NEWS_EQUITY = AGENT / "state" / "news-equity.jsonl"
NEWS_CALLS = AGENT / "state" / "news-calls.jsonl"
NEWS_USE = AGENT / "state" / "news-wakes.json"
NEWS_WAKES_PER_DAY = 5        # owner, 2026-10-16: "put the cap at 5"
NEWS_HOURS = 48
NEWS_MAX = 8                  # headlines shown per question
NEWS_MIN_SCORE = 7            # Belfort's bar
FEED_TIMEOUT = 15
ASK_TIMEOUT = 120
# Checked from here 2026-10-16: each answered with 20-36 items (CoinDesk's
# address without the trailing slash - with it, a redirect).
FEEDS = (("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss"),
         ("Cointelegraph", "https://cointelegraph.com/rss"),
         ("The Block", "https://www.theblock.co/rss.xml"),
         ("Decrypt", "https://decrypt.co/feed"))
# How a headline names each coin. Names in any case; tickers in capitals
# only, so "link" and "dot" in a sentence are not Chainlink and Polkadot,
# and "Binance" (the exchange) is not BNB.
NAMES = {"BTC": ["Bitcoin"], "ETH": ["Ethereum", "Ether"], "SOL": ["Solana"], "BNB": ["BNB Chain"],
         "XRP": ["Ripple"], "ADA": ["Cardano"], "AVAX": ["Avalanche"], "LINK": ["Chainlink"],
         "DOT": ["Polkadot"], "LTC": ["Litecoin"]}


class BadData(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def iso(when):
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def money(x):
    return f"${x:,.2f}"


def signed(x, nd=2):
    return f"{x:+.{nd}f}"


# ---------------------------------------------------------------- the market

def fetch_raw(timeout=30):
    """One call for the whole universe. The 7 days of hourly prices come with
    it (sparkline=true), so the averages need no second request - ten
    /market_chart calls would meet CoinGecko's free limit every tick."""
    params = urllib.parse.urlencode({
        "vs_currency": "usd", "ids": ",".join(UNIVERSE.values()),
        "sparkline": "true", "price_change_percentage": "1h,24h,7d",
    })
    headers = {"User-Agent": UA, "Accept": "application/json"}
    key = os.environ.get("COINGECKO_API_KEY", "").strip()
    if key:
        headers["x-cg-demo-api-key"] = key
    req = urllib.request.Request(f"{API}?{params}", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        return json.load(r)


def mean(xs):
    return sum(xs) / len(xs)


def parse_markets(rows, now):
    """market.json's contents from a /coins/markets reply, or BadData.

    All ten or nothing: a reply missing a coin, or a price, or the hours an
    average needs, is not written over the last good one. Bolt then trades
    on nothing this tick rather than on half a market."""
    if not isinstance(rows, list):
        raise BadData(f"expected a list of coins, got {type(rows).__name__}: {str(rows)[:160]}")
    by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
    coins = {}
    for sym, cid in UNIVERSE.items():
        r = by_id.get(cid)
        if not r:
            raise BadData(f"{sym} ({cid}) is missing from the reply")
        price = num(r.get("current_price"))
        if not price or price <= 0:
            raise BadData(f"{sym} has no price ({r.get('current_price')!r})")
        spark = [float(x) for x in ((r.get("sparkline_in_7d") or {}).get("price") or [])
                 if num(x) is not None]
        if len(spark) < MA_SLOW:
            raise BadData(f"{sym}: {len(spark)} hourly prices, {MA_SLOW} needed for the "
                          f"{MA_SLOW}-hour average")
        coins[sym] = {
            "id": cid, "name": r.get("name") or sym, "price": price,
            "ma_20h": mean(spark[-MA_FAST:]), "ma_50h": mean(spark[-MA_SLOW:]),
            "change_1h": num(r.get("price_change_percentage_1h_in_currency")),
            "change_24h": num(r.get("price_change_percentage_24h_in_currency")),
            "change_7d": num(r.get("price_change_percentage_7d_in_currency")),
            "market_cap": num(r.get("market_cap"), 0.0),
            "volume_24h": num(r.get("total_volume"), 0.0),
            "rank": r.get("market_cap_rank"),
            "updated_utc": r.get("last_updated"),
            "spark": [round(x, 8) for x in spark],
        }
    return {"fetched_utc": iso(now), "source": "CoinGecko /coins/markets",
            "btc_change_24h": coins["BTC"]["change_24h"], "coins": coins}


def write_json(path, data):
    """Written whole or not at all: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    tmp.replace(path)


def load_market():
    try:
        return json.loads(MARKET.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------- the book

def seed(now):
    return {"mode": "PAPER", "starting_cash": STARTING_CASH, "cash": STARTING_CASH,
            "positions": [], "trades": [], "marks": {},
            "halt": {"active": False, "since_utc": None, "reference": STARTING_CASH},
            "started_utc": iso(now), "btc_start": None,
            "last_cycle_block": None, "last_cycle_utc": None, "last_tick_utc": None}


def load_book(now=None, path=None):
    path = path or BOOK
    if path.is_file():
        return json.loads(path.read_text())
    return seed(now or utcnow())


def equity(book, marks=None):
    marks = marks if marks is not None else book.get("marks") or {}
    held = sum(p["qty"] * (marks.get(p["symbol"]) or p["entry_price"]) for p in book["positions"])
    return book["cash"] + held


def pnl_pct(pos, price):
    return (price / pos["entry_price"] - 1.0) * 100.0


def buy(book, sym, price, notional, now, reason):
    fee = notional * FEE_PCT / 100.0
    qty = notional / price
    book["cash"] -= notional + fee
    book["positions"].append({
        "symbol": sym, "qty": qty, "entry_price": price, "cost": notional + fee,
        "opened_utc": iso(now), "high_water_pct": 0.0,
        "stop_price": price * (1 - STOP_LOSS_PCT / 100.0),
    })
    t = {"utc": iso(now), "symbol": sym, "side": "buy", "qty": qty, "price": price,
         "notional": notional, "fee": fee, "reason": reason}
    book["trades"].append(t)
    return t


def sell(book, pos, price, now, reason):
    notional = pos["qty"] * price
    fee = notional * FEE_PCT / 100.0
    book["cash"] += notional - fee
    book["positions"] = [p for p in book["positions"] if p is not pos]
    pnl = notional - fee - pos["cost"]
    t = {"utc": iso(now), "symbol": pos["symbol"], "side": "sell", "qty": pos["qty"],
         "price": price, "notional": notional, "fee": fee, "reason": reason,
         "entry_price": pos["entry_price"], "opened_utc": pos["opened_utc"],
         "pnl": pnl, "pnl_pct": pnl / pos["cost"] * 100.0,
         # How far up it got - coach.py's "gave back a gain" reads it.
         "best_pct": round(pos.get("high_water_pct") or 0.0, 2)}
    book["trades"].append(t)
    return t


def stop_for(pos):
    """The stop: STOP_LOSS_PCT under the price paid, or TRAIL_PCT under the
    best price since, whichever is higher - so it only ever rises."""
    best = pos["entry_price"] * (1 + (pos.get("high_water_pct") or 0.0) / 100.0)
    return max(pos["entry_price"] * (1 - STOP_LOSS_PCT / 100.0), best * (1 - TRAIL_PCT / 100.0))


def trailing(pos):
    """True once the trailing stop is the one that counts."""
    return pos["stop_price"] > pos["entry_price"] * (1 - STOP_LOSS_PCT / 100.0) + 1e-12


def exit_reason(pos, price, now):
    """Why this position goes now, or None. Updates its high-water mark and
    its stop. A position opened under the old rules (2026-10-19) is carried
    over: its recorded best price sets the trailing stop from here on."""
    move = pnl_pct(pos, price)
    pos["high_water_pct"] = max(pos.get("high_water_pct") or 0.0, move)
    pos["stop_price"] = stop_for(pos)
    pos.pop("take_profit_price", None)
    if price <= pos["stop_price"]:
        return (f"trailing stop ({TRAIL_PCT:g}% off its best, +{pos['high_water_pct']:.1f}%)"
                if trailing(pos) else f"stop loss (-{STOP_LOSS_PCT:g}%)")
    held = (now - parse_iso(pos["opened_utc"])).total_seconds() / 3600.0
    if held >= MAX_HOLD_DAYS * 24:
        return f"time limit ({MAX_HOLD_DAYS:g} days, {move:+.1f}%)"
    return None


def cycle_block(now):
    return int(now.timestamp() // int(CYCLE_HOURS * 3600))


def assess(sym, c, btc_change, held):
    """The 10-point rubric for one coin. {'score', 'points', 'out'} - `out` is
    why it cannot be bought at all, whatever it scores."""
    price, ma20, ma50 = c["price"], c["ma_20h"], c["ma_50h"]
    ch1, ch24, ch7 = c.get("change_1h"), c.get("change_24h"), c.get("change_7d")
    out = None
    if sym in held:
        out = "already held"
    elif (c.get("market_cap") or 0) < MIN_MARKET_CAP:
        out = f"market cap ${(c.get('market_cap') or 0) / 1e9:.1f}B, under ${MIN_MARKET_CAP / 1e9:g}B"
    elif (c.get("volume_24h") or 0) < MIN_VOLUME_24H:
        out = f"24h volume ${(c.get('volume_24h') or 0) / 1e6:,.0f}M, under ${MIN_VOLUME_24H / 1e6:,.0f}M"
    elif ch24 is None:
        out = "no 24h change in the data"
    elif ch24 >= CHASE_PCT:
        out = f"up {ch24:.1f}% on the day - not chasing it"
    elif price <= ma50:
        out = "at or under its 50h average"

    rubric = [
        (4, "uptrend: price > 20h avg > 50h avg", price > ma20 > ma50),
        (2, "within 2% of its 20h avg and rising this hour",
         price > ma50 and abs(price / ma20 - 1) * 100 <= 2.0 and (ch1 or 0) > 0),
        (2, "24h +1% to +15%", ch24 is not None and 1.0 <= ch24 <= 15.0),
        (1, "up on the week", ch7 is not None and ch7 > 0),
        (1, "BTC up on the day", btc_change is not None and btc_change > 0),
    ]
    return {"symbol": sym, "score": sum(p for p, _, ok in rubric if ok),
            "points": [w for _, w, ok in rubric if ok], "out": out,
            "rubric": [{"pts": p, "rule": w, "ok": bool(ok)} for p, w, ok in rubric]}


def rolling(xs, n):
    """The n-hour average at every hour, None until n hours exist - the
    lines the screen draws, worked out here so the page does no sums."""
    out, acc = [], 0.0
    for i, x in enumerate(xs):
        acc += x
        if i >= n:
            acc -= xs[i - n]
        out.append(round(acc / n, 8) if i >= n - 1 else None)
    return out


def size_for(book, eq):
    """What a new position may cost: 15% of the account, never taking
    cash under the floor once the fee is paid."""
    room = (book["cash"] - eq * CASH_FLOOR_PCT / 100.0) / (1 + FEE_PCT / 100.0)
    return max(0.0, min(eq * TARGET_PCT / 100.0, room))


def update_halt(book, eq, now):
    """'halted', 'resumed' or None. Measured from `reference`: $10,000 at
    the start, and the equity he restarts at after each halt."""
    h = book["halt"]
    if h.get("active"):
        since = parse_iso(h.get("since_utc"))
        if since and (now - since).total_seconds() >= HALT_HOURS * 3600:
            h.update(active=False, since_utc=None, reference=eq, resumed_utc=iso(now))
            return "resumed"
        return None
    if eq < h["reference"] * (1 - MAX_DRAWDOWN_PCT / 100.0):
        h.update(active=True, since_utc=iso(now))
        return "halted"
    return None


def run(book, market, now, gate=None):
    """One tick on this tick's prices. Changes the book; returns what happened.
    gate(pick) -> (ok, note) is asked before a buy, the news book's check;
    the plain book has none."""
    coins = market["coins"]
    marks = {s: c["price"] for s, c in coins.items()}
    btc = market.get("btc_change_24h")
    if book.get("btc_start") is None:
        book["btc_start"] = marks["BTC"]
    ev = {"exits": [], "dumped": False, "halt": None, "cycle": None}

    for pos in list(book["positions"]):
        why = exit_reason(pos, marks[pos["symbol"]], now)
        if why:
            ev["exits"].append(sell(book, pos, marks[pos["symbol"]], now, why))
    if btc is not None and btc < BTC_DUMP_PCT:
        ev["dumped"] = True
        for pos in [p for p in book["positions"] if p["symbol"] != "BTC"]:
            ev["exits"].append(sell(book, pos, marks[pos["symbol"]], now,
                                    f"BTC dump switch (BTC {btc:+.1f}% on the day)"))
    book["marks"] = marks
    eq = equity(book, marks)
    # The book's own valuation at this tick, the way Belfort's carries one:
    # the briefing and the Deck read these rather than redo the sum.
    book["market_value"] = round(eq, 2)
    for p in book["positions"]:
        p["last_price"] = marks[p["symbol"]]
        p["market_value"] = round(p["qty"] * marks[p["symbol"]], 2)
    ev["halt"] = update_halt(book, eq, now)
    book["last_tick_utc"] = iso(now)

    block = cycle_block(now)
    if book.get("last_cycle_block") == block:
        return ev
    book["last_cycle_block"], book["last_cycle_utc"] = block, iso(now)
    held = {p["symbol"] for p in book["positions"]}
    rows = [assess(s, c, btc, held) for s, c in coins.items()]
    best = sorted((r for r in rows if not r["out"] and r["score"] >= MIN_SCORE),
                  key=lambda r: (-r["score"], coins[r["symbol"]].get("rank") or 999))
    action, bought = None, None
    if book["halt"].get("active"):
        action = f"no entry: halted after a {MAX_DRAWDOWN_PCT:g}% drawdown, resumes {HALT_HOURS:g}h after it began"
    elif btc is None:
        action = "no entry: BTC's 24h change is missing, so the dump switch cannot be checked"
    elif btc < BTC_DUMP_PCT:
        action = f"no entry: BTC {btc:+.1f}% on the day, past the {BTC_DUMP_PCT:g}% dump switch"
    elif not best:
        top = max(rows, key=lambda r: (0 if r["out"] else 1, r["score"]))
        action = (f"no entry: nothing scored {MIN_SCORE}+ (best eligible "
                  f"{top['symbol']} at {top['score']})" if not top["out"]
                  else f"no entry: nothing eligible scored {MIN_SCORE}+")
    else:
        pick = best[0]
        notional = size_for(book, eq)
        if notional < MIN_TRADE:
            action = (f"no entry: {pick['symbol']} scored {pick['score']}, but the "
                      f"{CASH_FLOOR_PCT:g}% cash floor leaves {money(notional)} to spend")
        else:
            ok, note = gate(pick) if gate else (True, None)
            if not ok:
                action = f"no entry: {pick['symbol']} scored {pick['score']}, but the news check said no - {note}"
            else:
                bought = buy(book, pick["symbol"], marks[pick["symbol"]], notional, now,
                             f"scored {pick['score']}/10: " + "; ".join(pick["points"])
                             + (f" · news: {note}" if note else ""))
                action = (f"bought {pick['symbol']}: {money(notional)} at "
                          f"{money(marks[pick['symbol']])}, scored {pick['score']}/10"
                          + (f" · news: {note}" if note else ""))
    ev["cycle"] = {"rows": rows, "action": action, "bought": bought}
    return ev


# ---------------------------------------------------------------- the news

def http_text(url, timeout=FEED_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, text/xml"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        return r.read(2_000_000).decode("utf-8", "replace")


def parse_feed(text, source):
    """[{title, link, published, source}] from one RSS feed. A broken feed is
    no headlines, not a crash."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        title = " ".join((it.findtext("title") or "").split())
        try:
            when = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
            when = when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            when = None
        if title and when:
            out.append({"title": title, "link": (it.findtext("link") or "").strip(),
                        "published": iso(when), "source": source})
    return out


def fetch_news(get=http_text):
    """Every feed's headlines, newest first; a feed that fails is left out."""
    items = []
    for source, url in FEEDS:
        try:
            items += parse_feed(get(url), source)
        except Exception:
            continue
    return sorted(items, key=lambda x: x["published"], reverse=True)


def mentions(sym, title):
    if re.search(rf"(?<![A-Za-z$])\$?{re.escape(sym)}(?![A-Za-z])", title):
        return True
    return any(re.search(rf"\b{re.escape(n)}\b", title, re.I) for n in NAMES.get(sym, []))


def headlines_for(sym, items, now):
    """This coin's headlines of the last NEWS_HOURS, newest first, each title
    once, with the ids the question cites them by."""
    since, seen, out = now - timedelta(hours=NEWS_HOURS), set(), []
    for x in items:
        when = parse_iso(x["published"])
        key = x["title"].lower()
        if when and when >= since and key not in seen and mentions(sym, x["title"]):
            seen.add(key)
            out.append(dict(x, id=f"h{len(out) + 1}"))
        if len(out) >= NEWS_MAX:
            break
    return out


NEWS_ASK = """You judge news for a paper-trading crypto bot. Code has already checked the price trend; your only job is the news.

Coin: {name} ({sym})
Its headlines from the last {hours} hours:
{lines}

A CATALYST is a specific, checkable event that could move this coin: an exchange listing, an ETF filing, approval or inflows, a protocol upgrade or launch, a major partnership or adoption, a regulatory decision or court ruling in its favour.
NOT a catalyst: a headline that only describes a price move or a prediction ("X surges", "price analysis", "could hit $Y"), a market roundup, an opinion piece.
BAD NEWS for this coin: a hack or exploit, a large token unlock, a lawsuit, ban or delisting, an outage.

Reply with JSON only:
{{"score": 0-10, "headline": "the id (h1, h2...) of the one headline that is the catalyst, or none", "bad_news": true or false, "why": "one sentence"}}
Score 7 or more only for a specific positive event in one of the headlines above. A price-move headline is never above 6."""


def news_question(sym, heads):
    name = (NAMES.get(sym) or [sym])[0]
    lines = "\n".join(f"[{h['id']}] {h['published'][:16].replace('T', ' ')} UTC · {h['source']} · {h['title']}"
                      for h in heads)
    return NEWS_ASK.format(name=name, sym=sym, hours=NEWS_HOURS, lines=lines)


def read_verdict(text, heads):
    """(ok, score, headline, why) - code's reading of the answer. Anything it
    cannot read is a no: an unread check is not a passed one."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else None
    except ValueError:
        d = None
    if not isinstance(d, dict):
        return False, None, None, "the answer could not be read"
    score = d.get("score")
    score = score if isinstance(score, (int, float)) and not isinstance(score, bool) else None
    head = next((h for h in heads if h["id"] == str(d.get("headline") or "").strip()), None)
    why = " ".join(str(d.get("why") or "").split())[:200]
    if score is None:
        return False, None, head, "no score in the answer"
    if d.get("bad_news") is True:
        return False, score, head, f"bad news - {why}"
    if score < NEWS_MIN_SCORE:
        return False, score, head, f"scored {score:g}, under {NEWS_MIN_SCORE} - {why}"
    if not head:
        return False, score, None, "scored high without citing one of the headlines it was shown"
    return True, score, head, why


def news_wakes(now, bump=False):
    """Questions asked this Eastern day; bump=True counts one more first."""
    day = et_time.day(et_time.to_eastern(now))
    try:
        d = json.loads(NEWS_USE.read_text())
    except Exception:
        d = {}
    n = int(d.get("n") or 0) if d.get("day") == day else 0
    if bump:
        n += 1
        write_json(NEWS_USE, {"day": day, "n": n})
    return n


def _ask(question):
    spec = importlib.util.spec_from_file_location("wake_bolt", Path(__file__).resolve().parent / "wake.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    return w.ask("bolt", question, ASK_TIMEOUT)


def news_gate(pick, now, news=None, ask=None):
    """(ok, note) for the news book's buy of pick, and the call written down."""
    news, ask = news or fetch_news, ask or _ask
    sym = pick["symbol"]
    heads = headlines_for(sym, news(), now)
    rec = {"utc": iso(now), "symbol": sym, "score_10": pick["score"], "headlines": len(heads)}
    if not heads:
        ok, note = False, f"no {sym} headlines in the last {NEWS_HOURS}h - it buys only on a catalyst"
    elif news_wakes(now) >= NEWS_WAKES_PER_DAY:
        ok, note = False, f"today's {NEWS_WAKES_PER_DAY} news checks are used"
    else:
        news_wakes(now, bump=True)
        answered, text = ask(news_question(sym, heads))
        if not answered:
            ok, note = False, f"the news check could not run ({text[:120]})"
        else:
            ok, score, head, why = read_verdict(text, heads)
            rec.update(asked=True, news_score=score, headline=(head or {}).get("title"),
                       source=(head or {}).get("source"), link=(head or {}).get("link"))
            note = (f"{score:g}/10 on \"{head['title'][:90]}\" ({head['source']}) - {why}" if ok else why)
    rec.update(ok=ok, note=note)
    NEWS_CALLS.parent.mkdir(parents=True, exist_ok=True)
    with open(NEWS_CALLS, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return ok, note


# ---------------------------------------------------------------- checking

def problems(book):
    """Does the book add up? [] when it does.

    Cash is replayed from the trades alone - starting cash, minus every buy
    and its fee, plus every sale less its fee - and must equal the cash the
    book says it has. Each open position must be exactly what its trades
    left. This is the check that would have caught Belfort's -41%."""
    out = []
    cash = book["starting_cash"]
    held = {}
    for t in book["trades"]:
        if t["side"] == "buy":
            cash -= t["notional"] + t["fee"]
            held[t["symbol"]] = held.get(t["symbol"], 0.0) + t["qty"]
        else:
            cash += t["notional"] - t["fee"]
            held[t["symbol"]] = held.get(t["symbol"], 0.0) - t["qty"]
    if abs(cash - book["cash"]) > 0.01:
        out.append(f"cash is {money(book['cash'])} but the trades leave {money(cash)}")
    if book["cash"] < -0.01:
        out.append(f"cash is negative: {money(book['cash'])}")
    open_ = {}
    for p in book["positions"]:
        if p["symbol"] in open_:
            out.append(f"{p['symbol']} is held twice")
        open_[p["symbol"]] = p["qty"]
    for sym in set(held) | set(open_):
        want, have = held.get(sym, 0.0), open_.get(sym, 0.0)
        if abs(want - have) > 1e-9 * max(1.0, abs(want)):
            out.append(f"{sym}: the trades leave {want:.8g}, the book holds {have:.8g}")
    return out


# ---------------------------------------------------------------- writing it down

def et_clock(now):
    return et_time.to_eastern(now).strftime("%H:%M ET")


def report_path(now):
    return REPORTS / f"{et_time.day(et_time.to_eastern(now))}.md"


def append_report(now, text):
    path = report_path(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.is_file()
    with open(path, "a") as f:
        if new:
            f.write(f"# Bolt - {path.stem}\n\nPaper account, plain code: every number here "
                    f"was worked out by scripts/bolt.py.\n")
        f.write("\n" + text.rstrip() + "\n")


def btc_hold(book, marks):
    """$10,000 put into BTC on Bolt's first tick and left there."""
    start, now = book.get("btc_start"), (marks or {}).get("BTC")
    if not start or not now:
        return None
    return book["starting_cash"] * now / start


def pct(x, base):
    return (x / base - 1.0) * 100.0


def exit_line(t):
    return (f"sold {t['symbol']} at {money(t['price'])} - {t['reason']}: "
            f"{signed(t['pnl_pct'], 1)}% ({'+' if t['pnl'] >= 0 else '-'}{money(abs(t['pnl']))})")


def cycle_text(book, market, ev, now):
    marks = book["marks"]
    eq = equity(book, marks)
    start = book["starting_cash"]
    hold = btc_hold(book, marks)
    btc = market.get("btc_change_24h")
    h = book["halt"]
    lines = [f"## {et_clock(now)} - cycle", "",
             f"Equity **{money(eq)}** ({signed(pct(eq, start))}% since start)"
             + (f" · BTC held from day one: {money(hold)} ({signed(pct(hold, start))}%)" if hold else "")
             + f" · cash {money(book['cash'])} ({book['cash'] / eq * 100:.0f}%)"
             + f" · {len(book['positions'])} open", "",
             f"Breakers: BTC {signed(btc, 2) if btc is not None else '?'}% on the day "
             f"(dump switch at {BTC_DUMP_PCT:g}%) · "
             f"{pct(eq, h['reference']):+.1f}% from {money(h['reference'])} "
             f"(halt at -{MAX_DRAWDOWN_PCT:g}%)" + (" · **HALTED**" if h.get("active") else ""), ""]
    if ev["exits"]:
        lines += ["Sold this tick:"] + [f"- {exit_line(t)}" for t in ev["exits"]] + [""]
    lines += ["| coin | price | 20h avg | 50h avg | 1h | 24h | 7d | score | |",
              "|---|---|---|---|---|---|---|---|---|"]
    coins = market["coins"]
    fmt = lambda v: "?" if v is None else f"{v:+.1f}%"
    for r in sorted(ev["cycle"]["rows"], key=lambda r: (r["out"] is not None, -r["score"])):
        c = coins[r["symbol"]]
        lines.append(f"| {r['symbol']} | {c['price']:,.6g} | {c['ma_20h']:,.6g} | {c['ma_50h']:,.6g} | "
                     f"{fmt(c.get('change_1h'))} | {fmt(c.get('change_24h'))} | {fmt(c.get('change_7d'))} | "
                     f"{r['score']} | {r['out'] or ''} |")
    if book["positions"]:
        lines.append("")
    for p in book["positions"]:
        lines.append(f"- holding {p['symbol']}: {signed(pnl_pct(p, marks[p['symbol']]), 1)}%, "
                     f"stop {money(p['stop_price'])}{' (trailing)' if trailing(p) else ''}")
    lines += ["", f"**Action:** {ev['cycle']['action']}"]
    return "\n".join(lines)


def memory_line(book, ev):
    eq = equity(book)
    return (f"Equity {money(eq)} | Cash {money(book['cash'])} | Open {len(book['positions'])} | "
            f"{ev['cycle']['action']}")


def save_equity(book, now, path=None):
    path = path or EQUITY
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps({"t": iso(now), "equity": round(equity(book), 2),
                            "btc_hold": round(btc_hold(book, book["marks"]) or 0, 2),
                            "cash": round(book["cash"], 2)}) + "\n")


# ---------------------------------------------------------------- the commands

def cmd_tick(now=None, fetch=fetch_raw):
    now = now or utcnow()
    book = load_book(now)
    try:
        market = parse_markets(fetch(), now)
    except Exception as exc:
        why = f"{type(exc).__name__}: {exc}"
        print(f"bolt: no prices this tick - {why}. Nothing traded.", file=sys.stderr)
        book["last_error"] = {"utc": iso(now), "error": why[:300]}
        if book.get("last_cycle_block") != cycle_block(now):
            # A cycle that could not look at the market still happened, and
            # says so - in the report, in memory and on the screen.
            book["last_cycle_block"], book["last_cycle_utc"] = cycle_block(now), iso(now)
            append_report(now, f"## {et_clock(now)} - cycle\n\nNo prices: {why}. "
                               f"Nothing was traded - not even the exits, which need this tick's prices.")
            remember.remember(AGENT, f"No prices at {et_clock(now)}: nothing traded ({why[:80]})",
                              now=et_time.to_eastern(now))
        write_json(BOOK, book)
        return 1
    write_json(MARKET, market)
    book.pop("last_error", None)
    ev = run(book, market, now)

    bad = problems(book)
    if bad:
        # Our own arithmetic disagreeing with itself is a bug. The book is not
        # saved: a wrong number written down becomes the next tick's truth.
        print("bolt: THE BOOK DOES NOT BALANCE - nothing saved this tick:", file=sys.stderr)
        for b in bad:
            print(f"  - {b}", file=sys.stderr)
        return 1
    write_json(BOOK, book)
    save_equity(book, now)

    for t in ev["exits"]:
        if not ev["cycle"]:
            append_report(now, f"## {et_clock(now)} - {exit_line(t)}")
    if ev["halt"]:
        append_report(now, f"## {et_clock(now)} - {'HALTED' if ev['halt'] == 'halted' else 'resumed'}: "
                           + (f"down {MAX_DRAWDOWN_PCT:g}% - no new positions for {HALT_HOURS:g} hours"
                              if ev["halt"] == "halted" else
                              f"counting again from {money(book['halt']['reference'])}"))
    if ev["cycle"]:
        append_report(now, cycle_text(book, market, ev, now))
        remember.remember(AGENT, memory_line(book, ev), now=et_time.to_eastern(now))

    news_line = tick_news_book(market, now)

    eq = equity(book)
    print(f"bolt: {money(eq)} ({signed(pct(eq, book['starting_cash']))}%), cash {money(book['cash'])}, "
          f"{len(book['positions'])} open"
          + "".join(f"\n  {exit_line(t)}" for t in ev["exits"])
          + (f"\n  {ev['halt']}" if ev["halt"] else "")
          + (f"\n  cycle: {ev['cycle']['action']}" if ev["cycle"] else "")
          + (f"\n  {news_line}" if news_line else ""))
    return 0


def tick_news_book(market, now, gate=None):
    """The news book's turn on the same prices. Its own file and its own
    balance check; a failure here is said, and never stops the plain book."""
    try:
        nbook = load_book(now, NEWS_BOOK)
        nev = run(nbook, market, now, gate=gate or (lambda pick: news_gate(pick, now)))
        bad = problems(nbook)
        if bad:
            return "bolt + news: THE BOOK DOES NOT BALANCE - nothing saved: " + "; ".join(bad)
        write_json(NEWS_BOOK, nbook)
        save_equity(nbook, now, NEWS_EQUITY)
        for t in nev["exits"]:
            append_report(now, f"## {et_clock(now)} - Bolt + news: {exit_line(t)}")
        if nev["cycle"]:
            append_report(now, f"**Bolt + news:** {nev['cycle']['action']}")
        eq = equity(nbook)
        return (f"bolt + news: {money(eq)} ({signed(pct(eq, nbook['starting_cash']))}%)"
                + (f" - {nev['cycle']['action']}" if nev["cycle"] else ""))
    except Exception as exc:
        return f"bolt + news: failed this tick - {type(exc).__name__}: {str(exc)[:200]}"


def cmd_check():
    if not BOOK.is_file():
        print("bolt: no book yet - the first tick writes it.")
        return 0
    bad = problems(load_book())
    if bad:
        print("bolt: the book does NOT balance:")
        for b in bad:
            print(f"  - {b}")
        return 1
    print("bolt: the book balances.")
    return 0


def cmd_show():
    book = load_book()
    eq = equity(book)
    print(f"Bolt (paper): {money(eq)} ({signed(pct(eq, book['starting_cash']))}%), "
          f"cash {money(book['cash'])}, {len(book['positions'])} open")
    for p in book["positions"]:
        px = book["marks"].get(p["symbol"]) or p["entry_price"]
        print(f"  {p['symbol']:<5} {signed(pnl_pct(p, px), 1)}%  in at {money(p['entry_price'])}, "
              f"now {money(px)}, stop {money(p['stop_price'])}{' (trailing)' if trailing(p) else ''}")
    hold = btc_hold(book, book["marks"])
    if hold:
        print(f"  BTC held from day one: {money(hold)} ({signed(pct(hold, book['starting_cash']))}%)")
    if book["halt"].get("active"):
        print(f"  HALTED since {book['halt']['since_utc']}")
    return 0


def series(points, limit=400):
    """Every nth point, the last always kept, so a year of 10-minute ticks
    draws as a few hundred."""
    if len(points) <= limit:
        return points
    step = len(points) / float(limit)
    out = [points[int(i * step)] for i in range(limit)]
    if out[-1] is not points[-1]:
        out[-1] = points[-1]
    return out


def position_rows(book, marks, now):
    out = []
    for p in book["positions"]:
        px = marks.get(p["symbol"]) or p["entry_price"]
        opened = parse_iso(p["opened_utc"])
        out.append({**p, "price": px, "value": p["qty"] * px,
                    "pnl": p["qty"] * px - p["cost"], "pnl_pct": pnl_pct(p, px),
                    "held_hours": (now - opened).total_seconds() / 3600 if opened else None,
                    "trailing": trailing(p),
                    "sell_by_utc": iso(opened + timedelta(days=MAX_HOLD_DAYS)) if opened else None})
    return out


def closed_stats(book):
    sells = [t for t in book["trades"] if t["side"] == "sell"]
    wins = [t for t in sells if t["pnl"] > 0]
    return {"n": len(sells), "wins": len(wins), "losses": len(sells) - len(wins),
            "win_rate": len(wins) / len(sells) * 100 if sells else None,
            "realized": sum(t["pnl"] for t in sells),
            "fees": sum(t["fee"] for t in book["trades"])}


def read_curve(path):
    out = []
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def news_board(now, marks):
    """The news book, for the screen: its account, its holdings, and every
    question asked with code's verdict - worked out here, drawn there."""
    nb = load_book(now, NEWS_BOOK)
    eq = equity(nb, marks)
    calls = []
    if NEWS_CALLS.is_file():
        for line in NEWS_CALLS.read_text().splitlines()[-15:]:
            try:
                calls.append(json.loads(line))
            except ValueError:
                continue
    return {"started_utc": nb.get("started_utc"), "equity": eq, "cash": nb["cash"],
            "pnl": eq - nb["starting_cash"], "pnl_pct": pct(eq, nb["starting_cash"]),
            "positions": position_rows(nb, marks, now), "closed": closed_stats(nb),
            "trades": list(reversed(nb["trades"][-30:])), "curve": series(read_curve(NEWS_EQUITY)),
            "calls": list(reversed(calls)), "asked_today": news_wakes(now),
            "cap": NEWS_WAKES_PER_DAY, "hours": NEWS_HOURS, "min_score": NEWS_MIN_SCORE,
            "feeds": [n for n, _u in FEEDS], "balances": not problems(nb)}


def board(now=None):
    """Everything the screen shows. The page draws this and works nothing out."""
    now = now or utcnow()
    book = load_book(now)
    market = load_market()
    marks = (book.get("marks") or {}) if not market else {s: c["price"] for s, c in market["coins"].items()}
    eq = equity(book, marks)
    start = book["starting_cash"]
    hold = btc_hold(book, marks)
    btc = (market or {}).get("btc_change_24h")
    held = {p["symbol"] for p in book["positions"]}
    coins = []
    for sym, c in ((market or {}).get("coins") or {}).items():
        a = assess(sym, c, btc, held)
        coins.append({"symbol": sym, "name": c.get("name"), "price": c["price"],
                      "ma_20h": c["ma_20h"], "ma_50h": c["ma_50h"],
                      "change_1h": c.get("change_1h"), "change_24h": c.get("change_24h"),
                      "change_7d": c.get("change_7d"), "market_cap": c.get("market_cap"),
                      "volume_24h": c.get("volume_24h"), "score": a["score"], "points": a["points"],
                      "vs_ma20_pct": pct(c["price"], c["ma_20h"]),
                      "vs_ma50_pct": pct(c["price"], c["ma_50h"]),
                      "rubric": a["rubric"], "out": a["out"], "held": sym in held,
                      "spark": c.get("spark") or [],
                      "ma20_line": rolling(c.get("spark") or [], MA_FAST),
                      "ma50_line": rolling(c.get("spark") or [], MA_SLOW)})
    positions = position_rows(book, marks, now)
    curve = read_curve(EQUITY)
    fetched = parse_iso((market or {}).get("fetched_utc"))
    age = (now - fetched).total_seconds() / 60 if fetched else None
    nxt = datetime.fromtimestamp((cycle_block(now) + 1) * int(CYCLE_HOURS * 3600), timezone.utc)
    h = book["halt"]
    return {
        "mode": "PAPER", "starting_cash": start, "equity": eq, "cash": book["cash"],
        "cash_pct": book["cash"] / eq * 100 if eq else None,
        "pnl": eq - start, "pnl_pct": pct(eq, start),
        "btc_hold": {"start_price": book.get("btc_start"), "price": marks.get("BTC"),
                     "equity": hold, "pnl_pct": pct(hold, start) if hold else None},
        "positions": positions, "coins": coins,
        "breakers": {"btc_change_24h": btc, "dump_at": BTC_DUMP_PCT,
                     "dumping": btc is not None and btc < BTC_DUMP_PCT,
                     "from_reference_pct": pct(eq, h["reference"]), "reference": h["reference"],
                     "halt_at": -MAX_DRAWDOWN_PCT, "halted": bool(h.get("active")),
                     "halted_since": h.get("since_utc"), "resumed_utc": h.get("resumed_utc")},
        "closed": closed_stats(book),
        "news": news_board(now, marks),
        "trades": list(reversed(book["trades"][-40:])),
        "curve": series(curve),
        "market": {"fetched_utc": (market or {}).get("fetched_utc"), "age_min": age,
                   "stale": age is None or age > STALE_MINUTES, "source": (market or {}).get("source")},
        "last_error": book.get("last_error"),
        "started_utc": book.get("started_utc"), "last_tick_utc": book.get("last_tick_utc"),
        "last_cycle_utc": book.get("last_cycle_utc"), "next_cycle_utc": iso(nxt),
        "balances": not problems(book),
        "rules": {"stop_loss_pct": STOP_LOSS_PCT, "trail_pct": TRAIL_PCT,
                  "max_hold_days": MAX_HOLD_DAYS, "btc_dump_pct": BTC_DUMP_PCT,
                  "max_drawdown_pct": MAX_DRAWDOWN_PCT, "halt_hours": HALT_HOURS,
                  "target_pct": TARGET_PCT,
                  "cash_floor_pct": CASH_FLOOR_PCT, "fee_pct": FEE_PCT, "min_score": MIN_SCORE,
                  "cycle_hours": CYCLE_HOURS, "chase_pct": CHASE_PCT,
                  "min_market_cap": MIN_MARKET_CAP, "min_volume_24h": MIN_VOLUME_24H},
    }


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "tick":
        return cmd_tick()
    if cmd == "check":
        return cmd_check()
    if cmd == "show":
        return cmd_show()
    if cmd == "board":
        print(json.dumps(board()))
        return 0
    print("usage: bolt.py tick | show | check | board", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
