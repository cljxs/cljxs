#!/usr/bin/env python3
"""Warren - the stock analyst. The owner's Before-You-Buy Checklist, filled in.

Owner, 2026-11-19: "I fill in the stuff like the stock symbol, how much I
wanna invest and the time line. All the data Claude does the research for.
Use the same steps and process as the worked example."

The six steps and their green / yellow / red rules are the checklist's own
(the Before-You-Buy Stock Checklist and its worked Kinder Morgan example).
Every number here is computed by this script from public data; one question
on Opus (the owner's Claude plan, wake.py ask, never a bill) writes only the
parts that need judgment - what the company does, its rivals, its risks, sell
rules - and code checks that answer before it is used (`read_answer`).

    warren.py check KMI --amount 125 --months 12 --goal both --why "..." --hold-drop yes
    warren.py show KMI          the latest check, as JSON
    warren.py deck              what the page shows
    warren.py holdings add KMI 125 [--sector Energy] | holdings remove KMI
    warren.py recheck           re-run every saved stock that filed a new report

Data (all free, no keys):
    SEC EDGAR companyfacts      revenue, EPS, cash flow, dividends, debt, interest
    Yahoo chart (as Belfort)    price, 52-week range, 200-day average, dividends paid
    Nasdaq                      sector, market cap, analysts' EPS forecasts
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS))
import et_time  # noqa: E402

AGENT = ROOT / "agents" / "warren"
STATE = AGENT / "state"
CHECKS = STATE / "checks"
CACHE = STATE / "cache"
HOLDINGS = STATE / "holdings.json"
RUNS = STATE / "runs.json"
RECHECKS = STATE / "rechecks.json"

# Opus on the plan is the owner's to spend; a check is one question, a
# re-check another. Ten a day is far more than one person buys.
RUNS_PER_DAY = 10
ASK_TIMEOUT = 600

# The SEC refuses (403) any request whose User-Agent lacks a name and a
# contact email - its fair-access rule. The first live check (HIMS,
# 2026-11-19) was refused for exactly that. The owner's contact is his to
# set, never in the code: WARREN_SEC_UA, or one line in this file.
SEC_CONTACT = STATE / "sec-contact"
SEC_HELP = ("the SEC needs a contact email before it will answer. Set it once on the droplet: "
            "echo you@example.com > agents/warren/state/sec-contact")


def sec_ua():
    if os.environ.get("WARREN_SEC_UA"):
        return os.environ["WARREN_SEC_UA"]
    try:
        contact = SEC_CONTACT.read_text().strip()
    except OSError:
        contact = ""
    if "@" not in contact or " " in contact:
        raise FetchError(SEC_HELP)
    return f"cljxs-ecosystem warren {contact}"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{tag}.json"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1y&interval=1d&events=div"
SUMMARY_URL = "https://api.nasdaq.com/api/quote/{sym}/summary?assetclass=stocks"
FORECAST_URL = "https://api.nasdaq.com/api/analyst/{sym}/earnings-forecast"

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
GREEN, YELLOW, RED = "Green", "Yellow", "Red"

SECTORS = ["Technology", "Communication Services", "Consumer Discretionary", "Consumer Staples",
           "Health Care", "Financials", "Industrials", "Energy", "Utilities", "Materials", "Real Estate"]
# The checklist's rough normal forward P/E by sector (Step 5).
PE_TYPICAL = {"Technology": (22, 30), "Consumer Discretionary": (22, 30), "Consumer Staples": (18, 23),
              "Industrials": (18, 23), "Health Care": (15, 20), "Utilities": (15, 20), "Materials": (15, 20),
              "Communication Services": (16, 22), "Financials": (11, 15), "Energy": (9, 14)}
# Pipelines, utilities, REITs and towers normally carry 3.5-5.5x (Step 4).
CAPITAL_HEAVY_OK = {"Energy", "Utilities", "Real Estate", "Communication Services"}

# Which SEC tag answers each question, in order of preference. Companies
# change tags over the years (Kinder Morgan's interest went from
# InterestExpense to InterestIncomeExpenseNonoperatingNet), so `pick_tag`
# takes the one with the newest annual figure, then this order.
TAGS = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
    "eps": ("EarningsPerShareDiluted",),
    "net_income": ("NetIncomeLoss",),
    "ocf": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
    "dividends": ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends", "DividendsCommonStockCash"),
    "buybacks": ("PaymentsForRepurchaseOfCommonStock",),
    "op_income": ("OperatingIncomeLoss",),
    "dna": ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
            "DepreciationAmortizationAndAccretionNet"),
    "interest": ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt",
                 "InterestIncomeExpenseNonoperatingNet", "InterestPaidNet"),
    "debt_issued": ("ProceedsFromIssuanceOfLongTermDebt", "ProceedsFromIssuanceOfDebt",
                    "ProceedsFromIssuanceOfSeniorLongTermDebt", "ProceedsFromConvertibleDebt"),
    "debt_repaid": ("RepaymentsOfLongTermDebt", "RepaymentsOfDebt"),
    "dps": ("CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"),
}
OUTFLOWS = {"capex", "dividends", "buybacks", "debt_repaid"}
DEBT_TAGS = ("LongTermDebtNoncurrent", "DebtCurrent", "LongTermDebtCurrent", "LongTermDebt",
             "CommercialPaper", "ShortTermBorrowings",
             # A company whose only debt is convertible notes tags nothing
             # above: HIMS's $1.37B (2026-11-19) read as "no debt".
             "ConvertibleDebtNoncurrent", "ConvertibleDebtCurrent", "ConvertibleNotesPayable")


# --------------------------------------------------------------- plumbing

SITES = {"sec.gov": "the SEC", "yahoo.com": "Yahoo", "nasdaq.com": "Nasdaq"}


class FetchError(Exception):
    """A data site refused or failed - said in one line, naming the site."""


def site_of(url):
    return next((name for host, name in SITES.items() if host in url), url.split("/")[2])


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def http_get(url, ua=None, headers=None):
    """Belfort's fetcher - its retries and rate-limit wait - not a second one."""
    return _load("belfort_fetch_for_warren", "belfort-fetch.py").http_get(url, ua=ua, headers=headers)


# Nasdaq's API turns away a request that does not look like its own web page
# asking: the droplet's first `warren.py sources` (2026-11-19) failed both
# Nasdaq lines with Belfort's plain User-Agent. These are the headers
# nasdaq.com's own pages send. No Accept-Encoding: urllib does not unzip.
NASDAQ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def get_json(url, get=None):
    sec = "sec.gov" in url
    ua = sec_ua() if sec and get is None else None
    headers = NASDAQ_HEADERS if "nasdaq.com" in url else None
    try:
        raw = (get or http_get)(url, ua=ua, headers=headers)
    except urllib.error.HTTPError as e:
        why = {403: "refused the request", 404: "has no such page", 429: "said too many requests"}
        raise FetchError(f"{site_of(url)} {why.get(e.code, 'answered an error')} (HTTP {e.code})") from e
    except OSError as e:
        raise FetchError(f"could not reach {site_of(url)} ({e})") from e
    if sec:
        time.sleep(0.15)   # SEC asks for under 10 requests a second
    return json.loads(raw)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def r1(x):
    return None if x is None else round(float(x), 1)


def r2(x):
    return None if x is None else round(float(x), 2)


def pct(a, b):
    return None if a is None or not b else (a / b - 1) * 100


def money(x):
    if x is None:
        return "n/a"
    a = abs(x)
    s = f"${a / 1e9:.2f}B" if a >= 1e9 else f"${a / 1e6:.0f}M" if a >= 1e6 else f"${a:,.0f}"
    return "-" + s if x < 0 else s


def ddate(s):
    return date.fromisoformat(s[:10])


