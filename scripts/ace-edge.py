#!/usr/bin/env python3
"""
ace-edge.py - where an edge in the betting might be: every sports bettor
side by side, every bet they made pooled, and every game's both sides,
grouped by what was known when the bet could be placed. Read-only.

    python3 scripts/ace-edge.py           the report
    python3 scripts/ace-edge.py --json    the same numbers

Owner, 2026-11-06, after Ace's evidence stop (60 bets, closing-line value
-5.3%): "analyze that data to generate ideas for finding a new edge or
identifying commonalities with strategies that are winning". Winning is
profit, believed only when the closing price or both halves of the period
back it up - with this few bets, profit alone is mostly luck.

  1. THE BETTORS: Ace, the six no-AI bettors and Ace Props - profit, a
     luck range, closing-line value, how often each beat the close.
  2. EVERY BET, POOLED: every game bet any of them made, grouped by sport,
     favourite or underdog, home or away, time before the start, how the
     price moved in the two hours before, and Pinnacle against DraftKings.
  3. THE WHOLE MARKET: every game in the price history, both sides, at
     DraftKings' price 6 hours before the start - far more than were bet -
     in the same groups. A group that beats the close here is a price the
     market gets wrong, whoever bets it.
  4. IDEAS: the groups with 30+ bets whose closing-line value is above zero
     and that made money in both halves. Many groups are tested, so some
     look good by luck: an idea here is something to run on paper, not proof.

Every price, close and result is read through ace-clv.py, ace-book.py and
ace-baselines.py; the luck range is ace-study.py's. Standard library only.
"""

import importlib.util
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
STATE = ROOT / "agents" / "ace" / "state"
STAKE = 100.0
MARKET_HOURS = 6              # the whole-market view prices each game this long before its start
MIN_IDEA = 30                 # bets a group needs before it can be called an idea
MOVE_PTS = 1.0                # a move in the two hours before, in no-vig points, that counts
GAP_PTS = 1.5                 # Pinnacle above or below DraftKings by this much counts


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mods():
    study = _load("ace_study_edge", "ace-study.py")
    book, clv, _judge, base = study.mods()
    return study, book, clv, base


# ------------------------------------------------------------------ one decision, graded

def decision(who, entry, side, price, pct_then, at, results, clv, book, sharp_then=None, event_id=None,
             sport=None):
    """One bet - placed, or one a rule could have placed - with what was known
    then and how it came out: its close, CLV, result and flat-$100 profit.
    None when the game has no result (it is too old, or not over)."""
    entry = entry or {}
    sport = sport or entry.get("sport")
    res = results.get(f"{sport}|{event_id or entry.get('event_id')}") or {}
    if res.get("winner") not in ("home", "away") or price is None or pct_then is None:
        return None
    start = clv.parse_start(entry.get("start_utc"))
    before = clv.price_at(entry, side, at - timedelta(hours=2)) if at else None
    close = clv.closing(entry, side, "draftkings", at)[0] if entry else None
    c = clv.clv_at(price, close)
    won = res["winner"] == side
    d = book.decimal_odds(price)
    return {"who": who, "sport": str(sport or "?").upper(), "side": side, "price": price,
            "pct_then": float(pct_then), "sharp_then": float(sharp_then) if sharp_then is not None else None,
            "hours_before": (start - at).total_seconds() / 3600 if start and at else None,
            "moved_pts": float(pct_then) - before if before is not None else None,
            "clv_pct": c, "won": won, "profit": STAKE * (d - 1) if won and d else -STAKE,
            "at": at.strftime("%Y-%m-%d %H:%M") if at else ""}


def bettor_decisions(clv, book, base, lines, results):
    """Every game bet the bettors placed: Ace's (never Ace Props'), then each
    no-AI bettor's."""
    out = []
    for b in book.fold(book.events(STATE / "bets.jsonl"))["settled"]:
        if book.is_props(b) or b.get("side") not in ("home", "away"):
            continue
        entry = lines.get(clv.game_key(b.get("sport"), b.get("match"), b.get("starts_utc")))
        at = clv.parse_start(str(b.get("utc") or "").replace(" ", "T") + "Z")
        d = decision("ace", entry, b["side"], b.get("price"), b.get("novig_pct"), at, results, clv, book,
                     b.get("sharp_pct"), b.get("event_id"), b.get("sport"))
        if d:
            out.append(d)
    for p in base.picks(STATE / "baselines.jsonl"):
        if p.get("side") not in ("home", "away"):
            continue
        d = decision(p.get("strategy"), lines.get(p.get("key")), p["side"], p.get("price"), p.get("novig_pct"),
                     clv.parse_start(p.get("utc")), results, clv, book, p.get("sharp_pct"), p.get("event_id"),
                     p.get("sport"))
        if d:
            out.append(d)
    return out


