"""Ace Props: NBA points milestones by plain code, on Ace's bankroll and out
of his measures (scripts/ace-props.py).

    python3 -m unittest tests.test_ace_props -b

Owner, 2026-10-26, from a @linemakersports TikTok ("Easiest Way to Predict
an NBA Players Points"), option A: the rule with guards, a separate page in
Ace's house, two bets a day not counted in his four, the same bankroll. The
ladder, box score and scoreboard are real ESPN replies (tests/fixtures).
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(SCRIPTS))

LADDER = json.loads((FIXTURES / "espn-nba-propbets-401909868.json").read_text())
SUMMARY = json.loads((FIXTURES / "espn-nba-summary-401909850.json").read_text())
BOARD = json.loads((FIXTURES / "espn-nba-scoreboard-2026-10-25-seasons.json").read_text())


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PropsCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "agents/ace/state").mkdir(parents=True)
        (self.root / "agents/ace/data").mkdir(parents=True)
        (self.root / "scripts").symlink_to(SCRIPTS)
        (self.root / "agents/ace/state/bankroll.json").write_text(json.dumps(
            {"starting_bankroll": 10000.0, "bankroll": 10000.0, "cycle_count": 3}))
        env = unittest.mock.patch.dict(os.environ, {"ECOSYSTEM_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.p = load(f"ace_props_test_{id(self)}", "ace-props.py")
        self.b = self.p.book()
        # Patched for this test only. `self.p.time` is the one `time` module of
        # the whole run: assigning to it left sleep a no-op for every later
        # test, and tiktok-kit.py, loaded afterwards, bound that no-op as its
        # lock wait - so test_shop's OneKitAtATime gave up at once (2026-10-26).
        nap = unittest.mock.patch.object(self.p.time, "sleep", lambda s: None)
        nap.start()
        self.addCleanup(nap.stop)

    def store(self, games):
        """games: [(date, [[team, opp, aid, name, pos, minutes, points], ...]), ...]"""
        return {"games": {f"g{i}": {"date": d, "rows": rows} for i, (d, rows) in enumerate(games)}, "done": []}

    def season(self, aid="6583", name="Player One", pos="F", team="AAA", vs=("BAD",), pts=None, n=20):
        """n games of one player against `vs`, plus a league in which BAD
        allows the most points to forwards."""
        pts = pts or [25] * n
        games = []
        for i in range(n):
            day = (datetime(2026, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            games.append((day, [[team, vs[i % len(vs)], aid, name, pos, 34, pts[i]]]))
        # the rest of the league: BAD gives up the most to forwards, his own
        # games included
        for i, t in enumerate(["BAD", "T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09", "T10", "T11", "T12"]):
            games.append(("2026-02-01", [["OPP", t, f"x{i}", "Someone", "F", 30, 40 if t == "BAD" else 15 - i]]))
        return self.store(games)

    def bet(self, **kw):
        f = {"book": "props", "sport": "nba", "event_id": "401909868", "match": "AAA @ BBB",
             "starts_utc": "2026-10-26T23:00Z", "selection": "Player One 20+ pts", "side": "over",
             "athlete_id": "6583", "athlete": "Player One", "line": 20.0, "price": -179}
        f.update(kw)
        return self.b.place(f)


class TheLadderAndTheBoxScore(PropsCase):

    def test_draftkings_ladder_with_its_prices(self):
        lad = self.p.ladder_from(LADDER["items"])
        self.assertEqual(lad["6583"][:6], [(12.0, -1700), (15.0, -610), (18.0, -287), (20.0, -179),
                                          (22.0, -118), (25.0, 156)])
        self.assertEqual(len(lad), 4, "Total Points rows are not the ladder")

    def test_the_chance_a_price_implies(self):
        self.assertEqual((self.p.implied(-610), self.p.implied(156), self.p.implied(-100)), (85.92, 39.06, 50.0))

    def test_every_player_in_a_real_box_score(self):
        rows = self.p.rows_from(SUMMARY)
        boozer = next(r for r in rows if r[3] == "Cameron Boozer")
        self.assertEqual(boozer, ["MEM", "GS", "5041935", "Cameron Boozer", "F", 37, 25])
        self.assertEqual({r[0] for r in rows}, {"MEM", "GS"})
        self.assertTrue(all(r[4] in ("G", "F", "C") for r in rows), "ESPN labels G, F and C")

    def test_positions(self):
        self.assertEqual([self.p.position(x) for x in ("G", "PG", "SG", "F", "SF", "PF", "C", "G-F", "", None)],
                         ["G", "G", "G", "F", "F", "F", "C", "G", None, None])

    def test_the_store_skips_preseason_and_keeps_finals(self):
        def get(url):
            return {"events": BOARD["events"]} if "scoreboard" in url else SUMMARY
        store, added = self.p.update_store("2026-10-25", "2026-10-25", get=get, store={"games": {}, "done": []},
                                           sleep=lambda s: None)
        finals = [e["id"] for e in BOARD["events"] if e["competitions"][0]["status"]["type"]["name"] == "STATUS_FINAL"]
        self.assertEqual(sorted(store["games"]), sorted(finals))
        self.assertIn("20261025", store["done"])
        pre = {"events": [dict(BOARD["events"][0], id="999", season={"year": 2026, "type": 1})]}
        store, added = self.p.update_store("2025-10-10", "2025-10-10", get=lambda u: pre if "scoreboard" in u else SUMMARY,
                                           store={"games": {}, "done": []}, sleep=lambda s: None)
        self.assertEqual(added, 0, "preseason games are never counted")


class TheRuleAndItsGuards(PropsCase):

    def test_bottom_ten_from_points_allowed_to_the_position(self):
        d = self.p.defense(self.season())
        self.assertEqual(d["F"]["bottom"][0], "BAD")
        self.assertEqual(len(d["F"]["bottom"]), 10)
        self.assertEqual(d["F"]["rank"]["BAD"], 1)

    def test_a_teams_defence_is_its_last_forty_games(self):
        old = [("2025-11-01", [["X", "GOOD", "a", "A", "G", 30, 60]])] * 1
        recent = [((datetime(2026, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d"), [["X", "GOOD", "a", "A", "G", 30, 10]])
                  for i in range(40)]
        d = self.p.defense(self.store(old + recent))
        self.assertEqual(d["G"]["allowed"]["GOOD"], 10.0, "the 60-point night is 41 games back")

    def test_the_hit_rate_is_shrunk_toward_the_price(self):
        games = [("2026-01-01", "BAD", 34, 25)] * 18 + [("2026-01-02", "BAD", 34, 10)] * 2     # 90% of 20
        r = self.p.judge(20.0, -300, games, {"BAD"})                                         # 75%
        self.assertEqual((r["hit_pct"], r["market_pct"], r["p_used"], r["edge_pts"], r["clears"]),
                         (90.0, 75.0, 81.0, 6.0, True))

    def test_the_videos_own_example_is_refused_at_minus_600(self):
        games = [("2026-01-01", "BAD", 34, 20)] * 18 + [("2026-01-02", "BAD", 34, 5)] * 2
        r = self.p.judge(15.0, -600, games, {"BAD"})
        self.assertFalse(r["clears"])
        self.assertIn("shorter than -400", r["why"])

    def test_too_few_games_and_too_small_an_edge(self):
        few = [("2026-01-01", "BAD", 34, 30)] * 9
        self.assertIn("10 needed", self.p.judge(20.0, -150, few, {"BAD"})["why"])
        even = [("2026-01-01", "BAD", 34, 30)] * 12 + [("2026-01-02", "BAD", 34, 5)] * 8     # 60% vs 60%
        self.assertIn("the bar is +2", self.p.judge(20.0, -150, even, {"BAD"})["why"])

    def test_a_game_he_did_not_play_is_not_a_miss(self):
        games = [("2026-01-01", "BAD", 34, 25)] * 10 + [("2026-01-02", "BAD", 0, 0)] * 5
        self.assertEqual(self.p.judge(20.0, -150, games, {"BAD"})["games"], 10)


class TheBookItKeeps(PropsCase):

    EVENT = {"id": "401909868", "shortName": "AAA @ BBB", "date": "2026-10-26T23:00Z", "season": {"type": 2},
             "competitions": [{"status": {"type": {"name": "STATUS_SCHEDULED"}},
                               "competitors": [{"homeAway": "home", "team": {"abbreviation": "BBB"}},
                                               {"homeAway": "away", "team": {"abbreviation": "AAA"}}]}]}
    NOW = datetime(2026, 10, 26, 21, 30, tzinfo=timezone.utc)          # 17:30 ET

    def boards(self, n_players=3):
        """Several games, each with a player who clears."""
        out = []
        for i in range(n_players):
            out.append({"event_id": f"e{i}", "match": f"G{i}", "starts_utc": "2026-10-26T23:30Z",
                        "status": "STATUS_SCHEDULED",
                        "players": [{"athlete_id": f"a{i}", "name": f"P{i}", "team": "AAA", "opp": "BAD", "pos": "F",
                                     "opp_rank": 1, "best": {"line": 20.0, "price": -150, "market_pct": 60.0,
                                                             "hit_pct": 80.0, "games": 15, "p_used": 68.0,
                                                             "edge_pts": 8.0 - i, "clears": True, "why": "w"}}]})
        return out

    def test_two_a_day_best_edge_first(self):
        bets, why = self.p.pick(self.boards(3), [], [], "2026-10-26", self.NOW)
        self.assertEqual([b["athlete"] for b in bets], ["P0", "P1"])
        self.assertEqual({b["book"] for b in bets}, {"props"})
        for b in bets:
            self.b.place(b, now=self.NOW)       # stamped on the test's day, not the day it runs
        open_bets, settled, _ = self.p.props_bets(self.b)
        bets, why = self.p.pick(self.boards(3), open_bets, settled, "2026-10-26", self.NOW)
        self.assertEqual(bets, [])
        self.assertIn("the cap is 2", why)

    def test_one_a_game_and_none_about_to_start(self):
        b = self.boards(2)
        b[1]["event_id"] = "e0"
        self.assertEqual(len(self.p.pick(b, [], [], "2026-10-26", self.NOW)[0]), 1)
        soon = self.boards(1)
        soon[0]["starts_utc"] = (self.NOW + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%MZ")
        self.assertEqual(self.p.pick(soon, [], [], "2026-10-26", self.NOW)[0], [])

    def test_the_props_stop(self):
        settled = [{"profit": -100.0}] * 10
        bets, why = self.p.pick(self.boards(1), [], settled, "2026-10-26", self.NOW)
        self.assertEqual(bets, [])
        self.assertIn("props stop", why)
        self.assertIn("Ace's own betting is not affected", why)

    def test_graded_by_his_points_and_voided_if_he_sat(self):
        bet = self.bet()
        store = self.store([("2026-10-26", [["AAA", "BBB", "6583", "Player One", "F", 33, 24]])])
        store["games"] = {"401909868": store["games"]["g0"]}
        self.assertEqual(self.p.grade(bet, store, self.NOW + timedelta(hours=4))[:2], ("won", 55.87))
        store["games"]["401909868"]["rows"][0][6] = 19
        self.assertEqual(self.p.grade(bet, store, self.NOW)[:2], ("lost", -100.0))
        store["games"]["401909868"]["rows"][0][5] = 0
        self.assertEqual(self.p.grade(bet, store, self.NOW)[0], "void")
        self.assertIsNone(self.p.grade(bet, {"games": {}}, self.NOW), "waits for the final")
        self.assertEqual(self.p.grade(bet, {"games": {}}, self.NOW + timedelta(hours=80))[0], "void")

    def test_a_dry_run_end_to_end_on_the_real_ladder(self):
        store = self.season(aid="6583", team="AAA", vs=("BAD",), pts=[24] * 18 + [15, 16])   # 90% at 20+
        event = json.loads(json.dumps(self.EVENT))
        event["competitions"][0]["competitors"][0]["team"]["abbreviation"] = "BAD"

        def get(url):
            if "scoreboard" in url:
                return {"events": [event]}
            return LADDER if "propBets" in url else {"items": [{"provider": {"name": "DraftKings"},
                                                                "propBets": {"$ref": "http://x/propBets?lang=en"}}]}
        doc = self.p.scan("late", get, store, self.NOW)
        player = next(p for p in doc["games"][0]["players"] if p["athlete_id"] == "6583")
        self.assertEqual((player["opp"], player["opp_rank"]), ("BAD", 1))
        # 24 points in 18 of 20: 20+ and 22+ were both cleared 90% of the time,
        # and 22+ at -118 (54.1%) is the bigger edge than 20+ at -179 (64.2%).
        best = player["best"]
        self.assertEqual((best["line"], best["price"]), (22.0, -118), "the real rung, the real price")
        self.assertEqual((best["hit_pct"], best["p_used"], best["edge_pts"]), (90.0, 68.5, 14.4))
        twenty = next(r for r in player["rungs"] if r["line"] == 20.0)
        self.assertEqual((twenty["market_pct"], twenty["edge_pts"]), (64.16, 10.3))
        bets, _ = self.p.pick(doc["games"], [], [], doc["day"], self.NOW)
        self.assertEqual([b["selection"] for b in bets], ["Player One 22+ pts"])
        self.assertTrue(json.loads((self.root / "agents/ace/data/props-board.json").read_text())["games"])


class EachRunTakesItsHalf(PropsCase):
    """2026-10-29, owner: "Why hasn't this placed any bets so far". The 17:30
    run decided its half by the 17:45 split between afternoon and evening
    games, so it took the afternoon - nothing, on a weekday - and the evening
    slate was never judged. Its board was saved with no games at all."""

    def test_the_timers_two_runs_take_the_two_halves(self):
        timer = (ROOT / "deploy" / "ace-props.timer").read_text()
        times = [l.split()[1] for l in timer.splitlines() if l.startswith("OnCalendar=")]
        parts = [self.p.part_of(datetime(2026, 10, 28, int(t[:2]), int(t[3:5]))) for t in times]
        self.assertEqual(dict(zip(times, parts)), {"12:30": "early", "17:30": "late"})

    def test_a_run_with_no_part_given_judges_the_evening_games_at_1730(self):
        event = {"id": "e1", "shortName": "CLE @ ATL", "date": "2026-10-29T23:30Z", "season": {"type": 2},
                 "competitions": [{"status": {"type": {"name": "STATUS_SCHEDULED"}},
                                   "competitors": [{"homeAway": "home", "team": {"abbreviation": "ATL"}},
                                                   {"homeAway": "away", "team": {"abbreviation": "CLE"}}]}]}
        get = lambda url: {"events": [event]} if "scoreboard" in url else {"items": []}
        at_1730 = datetime(2026, 10, 29, 21, 30, tzinfo=timezone.utc)
        part = self.p.part_of(self.p.et_time.to_eastern(at_1730))
        doc = self.p.scan(part, get, {"games": {}}, at_1730)
        self.assertEqual([g["match"] for g in doc["games"]], ["CLE @ ATL"], "the 19:30 ET game is on the board")


class AcesOwnBookIgnoresIt(PropsCase):
    """The bankroll is shared; nothing else is."""

    def ace_bet(self, i):
        return self.b.place({"sport": "nba", "event_id": f"ace{i}", "match": f"M{i}", "selection": f"T{i} ML",
                             "side": "home", "starts_utc": "2026-10-27T00:00Z", "price": 110})

    def test_the_money_is_shared(self):
        self.bet()
        doc = json.loads((self.root / "agents/ace/state/bankroll.json").read_text())
        self.assertEqual(doc["bankroll"], 9900.0)
        self.assertEqual((len(doc["open_bets"]), len(doc["props_open_bets"])), (0, 1))

    def test_not_in_his_four(self):
        for i in range(4):
            self.bet(event_id=f"p{i}", selection=f"P{i} 20+ pts", athlete_id=str(i))
        self.assertIsNone(self.b.refusal("nba", "ace1", "T1 ML"), "four props bets do not fill his cap")
        for i in range(4):
            self.ace_bet(i)
        self.assertIn("cap is 4", self.b.refusal("nba", "ace9", "T9 ML"))

    def test_not_in_his_fault_stop(self):
        for i in range(31):
            e = self.bet(event_id=f"p{i}", selection=f"P{i} 20+ pts", athlete_id=str(i))
            self.b.append({"kind": "settle", "id": e["id"], "utc": "2026-10-27 05:00:00", "result": "lost",
                           "profit": -100.0, "detail": "x"})
        self.assertIsNone(self.b.fault_stop(), "$3,100 of props losses do not stop Ace")

    def test_his_settle_leaves_it_to_props(self):
        self.bet()
        (self.root / "agents/ace/state/results.json").write_text(json.dumps(
            {"nba|401909868": {"status": "STATUS_FINAL", "winner": "home", "home": "BBB", "away": "AAA",
                               "home_score": 100, "away_score": 90, "match": "AAA @ BBB"}}))
        self.assertEqual(self.b.settle(results_path=self.root / "agents/ace/state/results.json"), [])
        # Three days on with no result, his settle voids his own bets; a props
        # bet waits for its box score, which only ace-props.py reads.
        late = datetime(2026, 10, 30, tzinfo=timezone.utc)
        self.assertEqual(self.b.settle(results_path=self.root / "nothing.json", now=late), [])

    def test_his_checker_still_balances(self):
        e = self.bet()
        self.b.append({"kind": "settle", "id": e["id"], "utc": "2026-10-27 05:00:00", "result": "won",
                       "profit": 55.87, "detail": "x"})
        self.ace_bet(1)
        self.b.rebuild()
        verify = load("ace_verify_props", "ace-verify.py")
        problems = []
        with contextlib.redirect_stdout(io.StringIO()):
            verify.reconcile(json.loads((self.root / "agents/ace/state/bankroll.json").read_text()), problems)
        self.assertEqual(problems, [])

    def test_not_in_his_coach(self):
        e = self.bet()
        self.b.append({"kind": "settle", "id": e["id"], "utc": "2026-10-27 05:00:00", "result": "won",
                       "profit": 55.87, "detail": "x"})
        coach = load("coach_props", "coach.py")
        self.assertEqual(coach.ace_rows(), [])

    def test_not_in_his_no_ai_comparison(self):
        src = (SCRIPTS / "ace-baselines.py").read_text()
        self.assertIn('b.get("side") in ("home", "away")', src, "a props bet's side is 'over'")


class WhereItShowsAndWhenItRuns(unittest.TestCase):

    def test_a_page_in_aces_house(self):
        server = (ROOT / "mission-control-api" / "server.js").read_text()
        self.assertIn("app.get('/ace-props'", server)
        self.assertIn("acePropsRoutes.register(app);", server)
        self.assertIn("app.get('/api/ace-props'", (ROOT / "mission-control-api" / "ace-props.js").read_text())
        village = (ROOT / "mission-control-api" / "public" / "village.html").read_text()
        self.assertIn("aceprops: '/ace-props?embed=1'", village)
        self.assertIn("['aceprops', '🎯', 'Ace Props']", village)
        self.assertIn("'aceprops-close'", village)
        ace = (ROOT / "mission-control-api" / "public" / "ace.html").read_text()
        self.assertIn('href="/ace-props"', ace)
        self.assertIn("props_profit", (ROOT / "mission-control-api" / "ace.js").read_text())

    def test_the_timers(self):
        t = (ROOT / "deploy" / "ace-props.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 12:30 America/New_York", t)
        self.assertIn("OnCalendar=*-*-* 17:30 America/New_York", t)
        self.assertIn("ace-props.py run", (ROOT / "deploy" / "ace-props.service").read_text())
        self.assertIn("ace-props.py close", (ROOT / "deploy" / "ace-props-close.service").read_text())
        self.assertIn('"$ROOT"/deploy/"$AGENT"-*.timer', (SCRIPTS / "deploy.sh").read_text(),
                      "deploy.sh ace installs ace-props too")

    def test_logged_in_the_preregistration_as_not_a_rule_change(self):
        prereg = (ROOT / "agents" / "ace" / "PREREGISTRATION.md").read_text()
        self.assertIn("2026-10-26 — not a rule change: Ace Props", prereg)

    def test_its_files_stay_off_the_repo(self):
        ignore = (ROOT / ".gitignore").read_text()
        self.assertIn("agents/ace/data/*", ignore)          # props-boxscores, props-board, props-close


if __name__ == "__main__":
    unittest.main()
