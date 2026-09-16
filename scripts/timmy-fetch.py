#!/usr/bin/env python3
"""
timmy-fetch.py — market data fetcher for the Timmy agent.

Plain code. NO AI. This is the anti-hallucination half of the design: this
script pulls candles and headlines, computes every indicator locally, and
writes JSON files. Timmy READS those files. If a number is not in a data
file, Timmy does not cite it.

Two modes:

    timmy-fetch.py              fetch and write the data files
    timmy-fetch.py --status     print how old the data is and whether the
                                cycle should proceed - see below

--status exists because the bail-out rule ("data older than 24h = skip the
cycle") is a time calculation, and a language model comparing timestamps is
exactly the kind of thing that goes quietly wrong. So plain code makes the
call and prints a verdict; Timmy only has to obey it.

Standard library only - no pip installs, no compiler needed.
"""

import html
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from indicators import sma, rsi, macd, pct_change, r2   # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
AGENT_DIR = ROOT / "agents" / "timmy"
DATA_DIR = AGENT_DIR / "data"
WATCHLIST = AGENT_DIR / "state" / "watchlist.json"

DEFAULT_WATCHLIST = ["HIMS", "ASTS", "UBER", "IREN"]

UA = "Mozilla/5.0 (compatible; timmy-fetch/1.0)"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d"
NEWS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
REQUEST_GAP = 0.4           # be polite to a free endpoint
TIMEOUT = 20
STALE_HOURS = 24            # the spec's bail-out threshold
HEADLINES_PER_TICKER = 8


