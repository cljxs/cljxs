#!/usr/bin/env python3
"""
coach.py - three coaches, one each for Belfort, Bolt and Ace: a journal, a
daily analysis, and a weekly review.

    coach.py journal [--coach C] [--day YYYY-MM-DD] [--json]   the day's trades
    coach.py daily   [--coach C] [--day YYYY-MM-DD]   journal + analysis, saved (no model)
    coach.py weekly  [--coach C] [--end YYYY-MM-DD] [--dry]   the week's review and proposals
    coach.py backfill --book ace --since YYYY-MM-DD   one book's past days, the rest kept
    coach.py split                                    the combined journal, split once
    coach.py show --json                              what the Deck draws

C is belfort, bolt or ace; without --coach, all three in turn.

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

THE WEEKLY REVIEW: each coach asks the owner's Claude plan ONE question (wake.py ask,
"coach" in tasks/engines.json, Sonnet, no tools): here is the week's
measured data and the current rules - what to STOP, START, CONTINUE, and the
rules proposed for next week, each citing a number from the data. Code
computes every number it is shown. The proposals are proposals: they are
saved for the owner, and nothing here edits a rule. A week with too few
closed trades gets no proposals - a rule changed on three trades is noise.

ACE (owner, 2026-10-22: "a coach for ace like how belfort has"). His settled
bets are journaled from his own book (ace-book.py), each with its edge from
ace-judge.py's bar and its closing-line value from ace-clv.py - the measure
his preregistration is judged on; a loss is not a mistake in betting, a
price the market then beat is. His rules are frozen until ace-judge.py's
FROZEN_UNTIL, so until then the Sunday review only comments on him, under
its own heading, and proposes nothing: a rule changed early restarts the
clock. After it, he is reviewed like the others.

THREE COACHES, NOT ONE (owner, 2026-10-23: "make bolt, belfort and aces
coach all separate from each other so me and the data [don't] get
confused"). Each keeps its own files under coach/<belfort|bolt|ace>/ and
asks its own Sunday question, which sees only its agent's numbers and rules.
Bolt's two books share his coach: they are one comparison, the same rules
with and without a news check.

Times are Eastern (et_time). Free apart from one question a week per coach.
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
COMBINED = HOME / "combined-before-split"     # the one coach's files, until 2026-10-23

FLAT_PCT = 0.25               # within this of zero, a trade is a scratch, not a win or a loss
CHASE_TOP = 0.10              # bought in the top 10% of the 24 hours before: chasing
EARLY_EXIT_PCT = 3.0          # sold, then it rose this much more: an early exit (stops excepted)
GAVE_BACK_PCT = 5.0           # was up this much and closed at or under zero
MIN_WEEK_TRADES = 5           # fewer closed trades than this: observe, propose nothing
MIN_WEEK_BETS = 10            # fewer settled Ace bets than this: no comment on him either
EDGE_BANDS = ((3.0, "edge under 3 pts"), (5.0, "edge 3-5 pts"), (float("inf"), "edge 5+ pts"))
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


def _ace():
    """Ace's book, closing lines and bar - read there, never restated."""
    return (_load("ace_book_coach", "ace-book.py"), _load("ace_clv_coach", "ace-clv.py"),
            _load("ace_judge_coach", "ace-judge.py"))


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


