#!/usr/bin/env python3
"""
ace-props-nfl.py - Ace Props NFL: the Ace Props rule on NFL player yardage
and catch ladders, by plain code, as its own book on Ace's bankroll.

    ace-props-nfl.py run [--dry]      update, settle, then judge the games starting soon and bet
    ace-props-nfl.py close            settle what finished; one price pull just before each bet's kick-off
    ace-props-nfl.py check [--spend]  is the key there, how many credits; --spend reads ONE game (6 credits at most)
    ace-props-nfl.py check --sample   that one game's saved reply read again, matched and judged (free)
    ace-props-nfl.py players          fill the player store from this season's box scores (free)
    ace-props-nfl.py board --json     what the page draws
    ace-props-nfl.py show             the book in words

THE RULE is Ace Props', imported from ace-props.py and never restated
(owner, 2026-11-11: "lets set up another one so we can do all nfl player
props"): the opponent is bottom BOTTOM at defending the player's position -
here, in the stat of the ladder (receiving yards allowed to wide receivers
for a receiver's yards ladder); his hit rate on each rung against such
defences this season, with no minimum, is pulled SHRINK of the way to the
price and must beat it by GAP_MIN_PTS; prices MAX_FAVOURITE to MAX_LONGSHOT.
judge() is ace-props.py's own function.

THE DEFENCES are Peter's (nfl-defense.py): the same table his page shows,
what each defence allows a game to quarterbacks, running backs, receivers
and tight ends this season. Nothing here counts a defence a second way.
Rush + receiving yards is his two columns added.

THE PLAYERS: every finished game's box score this season, read with
nfl-defense.py's own reader (lines_of), kept per player in
agents/ace/data/props-nfl-players.json. Free, ESPN.

THE PRICES are DraftKings' alternate ladders from The Odds API, on a SECOND
free account (owner's choice, 2026-11-11, aware the provider may not like
two accounts): ODDS_API_KEY_NFL, never Ace's ODDS_API_KEY, so Ace's Pinnacle
check keeps its own 500 credits. A free account has 500 credits a month; one
game's ladders cost one credit per market returned (MARKETS, at most six).
So the money is spent carefully:
  * the event list is free; a game is priced once, by the first run within
    LOOK_AHEAD_H of its kick-off, after the inactives (90 minutes before);
  * only the markets where one of its two defences is in the bottom BOTTOM
    for a position that market is about (wanted_markets);
  * no game is priced once the day's DAILY_CAP bets are placed;
  * at most WEEK_CREDITS an NFL week, and RESERVE kept back for the closes;
  * the close is one pull of only the bets' markets, CLOSE_WITHIN_MIN before
    kick-off, for games with a bet in them.

SEPARATE FROM ACE AND FROM NBA PROPS, ON ACE'S MONEY. Book "props-nfl" in
Ace's state/bets.jsonl: ace-book.py's is_props keeps it out of everything
his preregistration measures, and ace-props.py grades only book "props".
Its own cap (DAILY_CAP a day, by when placed), its own stop (STOP_USD of
settled losses), its own closing-line value.

No model is woken. Standard library only.
"""

import argparse
import importlib.util
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import et_time  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
DATA = ROOT / "agents" / "ace" / "data"
STATE = ROOT / "agents" / "ace" / "state"
STORE = DATA / "props-nfl-players.json"
BOARD = DATA / "props-nfl-board.json"
CLOSES = DATA / "props-nfl-close.json"
SAMPLE = DATA / "props-nfl-sample.json"     # the last raw reply `check --spend` read
USAGE = STATE / "props-nfl-usage.json"
CREDENTIALS = STATE / "credentials.env"

BOOK = "props-nfl"                 # ace-book.py's PROPS_NFL
KEY_NAME = "ODDS_API_KEY_NFL"      # the second account - never Ace's ODDS_API_KEY
SPORT = "americanfootball_nfl"
BOOKMAKER = "draftkings"

# The Odds API market -> (the stat, by nfl-defense.py's names, the words a
# bet is called by, the positions whose defence the market is about).
RUSH_REC = "rush_rec_yards"        # rushing_yards + receiving_yards
MARKETS = {
    "player_pass_yds_alternate":           ("passing_yards", "pass yds", ("QB",)),
    "player_pass_tds_alternate":           ("passing_tds", "pass TDs", ("QB",)),
    "player_rush_yds_alternate":           ("rushing_yards", "rush yds", ("QB", "RB")),
    "player_reception_yds_alternate":      ("receiving_yards", "rec yds", ("RB", "WR", "TE")),
    "player_receptions_alternate":         ("receptions", "catches", ("RB", "WR", "TE")),
    "player_rush_reception_yds_alternate": (RUSH_REC, "rush+rec yds", ("RB", "WR", "TE")),
}

# The defences a bet can rest on: "POS stat" for each market's positions.
USED = {f"{pos} {stat}" for stat, _w, positions in MARKETS.values() for pos in positions}