# --------------------------------------------------------------- SEC

def cik_for(symbol, tickers):
    sym = symbol.upper()
    for row in (tickers or {}).values():
        if str(row.get("ticker", "")).upper() == sym:
            return int(row["cik_str"]), row.get("title") or sym
    return None, None


def sec_tickers(get=None, now=None):
    path = CACHE / "sec-tickers.json"
    d = read_json(path)
    if d and time.time() - path.stat().st_mtime < 7 * 86400 and get is None:
        return d
    d = get_json(TICKERS_URL, get)
    write_json(path, d)
    return d


def sec_facts(cik, get=None):
    path = CACHE / f"facts-{cik}.json"
    d = read_json(path)
    if d and time.time() - path.stat().st_mtime < 6 * 3600 and get is None:
        return d
    d = get_json(FACTS_URL.format(cik=cik), get)
    write_json(path, d)
    return d


def _rows(facts, tag):
    for tax in ("us-gaap", "dei"):
        node = ((facts or {}).get("facts") or {}).get(tax, {}).get(tag)
        if node:
            units = node.get("units") or {}
            for u in ("USD", "USD/shares", "shares"):
                if u in units:
                    return units[u]
            return next(iter(units.values()), [])
    return []


def _days(r):
    return (ddate(r["end"]) - ddate(r["start"])).days if r.get("start") else None


def annual(facts, tag):
    """[(end, value)] oldest first: each fiscal year once, from 10-Ks, a
    year long (a 10-K also carries quarter rows), the latest filing winning."""
    best = {}
    for r in _rows(facts, tag):
        if not str(r.get("form", "")).startswith("10-K") or r.get("fp") != "FY":
            continue
        n = _days(r)
        if n is None or not 350 <= n <= 380:
            continue
        if r["end"] not in best or r.get("filed", "") > best[r["end"]].get("filed", ""):
            best[r["end"]] = r
    return [(e, best[e]["val"]) for e in sorted(best)]


def ytd(facts, tag):
    """[(start, end, value)]: year-to-date rows from 10-Qs."""
    best = {}
    for r in _rows(facts, tag):
        if not str(r.get("form", "")).startswith("10-Q"):
            continue
        n = _days(r)
        if n is None or not 80 <= n <= 290:
            continue
        k = (r["start"], r["end"])
        if k not in best or r.get("filed", "") > best[k].get("filed", ""):
            best[k] = r
    return [(s, e, best[(s, e)]["val"]) for s, e in sorted(best, key=lambda k: k[1])]


def instants(facts, tag):
    """[(end, value)] oldest first for a balance-sheet figure."""
    best = {}
    for r in _rows(facts, tag):
        if r.get("start") or not r.get("end"):
            continue
        if r["end"] not in best or r.get("filed", "") > best[r["end"]].get("filed", ""):
            best[r["end"]] = r
    return [(e, best[e]["val"]) for e in sorted(best)]


def pick_tag(facts, key):
    best, best_end = None, ""
    for tag in TAGS[key]:
        a = annual(facts, tag)
        if key == "interest" and tag == "InterestIncomeExpenseNonoperatingNet":
            a = [(e, v) for e, v in a if v < 0]     # net interest INCOME is not an expense
        if a and a[-1][0] > best_end:
            best, best_end = tag, a[-1][0]
    return best


def _clean(key, v):
    return abs(v) if (key in OUTFLOWS or key == "interest") and v is not None else v


def series(facts, key):
    tag = pick_tag(facts, key)
    if not tag:
        return [], None
    a = annual(facts, tag)
    if key == "interest" and tag == "InterestIncomeExpenseNonoperatingNet":
        a = [(e, v) for e, v in a if v < 0]
    return [(e, _clean(key, v)) for e, v in a], tag


def ttm(facts, key):
    """(value, as_of_end): last fiscal year + this year to date - the same
    stretch a year before. A company with no quarter since its 10-K: the year."""
    tag = pick_tag(facts, key)
    if not tag:
        return None, None
    a = annual(facts, tag)
    if not a:
        return None, None
    fy_end, fy = a[-1]
    rows = [r for r in ytd(facts, tag) if ddate(r[0]) > ddate(fy_end) and (ddate(r[0]) - ddate(fy_end)).days <= 10]
    if not rows:
        return _clean(key, fy), fy_end
    s, e, cur = rows[-1]
    n = (ddate(e) - ddate(s)).days
    prior = [v for s2, e2, v in ytd(facts, tag)
             if abs((ddate(e) - ddate(e2)).days - 365) <= 10 and abs((ddate(e2) - ddate(s2)).days - n) <= 10]
    if not prior:
        return _clean(key, fy), fy_end
    if key in OUTFLOWS or key == "interest":
        return abs(fy) + abs(cur) - abs(prior[-1]), e
    return fy + cur - prior[-1], e


def debt_at(facts):
    """{end: total debt} - current plus long-term, without counting a
    current portion twice when a company reports both totals."""
    by = {t: dict(instants(facts, t)) for t in DEBT_TAGS}
    out = {}
    for end in set().union(*[set(v) for v in by.values()]):
        g = lambda t: by[t].get(end)
        if g("LongTermDebtNoncurrent") is not None:
            cur = g("DebtCurrent")
            if cur is None:
                cur = (g("LongTermDebtCurrent") or 0) + (g("CommercialPaper") or 0) + (g("ShortTermBorrowings") or 0)
            out[end] = g("LongTermDebtNoncurrent") + cur
        elif g("LongTermDebt") is not None:
            out[end] = g("LongTermDebt") + (g("CommercialPaper") or 0) + (g("ShortTermBorrowings") or 0)
        elif g("ConvertibleDebtNoncurrent") is not None or g("ConvertibleDebtCurrent") is not None:
            out[end] = (g("ConvertibleDebtNoncurrent") or 0) + (g("ConvertibleDebtCurrent") or 0) + (g("ShortTermBorrowings") or 0)
        elif g("ConvertibleNotesPayable") is not None:
            out[end] = g("ConvertibleNotesPayable") + (g("ShortTermBorrowings") or 0)
    return dict(sorted(out.items()))


def latest_filed(facts):
    best = ""
    for tax in (facts or {}).get("facts", {}).values():
        for node in tax.values():
            for rows in (node.get("units") or {}).values():
                for r in rows:
                    if str(r.get("form", "")).startswith(("10-K", "10-Q")) and r.get("filed", "") > best:
                        best = r["filed"]
    return best or None


# --------------------------------------------------------------- price, Nasdaq

def price_facts(payload, now=None):
    """Price, the 52-week range, the 200-day average, the month's and year's
    move, and dividends paid in the last 365 days, from one Yahoo chart."""
    res = payload["chart"]["result"][0]
    meta = res.get("meta") or {}
    q = (res.get("indicators") or {}).get("quote", [{}])[0]
    stamps = res.get("timestamp") or []
    closes = [(t, float(c)) for t, c in zip(stamps, q.get("close") or []) if c is not None]
    if not closes:
        raise ValueError("no closes in the chart")
    price = float(meta.get("regularMarketPrice") or closes[-1][1])
    last_t = stamps[-1] if stamps else int(time.time())
    vals = [c for _, c in closes]
    month_ago = [c for t, c in closes if t <= last_t - 30 * 86400]
    divs = ((res.get("events") or {}).get("dividends") or {}).values()
    paid = sum(float(d["amount"]) for d in divs if last_t - int(d["date"]) <= 365 * 86400)
    return {
        "name": meta.get("longName") or meta.get("shortName"),
        "price": r2(price),
        "high_52w": r2(meta.get("fiftyTwoWeekHigh") or max(vals)),
        "low_52w": r2(meta.get("fiftyTwoWeekLow") or min(vals)),
        "sma200": r2(sum(vals[-200:]) / 200) if len(vals) >= 200 else None,
        "change_1m": r1(pct(price, month_ago[-1])) if month_ago else None,
        "change_1y": r1(pct(price, vals[0])) if len(vals) > 1 else None,
        "div_per_share_ttm": r2(paid) if paid else 0.0,
        "as_of": datetime.fromtimestamp(last_t, timezone.utc).strftime("%Y-%m-%d"),
    }