def ace_rows():
    """Ace's settled bets, as journal rows. The P&L is the book's; "entry" is
    the price taken and "exit" the closing price, because the close is what a
    bet is graded on. Closing lines are kept 30 days (ace-clv.KEEP_DAYS), so
    an older bet carries none."""
    book, clv, judge = _ace()
    if not book.BETS.is_file():
        return []
    lines = clv._read(clv.LINES, {})
    rows = []
    for b in book.fold(book.events())["settled"]:
        if book.is_props(b):           # Ace Props: his bankroll, not his record
            continue
        stake, profit = float(b.get("stake") or 0), float(b.get("profit") or 0)
        entry = lines.get(clv.game_key(b.get("sport"), b.get("match"), b.get("starts_utc")))
        g = clv.grade({"selection": b.get("selection"), "status": "bet", "price": b.get("price"),
                       "novig_pct": b.get("novig_pct"), "my_pct": b.get("my_pct")}, entry) if entry else None
        close, whose = clv.headline_clv(g) if g else (None, None)
        bar = judge.bar(b.get("my_pct"), b) if b.get("my_pct") is not None else None
        sport = str(b.get("sport") or "").upper()
        why_sold = f"{b.get('result')}: {b.get('detail') or ''}".strip()
        if close is not None:
            why_sold += f"; closed at {g.get('close_price')}, {whose} CLV {close:+.1f}%"
        rows.append({
            "book": "ace", "agent": "Ace", "asset": f"{sport} bet", "symbol": b.get("selection"),
            "side": b.get("side") or "", "opened_utc": b.get("utc"), "closed_utc": b.get("settled_utc"),
            "entry_price": b.get("price"), "exit_price": (g or {}).get("close_price") or "-",
            "size_usd": stake, "pnl_usd": round(profit, 2),
            "pnl_pct": round(profit / stake * 100, 2) if stake else 0.0,
            "setup": " · ".join(x for x in (sport, f"edge {bar['gap_pts']:+.1f} pts vs {bar['q_source']} "
                                                  f"{float(bar['q']):.1f}%" if bar and bar["gap_pts"] is not None
                                            else "") if x),
            "score": None, "entry_reason": b.get("why") or "", "exit_reason": why_sold,
            "exit_rule": b.get("result"), "stop_pct": None, "min_score": None,
            "best_pct": None, "after_exit_pct": None, "chased": None,
            "sport": sport, "market_pct": bar["q"] if bar else b.get("novig_pct"),
            "edge_pts": bar["gap_pts"] if bar else None,
            "under_bar": (bar["why"] if bar and bar["gap_pts"] is not None and not bar["clears"] else None),
            "clv_pct": close, "clv_book": whose,
        })
    return rows


COACHES = {
    "belfort": {"name": "Belfort", "what": "a paper stock trader", "books": ("belfort",),
                "rows": lambda: belfort_rows()},
    "bolt": {"name": "Bolt", "what": "a paper crypto trader with two books: plain rules, and the same rules "
                                     "with a news check before each buy", "books": ("bolt", "bolt-news"),
             "rows": lambda: bolt_rows() + bolt_rows(news=True)},
    "ace": {"name": "Ace", "what": "a paper sports bettor", "books": ("ace",), "rows": lambda: ace_rows()},
}
BOOK_COACH = {b: k for k, c in COACHES.items() for b in c["books"]}


def journal_dir(coach):
    return HOME / coach / "journal"


def weekly_dir(coach):
    return HOME / coach / "weekly"


def all_rows():
    return [r for c in COACHES.values() for r in c["rows"]()]


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
    # Ace: a lost bet is not a mistake - a 60% shot loses four times in ten.
    # A price the market beat by the close is, and so is a bet off his bar.
    if r.get("clv_pct") is not None and r["clv_pct"] < 0:
        out.append(("beaten by the close", f"took {r['entry_price']}, closed {r['exit_price']}: "
                                           f"{r['clv_book']} CLV {r['clv_pct']:+.1f}%"))
    if r.get("under_bar"):
        out.append(("bet under the bar", r["under_bar"]))
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
    trades = [r for r in rows if r["book"] != "ace"]
    patterns = {"by exit rule": grouped(rows, lambda r: f"{r['agent']}: {r['exit_rule']}"),
                "by setup score": grouped(trades, lambda r: f"{r['agent']}: {r['score'] if r.get('score') is not None else '?'}"),
                "by asset": grouped(trades, lambda r: r["symbol"])}
    return {"books": {a: summary(v) for a, v in books.items()}, "total": summary(rows),
            "went_well": well, "went_badly": badly, "mistakes": slips, "patterns": patterns,
            "ace": ace_summary(rows), "note": DAILY_ONLY}


def ace_summary(rows):
    """Ace's bets measured the way his preregistration measures them -
    closing-line value first - and grouped by what might explain it."""
    bets = [r for r in rows if r["book"] == "ace"]
    if not bets:
        return None
    closed = [r for r in bets if r.get("clv_pct") is not None]

    def band(r):
        e = r.get("edge_pts")
        return "edge unknown" if e is None else next(name for top, name in EDGE_BANDS if e < top)
    return {"bets": len(bets), "with_close": len(closed),
            "avg_clv_pct": round(sum(r["clv_pct"] for r in closed) / len(closed), 2) if closed else None,
            "beat_close": sum(1 for r in closed if r["clv_pct"] > 0),
            "frozen_until": _ace()[2].FROZEN_UNTIL,
            "by sport": grouped(bets, lambda r: r["sport"]),
            "favourite or underdog": grouped(bets, lambda r: "unknown" if r.get("market_pct") is None
                                             else "favourite" if float(r["market_pct"]) >= 50 else "underdog"),
            "by edge": grouped(bets, band)}