def log(msg):
    print(f"[timmy-fetch {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


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


def read_watchlist():
    """The watchlist is a file so it can be changed without editing code."""
    try:
        j = json.loads(WATCHLIST.read_text())
        syms = j.get("tickers") if isinstance(j, dict) else j
        clean = [str(s).strip().upper() for s in syms if str(s).strip()]
        if clean:
            return clean
    except Exception:
        pass
    return list(DEFAULT_WATCHLIST)


# ------------------------------------------------------------------ fetching

def fetch_one(sym):
    payload = json.loads(http_get(CHART_URL.format(sym=sym)))
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {}) or {}
    closes_raw = result["indicators"]["quote"][0]["close"]
    vols_raw = result["indicators"]["quote"][0].get("volume") or []
    stamps = result["timestamp"]

    closes, dates, vols = [], [], []
    for i, (ts, c) in enumerate(zip(stamps, closes_raw)):
        if c is not None:
            closes.append(float(c))
            dates.append(datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"))
            v = vols_raw[i] if i < len(vols_raw) else None
            vols.append(float(v) if v is not None else None)
    if len(closes) < 60:
        raise ValueError(f"only {len(closes)} usable closes")

    price = closes[-1]
    s20, s50 = sma(closes, 20), sma(closes, 50)
    m_line, m_sig, m_hist = macd(closes)
    vol_clean = [v for v in vols[-20:] if v is not None]

    return {
        "symbol": sym,
        "name": meta.get("longName") or meta.get("shortName") or sym,
        "exchange": meta.get("fullExchangeName"),
        "currency": meta.get("currency"),
        "price": r2(price),
        "prev_close": r2(closes[-2]) if len(closes) > 1 else None,
        "change_1d_pct": r2(pct_change(closes, 1)),
        "change_5d_pct": r2(pct_change(closes, 5)),
        "change_30d_pct": r2(pct_change(closes, 30)),
        "sma20": r2(s20),
        "sma50": r2(s50),
        "pct_from_sma20": r2((price / s20 - 1) * 100) if s20 else None,
        "pct_from_sma50": r2((price / s50 - 1) * 100) if s50 else None,
        "sma20_above_sma50": bool(s20 and s50 and s20 > s50),
        "rsi14": r2(rsi(closes, 14)),
        "macd": r2(m_line),
        "macd_signal": r2(m_sig),
        "macd_hist": r2(m_hist),
        "high_52w": r2(max(closes)),
        "low_52w": r2(min(closes)),
        "pct_from_52w_high": r2((price / max(closes) - 1) * 100),
        "avg_volume_20d": int(sum(vol_clean) / len(vol_clean)) if vol_clean else None,
        "last_bar": dates[-1],
        "bars_used": len(closes),
    }


def fetch_headlines(sym):
    """Per-ticker, unlike Belfort's pooled feed - Timmy writes one report per
    ticker and must not attribute another company's news to this one."""
    try:
        raw = http_get(NEWS_URL.format(sym=sym)).decode("utf-8", "replace")
    except Exception as exc:
        log(f"{sym}: headlines failed - {exc}")
        return []

    items, pos = [], 0
    while len(items) < HEADLINES_PER_TICKER:
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
            # Feed titles are XML-escaped: "Hims &amp; Hers" must not reach a
            # report looking like that.
            return html.unescape(txt)

        title = tag("title")
        if title:
            items.append({
                "title": title[:200],
                "published": tag("pubDate"),
                "link": tag("link"),
            })
    return items


# -------------------------------------------------------------- the --status

def status():
    """Print the data's age and a PROCEED / SKIP verdict for the cycle."""
    meta_path = DATA_DIR / "_meta.json"
    try:
        meta = json.loads(meta_path.read_text())
        asof = datetime.strptime(meta["asof_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception as exc:
        print("DATA AGE: no readable data file "
              f"({meta_path.name}: {type(exc).__name__})")
        print("VERDICT: SKIP THIS CYCLE. Write no analysis. Append one line to "
              "MEMORY.md saying the data file was missing or unreadable.")
        return 1

    age_h = (datetime.now(timezone.utc) - asof).total_seconds() / 3600.0
    age_txt = f"{age_h * 60:.0f} minutes" if age_h < 2 else f"{age_h:.1f} hours"
    print(f"DATA AGE: {age_txt} (fetched {meta['asof_utc']} UTC)")

    bars = {t: d.get("last_bar") for t, d in (meta.get("last_bars") or {}).items()}
    if bars:
        print(f"LAST BAR: {', '.join(f'{k} {v}' for k, v in bars.items())}")
    if meta.get("failed"):
        print(f"FAILED TICKERS: {[f['symbol'] for f in meta['failed']]} "
              f"- do not write a report for these, say the fetch failed")

    if age_h > STALE_HOURS:
        print(f"VERDICT: SKIP THIS CYCLE. The data is older than {STALE_HOURS}h. "
              "Do not analyse it as if it were fresh. Write no per-ticker report. "
              "Append one line to MEMORY.md recording the skip and the data age.")
        return 1

    print("VERDICT: PROCEED. Data is fresh enough to analyse.")
    if meta.get("stale_bar_days", 0) >= 4:
        print(f"NOTE: the most recent bar is {meta['stale_bar_days']} calendar days old "
              "(a long weekend or holiday). The prices are last session's close, not live - "
              "say which session you are reading.")
    return 0


# --------------------------------------------------------------------- main

def main():
    if "--status" in sys.argv:
        return status()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    watchlist = read_watchlist()

    tickers, headlines, failures = {}, {}, []
    for i, sym in enumerate(watchlist):
        try:
            tickers[sym] = fetch_one(sym)
        except Exception as exc:
            failures.append({"symbol": sym, "error": str(exc)[:140]})
            log(f"{sym}: FAILED - {exc}")
        time.sleep(REQUEST_GAP)
        headlines[sym] = fetch_headlines(sym)
        if i < len(watchlist) - 1:
            time.sleep(REQUEST_GAP)

    # A partial fetch is still useful - Timmy is told to skip the tickers that
    # failed. A total failure is not: keep yesterday's files rather than
    # replace them with nothing, and let --status report the growing age.
    if not tickers:
        log("no tickers fetched at all - leaving previous data files untouched")
        return 1

    stale_bar_days = 0
    for d in tickers.values():
        try:
            bar = datetime.strptime(d["last_bar"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            stale_bar_days = max(stale_bar_days, (started - bar).days)
        except Exception:
            pass

    stamp = started.strftime("%Y-%m-%d %H:%M:%S")

    (DATA_DIR / "tickers.json").write_text(json.dumps({
        "asof_utc": stamp,
        "note": "Every number here was computed in plain Python from Yahoo daily "
                "closes. Cite these values verbatim. Do not compute your own.",
        "tickers": tickers,
    }, indent=1) + "\n")

    (DATA_DIR / "headlines.json").write_text(json.dumps({
        "asof_utc": stamp,
        "note": "Headlines are per ticker and are TITLES ONLY - the article text "
                "was not fetched. Treat a headline as a thing that was published, "
                "not as a fact you have verified.",
        "headlines": headlines,
    }, indent=1) + "\n")

    (DATA_DIR / "_meta.json").write_text(json.dumps({
        "asof_utc": stamp,
        "watchlist": watchlist,
        "fetched_ok": sorted(tickers),
        "failed": failures,
        "last_bars": {t: {"last_bar": d["last_bar"]} for t, d in tickers.items()},
        "stale_bar_days": stale_bar_days,
        "headline_counts": {t: len(h) for t, h in headlines.items()},
        "stale_after_hours": STALE_HOURS,
        "source": "Yahoo Finance public chart endpoint + per-ticker RSS (no API key)",
    }, indent=1) + "\n")

    log(f"ok: {len(tickers)}/{len(watchlist)} tickers, "
        f"{sum(len(h) for h in headlines.values())} headlines, last bar {stale_bar_days}d old")
    if failures:
        log(f"{len(failures)} failed: {[f['symbol'] for f in failures]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
