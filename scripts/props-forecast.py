#!/usr/bin/env python3
"""
props-forecast.py — player prop forecasts across several markets, and the
scorekeeping that says whether they are any good.

    props-forecast.py forecast 401872947     write forecasts for one game
    props-forecast.py best 401872947         the strongest claim per player
    props-forecast.py grade                  score the ones whose game is over
    props-forecast.py calibration            when it says 70%, does it happen?
    props-forecast.py markets                what it prices, and off which stat

NO MODEL IS CALLED. This is arithmetic over ESPN's free game logs: take the
player's own past games and ask how often the bar is cleared. Fury's cycle
wakes no model either, and for the same reason - there is no judgement here,
only counting.

IT DOES NOT PRICE ANYTHING AND IT IS NOT A BET. There are no odds in this
file. A forecast is a probability; whether it is worth backing needs a price,
and comparing a number you computed to a line you did not is the exact trap
written into the top of Ace's instructions - a projection was deleted from
that agent because every report ended up reasoning "mine says 58, the line
says 54, that's value". This exists to be GRADED first. If the calibration
report says it is honest, that is when a price becomes interesting.

HOW MUCH TO TRUST ONE. Every percentage carries a 90% interval, because
"76.2% off fifteen games" and "76.2% off a hundred" are not the same claim
and printing them identically is how two different players end up quoted at
27.5% and 27.4%. The width of the bracket is the sample being honest.

WHAT THE NUMBER ASSUMES. A bootstrap over a player's own games assumes his
role has not changed. It has no opinion about the opponent, the weather, or
who else is injured - all of which the book has priced. It also assumes he
plays: every game in the sample is a game he was active for, so the number is
P(clears the bar GIVEN he takes the field), while a book prices P(plays) x
that. Where the two differ the gap looks like an enormous edge and is an
artifact, which is why every forecast carries an availability line as well as
its sample size and seasons.

A MARKET IS READ BY NAME, NEVER BY COLUMN. ESPN orders a game log's columns
by position: a tight end's first YDS is receiving, a running back's is
rushing, a quarterback's is passing. The box score then reorders them again
(TGTS is second in the log, last in the box score). Both payloads label every
column with a stat name - receivingYards, rushingAttempts - so MARKETS below
names the stat and column_of() finds it in whichever payload is in hand. This
file contains no column numbers.

Standard library only.
"""

import json
import math
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

# How many times the game log is resampled to say how firm a percentage is.
# The percentage itself is exact (see smoothed_probs) - this is only for the
# interval around it.
RESAMPLES = 400
INTERVAL_SPAN = 90                # a 90% interval: the 5th to the 95th

# Who gets forecast, and how they are ranked against each other. A
# quarterback throws thirty-five times a game and a receiver is targeted
# eight, so one list ranked by volume would give a side six quarterbacks and
# no receivers - they are different jobs and they compete in different pools.
# Each pool names the markets that measure its volume, which is why a backup
# quarterback (no passing yards) never displaces the starter.
POOLS = [
    {"name": "passer", "positions": ("QB",),
     "volume": ("passing_yards",), "per_team": 1},
    {"name": "skill", "positions": ("WR", "TE", "RB"),
     "volume": ("receiving_targets", "rushing_attempts"), "per_team": 6},
]
# Derived, not restated: a position this file forecasts is a position some
# pool claims. The two lists drifting apart would mean a player selected off
# the roster and then ranked by nothing.
POSITIONS = tuple(p for pool in POOLS for p in pool["positions"])

# Below this many games there is no distribution to resample, only a rumour.
# Week 3 gives two games; the fix is prior seasons, not a confident number off
# a sample of two.
MIN_GAMES = 8
SEASONS_BACK = 1                  # current season plus this many previous

