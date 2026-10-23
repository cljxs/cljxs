#!/usr/bin/env python3
"""
ace-baselines.py - bettors with no AI in them, on Ace's games, so there is
something to compare him to.

    python3 scripts/ace-baselines.py run       decide on the current candidates (the fetcher calls this)
    python3 scripts/ace-baselines.py report    each strategy's record, beside Ace's
    python3 scripts/ace-baselines.py report --json

WHY (2026-10-01, from the outside review). Ace's results alone cannot say
whether the AI adds anything: a rule a script can follow might do as well.
These follow fixed rules on the same candidates, the same prices, flat $100
stakes, graded on the same closes and the same final scores:

  random     a coin flip per game, fixed by the game's id - what no skill
             looks like, on this book's prices
  favourite  the side the market favours, every game
  news       the side a fresh "Out" or "Doubtful" report (3 hours or less)
             favours - the injured team's opponent - and only while the
             price has not moved toward it since (ace-clv.unpriced, the same
             check Ace's bet must pass). The AI-free version of Ace's rule 2,
             and the most important comparison: if this matches Ace, the
             model is decoration.
  sharp      the side where Pinnacle's no-vig chance is at least 2 points
             above DraftKings' - "the soft book has not caught up". Needs
             ace-sharp.py's key; silent without it.
  steam      the side whose DraftKings chance rose 1.5 points or more in
             the last hour - does the market's own direction carry on?

`random` and `favourite` decide on games starting within 6 hours, about
when Ace bets; the others decide whenever their signal appears. Each
strategy bets a game at most once. Nothing here wakes a model or moves
Ace's bankroll: it writes state/baselines.jsonl and nothing else.

Standard library only.
"""

import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
AGENT = ROOT / "agents" / "ace"
PICKS = AGENT / "state" / "baselines.jsonl"

STRATEGIES = ("random", "favourite", "news", "sharp", "steam")
EVERY_GAME_WITHIN_HOURS = 6.0
NEWS_HOURS = 3.0
NEWS_STATUSES = ("out", "doubtful")
SHARP_GAP_PTS = 2.0
STEAM_PTS, STEAM_MINUTES = 1.5, 60
STAKE = 100.0


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def picks(path=None):
    out = []
    try:
        text = Path(path or PICKS).read_text()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def games(rows):
    """{game key: {"home": row, "away": row}} from candidate rows."""
    out = {}
    for r in rows:
        if r.get("side") in ("home", "away") and r.get("price") is not None:
            out.setdefault((r.get("sport"), r.get("match"), r.get("starts_utc")), {})[r["side"]] = r
    return {k: v for k, v in out.items() if len(v) == 2}


def other(side):
    return "away" if side == "home" else "home"


def decide(rows, lines, now, clv):
    """[(strategy, row, why)] for every strategy that wants a side now."""
    out = []
    for (sport, match, start), sides in games(rows).items():
        begins = clv.parse_start(start)
        if begins is None or begins <= now:
            continue
        hours = (begins - now).total_seconds() / 3600
        entry = lines.get(clv.game_key(sport, match, start))
        h, a = sides["home"], sides["away"]

        if hours <= EVERY_GAME_WITHIN_HOURS:
            flip = int(hashlib.sha1(f"{sport}|{h.get('event_id')}".encode()).hexdigest(), 16) % 2
            out.append(("random", h if flip == 0 else a, "coin flip by game id"))
            if h.get("novig_pct") is not None and a.get("novig_pct") is not None:
                fav = h if float(h["novig_pct"]) >= float(a["novig_pct"]) else a
                out.append(("favourite", fav, f"market favourite at {fav['novig_pct']}%"))

        hurt = {}
        team_of = {str(r.get("selection") or "").rsplit(" ML", 1)[0]: r["side"] for r in (h, a)}
        for inj in h.get("fresh_injuries") or []:
            team_side = team_of.get(inj.get("team"))
            if team_side and str(inj.get("status") or "").lower() in NEWS_STATUSES \
                    and float(inj.get("hours_old") or 99) <= NEWS_HOURS:
                hurt.setdefault(team_side, []).append(inj)
        if len(hurt) == 1:
            injured = next(iter(hurt))
            pick = sides[other(injured)]
            inj = hurt[injured][0]
            reported = clv.parse_start(inj.get("reported_utc"))
            ok, moved = clv.unpriced(entry, pick["side"], reported, pick.get("novig_pct")) if reported else (False, "")
            if ok:
                out.append(("news", pick, f"{inj.get('team')} {inj.get('player')} {inj.get('status')}, "
                                          f"{inj.get('hours_old')}h ago; price moved {moved:+.1f}"))

        for r in (h, a):
            if r.get("sharp_pct") is not None and r.get("novig_pct") is not None:
                gap = float(r["sharp_pct"]) - float(r["novig_pct"])
                if gap >= SHARP_GAP_PTS:
                    out.append(("sharp", r, f"Pinnacle {r['sharp_pct']}% vs DraftKings {r['novig_pct']}%"))

        if entry:
            for r in (h, a):
                then = clv.price_at(entry, r["side"], now - timedelta(minutes=STEAM_MINUTES))
                if then is not None and r.get("novig_pct") is not None and float(r["novig_pct"]) - then >= STEAM_PTS:
                    out.append(("steam", r, f"{then:.1f}% -> {r['novig_pct']}% in the last hour"))
    return out