def ace_line(s):
    clv = (f"closing-line value {s['avg_clv_pct']:+.2f}% a bet over {s['with_close']}, beat the close on "
           f"{s['beat_close']}" if s["with_close"] else "no closing line kept for these bets")
    return f"{s['bets']} bet(s) settled; {clv}. Rules frozen until {s['frozen_until']}."


# ------------------------------------------------------------------ daily

def journal_line(r):
    return (f"| {et_clock(r['closed_utc'])} | {r['agent']} | {r['asset']} | {r['symbol']} | {r['side']} | "
            f"{et_clock(r['opened_utc'])} @ {r['entry_price']} | {r['exit_price']} | ${r['size_usd']:,.0f} | "
            f"${r['pnl_usd']:+,.2f} ({r['pnl_pct']:+.1f}%) | {r['setup'] or '-'} | "
            f"{_clip(r['entry_reason'])} | {_clip(r['exit_reason'])} | {outcome(r['pnl_pct'])} |")


def _clip(text, n=90):
    t = " ".join(str(text or "").split()).replace("|", "/")
    return t if len(t) <= n else t[:n - 1] + "…"


def daily_markdown(day, rows, a, coach):
    L = [f"# {COACHES[coach]['name']}'s journal - {day}", "", f"_{DAILY_ONLY}_", "",
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
    if a.get("ace"):
        L += ["", "**Ace**", "", f"- {ace_line(a['ace'])}"]
        for k in ("by sport", "favourite or underdog", "by edge"):
            L += [f"- {k}: " + ", ".join(f"{n} {g['wins']}/{g['trades']} won, ${g['net_usd']:+,.2f}"
                                         for n, g in a["ace"][k].items())]
    return "\n".join(L) + "\n"


def write_day(coach, day, rows):
    a = analyse(rows)
    folder = journal_dir(coach)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{day}.json").write_text(json.dumps({"day": day, "coach": coach, "rows": rows, "analysis": a},
                                                   indent=1) + "\n")
    (folder / f"{day}.md").write_text(daily_markdown(day, rows, a, coach))
    return a


def run_daily(day, coach, books=None):
    """One coach's journal and analysis for the day. With `books`, only those
    books are read again; the coach's other rows are kept from the day's
    saved journal, so a backfill does not lose what can no longer be worked
    out (Bolt's chase reading needs hourly prices only a week old)."""
    rows = rows_between(day, day, COACHES[coach]["rows"]())
    saved = journal_dir(coach) / f"{day}.json"
    if books is not None and saved.is_file():
        kept = [r for r in json.loads(saved.read_text()).get("rows", []) if r.get("book") not in books]
        rows = sorted(kept + [r for r in rows if r["book"] in books], key=lambda r: str(r["closed_utc"]))
    return rows, write_day(coach, day, rows)


def split_combined():
    """Until 2026-10-23 one coach kept one journal for every agent. Each
    saved day is split into the three coaches' journals - from what was
    saved, so Bolt's chase readings, which can no longer be worked out, are
    kept - and the old folders are moved to coach/combined-before-split/.
    The combined Sunday review moves there too: it was all three agents
    together and belongs to none of them. Runs once; after that there is
    nothing left to split."""
    old_j, old_w = HOME / "journal", HOME / "weekly"
    days = 0
    for f in sorted(old_j.glob("*.json")) if old_j.is_dir() else []:
        try:
            doc = json.loads(f.read_text())
        except Exception:
            continue
        for coach in COACHES:
            if not (journal_dir(coach) / f.name).exists():
                write_day(coach, doc.get("day") or f.stem,
                          [r for r in doc.get("rows", []) if BOOK_COACH.get(r.get("book")) == coach])
        days += 1
    for d in (old_j, old_w):
        if d.is_dir():
            COMBINED.mkdir(parents=True, exist_ok=True)
            target = COMBINED / d.name
            d.rename(target if not target.exists() else COMBINED / f"{d.name}-{datetime.now():%Y%m%d%H%M%S}")
    return days


# ------------------------------------------------------------------ weekly

