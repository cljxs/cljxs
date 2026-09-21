#!/usr/bin/env python3
"""
props-forecast.py — player receiving-yard forecasts, and the scorekeeping
that says whether they are any good.

    props-forecast.py forecast 401872947     write forecasts for one game
    props-forecast.py grade                  score the ones whose game is over
    props-forecast.py calibration            when it says 70%, does it happen?

NO MODEL IS CALLED. This is arithmetic over ESPN's free game logs: resample a
player's own past games, count how often the threshold is cleared. Fury's
cycle wakes no model either, and for the same reason - there is no judgement
here, only counting.

IT DOES NOT PRICE ANYTHING AND IT IS NOT A BET. There are no odds in this
file. A forecast is a probability; whether it is worth backing needs a price,
and comparing a number you computed to a line you did not is the exact trap
written into the top of Ace's instructions - a projection was deleted from
that agent because every report ended up reasoning "mine says 58, the line
says 54, that's value". This exists to be GRADED first. If the calibration
report says it is honest, that is when a price becomes interesting.

WHAT THE NUMBER ASSUMES. A bootstrap over a player's own games assumes his
role has not changed. It has no opinion about the opponent, the weather, or
who else is injured - all of which the book has priced. That is why every
forecast carries its sample size and which seasons it came from: a number
built on three games is not the same claim as one built on nineteen, and
printing them identically is how two different running backs end up quoted at
27.5% and 27.4%.

Standard library only.
"""

import json
import os
import random
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
STORE = ROOT / "agents" / "ace" / "data" / "forecasts.json"

API = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl"
LOGS = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes"
UA = "Mozilla/5.0 (compatible; props-forecast/1.0)"

# Printed with every forecast. It is the claim this script is allowed to make
# and the one it is not, and it lives here so a test can check the script says
# it rather than merely that the words appear somewhere in the file.
NOT_A_BET = ("NOT A BET and UNPRICED - there are no odds in this file. "
             "Grade it after the game:\n"
             "  python3 scripts/props-forecast.py grade")

DRAWS = 10_000
THRESHOLDS = (40, 50, 60, 70)     # receiving yards
POSITIONS = ("WR", "TE", "RB")
PER_TEAM = 6                      # deepest part of a depth chart worth pricing

# Below this many games there is no distribution to resample, only a rumour.
# Week 3 gives two games; the fix is prior seasons, not a confident number off
# a sample of two.
MIN_GAMES = 8
SEASONS_BACK = 1                  # current season plus this many previous


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30,
                                context=ssl.create_default_context()) as r:
        return json.load(r)


def receiving_yards(athlete_id, season=None):
    """[(season, yards)] from a game log, newest season first."""
    url = f"{LOGS}/{athlete_id}/gamelog" + (f"?season={season}" if season else "")
    try:
        log = get(url)
    except Exception:
        return []
    labels = [str(x).upper() for x in (log.get("labels") or [])]
    # REC, TGTS, YDS, ... - the receiving YDS is the first one, and taking
    # "YDS" blindly finds the rushing column on a running back.
    try:
        idx = labels.index("YDS")
    except ValueError:
        return []
    out = []
    for st in (log.get("seasonTypes") or []):
        name = str(st.get("displayName") or "")
        for cat in (st.get("categories") or []):
            for ev in (cat.get("events") or []):
                stats = ev.get("stats") or []
                if idx >= len(stats):
                    continue
                try:
                    out.append((name, float(stats[idx])))
                except (TypeError, ValueError):
                    continue
    return out


def sample_for(athlete_id, now=None):
    """(yards, seasons) - the games this forecast will be built on."""
    now = now or datetime.now(timezone.utc)
    games = receiving_yards(athlete_id)
    for back in range(1, SEASONS_BACK + 1):
        games += receiving_yards(athlete_id, now.year - back)
    return [y for _s, y in games], sorted({s for s, _y in games})


def simulate(sample, thresholds=THRESHOLDS, draws=DRAWS, seed=None):
    """P(yards >= t) for each threshold, by resampling the player's own games.

    A bootstrap, not a fitted curve: the only shape claimed is the one he has
    actually produced. Seeded so a forecast can be reproduced from its record -
    an unrepeatable number cannot be audited after the fact.
    """
    rng = random.Random(seed)
    if not sample:
        return {}
    hits = {t: 0 for t in thresholds}
    for _ in range(draws):
        y = rng.choice(sample)
        for t in thresholds:
            if y >= t:
                hits[t] += 1
    return {t: round(100.0 * n / draws, 1) for t, n in hits.items()}


def is_coarse(probs, thresholds=THRESHOLDS):
    """Did two thresholds come back identical?

    A bootstrap cannot produce a yardage the player has never produced, so
    thresholds with no observed game between them are indistinguishable. The
    number is real; the resolution it appears to have is not.
    """
    if not probs:
        return False
    return len(set(probs.values())) < len(thresholds)


