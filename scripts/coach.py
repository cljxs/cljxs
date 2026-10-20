#!/usr/bin/env python3
"""
coach.py - the trading journal, a daily analysis, and a weekly review.

    coach.py journal [--day YYYY-MM-DD] [--json]   the day's trades, every book
    coach.py daily   [--day YYYY-MM-DD]            journal + analysis, saved (no model)
    coach.py weekly  [--end YYYY-MM-DD] [--dry]    the week's review and proposals
    coach.py show --json                           what the Deck draws

Owner, 2026-10-19: "You are my Trading Performance Coach ... DAILY ROUTINE:
create and maintain a daily trading journal ... Do NOT change any trading
rules or strategies during the daily analysis. Only observe, log, and analyze.
WEEKLY ROUTINE (Every Sunday): ... Based ONLY on this weekly data, propose
specific, actionable changes ... STOP / START / CONTINUE / Updated Rules."

THE JOURNAL IS CODE. Every trade is read from the books the agents keep -
Belfort's portfolio.json (through belfort-trade.py's own closed_trades), and
Bolt's two books - so a journal line is what happened, not what a model
remembers. Every field the owner listed is there: date and time (Eastern),
asset, long or short (all long: neither agent sells short), entry and exit,
size, P&L, the setup, why it was bought, why it was sold, the outcome.

THE DAILY ANALYSIS IS CODE TOO, and it cannot change a rule: it has no way
to. "Mistakes" are the ones a script can see - a loss past the stop, a buy
under the bar, a buy at the top of the day's range (chasing), a sale the
price ran on from (an early exit), a gain given back - each with its number.

THE WEEKLY REVIEW asks the owner's Claude plan ONE question (wake.py ask,
"coach" in tasks/engines.json, Sonnet, no tools): here is the week's
measured data and the current rules - what to STOP, START, CONTINUE, and the
rules proposed for next week, each citing a number from the data. Code
computes every number it is shown. The proposals are proposals: they are
saved for the owner, and nothing here edits a rule. A week with too few
closed trades gets no proposals - a rule changed on three trades is noise.

Times are Eastern (et_time). Free apart from one question a week.
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
sys.path.insert(0, str(SCRIPTS))
import et_time  # noqa: E402

HOME = ROOT / "coach"
JOURNAL = HOME / "journal"
WEEKLY = HOME / "weekly"

FLAT_PCT = 0.25               # within this of zero, a trade is a scratch, not a win or a loss
CHASE_TOP = 0.10              # bought in the top 10% of the 24 hours before: chasing
EARLY_EXIT_PCT = 3.0          # sold, then it rose this much more: an early exit (stops excepted)
GAVE_BACK_PCT = 5.0           # was up this much and closed at or under zero
MIN_WEEK_TRADES = 5           # fewer closed trades than this: observe, propose nothing
DAILY_ONLY = "Observation only. The daily routine never changes a rule."


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bolt():
    return _load("bolt_coach", "bolt.py")


def _belfort():
    return _load("belfort_trade_coach", "belfort-trade.py")


def et_day(utc):
    e = et_time.to_eastern(utc or "")
    return e.strftime("%Y-%m-%d") if e else None


def et_clock(utc):
    e = et_time.to_eastern(utc or "")
    return e.strftime("%Y-%m-%d %H:%M ET") if e else "?"


def hours_between(a, b):
    x, y = et_time.to_eastern(a or ""), et_time.to_eastern(b or "")
    return round((y - x).total_seconds() / 3600, 1) if x and y else None


def outcome(pct):
    return "scratch" if abs(pct) < FLAT_PCT else "win" if pct > 0 else "loss"


# ------------------------------------------------------------------ the books

def belfort_rows():
    """Belfort's closed trades, as journal rows, through his own reader."""
    bt = _belfort()
    if not bt.STATE.is_file():
        return []
    p = json.loads(bt.STATE.read_text())
    # His stops are sized per name (ATR, STOP_MIN_PCT to STOP_MAX_PCT) and
    # older positions keep STOP_LOSS, so only a loss past the widest of them
    # is one a script can call past the stop.
    stop = max(abs(bt.STOP_LOSS), bt.STOP_MAX_PCT)
    rows = []
    for c in bt.closed_trades(p):
        rows.append({
            "book": "belfort", "agent": "Belfort", "asset": "stock", "symbol": c["symbol"], "side": "long",
            "opened_utc": c.get("bought_utc"), "closed_utc": c.get("sold_utc"),
            "entry_price": c.get("entry_price"), "exit_price": c.get("exit_price"),
            "size_usd": c.get("cost"), "pnl_usd": round(c["pnl"], 2), "pnl_pct": round(c["pct"], 2),
            "setup": " · ".join(x for x in (f"score {c['score']}" if c.get("score") else "",
                                            c.get("catalyst_type") or "", c.get("regime") or "") if x),
            "score": c.get("score"), "entry_reason": c.get("entry_reason") or "",
            "exit_reason": c.get("exit_reason") or "", "exit_rule": c.get("rule"),
            "stop_pct": stop, "min_score": getattr(bt, "MIN_SCORE", None),
            "best_pct": _best_from_reason(c.get("exit_reason")),
            "after_exit_pct": _after(bt.last_price(c["symbol"]), c.get("exit_price")),
        })
    return rows