def week_of(end_day):
    end = datetime.strptime(end_day, "%Y-%m-%d")
    return (end - timedelta(days=6)).strftime("%Y-%m-%d"), end_day


def rules_for(coach):
    """One coach's agent's rules as they stand, from the scripts that own
    them - what a proposal has to name to change. Only its own: a coach
    shown another agent's rules has proposed for the wrong one."""
    b, bt = _bolt(), _belfort()
    ab, _clv, aj = _ace()
    key = {"bolt": "Bolt (crypto, both books)", "belfort": "Belfort (stocks)", "ace": "Ace (sports bets)"}[coach]
    return {key: _rules(b, bt, ab, aj)[key]}


def _rules(b, bt, ab, aj):
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
        "Ace (sports bets)": {
            "bet": f"his blind estimate pulled {aj.SHRINK:.0%} of the way to the market (Pinnacle, else "
                   f"DraftKings) must clear it by {aj.GAP_MIN_PTS:g} pts or {aj.GAP_MIN_REL:g}% of it, with "
                   f"positive EV; sides priced {aj.BET_RANGE[0]:g}-{aj.BET_RANGE[1]:g}% only; "
                   f"${ab.STARTING_BANKROLL * ab.FLAT_STAKE_PCT / 100:,.0f} flat, at most {ab.MAX_OPEN_BETS} open",
            "measured by": "closing-line value (Pinnacle's close, else DraftKings'), not win rate",
            "stops": f"betting stops after {aj.EVIDENCE_MIN_BETS} bets with average CLV at or below zero, "
                     f"or at a {ab.FAULT_STOP_PCT:g}% loss",
            "status": f"FROZEN until {aj.FROZEN_UNTIL} by his preregistration"},
    }


ASK = """You are {name}'s performance coach - {name} is {what} - reviewing one week of {name} alone.
Use ONLY the data below - every number was computed by code from the books. Do not give
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

{notes}
CURRENT RULES
{rules}

THE WEEK ({start} to {end}, Eastern)
{data}
"""


def weekly_data(start, end, coach):
    rows = rows_between(start, end, COACHES[coach]["rows"]())
    a = analyse(rows)
    kinds = {}
    for m in a["mistakes"]:
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    return rows, {"summary_by_agent": a["books"], "total": a["total"], "mistake_counts": kinds,
                  "mistakes": a["mistakes"], "patterns": a["patterns"], "ace": a["ace"],
                  "by_weekday": grouped(rows, lambda r: (et_time.to_eastern(r["closed_utc"]) or datetime.min).strftime("%a"))}


def week_notes(coach, rows, end):
    """(notes for the question, why there is no question or None) - decided
    here rather than left to the model: whether the week has enough data,
    and whether Ace's rules are still frozen."""
    if coach != "ace":
        if len(rows) < MIN_WEEK_TRADES:
            return [], f"{len(rows)} closed trade(s) this week - fewer than {MIN_WEEK_TRADES}"
        return (["Bolt and Bolt + news trade the same rules; the news book also needs a cited news catalyst. "
                 "Say whether the news check helped this week, with the numbers."] if coach == "bolt" else []), None
    if len(rows) < MIN_WEEK_BETS:
        return [], f"{len(rows)} settled bet(s) this week - fewer than {MIN_WEEK_BETS}"
    frozen_until = _ace()[2].FROZEN_UNTIL
    judge = "Judge him on closing-line value, not wins: a lost bet is not a mistake."
    if end < frozen_until:
        return [f"HIS RULES ARE FROZEN until {frozen_until} by a preregistration: changing one restarts his "
                f"experiment. Under UPDATED RULES write only \"frozen until {frozen_until}\" and propose NO "
                f"change. Add a fifth section, ## NOTES FOR THE {frozen_until} READ-OUT, with at most three "
                f"observations, each citing a number. {judge}"], None
    return [judge], None