# Every market, once: the ESPN stat name it is read from, the unit it is
# quoted in, and the bars worth asking about. Adding a market is a line here -
# the log reader, the box score reader, the forecast rows and the Deck all
# come off this table, so there is nowhere else for a second opinion to live.
MARKETS = {
    "passing_yards":     {"stat": "passingYards",      "unit": "yds",
                          "thresholds": (200, 225, 250, 275),
                          "counts": False},
    "passing_tds":       {"stat": "passingTouchdowns", "unit": "td",
                          "thresholds": (1, 2, 3),
                          "counts": True},
    "receiving_yards":   {"stat": "receivingYards",    "unit": "yds",
                          "thresholds": (40, 50, 60, 70),
                          "counts": False},
    "receptions":        {"stat": "receptions",        "unit": "rec",
                          "thresholds": (3, 4, 5, 6),
                          "counts": True},
    "receiving_targets": {"stat": "receivingTargets",  "unit": "tgts",
                          "thresholds": (4, 5, 6, 8),
                          "counts": True},
    "rushing_yards":     {"stat": "rushingYards",      "unit": "yds",
                          "thresholds": (30, 50, 70, 90),
                          "counts": False},
    "rushing_attempts":  {"stat": "rushingAttempts",   "unit": "att",
                          "thresholds": (8, 12, 15, 18),
                          "counts": True},
}

# A market belongs to a player only if he has actually done it. A wide
# receiver has a rushingYards column and it is zero every week, and a blocking
# tight end has a receptions column he clears twice a season; forecasting
# either prints "0.0%  0.0%  0.0%" and buries the rows that mean something.
# The bar is a rate, not a count - cleared in at least a quarter of his games -
# because "twice" is nothing in twenty-one games and a lot in nine. It is read
# off percentile(), so the rule and the P25-P75 on the same row cannot drift
# apart.
RELEVANT_PERCENTILE = 75

# Below this share of his team's games, the sample is a different claim from
# the one a book prices: it says what he does when he plays, and says nothing
# about whether he will. Flagged, never silently multiplied away.
AVAILABILITY_FLOOR = 0.80


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30,
                                context=ssl.create_default_context()) as r:
        return json.load(r)


def column_of(keys, stat):
    """Where a stat sits in a row of numbers, by ESPN's name for it.

    The one resolver, used on the game log's `names` and on the box score's
    `keys` alike. Both name their columns; neither orders them the same way
    twice, which is why nothing here counts columns.
    """
    try:
        return [str(k) for k in (keys or [])].index(stat)
    except ValueError:
        return None


LOG_CACHE = {}


def game_values(athlete_id, season=None):
    """{market: [(season_label, value)]} from one game log.

    One fetch fills every market at once: the log carries rushing and
    receiving columns side by side, so asking five times would be five
    identical requests. Cached for the same reason at the other end: the
    current season's log is read to rank a player and again to forecast him,
    and two fetches of the same URL are two chances to disagree about him.
    """
    key = (str(athlete_id), season)
    if key in LOG_CACHE:
        return LOG_CACHE[key]
    url = f"{LOGS}/{athlete_id}/gamelog" + (f"?season={season}" if season else "")
    try:
        log = get(url)
    except Exception:
        LOG_CACHE[key] = {}
        return {}
    LOG_CACHE[key] = values_from_log(log)
    return LOG_CACHE[key]


def values_from_log(log):
    """The parsing half of game_values, with no network in it.

    Split out so it can be run against a captured payload. Every parser bug
    in this repo was a format assumption, and the only thing that catches one
    is a real sample.
    """
    names = log.get("names") or []
    cols = {m: column_of(names, spec["stat"]) for m, spec in MARKETS.items()}
    out = {m: [] for m in MARKETS}
    for st in (log.get("seasonTypes") or []):
        label = str(st.get("displayName") or "")
        for cat in (st.get("categories") or []):
            for ev in (cat.get("events") or []):
                stats = ev.get("stats") or []
                for market, idx in cols.items():
                    if idx is None or idx >= len(stats):
                        continue
                    try:
                        out[market].append((label, float(stats[idx])))
                    except (TypeError, ValueError):
                        continue
    return out


def games_in(log):
    """How many games a log covers, whichever columns it happens to carry.

    Every market in a log has one entry per game, so the longest list is the
    game count - asked this way rather than off a named market, because a
    player with no receiving column would then look like he had played none.
    """
    return max((len(rows) for rows in log.values()), default=0)


def samples_for(athlete_id, now=None):
    """({market: [values]}, seasons, games this season).

    The current season's log is fetched once and used for both the sample and
    the availability count. Asking for it twice would be two identical
    requests and two chances for them to disagree.
    """
    now = now or datetime.now(timezone.utc)
    current = game_values(athlete_id)
    logs = [current]
    for back in range(1, SEASONS_BACK + 1):
        logs.append(game_values(athlete_id, now.year - back))
    seasons, out = set(), {m: [] for m in MARKETS}
    for log in logs:
        for market, rows in log.items():
            for label, value in rows:
                seasons.add(label)
                out[market].append(value)
    return out, sorted(seasons), games_in(current)