def _num(s):
    s = str(s or "").replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def summary_facts(payload):
    d = ((payload or {}).get("data") or {}).get("summaryData") or {}
    v = lambda k: (d.get(k) or {}).get("value")
    sector = v("Sector")
    return {"nasdaq_sector": sector if sector and sector != "N/A" else None,
            "nasdaq_industry": v("Industry") if v("Industry") not in (None, "N/A") else None,
            "market_cap": _num(v("MarketCap"))}


def forecast_facts(payload):
    """Next twelve months' EPS (the next four quarters' consensus) and the
    growth a PEG divides by: this fiscal year to two years on, a year."""
    d = (payload or {}).get("data") or {}
    qs = ((d.get("quarterlyForecast") or {}).get("rows") or [])[:4]
    ys = [r for r in ((d.get("yearlyForecast") or {}).get("rows") or []) if (r.get("noOfEstimates") or 0) >= 3]
    ntm = round(sum(float(r["consensusEPSForecast"]) for r in qs), 2) if len(qs) == 4 and all(
        r.get("consensusEPSForecast") is not None for r in qs) else None
    growth = None
    if len(ys) >= 3:
        a, b = float(ys[0]["consensusEPSForecast"]), float(ys[2]["consensusEPSForecast"])
        if a > 0 and b > 0:
            growth = ((b / a) ** 0.5 - 1) * 100
    return {"eps_ntm": ntm, "eps_growth": r1(growth),
            "eps_years": [[r["fiscalEnd"], r["consensusEPSForecast"]] for r in ys[:3]]}


# --------------------------------------------------------------- the numbers

def numbers(symbol, get=None, tickers=None):
    """Every figure the checklist needs for one stock, computed here."""
    sym = symbol.upper()
    tickers = tickers if tickers is not None else sec_tickers(get)
    cik, title = cik_for(sym, tickers)
    if not cik:
        raise LookupError(f"{sym} is not in the SEC's list of US-listed companies")
    facts = sec_facts(cik, get)
    pf = price_facts(get_json(CHART_URL.format(sym=sym), get))
    try:
        sm = summary_facts(get_json(SUMMARY_URL.format(sym=sym), get))
    except Exception:
        sm = {"nasdaq_sector": None, "nasdaq_industry": None, "market_cap": None}
    try:
        fc = forecast_facts(get_json(FORECAST_URL.format(sym=sym), get))
    except Exception:
        fc = {"eps_ntm": None, "eps_growth": None, "eps_years": []}

    n = {"symbol": sym, "cik": cik, "company": pf["name"] or title, **pf, **sm, **fc,
         "data_filed": latest_filed(facts)}
    for key in TAGS:
        s, tag = series(facts, key)
        n[key + "_years"] = [[e[:10], v] for e, v in s[-4:]]
        n[key + "_tag"] = tag
        n[key + "_ttm"], n[key + "_ttm_end"] = ttm(facts, key) if key not in ("eps", "dps") else (None, None)

    debt = debt_at(facts)
    cash = dict(instants(facts, "CashAndCashEquivalentsAtCarryingValue"))
    if debt:
        end = max(debt)
        n["debt"], n["debt_as_of"] = debt[end], end
        year_ago = [e for e in debt if abs((ddate(end) - ddate(e)).days - 365) <= 20]
        n["debt_year_ago"] = debt[year_ago[-1]] if year_ago else None
        n["cash"] = cash.get(end, cash[max(cash)] if cash else 0)
    else:
        n["debt"] = n["debt_year_ago"] = None
        n["cash"] = cash[max(cash)] if cash else None
    if not n.get("market_cap"):
        sh = instants(facts, "EntityCommonStockSharesOutstanding")
        n["market_cap"] = sh[-1][1] * n["price"] if sh and n["price"] else None
    return derive(n)


def derive(n):
    """The checklist's arithmetic on the raw figures. Pure: tests feed it."""
    g = n.get
    rev = g("revenue_ttm")
    ni, ocf, capex = g("net_income_ttm"), g("ocf_ttm"), g("capex_ttm")
    n["fcf_ttm"] = ocf - capex if ocf is not None and capex is not None else None
    years = {e: v for e, v in g("ocf_years") or []}
    cap = {e: v for e, v in g("capex_years") or []}
    n["fcf_years"] = [[e, years[e] - cap[e]] for e in years if e in cap]
    n["margin_years"] = [[e, r1(100 * v / r)] for (e, v), (e2, r) in zip(g("net_income_years") or [], g("revenue_years") or [])
                         if e == e2 and r]
    n["margin_ttm"] = r1(100 * ni / rev) if ni is not None and rev else None
    div = g("dividends_ttm") or 0
    n["coverage"] = r2(n["fcf_ttm"] / div) if div and n["fcf_ttm"] is not None else None
    n["payout"] = r1(100 * div / ni) if div and ni and ni > 0 else None
    n["yield"] = r2(100 * g("div_per_share_ttm") / g("price")) if g("price") and g("div_per_share_ttm") else 0.0
    n["fcf_yield"] = r1(100 * n["fcf_ttm"] / g("market_cap")) if g("market_cap") and n["fcf_ttm"] is not None else None
    op, dna = g("op_income_ttm"), g("dna_ttm")
    n["ebitda_ttm"] = op + dna if op is not None and dna is not None else None
    n["net_debt"] = g("debt") - (g("cash") or 0) if g("debt") is not None else None
    n["leverage"] = (r1(n["net_debt"] / n["ebitda_ttm"]) if n["net_debt"] is not None and n["ebitda_ttm"]
                     and n["ebitda_ttm"] > 0 else None)
    it = g("interest_ttm")
    n["interest_cover"] = r1(op / it) if op is not None and it else None
    n["ev"] = g("market_cap") + n["net_debt"] if g("market_cap") and n["net_debt"] is not None else g("market_cap")
    n["ev_ebitda"] = r1(n["ev"] / n["ebitda_ttm"]) if n["ev"] and n["ebitda_ttm"] and n["ebitda_ttm"] > 0 else None
    rev_ok = rev is not None and rev > 0
    n["ps"] = r2(g("market_cap") / rev) if g("market_cap") and rev_ok else None
    eps_ntm = g("eps_ntm")
    # Losing money now, or expected to over the next year: P/E means nothing,
    # and Step 5 prices it on sales.
    n["unprofitable"] = (ni is not None and ni <= 0) or (eps_ntm is not None and eps_ntm <= 0)
    n["pe_forward"] = r1(g("price") / eps_ntm) if eps_ntm and eps_ntm > 0 and g("price") else None
    gr = g("eps_growth")
    n["peg"] = r2(n["pe_forward"] / gr) if n["pe_forward"] and gr and gr > 0 else None
    # Part B of Step 4: the latest full year's cash, line by line.
    last = lambda k: (g(k + "_years") or [[None, None]])[-1][1]
    n["fy"] = {"year_end": (g("ocf_years") or [[None]])[-1][0], "ocf": last("ocf"), "capex": last("capex"),
               "dividends": last("dividends") or 0, "buybacks": last("buybacks") or 0,
               "net_borrowing": (last("debt_issued") or 0) - (last("debt_repaid") or 0)}
    return n


# --------------------------------------------------------------- the ratings

def row(check, answer, rating, why=""):
    return {"check": check, "answer": answer, "rating": rating, "why": why}