def players_in(event_id):
    """[(athlete_id, name, position, team)] for both sides of a fixture."""
    summary = get(f"{API}/summary?event={event_id}")
    header = (summary.get("header") or {})
    comps = ((header.get("competitions") or [{}])[0].get("competitors") or [])
    out = []
    for c in comps:
        team = c.get("team") or {}
        tid, abbr = team.get("id"), team.get("abbreviation")
        if not tid:
            continue
        try:
            roster = get(f"{API}/teams/{tid}/roster")
        except Exception:
            continue
        picked = 0
        for group in (roster.get("athletes") or []):
            # Only players who might take the field. injuredReserveOrOut,
            # suspended and practiceSquad are separate groups and stay out.
            if isinstance(group, dict) and group.get("position") not in (
                    "offense",):
                continue
            for a in (group.get("items") if isinstance(group, dict) else [group]):
                pos = ((a.get("position") or {}).get("abbreviation") or "")
                if pos in POSITIONS and picked < PER_TEAM:
                    out.append((str(a.get("id")), a.get("displayName"), pos, abbr))
                    picked += 1
    return out


def load():
    try:
        return json.loads(STORE.read_text())
    except Exception:
        return {"forecasts": []}


def save(data):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, indent=1) + "\n")


def cmd_forecast(event_id):
    try:
        summary = get(f"{API}/summary?event={event_id}")
    except Exception as exc:
        print(f"cannot read event {event_id}: {exc}", file=sys.stderr)
        return 1
    header = summary.get("header") or {}
    comp = ((header.get("competitions") or [{}])[0])
    fixture = " @ ".join(reversed([
        ((c.get("team") or {}).get("abbreviation") or "?")
        for c in (comp.get("competitors") or [])]))
    kickoff = comp.get("date") or header.get("date")

    data = load()
    made = thin = 0
    for aid, name, pos, team in players_in(event_id):
        sample, seasons = sample_for(aid)
        if len(sample) < MIN_GAMES:
            # Named, not silently dropped: "no forecast" and "a forecast from
            # three games" are different, and only one of them is honest.
            print(f"  {name:<24} {pos:<3} {team:<4} SKIPPED - {len(sample)} game(s), "
                  f"need {MIN_GAMES}")
            thin += 1
            continue
        seed = f"{event_id}-{aid}"
        probs = simulate(sample, seed=seed)
        avg = sum(sample) / len(sample)
        # A bootstrap cannot produce a yardage the player has never produced,
        # so two thresholds with no observed game between them come back
        # identical. That is the method being honest, not a bug - but printed
        # without comment it reads as one, and it is the sharpest limit on how
        # much this number is worth. It is the thing to fix next.
        coarse = is_coarse(probs)
        row = {
            "event_id": str(event_id), "fixture": fixture, "kickoff_utc": kickoff,
            "athlete_id": aid, "player": name, "position": pos, "team": team,
            "market": "receiving_yards",
            "probabilities": {str(t): p for t, p in probs.items()},
            "sample_games": len(sample), "sample_seasons": seasons,
            "sample_mean": round(avg, 1),
            "sample_low": min(sample), "sample_high": max(sample),
            "sample_distinct": len(set(sample)),
            "coarse": coarse,
            "draws": DRAWS, "seed": seed,
            "forecast_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "actual_yards": None, "graded_utc": None,
        }
        data["forecasts"] = [f for f in data["forecasts"]
                             if not (f["event_id"] == row["event_id"]
                                     and f["athlete_id"] == row["athlete_id"])]
        data["forecasts"].append(row)
        made += 1
        line = "  ".join(f"{t}+: {probs[t]:>5.1f}%" for t in THRESHOLDS)
        note = "  COARSE" if coarse else ""
        print(f"  {name:<24} {pos:<3} {team:<4} {line}   "
              f"(mean {avg:.1f}, {len(sample)} games){note}")

    save(data)
    rough = sum(1 for f in data["forecasts"]
                if f["event_id"] == str(event_id) and f.get("coarse"))
    print(f"\n{made} forecast(s) written, {thin} skipped for a thin sample.")
    if rough:
        print(f"{rough} marked COARSE: two thresholds came back identical because "
              f"the player has\nno game between them. The number is real, its "
              f"resolution is not - treat those\nas the weakest rows here.")
    print(NOT_A_BET)
    return 0 if made else 1