def is_relevant(sample, thresholds):
    """Has he done this often enough for the market to be about him?

    His 75th percentile has to reach the lowest bar: a quarter of his games
    clear it, or the market is somebody else's.
    """
    if not sample:
        return False
    p = percentile(sample, RELEVANT_PERCENTILE)
    return p is not None and p >= thresholds[0]


def bandwidth(sample):
    """How far a game is allowed to smear, in the units of the market.

    THE ONE NUMBER THAT WAS NOT INVENTED. A plain bootstrap can only produce
    a value the player has already produced, so with twenty-one games and a
    bar every twenty-five yards, two bars with no game between them come back
    identical - Stafford at 76.2% for both 200+ and 225+. The fix is to let
    each past game stand for a small neighbourhood rather than a single point,
    and that needs a width.

    A width picked by hand would be the whole answer smuggled in as a
    constant, so this is Silverman's rule: 0.9 x min(sd, IQR/1.34) x n^(-1/5),
    the standard estimate, computed from his own games. A streaky player gets
    a wide one and a metronome gets a narrow one, and nobody here chose
    either.

    Zero when his games are all identical - there is genuinely nothing to
    spread, and smoothing a point mass would invent the spread outright.
    """
    n = len(sample)
    if n < 2:
        return 0.0
    mean = sum(sample) / n
    var = sum((x - mean) ** 2 for x in sample) / (n - 1)
    sd = math.sqrt(var)
    iqr = (percentile(sample, 75) or 0.0) - (percentile(sample, 25) or 0.0)
    spread = min(sd, iqr / 1.34) if iqr > 0 else sd
    return 0.9 * spread * (n ** -0.2)


def point_prob(x, threshold, h, counts):
    """P(this one game, smeared, clears the bar).

    Exact, not sampled: a Gaussian smear has a closed form, so the whole
    Monte Carlo it replaces was 10,000 draws spent approximating a number
    arithmetic gives outright - and giving it a different answer each run.

    `counts` is what separates receptions from yards. A catch is a whole
    number: 3.4 receptions is not a thing, and a book settles 4+ on whether
    the count rounds to 4 or more. So a counting market asks the bar at
    t - 0.5, which is the same question asked of a number that will be
    rounded. Yards are asked at the bar itself.
    """
    bar = (threshold - 0.5) if counts else float(threshold)
    if h <= 0:
        return 1.0 if x >= bar else 0.0
    return 0.5 * math.erfc((bar - x) / (h * math.sqrt(2.0)))


def smoothed_probs(sample, thresholds, counts=False, h=None):
    """P(value >= t) for each threshold, over his own games, smoothed.

    Still a bootstrap over the games he actually played - the centre of every
    smear is one of his own afternoons, and no curve is fitted to him. The
    only thing assumed is that a player who has gone for 243 and 304 could
    have gone for 260, which is the assumption that makes two neighbouring
    bars different numbers.

    Reproducible by construction: same games in, same percentages out, no
    seed involved.
    """
    if not sample:
        return {}
    h = bandwidth(sample) if h is None else h
    n = len(sample)
    return {t: round(100.0 * sum(point_prob(x, t, h, counts) for x in sample) / n, 1)
            for t in thresholds}


def interval(sample, thresholds, counts=False, seed=None,
             resamples=RESAMPLES, span=INTERVAL_SPAN):
    """How firm each percentage is, as a range.

    This is what COARSE was reaching for and could not say. "76.2%" off
    fifteen games and "76.2%" off a hundred are different claims, and the
    honest way to print the difference is the width of the interval rather
    than a flag that fires on a symptom.

    Resample his games, recompute, and take the middle span% of the answers.
    The bandwidth is held at the full sample's - recomputing it inside each
    resample would be more correct and much slower, and it moves the interval
    by less than the rounding.
    """
    if not sample:
        return {}
    h = bandwidth(sample)
    n = len(sample)
    points = {t: [point_prob(x, t, h, counts) for x in sample] for t in thresholds}
    rng = random.Random(seed)
    spread = {t: [] for t in thresholds}
    for _ in range(resamples):
        # One resampled set of games, scored at every bar - drawing a fresh
        # set per bar would let 250+ come back above 225+.
        idx = [rng.randrange(n) for _ in range(n)]
        for t in thresholds:
            col = points[t]
            spread[t].append(100.0 * sum(col[i] for i in idx) / n)
    edge = (100.0 - span) / 2.0
    return {t: (percentile(v, edge), percentile(v, 100.0 - edge))
            for t, v in spread.items()}