def market_decisions(clv, book, lines, results, hours=MARKET_HOURS):
    """Both sides of every game with a result, at DraftKings' price `hours`
    before the start - what any rule could have bet then."""
    out = []
    for entry in lines.values():
        start = clv.parse_start(entry.get("start_utc"))
        if not start:
            continue
        at = start - timedelta(hours=hours)
        for side in ("home", "away"):
            snap = clv.snapshot_at(entry, side, at)
            if not snap or snap.get(f"moneyline_{side}") is None:
                continue
            d = decision("market", entry, side, snap[f"moneyline_{side}"], snap.get(f"novig_{side}_pct"), at,
                         results, clv, book, snap.get(f"sharp_{side}_pct"))
            if d:
                out.append(d)
    return out


# ------------------------------------------------------------------ grouping

def band(d):
    q = d["pct_then"]
    return ("big favourite (65%+)" if q >= 65 else "favourite (50-65%)" if q >= 50
            else "underdog (35-50%)" if q >= 35 else "long shot (under 35%)")


def timing(d):
    h = d["hours_before"]
    return None if h is None else "under 1h" if h < 1 else "1-3h" if h < 3 else "3-8h" if h < 8 else "8h+"


def moved(d):
    m = d["moved_pts"]
    return (None if m is None else f"moved toward it {MOVE_PTS:g}+ pts" if m >= MOVE_PTS
            else f"moved against it {MOVE_PTS:g}+ pts" if m <= -MOVE_PTS else "barely moved")


def sharp_gap(d):
    if d["sharp_then"] is None:
        return "no Pinnacle price"
    g = d["sharp_then"] - d["pct_then"]
    return (f"Pinnacle {GAP_PTS:g}+ pts higher" if g >= GAP_PTS else f"Pinnacle {GAP_PTS:g}+ pts lower"
            if g <= -GAP_PTS else "Pinnacle about the same")


FEATURES = (
    ("sport", lambda d: d["sport"]),
    ("favourite or underdog", band),
    ("home or away", lambda d: d["side"]),
    ("time before the start", timing),
    ("price in the 2 hours before", moved),
    ("Pinnacle against DraftKings", sharp_gap),
)


def summarise(rows, ci):
    """n, profit, ROI with its luck range, CLV, beat-close share, both halves."""
    rows = sorted(rows, key=lambda d: d["at"])
    graded = [d["clv_pct"] for d in rows if d["clv_pct"] is not None]
    half = len(rows) // 2
    first, second = sum(d["profit"] for d in rows[:half]), sum(d["profit"] for d in rows[half:])
    profit = sum(d["profit"] for d in rows)
    return {"bets": len(rows), "won": sum(d["won"] for d in rows), "profit": round(profit, 2),
            "roi_pct": round(profit / (STAKE * len(rows)) * 100, 1) if rows else None,
            "roi_range_pct": ci([d["profit"] / STAKE * 100 for d in rows]) if len(rows) > 1 else None,
            "avg_clv_pct": round(sum(graded) / len(graded), 2) if graded else None,
            "beat_close_pct": round(sum(1 for c in graded if c > 0) / len(graded) * 100) if graded else None,
            "both_halves": len(rows) >= 6 and first > 0 and second > 0}


def grouped(rows, ci):
    out = {}
    for name, key in FEATURES:
        by = {}
        for d in rows:
            k = key(d)
            if k is not None:
                by.setdefault(k, []).append(d)
        out[name] = {k: summarise(v, ci) for k, v in sorted(by.items())}
    return out


def ideas(sections):
    """[(where, feature, group, summary)] with MIN_IDEA+ bets, closing-line
    value above zero and money made in both halves - best CLV first."""
    found, tested = [], 0
    for where, groups in sections.items():
        for feature, gs in groups.items():
            for g, s in gs.items():
                tested += 1
                if s["bets"] >= MIN_IDEA and (s["avg_clv_pct"] or 0) > 0 and s["both_halves"]:
                    found.append((where, feature, g, s))
    return sorted(found, key=lambda x: -x[3]["avg_clv_pct"]), tested


