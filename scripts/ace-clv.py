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
# Every price seen is kept, not only the first and last (2026-10-01): "has
# DraftKings moved since the news?" needs the price from before the news,
# and the close is now polled every 5 minutes in the last two hours. 400 is
# 30 hours of half-hourly prices plus two hours of 5-minute ones, with room.
HISTORY_CAP = 400
# How far a side's no-vig chance may have risen since a piece of news and
# the news still count as unpriced. ~2 points is 8-10 cents on a moneyline
# near even - the move a book makes when it has reacted.
MOVED_MAX_PTS = 2.0


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
        sharp = c.get("sharp") or {}
        if sharp.get("novig_home_pct") is not None:
            # Pinnacle's, when ace-sharp.py had one: the sharper close.
            seen.update(sharp_home_pct=sharp["novig_home_pct"], sharp_away_pct=sharp["novig_away_pct"],
                        sharp_at=sharp.get("at"))
        entry = lines.get(key) or {
            "sport": c.get("sport"), "match": match, "start_utc": c.get("start_utc"),
            "home": (c.get("home") or {}).get("abbr"), "away": (c.get("away") or {}).get("abbr"),
            "first": seen, "observations": 0}
        entry["last"] = seen
        entry["observations"] = int(entry.get("observations") or 0) + 1
        hist = entry.get("history") or [entry["first"]]
        if hist[-1] != seen:
            hist.append(seen)
        entry["history"] = hist[-HISTORY_CAP:]
        for k in ("event_id",):
            if c.get(k) is not None:
                entry[k] = c.get(k)
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


def price_at(entry, side, at):
    """The side's no-vig chance at the last price seen at or before `at`
    (a datetime), or None when no price had been seen yet."""
    best = None
    for snap in (entry or {}).get("history") or [(entry or {}).get("first") or {}]:
        when = parse_start(snap.get("at"))
        if when is None or when > at or snap.get(f"novig_{side}_pct") is None:
            continue
        if best is None or when >= parse_start(best.get("at")):
            best = snap
    return None if best is None else float(best[f"novig_{side}_pct"])


def unpriced(entry, side, reported, now_pct):
    """(True, how far it moved) when the side's price has moved less than
    MOVED_MAX_PTS toward it since `reported` (a datetime) - the news may not
    be in the price yet - or (False, why). Nobody saw a price before the
    news: (False, ...), because nothing then shows it is unpriced.

    Ace's bet and the no-AI news bettor both ask this, so it is answered
    once, here, where the price history lives."""
    before = price_at(entry, side, reported)
    if before is None:
        return False, "no price was seen before the news, so nothing shows it is still unpriced"
    if now_pct is None:
        return False, "no current price"
    moved = round(float(now_pct) - before, 1)
    if moved >= MOVED_MAX_PTS:
        return False, (f"the price has already moved {moved:+.1f} pts toward this side since the "
                       f"news ({before:.1f}% -> {float(now_pct):.1f}%) - the market has it")
    return True, moved


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


def sharp_close(entry, side):
    """Pinnacle's last no-vig chance for the side before the start, or None."""
    for snap in reversed((entry or {}).get("history") or []):
        if snap.get(f"sharp_{side}_pct") is not None:
            return float(snap[f"sharp_{side}_pct"])
    return None


def clv_at(price, close_pct):
    """The price taken, valued at a closing chance: close x decimal - 1, in %."""
    d = decimal_odds(price)
    if d is None or close_pct is None:
        return None
    return round((float(close_pct) / 100 * d - 1) * 100, 1)


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
           "clv_pct": None, "moved": None, "toward": None,
           "sharp_close_pct": sharp_close(entry, side), "sharp_clv_pct": None}
    if then_pct is not None:
        out["moved"] = round(close_pct - float(then_pct), 1)
    if judgement.get("status") == "bet":
        out["clv_pct"] = clv_at(judgement.get("price"), close_pct)
        out["sharp_clv_pct"] = clv_at(judgement.get("price"), out["sharp_close_pct"])
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


def calibration(blind_path=None, results_path=None, lines_path=None):
    """Are Ace's BLIND estimates better forecasts than the market's own
    numbers? Brier score - the mean squared gap between a chance and what
    happened, 0 is perfect, 0.25 is a coin flip - over every finished game he
    estimated before seeing a price, against the same games' DraftKings
    no-vig chance at the moment he estimated, at the close, and Pinnacle's
    close where there is one. The review's first gate: if his blind number
    cannot beat the market's, no bar or staking rule turns it into money."""
    blind = {}
    try:
        text = Path(blind_path or STATE / "blind.jsonl").read_text()
    except FileNotFoundError:
        text = ""
    for line in text.splitlines():
        try:
            j = json.loads(line)
        except Exception:
            continue
        start, when = parse_start(j.get("starts_utc")), parse_start(j.get("utc"))
        if start and when and when < start:
            blind[j.get("key")] = j                    # the latest before the start
    results = _read(results_path or STATE / "results.json", {})
    lines = _read(lines_path or LINES, {})
    rows = []
    for k, j in blind.items():
        res = results.get(f"{j.get('sport')}|{j.get('event_id')}") or {}
        if res.get("winner") not in ("home", "away"):
            continue
        y = 1.0 if res["winner"] == "home" else 0.0
        entry = lines.get(k)
        rows.append({"y": y, "blind": float(j["home_pct"]) / 100,
                     "then": (price_at(entry, "home", parse_start(j["utc"])) if entry else None),
                     "close": ((entry or {}).get("last") or {}).get("novig_home_pct"),
                     "sharp": sharp_close(entry, "home")})

    def brier(key):
        xs = [(r[key] if key == "blind" else (r[key] / 100 if r[key] is not None else None), r["y"]) for r in rows]
        xs = [(p, y) for p, y in xs if p is not None]
        return (round(sum((p - y) ** 2 for p, y in xs) / len(xs), 4), len(xs)) if xs else (None, 0)

    out = {"games": len(rows)}
    for k in ("blind", "then", "close", "sharp"):
        out[f"brier_{k}"], out[f"n_{k}"] = brier(k)
    paired = [r for r in rows if r["then"] is not None]
    out["paired"] = len(paired)
    if paired:
        out["blind_vs_market_then"] = round(
            sum((r["blind"] - r["y"]) ** 2 - (r["then"] / 100 - r["y"]) ** 2 for r in paired) / len(paired), 4)
    return out


def calibration_lines(c):
    if not c["games"]:
        return ["Blind estimates: none graded yet - a game counts once it is final."]
    out = [f"Blind estimates graded: {c['games']} finished games (Brier score: lower is better, 0.25 is a coin flip)",
           f"  Ace, blind:                 {c['brier_blind']}"]
    for k, label in (("then", "DraftKings when he estimated"), ("close", "DraftKings close"),
                     ("sharp", "Pinnacle close")):
        if c[f"brier_{k}"] is not None:
            out.append(f"  {label + ':':<28}{c[f'brier_{k}']}   ({c[f'n_{k}']} games)")
    if c.get("blind_vs_market_then") is not None:
        d = c["blind_vs_market_then"]
        out.append(f"  On the same {c['paired']} games Ace is {'WORSE' if d > 0 else 'better'} than the market "
                   f"by {abs(d):.4f}. The preregistered gate is 300 games.")
    return out


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
    r["calibration"] = calibration()
    print(json.dumps(r, indent=1) if "--json" in argv
          else "\n".join(lines_of(r) + [""] + calibration_lines(r["calibration"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
