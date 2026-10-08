#!/usr/bin/env python3
"""
belfort-fetch.py — market data fetcher for the Belfort agent.

Plain code. NO AI. This is the anti-hallucination half of the design: this
script pulls prices, computes every indicator locally, and writes JSON files.
Belfort READS those files. If a number is not in a data file, Belfort does not
cite it.

Standard library only — no pip installs, no compiler needed.
"""

import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import et_time  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = ROOT / "agents" / "belfort" / "data"

# US names, all well above $5B market cap. The first 30 are tech and
# momentum; the owner added the four groups after them on 2026-10-06 because
# most of what Belfort picked was chips. Every name needs a cluster in
# belfort-trade.py's CLUSTERS, or the 40% cap cannot see it.
UNIVERSE = [
    "NVDA", "AMD", "AVGO", "MU", "TSM", "SMCI", "ARM", "QCOM", "INTC", "MRVL",
    "PLTR", "COIN", "HOOD", "SHOP", "NET", "DDOG", "SNOW", "CRWD", "ZS", "PANW",
    "MDB", "TSLA", "META", "GOOGL", "AMZN", "MSFT", "AAPL", "UBER", "ABNB", "RBLX",
    "LLY", "ISRG", "VRTX", "ABBV", "HIMS",
    "JPM", "GS", "V", "MA",
    "GE", "CAT", "ETN", "XOM",
    "COST", "NFLX", "WMT", "NKE",
    "SPCX", "RKLB", "ASTS", "LUNR", "PL", "RDW",
]

# What Belfort is shown each wake. Code screens every name; he judges at most
# MAX_CANDIDATES of those that pass, no more than CANDIDATES_PER_CLUSTER from
# one group, so a chip rally cannot fill his whole list - and what he reads,
# which is what he costs, stays the same size however wide the list grows.
MAX_CANDIDATES = 10
CANDIDATES_PER_CLUSTER = 3

# Not traded - read by belfort-trade.py's regime() and stats: the market
# Belfort's universe lives in, and what holding it instead would have made.
BENCHMARKS = ["QQQ", "SPY"]

UA = "Mozilla/5.0 (compatible; belfort-fetch/1.0)"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"
NEWS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={syms}&region=US&lang=en-US"
REQUEST_GAP = 0.35          # be polite to a free endpoint
TIMEOUT = 20
# Daily closes kept per name: the memory belfort-trade.py's exit rules read
# (the highest close since entry, trading days held).
HISTORY_BARS = 90
# The Markets page's candles (owner, 2026-10-06): open/high/low/close/volume
# and the indicator lines, in data/bars.json - NOT quotes.json, which
# Belfort reads every wake. 90 days of five numbers for 32 names would
# multiply what he reads for nothing he decides with.
CHART_BARS = 90
CHART_MACD_BARS = 30
# News, one request per name. It used to be one request for the first 12 of
# the 30 names, which Yahoo answers with 20 headlines between them - and 18
# names, two of them held, had none. Hourly, not every 10 minutes: 30
# requests a fetch would be 1,300 a day to a free endpoint for headlines that
# change a few times a day.
NEWS_PER_NAME = 6
NEWS_EVERY_SECONDS = 55 * 60
# Earnings dates, from Nasdaq's public calendar (no key; checked 2026-10-01:
# TSM on the 15th, INTC the 22nd ...). One request a weekday for the next
# EARNINGS_DAYS, twice a day - dates move rarely and 15 requests every ten
# minutes would be rude.
EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings?date={day}"
# Nasdaq holds a request open until it times out when the user agent names a
# bot (seen 2026-10-01: "compatible; belfort-fetch" stalled every day; a
# plain "Mozilla/5.0" was answered at once).
EARNINGS_UA = "Mozilla/5.0"
EARNINGS_DAYS = 21
EARNINGS_EVERY_SECONDS = 12 * 3600