def percentile(sample, pct):
    """The usual linear-interpolated percentile, off the sample itself.

    Not off the draws: a bootstrap draw IS one of his games, so the draws'
    quartiles are the sample's quartiles plus sampling noise. Taking them
    exactly means P25-P75 does not wobble between two runs of a number that
    did not change.
    """
    s = sorted(sample)
    if not s:
        return None
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def is_coarse(probs, thresholds):
    """Did two thresholds STILL come back identical?

    This used to fire constantly: a plain bootstrap cannot produce a value the
    player has never produced, so any two bars with no game between them were
    the same number. Smoothing is what fixed that, and this stayed - because a
    flag that no longer fires is the cheapest possible test of the fix, and
    because there is one case smoothing cannot help with. If every one of his
    games is the identical value the bandwidth is zero, there is genuinely
    nothing to spread, and a spread invented for that player would be the one
    piece of fiction in the file.
    """
    if not probs:
        return False
    return len(set(probs.values())) < len(thresholds)


def team_games(team_id):
    """How many games the team has played, for the availability line."""
    try:
        team = (get(f"{API}/teams/{team_id}") or {}).get("team") or {}
    except Exception:
        return None
    for item in ((team.get("record") or {}).get("items") or []):
        if str(item.get("type") or "") != "total":
            continue
        for stat in (item.get("stats") or []):
            if str(stat.get("name")) == "gamesPlayed":
                try:
                    return int(float(stat.get("value")))
                except (TypeError, ValueError):
                    return None
    return None


def availability(season_games, played):
    """What share of his team's games he has been active for.

    Reported, never applied. The forecast is conditional on him playing and
    saying so is honest; multiplying it by a three-game attendance record
    would be inventing precision, and a player back from injury would be
    marked down for the weeks he missed.
    """
    if not season_games:
        return None
    rate = round(min(1.0, played / float(season_games)), 3)
    return {"team_games": season_games, "played": played, "rate": rate,
            "thin": rate < AVAILABILITY_FLOOR}


def players_in(event_id):
    """[(athlete_id, name, position, team_abbr, team_id)] for both sides."""
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
        for group in (roster.get("athletes") or []):
            # Only players who might take the field. injuredReserveOrOut,
            # suspended and practiceSquad are separate groups and stay out.
            if isinstance(group, dict) and group.get("position") not in (
                    "offense",):
                continue
            for a in (group.get("items") if isinstance(group, dict) else [group]):
                pos = ((a.get("position") or {}).get("abbreviation") or "")
                if pos in POSITIONS:
                    out.append((str(a.get("id")), a.get("displayName"), pos,
                                abbr, str(tid)))
    return out


def usage_of(log, markets):
    """How much of the offence goes through him, across the given markets.

    Yards are the thing being forecast, so ranking by yards would prefer the
    player who had one good afternoon over the one who gets the ball every
    week. Volume is what survives to next Sunday - targets and carries for a
    skill player, and for a quarterback the only volume that matters is
    whether he is the one throwing.
    """
    return sum(v for m in markets for _s, v in (log.get(m) or []))


def pool_of(position):
    for pool in POOLS:
        if position in pool["positions"]:
            return pool
    return None