# ------------------------------------------------------------------ the report

def edge():
    study, book, clv, base = mods()
    lines = clv._read(clv.LINES, {})
    results = clv._read(STATE / "results.json", {})
    bets = bettor_decisions(clv, book, base, lines, results)
    market = market_decisions(clv, book, lines, results)
    who = {}
    for d in bets:
        who.setdefault(d["who"], []).append(d)
    bettors = {k: summarise(v, study.ci) for k, v in who.items()}
    try:
        record = base.report(STATE / "baselines.jsonl")      # Ace's own book profit, beside the re-grade
        bettors.setdefault("ace", {})["book_profit"] = record.get("ace", {}).get("profit")
    except Exception:
        pass
    try:
        props = _load("ace_props_edge", "ace-props.py").board_json()["summary"]
        bettors["ace props"] = {"bets": props["won"] + props["lost"], "won": props["won"], "profit": props["profit"],
                                "avg_clv_pct": props["avg_clv_pct"], "props": True}
    except Exception as exc:
        bettors["ace props"] = {"error": str(exc)[:160]}
    sections = {"every bet, pooled": grouped(bets, study.ci),
                f"the whole market, {MARKET_HOURS}h before": grouped(market, study.ci)}
    found, tested = ideas(sections)
    return {"bettors": bettors, "pooled_bets": len(bets), "market_sides": len(market),
            "market": summarise(market, study.ci) if market else None,
            "sections": sections, "ideas": [{"where": w, "feature": f, "group": g, **s} for w, f, g, s in found],
            "groups_tested": tested}


def line(name, s, width=34):
    if not s or not s.get("bets"):
        return f"   {name:<{width}} no settled bets"
    r = s.get("roi_range_pct")
    return (f"   {name:<{width}} {s['bets']:>4} bets  ${s['profit']:+9,.0f}"
            + (f"  {s['roi_pct']:+6.1f}% a bet" if s.get("roi_pct") is not None else "")
            + (f" (luck {r[0]:+.0f} to {r[1]:+.0f})" if r else "")
            + (f"  close {s['avg_clv_pct']:+.1f}%" if s.get("avg_clv_pct") is not None else "")
            + (f", beat it {s['beat_close_pct']}%" if s.get("beat_close_pct") is not None else "")
            + ("  *" if s.get("both_halves") else ""))


def text(e):
    out = ["WHERE AN EDGE MIGHT BE - the sports bettors (read-only; changes nothing)", "",
           "Profit is flat $100 a bet. 'close' is closing-line value: above zero means the price taken beat",
           "DraftKings' last price - the early sign of a real edge. * = made money in both halves.", "",
           "1. THE BETTORS"]
    for name, s in sorted(e["bettors"].items(), key=lambda kv: -(kv[1].get("profit") or 0)):
        if s.get("error"):
            out.append(f"   {name:<34} could not be read: {s['error']}")
            continue
        out.append(line(name, s) + (f"  (his book: ${s['book_profit']:+,.0f})" if s.get("book_profit") is not None
                                    else "") + ("  - player props, own measures" if s.get("props") else ""))
    for where, groups in e["sections"].items():
        n = e["pooled_bets"] if where.startswith("every bet") else e["market_sides"]
        out += ["", f"{'2' if where.startswith('every bet') else '3'}. {where.upper()} ({n} sides with a result)"]
        if where.startswith("the whole") and e["market"]:
            out.append(line("all of it", e["market"]) + "   <- what betting everything costs")
        for feature, gs in groups.items():
            out.append(f"  {feature}:")
            out += [line(g, s) for g, s in gs.items()]
    out += ["", f"4. IDEAS - {MIN_IDEA}+ bets, beat the close on average, made money in both halves "
                f"({len(e['ideas'])} of {e['groups_tested']} groups tested)"]
    if not e["ideas"]:
        out.append("   None yet. Nothing that can be seen before a bet has beaten the close with enough bets -")
        out.append("   the market's price is right on average in every group measured so far.")
    for i in e["ideas"]:
        out.append(line(f"{i['group']} ({i['feature']}, {i['where']})", i, 0))
    out += ["", f"With {e['groups_tested']} groups tested, about 1 in 20 can look good by luck alone. An idea is",
            "something to run on paper from today, not proof. Price history is kept 30 days, results 40."]
    return "\n".join(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    e = edge()
    print(json.dumps(e, indent=1) if "--json" in argv else text(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