def log(msg):
    print(f"[belfort-fetch {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def http_get(url, retries=2, ua=None):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua or UA, "Accept": "application/json, */*"})
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                return r.read()
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last


# --- indicators, all computed here in plain code --------------------------

def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema_series(values, n):
    if len(values) < n:
        return []
    k = 2.0 / (n + 1)
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values, n=14):
    """Wilder's RSI."""
    if len(values) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0.0)) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd_series(values, fast=12, slow=26, signal=9):
    """(line, signal, histogram) lists, newest last; the histogram is as
    long as the signal line. Empty when there is not enough history."""
    if len(values) < slow + signal:
        return [], [], []
    ema_fast = ema_series(values, fast)
    ema_slow = ema_series(values, slow)
    offset = len(ema_fast) - len(ema_slow)
    line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    sig = ema_series(line, signal)
    return line, sig, [m - g for m, g in zip(line[len(line) - len(sig):], sig)]


def macd(values, fast=12, slow=26, signal=9):
    line, sig, hist = macd_series(values, fast, slow, signal)
    if not sig:
        return None, None, None
    return line[-1], sig[-1], hist[-1]


def atr(highs, lows, closes, n=14):
    """Average true range over the last n days: how far the price usually
    moves in a day, gaps included. belfort-trade.py sets stops and sizes from
    it, so a wild name gets room and a calm one does not over-risk."""
    trs = [max(h - l, abs(h - pc), abs(l - pc))
           for h, l, pc in zip(highs[1:], lows[1:], closes[:-1])]
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n


def r2(x):
    return None if x is None else round(x, 2)


def fetch_one(sym):
    raw = http_get(CHART_URL.format(sym=sym))
    payload = json.loads(raw)
    result = payload["chart"]["result"][0]
    bars = result["indicators"]["quote"][0]
    stamps = result["timestamp"]
    closes, dates, highs, lows, opens, vols = [], [], [], [], [], []
    n = len(stamps)
    for ts, c, h, l, o, v in zip(stamps, bars["close"], bars.get("high") or [], bars.get("low") or [],
                                 bars.get("open") or [None] * n, bars.get("volume") or [None] * n):
        if c is not None and h is not None and l is not None:
            closes.append(float(c))
            highs.append(float(h))
            lows.append(float(l))
            opens.append(float(o) if o is not None else float(c))
            vols.append(int(v or 0))
            dates.append(datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"))
    if len(closes) < 60:
        raise ValueError(f"only {len(closes)} usable closes")

    price = closes[-1]
    a14 = atr(highs, lows, closes)
    s20, s50 = sma(closes, 20), sma(closes, 50)
    r14 = rsi(closes, 14)
    m_line, m_sig, m_hist = macd(closes)

    trend_ok = bool(s20 and s50 and price > s20 > s50)
    rsi_ok = bool(r14 is not None and 40 <= r14 <= 65)
    macd_ok = bool(m_hist is not None and m_line is not None and m_line > 0 and m_hist > 0)

    return {
        "symbol": sym,
        "price": r2(price),
        "prev_close": r2(closes[-2]) if len(closes) > 1 else None,
        "change_pct": r2((price / closes[-2] - 1) * 100) if len(closes) > 1 else None,
        "sma20": r2(s20),
        "sma50": r2(s50),
        "rsi14": r2(r14),
        "macd": r2(m_line),
        "macd_signal": r2(m_sig),
        "macd_hist": r2(m_hist),
        "pct_from_sma20": r2((price / s20 - 1) * 100) if s20 else None,
        # Mechanical screen components. Belfort still judges the catalyst and
        # sets the final 0-10 score; these three are pure arithmetic, so they
        # are computed here rather than guessed by a language model.
        "trend_ok": trend_ok,
        "rsi_ok": rsi_ok,
        "macd_ok": macd_ok,
        "mechanical_score": sum([trend_ok, rsi_ok, macd_ok]),
        "last_bar": dates[-1],
        "history": [[d, r2(c)] for d, c in zip(dates[-HISTORY_BARS:], closes[-HISTORY_BARS:])],
        "atr14": r2(a14),
        "atr_pct": r2(a14 / price * 100) if a14 and price else None,
        "_chart": chart(dates, opens, highs, lows, closes, vols),
    }


def chart(dates, opens, highs, lows, closes, vols):
    """The Markets page's view of one name, for data/bars.json: candles and
    the same SMA and MACD arithmetic the quote uses, as lines. Volume today
    is against the 20 days before it - today's own volume would pull the
    average toward itself."""
    k = min(CHART_BARS, len(closes))
    first = len(closes) - k
    _, _, hist = macd_series(closes)
    prior = vols[-21:-1]
    avg = sum(prior) / len(prior) if prior and any(prior) else None
    return {
        "bars": [[d, r2(o), r2(h), r2(lo), r2(c), v] for d, o, h, lo, c, v in
                 zip(dates[first:], opens[first:], highs[first:], lows[first:], closes[first:], vols[first:])],
        "sma20": [r2(sma(closes[:i + 1], 20)) for i in range(first, len(closes))],
        "sma50": [r2(sma(closes[:i + 1], 50)) for i in range(first, len(closes))],
        "macd_hist": [r2(x) for x in hist[-CHART_MACD_BARS:]],
        "volume": vols[-1] if vols else None,
        "volume_avg20": int(avg) if avg else None,
        "volume_ratio": r2(vols[-1] / avg) if avg and vols else None,
    }


def parse_rss(raw, limit=25):
    """[{title, published}] from a Yahoo headline RSS feed."""
    items, pos = [], 0
    while len(items) < limit:
        start = raw.find("<item>", pos)
        if start == -1:
            break
        end = raw.find("</item>", start)
        if end == -1:
            break
        block = raw[start:end]
        pos = end

        def tag(name):
            a = block.find(f"<{name}>")
            b = block.find(f"</{name}>", a)
            if a == -1 or b == -1:
                return None
            txt = block[a + len(name) + 2:b].strip()
            if txt.startswith("<![CDATA["):
                txt = txt[9:-3].strip() if txt.endswith("]]>") else txt[9:].strip()
            return txt

        title = tag("title")
        if title:
            link = tag("link") or ""
            host = urllib.parse.urlsplit(link).hostname or ""
            items.append({"title": title[:180], "published": tag("pubDate"),
                          # what a buy cites (belfort-trade.py --headline): the
                          # same headline keeps the same id across refreshes
                          "id": hashlib.sha1(title.encode()).hexdigest()[:8],
                          "publisher": host[4:] if host.startswith("www.") else host})
    return items


def fetch_news(symbols, get=None, gap=REQUEST_GAP):
    """Up to NEWS_PER_NAME headlines for EACH name, tagged with it. A
    headline about several names is kept once, under the first."""
    get = get or http_get
    out, seen = [], set()
    for i, sym in enumerate(symbols):
        try:
            raw = get(NEWS_URL.format(syms=sym)).decode("utf-8", "replace")
        except Exception as exc:
            log(f"news {sym}: {exc}")
            continue
        for item in parse_rss(raw, NEWS_PER_NAME):
            if item["title"] not in seen:
                seen.add(item["title"])
                out.append(dict(item, symbol=sym))
        if gap and i < len(symbols) - 1:
            time.sleep(gap)
    return out


def fetch_earnings(symbols, today=None, get=None, gap=REQUEST_GAP):
    """{symbol: "YYYY-MM-DD"} - the next report date of each name that
    reports in the next EARNINGS_DAYS. None if no day could be read at all,
    so a failed fetch never reads as "nobody reports"."""
    get = get or (lambda url: http_get(url, retries=0, ua=EARNINGS_UA))
    today = today or et_time.eastern_now().date()
    out, read = {}, 0
    for i in range(EARNINGS_DAYS):
        day = today + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        try:
            rows = ((json.loads(get(EARNINGS_URL.format(day=day))).get("data") or {}).get("rows")) or []
            read += 1
        except Exception as exc:
            log(f"earnings {day}: {exc}")
            continue
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            if sym in symbols and sym not in out:
                out[sym] = str(day)
        if gap:
            time.sleep(gap)
    return out if read else None


def file_is_fresh(path, seconds, now=None):
    try:
        d = json.loads(path.read_text())
        return (now or time.time()) - datetime.strptime(
            d["asof_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() < seconds
    except Exception:
        return False


def momentum(q):
    """The MACD histogram as a share of price. The raw histogram is in
    dollars, so ranking by it put a $900 stock ahead of a $50 one with the
    same push behind it."""
    return (q.get("macd_hist") or 0) / q["price"] if q.get("price") else 0


def _cluster_of():
    """belfort-trade.py's cluster_of: the groups live with the cap that uses
    them. Loaded by path - the hyphen keeps it from a normal import."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("belfort_trade_clusters",
                                                  Path(__file__).resolve().parent / "belfort-trade.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.cluster_of


def shortlist(quotes, cluster_of=None):
    """(shown, left_out): the names that pass all three screens, strongest
    first, at most CANDIDATES_PER_CLUSTER per cluster and MAX_CANDIDATES in
    all. left_out is [{symbol, cluster, why}] for what passed and was not shown."""
    cluster_of = cluster_of or _cluster_of()
    shown, left_out, per = [], [], {}
    for q in sorted((q for q in quotes if q.get("mechanical_score") == 3), key=lambda q: -momentum(q)):
        group = cluster_of(q["symbol"])
        if per.get(group, 0) >= CANDIDATES_PER_CLUSTER:
            why = f"already {CANDIDATES_PER_CLUSTER} from {group}"
        elif len(shown) >= MAX_CANDIDATES:
            why = f"list full at {MAX_CANDIDATES}"
        else:
            per[group] = per.get(group, 0) + 1
            shown.append(q)
            continue
        left_out.append({"symbol": q["symbol"], "cluster": group, "why": why})
    return shown, left_out


def news_is_fresh(path, now=None):
    """Whether news.json was written within NEWS_EVERY_SECONDS, for every name."""
    try:
        d = json.loads(path.read_text())
        age = (now or time.time()) - datetime.strptime(
            d["asof_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        return age < NEWS_EVERY_SECONDS and set(d.get("symbols") or []) >= set(UNIVERSE)
    except Exception:
        return False


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)

    quotes, failures = {}, []
    for i, sym in enumerate(UNIVERSE):
        try:
            quotes[sym] = fetch_one(sym)
        except Exception as exc:
            failures.append({"symbol": sym, "error": str(exc)[:120]})
            log(f"{sym}: FAILED — {exc}")
        if i < len(UNIVERSE) - 1:
            time.sleep(REQUEST_GAP)

    if not quotes:
        log("no quotes fetched at all — leaving previous data files untouched")
        return 1

    benchmarks = {}
    for sym in BENCHMARKS:
        time.sleep(REQUEST_GAP)
        try:
            benchmarks[sym] = fetch_one(sym)
        except Exception as exc:
            failures.append({"symbol": sym, "error": str(exc)[:120]})
            log(f"{sym}: FAILED — {exc}")

    # Pre-screen in plain code so the agent reasons over a short list.
    candidates, left_out = shortlist(quotes.values())

    charts = {sym: q.pop("_chart") for sym, q in list(quotes.items()) + list(benchmarks.items())
              if "_chart" in q}
    (DATA_DIR / "bars.json").write_text(json.dumps({
        "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "For the Markets page only. Belfort decides from quotes.json and candidates.json.",
        "bars": charts,
    }, separators=(",", ":")) + "\n")

    (DATA_DIR / "quotes.json").write_text(json.dumps({
        "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(quotes),
        "quotes": quotes,
        "benchmarks": benchmarks,
    }, indent=1) + "\n")

    (DATA_DIR / "candidates.json").write_text(json.dumps({
        "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "Passed all three mechanical screens (trend, RSI band, MACD). "
                "Belfort must still identify a nameable catalyst and score 7+ before opening. "
                f"At most {MAX_CANDIDATES}, at most {CANDIDATES_PER_CLUSTER} per cluster, ranked by "
                "MACD histogram as a share of price. Daily closes are in quotes.json.",
        "screened": len(quotes),
        "passed": len(candidates) + len(left_out),
        "candidates": [{k: v for k, v in q.items() if k != "history"} for q in candidates],
        "left_out": left_out,
    }, indent=1) + "\n")

    news_file = DATA_DIR / "news.json"
    if news_is_fresh(news_file):
        news = json.loads(news_file.read_text())["headlines"]
    else:
        news = fetch_news(UNIVERSE)
        if news:
            news_file.write_text(json.dumps({
                "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
                "symbols": UNIVERSE,
                "headlines": news,
            }, indent=1) + "\n")

    earnings_file = DATA_DIR / "earnings.json"
    if not file_is_fresh(earnings_file, EARNINGS_EVERY_SECONDS):
        dates = fetch_earnings(UNIVERSE)
        if dates is not None:
            earnings_file.write_text(json.dumps({
                "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
                "source": "Nasdaq earnings calendar", "days_ahead": EARNINGS_DAYS,
                "dates": dates,
            }, indent=1) + "\n")

    # Eastern, not UTC. The 09:35 ET open is 13:35 UTC, which looks like the
    # afternoon - and that is exactly how it got filed as `-close.md`.
    now_et = et_time.eastern_now()
    (DATA_DIR / "_meta.json").write_text(json.dumps({
        "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
        "day_et": et_time.day(now_et),
        "slot": et_time.slot("belfort", now_et),
        "report_name": et_time.report_name("belfort", now_et),
        "universe_size": len(UNIVERSE),
        "fetched_ok": len(quotes),
        "failed": failures,
        "news_items": len(news),
        "source": "Yahoo Finance public chart endpoint (no API key)",
    }, indent=1) + "\n")

    log(f"ok: {len(quotes)}/{len(UNIVERSE)} quotes, {len(candidates)} candidates shown"
        f" ({len(left_out)} more passed), {len(news)} headlines")
    if failures:
        log(f"{len(failures)} failed: {[f['symbol'] for f in failures]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