def top_by_usage(candidates, now=None):
    """The players per side worth forecasting, ranked inside their own pool.

    ESPN returns a roster in alphabetical order, so taking the first six gave
    Adams, Allen, Atwell, Corum, Daniels - and left the team's best receiver
    out of the file entirely. Rank by this season's volume instead, and fall
    back to last season's for a side that has not played yet, which is every
    side in week one.
    """
    now = now or datetime.now(timezone.utc)
    groups = {}
    for cand in candidates:
        pool = pool_of(cand[2])
        if pool:
            groups.setdefault((cand[4], pool["name"]), (pool, []))[1].append(cand)
    out = []
    for (_tid, _pool_name), (pool, group) in groups.items():
        volume = pool["volume"]
        scored = [(usage_of(game_values(c[0]), volume), c) for c in group]
        if not any(u for u, _c in scored):
            scored = [(usage_of(game_values(c[0], now.year - 1), volume), c)
                      for c in group]
        scored.sort(key=lambda row: (-row[0], str(row[1][1])))
        out += [c for _u, c in scored[:pool["per_team"]]]
    return out


def load():
    try:
        return json.loads(STORE.read_text())
    except Exception:
        return {"forecasts": []}


def catalogue():
    """MARKETS, in a form something other than Python can read.

    The Deck draws a tab per market and has to know their order, units and
    bars. It could hold its own list - and then adding a market here would
    quietly leave a tab missing there, which is the drift this repo keeps
    paying for. So the catalogue travels with the data: written on every
    save, served by the API, rendered by the village. MARKETS stays the only
    place a market is defined.
    """
    return {m: {"unit": spec["unit"], "thresholds": list(spec["thresholds"]),
                "counts": spec["counts"], "stat": spec["stat"]}
            for m, spec in MARKETS.items()}


def save(data):
    data["markets"] = catalogue()
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, indent=1) + "\n")


def fixture_of(summary):
    header = summary.get("header") or {}
    comp = ((header.get("competitions") or [{}])[0])
    name = " @ ".join(reversed([
        ((c.get("team") or {}).get("abbreviation") or "?")
        for c in (comp.get("competitors") or [])]))
    return name, (comp.get("date") or header.get("date"))


def rows_for_player(event_id, fixture, kickoff, aid, name, pos, team,
                    samples, seasons, avail):
    """One row per market this player actually has a history in."""
    rows = []
    for market, spec in MARKETS.items():
        sample = samples.get(market) or []
        thresholds = spec["thresholds"]
        if len(sample) < MIN_GAMES or not is_relevant(sample, thresholds):
            continue
        seed = f"{event_id}-{aid}-{market}"
        counts = spec["counts"]
        h = bandwidth(sample)
        probs = smoothed_probs(sample, thresholds, counts, h=h)
        band = interval(sample, thresholds, counts, seed=seed)
        rows.append({
            "event_id": str(event_id), "fixture": fixture, "kickoff_utc": kickoff,
            "athlete_id": aid, "player": name, "position": pos, "team": team,
            "market": market, "stat": spec["stat"], "unit": spec["unit"],
            "thresholds": list(thresholds),
            "probabilities": {str(t): p for t, p in probs.items()},
            "interval": {str(t): [lo, hi] for t, (lo, hi) in band.items()},
            "sample_games": len(sample), "sample_seasons": seasons,
            "sample_mean": round(sum(sample) / len(sample), 1),
            "sample_low": min(sample), "sample_high": max(sample),
            "sample_distinct": len(set(sample)),
            "p25": percentile(sample, 25), "p50": percentile(sample, 50),
            "p75": percentile(sample, 75),
            "availability": avail,
            "coarse": is_coarse(probs, thresholds),
            "counts": counts, "bandwidth": round(h, 2),
            "resamples": RESAMPLES, "interval_span": INTERVAL_SPAN,
            "seed": seed,
            "forecast_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "actual": None, "graded_utc": None,
        })
    return rows


