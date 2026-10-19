#!/usr/bin/env python3
"""
nfl-defense.py - what each NFL defense allows, by position, ranked.

    nfl-defense.py update          read the season's finished box scores (only new ones)
    nfl-defense.py table [--json]  the ranks, from what update saved - no network
    nfl-defense.py team SF         one defense, every position

Owner, 2026-10-19: "Can we find and implement a stat like the 49ers are
allowing the 2nd most fantasy points/yards/touchdowns to running backs/
quarterbacks". Every finished game's box score says what each player did;
the roster says what he plays. Credit his line to the defense across the
field, average it over that defense's games, and rank the 32 teams: 1 is the
most allowed.

The stats are Peter's markets - read by ESPN's stat names from
props-forecast.py's MARKETS, with its own reader (get, column_of) - plus the
touchdowns, interceptions and lost fumbles that fantasy points need. So
"passing yards" here is the same number Peter forecasts, from the same
column, and the two cannot drift apart.

FANTASY POINTS are full PPR, computed here from the box score (the scoring is
FANTASY below). Two-point conversions are not in a box score, so they are not
counted - for every defense alike, so the ranks are unaffected in practice.

A finished game never changes, so each is read once and kept in
agents/ace/data/defense.json. Free: ESPN's public pages, no model, no key.
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
STORE = ROOT / "agents" / "ace" / "data" / "defense.json"

_spec = importlib.util.spec_from_file_location("props_forecast_defense", SCRIPTS / "props-forecast.py")
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)

# Peter's markets, by his names, plus what fantasy scoring needs.
STATS = {m: spec["stat"] for m, spec in pf.MARKETS.items()}
STATS.update({"rushing_tds": "rushingTouchdowns", "receiving_tds": "receivingTouchdowns",
              "interceptions": "interceptions", "fumbles_lost": "fumblesLost"})
# Only the offence's tables. The defensive "interceptions" table has an
# `interceptions` column too - the ones a defender caught - and reading it
# would charge a quarterback for his opponent's picks.
OFFENCE = ("passing", "rushing", "receiving", "fumbles")

# Full PPR, per unit of each stat.
FANTASY = {"passing_yards": 0.04, "passing_tds": 4, "interceptions": -2,
           "rushing_yards": 0.1, "rushing_tds": 6,
           "receptions": 1, "receiving_yards": 0.1, "receiving_tds": 6,
           "fumbles_lost": -2}

POSITIONS = ("QB", "RB", "WR", "TE")
SAME_AS = {"FB": "RB", "HB": "RB"}          # a fullback's carries are a running back's
WORDS = {"QB": "quarterbacks", "RB": "running backs", "WR": "wide receivers", "TE": "tight ends"}

# What the page shows for each position: the stats a defense is judged on.
SHOWN = {"QB": ("fantasy_points", "passing_yards", "passing_tds", "rushing_yards"),
         "RB": ("fantasy_points", "rushing_yards", "rushing_tds", "receptions", "receiving_yards"),
         "WR": ("fantasy_points", "receptions", "receiving_yards", "receiving_tds", "receiving_targets"),
         "TE": ("fantasy_points", "receptions", "receiving_yards", "receiving_tds", "receiving_targets")}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path=None):
    try:
        d = json.loads(Path(path or STORE).read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(doc, path=None):
    path = Path(path or STORE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(doc, indent=1) + "\n")
    tmp.replace(path)


# ------------------------------------------------------------------ reading

def lines_of(summary):
    """{team_abbr: {athlete_id: {stat: value}}} from a finished game's box
    score, offence tables only, by stat name."""
    out = {}
    for team in ((summary.get("boxscore") or {}).get("players") or []):
        abbr = (team.get("team") or {}).get("abbreviation")
        if not abbr:
            continue
        players = out.setdefault(abbr, {})
        for cat in team.get("statistics") or []:
            if cat.get("name") not in OFFENCE:
                continue
            cols = {name: pf.column_of(cat.get("keys"), stat) for name, stat in STATS.items()}
            for a in cat.get("athletes") or []:
                aid = str((a.get("athlete") or {}).get("id") or "")
                vals = a.get("stats") or []
                if not aid:
                    continue
                row = players.setdefault(aid, {})
                for name, i in cols.items():
                    if i is None or i >= len(vals):
                        continue
                    try:
                        row[name] = row.get(name, 0.0) + float(vals[i])
                    except (TypeError, ValueError):
                        pass                     # "--" for a stat he had no chance at
    return out


def is_final(summary):
    comp = (((summary.get("header") or {}).get("competitions") or [{}])[0])
    return ((comp.get("status") or {}).get("type") or {}).get("name") == "STATUS_FINAL"


def opponents(summary):
    """{abbr: the abbr across the field}."""
    comp = (((summary.get("header") or {}).get("competitions") or [{}])[0])
    teams = [((c.get("team") or {}).get("abbreviation")) for c in comp.get("competitors") or []]
    teams = [t for t in teams if t]
    return {teams[0]: teams[1], teams[1]: teams[0]} if len(teams) == 2 else {}


def position_of(pos):
    pos = SAME_AS.get(pos, pos)
    return pos if pos in POSITIONS else None


def roster_positions(get=None):
    """{athlete_id: position} from all 32 rosters, every group."""
    get = get or pf.get
    out = {}
    teams = (((get(f"{pf.API}/teams").get("sports") or [{}])[0].get("leagues") or [{}])[0].get("teams") or [])
    for t in teams:
        tid = (t.get("team") or {}).get("id")
        if not tid:
            continue
        try:
            roster = get(f"{pf.API}/teams/{tid}/roster")
        except Exception:
            continue
        for group in roster.get("athletes") or []:
            for a in (group.get("items") if isinstance(group, dict) else [group]) or []:
                pos = (a.get("position") or {}).get("abbreviation")
                if a.get("id") and pos:
                    out[str(a["id"])] = pos
    return out


def athlete_position(aid, get=None):
    """One player's position, for one no longer on any roster (released)."""
    try:
        d = (get or pf.get)(f"{pf.LOGS}/{aid}")
    except Exception:
        return None
    return ((d.get("athlete") or {}).get("position") or {}).get("abbreviation")


