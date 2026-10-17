#!/usr/bin/env python3
"""
ace-fetch.py — sports data fetcher for the Ace agent.

Plain code. NO AI. Pulls scoreboards and per-game context, computes every
number locally, writes JSON. Ace READS those files. If a number is not in a
data file, Ace does not cite it.

Standard library only — no pip installs, no compiler needed.
"""

import hashlib
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
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
# Per-game context fetches, per sport. Was 12, a cost cap from when Ace read
# every file; it capped what Ace could estimate, not what he could risk. The
# review (2026-10-01): "cap actions, not data". ESPN is free, and a real
# college Saturday has about 75 games inside ESTIMATE_WINDOW_HOURS.
DEEP_CAP = 80

# How many games Ace is asked to judge in one cycle, and how far ahead it looks.
#
# The fetcher used to hand over every scheduled game on both slates - about 30
# games, 60 moneyline rows. Judging them cost more tool calls than a five
# minute cycle has, and Ace timed out mid-run twice. Narrowing the field is
# mechanical work: a game starting in three days cannot be bet on information
# that does not exist yet, and one that has started cannot be bet at all. So
# the field is narrowed here and the judgement is left to Ace.
BET_WINDOW_HOURS = 12  # what may be BET: tonight's slate, not tomorrow's
MAX_GAMES = 24         # games offered for betting; judged in batches, so a long list is cheap
# What Ace ESTIMATES, blind, before he sees a price (data/blind.json): every
# game starting within this many hours, priced or not, lopsided or not. A
# calibration study needs every game, not the ones he chose - so tomorrow
# afternoon's games are in tonight's sheet too.
ESTIMATE_WINDOW_HOURS = 30

SPORTS = {
    "mlb": {"path": "baseball/mlb", "months": {4, 5, 6, 7, 8, 9, 10}},
    "nfl": {"path": "football/nfl", "months": {9, 10, 11, 12, 1, 2}},
    "nba": {"path": "basketball/nba", "months": {10, 11, 12, 1, 2, 3, 4, 5, 6}},
    # College football is the same bet Ace already makes - same endpoint, same
    # pickcenter block, same no-vig maths - so it needs no new judgement, only
    # a place in this table. What it does need is PRICE_BAND below: sampled on
    # a real board, CFB carried a moneyline on 9 of 14 games and the ones it
    # did included -1350/+800 and +1300/-2800.
    # groups=80 is FBS and limit lifts ESPN's default page. Without them the
    # college scoreboard returns 22 events where the real board has 75 - Ace
    # was judging a third of a Saturday and the rest was never fetched at all.
    # `limit` alone does nothing; it is `groups` that opens the board.
    "cfb": {"path": "football/college-football", "months": {8, 9, 10, 11, 12, 1},
            "query": "?groups=80&limit=300"},
}

# A candidate has to be a bet Ace's own rule could take. At -2800 the no-vig
# line is about 96%, so clearing the EV bar means believing something no
# injury report supports; the row can only ever be passed. Before this, the
# slate was the eight games starting soonest whatever their price, so one CFB
# Saturday could fill the whole window with blowouts and crowd out the NFL
# games that were actually playable.
#
# Lopsidedness is the FAVOURITE's price - the most negative of the two. The
# first version of this took the smaller magnitude of the pair, which let
# -410/+320 through a band its own comment said was "favourite no shorter than
# -400", because the underdog's +320 was the smaller number. The code and the
# sentence describing it disagreed, which is the thing this repo keeps finding.
#
# 600 rather than 400: -600 is about 86% implied, and an 8-point move from
# there is still a real opinion about a real game. -1000 and beyond is not.
MAX_FAVOURITE = 600