def run_weekly(end_day, coach, ask=None, dry=False):
    start, end = week_of(end_day)
    rows, data = weekly_data(start, end, coach)
    rules = rules_for(coach)
    name = COACHES[coach]["name"]
    notes, too_few = week_notes(coach, rows, end)
    if too_few:
        review = f"_{too_few}. Observing only: no rule change is proposed on this little data._"
        asked = False
    elif dry:
        review, asked = "_dry run - the question was not asked_", False
    else:
        question = ASK.format(name=name, what=COACHES[coach]["what"], rules=json.dumps(rules, indent=1),
                              start=start, end=end, data=json.dumps(data, indent=1),
                              notes=("THIS WEEK\n" + "\n".join(f"- {n}" for n in notes) + "\n") if notes else "")
        ask = ask or _load("wake_coach", "wake.py").ask
        ok, text = ask("coach", question, 300)
        review = text.strip() if ok else f"_the review could not be written: {text}_"
        asked = ok
    md = [f"# {name}'s weekly review - {start} to {end}", "",
          "_Proposals only. Nothing here changes a rule until you approve it._", ""]
    if data["ace"]:
        md.append(f"- **Ace**: {ace_line(data['ace'])}")
    else:
        for agent, s in data["summary_by_agent"].items():
            md.append(f"- **{agent}**: {s['trades']} trades, win rate {s['win_rate']}%, net ${s['net_usd']:+,.2f}, "
                      f"expectancy {s['expectancy_pct']:+.2f}% a trade, avg hold {s['avg_hold_h']}h")
    if data["mistake_counts"]:
        md.append("- mistakes seen: " + ", ".join(f"{k} {v}" for k, v in data["mistake_counts"].items()))
    md += ["", review, ""]
    folder = weekly_dir(coach)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{end}.md").write_text("\n".join(md))
    (folder / f"{end}.json").write_text(json.dumps({"start": start, "end": end, "coach": coach, "data": data,
                                                    "rules": rules, "review": review, "asked": asked,
                                                    "status": "proposed"}, indent=1) + "\n")
    return review


# ------------------------------------------------------------------ the Deck

def show():
    def latest(folder, n):
        files = sorted(folder.glob("*.json"), reverse=True)[:n] if folder.is_dir() else []
        return [json.loads(f.read_text()) for f in files]
    return {"coaches": {k: {"name": c["name"], "what": c["what"], "daily": latest(journal_dir(k), 14),
                            "weekly": latest(weekly_dir(k), 4)} for k, c in COACHES.items()},
            "note": DAILY_ONLY}


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
    for x in (j, d, w):
        x.add_argument("--coach", choices=list(COACHES), help="one coach; all three when left out")
    sub.add_parser("split", help="split the combined journal into the three coaches (done by itself too)")
    bf = sub.add_parser("backfill", help="one book's past days, every other book's rows kept as saved")
    bf.add_argument("--book", required=True, choices=["ace", "belfort", "bolt", "bolt-news"])
    bf.add_argument("--since", required=True, help="YYYY-MM-DD, Eastern")
    s = sub.add_parser("show")
    s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    split = split_combined()
    if split and a.cmd != "show":
        print(f"split the combined journal: {split} day(s) into {', '.join(COACHES)}; the old files are in "
              f"{COMBINED.relative_to(ROOT)}/")
    today = et_time.day()
    coaches = [a.coach] if getattr(a, "coach", None) else list(COACHES)
    if a.cmd == "journal":
        rows = [r for c in coaches for r in rows_between(a.day or today, a.day or today, COACHES[c]["rows"]())]
        print(json.dumps(rows) if a.json else "\n".join(journal_line(r) for r in rows) or "no trades closed")
    elif a.cmd == "daily":
        day = a.day or (et_time.eastern_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        for c in coaches:
            rows, an = run_daily(day, c)
            print(f"{COACHES[c]['name']}'s journal {day}: {len(rows)} closed, {len(an['mistakes'])} mistake(s) "
                  f"seen - coach/{c}/journal/{day}.md")
    elif a.cmd == "weekly":
        for c in coaches:
            print(f"# {COACHES[c]['name']}\n{run_weekly(a.end or today, c, dry=a.dry)}\n")
    elif a.cmd == "split":
        print("nothing left to split" if not split else "done")
    elif a.cmd == "backfill":
        day = datetime.strptime(a.since, "%Y-%m-%d")
        yesterday = (et_time.eastern_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        n = 0
        while day.strftime("%Y-%m-%d") <= yesterday:
            rows, _an = run_daily(day.strftime("%Y-%m-%d"), BOOK_COACH[a.book], books={a.book})
            n += sum(1 for r in rows if r["book"] == a.book)
            day += timedelta(days=1)
        print(f"backfill {a.book} since {a.since}: {n} row(s) journaled in coach/{BOOK_COACH[a.book]}/; "
              f"other books kept as saved")
    elif a.cmd == "show":
        print(json.dumps(show()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
