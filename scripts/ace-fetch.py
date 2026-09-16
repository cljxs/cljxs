#!/usr/bin/env python3
"""
ace-fetch.py — sports data fetcher for the Ace agent.

Plain code. NO AI. Pulls scoreboards and per-game context, computes every
number locally, writes JSON. Ace READS those files. If a number is not in a
data file, Ace does not cite it.

Standard library only — no pip installs, no compiler needed.
"""

import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import et_time  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
DATA = ROOT / "agents" / "ace" / "data"
CTX = DATA / "context"

# site.api.espn.com is blocked from many datacentre IPs; site.web.api serves
# the same API and is reachable.
BASE = "https://site.web.api.espn.com/apis/site/v2/sports"
UA = "Mozilla/5.0 (compatible; ace-fetch/1.0)"
TIMEOUT = 20
GAP = 0.4
DEEP_CAP = 12          # polite cap on per-game context fetches, per sport

# How many games Ace is asked to judge in one cycle, and how far ahead it looks.
#
# The fetcher used to hand over every scheduled game on both slates - about 30
# games, 60 moneyline rows. Judging them cost more tool calls than a five
# minute cycle has, and Ace timed out mid-run twice. Narrowing the field is
# mechanical work: a game starting in three days cannot be bet on information
# that does not exist yet, and one that has started cannot be bet at all. So
# the field is narrowed here and the judgement is left to Ace.
BET_WINDOW_HOURS = 14  # far enough to cover tonight's slate, not next weekend
MAX_GAMES = 8          # 8 games = 16 rows, which a cycle can actually get through

SPORTS = {
    "mlb": {"path": "baseball/mlb", "months": {4, 5, 6, 7, 8, 9, 10}},
    "nfl": {"path": "football/nfl", "months": {9, 10, 11, 12, 1, 2}},
    "nba": {"path": "basketball/nba", "months": {10, 11, 12, 1, 2, 3, 4, 5, 6}},
}


def log(m):
    print(f"[ace-fetch {datetime.now(timezone.utc):%H:%M:%S}] {m}", flush=True)


def get(url, retries=2):
    last = None
    for a in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT,
                                        context=ssl.create_default_context()) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            if a < retries:
                time.sleep(1.5 * (a + 1))
    raise last


# --- the maths that matters, done here and not by a language model ---------

def implied(ml):
    """American moneyline -> implied probability (includes the vig)."""
    if ml is None:
        return None
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    if ml == 0:
        return None
    return (-ml) / ((-ml) + 100) if ml < 0 else 100.0 / (ml + 100)


def devig(p_home, p_away):
    """Strip the vig so the two sides sum to 100%. This is the number an
    estimate must beat — not the raw implied probability."""
    if p_home is None or p_away is None:
        return None, None, None
    total = p_home + p_away
    if total <= 0:
        return None, None, None
    return p_home / total, p_away / total, (total - 1.0)


def r1(x):
    return None if x is None else round(x * 100, 1)


def in_season(now=None):
    m = (now or datetime.now(timezone.utc)).month
    return [k for k, v in SPORTS.items() if m in v["months"]]


def team_of(comp, home):
    for c in comp.get("competitors", []):
        if (c.get("homeAway") == "home") == home:
            return c
    return {}