DAILY_CAP = 2                      # bets a day (Eastern, by when placed) - as NBA props
STOP_USD = 1000.0                  # settled losses that pause NFL props; nothing else is touched
LOOK_AHEAD_H = 3.5                 # a run prices the games kicking off within this...
NOT_WITHIN_MIN = 20                # ...but never one starting sooner than this
CLOSE_WITHIN_MIN = 25              # the close pull: a bet's game kicking off within this
WEEK_CREDITS = 95                  # 500 a month over up to five NFL weeks, the closes kept apart
RESERVE = 10                       # credits only a close may spend
VOID_AFTER_HOURS = 72
GAP = 0.4
STAMP = "%Y-%m-%d %H:%M:%S"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AP = _load("ace_props_for_nfl", "ace-props.py")      # judge, implied, the rule's numbers
NFL = _load("nfl_defense_for_props", "nfl-defense.py")  # the defences and the box-score reader
SHARP = _load("ace_sharp_for_nfl", "ace-sharp.py")   # the Odds API call, team names


def book():
    b = _load("ace_book_props_nfl", "ace-book.py")
    b.STATE = STATE
    b.BETS, b.BANK = b.STATE / "bets.jsonl", b.STATE / "bankroll.json"
    return b


def utc_now():
    return datetime.now(timezone.utc)


parse_utc = AP.parse_utc
_read, _write = AP._read, AP._write