# How recently an injury has to have been reported to be worth Ace's attention.
#
# Rule 2 of Ace's own bar asks for "real information the market has not priced
# yet - a just-announced injury, a scratched starter". It could not be
# satisfied: injuries live inside the per-game context files, Ace is told to
# open three or four of them, so it had no way to scan a board for news and no
# way to tell a report filed an hour ago from one filed eleven days ago.
#
# ESPN timestamps them, and on a real NFL board the ages ran 0h, 2h, 3h, 4h
# alongside entries 264h and 449h old.
#
# 24, not 6 (owner, 2026-10-02). On a real Thursday board 1 of 8 bettable
# games had any news inside six hours, and most NFL news lands Wednesday to
# Friday - days before kick-off - so a six-hour window made a bet nearly
# impossible. What keeps stale news out is not the clock but the price check
# beside it (ace-clv.unpriced): a bet is refused once the line has moved 2
# points on the news, however recent it is.
FRESH_INJURY_HOURS = 24.0

# Enough for Ace to see the shape of a team's news without the candidate file
# turning into an injury report. 10 since the window became a day.
MAX_FRESH_INJURIES = 10


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


def board_spread(event):
    """The scoreboard's point spread for a game, or None.

    Free: it rides along with the scoreboard call that was already made. All
    44 scheduled games on a real college Saturday carried one, while none of
    them carried a moneyline there - that only appears in the per-game summary,
    which is the fetch this is deciding whether to spend.
    """
    for o in ((event.get("competitions") or [{}])[0].get("odds") or []):
        spread = o.get("spread")
        if isinstance(spread, (int, float)):
            return abs(float(spread))
    return None


def pick_for_context(scheduled, now=None):
    """Which scheduled games are worth a per-game fetch, best first.

    Inside the betting window, closest game first. A game outside the window
    cannot be bet on information that does not exist yet, and a 45-point
    favourite cannot clear the EV bar, so neither is worth a call while a
    three-point game is waiting for one. Games with no spread published are
    kept, last: unknown is not the same as lopsided.
    """
    now = now or datetime.now(timezone.utc)
    inside, outside = [], []
    for e in scheduled:
        try:
            start = datetime.strptime(e.get("date", ""), "%Y-%m-%dT%H:%MZ") \
                .replace(tzinfo=timezone.utc)
            hours = (start - now).total_seconds() / 3600
        except Exception:
            hours = 999
        spread = board_spread(e)
        # None sorts last within its group without pretending to be a number.
        key = (spread is None, spread if spread is not None else 0.0, hours)
        (inside if 0 < hours <= ESTIMATE_WINDOW_HOURS else outside).append((key, e))
    inside.sort(key=lambda x: x[0])
    outside.sort(key=lambda x: x[0])
    return [e for _k, e in inside] + [e for _k, e in outside]


