"""What each NFL defense allows by position (nfl-defense.py).

    python3 -m unittest tests.test_defense -b

Owner, 2026-10-19: "Can we find and implement a stat like the 49ers are
allowing the 2nd most fantasy points/yards/touchdowns to running backs/
quarterbacks". Read against a real ESPN box score (TB at DAL, week 5 of
2026, trimmed to the header and the box score): every parser bug here has
been a format assumption.
"""

import importlib.util
import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIX = ROOT / "tests" / "fixtures"


def load():
    spec = importlib.util.spec_from_file_location("nfl_defense_test", SCRIPTS / "nfl-defense.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReadingABoxScore(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = load()
        cls.summary = json.loads((FIX / "espn-nfl-summary-401872980.json").read_text())

    def test_each_players_line_by_stat_name(self):
        lines = self.m.lines_of(self.summary)
        self.assertEqual(set(lines), {"TB", "DAL"})
        daniels, irving = lines["TB"]["4596472"], lines["TB"]["4596448"]
        self.assertEqual((daniels["passing_yards"], daniels["passing_tds"]), (189.0, 1.0))
        self.assertEqual((irving["rushing_yards"], irving["rushing_attempts"]), (165.0, 21.0))

    def test_a_defenders_interception_is_not_charged_to_anyone(self):
        # Yaya Diaby (TB) caught one. The defensive table has an
        # `interceptions` column too; read, it would count as a pick thrown.
        lines = self.m.lines_of(self.summary)
        self.assertNotIn("4686334", lines["TB"])
        thrown = sum(r.get("interceptions", 0) for r in lines["DAL"].values())
        dal_qb = [r for r in lines["DAL"].values() if "passing_yards" in r]
        self.assertEqual(thrown, sum(r.get("interceptions", 0) for r in dal_qb), "only passers throw them")

    def test_the_offence_is_charged_to_the_defense_across_the_field(self):
        lines = self.m.lines_of(self.summary)
        positions = {"4596472": "QB", "4596448": "RB"}
        got = self.m.allowed(lines, self.m.opponents(self.summary), positions)
        self.assertEqual(got["DAL"]["QB"]["passing_yards"], 189.0, "TB's quarterback, against DAL")
        self.assertGreaterEqual(got["DAL"]["RB"]["rushing_yards"], 165.0)
        self.assertNotIn("QB", got["TB"], "TB's own quarterback is never charged to TB")

    def test_what_was_caught_is_what_was_thrown(self):
        # Every passing yard is somebody's receiving yard: a check on the
        # whole attribution, not just one player.
        lines = self.m.lines_of(self.summary)
        for team, players in lines.items():
            thrown = sum(r.get("passing_yards", 0) for r in players.values())
            caught = sum(r.get("receiving_yards", 0) for r in players.values())
            self.assertEqual(thrown, caught, team)

    def test_the_stats_are_peters_markets_by_his_own_names(self):
        # One fact, one place: the column each market is read from.
        for market, spec in self.m.pf.MARKETS.items():
            self.assertEqual(self.m.STATS[market], spec["stat"])


class RankingTheDefenses(unittest.TestCase):

    def setUp(self):
        self.m = load()

    def game(self, eid, week, **teams):
        return eid, {"week": week, "allowed": {t: {"RB": {"rushing_yards": y}} for t, y in teams.items()}}

    def test_one_is_the_most_allowed_and_ties_share(self):
        doc = {"games": dict([self.game("1", 1, SF=150, DAL=80), self.game("2", 1, NYG=150, KC=40)])}
        r = self.m.ranks(doc)
        self.assertEqual((r["SF"]["RB"]["rushing_yards"]["rank"], r["NYG"]["RB"]["rushing_yards"]["rank"]), (1, 1))
        self.assertEqual(r["SF"]["RB"]["rushing_yards"]["said"], "most")
        self.assertEqual(r["DAL"]["RB"]["rushing_yards"]["said"], "2nd fewest")
        self.assertEqual(r["KC"]["RB"]["rushing_yards"]["said"], "fewest")

    def test_it_is_a_game_average_not_a_total(self):
        # A team with a bye has played fewer games; totals would rank it kinder.
        doc = {"games": dict([self.game("1", 1, SF=100, DAL=90), self.game("2", 2, SF=100, NYG=10),
                              self.game("3", 3, DAL=90, NYG=10)])}
        r = self.m.ranks(doc)
        self.assertEqual(r["SF"]["RB"]["rushing_yards"]["avg"], 100.0)
        self.assertEqual(r["SF"]["games"], 2)

    def test_ppr_fantasy_points(self):
        row = {"passing_yards": 250, "passing_tds": 2, "interceptions": 1, "rushing_yards": 20,
               "receptions": 5, "receiving_yards": 50, "receiving_tds": 1, "fumbles_lost": 1}
        self.assertEqual(self.m.fantasy(row), 10 + 8 - 2 + 2 + 5 + 5 + 6 - 2)

    def test_ordinals(self):
        self.assertEqual([self.m.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd"])


class UpdatingReadsEachGameOnce(unittest.TestCase):
    """A finished game never changes, so it is read once."""

    def setUp(self):
        self.m = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "defense.json"
        self.summary = json.loads((FIX / "espn-nfl-summary-401872980.json").read_text())
        self.asked = []

    def get(self, url):
        self.asked.append(url)
        api = self.m.pf.API
        if url == f"{api}/scoreboard":
            return {"season": {"year": 2026}, "week": {"number": 1},
                    "events": [{"id": "401872980", "competitions": [{"status": {"type": {"name": "STATUS_FINAL"}}}]}]}
        if url == f"{api}/summary?event=401872980":
            return self.summary
        if url == f"{api}/teams":
            return {"sports": [{"leagues": [{"teams": [{"team": {"id": "27"}}]}]}]}
        if url == f"{api}/teams/27/roster":
            return {"athletes": [{"position": "offense", "items": [{"id": "4596472", "position": {"abbreviation": "QB"}}]}]}
        if url.startswith(self.m.pf.LOGS):
            return {"athlete": {"position": {"abbreviation": "RB"}}}   # off every roster: asked for himself
        raise AssertionError(url)

    def test_new_games_only_and_positions_from_rosters_first(self):
        said = []
        self.assertEqual(self.m.update(get=self.get, say=said.append, path=self.path), 1)
        doc = json.loads(self.path.read_text())
        self.assertEqual(doc["positions"]["4596472"], "QB", "from the roster")
        self.assertEqual(doc["through_week"], 1)
        self.assertEqual(doc["games"]["401872980"]["allowed"]["DAL"]["QB"]["passing_yards"], 189.0)
        self.asked.clear()
        self.assertEqual(self.m.update(get=self.get, say=said.append, path=self.path), 0)
        self.assertEqual(self.asked, [f"{self.m.pf.API}/scoreboard"], "the second run reads no box score")


class OnPetersPage(unittest.TestCase):

    def test_the_page_the_route_and_the_timer(self):
        page = (ROOT / "mission-control-api" / "public" / "peter.html").read_text()
        self.assertIn("fetch('/api/props/defense'", page)
        self.assertIn("${defLine(f)}", page, "on every forecast card")
        self.assertIn("${defTable(g)}", page, "the picked game's two defenses")
        js = (ROOT / "mission-control-api" / "props.js").read_text()
        self.assertIn("app.get('/api/props/defense'", js)
        self.assertIn("[DEFENSE, 'table', '--json']", js, "the page reads; it never fetches from ESPN")
        unit = (ROOT / "deploy" / "peter-defense.service").read_text()
        self.assertIn("scripts/nfl-defense.py update", unit)
        self.assertIn("OnCalendar=", (ROOT / "deploy" / "peter-defense.timer").read_text())


if __name__ == "__main__":
    unittest.main()