def log(msg):
    print(f"[ace-props-nfl {utc_now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ the key and the credits

def api_key():
    """The second account's key: the environment, else Ace's credentials
    file. Never falls back to ODDS_API_KEY - that is Ace's own 500."""
    if os.environ.get(KEY_NAME):
        return os.environ[KEY_NAME].strip() or None
    if not CREDENTIALS.exists():
        return None
    try:
        return (dict(SHARP._env_reader()(CREDENTIALS)).get(KEY_NAME) or "").strip() or None
    except Exception:
        return None


def week_label(season, week):
    return f"{season} week {week}"


def usage(week=None):
    u = _read(USAGE, {}) or {}
    u.setdefault("remaining", None)
    u.setdefault("weeks", {})
    u.setdefault("log", [])
    if week:
        u["weeks"].setdefault(week, 0)
    return u


def spend_ok(u, week, cost, close=False):
    """May a pull of `cost` credits be made? A decision keeps RESERVE back
    and stays inside the week's WEEK_CREDITS; a close needs only the credits."""
    rem = u.get("remaining")
    if rem is not None and rem - cost < (0 if close else RESERVE):
        return False
    return close or u["weeks"].get(week, 0) + cost <= WEEK_CREDITS


def note_spend(u, week, what, remaining, cost):
    """Record a pull: the account's remaining credits as the reply said, and
    what it cost - the drop in remaining when both are known (the truth),
    else the number of markets returned (the documented price)."""
    before = u.get("remaining")
    spent = before - remaining if (before is not None and remaining is not None and before >= remaining) else cost
    if remaining is not None:
        u["remaining"] = remaining
    if week:
        u["weeks"][week] = u["weeks"].get(week, 0) + spent
    u["log"] = (u["log"] + [{"at": utc_now().strftime(STAMP), "what": what, "cost": spent,
                             "remaining": u.get("remaining")}])[-60:]
    _write(USAGE, u)
    return spent


def odds_call(path, params, key):
    return SHARP.call(path, params, key=key)


def odds_events(key, u):
    """The NFL's upcoming games on The Odds API - free."""
    data, remaining = odds_call(f"/v4/sports/{SPORT}/events", {"dateFormat": "iso"}, key)
    if remaining is not None:
        u["remaining"] = remaining
        _write(USAGE, u)
    return data if isinstance(data, list) else []


def event_odds(key, event_id, markets):
    """(reply, remaining) for one game's DraftKings ladders in `markets`."""
    return odds_call(f"/v4/sports/{SPORT}/events/{event_id}/odds",
                     {"regions": "us", "markets": ",".join(markets), "bookmakers": BOOKMAKER,
                      "oddsFormat": "american", "dateFormat": "iso"}, key)


# ------------------------------------------------------------------ ladders

def name_key(name):
    """A player's name for matching The Odds API to ESPN: SHARP.norm, less a
    Jr./Sr./II-V suffix, which one side sometimes leaves off."""
    s = SHARP.norm(name)
    return re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", s)


def ladders_from(reply):
    """({(market, name_key): {"name", "rungs": [(line, price)]}}, markets
    returned). DraftKings' Overs only. A rung at 24.5 is "25+"; a
    whole-number point can push, so it is left out."""
    out, returned = {}, set()
    for bk in (reply or {}).get("bookmakers") or []:
        if bk.get("key") != BOOKMAKER:
            continue
        for m in bk.get("markets") or []:
            key = m.get("key")
            if key not in MARKETS:
                continue
            returned.add(key)
            for o in m.get("outcomes") or []:
                pt, price = o.get("point"), o.get("price")
                if o.get("name") != "Over" or not o.get("description") or pt is None or price is None:
                    continue
                try:
                    pt, price = float(pt), int(round(float(price)))
                except (TypeError, ValueError):
                    continue
                if abs(pt - math.floor(pt) - 0.5) > 1e-9:
                    continue
                row = out.setdefault((key, name_key(o["description"])), {"name": o["description"], "rungs": []})
                row["rungs"].append((math.floor(pt) + 1, price))
    for row in out.values():
        row["rungs"] = sorted(set(row["rungs"]))
    return out, sorted(returned)


# ------------------------------------------------------------------ players

def names_of(summary):
    """{athlete_id: name} off a box score."""
    out = {}
    for team in ((summary.get("boxscore") or {}).get("players") or []):
        for cat in team.get("statistics") or []:
            for a in cat.get("athletes") or []:
                ath = a.get("athlete") or {}
                if ath.get("id") and ath.get("displayName"):
                    out[str(ath["id"])] = ath["displayName"]
    return out


def game_rows(summary):
    """(Eastern day, [[team, opp, athlete_id, name, {stat: value}]]) for one
    finished game - nfl-defense.py's lines_of, with names."""
    comp = (((summary.get("header") or {}).get("competitions") or [{}])[0])
    day = et_time.day(et_time.to_eastern(comp.get("date"))) if comp.get("date") else None
    opp, names = NFL.opponents(summary), names_of(summary)
    rows = []
    for team, players in NFL.lines_of(summary).items():
        for aid, stats in players.items():
            rows.append([team, opp.get(team), aid, names.get(aid), stats])
    return day, rows


def update_players(get=None, events=None, say=log):
    """Read this season's finished games not yet stored. Returns (store, added)."""
    get = get or NFL.pf.get
    season, _week, events = events or NFL.season_events(get)
    store = _read(STORE, {}) or {}
    if store.get("season") != season:
        store = {"season": season, "games": {}}
    added = 0
    for eid, w in events:
        if eid in store["games"]:
            continue
        try:
            summary = get(f"{NFL.pf.API}/summary?event={eid}")
        except Exception as exc:
            say(f"  {eid}: could not read the box score ({exc}) - next time")
            continue
        if not NFL.is_final(summary):
            continue
        day, rows = game_rows(summary)
        store["games"][eid] = {"date": day, "week": w, "rows": rows}
        added += 1
        time.sleep(GAP / 4)
    if added:
        _write(STORE, store)
    return store, added


def stat_of(stats, stat):
    if stat == RUSH_REC:
        return float(stats.get("rushing_yards") or 0) + float(stats.get("receiving_yards") or 0)
    return float(stats.get(stat) or 0)


def players(store):
    """{athlete_id: {"name", "team", "rows": [(date, opp, stats)]}}, team from his latest game."""
    out = {}
    for g in sorted((store.get("games") or {}).values(), key=lambda g: str(g.get("date"))):
        for team, opp, aid, name, stats in g["rows"]:
            p = out.setdefault(aid, {"rows": []})
            p.update(team=team, name=name or p.get("name"))
            p["rows"].append((g["date"], opp, stats))
    return out


def by_name(who, teams):
    """{name_key: athlete_id} for the players whose latest team is one of
    `teams`; a name two of them share is left out rather than guessed."""
    seen = {}
    for aid, p in who.items():
        if p.get("team") in teams and p.get("name"):
            seen.setdefault(name_key(p["name"]), []).append(aid)
    return {k: v[0] for k, v in seen.items() if len(v) == 1}


# ------------------------------------------------------------------ defences

def defence(doc):
    """{"POS stat": {"allowed": {team: a game}, "rank": {team: 1 = most}, "bottom": [...]}}
    from Peter's table (nfl-defense.py ranks): the BOTTOM defences are the
    ones that allow the most."""
    ranks = NFL.ranks(doc) if (doc or {}).get("games") else {}
    out = {}
    stats = {s for s, _w, _p in MARKETS.values()}
    for pos in NFL.POSITIONS:
        for stat in stats:
            avg = {}
            for team, t in ranks.items():
                by = t.get(pos) or {}
                if stat == RUSH_REC:
                    avg[team] = round((by.get("rushing_yards") or {}).get("avg", 0) +
                                      (by.get("receiving_yards") or {}).get("avg", 0), 2)
                else:
                    avg[team] = (by.get(stat) or {}).get("avg", 0)
            order = sorted(avg, key=lambda t: (-avg[t], t))
            out[f"{pos} {stat}"] = {"allowed": avg, "rank": {t: i + 1 for i, t in enumerate(order)},
                                    "bottom": order[:AP.BOTTOM]}
    return out


def wanted_markets(home, away, dfn):
    """The markets worth paying for in one game: those where one side's
    opponent is bottom BOTTOM for a position the market is about."""
    out = []
    for market, (stat, _w, positions) in MARKETS.items():
        for pos in positions:
            bottom = (dfn.get(f"{pos} {stat}") or {}).get("bottom") or []
            if home in bottom or away in bottom:
                out.append(market)
                break
    return out


# ------------------------------------------------------------------ judging

def game_board(event, ladders, who, positions, dfn):
    """Every (player, market) on a game's ladders, judged by ace-props.py's
    judge() on his games this season against the bottom defences."""
    teams = event["teams"]
    index = by_name(who, set(teams.values()))
    rows = []
    for (market, key), lad in sorted(ladders.items()):
        stat, words, _positions = MARKETS[market]
        base = {"market": market, "words": words, "name": lad["name"]}
        aid = index.get(key)
        if not aid:
            rows.append(dict(base, why="not in either team's box scores this season under that name"))
            continue
        p = who[aid]
        pos = NFL.position_of(positions.get(aid))
        opp = teams["away"] if p["team"] == teams["home"] else teams["home"]
        if not pos:
            rows.append(dict(base, athlete_id=aid, why="no QB/RB/WR/TE position known for him"))
            continue
        d = dfn.get(f"{pos} {stat}") or {}
        row = dict(base, athlete_id=aid, team=p["team"], opp=opp, pos=pos, stat=stat,
                   opp_rank=(d.get("rank") or {}).get(opp), opp_allowed=(d.get("allowed") or {}).get(opp),
                   rungs=[], best=None, why="")
        if opp not in (d.get("bottom") or []):
            row["why"] = (f"{opp} is not bottom {AP.BOTTOM} against {pos}s in {words} "
                          f"(rank {row['opp_rank']} of {len(d.get('rank') or {})})")
        else:
            games = [(day, o, 1, stat_of(stats, stat)) for day, o, stats in p["rows"]]
            row["rungs"] = [AP.judge(line, price, games, set(d["bottom"])) for line, price in lad["rungs"]]
            ok = [r for r in row["rungs"] if r["clears"]]
            row["best"] = max(ok, key=lambda r: r["edge_pts"]) if ok else None
            row["why"] = row["best"]["why"] if row["best"] else "no rung clears the bar"
        rows.append(row)
    rows.sort(key=lambda r: (r.get("best") is None, -((r.get("best") or {}).get("edge_pts") or 0), str(r.get("name"))))
    return {"event_id": event["id"], "odds_id": event.get("odds_id"), "match": event["match"],
            "starts_utc": event["starts_utc"], "status": event["status"], "players": rows}


# ------------------------------------------------------------------ the book

def nfl_bets(b=None):
    b = b or book()
    f = b.fold(b.events())
    mine = lambda x: x.get("book") == BOOK
    return [x for x in f["open"] if mine(x)], [x for x in f["settled"] if mine(x)], f


def stop_reason(settled):
    lost = -sum(float(x.get("profit") or 0) for x in settled)
    if lost >= STOP_USD:
        return (f"NFL props have lost ${lost:,.2f} settled - the ${STOP_USD:,.0f} stop. "
                f"Ace and NBA props are not affected")
    return None


def room_today(open_bets, settled, day):
    return DAILY_CAP - len(AP.placed_on(open_bets + settled, day))


def pick(boards, open_bets, settled, day, now=None):
    """The bets to place now: the best clearing rung per player, best edge
    first, one per game and per player a day, up to DAILY_CAP a day."""
    now = now or utc_now()
    why = stop_reason(settled)
    if why:
        return [], why
    today = AP.placed_on(open_bets + settled, day)
    room = DAILY_CAP - len(today)
    if room <= 0:
        return [], f"{len(today)} NFL props bet(s) placed today - the cap is {DAILY_CAP}"
    games = {str(x.get("event_id")) for x in today}
    people = {str(x.get("athlete_id")) for x in today}
    cands = []
    for g in boards:
        start = parse_utc(g["starts_utc"])
        if g["status"] != "STATUS_SCHEDULED" or not start or (start - now).total_seconds() < NOT_WITHIN_MIN * 60:
            continue
        for p in g["players"]:
            if p.get("best"):
                cands.append((p["best"]["edge_pts"], g, p))
    out = []
    for _edge, g, p in sorted(cands, key=lambda c: -c[0]):
        if len(out) >= room or str(g["event_id"]) in games or str(p["athlete_id"]) in people:
            continue
        games.add(str(g["event_id"]))
        people.add(str(p["athlete_id"]))
        r = p["best"]
        out.append({"book": BOOK, "sport": "nfl", "event_id": g["event_id"], "odds_id": g.get("odds_id"),
                    "match": g["match"], "starts_utc": g["starts_utc"],
                    "selection": f"{p['name']} {r['line']:g}+ {p['words']}", "side": "over",
                    "athlete_id": p["athlete_id"], "athlete": p["name"], "market": p["market"],
                    "stat": p["stat"], "line": r["line"], "team": p["team"], "opp": p["opp"], "pos": p["pos"],
                    "opp_rank": p["opp_rank"], "price": r["price"], "novig_pct": r["market_pct"],
                    "hit_pct": r["hit_pct"], "games": r["games"], "p_used": r["p_used"], "edge_pts": r["edge_pts"],
                    "price_source": f"DraftKings via The Odds API at {now.strftime(STAMP)} UTC",
                    "why": f"{p['opp']} rank {p['opp_rank']} against {p['pos']}s in {p['words']}; {r['why']}"})
    return out, None


def grade(bet, store, now=None):
    """(result, profit, detail) for an open bet, or None while it waits."""
    now = now or utc_now()
    g = (store.get("games") or {}).get(str(bet.get("event_id")))
    start = parse_utc(bet.get("starts_utc"))
    if g is None:
        if start and now - start > timedelta(hours=VOID_AFTER_HOURS):
            return "void", 0.0, f"no final box score {VOID_AFTER_HOURS}h after the kick-off"
        return None
    row = next((r for r in g["rows"] if r[2] == str(bet.get("athlete_id"))), None)
    if row is None:
        # ESPN lists only players with a line in the box score; a starter
        # with a ladder who is missing from it was, all but always, inactive.
        return "void", 0.0, f"{bet.get('athlete')} is not in the box score - did not play, DraftKings voids it"
    got, line = stat_of(row[4], bet.get("stat")), float(bet.get("line"))
    words = (MARKETS.get(bet.get("market")) or ("", "", ()))[1]
    if got >= line:
        d = book().decimal_odds(bet.get("price"))
        return "won", round(float(bet.get("stake") or 0) * (d - 1), 2), f"{bet.get('athlete')} had {got:g} {words}"
    return "lost", -float(bet.get("stake") or 0), f"{bet.get('athlete')} had {got:g} {words}"


def settle(store, b=None, now=None):
    b = b or book()
    now = now or utc_now()
    done = []
    for bet in nfl_bets(b)[0]:
        got = grade(bet, store, now)
        if not got:
            continue
        word, profit, detail = got
        b.append({"kind": "settle", "id": bet["id"], "utc": now.strftime(STAMP), "result": word,
                  "profit": profit, "detail": detail})
        done.append(f"{bet['selection']}: {word} {profit:+,.2f} ({detail})")
    if done:
        b.rebuild()
    return done


def clv_of(bet, closes):
    return AP.clv_of(bet, closes)


# ------------------------------------------------------------------ ESPN's games

def espn_games(get=None):
    """This NFL week's games from ESPN's scoreboard, as {id, match, starts_utc,
    status, teams {home, away}, names {home, away}, season, week}."""
    get = get or NFL.pf.get
    board = get(f"{NFL.pf.API}/scoreboard")
    season = (board.get("season") or {}).get("year")
    week = (board.get("week") or {}).get("number")
    out = []
    for e in board.get("events") or []:
        comp = (e.get("competitions") or [{}])[0]
        teams, names = {}, {}
        for c in comp.get("competitors") or []:
            teams[c.get("homeAway")] = (c.get("team") or {}).get("abbreviation")
            names[c.get("homeAway")] = (c.get("team") or {}).get("displayName")
        if not teams.get("home") or not teams.get("away"):
            continue
        out.append({"id": str(e.get("id")), "match": e.get("shortName"), "starts_utc": e.get("date"),
                    "status": ((comp.get("status") or {}).get("type") or {}).get("name"),
                    "teams": teams, "names": names, "season": season, "week": week})
    return out


def odds_id_for(game, odds_list):
    ctx = {"start_utc": game["starts_utc"], "home": {"name": game["names"]["home"]},
           "away": {"name": game["names"]["away"]}}
    hit = SHARP.join(ctx, odds_list)
    return hit.get("id") if hit else None


def due(games, now, judged):
    """The games a run prices now: scheduled, kicking off between
    NOT_WITHIN_MIN and LOOK_AHEAD_H from now, not priced already."""
    out = []
    for g in games:
        start = parse_utc(g["starts_utc"])
        if g["status"] != "STATUS_SCHEDULED" or not start or g["id"] in judged:
            continue
        mins = (start - now).total_seconds() / 60
        if NOT_WITHIN_MIN <= mins <= LOOK_AHEAD_H * 60:
            out.append(g)
    return out


# ------------------------------------------------------------------ runs

def refresh(get=None, say=log):
    """Peter's defences and the player store, both free. Returns (store, dfn, positions)."""
    get = get or NFL.pf.get
    try:
        NFL.update(get, say=lambda m: None)
    except Exception as exc:
        say(f"Peter's defences not updated ({exc}) - using what is saved")
    store, added = update_players(get, say=say)
    doc = NFL.load()
    say(f"box scores: {added} new game(s), {len(store.get('games') or {})} this season")
    return store, defence(doc), doc.get("positions") or {}


def run(dry=False, get=None, now=None, key=None, say=log):
    now = now or utc_now()
    day = et_time.day(et_time.to_eastern(now))
    store, dfn, positions = refresh(get, say)
    b = book()
    lines = settle(store, b, now)
    open_bets, settled, _f = nfl_bets(b)
    old = _read(BOARD, {}) or {}
    boards = old.get("games", []) if old.get("day") == day else []
    judged = {g["event_id"] for g in boards if g.get("done")}      # an error is tried again
    boards = [g for g in boards if g.get("done")]
    games = espn_games(get)
    week = week_label(games[0]["season"], games[0]["week"]) if games else None
    todo = due(games, now, judged)
    room = room_today(open_bets, settled, day)
    stopped = stop_reason(settled)
    key = key or api_key()
    u = usage(week)
    notes = []
    if not todo:
        notes.append("no game kicks off in the next few hours that has not been priced")
    elif stopped:
        notes.append(stopped)
    elif room <= 0:
        notes.append(f"the day's {DAILY_CAP} bets are placed - no credits spent")
    elif not key:
        notes.append(f"no {KEY_NAME} - put it in with: python3 scripts/set-credential.py ace {KEY_NAME}")
    else:
        try:
            listed = odds_events(key, u)
        except SHARP.ApiError as exc:
            listed = []
            notes.append(f"The Odds API: {exc}")
        who = players(store)
        for g in todo:
            want = wanted_markets(g["teams"]["home"], g["teams"]["away"], dfn)
            entry = {"event_id": g["id"], "match": g["match"], "starts_utc": g["starts_utc"],
                     "status": g["status"], "players": [], "markets": want}
            oid = odds_id_for(g, listed) if listed else None
            if not want:
                entry.update(done=True, note="neither defence is bottom 10 in anything priced - not paid for")
            elif not oid:
                entry["note"] = "not found on The Odds API" if listed else "The Odds API not read"
            elif not spend_ok(u, week, len(want)):
                entry.update(done=True, note=f"credits: {u.get('remaining')} left, {u['weeks'].get(week, 0)} of "
                                             f"{WEEK_CREDITS} spent this week - not priced")
            else:
                try:
                    reply, remaining = event_odds(key, oid, want)
                except SHARP.ApiError as exc:
                    entry["note"] = f"The Odds API: {exc}"
                    boards.append(entry)
                    continue
                ladders, returned = ladders_from(reply)
                cost = note_spend(u, week, f"ladders {g['match']}", remaining, len(returned))
                g["odds_id"] = oid
                entry = dict(game_board(g, ladders, who, positions, dfn), markets=want, done=True,
                             note=f"{len(returned)} market(s) priced, {cost} credit(s)")
                time.sleep(GAP)
            boards.append(entry)
        bets, why = pick([g for g in boards if g["event_id"] in {t["id"] for t in todo}],
                         open_bets, settled, day, now)
        for f in bets:
            if dry:
                lines.append(f"would bet {f['selection']} ({f['match']}) {f['price']}: {f['why']}")
            else:
                e = b.place(f)
                lines.append(f"BET {e['selection']} ({e['match']}) {e['price']} ${e['stake']:,.2f}: {e['why']}")
        if not bets:
            notes.append(why or "nothing cleared the bar")
    _write(BOARD, {"asof_utc": now.strftime(STAMP), "day": day, "week": week,
                   "games": sorted(boards, key=lambda g: str(g["starts_utc"])),
                   "defense": {k: {"bottom": d["bottom"], "allowed": {t: d["allowed"][t] for t in d["bottom"]}}
                               for k, d in dfn.items() if k in USED},
                   "store": {"games": len(store.get("games") or {}),
                             "latest": max((g["date"] for g in (store.get("games") or {}).values()
                                            if g.get("date")), default=None)}})
    return lines + [f"no NFL props bet: {n}" for n in notes]


def finals(open_bets, get=None, now=None):
    """The box scores of open bets' games once they could be over - free,
    and only those games, so a close every 10 minutes reads next to nothing."""
    get = get or NFL.pf.get
    now = now or utc_now()
    store = _read(STORE, {}) or {}
    store.setdefault("games", {})
    added = 0
    for eid in {str(x["event_id"]) for x in open_bets}:
        start = parse_utc(next(x for x in open_bets if str(x["event_id"]) == eid).get("starts_utc"))
        if eid in store["games"] or not start or now - start < timedelta(hours=3):
            continue
        try:
            summary = get(f"{NFL.pf.API}/summary?event={eid}")
        except Exception:
            continue
        if NFL.is_final(summary):
            day, rows = game_rows(summary)
            store["games"][eid] = {"date": day, "week": None, "rows": rows}
            added += 1
    if added:
        _write(STORE, store)
    return store


def record_closes(open_bets, now=None, key=None, say=log):
    """One pull per game with a bet, CLOSE_WITHIN_MIN before kick-off, of
    only the bets' markets; the price then is the bet's close."""
    now = now or utc_now()
    closes = _read(CLOSES, {}) or {}
    by_game = {}
    for bet in open_bets:
        start = parse_utc(bet.get("starts_utc"))
        if bet["id"] in closes or not start or not bet.get("odds_id"):
            continue
        if now < start <= now + timedelta(minutes=CLOSE_WITHIN_MIN):
            by_game.setdefault(bet["odds_id"], []).append(bet)
    if not by_game:
        return closes
    key = key or api_key()
    if not key:
        say(f"no {KEY_NAME} - no closes")
        return closes
    u = usage()
    for oid, bets in by_game.items():
        markets = sorted({x["market"] for x in bets})
        if not spend_ok(u, None, len(markets), close=True):
            say(f"no credits left for the close of {bets[0]['match']}")
            continue
        try:
            reply, remaining = event_odds(key, oid, markets)
        except SHARP.ApiError as exc:
            say(f"close of {bets[0]['match']}: {exc}")
            continue
        ladders, returned = ladders_from(reply)
        note_spend(u, None, f"close {bets[0]['match']}", remaining, len(returned))
        for bet in bets:
            lad = ladders.get((bet["market"], name_key(bet["athlete"]))) or {}
            price = dict(lad.get("rungs") or []).get(int(bet["line"]))
            closes[bet["id"]] = {"price": price, "at": now.strftime(STAMP)} if price is not None else \
                {"price": None, "at": now.strftime(STAMP), "why": "the rung was gone"}
    _write(CLOSES, closes)
    return closes


def close(get=None, now=None, key=None, say=log):
    now = now or utc_now()
    b = book()
    open_bets = nfl_bets(b)[0]
    if not open_bets:
        return ["no NFL props bet open"]
    store = finals(open_bets, get, now)
    lines = settle(store, b, now)
    open_bets = nfl_bets(b)[0]
    record_closes(open_bets, now, key, say)
    return lines + [f"{len(open_bets)} NFL props bet(s) open"]


# ------------------------------------------------------------------ the owner's check

def check(spend=False, get=None, key=None, say=print, sample=False):
    """Is the second key there, how many credits are left, and - with
    spend - one game's real ladders read and matched to ESPN's players, so
    the reading is seen working on a real reply before any bet rests on it.
    With sample, the reply `--spend` saved is read again: free."""
    if sample:
        reply = _read(SAMPLE, None)
        if not reply:
            say("no saved reply yet - run: python3 scripts/ace-props-nfl.py check --spend")
            return 1
        g = next((g for g in espn_games(get) if odds_id_for(g, [reply])), None)
        if not g:
            say(f"the saved reply ({reply.get('away_team')} at {reply.get('home_team')}) is not on ESPN's board this week")
            return 1
        say(f"{g['match']}: the reply saved by check --spend, read again (free)")
        return reading(g, reply, say)
    key = key or api_key()
    if not key:
        say(f"no {KEY_NAME} yet. Put it in with: python3 scripts/set-credential.py ace {KEY_NAME}")
        return 1
    u = usage()
    try:
        listed = odds_events(key, u)
    except SHARP.ApiError as exc:
        say(f"The Odds API refused the key: {exc}")
        return 1
    say(f"key works: {len(listed)} NFL game(s) listed, {u.get('remaining')} credit(s) left on this account")
    if not spend:
        say("add --spend to read one game's ladders (6 credits at most) and check the reading")
        return 0
    games = espn_games(get)
    soon = sorted((g for g in games if g["status"] == "STATUS_SCHEDULED"), key=lambda g: str(g["starts_utc"]))
    pair = next(((g, odds_id_for(g, listed)) for g in soon if odds_id_for(g, listed)), (None, None))
    if not pair[0]:
        say("no scheduled ESPN game matched a game on The Odds API - nothing read")
        return 1
    g, oid = pair
    reply, remaining = event_odds(key, oid, list(MARKETS))
    _write(SAMPLE, reply)
    _ladders, returned = ladders_from(reply)
    week = week_label(g["season"], g["week"])
    cost = note_spend(usage(week), week, f"check {g['match']}", remaining, len(returned))
    say(f"{g['match']}: {len(returned)} market(s) back, {cost} credit(s) spent, {remaining} left")
    return reading(g, reply, say)


def reading(g, reply, say=print):
    """What one real reply reads as: each market's players, how many are
    matched to this season's box scores, and the game judged by the rule."""
    ladders, _returned = ladders_from(reply)
    store = _read(STORE, {}) or {}
    who = players(store)
    index = by_name(who, set(g["teams"].values()))
    for market in MARKETS:
        rows = [(k, v) for k, v in ladders.items() if k[0] == market]
        matched = sum(1 for (m, k), _v in rows if k in index)
        say(f"  {MARKETS[market][1]:<14}{len(rows):>3} player(s), {matched} matched to a box score")
        for (m, k), v in rows[:2]:
            say(f"      {v['name']}: " + ", ".join(f"{line}+ {p:+d}" for line, p in v["rungs"][:5]))
    if not store.get("games"):
        say("  (the player store is empty - run: python3 scripts/ace-props-nfl.py players)")
        return 0
    missing = sorted({v["name"] for (m, k), v in ladders.items() if k not in index})
    if missing:
        say(f"  not matched: {', '.join(missing)}")
    doc = NFL.load()
    board = game_board(g, ladders, who, doc.get("positions") or {}, defence(doc))
    clear = [p for p in board["players"] if p.get("best")]
    say(f"  by the rule: {len(clear)} of {len(board['players'])} ladder(s) clear the bar")
    for p in clear[:6]:
        r = p["best"]
        say(f"      {p['name']} {r['line']}+ {p['words']} at {r['price']:+d} vs {p['opp']} (rank {p['opp_rank']}): {r['why']}")
    return 0


# ------------------------------------------------------------------ the page

def board_json(now=None):
    b = book()
    open_bets, settled, f = nfl_bets(b)
    closes = _read(CLOSES, {}) or {}
    u = usage()
    doc = _read(BOARD, {}) or {}
    week = doc.get("week")

    def row(x):
        clv, close_price = clv_of(x, closes) if (closes.get(x.get("id")) or {}).get("price") is not None else (None, None)
        return dict({k: x.get(k) for k in ("id", "selection", "match", "starts_utc", "price", "stake", "line",
                                            "athlete", "team", "opp", "pos", "opp_rank", "hit_pct", "games",
                                            "p_used", "novig_pct", "edge_pts", "why", "utc", "result", "market",
                                            "profit", "detail", "settled_utc")}, clv_pct=clv, close_price=close_price)
    graded = [row(x) for x in settled]
    with_close = [x for x in graded if x["clv_pct"] is not None and x["result"] in ("won", "lost")]
    real = [x for x in settled if x.get("result") in ("won", "lost")]
    day = et_time.day()
    return {
        "sport": "nfl",
        "board": doc,
        "open": [row(x) for x in open_bets],
        "settled": list(reversed(graded[-40:])),
        "summary": {"profit": round(sum(float(x.get("profit") or 0) for x in settled), 2),
                    "won": sum(1 for x in real if x["result"] == "won"),
                    "lost": sum(1 for x in real if x["result"] == "lost"),
                    "void": len(settled) - len(real), "open": len(open_bets),
                    "today": len(AP.placed_on(open_bets + settled, day)), "cap": DAILY_CAP,
                    "avg_clv_pct": round(sum(x["clv_pct"] for x in with_close) / len(with_close), 2)
                    if with_close else None,
                    "with_close": len(with_close), "beat_close": sum(1 for x in with_close if x["clv_pct"] > 0),
                    "stopped": stop_reason(settled), "bankroll": f["bankroll"]},
        "credits": {"remaining": u.get("remaining"), "week": week, "week_spent": u["weeks"].get(week, 0) if week else 0,
                    "week_cap": WEEK_CREDITS, "reserve": RESERVE, "key": bool(api_key()),
                    "log": list(reversed(u["log"][-10:]))},
        "rules": {"cap": DAILY_CAP, "stake": b.flat_stake(), "stop_usd": STOP_USD, "shrink": AP.SHRINK,
                  "gap_min_pts": AP.GAP_MIN_PTS, "max_favourite": AP.MAX_FAVOURITE,
                  "max_longshot": AP.MAX_LONGSHOT, "bottom": AP.BOTTOM,
                  "markets": {m: w for m, (_s, w, _p) in MARKETS.items()}},
    }


def show_lines():
    d = board_json()
    s, c = d["summary"], d["credits"]
    out = [f"Ace Props NFL: {s['won']} won, {s['lost']} lost, {s['void']} void, ${s['profit']:+,.2f}; "
           f"{s['open']} open; {s['today']}/{s['cap']} placed today; shared bankroll ${s['bankroll']:,.2f}",
           f"credits: {c['remaining'] if c['remaining'] is not None else 'unknown'} left on the NFL account; "
           f"{c['week_spent']} of {c['week_cap']} spent in {c['week'] or 'this week'}"]
    if s["avg_clv_pct"] is not None:
        out.append(f"closing-line value {s['avg_clv_pct']:+.2f}% a bet over {s['with_close']}, "
                   f"beat the close on {s['beat_close']}")
    if s["stopped"]:
        out.append("STOPPED: " + s["stopped"])
    for x in d["open"]:
        out.append(f"  open  {x['selection']} ({x['match']}) {x['price']}: {x['why']}")
    for x in d["settled"][:10]:
        out.append(f"  {x['result']:<5} {x['selection']} ({x['match']}) {x['price']} {float(x['profit'] or 0):+,.2f}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ace Props NFL: the props rule on NFL ladders, on Ace's bankroll")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry", action="store_true")
    sub.add_parser("close")
    c = sub.add_parser("check")
    c.add_argument("--spend", action="store_true")
    c.add_argument("--sample", action="store_true", help="read the reply --spend saved again, free")
    sub.add_parser("players")
    j = sub.add_parser("board")
    j.add_argument("--json", action="store_true")
    sub.add_parser("show")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        print("\n".join(run(a.dry)))
    elif a.cmd == "close":
        print("\n".join(close()))
    elif a.cmd == "check":
        return check(a.spend, sample=a.sample)
    elif a.cmd == "players":
        store, added = update_players()
        print(f"{added} game(s) added; {len(store.get('games') or {})} this season")
    elif a.cmd == "board":
        print(json.dumps(board_json()))
    else:
        print("\n".join(show_lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
