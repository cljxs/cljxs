#!/usr/bin/env python3
"""
belfort-fetch.py — market data fetcher for the Belfort agent.

Plain code. NO AI. This is the anti-hallucination half of the design: this
script pulls prices, computes every indicator locally, and writes JSON files.
Belfort READS those files. If a number is not in a data file, Belfort does not
cite it.

Standard library only — no pip installs, no compiler needed.
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = ROOT / "agents" / "belfort" / "data"

# ~30 US tech / momentum names, all well above $5B market cap.
UNIVERSE = [
    "NVDA", "AMD", "AVGO", "MU", "TSM", "SMCI", "ARM", "QCOM", "INTC", "MRVL",
    "PLTR", "COIN", "HOOD", "SHOP", "NET", "DDOG", "SNOW", "CRWD", "ZS", "PANW",
    "MDB", "TSLA", "META", "GOOGL", "AMZN", "MSFT", "AAPL", "UBER", "ABNB", "RBLX",
]

UA = "Mozilla/5.0 (compatible; belfort-fetch/1.0)"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"
NEWS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={syms}&region=US&lang=en-US"
REQUEST_GAP = 0.35          # be polite to a free endpoint
TIMEOUT = 20


def log(msg):
    print(f"[belfort-fetch {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def http_get(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
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


def macd(values, fast=12, slow=26, signal=9):
    if len(values) < slow + signal:
        return None, None, None
    ema_fast = ema_series(values, fast)
    ema_slow = ema_series(values, slow)
    offset = len(ema_fast) - len(ema_slow)
    line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    sig = ema_series(line, signal)
    if not sig:
        return None, None, None
    return line[-1], sig[-1], line[-1] - sig[-1]


def r2(x):
    return None if x is None else round(x, 2)


def fetch_one(sym):
    raw = http_get(CHART_URL.format(sym=sym))
    payload = json.loads(raw)
    result = payload["chart"]["result"][0]
    closes_raw = result["indicators"]["quote"][0]["close"]
    stamps = result["timestamp"]
    closes, dates = [], []
    for ts, c in zip(stamps, closes_raw):
        if c is not None:
            closes.append(float(c))
            dates.append(datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"))
    if len(closes) < 60:
        raise ValueError(f"only {len(closes)} usable closes")

    price = closes[-1]
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
    }


def fetch_news(symbols):
    try:
        raw = http_get(NEWS_URL.format(syms=",".join(symbols))).decode("utf-8", "replace")
    except Exception as exc:
        log(f"news fetch failed: {exc}")
        return []
    items, pos = [], 0
    while len(items) < 25:
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
            items.append({"title": title[:180], "published": tag("pubDate")})
    return items


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

    # Pre-screen in plain code so the agent reasons over a short list, not 30.
    candidates = sorted(
        [q for q in quotes.values() if q["mechanical_score"] == 3],
        key=lambda q: (-q["mechanical_score"], -(q["macd_hist"] or 0)),
    )

    (DATA_DIR / "quotes.json").write_text(json.dumps({
        "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(quotes),
        "quotes": quotes,
    }, indent=1) + "\n")

    (DATA_DIR / "candidates.json").write_text(json.dumps({
        "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "Passed all three mechanical screens (trend, RSI band, MACD). "
                "Belfort must still identify a nameable catalyst and score 7+ before opening.",
        "candidates": candidates,
    }, indent=1) + "\n")

    news = fetch_news(UNIVERSE[:12])
    (DATA_DIR / "news.json").write_text(json.dumps({
        "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
        "headlines": news,
    }, indent=1) + "\n")

    (DATA_DIR / "_meta.json").write_text(json.dumps({
        "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
        "universe_size": len(UNIVERSE),
        "fetched_ok": len(quotes),
        "failed": failures,
        "news_items": len(news),
        "source": "Yahoo Finance public chart endpoint (no API key)",
    }, indent=1) + "\n")

    log(f"ok: {len(quotes)}/{len(UNIVERSE)} quotes, {len(candidates)} candidates, {len(news)} headlines")
    if failures:
        log(f"{len(failures)} failed: {[f['symbol'] for f in failures]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
