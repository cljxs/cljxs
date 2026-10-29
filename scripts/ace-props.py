#!/usr/bin/env python3
"""
ace-props.py - Ace Props: NBA player points milestones, by plain code, as a
separate book on Ace's bankroll.

    ace-props.py run [--part early|late] [--dry]   settle, refresh, then bet up to the day's cap
    ace-props.py close            settle what finished; keep each open bet's latest price (its close)
    ace-props.py scan             today's board without betting, saved for the page
    ace-props.py boxscores [--since YYYY-MM-DD]   fill the box-score store (the first run reads last season)
    ace-props.py board --json     what the page draws
    ace-props.py show             the book in words

THE IDEA (owner, 2026-10-26, from @linemakersports on TikTok, "Easiest Way
to Predict an NBA Players Points"): pick a player whose opponent is bottom
10 in the league at defending his position; his "true chance" of clearing
each alternate line (15+, 20+, 25+ ...) is how often he cleared it against
bottom-10 defences at his position; bet when that beats the chance the
price implies.

THE GUARDS (owner's option A, against the two ways that rule fools itself -
twenty games is a small sample, and the best-looking edge of hundreds each
day is mostly the luckiest one):
  * the hit rate is pulled SHRINK of the way back to the market's chance,
    as Ace's own estimates are;
  * MIN_GAMES games against those defences, or no bet;
  * no price shorter than MAX_FAVOURITE or longer than MAX_LONGSHOT;
  * the shrunk chance must clear the price's own by GAP_MIN_PTS.

THE DATA is free and ESPN's: DraftKings' "Points Milestones" ladder with its
prices (sports.core.api.espn.com propBets - for the NBA they carry prices),
and every box score, which gives both halves of the rule at once: points
allowed to guards, forwards and centres (ESPN labels positions G, F and C
only, not the five the video names), and every player's points against
every opponent. A team's defence is its last DEFENSE_GAMES games, across
seasons, so it means something in the first weeks of one.

SEPARATE FROM ACE, ON HIS MONEY. The bets go into Ace's own state/bets.jsonl
with book "props", so his bankroll is theirs too. ace-book.py keeps them out
of everything his preregistration measures: his MAX_OPEN_BETS, his fault
stop, his closing-line value, his no-AI comparison. They have their own cap,
DAILY_CAP a day by when they were placed, and their own stop at STOP_USD of
settled losses. Graded on the close, too: the last DraftKings price seen for
the same player and line before the tip (with the book's margin in it, so
the closing-line value is if anything understated).

No model is woken. Standard library only.
"""

import argparse
import importlib.util
import json
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
STORE = DATA / "props-boxscores.json"
BOARD = DATA / "props-board.json"
CLOSES = DATA / "props-close.json"

BOOK = "props"                 # the tag ace-book.py keeps out of Ace's measures
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
LADDER = "Points Milestones"   # DraftKings' 15+ / 20+ / 25+ ladder, as ESPN names it

DAILY_CAP = 2                  # bets a day (Eastern, by when placed) - the owner's number
STOP_USD = 1000.0              # settled props losses that pause props; Ace's own betting is untouched
SHRINK = 0.6                   # the same 60% as Ace's, its own number: one never moves the other
GAP_MIN_PTS = 2.0
MIN_GAMES = 10
MAX_FAVOURITE = -400           # an 80% price: past it one loss costs four wins
MAX_LONGSHOT = 400             # a 20% price
BOTTOM = 10                    # "bottom 10 in the league at defending that position"
DEFENSE_GAMES = 40             # a team's defence is its last 40 games, across seasons
POSITIONS = ("G", "F", "C")
SEASON_TYPES = (2, 3)          # regular season and playoffs; never preseason
FIRST_SEASON_DAY = "2025-10-21"   # last season's first day: the store starts there
# Two runs a day, split by tip-off: early takes games before 17:45 ET (weekend
# afternoons), late the evening slate. Both bet within the one daily cap.
SPLIT_ET = (17, 45)
# Which half a run takes is decided by the clock at a time between the two
# runs - NOT at SPLIT_ET. Until 2026-10-29 it was SPLIT_ET, and the 17:30 run,
# being before 17:45, took the afternoon half: on a weekday that is no game at
# all, so no evening game was ever judged and no bet placed in three days.
LATE_RUN_FROM = (15, 0)
NOT_WITHIN_MIN = 20            # never bet a game starting in less than this
VOID_AFTER_HOURS = 72
GAP = 0.4
SAVE_EVERY = 50
STAMP = "%Y-%m-%d %H:%M:%S"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def book():
    b = _load("ace_book_props", "ace-book.py")
    b.STATE = ROOT / "agents" / "ace" / "state"
    b.BETS, b.BANK = b.STATE / "bets.jsonl", b.STATE / "bankroll.json"
    return b