def cmd_forecast(event_id):
    try:
        summary = get(f"{API}/summary?event={event_id}")
    except Exception as exc:
        print(f"cannot read event {event_id}: {exc}", file=sys.stderr)
        return 1
    fixture, kickoff = fixture_of(summary)
    now = datetime.now(timezone.utc)

    data = load()
    made = thin = flagged = 0
    played_in = {}
    for aid, name, pos, team, tid in top_by_usage(players_in(event_id), now):
        if tid not in played_in:
            played_in[tid] = team_games(tid)
        samples, seasons, current = samples_for(aid, now)
        deepest = games_in(samples)
        if deepest < MIN_GAMES:
            # Named, not silently dropped: "no forecast" and "a forecast from
            # three games" are different, and only one of them is honest.
            print(f"  {name:<24} {pos:<3} {team:<4} SKIPPED - {deepest} game(s), "
                  f"need {MIN_GAMES}")
            thin += 1
            continue
        avail = availability(played_in.get(tid), current)
        rows = rows_for_player(event_id, fixture, kickoff, aid, name, pos, team,
                               samples, seasons, avail)
        if not rows:
            print(f"  {name:<24} {pos:<3} {team:<4} SKIPPED - no market he "
                  f"reaches in a quarter of his games")
            thin += 1
            continue
        for row in rows:
            data["forecasts"] = [
                f for f in data["forecasts"]
                if not (f.get("event_id") == row["event_id"]
                        and f.get("athlete_id") == row["athlete_id"]
                        and f.get("market") == row["market"])]
            data["forecasts"].append(row)
            made += 1
            probs, band = row["probabilities"], row["interval"]
            # The interval is printed beside every percentage, not saved for
            # the weak ones: "76.2%" off fifteen games and "76.2%" off a
            # hundred are different claims, and only one of them survives
            # being written down without its width.
            line = "  ".join(
                f"{t}+: {probs[str(t)]:>5.1f}% ({band[str(t)][0]:.0f}-"
                f"{band[str(t)][1]:.0f})" for t in row["thresholds"])
            note = "  FLAT" if row["coarse"] else ""
            if avail and avail["thin"]:
                note += f"  PLAYED {avail['played']}/{avail['team_games']}"
                flagged += 1
            print(f"  {name:<20} {row['market']:<17} {line}   "
                  f"(exp {row['sample_mean']:.1f} {row['unit']}, "
                  f"{row['sample_games']}g, h={row['bandwidth']:g}){note}")

    save(data)
    rough = sum(1 for f in data["forecasts"]
                if f.get("event_id") == str(event_id) and f.get("coarse"))
    print(f"\n{made} forecast(s) written across {len(MARKETS)} markets, "
          f"{thin} player(s) skipped.")
    print(f"Each percentage carries its {INTERVAL_SPAN}% interval in brackets - how far it "
          f"moves when his\nown games are resampled. A wide one is a small "
          f"sample saying so out loud.")
    if rough:
        print(f"{rough} marked FLAT: two bars came back identical even after "
              f"smoothing, which\ntakes every one of his games being the same "
              f"value. There is nothing to spread\nthere and none was "
              f"invented - treat those as the weakest rows here.")
    if flagged:
        print(f"{flagged} marked PLAYED n/m: he has missed games, so the "
              f"percentage is what he\ndoes WHEN HE PLAYS. A book prices that "
              f"times the chance he is active, which is\nwhy a row like this "
              f"can look like a huge edge and be nothing of the kind.")
    print(NOT_A_BET)
    return 0 if made else 1


def strongest(rows):
    """One row per player: the claim furthest from a coin flip.

    'Best' cannot mean 'highest percentage' while there is no price in this
    file. The highest percentage is always the lowest bar, so that ranking
    would print "3+ receptions, 96%" for everyone and tell you nothing - the
    market's answer to an easy bar is a short price, and without the price
    there is no way to prefer one number over another. What can be ranked
    without a price is how far a call is from 50/50, which is the same
    question a book answers with its longest and shortest odds.

    COARSE rows are excluded: a claim whose resolution is an artifact has no
    business being the one shown.

    Ranked on the CAUTIOUS END of each interval, not on the estimate. A
    maximum over many noisy numbers is biased upward - whichever estimate got
    luckiest wins - and taking the near edge of the bracket is what stops a
    nine-game sample beating a twenty-one game one on the strength of having
    less idea. 95% from (54-99) loses to 90% from (86-94), which is the right
    answer: one of those is a claim and the other is a shrug.
    """
    best = {}
    for r in rows:
        if r.get("coarse"):
            continue
        band = r.get("interval") or {}
        for t, pct in (r.get("probabilities") or {}).items():
            lo, hi = band.get(t) or [float(pct), float(pct)]
            over = float(pct) >= 50.0
            conf = float(lo) if over else 100.0 - float(hi)
            key = r.get("athlete_id")
            have = best.get(key)
            if have is None or (conf, r.get("sample_games", 0)) > (
                    have["confidence"], have["row"].get("sample_games", 0)):
                best[key] = {
                    "row": r, "threshold": float(t), "probability": float(pct),
                    "confidence": round(conf, 1),
                    "interval": [lo, hi],
                    "side": "OVER" if over else "UNDER",
                }
    return sorted(best.values(), key=lambda b: -b["confidence"])