def overall(rows):
    """A step's rating: Red if any row is Red, Yellow if half or more are
    Yellow, Green otherwise - so one soft row does not sink a step."""
    rs = [r["rating"] for r in rows if r["rating"] in (GREEN, YELLOW, RED)]
    if RED in rs:
        return RED
    if rs and rs.count(YELLOW) * 2 >= len(rs):
        return YELLOW
    return GREEN if rs else YELLOW


def trend(values):
    """'up' in most years, 'down' two or more in a row at the end, else 'flat'."""
    v = [x for x in values if x is not None]
    if len(v) < 2:
        return None
    steps = [b > a for a, b in zip(v, v[1:])]
    if len(steps) >= 2 and not steps[-1] and not steps[-2]:
        return "down"
    return "up" if sum(steps) * 2 > len(steps) else "flat"


def yrs(pairs, f=money):
    return " -> ".join(f"{f(v)} ({e[:4]})" for e, v in pairs)


def rate_business(ai):
    if not ai:
        return {"rating": YELLOW, "rows": [row("How does it make money?", "Warren's write-up was not available - answer this yourself", YELLOW)]}
    rows = [row("How does it make money?", ai["business"], GREEN),
            row("What does it sell, and who buys it?", ai["customers"], GREEN),
            row("Sector", ai["sector"], GREEN),
            row("Cyclical or steady?", ("Cyclical: " if ai["cyclical"] else "Mostly steady: ") + ai["cyclical_why"][:1].lower() + ai["cyclical_why"][1:], GREEN),
            row("2 main competitors", ", ".join(ai["peers"]) or "none found",
                GREEN if len(ai["peers"]) == 2 else YELLOW,
                "" if len(ai["peers"]) == 2 else "Warren could not confirm two listed rivals")]
    return {"rating": overall(rows), "rows": rows}


def rate_growth(n, ai):
    """The last three fiscal years (and the last twelve months), as the
    checklist asks - not every year the filings happen to carry. Every row
    that is not Green says why, in words (owner, 2026-11-19)."""
    ry, ey, my = (n.get(k) or [] for k in ("revenue_years", "eps_years", "margin_years"))
    ry, ey, my = ry[-3:], ey[-3:], my[-3:]
    rev = [v for _, v in ry] + ([n["revenue_ttm"]] if n.get("revenue_ttm") else [])
    eps = [v for _, v in ey]
    mar = [v for _, v in my] + ([n["margin_ttm"]] if n.get("margin_ttm") is not None else [])
    def r(t):
        return {"up": GREEN, "down": RED}.get(t, YELLOW)
    def why(t, what):
        return {None: f"not enough years of {what} reported to see a trend",
                "down": f"{what} fell two periods in a row",
                "flat": f"{what} went up and down - no clear trend"}.get(t, "")
    rev_t, eps_t, mar_t = trend(rev), trend(eps), trend(mar)
    eps_why = why(eps_t, "earnings per share")
    if eps and eps[-1] is not None and eps[-1] < 0:
        eps_t, eps_why = "down", "the latest year lost money"
    mar_why = why(mar_t, "profit margin")
    if mar and mar[-1] is not None and mar[-1] < 0:
        mar_t = "down"
        mar_why = "it lost money over the last 12 months" + (" - " + mar_why if mar_why and trend(mar) == "down" else "")
    rows = [row("Revenue, last 3 years", yrs(ry) + (f"; {money(n['revenue_ttm'])} last 12 months" if n.get("revenue_ttm") else ""),
                r(rev_t), why(rev_t, "revenue")),
            row("EPS, last 3 years", yrs(ey, lambda v: f"${v:.2f}"), r(eps_t), eps_why),
            row("Profit margin", yrs(my, lambda v: f"{v}%") + (f"; {n['margin_ttm']}% last 12 months" if n.get("margin_ttm") is not None else ""),
                r(mar_t), mar_why)]
    if ai:
        rows.append(row("Did profits jump mainly because of high commodity prices?", ai["commodity_why"],
                        {"no": GREEN, "partly": YELLOW, "yes": RED}[ai["commodity_boost"]],
                        {"no": "", "partly": "some of the profit rode on high commodity prices, which can fall back",
                         "yes": "the profit mostly rode on high commodity prices - when they fall, so will it"}[ai["commodity_boost"]]))
    return {"rating": overall(rows), "rows": rows}


def rate_cash(n):
    rows = []
    fcf, ocf = n.get("fcf_ttm"), n.get("ocf_ttm")
    if fcf is None:
        rows.append(row("Free cash flow (last 12 months)", "not reported", YELLOW, "its filings did not give the cash figures"))
    elif fcf >= 0:
        rows.append(row("Free cash flow (last 12 months)", money(fcf), GREEN))
    else:
        weak = ocf is None or ocf <= 0 or (n.get("dividends_ttm") or 0) > ocf
        rows.append(row("Free cash flow (last 12 months)", money(fcf), RED if weak else YELLOW,
                        "it spent more cash than it brought in, and its day-to-day business is not making enough"
                        if weak else "negative from heavy building - Step 4 says whether that is healthy"))
    paid = n.get("dividends_ttm") or 0
    if paid:
        cov = n.get("coverage")
        rows.append(row("Dividend coverage (FCF / dividends paid)", f"{cov}x" if cov is not None else "n/a",
                        YELLOW if cov is None else GREEN if cov >= 1.5 else RED if cov < 1.0 else YELLOW,
                        "could not be worked out" if cov is None else "" if cov >= 1.5 else
                        "free cash flow does not cover the dividend - the gap comes from savings or loans" if cov < 1.0 else
                        "covered, but with little room to spare (1.5x or more is comfortable)"))
        po = n.get("payout")
        heavy = n.get("_capital_heavy")
        po_rating = YELLOW if po is None else GREEN if po < 60 else (YELLOW if heavy else RED) if po > 90 else YELLOW
        po_why = ("normal for pipelines, utilities and REITs - trust coverage" if heavy and po and po >= 60 else
                  "no profit to pay it from" if po is None else "" if po < 60 else
                  "it pays out over 90% of its profit - little is left if profits dip" if po > 90 else
                  "it pays out 60-90% of its profit - fine if profits are steady, tight if not")
        rows.append(row("Payout ratio", f"{po}% of earnings" if po is not None else "n/a", po_rating, po_why))
        y = n.get("yield") or 0
        rows.append(row("Dividend yield", f"{y}%", RED if y > 8 else GREEN if 2 <= y <= 5 else YELLOW,
                        "a yield above 8% often means the market expects a cut" if y > 8 else "" if 2 <= y <= 5 else
                        "under 2% - a small income" if y < 2 else "high - make sure the coverage above holds"))
        dps = [v for _, v in n.get("dps_years") or []]
        if len(dps) >= 2:
            cut = any(b < a - 1e-9 for a, b in zip(dps, dps[1:]))
            rows.append(row("Dividend history", yrs(n.get("dps_years") or [], lambda v: f"${v:.2f}"), RED if cut else GREEN,
                            "cut at least once" if cut else "held or raised every year shown"))
    else:
        rows.append(row("Dividend", "pays no dividend", YELLOW, "nothing to cover - judge it on growth"))
    fy = n.get("fcf_yield")
    rows.append(row("FCF yield (FCF / market cap)", f"{fy}%" if fy is not None else "n/a",
                    YELLOW if fy is None else GREEN if fy >= 5 else RED if fy < 2 else YELLOW,
                    "could not be worked out" if fy is None else "" if fy >= 5 else
                    f"for every $100 of the company you buy, it makes about ${max(fy, 0):.2f} of free cash a year - "
                    "the price is high next to the cash it makes (5% or more is good)" if fy < 2 else
                    "a fair amount of cash for the price, not a bargain (5% or more is good)"))
    return {"rating": overall(rows), "rows": rows}