def run(now=None, path=None, candidates=None, lines_path=None):
    """Record every new decision. Returns the rows written."""
    now = now or datetime.now(timezone.utc)
    clv = _load("ace_clv_b", "ace-clv.py")
    doc = _read(candidates or AGENT / "data" / "candidates.json", {})
    rows = doc.get("candidates") if isinstance(doc, dict) else (doc or [])
    lines = _read(lines_path or AGENT / "state" / "lines.json", {})
    have = {(p.get("strategy"), p.get("key")) for p in picks(path)}
    written = []
    path = Path(path or PICKS)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for strategy, r, why in decide(rows or [], lines, now, clv):
            key = clv.game_key(r.get("sport"), r.get("match"), r.get("starts_utc"))
            if (strategy, key) in have:
                continue
            have.add((strategy, key))
            row = {"utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "strategy": strategy, "key": key,
                   "sport": r.get("sport"), "event_id": r.get("event_id"), "match": r.get("match"),
                   "starts_utc": r.get("starts_utc"), "selection": r.get("selection"),
                   "side": r.get("side"), "price": r.get("price"), "novig_pct": r.get("novig_pct"),
                   "sharp_pct": r.get("sharp_pct"), "stake": STAKE, "why": why}
            f.write(json.dumps(row) + "\n")
            written.append(row)
    return written


def score(rows, lines, results, clv, book):
    """One strategy's record: bets, settled, won, profit, CLV."""
    out = {"bets": len(rows), "settled": 0, "won": 0, "profit": 0.0,
           "clv": [], "sharp_clv": [], "beat_close": 0}
    for p in rows:
        entry = lines.get(p.get("key"))
        if entry and clv.parse_start(entry.get("start_utc")) and clv.parse_start(entry["start_utc"]) <= datetime.now(timezone.utc):
            c = clv.clv_at(p.get("price"), ((entry.get("last") or {}).get(f"novig_{p['side']}_pct")))
            if c is not None:
                out["clv"].append(c)
                out["beat_close"] += c > 0
            sc = clv.clv_at(p.get("price"), clv.sharp_close(entry, p["side"]))
            if sc is not None:
                out["sharp_clv"].append(sc)
        res = results.get(f"{p.get('sport')}|{p.get('event_id')}")
        if res and res.get("winner") in ("home", "away", "tie"):
            out["settled"] += 1
            if res["winner"] == p["side"]:
                out["won"] += 1
                out["profit"] += float(p.get("stake") or STAKE) * (book.decimal_odds(p.get("price")) - 1)
            elif res["winner"] != "tie":
                out["profit"] -= float(p.get("stake") or STAKE)
    n = len(out["clv"])
    out["avg_clv_pct"] = round(sum(out["clv"]) / n, 2) if n else None
    out["avg_sharp_clv_pct"] = round(sum(out["sharp_clv"]) / len(out["sharp_clv"]), 2) if out["sharp_clv"] else None
    out["beat_close_pct"] = round(out["beat_close"] / n * 100, 1) if n else None
    out["graded_on_close"] = n
    out["profit"] = round(out["profit"], 2)
    del out["clv"], out["sharp_clv"], out["beat_close"]
    return out


def report(path=None):
    clv = _load("ace_clv_r", "ace-clv.py")
    book = _load("ace_book_r", "ace-book.py")
    book.STATE = AGENT / "state"
    lines = _read(AGENT / "state" / "lines.json", {})
    results = _read(AGENT / "state" / "results.json", {})
    by = {s: [] for s in STRATEGIES}
    for p in picks(path):
        by.setdefault(p.get("strategy"), []).append(p)
    out = {s: score(rows, lines, results, clv, book) for s, rows in by.items()}
    ace = [dict(b, key=clv.game_key(b.get("sport"), b.get("match"), b.get("starts_utc")))
           for b in book.events(AGENT / "state" / "bets.jsonl")
           if b.get("kind") == "bet" and b.get("side") in ("home", "away")]
    out["ace"] = score(ace, lines, results, clv, book)
    return out


def report_lines(r):
    out = ["No-AI bettors beside Ace - flat $100, same games, same closes. CLV is the "
           "price taken valued at the closing no-vig chance; positive beats the close.",
           f"  {'strategy':<10} {'bets':>5} {'settled':>8} {'won':>4} {'profit':>9} "
           f"{'CLV (DK)':>9} {'CLV (Pin)':>10} {'beat close':>11}"]
    for name in ("ace",) + STRATEGIES:
        s = r.get(name) or {}
        f = lambda v, suf="": "-" if v is None else f"{v:+.2f}{suf}"
        out.append(f"  {name:<10} {s.get('bets', 0):>5} {s.get('settled', 0):>8} {s.get('won', 0):>4} "
                   f"{s.get('profit', 0):>+9.2f} {f(s.get('avg_clv_pct'), '%'):>9} "
                   f"{f(s.get('avg_sharp_clv_pct'), '%'):>10} "
                   f"{('-' if s.get('beat_close_pct') is None else str(s['beat_close_pct']) + '%'):>11}")
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="The no-AI bettors beside Ace")
    ap.add_argument("cmd", nargs="?", default="report", choices=["run", "report"])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    cmd, argv = a.cmd, (["--json"] if a.json else [])
    if cmd == "run":
        for row in run():
            print(f"{row['strategy']}: {row['selection']} ({row['match']}) {row['price']} - {row['why']}")
        return 0
    if cmd == "report":
        r = report()
        print(json.dumps(r, indent=1) if "--json" in argv else "\n".join(report_lines(r)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