def cmd_best(event_id):
    rows = [f for f in load()["forecasts"] if f.get("event_id") == str(event_id)]
    if not rows:
        print(f"no forecasts for event {event_id}. Write them first:\n"
              f"  python3 scripts/props-forecast.py forecast {event_id}",
              file=sys.stderr)
        return 1
    picks = strongest(rows)
    if not picks:
        print("every row for this game is COARSE - nothing here is worth "
              "singling out.")
        return 1
    print(f"  {'player':<20}{'market':<18}{'call':<22}{'model':>7}{'floor':>8}"
          f"   context")
    print("  " + "-" * 94)
    for b in picks:
        r = b["row"]
        t = b["threshold"]
        call = f"{b['side']} {t:g} {r['unit']}"
        ctx = (f"exp {r['sample_mean']:g}, P25-P75 {r['p25']}-{r['p75']}, "
               f"{r['sample_games']}g")
        a = r.get("availability") or {}
        if a.get("thin"):
            ctx += f", PLAYED {a['played']}/{a['team_games']}"
        model = b["probability"] if b["side"] == "OVER" else 100.0 - b["probability"]
        print(f"  {r['player']:<20}{r['market']:<18}{call:<22}"
              f"{model:>6.1f}%{b['confidence']:>7.1f}%   {ctx}")
    print(f"\n  One line per player: his call that sits furthest from 50/50, "
          f"not his\n  highest percentage - the highest percentage is always "
          f"the easiest bar, and\n  without a price that ranking says nothing.")
    print(f"\n  MODEL is the estimate. FLOOR is the cautious end of its 90% "
          f"interval, and\n  it is what the list is SORTED on: a maximum over "
          f"many noisy numbers is won\n  by whichever got luckiest, so ranking "
          f"on the estimate would put every\n  nine-game sample on top. Read "
          f"the floor.")
    print(NOT_A_BET)
    return 0