def debt_purpose(n):
    """Part B of Step 4: what the debt is paying for, from the year's cash."""
    fy = n.get("fy") or {}
    ocf, capex, div = fy.get("ocf"), fy.get("capex") or 0, fy.get("dividends") or 0
    borrow, buy = fy.get("net_borrowing") or 0, fy.get("buybacks") or 0
    if ocf is None:
        return "unknown", YELLOW, "the cash flow statement was not reported"
    if div and ocf < div:
        return "dividend on a loan", RED, "operating cash is less than the dividends paid"
    if borrow <= 0 and ocf >= div + capex:
        return "own cash", GREEN, "operating cash covered the dividend and the spending, and debt did not grow"
    if borrow > 0 and buy > 0.5 * ocf:
        return "borrowing for buybacks", YELLOW, "debt rose while it spent heavily buying back stock"
    if borrow > 0 and ocf >= div + capex:
        return "borrowing to grow", GREEN, (
            f"its own cash already covered {'the dividend and ' if div else ''}its spending, so the new borrowing "
            f"({money(borrow)}) went to growth, deals or savings - Warren's note on its debt says which")
    if borrow > 0:
        return "borrowing to grow", GREEN, (("operating cash covered the dividend; " if div else "")
                                            + "new borrowing paid for the building its own cash could not")
    return "own cash", GREEN, "operating cash and its own savings paid for everything"


def rate_debt(n):
    heavy = n.get("_capital_heavy")
    rows = []
    lev, nd, eb = n.get("leverage"), n.get("net_debt"), n.get("ebitda_ttm")
    if nd is not None and nd <= 0:
        rows.append(row("Leverage (net debt / EBITDA)", "more cash than debt", GREEN))
    elif nd is not None and eb is not None and eb <= 0:
        rows.append(row("Leverage (net debt / EBITDA)", f"{money(nd)} net debt, EBITDA {money(eb)}", RED,
                        "it owes more than it holds in cash, and its earnings (EBITDA) are negative - nothing is paying the debt down"))
    elif lev is None:
        rows.append(row("Leverage (net debt / EBITDA)", "n/a", YELLOW,
                        "no debt was found in its filings" if n.get("debt") is None else "its earnings were not reported"))
    else:
        g, r = (4.5, 5.5) if heavy else (2.0, 3.5)
        rows.append(row("Leverage (net debt / EBITDA)", f"{lev}x", GREEN if lev < g else RED if lev > r else YELLOW,
                        ("normal for its industry is 3.5-5.5x; " if heavy else "") +
                        ("" if lev < g else f"it would take about {lev:.0f} years of earnings to repay its debt"
                         f" (under {g:g}x is comfortable, over {r:g}x is risky)")))
    ic, op = n.get("interest_cover"), n.get("op_income_ttm")
    rows.append(row("Interest coverage", f"{ic}x" if ic is not None else "n/a",
                    YELLOW if ic is None else GREEN if ic > 8 else RED if ic < 3 else YELLOW,
                    "no interest expense was reported" if ic is None else "" if ic > 8 else
                    "its operating profit is negative, so earnings are not covering the interest" if op is not None and op < 0 else
                    "operating profit covers its interest bill less than 3 times" if ic < 3 else
                    "operating profit covers its interest 3-8 times - fine, not roomy (over 8x is comfortable)"))
    d0, d1 = n.get("debt"), n.get("debt_year_ago")
    if d0 is not None and d1:
        ch = pct(d0, d1)
        rows.append(row("Total debt vs. a year ago", f"{money(d0)} vs. {money(d1)}", GREEN if ch <= 5 else YELLOW if ch <= 25 else RED,
                        "" if ch <= 5 else f"up {ch:.0f}% in a year" + (" - Part B says why" if ch > 25 else "")))
    what, rating, why = debt_purpose(n)
    fy = n.get("fy") or {}
    part_b = [["Operating cash flow", money(fy.get("ocf"))], ["Capital expenditure", money(fy.get("capex"))],
              ["Dividends paid", money(fy.get("dividends"))], ["Stock buybacks", money(fy.get("buybacks"))],
              ["Net new borrowing", money(fy.get("net_borrowing"))]]
    quick = fy.get("ocf") - fy.get("dividends") if fy.get("ocf") is not None else None
    rows.append(row("What is the debt paying for?", what, rating, why))
    return {"rating": overall(rows), "rows": rows, "part_b": part_b, "year_end": fy.get("year_end"),
            "note": "Leverage here is operating income plus depreciation from the SEC filings; a company's own "
                    "'adjusted EBITDA' usually adds more and shows a lower figure.",
            "quick_test": money(quick), "quick_test_ok": quick is not None and quick >= 0,
            "pays_dividend": bool(fy.get("dividends"))}


def peer_avg(peers, key):
    v = [p[key] for p in peers if p.get(key) is not None]
    return r1(sum(v) / len(v)) if v else None


def rate_price(n, peers, sector, cyclical):
    """A company losing money has no P/E worth the name, so it is priced on
    sales instead - P/S against its rivals' P/S - and PEG, built on P/E, does
    not apply (owner, 2026-11-19, after HIMS)."""
    rows = []
    loss = n.get("unprofitable")
    pe, ev, peg = n.get("pe_forward"), n.get("ev_ebitda"), n.get("peg")
    def vs(mine, avg, label, typical=None):
        if mine is None:
            return row(label, "n/a", YELLOW, "could not be worked out from the data")
        if avg is None:
            if typical:
                r = GREEN if mine <= typical[1] else YELLOW if mine <= typical[1] * 1.25 else RED
                return row(label, f"{mine} (sector normal {typical[0]}-{typical[1]})", r,
                           "" if r == GREEN else f"above the {typical[0]}-{typical[1]} that is normal for its sector")
            return row(label, f"{mine} (no peers to compare)", YELLOW, "no rival's figure to compare it with")
        if mine <= avg or (not loss and peg is not None and peg < 1.5):
            r, why = GREEN, ""
        elif mine <= avg * 1.25:
            r, why = YELLOW, f"up to a quarter dearer than its rivals' average of {avg}"
        else:
            r, why = RED, f"{pct(mine, avg):.0f}% dearer than its rivals' average of {avg}"
        return row(label, f"{mine} vs. peer average {avg}", r, why)
    if loss:
        ps = vs(n.get("ps"), peer_avg(peers, "ps"), "Price/Sales (P/S)")
        ps["why"] = ("it is not making a profit, so it is priced on sales instead of earnings"
                     + ("; " + ps["why"] if ps["why"] else ""))
        rows.append(ps)
    else:
        rows.append(vs(pe, peer_avg(peers, "pe_forward"), "Forward P/E", PE_TYPICAL.get(sector)))
    if n.get("ebitda_ttm") is not None and n["ebitda_ttm"] <= 0:
        rows.append(row("EV/EBITDA", "doesn't apply", "n/a", "its EBITDA is negative, so there is nothing to divide by"))
    else:
        rows.append(vs(ev, peer_avg(peers, "ev_ebitda"), "EV/EBITDA"))
    if loss:
        rows.append(row("PEG", "doesn't apply", "n/a", "PEG is built on P/E, which needs a profit"))
    else:
        rows.append(row("PEG", f"{peg}" if peg is not None else "earnings not expected to grow" if n.get("eps_ntm") else "n/a",
                        GREEN if peg is not None and peg < 1.5 else RED if peg is not None and peg > 2.5 else YELLOW,
                        "" if peg is not None and peg < 1.5 else
                        "analysts expect earnings to shrink, so there is no growth to pay for" if peg is None and n.get("eps_ntm") else
                        "no analyst growth forecast" if peg is None else
                        "you would pay a lot for its expected growth (under 1.5 is good)" if peg > 2.5 else
                        "a fair price for its growth, not cheap (under 1.5 is good)"))
    eps = [v for _, v in n.get("eps_years") or []]
    if cyclical and pe is not None and pe < 10 and eps and eps[-1] == max(eps):
        rows.append(row("Cyclical trap?", "a very low P/E at record profits", RED,
                        "for a cyclical company that 'cheap' price can be a trap"))
    chart = [["Where in its 52-week range?", f"${n['low_52w']}-${n['high_52w']}, now ${n['price']} ({where(n)})"],
             ["Above or below its 200-day average?", "n/a" if not n.get("sma200") else
              f"{'above' if n['price'] >= n['sma200'] else 'below'} (${n['sma200']})"],
             ["Jumped 20%+ in the last month?", "n/a" if n.get("change_1m") is None else
              f"{'yes' if n['change_1m'] >= 20 else 'no'} ({n['change_1m']:+}%)"]]
    chase = n.get("change_1m") is not None and n["change_1m"] >= 20
    return {"rating": overall(rows), "rows": rows, "chart": chart,
            "chart_note": "It just jumped 20%+ - don't chase it; buy in steps." if chase else "No sign of a buying frenzy.",
            "table": comparison(n, peers)}