def build_context(sport, path, event):
    """One compact context file per game. Compact matters: Ace reads these,
    and every byte it reads is fuel."""
    eid = event["id"]
    comp = event["competitions"][0]
    home, away = team_of(comp, True), team_of(comp, False)

    summary = get(f"{BASE}/{path}/summary?event={eid}")

    # odds
    pc = (summary.get("pickcenter") or comp.get("odds") or [])
    odds = {}
    if pc:
        o = pc[0]
        hm = (o.get("homeTeamOdds") or {}).get("moneyLine")
        am = (o.get("awayTeamOdds") or {}).get("moneyLine")
        ph, pa = implied(hm), implied(am)
        nh, na, vig = devig(ph, pa)
        odds = {
            "book": (o.get("provider") or {}).get("name"),
            "details": o.get("details"),
            "over_under": o.get("overUnder"),
            "spread": o.get("spread"),
            "moneyline_home": hm, "moneyline_away": am,
            "novig_home_pct": r1(nh), "novig_away_pct": r1(na),
            "vig_pct": r1(vig),
            "_note": "novig_*_pct is the real break-even. It is the ONLY number "
                     "your estimate is compared against.",
        }
        # implied_*_pct used to be written here alongside the no-vig numbers,
        # and every report built itself on the implied figure instead - it is
        # the bigger, more confident-looking number, and it sits right next to
        # the one that matters. Betting against implied means betting into the
        # vig on every wager. The raw probabilities are still computed above,
        # because vig_pct needs them; they just do not leave this function.

    # ESPN's predictor used to be written here, carrying a warning that a gap
    # between it and the line is never an edge. The warning did not work: every
    # line of the last report read "Predictor: home win 72.5% vs line implied
    # 70.4%", which is the exact comparison the warning forbade, structured as
    # the whole analysis.
    #
    # There is no case where that number may legitimately drive a decision -
    # the book has already priced public models like it - so it is not written
    # at all. A field that can only ever be misused is not context, it is bait.

    # injuries: name + status only, capped
    injuries = []
    for blk in (summary.get("injuries") or []):
        tm = (blk.get("team") or {}).get("abbreviation")
        for i in (blk.get("injuries") or [])[:6]:
            ath = i.get("athlete") or {}
            injuries.append({
                "team": tm,
                "player": ath.get("displayName"),
                "position": ((ath.get("position") or {}).get("abbreviation")),
                "status": i.get("status"),
                "date": i.get("date"),
            })

    last5 = []
    for blk in (summary.get("lastFiveGames") or []):
        tm = (blk.get("team") or {}).get("abbreviation")
        for g in (blk.get("events") or [])[:5]:
            last5.append({"team": tm, "result": g.get("gameResult"),
                          "score": g.get("score"), "opponent": (g.get("opponent") or {}).get("abbreviation")})

    ats = [{"team": (b.get("team") or {}).get("abbreviation"),
            "records": [(r.get("type"), r.get("summary")) for r in (b.get("records") or [])[:3]]}
           for b in (summary.get("againstTheSpread") or [])]

    gi = summary.get("gameInfo") or {}
    weather = gi.get("weather") or {}

    probables = []
    for p in (comp.get("probables") or []):
        ath = p.get("athlete") or {}
        probables.append({"team": (p.get("homeAway") or ""), "player": ath.get("displayName")})

    return {
        "sport": sport,
        "event_id": eid,
        "name": event.get("name"),
        "short": event.get("shortName"),
        "start_utc": event.get("date"),
        "status": comp["status"]["type"]["name"],
        "home": {"abbr": (home.get("team") or {}).get("abbreviation"),
                 "record": ((home.get("records") or [{}])[0]).get("summary")},
        "away": {"abbr": (away.get("team") or {}).get("abbreviation"),
                 "record": ((away.get("records") or [{}])[0]).get("summary")},
        "odds": odds,
        "injuries": injuries,
        "last_five": last5,
        "against_the_spread": ats,
        "venue": (gi.get("venue") or {}).get("fullName"),
        "weather": {"temp_f": weather.get("temperature"),
                    "conditions": weather.get("conditionId") and weather.get("displayValue")} if weather else {},
        "probable_pitchers": probables,
        "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }


# ------------------------------------------------------------ shadow ledger

def build_ledger(day, ctx_dir, slot):
    """Every game that could still be bet, with the mechanical verdict already
    filled in.

    The interesting output of a disciplined betting agent is mostly its passes
    - "six games, none cleared the bar" is the system working, but it is
    worthless unless you can see which six and why. So the ledger is built
    here, in plain code, from the context files this script already wrote.

    Everything except Ace's own probability estimate is decidable without a
    model: the price, the no-vig line, whether the game has started, whether a
    context file exists at all. Ace overlays its estimate and any further
    reasons in state/ledger.json; if it never does, the board still shows what
    was on the slate and what plain code already knew about it.
    """
    now = datetime.now(timezone.utc)
    games = []
    for f in sorted(ctx_dir.glob("*.json")):
        try:
            c = json.loads(f.read_text())
        except Exception:
            continue
        # Sort by kick-off and keep the nearest few that have not started.
        try:
            start = datetime.strptime(c.get("start_utc", ""), "%Y-%m-%dT%H:%MZ") \
                .replace(tzinfo=timezone.utc)
        except Exception:
            start = None
        started = str(c.get("status") or "").upper() != "STATUS_SCHEDULED"
        hours = (start - now).total_seconds() / 3600 if start else 999
        # The clock outranks the status field. ESPN reported STATUS_SCHEDULED
        # for a game whose listed start was two hours in the past, and Ace's
        # own bar requires that the game has not started - so a start time that
        # has passed disqualifies it whatever the status says.
        if started or hours <= 0 or hours > BET_WINDOW_HOURS:
            continue
        games.append((hours, f.name, c))
    games.sort(key=lambda g: (g[0], g[1]))
    dropped = max(0, len(games) - MAX_GAMES)
    games = games[:MAX_GAMES]

    rows = []
    for _, _, c in games:
        odds = c.get("odds") or {}
        nh, na = odds.get("novig_home_pct"), odds.get("novig_away_pct")
        # These two keys are the ones build_context actually writes. They were
        # read as home_ml/away_ml, which has never been a key in this file, so
        # every candidate row carried price: null - including the rows Ace was
        # told to copy verbatim into its ledger.
        for side, pct, ml in (("home", nh, odds.get("moneyline_home")),
                              ("away", na, odds.get("moneyline_away"))):
            # `home` and `away` are objects: {"abbr": "BUF", "record": "1-0"}.
            # Formatting one straight into a string produced selections named
            # "{'abbr': 'BUF', 'record': '1-0'} ML", and `selection` is the key
            # the ledger merge joins on, so nothing Ace wrote could ever match.
            t = c.get(side)
            team = (t.get("abbr") or side) if isinstance(t, dict) else (t or side)
            why = []
            if pct is None:
                why.append("no priced line in the context file")
            rows.append({
                "selection": f"{team} ML",
                "sport": c.get("sport"),
                "match": c.get("short") or c.get("match"),
                "starts_utc": c.get("start_utc"),
                "price": ml,
                "novig_pct": pct,
                "my_pct": None,
                "edge_pts": None,
                "why_not": why,
                # Nothing here is a pass yet - Ace has not looked. Saying
                # "passed" before it has would be a lie the board repeats.
                "status": "passed" if why else "unjudged",
            })
    return {
        "asof_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "day": day,
        # Filled in here so Ace never computes it. It got this wrong from a
        # UTC clock and filed a 23:31 ET run as "2026-09-16-afternoon.md".
        "slot": slot,
        "verdict": None,
        "source": "ace-fetch.py - mechanical fields only, awaiting Ace's estimates",
        "window_hours": BET_WINDOW_HOURS,
        "games_shown": len(games),
        "games_outside_window": dropped,
        "candidates": rows,
    }


def main():
    CTX.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    season = in_season(started)
    log(f"in season: {season or '(none)'}")

    slate, failures, deep_written = [], [], 0

    for sport in season:
        path = SPORTS[sport]["path"]
        try:
            board = get(f"{BASE}/{path}/scoreboard")
        except Exception as e:
            failures.append({"sport": sport, "error": str(e)[:140]})
            log(f"{sport}: scoreboard FAILED — {e}")
            continue

        events = board.get("events", [])
        scheduled = [e for e in events
                     if e["competitions"][0]["status"]["type"]["name"] == "STATUS_SCHEDULED"]
        finals = [e for e in events
                  if e["competitions"][0]["status"]["type"]["name"] == "STATUS_FINAL"]

        for e in events:
            c = e["competitions"][0]
            h, a = team_of(c, True), team_of(c, False)
            slate.append({
                "sport": sport, "event_id": e["id"], "short": e.get("shortName"),
                "start_utc": e.get("date"), "status": c["status"]["type"]["name"],
                "home": (h.get("team") or {}).get("abbreviation"),
                "away": (a.get("team") or {}).get("abbreviation"),
                "home_score": h.get("score"), "away_score": a.get("score"),
            })

        # Deep context only for games that can still be bet, capped.
        for e in scheduled[:DEEP_CAP]:
            try:
                ctx = build_context(sport, path, e)
                (CTX / f"{sport}-{e['id']}.json").write_text(json.dumps(ctx, indent=1) + "\n")
                deep_written += 1
            except Exception as exc:
                failures.append({"sport": sport, "event": e["id"], "error": str(exc)[:140]})
                log(f"{sport} {e.get('shortName')}: context FAILED — {exc}")
            time.sleep(GAP)

        log(f"{sport}: {len(events)} games ({len(scheduled)} scheduled, {len(finals)} final)")

    if not slate and failures:
        log("nothing fetched — leaving previous data files untouched")
        return 1

    # Eastern, not UTC. A 23:30 ET wake happens on the next UTC day, and
    # using that date filed a report under tomorrow's name.
    now_et = et_time.eastern_now()
    (DATA / "candidates.json").write_text(
        json.dumps(build_ledger(et_time.day(now_et), CTX,
                                et_time.slot("ace", now_et)), indent=1) + "\n")

    (DATA / "slate.json").write_text(json.dumps({
        "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
        "sports_in_season": season,
        "games": slate,
    }, indent=1) + "\n")

    # Prune context files for games no longer on the slate.
    live_ids = {f"{g['sport']}-{g['event_id']}.json" for g in slate}
    for f in CTX.glob("*.json"):
        if f.name not in live_ids:
            f.unlink(missing_ok=True)

    (DATA / "_meta.json").write_text(json.dumps({
        "asof_utc": started.strftime("%Y-%m-%d %H:%M:%S"),
        "sports_in_season": season,
        "day_et": et_time.day(now_et),
        "slot": et_time.slot("ace", now_et),
        "report_name": et_time.report_name("ace", now_et),
        "games_on_slate": len(slate),
        "context_files_written": deep_written,
        "failures": failures,
        "source": "ESPN public API (site.web.api.espn.com), no API key",
    }, indent=1) + "\n")

    log(f"ok: {len(slate)} games, {deep_written} context files, {len(failures)} failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