def actual_value(event_id, athlete_id, stat):
    """A stat from a finished game's box score, or None if it is not final.

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
            idx = column_of(cat.get("keys"), stat)
            if idx is None:
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
    # Final, and he is not in that table: he did nothing in it, or did not
    # play. Those are different and the box score does not say which.
    return None, "final, no line for that stat (did not play, or no touches)"


def cmd_grade():
    data = load()
    pending = [f for f in data["forecasts"] if f.get("actual") is None
               and f.get("graded_utc") is None]
    if not pending:
        print("nothing waiting to be graded.")
        return 0

    # Saved in a finally: grading walks the whole file making network calls,
    # and the first version wrote the results only after the last one. Piping
    # the output through `head` broke the pipe, killed the process mid-print,
    # and threw away every grade it had just collected - silently, because the
    # rows simply stayed pending. A dropped connection would do the same.
    try:
        graded, skipped = grade_all(pending)
    finally:
        save(data)
    print(f"\n{graded} graded, {skipped} still waiting on a final score.")
    return 0


def grade_all(pending):
    graded = skipped = 0
    for f in pending:
        stat = f.get("stat") or (MARKETS.get(f.get("market")) or {}).get("stat")
        value, why = actual_value(f["event_id"], f["athlete_id"], stat)
        if value is None:
            print(f"  {f['player']:<20} {f.get('market',''):<18} {why}")
            if why.startswith("final"):
                # Record that it was looked at, so it is not retried forever.
                f["graded_utc"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S")
                f["grade_note"] = why
                graded += 1
            else:
                skipped += 1
            continue
        f["actual"] = value
        f["graded_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        hits = {t: (value >= float(t)) for t in f["probabilities"]}
        f["hits"] = {t: bool(v) for t, v in hits.items()}
        graded += 1
        marks = "  ".join(
            f"{t}+ {'HIT ' if hits[t] else 'miss'} ({f['probabilities'][t]}%)"
            for t in sorted(f["probabilities"], key=float))
        print(f"  {f['player']:<20} {f.get('market',''):<18} "
              f"{value:>5.0f} {f.get('unit','')}   {marks}")
    return graded, skipped


def buckets(forecasts, width=10):
    """Forecasts grouped by what they predicted, with what actually happened.

    The only question that matters: when it said 70%, did it happen about 70%
    of the time? A model can be sharp and wrong; this catches wrong.
    """
    out = {}
    for f in forecasts:
        if f.get("actual") is None:
            continue
        for t, pct in (f.get("probabilities") or {}).items():
            # 100.0% would otherwise open a "100-109%" bucket, which is not
            # a thing a probability can be. It belongs in the top one.
            lo = min(int(float(pct) // width) * width, 100 - width)
            b = out.setdefault(lo, {"n": 0, "hits": 0, "predicted": 0.0})
            b["n"] += 1
            b["predicted"] += float(pct)
            if float(f["actual"]) >= float(t):
                b["hits"] += 1
    for b in out.values():
        b["predicted"] = round(b["predicted"] / b["n"], 1)
        b["observed"] = round(100.0 * b["hits"] / b["n"], 1)
        b["gap"] = round(b["observed"] - b["predicted"], 1)
    return dict(sorted(out.items()))


def by_market(forecasts):
    """Calibration one market at a time.

    Pooling them hides the answer that matters: receptions are integers off a
    short list and receiving yards are not, so one market can be honest while
    another is badly off and the average looks fine.
    """
    out = {}
    for f in forecasts:
        if f.get("actual") is None:
            continue
        out.setdefault(f.get("market") or "?", []).append(f)
    return dict(sorted(out.items()))


def cmd_calibration():
    data = load()
    done = [f for f in data["forecasts"] if f.get("actual") is not None]
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
        print(f"  {f'{lo}-{lo + 9}%':<10}{b['predicted']:>9.1f}%{b['observed']:>9.1f}%"
              f"{b['gap']:>+8.1f}{b['n']:>7}")

    markets = by_market(done)
    if len(markets) > 1:
        print(f"\n  {'market':<20}{'predicted':>10}{'happened':>10}{'gap':>8}"
              f"{'calls':>7}")
        print("  " + "-" * 55)
        for market, rs in markets.items():
            mb = buckets(rs)
            calls = sum(b["n"] for b in mb.values())
            pred = sum(b["predicted"] * b["n"] for b in mb.values()) / calls
            obs = 100.0 * sum(b["hits"] for b in mb.values()) / calls
            print(f"  {market:<20}{pred:>9.1f}%{obs:>9.1f}%{obs - pred:>+8.1f}"
                  f"{calls:>7}")

    print(f"\n  A row whose gap is near zero is a forecast that means what it "
          f"says.\n  Consistently positive means it is too cautious; negative "
          f"means too\n  confident, which is the one that costs money.")
    if n < 50:
        print(f"\n  {n} calls is not enough to conclude anything. This needs "
              f"weeks, not\n  a night - a coin lands 7 of 10 often enough that "
              f"it means nothing.")
    return 0


def cmd_markets():
    print(f"  {'market':<20}{'ESPN stat':<20}{'unit':<7}thresholds")
    print("  " + "-" * 62)
    for market, spec in MARKETS.items():
        bars = ", ".join(f"{t:g}+" for t in spec["thresholds"])
        print(f"  {market:<20}{spec['stat']:<20}{spec['unit']:<7}{bars}")
    print(f"\n  Read by name, never by column: a tight end's first YDS column "
          f"is receiving\n  and a running back's is rushing, and the box score "
          f"orders them differently\n  again. Adding a market is one line in "
          f"MARKETS.")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in ("forecast", "best"):
        if len(sys.argv) < 3:
            print(f"usage: props-forecast.py {cmd} <event_id>\n"
                  "  find one with: curl -s "
                  "'https://site.web.api.espn.com/apis/site/v2/sports/football/"
                  "nfl/scoreboard' | python3 -m json.tool | grep -A2 shortName",
                  file=sys.stderr)
            return 2
        return cmd_forecast(sys.argv[2]) if cmd == "forecast" else cmd_best(sys.argv[2])
    if cmd == "grade":
        return cmd_grade()
    if cmd == "calibration":
        return cmd_calibration()
    if cmd == "markets":
        return cmd_markets()
    print("usage: props-forecast.py forecast <event_id> | best <event_id> | "
          "grade | calibration | markets", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