def where(n):
    lo, hi, p = n.get("low_52w"), n.get("high_52w"), n.get("price")
    if not (lo and hi and p) or hi <= lo:
        return "n/a"
    f = (p - lo) / (hi - lo)
    return "near the low" if f < 0.25 else "near the high" if f > 0.75 else "middle"


def comparison(n, peers):
    cols = [n] + list(peers)
    keys = [("Forward P/E", "pe_forward", ""), ("PEG", "peg", ""), ("P/S", "ps", ""), ("EV/EBITDA", "ev_ebitda", ""),
            ("Dividend yield", "yield", "%"), ("Leverage", "leverage", "x"), ("Profit margin", "margin_ttm", "%"),
            ("1-year price change", "change_1y", "%")]
    return {"columns": [c["symbol"] for c in cols],
            "rows": [[label] + [("" if c.get(k) is None else f"{c[k]:+}{unit}" if k == "change_1y" else f"{c[k]}{unit}")
                                for c in cols] for label, k, unit in keys]}


def position(holding, price, amount=0):
    """What you already own of this stock, from the holdings list: your
    average price paid is your breakeven (before any trading fees), and
    buying `amount` more at `price` moves it. Code's arithmetic, not a
    model's (owner, 2026-11-19)."""
    if not holding:
        return None
    paid = float(holding.get("amount") or 0)
    avg = holding.get("avg_price")
    out = {"amount": r2(paid), "avg_price": avg, "breakeven": avg}
    if not avg or avg <= 0:
        return out
    shares = paid / avg
    out["shares"] = round(shares, 4)
    if price:
        out["price"] = price
        out["value"] = r2(shares * price)
        out["gain"] = r2(shares * price - paid)
        out["gain_pct"] = r1(100 * (price / avg - 1))
        if amount:
            more = amount / price
            out["after"] = {"shares": round(shares + more, 4), "paid": r2(paid + amount),
                            "breakeven": r2((paid + amount) / (shares + more))}
    return out


def concentration(holdings, symbol, amount, sector):
    """This stock's and this sector's share of everything after the buy. With
    no other holdings recorded there is nothing to measure against."""
    others = [h for h in (holdings or []) if h.get("symbol") != symbol]
    if not others:
        return None
    mine = amount + sum(float(h.get("amount") or 0) for h in (holdings or []) if h.get("symbol") == symbol)
    total = sum(float(h.get("amount") or 0) for h in others) + mine
    same = sum(float(h.get("amount") or 0) for h in others if h.get("sector") == sector) + mine
    return {"stock_pct": r1(100 * mine / total), "sector_pct": r1(100 * same / total), "total": r2(total)}


def rate_risk(ai, conc, inputs, sector):
    rows = []
    rules = (ai or {}).get("sell_rules") or []
    rows.append(row("3 things that would make me sell", "listed below" if rules else "write your own three",
                    GREEN if len(rules) == 3 else YELLOW, "" if len(rules) == 3 else "Warren could not write three"))
    if conc is None:
        rows.append(row("How much of my money is in this sector?", "add your holdings to check this", YELLOW,
                        "with no holdings listed, Warren cannot tell how concentrated you would be"))
    else:
        rows.append(row("How much of my money is in this stock?", f"{conc['stock_pct']}% of ${conc['total']:,.0f}",
                        GREEN if conc["stock_pct"] <= 30 else YELLOW,
                        "" if conc["stock_pct"] <= 30 else "more than 30% of your money in one stock"))
        rows.append(row("How much is in this sector?", f"{conc['sector_pct']}% in {sector}",
                        RED if conc["sector_pct"] > 50 else GREEN,
                        "more than half your money would be in one sector - one bad year for it hits everything" if conc["sector_pct"] > 50 else ""))
    if inputs.get("hold_drop") == "no":
        rows.append(row("Could I hold through a 30% drop?", "no - invest less or pick a steadier stock", YELLOW,
                        "you said a 30% drop would make you sell - stocks like this one can drop that much"))
    return {"rating": overall(rows), "rows": rows, "risks": (ai or {}).get("risks") or [],
            "buy_more": (ai or {}).get("buy_more"), "debt_note": (ai or {}).get("debt_note")}


STEP_NAMES = {"business": "Step 1 (the business)", "growth": "Step 2 (growth)", "cash": "Step 3 (cash)",
              "debt": "Step 4 (debt)", "price": "Step 5 (price)", "risk": "Step 6 (risk)"}


def red_reasons(steps):
    """Each Red step, with the row that turned it Red and why - the decision
    names its reasons instead of quoting the rule (owner, 2026-11-19)."""
    out = []
    for k, name in STEP_NAMES.items():
        st = steps.get(k) or {}
        if st.get("rating") != RED:
            continue
        bad = [r for r in st.get("rows") or [] if r.get("rating") == RED]
        out += [f"{name} - {r['check']}: {r['why'] or r['answer']}" for r in bad] or [name]
    return out


def decide(steps):
    """The checklist's decision rule, word for word, then the reasons."""
    r = {k: v["rating"] for k, v in steps.items()}
    if r["cash"] == RED or r["debt"] == RED:
        return "Skip", "Step 3 or Step 4 is Red - weak cash or risky debt is the hardest problem to fix."
    reds = [k for k, v in r.items() if v == RED]
    yellows = [k for k, v in r.items() if v == YELLOW]
    if reds or len(yellows) >= 3:
        why = "a step is Red" if reds else f"{len(yellows)} steps are Yellow (" + ", ".join(STEP_NAMES[k] for k in yellows) + ")"
        if reds == ["risk"]:
            why = "the only Red is your portfolio, not the stock: keep this sector to about half your money or less, then it becomes a Buy"
        return "Wait", f"{why[0].upper() + why[1:]}. Re-check after the next earnings report or a lower price."
    return "Buy", "Everything is Green with at most 2 Yellows. Buy in 2-3 steps a few weeks apart, with limit orders."


# --------------------------------------------------------------- the one question

def facts_for_ai(n):
    lines = [f"Price ${n['price']}; forward P/E {n.get('pe_forward')}; price/sales {n.get('ps')}; profit margin {n.get('margin_ttm')}%",
             f"Revenue by year: {yrs(n.get('revenue_years') or [])}",
             f"Free cash flow, last 12 months: {money(n.get('fcf_ttm'))}; dividends paid {money(n.get('dividends_ttm'))}",
             f"Net debt {money(n.get('net_debt'))}; leverage {n.get('leverage')}x; interest coverage {n.get('interest_cover')}x",
             f"Last full year: borrowed {money((n.get('fy') or {}).get('net_borrowing'))} net, "
             f"capital spending {money((n.get('fy') or {}).get('capex'))}"]
    return "\n".join(lines)