def fresh_injuries(context, now=None, window_hours=FRESH_INJURY_HOURS):
    """Injuries reported within the window, newest first.

    Both teams' news is carried on every row of a fixture: a side is priced
    against its opponent, so the opponent losing a starter is as much a reason
    to look as your own team losing one.

    An injury with no date, or one whose date will not parse, is NOT fresh.
    Unknown is not the same as recent, and guessing the other way would put
    a decade-old entry at the top of the list Ace is told to trust.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for inj in ((context or {}).get("injuries") or []):
        raw = inj.get("date")
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        hours = (now - when).total_seconds() / 3600
        # A report dated in the future is a clock problem, not news.
        if hours < 0 or hours > window_hours:
            continue
        out.append({
            # What a bet cites (ace-judge.py bet --event ID), and when it was
            # reported - the moment the price is checked against.
            "id": injury_id(inj),
            "team": inj.get("team"),
            "player": inj.get("player"),
            "position": inj.get("position"),
            "status": inj.get("status"),
            "reported_utc": when.strftime("%Y-%m-%dT%H:%MZ"),
            "hours_old": round(hours, 1),
        })
    out.sort(key=lambda i: i["hours_old"])
    return out[:MAX_FRESH_INJURIES]


def injury_id(inj):
    raw = "|".join(str(inj.get(k) or "") for k in ("team", "player", "status", "date"))
    return "i" + hashlib.sha1(raw.encode()).hexdigest()[:6]


def unplayable(context):
    """Why this game cannot be a candidate, or "" if it can.

    A reason, not a boolean, because "no line was posted" and "the line is
    -2800" are different facts and the slate summary says which.
    """
    odds = (context or {}).get("odds") or {}
    home, away = odds.get("moneyline_home"), odds.get("moneyline_away")
    if home is None or away is None:
        return "no moneyline posted"
    try:
        home, away = int(home), int(away)
    except (TypeError, ValueError):
        return "moneyline is not a number"
    # The favourite is the negative price. A pick-em has both near +100 and no
    # negative side worth worrying about; a blowout has one side at -5000.
    favourite = min(home, away)          # most negative, or the smaller plus
    if favourite < 0 and abs(favourite) > MAX_FAVOURITE:
        return f"lopsided ({home:+d}/{away:+d})"
    return ""


def odds_of(summary, comp=None):
    """DraftKings' moneyline and the no-vig chances from a game's summary.
    One reader, used by the fetch, the 5-minute close and ace-judge.py's
    price at the moment of a bet."""
    pc = (summary.get("pickcenter") or (comp or {}).get("odds") or [])
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
    return odds


def summary_of(sport, event_id):
    return get(f"{BASE}/{SPORTS[sport]['path']}/summary?event={event_id}")


def build_context(sport, path, event):
    """One compact context file per game. Compact matters: Ace reads these,
    and every byte it reads is fuel."""
    eid = event["id"]
    comp = event["competitions"][0]
    home, away = team_of(comp, True), team_of(comp, False)

    summary = get(f"{BASE}/{path}/summary?event={eid}")
    odds = odds_of(summary, comp)

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
                 "name": (home.get("team") or {}).get("displayName"),
                 "record": ((home.get("records") or [{}])[0]).get("summary")},
        "away": {"abbr": (away.get("team") or {}).get("abbreviation"),
                 "name": (away.get("team") or {}).get("displayName"),
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

def ace_clv():
    """ace-clv.py, which owns the price history the closing-line grade reads."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ace_clv",
                                                  Path(__file__).resolve().parent / "ace-clv.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ace_focus():
    """Today's focus from ace-focus.py, or None. Read there, so the fetcher and
    ace-judge.py agree on whether today is a focus day."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ace_focus",
                                                  Path(__file__).resolve().parent / "ace-focus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.today()


def build_ledger(day, ctx_dir, slot, focus=None):
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

    # Unpriced and lopsided games are dropped BEFORE the slate is cut to
    # MAX_GAMES, so the window fills with games that could actually be bet
    # rather than with whatever kicks off first.
    playable, skipped = [], []
    for g in games:
        why = unplayable(g[2])
        (skipped if why else playable).append((g, why))

    # Share the window between the sports in season instead of handing it to
    # whichever plays most often. Cutting the playable games soonest-first put
    # fourteen baseball games ahead of every football game on a Saturday, so
    # college football could be configured, fetched, priced - and never once
    # reach the slate. Each sport gets its next game in turn, soonest first
    # within a sport, until the window is full.
    #
    # A FOCUS DAY (ace-focus.py) replaces all of that: one sport, and only as
    # many games as Ace studies in a cycle, so none is swept unread. Which few
    # is decided here, not by him: games with fresh injury news first - the
    # one kind of information the line may not have priced yet - then soonest.
    if focus:
        mine = [g for g, _ in playable if (g[2].get("sport") or "") == focus["sport"]]
        mine.sort(key=lambda g: (not fresh_injuries(g[2], now), g[0], g[1]))
        chosen = mine[:focus["games"]]
    else:
        # Within a sport, games with fresh news first, then soonest - the
        # review: rank by information, not by how close the spread is.
        by_sport = {}
        for g, _ in sorted(playable, key=lambda x: (not fresh_injuries(x[0][2], now), x[0][0], x[0][1])):
            by_sport.setdefault(g[2].get("sport") or "?", []).append(g)
        chosen, order = [], sorted(by_sport)
        while len(chosen) < MAX_GAMES and any(by_sport.values()):
            for sport in order:
                if by_sport.get(sport) and len(chosen) < MAX_GAMES:
                    chosen.append(by_sport[sport].pop(0))
    dropped = max(0, len(playable) - len(chosen))
    games = sorted(chosen, key=lambda g: (g[0], g[1]))

    rows = []
    news_games = 0
    for _, _, c in games:
        # Computed once per fixture, carried on both of its rows.
        fresh = fresh_injuries(c, now)
        if fresh:
            news_games += 1
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
            sharp = (c.get("sharp") or {}).get(f"novig_{side}_pct")
            rows.append({
                "selection": f"{team} ML",
                "sport": c.get("sport"),
                "match": c.get("short") or c.get("match"),
                "starts_utc": c.get("start_utc"),
                "event_id": c.get("event_id"),
                "side": side,
                "price": ml,
                "novig_pct": pct,
                # Pinnacle's no-vig chance for this side, when ace-sharp.py
                # has one - the sharper market the bar measures against.
                "sharp_pct": sharp,
                "my_pct": None,
                "edge_pts": None,
                # Rule 2 made visible. An empty list is a real answer: no news
                # on this game in the last few hours, so nothing here can be
                # the unpriced information the bar asks for.
                "fresh_injuries": fresh,
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
        # Said on the board Ace reads, so a four-game slate is not mistaken
        # for a quiet day - and the rule that comes with it travels with it.
        "focus": dict(focus, rule="focus day: every game gets its own estimate - "
                                  "ace-judge.py refuses `rest` and multi-row passes")
                 if focus else None,
        "source": "ace-fetch.py - mechanical fields only, awaiting Ace's estimates",
        "window_hours": BET_WINDOW_HOURS,
        "games_shown": len(games),
        "games_outside_window": dropped,
        # What the price band held back, and why. A filter nobody can see is a
        # filter that silently decides the slate - and on a CFB Saturday this
        # one decides most of it. Ace is told these exist so "the board was
        # quiet" and "most of the board was unbettable" stay different facts.
        "fresh_injury_hours": FRESH_INJURY_HOURS,
        "games_with_news": news_games,
        "games_filtered": len(skipped),
        "filtered": [{"match": g[2].get("short") or g[2].get("match"),
                      "sport": g[2].get("sport"), "why": why}
                     for g, why in skipped[:20]],
        "candidates": rows,
    }


def build_blind(day, ctx_dir, slot, now=None):
    """data/blind.json: every game starting within ESTIMATE_WINDOW_HOURS, with
    no price on it - no moneyline, no no-vig chance, no spread, total or
    against-the-spread record. Ace gives each game a home-win chance from
    this sheet BEFORE he sees a price (ace-judge.py blind), because an
    estimate made after reading the line is an estimate of the line.

    Lopsided and unpriced games are kept: a calibration study needs every
    game, and the -600 cut-off is a betting rule, not an estimating one."""
    now = now or datetime.now(timezone.utc)
    games = []
    for f in sorted(ctx_dir.glob("*.json")):
        try:
            c = json.loads(f.read_text())
            start = datetime.strptime(c.get("start_utc", ""), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        hours = (start - now).total_seconds() / 3600
        if str(c.get("status") or "").upper() != "STATUS_SCHEDULED" or not 0 < hours <= ESTIMATE_WINDOW_HOURS:
            continue
        form = {}
        for g in c.get("last_five") or []:
            form.setdefault(g.get("team"), []).append(f"{g.get('result')} {g.get('score')} v {g.get('opponent')}")
        games.append({
            "sport": c.get("sport"), "event_id": c.get("event_id"),
            "match": c.get("short") or c.get("match"), "starts_utc": c.get("start_utc"),
            "home": {k: (c.get("home") or {}).get(k) for k in ("abbr", "record")},
            "away": {k: (c.get("away") or {}).get(k) for k in ("abbr", "record")},
            "last_five": form,
            "fresh_injuries": fresh_injuries(c, now),
            "probable_pitchers": c.get("probable_pitchers") or [],
            "weather": c.get("weather") or {},
            "venue": c.get("venue"),
        })
    games.sort(key=lambda g: (g["starts_utc"] or "", g["match"] or ""))
    for n, g in enumerate(games, 1):
        g["n"] = n
    return {
        "asof_utc": now.strftime("%Y-%m-%d %H:%M:%S"), "day": day, "slot": slot,
        "window_hours": ESTIMATE_WINDOW_HOURS,
        "how": "Give every game the HOME team's chance of winning, from this sheet only: "
               "python3 ../../scripts/ace-judge.py blind \"1:55,2:41,...\". No prices are on it on purpose.",
        "games": games,
    }


def ace_book():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ace_book", Path(__file__).resolve().parent / "ace-book.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Sports whose scoreboard is one day: a game ending after the last fetch
# (11:30pm ET - a West-coast NBA game) is only final on yesterday's board.
DAILY_BOARDS = ("mlb", "nba")


def settle_bets(boards, now=None):
    """Results off the scoreboards already fetched, then every open bet that
    has finished is graded by ace-book.py. Never fatal: a failure here must
    not cost the slate."""
    try:
        book = ace_book()
        n = book.migrate()
        if n:
            log(f"book: imported {n} hand-written bet(s) into state/bets.jsonl")
            book.rebuild()
        for sport, events in boards:
            book.record_results(sport, events, now=now)
        for d in book.settle(now=now):
            log(f"book: settled {d['selection']} ({d['match']}) {d['result']} {d['profit']:+,.2f}")
    except Exception as exc:
        log(f"book: NOT settled - {exc}")


def ace_sharp():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ace_sharp", Path(__file__).resolve().parent / "ace-sharp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sharp_into_contexts(ctx_dir, close=False, now=None):
    """Ask ace-sharp.py for Pinnacle's prices where its budget allows, and
    write each joined game's into its context file as "sharp". Returns the
    contexts that changed."""
    sharp = ace_sharp()
    if not sharp.api_key():
        return []
    contexts = {}
    for f in ctx_dir.glob("*.json"):
        try:
            contexts[f] = json.loads(f.read_text())
        except Exception:
            continue
    changed = sharp.refresh(list(contexts.values()), close=close, now=now)
    for f, c in contexts.items():
        if id(c) in changed:
            f.write_text(json.dumps(c, indent=1) + "\n")
    return [c for c in contexts.values() if id(c) in changed]


# The close (2026-10-01): a game's last price was up to 30 minutes old at
# kick-off, and the last half hour is when lineups and inactives move it.
# ace-close.timer runs `ace-fetch.py --close` every 5 minutes; it refreshes
# only the games starting within this many hours, so it stays a few calls.
CLOSE_WITHIN_HOURS = 2.0


def close_run(now=None, fetch_summary=None):
    now = now or datetime.now(timezone.utc)
    fetch_summary = fetch_summary or summary_of
    fresh = []
    for f in sorted(CTX.glob("*.json")):
        try:
            c = json.loads(f.read_text())
            start = datetime.strptime(c.get("start_utc", ""), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if not 0 < (start - now).total_seconds() / 3600 <= CLOSE_WITHIN_HOURS:
            continue
        try:
            odds = odds_of(fetch_summary(c["sport"], c["event_id"]))
        except Exception as exc:
            log(f"close {c.get('short')}: FAILED - {exc}")
            continue
        if odds.get("novig_home_pct") is None:
            continue
        c["odds"], c["fetched_utc"] = odds, now.strftime("%Y-%m-%d %H:%M:%S")
        f.write_text(json.dumps(c, indent=1) + "\n")
        fresh.append(c)
        time.sleep(GAP)
    try:
        sharp_into_contexts(CTX, close=True, now=now)
    except Exception as exc:
        log(f"close: sharp price NOT added - {exc}")
    if fresh:
        # Re-read: the sharp step may have added Pinnacle's price to them.
        fresh = [json.loads((CTX / f"{c['sport']}-{c['event_id']}.json").read_text()) for c in fresh]
        ace_clv().record_lines(fresh, now=now)
    log(f"close: {len(fresh)} game(s) starting within {CLOSE_WITHIN_HOURS:g}h re-priced")
    return 0


def main():
    if "--close" in sys.argv[1:]:
        CTX.mkdir(parents=True, exist_ok=True)
        return close_run()
    CTX.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    focus = ace_focus()
    season = [focus["sport"]] if focus else in_season(started)
    if focus:
        log(f"FOCUS today: {focus['sport']} only, {focus['games']} games (ace-focus.py)")
    log(f"in season: {season or '(none)'}")

    slate, failures, deep_written = [], [], 0
    written = []            # every context this run, for the closing-line history
    boards = []             # (sport, events) - where results come from

    for sport in season:
        path = SPORTS[sport]["path"]
        try:
            board = get(f"{BASE}/{path}/scoreboard{SPORTS[sport].get('query', '')}")
        except Exception as e:
            failures.append({"sport": sport, "error": str(e)[:140]})
            log(f"{sport}: scoreboard FAILED — {e}")
            continue

        events = board.get("events", [])
        boards.append((sport, events))
        if sport in DAILY_BOARDS:
            yesterday = (et_time.eastern_now() - timedelta(days=1)).strftime("%Y%m%d")
            try:
                boards.append((sport, get(f"{BASE}/{path}/scoreboard?dates={yesterday}").get("events", [])))
            except Exception as e:
                log(f"{sport}: yesterday's board FAILED - {e} (late finals wait for the next run)")
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

        # Deep context only for games that can still be bet, capped - and on a
        # 44-game college Saturday, WHICH twelve is the whole question. It used
        # to be the first twelve in ESPN's order, which is neither soonest nor
        # closest. The scoreboard carries DraftKings' spread for every game at
        # no extra call, so the twelve are the most competitive games inside
        # the betting window: a 45.5-point spread is the same fact as a -8000
        # moneyline, and spending a fetch on it buys a row that can only be
        # passed.
        for e in pick_for_context(scheduled)[:DEEP_CAP]:
            try:
                ctx = build_context(sport, path, e)
                (CTX / f"{sport}-{e['id']}.json").write_text(json.dumps(ctx, indent=1) + "\n")
                deep_written += 1
                written.append(ctx)
            except Exception as exc:
                failures.append({"sport": sport, "event": e["id"], "error": str(exc)[:140]})
                log(f"{sport} {e.get('shortName')}: context FAILED — {exc}")
            time.sleep(GAP)

        log(f"{sport}: {len(events)} games ({len(scheduled)} scheduled, {len(finals)} final)")

    if not slate and failures:
        log("nothing fetched — leaving previous data files untouched")
        return 1

    # Pinnacle's price beside DraftKings', when there is a key and credit
    # for it (ace-sharp.py keeps the budget). Written into the context files
    # so the candidates, the bar and the price history can read it.
    try:
        sharp_into_contexts(CTX)
    except Exception as exc:
        log(f"sharp price NOT added - {exc}")

    try:
        written = [json.loads((CTX / f"{c['sport']}-{c['event_id']}.json").read_text()) for c in written]
    except Exception:
        pass

    # Each game's price, kept until it starts: the last one kept is the close
    # Ace's verdicts are graded against. A failure here must not cost the
    # slate, so it is said and the run goes on.
    try:
        tracked = ace_clv().record_lines(written)
        log(f"closing-line history: {len(tracked)} games tracked")
    except Exception as exc:
        log(f"closing-line history NOT updated - {exc}")

    # Every finished bet is graded here, by code, from these scoreboards.
    settle_bets(boards)

    # Eastern, not UTC. A 23:30 ET wake happens on the next UTC day, and
    # using that date filed a report under tomorrow's name.
    now_et = et_time.eastern_now()
    (DATA / "blind.json").write_text(json.dumps(
        build_blind(et_time.day(now_et), CTX, et_time.slot("ace", now_et)), indent=1) + "\n")
    (DATA / "candidates.json").write_text(
        json.dumps(build_ledger(et_time.day(now_et), CTX,
                                et_time.slot("ace", now_et), focus), indent=1) + "\n")

    # The no-AI bettors decide on the same candidates, at the same prices.
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ace_baselines", Path(__file__).resolve().parent / "ace-baselines.py")
        bl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bl)
        for row in bl.run():
            log(f"no-AI {row['strategy']}: {row['selection']} ({row['match']}) {row['price']}")
    except Exception as exc:
        log(f"no-AI bettors NOT run - {exc}")

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