def espn():
    """ace-fetch.py's ESPN host and its get(): the datacentre-safe host and
    the retries are decided there."""
    return _load("ace_fetch_props", "ace-fetch.py")


def utc_now():
    return datetime.now(timezone.utc)


def parse_utc(s):
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ", STAMP):
        try:
            return datetime.strptime(str(s or ""), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _read(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _write(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    tmp.replace(path)


# ------------------------------------------------------------------ prices

def implied(american):
    """The chance a price implies, margin included, in percent."""
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a <= -100:
        return round(-a / (-a + 100) * 100, 2)
    if a >= 100:
        return round(100 / (a + 100) * 100, 2)
    return None


def ladder_from(items):
    """{athlete_id: [(line, price), ...]} off ESPN's propBets items - the
    Points Milestones ladder only, each rung with DraftKings' price."""
    out = {}
    for it in items or []:
        if ((it.get("type") or {}).get("name")) != LADDER:
            continue
        found = re.search(r"/athletes/(\d+)", ((it.get("athlete") or {}).get("$ref")) or "")
        line = (((it.get("current") or {}).get("target") or {}).get("value"))
        price = (((it.get("odds") or {}).get("american") or {}).get("value"))
        try:
            price = int(str(price).replace("+", ""))
        except (TypeError, ValueError):
            continue
        if not found or not isinstance(line, (int, float)):
            continue
        rungs = out.setdefault(found.group(1), {})
        rungs[float(line)] = price
    return {a: sorted(r.items()) for a, r in out.items()}


def fetch_ladder(event_id, get=None):
    """(ladder, note) for one game. Never raises: no ladder is no board."""
    get = get or espn().get
    try:
        odds = get(f"{CORE}/events/{event_id}/competitions/{event_id}/odds")
    except Exception as exc:
        return {}, f"no odds: {exc}"
    for item in odds.get("items") or []:
        ref = ((item.get("propBets") or {}).get("$ref")) or ""
        if not ref:
            continue
        base = ref.replace("http://", "https://", 1).split("?")[0]
        items, page, pages = [], 1, 1
        try:
            while page <= pages:
                d = get(f"{base}?limit=1000&page={page}")
                items += d.get("items") or []
                pages = int(d.get("pageCount") or 1)
                page += 1
        except Exception as exc:
            return {}, f"no props: {exc}"
        return ladder_from(items), None
    return {}, "no book has posted player props for this game yet"


# ------------------------------------------------------------------ box scores

def position(abbr):
    """ESPN's G / F / C, or a PG / SF / ... should it send one; a G-F is a G."""
    a = str(abbr or "").upper()
    if a.startswith("C"):
        return "C"
    if a.startswith(("G", "PG", "SG")):
        return "G"
    if a.startswith(("F", "SF", "PF")):
        return "F"
    return None


def rows_from(summary):
    """[[team, opp, athlete_id, name, pos, minutes, points], ...] for every
    player in a finished game's box score - the one shape the store keeps."""
    teams = (summary.get("boxscore") or {}).get("players") or []
    abbrs = [((t.get("team") or {}).get("abbreviation")) for t in teams]
    out = []
    for i, t in enumerate(teams):
        team = abbrs[i]
        opp = abbrs[1 - i] if len(abbrs) == 2 else None
        for block in t.get("statistics") or []:
            labels = block.get("labels") or []
            if "PTS" not in labels or "MIN" not in labels:
                continue
            for a in block.get("athletes") or []:
                stats = a.get("stats") or []
                ath = a.get("athlete") or {}
                if a.get("didNotPlay") or len(stats) != len(labels):
                    continue
                try:
                    minutes = int(str(stats[labels.index("MIN")]).split(":")[0] or 0)
                    pts = int(stats[labels.index("PTS")])
                except (TypeError, ValueError):
                    continue
                out.append([team, opp, str(ath.get("id")), ath.get("displayName"),
                            position((ath.get("position") or {}).get("abbreviation")), minutes, pts])
    return out


def status_of(summary):
    return ((((summary.get("header") or {}).get("competitions") or [{}])[0].get("status") or {})
            .get("type") or {}).get("name")


def board_events(day, get):
    """The NBA scoreboard for an Eastern day, YYYYMMDD."""
    return (get(f"{espn().BASE}/basketball/nba/scoreboard?dates={day}") or {}).get("events") or []


def update_store(since=None, until=None, get=None, store=None, sleep=time.sleep):
    """Add every finished regular-season or playoff game from `since` to
    `until` (Eastern days) to the store. Days already complete are skipped;
    a day with a game not yet final is read again next time."""
    get = get or espn().get
    store = store if store is not None else _read(STORE, {"games": {}, "done": []})
    store.setdefault("games", {})
    done = set(store.setdefault("done", []))
    today = et_time.eastern_now().date()
    day = datetime.strptime(since or FIRST_SEASON_DAY, "%Y-%m-%d").date()
    last = datetime.strptime(until, "%Y-%m-%d").date() if until else today
    added = 0
    while day <= last:
        key = day.strftime("%Y%m%d")
        if key not in done:
            try:
                events = board_events(key, get)
            except Exception as exc:
                print(f"ace-props: scoreboard {key} not read - {exc}", file=sys.stderr)
                day += timedelta(days=1)
                continue
            complete = True
            for e in events:
                if ((e.get("season") or {}).get("type")) not in SEASON_TYPES:
                    continue
                state = e["competitions"][0]["status"]["type"]["name"]
                if state != "STATUS_FINAL":
                    # Postponed or cancelled will not come back to this day;
                    # anything else (scheduled, halftime ...) is read again.
                    complete = complete and state in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED")
                    continue
                if e["id"] in store["games"]:
                    continue
                try:
                    summary = get(f"{espn().BASE}/basketball/nba/summary?event={e['id']}")
                except Exception as exc:
                    print(f"ace-props: box score {e['id']} not read - {exc}", file=sys.stderr)
                    complete = False
                    continue
                rows = rows_from(summary)
                if rows:
                    store["games"][e["id"]] = {"date": day.isoformat(), "rows": rows}
                    added += 1
                    if added % SAVE_EVERY == 0:      # a first fill that is stopped keeps what it read
                        store["done"] = sorted(done)
                        _write(STORE, store)
                sleep(GAP)
            if complete and day < today:
                done.add(key)
        day += timedelta(days=1)
    store["done"] = sorted(done)
    _write(STORE, store)
    return store, added


# ------------------------------------------------------------------ the rule

def defense(store, before=None):
    """{position: {"allowed": {team: points a game}, "rank": {team: 1 = most allowed},
    "bottom": [the BOTTOM teams that allow the most]}} over each team's last
    DEFENSE_GAMES games before `before` (an ISO date)."""
    per = {}
    for g in sorted(store.get("games", {}).values(), key=lambda g: g["date"]):
        if before and g["date"] >= before:
            continue
        allowed = {}
        for team, opp, _aid, _name, pos, minutes, pts in g["rows"]:
            if pos in POSITIONS and opp:
                allowed.setdefault(opp, {p: 0 for p in POSITIONS})[pos] += pts
        for team, by in allowed.items():
            per.setdefault(team, []).append(by)
    out = {}
    for pos in POSITIONS:
        avg = {t: round(sum(g[pos] for g in games[-DEFENSE_GAMES:]) / len(games[-DEFENSE_GAMES:]), 1)
               for t, games in per.items() if games}
        order = sorted(avg, key=lambda t: (-avg[t], t))
        out[pos] = {"allowed": avg, "rank": {t: i + 1 for i, t in enumerate(order)}, "bottom": order[:BOTTOM]}
    return out


def players(store):
    """{athlete_id: {"name", "team", "pos", "games": [(date, opp, minutes, points)]}},
    team and position from his latest game."""
    out = {}
    for g in sorted(store.get("games", {}).values(), key=lambda g: g["date"]):
        for team, opp, aid, name, pos, minutes, pts in g["rows"]:
            p = out.setdefault(aid, {"games": []})
            p.update(name=name, team=team, pos=pos or p.get("pos"))
            p["games"].append((g["date"], opp, minutes, pts))
    return out


def judge(rung_line, price, games, against):
    """One rung for one player: the hit rate against `against` (the bottom
    teams at his position), the shrunk chance, the edge, and why not."""
    played = [g for g in games if g[1] in against and g[2] > 0]
    n = len(played)
    q = implied(price)
    hit = round(sum(1 for g in played if g[3] >= rung_line) / n * 100, 1) if n else None
    out = {"line": rung_line, "price": price, "market_pct": q, "games": n, "hit_pct": hit,
           "p_used": None, "edge_pts": None, "clears": False, "why": ""}
    if q is None:
        out["why"] = "no price"
    elif n < MIN_GAMES:
        out["why"] = f"{n} game(s) against these defences - {MIN_GAMES} needed"
    else:
        p_used = round(q + (1 - SHRINK) * (hit - q), 1)
        out.update(p_used=p_used, edge_pts=round(p_used - q, 1))
        if price < 0 and price < MAX_FAVOURITE:
            out["why"] = f"{price} is shorter than {MAX_FAVOURITE}"
        elif price > 0 and price > MAX_LONGSHOT:
            out["why"] = f"+{price} is longer than +{MAX_LONGSHOT}"
        elif out["edge_pts"] < GAP_MIN_PTS:
            out["why"] = (f"cleared it {hit:g}% of {n}; counted as {p_used:g}% against the price's "
                          f"{q:g}% - {out['edge_pts']:+.1f} pts, the bar is +{GAP_MIN_PTS:g}")
        else:
            out["clears"] = True
            out["why"] = f"cleared it {hit:g}% of {n}; counted as {p_used:g}% vs {q:g}%: {out['edge_pts']:+.1f} pts"
    return out


def game_board(event, ladder, store_players, dfn):
    """Every player on a game's ladder, judged."""
    comp = event["competitions"][0]
    teams = {t["homeAway"]: t["team"]["abbreviation"] for t in comp["competitors"]}
    rows = []
    for aid, rungs in ladder.items():
        p = store_players.get(aid)
        if not p:
            rows.append({"athlete_id": aid, "name": None, "why": "no box score of his yet"})
            continue
        if p["team"] not in teams.values():
            rows.append({"athlete_id": aid, "name": p["name"], "why": f"last played for {p['team']}"})
            continue
        opp = teams["away"] if p["team"] == teams["home"] else teams["home"]
        pos = p.get("pos")
        if pos not in POSITIONS:
            rows.append({"athlete_id": aid, "name": p["name"], "why": "no position on his box scores"})
            continue
        d = dfn.get(pos) or {}
        row = {"athlete_id": aid, "name": p["name"], "team": p["team"], "opp": opp, "pos": pos,
               "opp_rank": (d.get("rank") or {}).get(opp), "opp_allowed": (d.get("allowed") or {}).get(opp),
               "rungs": [], "best": None, "why": ""}
        if opp not in (d.get("bottom") or []):
            row["why"] = f"{opp} is not bottom {BOTTOM} against {pos}s (rank {row['opp_rank']} of {len(d.get('rank') or {})})"
        else:
            row["rungs"] = [judge(line, price, p["games"], set(d["bottom"])) for line, price in rungs]
            ok = [r for r in row["rungs"] if r["clears"]]
            row["best"] = max(ok, key=lambda r: r["edge_pts"]) if ok else None
            row["why"] = row["best"]["why"] if row["best"] else "no rung clears the bar"
        rows.append(row)
    rows.sort(key=lambda r: (r.get("best") is None, -((r.get("best") or {}).get("edge_pts") or 0), str(r.get("name"))))
    return {"event_id": event["id"], "match": event.get("shortName"), "starts_utc": event.get("date"),
            "status": comp["status"]["type"]["name"], "players": rows}


# ------------------------------------------------------------------ the book

def props_bets(b=None):
    b = b or book()
    f = b.fold(b.events())
    return ([x for x in f["open"] if b.is_props(x)], [x for x in f["settled"] if b.is_props(x)], f)


def stop_reason(settled):
    lost = -sum(float(x.get("profit") or 0) for x in settled)
    if lost >= STOP_USD:
        return f"props have lost ${lost:,.2f} settled - the ${STOP_USD:,.0f} props stop. Ace's own betting is not affected"
    return None


def placed_on(bets, day):
    return [x for x in bets if et_time.day(et_time.to_eastern(x.get("utc"))) == day]


def pick(boards, open_bets, settled, day, now=None):
    """The bets to place now: the best clearing rung per player, best edge
    first, one per game and per player a day, up to DAILY_CAP a day."""
    now = now or utc_now()
    if stop_reason(settled):
        return [], stop_reason(settled)
    today = placed_on(open_bets + settled, day)
    room = DAILY_CAP - len(today)
    if room <= 0:
        return [], f"{len(today)} props bet(s) placed today - the cap is {DAILY_CAP}"
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
        out.append({"book": BOOK, "sport": "nba", "event_id": g["event_id"], "match": g["match"],
                    "starts_utc": g["starts_utc"], "selection": f"{p['name']} {r['line']:g}+ pts",
                    "side": "over", "athlete_id": p["athlete_id"], "athlete": p["name"], "line": r["line"],
                    "team": p["team"], "opp": p["opp"], "pos": p["pos"], "opp_rank": p["opp_rank"],
                    "price": r["price"], "novig_pct": r["market_pct"], "hit_pct": r["hit_pct"],
                    "games": r["games"], "p_used": r["p_used"], "edge_pts": r["edge_pts"],
                    "price_source": f"DraftKings via ESPN at {now.strftime(STAMP)} UTC",
                    "why": f"{p['opp']} rank {p['opp_rank']} against {p['pos']}s; {r['why']}"})
    return out, None


def grade(bet, store, now=None):
    """(result, profit, detail) for an open props bet, or None while it waits."""
    now = now or utc_now()
    g = store.get("games", {}).get(str(bet.get("event_id")))
    start = parse_utc(bet.get("starts_utc"))
    if g is None:
        if start and now - start > timedelta(hours=VOID_AFTER_HOURS):
            return "void", 0.0, f"no final box score {VOID_AFTER_HOURS}h after the start"
        return None
    row = next((r for r in g["rows"] if r[2] == str(bet.get("athlete_id"))), None)
    if row is None or row[5] <= 0:
        return "void", 0.0, f"{bet.get('athlete')} did not play - DraftKings voids it"
    pts, line = row[6], float(bet.get("line"))
    if pts >= line:
        d = book().decimal_odds(bet.get("price"))
        return "won", round(float(bet.get("stake") or 0) * (d - 1), 2), f"{bet.get('athlete')} scored {pts}"
    return "lost", -float(bet.get("stake") or 0), f"{bet.get('athlete')} scored {pts}"


def settle(store, b=None, now=None):
    b = b or book()
    now = now or utc_now()
    done = []
    for bet in props_bets(b)[0]:
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


def record_closes(open_bets, get=None, now=None):
    """The latest DraftKings price for every open bet's player and line until
    its game starts; the last one kept is its close."""
    now = now or utc_now()
    closes = _read(CLOSES, {})
    by_game = {}
    for bet in open_bets:
        start = parse_utc(bet.get("starts_utc"))
        if start and start > now:
            by_game.setdefault(str(bet["event_id"]), []).append(bet)
    for event_id, bets in by_game.items():
        ladder, _note = fetch_ladder(event_id, get)
        for bet in bets:
            price = dict(ladder.get(str(bet.get("athlete_id")), [])).get(float(bet["line"]))
            if price is not None:
                closes[bet["id"]] = {"price": price, "at": now.strftime(STAMP)}
    _write(CLOSES, closes)
    return closes


def clv_of(bet, closes, clv=None):
    """The price taken, valued at the close's implied chance: close x decimal
    - 1, in % - ace-clv.py's own clv_at."""
    c = closes.get(bet.get("id"))
    if not c:
        return None, None
    clv = clv or _load("ace_clv_props", "ace-clv.py")
    return clv.clv_at(bet.get("price"), implied(c["price"])), c["price"]


# ------------------------------------------------------------------ runs

def part_of(now_et):
    """The half of the day a run judges: the 12:30 run the early games, the
    17:30 run the evening slate."""
    return "early" if (now_et.hour, now_et.minute) < LATE_RUN_FROM else "late"


def in_part(start_utc, part):
    e = et_time.to_eastern(start_utc)
    if e is None:
        return False
    early = (e.hour, e.minute) < SPLIT_ET
    return early if part == "early" else not early


def scan(part=None, get=None, store=None, now=None):
    """Today's boards (Eastern), judged, saved for the page."""
    get = get or espn().get
    now = now or utc_now()
    day = et_time.day(et_time.to_eastern(now))
    store = store if store is not None else _read(STORE, {"games": {}})
    dfn, who = defense(store, before=day), players(store)
    boards = []
    for e in board_events(day.replace("-", ""), get):
        if ((e.get("season") or {}).get("type")) not in SEASON_TYPES:
            continue
        if part and not in_part(e.get("date"), part):
            continue
        ladder, note = fetch_ladder(e["id"], get)
        g = game_board(e, ladder, who, dfn)
        g["note"] = note
        boards.append(g)
        time.sleep(GAP)
    doc = {"asof_utc": now.strftime(STAMP), "day": day, "part": part, "games": boards,
           "defense": {p: {"bottom": d["bottom"], "allowed": {t: d["allowed"][t] for t in d["bottom"]}}
                       for p, d in dfn.items()},
           "store": {"games": len(store.get("games", {})),
                     "latest": max((g["date"] for g in store.get("games", {}).values()), default=None)}}
    old = _read(BOARD, {})
    if part and old.get("day") == day:
        keep = [g for g in old.get("games", []) if not in_part(g.get("starts_utc"), part)]
        doc["games"] = sorted(keep + boards, key=lambda g: str(g["starts_utc"]))
    _write(BOARD, doc)
    return doc


def run(part=None, dry=False, get=None, now=None):
    now = now or utc_now()
    part = part or part_of(et_time.to_eastern(now))
    store, added = update_store(since=_resume_from(), get=get)
    b = book()
    lines = [f"box scores: {added} new game(s), {len(store['games'])} stored"]
    lines += settle(store, b, now)
    doc = scan(part, get, store, now)
    open_bets, settled, _f = props_bets(b)
    # Only this run's half of the day: the other half's rows on the board
    # were judged at its own run and their prices have moved since.
    mine = [g for g in doc["games"] if in_part(g["starts_utc"], part)]
    bets, why = pick(mine, open_bets, settled, doc["day"], now)
    for f in bets:
        if dry:
            lines.append(f"would bet {f['selection']} ({f['match']}) {f['price']}: {f['why']}")
        else:
            e = b.place(f)
            lines.append(f"BET {e['selection']} ({e['match']}) {e['price']} ${e['stake']:,.2f}: {e['why']}")
    if not bets:
        lines.append(f"no props bet ({part}): {why or 'nothing cleared the bar'}")
    return lines


def close(get=None, now=None):
    now = now or utc_now()
    day = et_time.eastern_now()
    store, _added = update_store(since=(day - timedelta(days=1)).strftime("%Y-%m-%d"), get=get)
    b = book()
    lines = settle(store, b, now)
    open_bets = props_bets(b)[0]
    record_closes(open_bets, get, now)
    return lines + [f"{len(open_bets)} props bet(s) open"]


def _resume_from():
    """Where an incremental update starts: two days before the newest day the
    store holds - a late final is re-read - or last season's first day."""
    store = _read(STORE, {})
    done = store.get("done") or []
    if not done:
        return FIRST_SEASON_DAY
    last = datetime.strptime(done[-1], "%Y%m%d") - timedelta(days=2)
    return last.strftime("%Y-%m-%d")


def board_json(now=None):
    """Everything the page shows, worked out here."""
    b = book()
    open_bets, settled, f = props_bets(b)
    closes = _read(CLOSES, {})
    clv_mod = _load("ace_clv_props", "ace-clv.py")

    def row(x):
        clv, close_price = clv_of(x, closes, clv_mod)
        return dict({k: x.get(k) for k in ("id", "selection", "match", "starts_utc", "price", "stake", "line",
                                            "athlete", "team", "opp", "pos", "opp_rank", "hit_pct", "games",
                                            "p_used", "novig_pct", "edge_pts", "why", "utc", "result",
                                            "profit", "detail", "settled_utc")}, clv_pct=clv, close_price=close_price)
    graded = [row(x) for x in settled]
    with_close = [x for x in graded if x["clv_pct"] is not None and x["result"] in ("won", "lost")]
    real = [x for x in settled if x.get("result") in ("won", "lost")]
    day = et_time.day()
    return {
        "board": _read(BOARD, {}),
        "open": [row(x) for x in open_bets],
        "settled": list(reversed(graded[-40:])),
        "summary": {"profit": round(sum(float(x.get("profit") or 0) for x in settled), 2),
                    "won": sum(1 for x in real if x["result"] == "won"),
                    "lost": sum(1 for x in real if x["result"] == "lost"),
                    "void": len(settled) - len(real), "open": len(open_bets),
                    "today": len(placed_on(open_bets + settled, day)), "cap": DAILY_CAP,
                    "avg_clv_pct": round(sum(x["clv_pct"] for x in with_close) / len(with_close), 2)
                    if with_close else None,
                    "with_close": len(with_close), "beat_close": sum(1 for x in with_close if x["clv_pct"] > 0),
                    "stopped": stop_reason(settled), "bankroll": f["bankroll"]},
        "rules": {"cap": DAILY_CAP, "stake": b.flat_stake(), "stop_usd": STOP_USD, "shrink": SHRINK,
                  "gap_min_pts": GAP_MIN_PTS, "min_games": MIN_GAMES, "max_favourite": MAX_FAVOURITE,
                  "max_longshot": MAX_LONGSHOT, "bottom": BOTTOM, "defense_games": DEFENSE_GAMES},
    }


def show_lines():
    d = board_json()
    s = d["summary"]
    out = [f"Ace Props: {s['won']} won, {s['lost']} lost, {s['void']} void, ${s['profit']:+,.2f}; "
           f"{s['open']} open; {s['today']}/{s['cap']} placed today; shared bankroll ${s['bankroll']:,.2f}"]
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
    ap = argparse.ArgumentParser(description="Ace Props: NBA points milestones by plain code, on Ace's bankroll")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--part", choices=["early", "late"])
    r.add_argument("--dry", action="store_true")
    sub.add_parser("close")
    s = sub.add_parser("scan")
    s.add_argument("--part", choices=["early", "late"])
    x = sub.add_parser("boxscores")
    x.add_argument("--since", help=f"YYYY-MM-DD; default {FIRST_SEASON_DAY}, or where the store left off")
    j = sub.add_parser("board")
    j.add_argument("--json", action="store_true")
    sub.add_parser("show")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        print("\n".join(run(a.part, a.dry)))
    elif a.cmd == "close":
        print("\n".join(close()))
    elif a.cmd == "scan":
        doc = scan(a.part)
        n = sum(1 for g in doc["games"] for p in g["players"] if p.get("best"))
        print(f"{len(doc['games'])} game(s) on the board, {n} player(s) clearing the bar")
    elif a.cmd == "boxscores":
        store, added = update_store(since=a.since or _resume_from())
        print(f"{added} game(s) added; {len(store['games'])} stored")
    elif a.cmd == "board":
        print(json.dumps(board_json()))
    else:
        print("\n".join(show_lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