def question(n, inputs):
    return (
        "You are Warren, a careful stock analyst writing for a beginner investor. Fill in the parts of a "
        "before-you-buy checklist that need judgment. Code has already computed every number, so never "
        "write a number, a percentage or a dollar amount yourself.\n\n"
        f"Company: {n['company']} ({n['symbol']}). Nasdaq lists it as sector \"{n.get('nasdaq_sector')}\", "
        f"industry \"{n.get('nasdaq_industry')}\" - that label can be wrong; judge for yourself.\n"
        f"What the code found:\n{facts_for_ai(n)}\n\n"
        f"The buyer is investing for about {inputs['months']} months, goal: {inputs['goal']}. "
        f"Their reason: \"{inputs.get('why') or 'not given'}\".\n\n"
        "You may search the web for the company's latest news and filings first.\n"
        "Reply with ONLY one JSON object and nothing else, with these keys:\n"
        '{"business": "two plain sentences: how it makes money",\n'
        ' "customers": "one sentence: what it sells and who buys it",\n'
        f' "sector": "exactly one of {SECTORS}",\n'
        ' "capital_heavy": true only for pipelines, utilities, REITs or telecom towers, which normally carry more debt,\n'
        ' "cyclical": true or false, "cyclical_why": "one sentence",\n'
        ' "peers": ["TICKER", "TICKER"] - its two closest US-listed public rivals,\n'
        ' "commodity_boost": "yes" or "partly" or "no", "commodity_why": "one sentence: did recent profits jump mainly on high commodity prices?",\n'
        ' "risks": ["three short risks from its latest news or filings"],\n'
        ' "sell_rules": ["three rules, each finishing the sentence I would sell if ..., about the business, not the price"],\n'
        ' "buy_more": "one sentence: what would justify buying more",\n'
        ' "debt_note": "one sentence: what its debt is mostly paying for, if known"}'
    )


PROSE = ("business", "customers", "cyclical_why", "commodity_why", "buy_more", "debt_note")
STRAY = re.compile(r"\$|%|\d+\.\d+|\d{3,}|\d{1,2},\d{3}")