def _best_from_reason(reason):
    """Belfort's protect-gain exit says how far up it was: "was up +12.3%"."""
    m = re.search(r"was up ([+-]?\d+(?:\.\d+)?)%", reason or "")
    return float(m.group(1)) if m else None


def _after(now_price, exit_price):
    try:
        return round((float(now_price) / float(exit_price) - 1) * 100, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def bolt_rows(news=False):
    """One of Bolt's books, as journal rows."""
    b = _bolt()
    path = b.NEWS_BOOK if news else b.BOOK
    if not path.is_file():
        return []
    book = json.loads(path.read_text())
    marks = book.get("marks") or {}
    buys = {(t["symbol"], t["utc"]): t for t in book.get("trades", []) if t.get("side") == "buy"}
    rows = []
    for t in book.get("trades", []):
        if t.get("side") != "sell":
            continue
        buy = buys.get((t["symbol"], t.get("opened_utc"))) or {}
        reason = buy.get("reason") or ""
        m = re.search(r"scored (\d+)/10", reason)
        rows.append({
            "book": "bolt-news" if news else "bolt", "agent": "Bolt + news" if news else "Bolt",
            "asset": "crypto", "symbol": t["symbol"], "side": "long",
            "opened_utc": t.get("opened_utc"), "closed_utc": t.get("utc"),
            "entry_price": t.get("entry_price"), "exit_price": t.get("price"),
            "size_usd": round(float(buy.get("notional") or 0) or float(t.get("notional") or 0), 2),
            "pnl_usd": round(float(t.get("pnl") or 0), 2), "pnl_pct": round(float(t.get("pnl_pct") or 0), 2),
            "setup": f"score {m.group(1)}/10" if m else "", "score": int(m.group(1)) if m else None,
            "entry_reason": reason, "exit_reason": t.get("reason") or "",
            "exit_rule": str(t.get("reason") or "").split(" (")[0],
            "stop_pct": b.STOP_LOSS_PCT, "min_score": b.MIN_SCORE,
            "best_pct": t.get("best_pct"),
            "after_exit_pct": _after(marks.get(t["symbol"]), t.get("price")),
            "chased": _chased(b, t["symbol"], buy),
        })
    return rows


def _chased(b, sym, buy):
    """Where the buy sat in the 24 hours before it, 0 (low) to 1 (high), from
    the hourly prices Bolt keeps - None when the buy is older than they go."""
    try:
        market = json.loads(b.MARKET.read_text())
        spark = market["coins"][sym]["spark"]
        fetched = et_time.to_eastern(market["fetched_utc"])
        bought = et_time.to_eastern(buy["utc"])
        back = int((fetched - bought).total_seconds() // 3600)
    except Exception:
        return None
    end = len(spark) - back
    if back < 0 or end < 25:
        return None
    window = spark[end - 24:end]
    lo, hi = min(window), max(window)
    return round((float(buy["price"]) - lo) / (hi - lo), 2) if hi > lo else None


def all_rows():
    return belfort_rows() + bolt_rows() + bolt_rows(news=True)


def rows_between(start_day, end_day, rows=None):
    """Trades CLOSED on these Eastern days, inclusive, oldest first."""
    rows = all_rows() if rows is None else rows
    out = [r for r in rows if start_day <= (et_day(r["closed_utc"]) or "") <= end_day]
    return sorted(out, key=lambda r: str(r["closed_utc"]))


# ------------------------------------------------------------------ the analysis

def mistakes(r):
    """[(kind, detail)] a script can see in one trade."""
    out = []
    if r["pnl_pct"] < -(float(r.get("stop_pct") or 0) + 1.0) and r.get("stop_pct"):
        out.append(("loss past the stop", f"{r['pnl_pct']:+.1f}% against a {r['stop_pct']:g}% stop"))
    if r.get("score") is not None and r.get("min_score") and r["score"] < r["min_score"]:
        out.append(("bought under the bar", f"scored {r['score']}, the bar is {r['min_score']}"))
    if r.get("chased") is not None and r["chased"] >= 1 - CHASE_TOP:
        out.append(("chased", f"bought in the top {CHASE_TOP:.0%} of the 24 hours before"))
    stopped = "stop" in str(r.get("exit_rule") or "").lower()
    if r.get("after_exit_pct") is not None and r["after_exit_pct"] >= EARLY_EXIT_PCT and not stopped:
        out.append(("early exit", f"{r['after_exit_pct']:+.1f}% more since it was sold"))
    if r.get("best_pct") is not None and r["best_pct"] >= GAVE_BACK_PCT and r["pnl_pct"] <= 0:
        out.append(("gave back a gain", f"was up {r['best_pct']:+.1f}%, closed {r['pnl_pct']:+.1f}%"))
    return out


def summary(rows):
    wins = [r for r in rows if outcome(r["pnl_pct"]) == "win"]
    losses = [r for r in rows if outcome(r["pnl_pct"]) == "loss"]
    gross_w, gross_l = sum(r["pnl_usd"] for r in wins), -sum(r["pnl_usd"] for r in losses)
    return {"trades": len(rows), "wins": len(wins), "losses": len(losses),
            "scratches": len(rows) - len(wins) - len(losses),
            "net_usd": round(sum(r["pnl_usd"] for r in rows), 2),
            "win_rate": round(100 * len(wins) / len(rows), 1) if rows else None,
            "avg_win_pct": round(sum(r["pnl_pct"] for r in wins) / len(wins), 2) if wins else None,
            "avg_loss_pct": round(sum(r["pnl_pct"] for r in losses) / len(losses), 2) if losses else None,
            "expectancy_pct": round(sum(r["pnl_pct"] for r in rows) / len(rows), 2) if rows else None,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
            "avg_hold_h": round(sum(h for h in (hours_between(r["opened_utc"], r["closed_utc"]) for r in rows)
                                    if h is not None) / len(rows), 1) if rows else None}


def grouped(rows, key):
    out = {}
    for r in rows:
        out.setdefault(str(key(r)), []).append(r)
    return {k: {"trades": len(v), "net_usd": round(sum(x["pnl_usd"] for x in v), 2),
                "avg_pct": round(sum(x["pnl_pct"] for x in v) / len(v), 2),
                "wins": sum(1 for x in v if outcome(x["pnl_pct"]) == "win")} for k, v in sorted(out.items())}


def analyse(rows):
    """What went well and badly, the mistakes and the patterns - observed,
    never acted on."""
    books = {}
    for r in rows:
        books.setdefault(r["agent"], []).append(r)
    well, badly, slips = [], [], []
    for r in rows:
        line = f"{r['agent']} {r['symbol']} {r['pnl_pct']:+.1f}% (${r['pnl_usd']:+,.2f}) - {r['exit_rule']}"
        (well if outcome(r["pnl_pct"]) == "win" else badly if outcome(r["pnl_pct"]) == "loss" else []).append(line)
        for kind, detail in mistakes(r):
            slips.append({"agent": r["agent"], "symbol": r["symbol"], "kind": kind, "detail": detail})
    small = [r for r in rows if outcome(r["pnl_pct"]) == "loss" and r.get("stop_pct")
             and r["pnl_pct"] >= -float(r["stop_pct"])]
    if small:
        well.append(f"{len(small)} loss(es) kept inside the stop")
    patterns = {"by exit rule": grouped(rows, lambda r: f"{r['agent']}: {r['exit_rule']}"),
                "by setup score": grouped(rows, lambda r: f"{r['agent']}: {r['score'] if r.get('score') is not None else '?'}"),
                "by asset": grouped(rows, lambda r: r["symbol"])}
    return {"books": {a: summary(v) for a, v in books.items()}, "total": summary(rows),
            "went_well": well, "went_badly": badly, "mistakes": slips, "patterns": patterns,
            "note": DAILY_ONLY}


# ------------------------------------------------------------------ daily

def journal_line(r):
    return (f"| {et_clock(r['closed_utc'])} | {r['agent']} | {r['asset']} | {r['symbol']} | {r['side']} | "
            f"{et_clock(r['opened_utc'])} @ {r['entry_price']} | {r['exit_price']} | ${r['size_usd']:,.0f} | "
            f"${r['pnl_usd']:+,.2f} ({r['pnl_pct']:+.1f}%) | {r['setup'] or '-'} | "
            f"{_clip(r['entry_reason'])} | {_clip(r['exit_reason'])} | {outcome(r['pnl_pct'])} |")


def _clip(text, n=90):
    t = " ".join(str(text or "").split()).replace("|", "/")
    return t if len(t) <= n else t[:n - 1] + "…"


def daily_markdown(day, rows, a):
    L = [f"# Trading journal - {day}", "", f"_{DAILY_ONLY}_", "",
         "| Closed | Agent | Asset | Symbol | Side | Entry | Exit | Size | P&L | Setup | Why bought | Why sold | Outcome |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    L += [journal_line(r) for r in rows] or ["| - | no trades closed today | | | | | | | | | | | |"]
    L += ["", "## Daily analysis", ""]
    for agent, s in a["books"].items():
        L.append(f"- **{agent}**: {s['trades']} closed, {s['wins']} won / {s['losses']} lost, "
                 f"net ${s['net_usd']:+,.2f}")
    L += ["", "**What went well**", ""] + [f"- {x}" for x in a["went_well"] or ["nothing closed in profit"]]
    L += ["", "**What went badly**", ""] + [f"- {x}" for x in a["went_badly"] or ["no losing trade"]]
    L += ["", "**Mistakes**", ""] + ([f"- {m['agent']} {m['symbol']}: {m['kind']} - {m['detail']}"
                                       for m in a["mistakes"]] or ["- none a script can see"])
    L += ["", "**Patterns**", ""]
    for k, g in a["patterns"]["by exit rule"].items():
        L.append(f"- {k}: {g['trades']} trade(s), {g['wins']} won, avg {g['avg_pct']:+.1f}%")
    return "\n".join(L) + "\n"


def run_daily(day):
    rows = rows_between(day, day)
    a = analyse(rows)
    JOURNAL.mkdir(parents=True, exist_ok=True)
    (JOURNAL / f"{day}.json").write_text(json.dumps({"day": day, "rows": rows, "analysis": a}, indent=1) + "\n")
    (JOURNAL / f"{day}.md").write_text(daily_markdown(day, rows, a))
    return rows, a


# ------------------------------------------------------------------ weekly

def week_of(end_day):
    end = datetime.strptime(end_day, "%Y-%m-%d")
    return (end - timedelta(days=6)).strftime("%Y-%m-%d"), end_day


def current_rules():
    """The rules as they stand, from the scripts that own them - what a
    proposal has to name to change."""
    b, bt = _bolt(), _belfort()
    return {
        "Bolt (crypto, both books)": {
            "buy": f"best scorer {b.MIN_SCORE}+/10 on the rubric, one buy each {b.CYCLE_HOURS:g}h, "
                   f"{b.TARGET_PCT:g}% of the account, {b.CASH_FLOOR_PCT:g}% kept in cash; the news book also "
                   f"needs a cited catalyst scored {b.NEWS_MIN_SCORE}+",
            "exits": f"stop -{b.STOP_LOSS_PCT:g}%, trailing stop {b.TRAIL_PCT:g}% under the best price, "
                     f"sold after {b.MAX_HOLD_DAYS:g} days, BTC dump switch at {b.BTC_DUMP_PCT:g}%",
            "halt": f"no buys for {b.HALT_HOURS:g}h after a {b.MAX_DRAWDOWN_PCT:g}% drawdown"},
        "Belfort (stocks)": {
            "exits": f"stop {bt.STOP_LOSS:g}% (or {bt.ATR_MULT:g}x ATR, {bt.STOP_MIN_PCT:g}-{bt.STOP_MAX_PCT:g}%), "
                     f"half sold at +{bt.TAKE_PROFIT:g}%, the rest on a {bt.TRAIL:g}% trailing stop once "
                     f"+{bt.PROTECT_AT:g}%, dead money after {bt.DEAD_DAYS} days within {bt.DEAD_BAND:g}% and behind QQQ",
            "size": f"risk {bt.RISK_PER_TRADE_PCT:g}% of the portfolio a trade, at most {bt.MAX_POSITION_PCT:g}% "
                    f"in one name, {bt.MIN_CASH_PCT:g}% cash"},
    }


ASK = """You are a trading performance coach reviewing one week of two paper-trading agents.
Use ONLY the data below - every number was computed by code from their books. Do not give
financial advice; improve the process. Do not invent numbers: cite the data for every claim.

Reply in markdown with exactly these four sections:
## STOP
## START
## CONTINUE
## UPDATED RULES FOR NEXT WEEK
Under UPDATED RULES, each line names the agent, the current rule (as given below), the
proposed rule with its exact number, and the data that supports it. Propose at most three
changes in all, only where the week's data is clear; "no change" is a valid answer. A
change based on fewer than 3 trades of its kind is not clear.

CURRENT RULES
{rules}

THE WEEK ({start} to {end}, Eastern)
{data}
"""


def weekly_data(start, end):
    rows = rows_between(start, end)
    a = analyse(rows)
    kinds = {}
    for m in a["mistakes"]:
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    return rows, {"summary_by_agent": a["books"], "total": a["total"], "mistake_counts": kinds,
                  "mistakes": a["mistakes"], "patterns": a["patterns"],
                  "by_weekday": grouped(rows, lambda r: (et_time.to_eastern(r["closed_utc"]) or datetime.min).strftime("%a"))}


def run_weekly(end_day, ask=None, dry=False):
    start, end = week_of(end_day)
    rows, data = weekly_data(start, end)
    rules = current_rules()
    if len(rows) < MIN_WEEK_TRADES:
        review = (f"_{len(rows)} closed trade(s) this week - fewer than {MIN_WEEK_TRADES}. Observing only: "
                  f"no rule change is proposed on this little data._")
        asked = False
    elif dry:
        review, asked = "_dry run - the question was not asked_", False
    else:
        question = ASK.format(rules=json.dumps(rules, indent=1), start=start, end=end,
                              data=json.dumps(data, indent=1))
        ask = ask or _load("wake_coach", "wake.py").ask
        ok, text = ask("coach", question, 300)
        review = text.strip() if ok else f"_the review could not be written: {text}_"
        asked = ok
    md = [f"# Weekly review - {start} to {end}", "",
          "_Proposals only. Nothing here changes a rule until you approve it._", ""]
    for agent, s in data["summary_by_agent"].items():
        md.append(f"- **{agent}**: {s['trades']} trades, win rate {s['win_rate']}%, net ${s['net_usd']:+,.2f}, "
                  f"expectancy {s['expectancy_pct']:+.2f}% a trade, avg hold {s['avg_hold_h']}h")
    if data["mistake_counts"]:
        md.append("- mistakes seen: " + ", ".join(f"{k} {v}" for k, v in data["mistake_counts"].items()))
    md += ["", review, ""]
    WEEKLY.mkdir(parents=True, exist_ok=True)
    (WEEKLY / f"{end}.md").write_text("\n".join(md))
    (WEEKLY / f"{end}.json").write_text(json.dumps({"start": start, "end": end, "data": data, "rules": rules,
                                                    "review": review, "asked": asked,
                                                    "status": "proposed"}, indent=1) + "\n")
    return review


# ------------------------------------------------------------------ the Deck

def show():
    def latest(folder, n):
        files = sorted(folder.glob("*.json"), reverse=True)[:n] if folder.is_dir() else []
        return [json.loads(f.read_text()) for f in files]
    return {"daily": latest(JOURNAL, 14), "weekly": latest(WEEKLY, 4), "note": DAILY_ONLY}


def main(argv):
    ap = argparse.ArgumentParser(description="trading journal, daily analysis, weekly review")
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("journal")
    j.add_argument("--day")
    j.add_argument("--json", action="store_true")
    d = sub.add_parser("daily")
    d.add_argument("--day", help="default: yesterday, Eastern")
    w = sub.add_parser("weekly")
    w.add_argument("--end", help="the Sunday the week ends on; default today, Eastern")
    w.add_argument("--dry", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    today = et_time.day()
    if a.cmd == "journal":
        rows = rows_between(a.day or today, a.day or today)
        print(json.dumps(rows) if a.json else "\n".join(journal_line(r) for r in rows) or "no trades closed")
    elif a.cmd == "daily":
        day = a.day or (et_time.eastern_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        rows, an = run_daily(day)
        print(f"journal {day}: {len(rows)} trade(s), {len(an['mistakes'])} mistake(s) seen - coach/journal/{day}.md")
    elif a.cmd == "weekly":
        print(run_weekly(a.end or today, dry=a.dry))
    elif a.cmd == "show":
        print(json.dumps(show()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
