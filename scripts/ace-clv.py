#!/usr/bin/env python3
"""
ace-clv.py — did the market move Ace's way? Closing line value, in plain code.

    python3 scripts/ace-clv.py               the report: every started game Ace judged
    python3 scripts/ace-clv.py --json        the same, for the Deck or the GM

WHY. Wins and losses take thousands of bets to separate skill from luck.
The closing line - the last price before kickoff - is the market's best
estimate, and the research on sports betting agrees that beating it is the
most reliable sign of a real edge (researched 2026-09-26). So Ace is graded
on two things, both measurable within days:

  * BETS - closing line value. The price Ace took, valued at the closing
    no-vig chance: close_pct x decimal_odds - 1. Positive means the price he
    took was better than the market's final word.
  * ESTIMATES - including passes. Did the line move toward Ace's number
    between when he judged it and kickoff? An estimate above the market that
    the market then rose toward was information; one it moved away from was
    noise. A game swept with no estimate has nothing to grade.

WHERE THE NUMBERS COME FROM. ace-fetch.py runs every 30 minutes until 23:30
ET and calls record_lines() here: each game's moneyline is kept until it
starts, so the last one kept is the close (within half an hour of it - the
report says how close). ace-judge.py calls log_judgements() here every time
Ace records a verdict, because the ledger itself is replaced every cycle and
the estimate would be gone before its game closed. The odds are DraftKings'
through ESPN, the one book Ace sees; the close is that same book's.

Nothing here wakes a model or spends anything.

Standard library only.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
STATE = ROOT / "agents" / "ace" / "state"
LINES = STATE / "lines.json"
JOURNAL = STATE / "judgements.jsonl"

KEEP_DAYS = 30          # how long a game's line history is kept after it starts
STAMP = "%Y-%m-%dT%H:%M:%SZ"


def utc_now():
    return datetime.now(timezone.utc)


def parse_start(s):
    """ESPN's "2026-09-26T19:30Z" (or with seconds) as a UTC datetime, or None."""
    for fmt in ("%Y-%m-%dT%H:%MZ", STAMP):
        try:
            return datetime.strptime(str(s or ""), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def game_key(sport, match, start_utc):
    """The one join between a fetched game and a judged row: both carry the
    sport, ESPN's short name and the start time."""
    return f"{sport}|{match}|{start_utc}"


# ------------------------------------------------------------------ writing

def _read(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def record_lines(contexts, now=None, path=None):
    """Keep each game's latest moneyline until it starts. `contexts` are the
    context dicts ace-fetch.py just wrote. A game that has started is never
    updated again, so what it holds is the last price before kickoff."""
    now = now or utc_now()
    path = Path(path or LINES)
    lines = _read(path, {})
    for c in contexts:
        odds = c.get("odds") or {}
        start = parse_start(c.get("start_utc"))
        if start is None or start <= now:
            continue
        if odds.get("novig_home_pct") is None or odds.get("novig_away_pct") is None:
            continue
        match = c.get("short") or c.get("match")
        key = game_key(c.get("sport"), match, c.get("start_utc"))
        seen = {"at": now.strftime(STAMP),
                "moneyline_home": odds.get("moneyline_home"),
                "moneyline_away": odds.get("moneyline_away"),
                "novig_home_pct": odds.get("novig_home_pct"),
                "novig_away_pct": odds.get("novig_away_pct")}
        entry = lines.get(key) or {
            "sport": c.get("sport"), "match": match, "start_utc": c.get("start_utc"),
            "home": (c.get("home") or {}).get("abbr"), "away": (c.get("away") or {}).get("abbr"),
            "first": seen, "observations": 0}
        entry["last"] = seen
        entry["observations"] = int(entry.get("observations") or 0) + 1
        lines[key] = entry
    cutoff = now - timedelta(days=KEEP_DAYS)
    lines = {k: v for k, v in lines.items()
             if (parse_start(v.get("start_utc")) or now) > cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(lines, indent=1) + "\n")
    tmp.replace(path)
    return lines


def log_judgements(rows, now=None, path=None):
    """Append every verdict Ace just recorded, with the price it was judged at.
    The ledger is replaced each cycle; this is the record that outlives it."""
    now = now or utc_now()
    path = Path(path or JOURNAL)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps({
                "judged_utc": now.strftime(STAMP),
                "key": game_key(r.get("sport"), r.get("match"), r.get("starts_utc")),
                "selection": r.get("selection"), "match": r.get("match"),
                "status": r.get("status"), "price": r.get("price"),
                "novig_pct": r.get("novig_pct"), "my_pct": r.get("my_pct"),
                "stake": r.get("stake")}) + "\n")


# ------------------------------------------------------------------ grading

def decimal_odds(american):
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a >= 100:
        return 1 + a / 100
    if a <= -100:
        return 1 + 100 / -a
    return None


def side_of(selection, entry):
    """"IOWA ML" -> "away" when IOWA is the away team, else "home", or None."""
    team = str(selection or "").rsplit(" ML", 1)[0].strip()
    if team and team == entry.get("home"):
        return "home"
    if team and team == entry.get("away"):
        return "away"
    return None


def grade(judgement, entry, now=None):
    """One judged row against its game's closing line. None if the game has not
    started, or the row cannot be joined to a price."""
    now = now or utc_now()
    start = parse_start(entry.get("start_utc"))
    if start is None or start > now:
        return None
    side = side_of(judgement.get("selection"), entry)
    last = entry.get("last") or {}
    if side is None or last.get(f"novig_{side}_pct") is None:
        return None
    judged_at = parse_start(judgement.get("judged_utc"))
    close_at = parse_start(last.get("at"))
    close_pct = float(last[f"novig_{side}_pct"])
    then_pct = judgement.get("novig_pct")
    out = {"match": judgement.get("match"), "selection": judgement.get("selection"),
           "status": judgement.get("status"), "start_utc": entry.get("start_utc"),
           "price": judgement.get("price"), "then_pct": then_pct,
           "close_price": last.get(f"moneyline_{side}"), "close_pct": close_pct,
           "my_pct": judgement.get("my_pct"),
           "close_minutes_before_start": (round((start - close_at).total_seconds() / 60)
                                          if close_at else None),
           # A close seen before the verdict says nothing about the verdict.
           "later_price": bool(judged_at and close_at and close_at > judged_at),
           "clv_pct": None, "moved": None, "toward": None}
    if then_pct is not None:
        out["moved"] = round(close_pct - float(then_pct), 1)
    if judgement.get("status") == "bet":
        d = decimal_odds(judgement.get("price"))
        if d:
            out["clv_pct"] = round((close_pct / 100 * d - 1) * 100, 1)
    if out["my_pct"] is not None and then_pct is not None and out["later_price"]:
        lean = float(out["my_pct"]) - float(then_pct)
        if lean:
            out["toward"] = round(out["moved"] * (1 if lean > 0 else -1), 1)
    return out


def latest_judgements(path=None):
    """The last verdict per game side: Ace may judge a row twice in a cycle,
    or again in a later one, and only his latest word is his word."""
    last = {}
    try:
        text = Path(path or JOURNAL).read_text()
    except FileNotFoundError:
        return []
    for line in text.splitlines():
        try:
            j = json.loads(line)
        except Exception:
            continue
        last[(j.get("key"), j.get("selection"))] = j
    return list(last.values())


def report(now=None, lines_path=None, journal_path=None):
    lines = _read(lines_path or LINES, {})
    graded = []
    for j in latest_judgements(journal_path):
        entry = lines.get(j.get("key"))
        g = grade(j, entry, now) if entry else None
        if g:
            graded.append(g)
    graded.sort(key=lambda g: g["start_utc"] or "", reverse=True)
    bets = [g for g in graded if g["status"] == "bet" and g["clv_pct"] is not None]
    leans = [g for g in graded if g["toward"] is not None]
    return {
        "graded": graded,
        "bets": len(bets),
        "bet_avg_clv_pct": round(sum(g["clv_pct"] for g in bets) / len(bets), 2) if bets else None,
        "bets_beat_close": sum(1 for g in bets if g["clv_pct"] > 0),
        "estimates": len(leans),
        "estimates_toward": sum(1 for g in leans if g["toward"] > 0),
        "estimates_away": sum(1 for g in leans if g["toward"] < 0),
        "estimates_avg_toward": round(sum(g["toward"] for g in leans) / len(leans), 2) if leans else None,
    }


def lines_of(r):
    out = ["Ace's closing line value - games that have started. The close is the last "
           "DraftKings price seen before kickoff."]
    if r["bets"]:
        out.append(f"  Bets: {r['bets']}, average CLV {r['bet_avg_clv_pct']:+.2f}%, beat the "
                   f"close {r['bets_beat_close']} of {r['bets']}")
    else:
        out.append("  Bets: none graded yet")
    if r["estimates"]:
        out.append(f"  Estimates: {r['estimates']} - the line moved toward Ace's number "
                   f"{r['estimates_toward']} times, away {r['estimates_away']}, flat "
                   f"{r['estimates'] - r['estimates_toward'] - r['estimates_away']}; "
                   f"average {r['estimates_avg_toward']:+.2f} pts his way")
    else:
        out.append("  Estimates: none graded yet")
    for g in r["graded"][:30]:
        price = f"{g['price']:+d}" if isinstance(g["price"], int) else str(g["price"])
        close = f"{g['close_price']:+d}" if isinstance(g["close_price"], int) else str(g["close_price"])
        then = "?" if g["then_pct"] is None else f"{g['then_pct']:.1f}%"
        bit = f"  [{g['status']}] {g['selection']} ({g['match']}) {price} {then} -> close {close} {g['close_pct']:.1f}%"
        if g["clv_pct"] is not None:
            bit += f"  CLV {g['clv_pct']:+.1f}%"
        if g["toward"] is not None:
            bit += f"  Ace {g['my_pct']:.1f}%: moved {g['toward']:+.1f} his way"
        elif g["my_pct"] is None:
            bit += "  (no estimate)"
        elif not g["later_price"]:
            bit += "  (no price seen after his verdict)"
        if g["close_minutes_before_start"] is not None and g["close_minutes_before_start"] > 60:
            bit += f"  [close seen {g['close_minutes_before_start']} min before start]"
        out.append(bit)
    if not r["graded"]:
        out.append("  Nothing to grade yet: a row is graded once its game has started and "
                   "the fetcher has seen its price.")
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    r = report()
    print(json.dumps(r, indent=1) if "--json" in argv else "\n".join(lines_of(r)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