def read_answer(text, symbol, tickers):
    """(answer or None, problems). Code checks Opus before anything is used:
    the shape, the sector, the rivals against the SEC's own list, and that
    no field quotes a number this script did not compute."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None, ["no JSON object in the reply"]
    try:
        a = json.loads(m.group(0))
    except ValueError as e:
        return None, [f"the JSON did not parse: {e}"]
    problems = []
    out = {}
    for k in PROSE:
        v = str(a.get(k) or "").strip()[:400]
        if k in ("business", "customers", "cyclical_why", "commodity_why") and not v:
            problems.append(f"{k} is empty")
        if STRAY.search(v):
            problems.append(f"{k} quotes a number: {v[:80]}")
            v = ""
        out[k] = v
    sector = str(a.get("sector") or "").strip()
    if sector not in SECTORS:
        problems.append(f"sector {sector!r} is not one of the eleven")
    out["sector"] = sector if sector in SECTORS else None
    out["cyclical"] = bool(a.get("cyclical"))
    out["capital_heavy"] = bool(a.get("capital_heavy")) and sector in CAPITAL_HEAVY_OK
    cb = str(a.get("commodity_boost") or "").strip().lower()
    out["commodity_boost"] = cb if cb in ("yes", "partly", "no") else "partly"
    peers = []
    for p in a.get("peers") or []:
        p = str(p).strip().upper()
        if p != symbol and SYMBOL_RE.match(p) and cik_for(p, tickers)[0] and p not in peers:
            peers.append(p)
    if len(peers) < 2:
        problems.append("fewer than two rivals found in the SEC's list")
    out["peers"] = peers[:2]
    for k in ("risks", "sell_rules"):
        items = [str(x).strip()[:200] for x in (a.get(k) or []) if str(x).strip()]
        bad = [x for x in items if STRAY.search(x)]
        if bad:
            problems.append(f"{k} quotes a number: {bad[0][:80]}")
        out[k] = [x for x in items if not STRAY.search(x)][:3]
        if len(out[k]) < 3:
            problems.append(f"{k} has fewer than three")
    ok = out["sector"] and out["business"] and out["customers"]
    return (out if ok else None), problems


def _ask(q):
    w = _load("wake_warren", "wake.py")
    return w.ask("warren", q, ASK_TIMEOUT, tools=("WebSearch",))


def runs_today(now=None, bump=False):
    day = et_time.day(now)
    d = read_json(RUNS, {}) or {}
    n = int(d.get("n") or 0) if d.get("day") == day else 0
    if bump:
        n += 1
        write_json(RUNS, {"day": day, "n": n})
    return n


def ai_part(n, inputs, tickers, ask=None):
    """One question, one retry with the problems named. (answer, note)."""
    if runs_today() >= RUNS_PER_DAY:
        return None, f"today's {RUNS_PER_DAY} checks on Opus are used - the numbers are below, the write-up is not"
    runs_today(bump=True)
    ask = ask or _ask
    q = question(n, inputs)
    ok, text = ask(q)
    if not ok:
        return None, f"Opus could not answer ({text[:150]})"
    ans, problems = read_answer(text, n["symbol"], tickers)
    if problems:
        ok2, text2 = ask(q + "\n\nYour last reply had these problems - fix them:\n- " + "\n- ".join(problems))
        if ok2:
            ans2, problems2 = read_answer(text2, n["symbol"], tickers)
            if ans2 and len(problems2) < len(problems):
                ans, problems = ans2, problems2
    return ans, ("; ".join(problems) if problems else "")


# --------------------------------------------------------------- a check

def holdings():
    return (read_json(HOLDINGS, {}) or {}).get("holdings") or []


def build(symbol, inputs, get=None, ask=None, peer_numbers=None):
    sym = symbol.upper()
    tickers = sec_tickers(get)
    n = numbers(sym, get, tickers)
    ai, ai_note = ai_part(n, inputs, tickers, ask)
    sector = (ai or {}).get("sector") or n.get("nasdaq_sector") or "Unknown"
    n["_capital_heavy"] = bool((ai or {}).get("capital_heavy"))
    peers = []
    for p in (ai or {}).get("peers") or []:
        try:
            peers.append((peer_numbers or numbers)(p, get, tickers))
        except Exception as e:
            ai_note = (ai_note + "; " if ai_note else "") + f"{p}'s numbers could not be read ({str(e)[:80]})"
    steps = {"business": rate_business(ai), "growth": rate_growth(n, ai), "cash": rate_cash(n),
             "debt": rate_debt(n), "price": rate_price(n, peers, sector, (ai or {}).get("cyclical")),
             "risk": rate_risk(ai, concentration(holdings(), sym, float(inputs["amount"]), sector), inputs, sector)}
    decision, why = decide(steps)
    now = datetime.now(timezone.utc)
    return {"symbol": sym, "company": n["company"], "sector": sector, "when_utc": now.isoformat(timespec="seconds"),
            "inputs": inputs, "numbers": {k: v for k, v in n.items() if not k.startswith("_")},
            "peers": [{k: p.get(k) for k in ("symbol", "company", "pe_forward", "peg", "ps", "ev_ebitda", "yield", "leverage",
                                             "margin_ttm", "change_1y")} for p in peers],
            "ai": ai, "ai_note": ai_note, "steps": steps, "decision": decision, "decision_why": why, "decision_reasons": red_reasons(steps),
            "position": position(next((h for h in holdings() if h.get("symbol") == sym), None), n.get("price"),
                                 float(inputs["amount"])),
            "data_filed": n.get("data_filed"),
            "sources": {"sec": FACTS_URL.format(cik=n["cik"]), "price": CHART_URL.format(sym=sym),
                        "nasdaq": SUMMARY_URL.format(sym=sym), "forecast": FORECAST_URL.format(sym=sym)}}


def save(check):
    stamp = check["when_utc"][:16].replace(":", "").replace("T", "-")
    path = CHECKS / check["symbol"] / f"{stamp}.json"
    write_json(path, check)
    return path


def latest(symbol):
    d = CHECKS / symbol.upper()
    files = sorted(d.glob("*.json")) if d.is_dir() else []
    return read_json(files[-1]) if files else None


def ratings(check):
    return {k: v["rating"] for k, v in (check.get("steps") or {}).items()}


def changes(old, new):
    out = []
    a, b = ratings(old or {}), ratings(new)
    for k in b:
        if a.get(k) and a[k] != b[k]:
            out.append(f"{k}: {a[k]} -> {b[k]}")
    if old and old.get("decision") != new.get("decision"):
        out.append(f"decision: {old.get('decision')} -> {new['decision']}")
    return out


# --------------------------------------------------------------- commands

def inputs_from(a):
    if not SYMBOL_RE.match(a.symbol.upper()):
        raise SystemExit("a ticker is letters, like KMI")
    if a.amount <= 0 or a.months <= 0:
        raise SystemExit("amount and months must be more than zero")
    return {"amount": float(a.amount), "months": int(a.months), "goal": a.goal,
            "why": (a.why or "").replace("\n", " ").strip()[:300], "hold_drop": a.hold_drop}


def cmd_check(a):
    check = build(a.symbol, inputs_from(a))
    check["changes"] = changes(latest(a.symbol), check)
    save(check)
    print(json.dumps(check))
    return 0


def cmd_show(a):
    c = latest(a.symbol)
    if not c:
        print(f"no check of {a.symbol.upper()} yet", file=sys.stderr)
        return 1
    print(json.dumps(c))
    return 0


def cmd_deck(a):
    rows = []
    if CHECKS.is_dir():
        for d in sorted(p for p in CHECKS.iterdir() if p.is_dir()):
            c = latest(d.name)
            if c:
                rows.append({"symbol": c["symbol"], "company": c.get("company"), "sector": c.get("sector"),
                             "when_utc": c["when_utc"], "decision": c["decision"], "ratings": ratings(c),
                             "amount": (c.get("inputs") or {}).get("amount"), "changes": c.get("changes") or []})
    rows.sort(key=lambda r: r["when_utc"], reverse=True)
    held = []
    for h in holdings():
        c = latest(h["symbol"]) or {}
        price = (c.get("numbers") or {}).get("price")
        held.append({**h, "position": position(h, price), "price_as_of": (c.get("numbers") or {}).get("as_of")})
    print(json.dumps({"checks": rows, "holdings": held, "runs_today": runs_today(),
                      "runs_per_day": RUNS_PER_DAY, "sectors": SECTORS,
                      "rechecks": (read_json(RECHECKS, []) or [])[-20:]}))
    return 0


def cmd_holdings(a):
    hs = holdings()
    sym = a.symbol.upper()
    if not SYMBOL_RE.match(sym):
        raise SystemExit("a ticker is letters, like KMI")
    old = next((h for h in hs if h["symbol"] == sym), {})
    hs = [h for h in hs if h["symbol"] != sym]
    if a.action == "add":
        sector = a.sector or old.get("sector") or ((latest(sym) or {}).get("sector"))
        if sector not in SECTORS:
            raise SystemExit(f"which sector is {sym}? one of: {', '.join(SECTORS)}")
        h = {"symbol": sym, "amount": round(float(a.amount or 0), 2), "sector": sector}
        avg = a.avg_price if a.avg_price is not None else old.get("avg_price")   # an update keeps the price it had
        if avg is not None:
            if not 0 < avg < 1e6:
                raise SystemExit("the average price paid is a dollar amount per share, like 27.50")
            h["avg_price"] = round(float(avg), 4)
        hs.append(h)
    write_json(HOLDINGS, {"holdings": sorted(hs, key=lambda h: h["symbol"])})
    print(json.dumps({"holdings": sorted(hs, key=lambda h: h["symbol"])}))
    return 0


def new_report(check, get=None):
    """Has the company filed a 10-Q or 10-K since this check's data? One
    small SEC request, not the whole companyfacts file."""
    cik = (check.get("numbers") or {}).get("cik")
    if not cik:
        return False
    d = get_json(CONCEPT_URL.format(cik=int(cik), tag="NetCashProvidedByUsedInOperatingActivities"), get)
    filed = max((r.get("filed", "") for rows in (d.get("units") or {}).values() for r in rows), default="")
    return bool(filed and filed > (check.get("data_filed") or ""))


def cmd_recheck(a):
    log = read_json(RECHECKS, []) or []
    done = 0
    for d in sorted(p for p in CHECKS.iterdir() if p.is_dir()) if CHECKS.is_dir() else []:
        old = latest(d.name)
        if not old:
            continue
        try:
            if not new_report(old):
                continue
            new = build(old["symbol"], old["inputs"])
        except Exception as e:
            print(f"{d.name}: {e}", file=sys.stderr)
            continue
        new["changes"] = changes(old, new)
        new["recheck"] = True
        save(new)
        done += 1
        log.append({"symbol": new["symbol"], "when_utc": new["when_utc"], "changes": new["changes"],
                    "decision": new["decision"]})
        print(f"{new['symbol']}: {', '.join(new['changes']) or 'no rating changed'}")
    write_json(RECHECKS, log[-200:])
    print(f"re-checked {done}")
    return 0


def cmd_sources(a, get=None):
    """Can this machine read each of Warren's sites? One line each - free,
    no model woken. The question a 403 on the page leaves open."""
    sym = a.symbol.upper()
    tries = [("SEC company list", TICKERS_URL), ("Yahoo price chart", CHART_URL.format(sym=sym)),
             ("Nasdaq summary (optional)", SUMMARY_URL.format(sym=sym)),
             ("Nasdaq forecasts (optional)", FORECAST_URL.format(sym=sym))]
    bad = 0
    for i, (what, url) in enumerate(tries):
        try:
            d = get_json(url, get)
            if i == 0:
                cik, _ = cik_for(sym, d)
                if cik:
                    tries.insert(1, ("SEC filings", FACTS_URL.format(cik=cik)))
            print(f"OK    {what}")
        except Exception as e:
            bad += 0 if "optional" in what else 1
            print(f"FAIL  {what}: {e}")
    return 1 if bad else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("symbol")
    c.add_argument("--amount", type=float, required=True)
    c.add_argument("--months", type=int, required=True)
    c.add_argument("--goal", choices=("growth", "dividends", "both"), default="both")
    c.add_argument("--why", default="")
    c.add_argument("--hold-drop", choices=("yes", "no", "unsure"), default="unsure")
    c.set_defaults(fn=cmd_check)
    s = sub.add_parser("show")
    s.add_argument("symbol")
    s.set_defaults(fn=cmd_show)
    sub.add_parser("deck").set_defaults(fn=cmd_deck)
    h = sub.add_parser("holdings")
    h.add_argument("action", choices=("add", "remove"))
    h.add_argument("symbol")
    h.add_argument("amount", nargs="?", type=float)
    h.add_argument("--sector")
    h.add_argument("--avg-price", type=float, help="average price paid per share: your breakeven")
    h.set_defaults(fn=cmd_holdings)
    sub.add_parser("recheck").set_defaults(fn=cmd_recheck)
    so = sub.add_parser("sources")
    so.add_argument("symbol", nargs="?", default="KMI")
    so.set_defaults(fn=cmd_sources)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (FetchError, LookupError) as e:
        print(f"{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