def actual_receiving_yards(event_id, athlete_id):
    """Yards from a finished game's box score, or None if it is not final.

    None is not zero. A player who did not play and a player held to nothing
    look identical in a total, and grading a forecast against a game that has
    not finished would score it against a scoreline still moving.
    """
    try:
        summary = get(f"{API}/summary?event={event_id}")
    except Exception:
        return None, "could not read the box score"
    status = ((((summary.get("header") or {}).get("competitions") or [{}])[0]
               .get("status") or {}).get("type") or {}).get("name")
    if status != "STATUS_FINAL":
        return None, f"not final ({status})"

    for team in ((summary.get("boxscore") or {}).get("players") or []):
        for cat in (team.get("statistics") or []):
            if str(cat.get("name") or "").lower() != "receiving":
                continue
            labels = [str(x).upper() for x in (cat.get("labels") or [])]
            try:
                idx = labels.index("YDS")
            except ValueError:
                continue
            for a in (cat.get("athletes") or []):
                if str((a.get("athlete") or {}).get("id")) != str(athlete_id):
                    continue
                stats = a.get("stats") or []
                if idx >= len(stats):
                    continue
                try:
                    return float(stats[idx]), "final"
                except (TypeError, ValueError):
                    return None, "unreadable stat line"
    # Final, and he is not in the receiving table: he caught nothing, or did
    # not play. Those are different and the box score does not say which.
    return None, "final, no receiving line (did not play, or no targets)"


def cmd_grade():
    data = load()
    pending = [f for f in data["forecasts"] if f.get("actual_yards") is None
               and f.get("graded_utc") is None]
    if not pending:
        print("nothing waiting to be graded.")
        return 0

    graded = skipped = 0
    for f in pending:
        yards, why = actual_receiving_yards(f["event_id"], f["athlete_id"])
        if yards is None:
            print(f"  {f['player']:<24} {why}")
            if why.startswith("final"):
                # Record that it was looked at, so it is not retried forever.
                f["graded_utc"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S")
                f["grade_note"] = why
                graded += 1
            else:
                skipped += 1
            continue
        f["actual_yards"] = yards
        f["graded_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        hits = {t: (yards >= float(t)) for t in f["probabilities"]}
        f["hits"] = {t: bool(v) for t, v in hits.items()}
        graded += 1
        marks = "  ".join(
            f"{t}+ {'HIT ' if hits[t] else 'miss'} ({f['probabilities'][t]}%)"
            for t in sorted(f["probabilities"], key=int))
        print(f"  {f['player']:<24} {yards:>5.0f} yds   {marks}")

    save(data)
    print(f"\n{graded} graded, {skipped} still waiting on a final score.")
    return 0


def buckets(forecasts, width=10):
    """Forecasts grouped by what they predicted, with what actually happened.

    The only question that matters: when it said 70%, did it happen about 70%
    of the time? A model can be sharp and wrong; this catches wrong.
    """
    out = {}
    for f in forecasts:
        if f.get("actual_yards") is None:
            continue
        for t, pct in (f.get("probabilities") or {}).items():
            lo = int(float(pct) // width) * width
            b = out.setdefault(lo, {"n": 0, "hits": 0, "predicted": 0.0})
            b["n"] += 1
            b["predicted"] += float(pct)
            if float(f["actual_yards"]) >= float(t):
                b["hits"] += 1
    for b in out.values():
        b["predicted"] = round(b["predicted"] / b["n"], 1)
        b["observed"] = round(100.0 * b["hits"] / b["n"], 1)
        b["gap"] = round(b["observed"] - b["predicted"], 1)
    return dict(sorted(out.items()))


def cmd_calibration():
    data = load()
    done = [f for f in data["forecasts"] if f.get("actual_yards") is not None]
    if not done:
        print("nothing graded yet. Forecast a game, wait for it to finish, "
              "then:\n  python3 scripts/props-forecast.py grade")
        return 1

    rows = buckets(done)
    n = sum(b["n"] for b in rows.values())
    print(f"{len(done)} graded forecast(s), {n} threshold call(s)\n")
    print(f"  {'said':<10}{'predicted':>10}{'happened':>10}{'gap':>8}{'calls':>7}")
    print("  " + "-" * 45)
    for lo, b in rows.items():
        print(f"  {lo}-{lo + 9}%{'':<3}{b['predicted']:>9.1f}%{b['observed']:>9.1f}%"
              f"{b['gap']:>+8.1f}{b['n']:>7}")
    print(f"\n  A row whose gap is near zero is a forecast that means what it "
          f"says.\n  Consistently positive means it is too cautious; negative "
          f"means too\n  confident, which is the one that costs money.")
    if n < 50:
        print(f"\n  {n} calls is not enough to conclude anything. This needs "
              f"weeks, not\n  a night - a coin lands 7 of 10 often enough that "
              f"it means nothing.")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "forecast":
        if len(sys.argv) < 3:
            print("usage: props-forecast.py forecast <event_id>\n"
                  "  find one with: curl -s "
                  "'https://site.web.api.espn.com/apis/site/v2/sports/football/"
                  "nfl/scoreboard' | python3 -m json.tool | grep -A2 shortName",
                  file=sys.stderr)
            return 2
        return cmd_forecast(sys.argv[2])
    if cmd == "grade":
        return cmd_grade()
    if cmd == "calibration":
        return cmd_calibration()
    print("usage: props-forecast.py forecast <event_id> | grade | calibration",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