def allowed(lines, opp, positions):
    """{defense_abbr: {position: {stat: total}}} for one game: each offence's
    lines credited to the defense it played. Players of no known position go
    under "other", counted so a gap is visible rather than silent."""
    out = {}
    for offence, players in lines.items():
        defense = opp.get(offence)
        if not defense:
            continue
        per = out.setdefault(defense, {})
        for aid, row in players.items():
            pos = position_of(positions.get(aid)) or "other"
            tot = per.setdefault(pos, {})
            for k, v in row.items():
                tot[k] = round(tot.get(k, 0.0) + v, 2)
    return out


def fantasy(row):
    return round(sum(row.get(k, 0.0) * w for k, w in FANTASY.items()), 2)


# ------------------------------------------------------------------ the season

def season_events(get=None):
    """(season, week now, [(event_id, week)] of finished regular-season games)."""
    get = get or pf.get
    board = get(f"{pf.API}/scoreboard")
    season = ((board.get("season") or {}).get("year"))
    week = ((board.get("week") or {}).get("number")) or 1
    out = []
    for w in range(1, int(week) + 1):
        b = board if w == week else get(f"{pf.API}/scoreboard?seasontype=2&week={w}&dates={season}")
        for e in b.get("events") or []:
            st = (((e.get("competitions") or [{}])[0].get("status") or {}).get("type") or {}).get("name")
            if st == "STATUS_FINAL":
                out.append((str(e.get("id")), w))
    return season, week, out


def update(get=None, say=print, path=None):
    """Read the finished games not yet read. Returns how many were added."""
    get = get or pf.get
    doc = load(path)
    season, week, events = season_events(get)
    if doc.get("season") != season:
        doc = {"season": season, "games": {}, "positions": doc.get("positions") or {}}
    games, positions = doc.setdefault("games", {}), doc.setdefault("positions", {})
    todo = [(e, w) for e, w in events if e not in games]
    say(f"season {season}, week {week}: {len(events)} finished game(s), {len(todo)} new")
    rosters_read = False
    added = 0
    for eid, w in todo:
        try:
            summary = get(f"{pf.API}/summary?event={eid}")
        except Exception as exc:
            say(f"  {eid}: could not read the box score ({exc}) - next time")
            continue
        if not is_final(summary):
            continue
        lines = lines_of(summary)
        unknown = {aid for players in lines.values() for aid in players if aid not in positions}
        if unknown and not rosters_read:
            positions.update(roster_positions(get))
            rosters_read = True
            unknown = {aid for aid in unknown if aid not in positions}
        for aid in unknown:
            pos = athlete_position(aid, get)
            if pos:
                positions[aid] = pos
        games[eid] = {"week": w, "allowed": allowed(lines, opponents(summary), positions)}
        added += 1
    doc["updated_utc"] = _now()
    doc["through_week"] = max([g["week"] for g in games.values()], default=0)
    save(doc, path)
    say(f"  {added} game(s) added - {len(games)} in all, through week {doc['through_week']}")
    return added


def ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def ranks(doc):
    """{team: {"games": n, pos: {stat: {"avg", "rank", "said"}}}} - rank 1 is
    the most allowed a game. Ties share a rank. `said` is the words the page
    prints: "2nd most", or from the other end, "3rd fewest"."""
    totals, played = {}, {}
    for g in (doc.get("games") or {}).values():
        for team, per in (g.get("allowed") or {}).items():
            played[team] = played.get(team, 0) + 1
            t = totals.setdefault(team, {})
            for pos in POSITIONS:
                row = dict(per.get(pos) or {})
                row["fantasy_points"] = fantasy(row)
                acc = t.setdefault(pos, {})
                for k, v in row.items():
                    acc[k] = acc.get(k, 0.0) + v
    out = {team: {"games": n} for team, n in played.items()}
    stats = set(STATS) | {"fantasy_points"}
    for pos in POSITIONS:
        for stat in stats:
            avgs = {team: round(totals[team].get(pos, {}).get(stat, 0.0) / played[team], 2)
                    for team in played}
            n = len(avgs)
            for team, avg in avgs.items():
                rank = 1 + sum(1 for v in avgs.values() if v > avg)          # 1 = the most
                from_bottom = 1 + sum(1 for v in avgs.values() if v < avg)   # 1 = the fewest
                said = ("most" if rank == 1 else f"{ordinal(rank)} most") if rank <= (n + 1) // 2 else \
                       ("fewest" if from_bottom == 1 else f"{ordinal(from_bottom)} fewest")
                out[team].setdefault(pos, {})[stat] = {"avg": avg, "rank": rank, "said": said}
    return out


def table(doc=None):
    doc = doc if doc is not None else load()
    return {"season": doc.get("season"), "through_week": doc.get("through_week"),
            "updated_utc": doc.get("updated_utc"), "teams": ranks(doc) if doc.get("games") else {},
            "shown": SHOWN, "words": WORDS, "of": len({t for g in (doc.get("games") or {}).values()
                                                       for t in (g.get("allowed") or {})})}


# ------------------------------------------------------------------ commands

def label(stat):
    return stat.replace("_tds", " TDs").replace("_", " ")


def cmd_team(abbr):
    t = table()
    team = (t["teams"] or {}).get(abbr.upper())
    if not team:
        print(f"no games for {abbr.upper()} yet - run: nfl-defense.py update", file=sys.stderr)
        return 1
    print(f"{abbr.upper()} defense, {team['games']} game(s), through week {t['through_week']} "
          f"(rank 1 = allows the most)\n")
    for pos in POSITIONS:
        print(f"  vs {WORDS[pos]}")
        for stat in SHOWN[pos]:
            r = team[pos][stat]
            print(f"    {label(stat):<20}{r['avg']:>7.1f} a game   {r['said']}")
    return 0


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "update":
        try:
            update()
        except Exception as exc:
            print(f"could not update: {exc}", file=sys.stderr)
            return 1
        return 0
    if cmd == "table":
        t = table()
        if "--json" in argv:
            print(json.dumps(t))
            return 0
        for team in sorted(t["teams"]):
            r = t["teams"][team]
            print(f"  {team:<4} {r['games']} gm   " + "   ".join(
                f"{pos} {r[pos]['fantasy_points']['said']:<10}" for pos in POSITIONS))
        return 0
    if cmd == "team" and len(argv) > 1:
        return cmd_team(argv[1])
    print("usage: nfl-defense.py update | table [--json] | team <ABBR>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
