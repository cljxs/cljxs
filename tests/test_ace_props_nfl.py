"""Ace Props NFL: the Ace Props rule on NFL ladders, on a second Odds API
account, on Ace's bankroll and out of his measures (scripts/ace-props-nfl.py).

    python3 -m unittest tests.test_ace_props_nfl -b

Owner, 2026-11-11: "lets set up another one so we can do all nfl player
props". The box score is a real ESPN reply (DAL @ IND, 2026-11-08, trimmed).
The Odds API reply is SHAPED FROM ITS DOCUMENTATION, not captured - no key
was here to capture one - so `check --spend` exists for the owner to read a
real one before a bet rests on the reader; replace the fixture with the
sample it saves. Each test names the way the book could go wrong.
"""

import importlib.util
import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"

SUMMARY = json.loads((FIXTURES / "espn-nfl-summary-401873040.json").read_text())
REPLY = json.loads((FIXTURES / "odds-api-nfl-alternates-shape.json").read_text())

LAMB, FERGUSON, WILLIAMS, DOWNS = "4241389", "4242355", "4361579", "4688813"
NOW = datetime(2026, 11, 15, 16, 0, tzinfo=timezone.utc)      # a Sunday, 11:00 ET
KICK = "2026-11-15T18:00Z"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def allowed_by(team):
    """What `team`'s defence allows a game. IND gives up the most to wide
    receivers and nothing much else; DAL is stingy everywhere; ten others
    sit in between."""
    mid = {"QB": {"passing_yards": 230, "passing_tds": 1.5, "rushing_yards": 20},
           "RB": {"rushing_yards": 100, "receiving_yards": 30, "receptions": 4},
           "WR": {"receiving_yards": 150, "receptions": 12, "rushing_yards": 3},
           "TE": {"receiving_yards": 50, "receptions": 5}}
    if team == "IND":
        return dict(mid, QB={"passing_yards": 150, "passing_tds": 0.5, "rushing_yards": 5},
                    RB={"rushing_yards": 60, "receiving_yards": 10, "receptions": 1},
                    WR={"receiving_yards": 260, "receptions": 20, "rushing_yards": 3},
                    TE={"receiving_yards": 20, "receptions": 2})
    if team == "DAL":
        return {pos: {k: v / 3 for k, v in d.items()} for pos, d in mid.items()}
    return mid


def defense_doc():
    teams = ["DAL", "IND"] + [f"T{i:02d}" for i in range(10)]
    games = {f"g{i}": {"week": 1, "allowed": {t: allowed_by(t)}} for i, t in enumerate(teams)}
    return {"season": 2026, "games": games, "through_week": 1,
            "positions": {LAMB: "WR", FERGUSON: "TE", WILLIAMS: "RB", DOWNS: "WR", "2577417": "QB"}}


def scoreboard(status="STATUS_SCHEDULED"):
    final = {"id": "401873040", "date": "2026-11-08T18:00Z", "shortName": "DAL @ IND",
             "competitions": [{"status": {"type": {"name": "STATUS_FINAL"}}, "competitors": [
                 {"homeAway": "home", "team": {"abbreviation": "IND", "displayName": "Indianapolis Colts"}},
                 {"homeAway": "away", "team": {"abbreviation": "DAL", "displayName": "Dallas Cowboys"}}]}]}
    soon = {"id": "401999999", "date": KICK, "shortName": "DAL @ IND",
            "competitions": [{"status": {"type": {"name": status}}, "competitors": [
                {"homeAway": "home", "team": {"abbreviation": "IND", "displayName": "Indianapolis Colts"}},
                {"homeAway": "away", "team": {"abbreviation": "DAL", "displayName": "Dallas Cowboys"}}]}]}
    return {"season": {"year": 2026, "type": 2}, "week": {"number": 1}, "events": [final, soon]}


class NflCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "agents/ace/state").mkdir(parents=True)
        (self.root / "agents/ace/data").mkdir(parents=True)
        (self.root / "scripts").symlink_to(SCRIPTS)
        (self.root / "agents/ace/state/bankroll.json").write_text(json.dumps(
            {"starting_bankroll": 10000.0, "bankroll": 10000.0, "cycle_count": 3}))
        env = unittest.mock.patch.dict(os.environ, {"ECOSYSTEM_ROOT": str(self.root), "ODDS_API_KEY_NFL": "",
                                                    "ODDS_API_KEY": ""})
        env.start()
        self.addCleanup(env.stop)
        self.n = load(f"ace_props_nfl_t{id(self)}", "ace-props-nfl.py")
        for mod in (self.n, self.n.AP):
            nap = unittest.mock.patch.object(mod.time, "sleep", lambda s: None)
            nap.start()
            self.addCleanup(nap.stop)
        self.n.NFL.save(defense_doc())
        self.n.NFL.update = lambda get=None, say=print, path=None: 0      # Peter's table is set above
        self.calls = []
        self.board = scoreboard()

    def get(self, url):
        if "/scoreboard" in url:
            return self.board
        if "summary?event=401873040" in url:
            return SUMMARY
        raise AssertionError(f"unexpected ESPN call {url}")

    def odds(self, remaining=(400, 397)):
        left = list(remaining)

        def call(path, params, key=None):
            self.calls.append((path, dict(params)))
            if path.endswith("/events"):
                return ([{"id": "abc123", "home_team": "Indianapolis Colts", "away_team": "Dallas Cowboys",
                          "commence_time": "2026-11-15T18:00:00Z"}], left.pop(0) if left else None)
            want = set(params["markets"].split(","))
            reply = dict(REPLY, bookmakers=[dict(bk, markets=[m for m in bk["markets"] if m["key"] in want])
                                            for bk in REPLY["bookmakers"]])
            return reply, left.pop(0) if left else None
        self.n.odds_call = call

    def paid(self):
        return [c for c in self.calls if not c[0].endswith("/events")]


class TheLadderAndTheBoxScore(NflCase):

    def test_draftkings_overs_only_and_a_half_point_is_the_next_whole_number(self):
        ladders, returned = self.n.ladders_from(REPLY)
        lamb = ladders[("player_reception_yds_alternate", "ceedee lamb")]
        self.assertEqual(lamb["rungs"], [(60, -250), (80, 120), (100, 260), (125, 550)],
                         "24.5 is 25+; the Under, the whole-number 100 (it can push) and FanDuel's +400 left out")
        self.assertEqual(returned, ["player_reception_yds_alternate", "player_receptions_alternate",
                                    "player_rush_reception_yds_alternate"], "what was billed: markets returned")

    def test_a_real_box_score_by_player_with_rush_plus_receiving(self):
        day, rows = self.n.game_rows(SUMMARY)
        self.assertEqual(day, "2026-11-08", "Eastern day of a 1pm kick-off")
        by = {r[2]: r for r in rows}
        self.assertEqual(by[LAMB][:4], ["DAL", "IND", LAMB, "CeeDee Lamb"])
        self.assertEqual(by[LAMB][4]["receiving_yards"], 195.0)
        self.assertEqual(self.n.stat_of(by[WILLIAMS][4], self.n.RUSH_REC), 124.0, "101 rushing + 23 receiving")
        self.assertNotIn("4040982", by, "a defender's tackles are not an offensive line")

    def test_names_match_without_a_suffix_and_a_shared_name_is_not_guessed(self):
        self.assertEqual(self.n.name_key("Michael Pittman Jr."), "michael pittman")
        who = {"1": {"name": "Josh Allen", "team": "BUF"}, "2": {"name": "Josh Allen", "team": "JAX"},
               "3": {"name": "D.J. Moore", "team": "BUF"}}
        idx = self.n.by_name(who, {"BUF", "JAX"})
        self.assertNotIn("josh allen", idx)
        self.assertEqual(idx["d j moore"], "3")


class TheDefencesArePeters(NflCase):

    def test_bottom_ten_from_his_table_and_rush_plus_receiving_is_his_two_columns(self):
        dfn = self.n.defence(self.n.NFL.load())
        self.assertEqual(dfn["WR receiving_yards"]["bottom"][0], "IND")
        self.assertNotIn("DAL", dfn["WR receiving_yards"]["bottom"])
        self.assertEqual(len(dfn["WR receiving_yards"]["bottom"]), 10)
        self.assertEqual(dfn["WR rush_rec_yards"]["allowed"]["IND"], 263.0)
        self.assertNotIn("IND", dfn["QB passing_yards"]["bottom"])

    def test_only_the_markets_a_bottom_defence_is_about_are_paid_for(self):
        dfn = self.n.defence(self.n.NFL.load())
        self.assertEqual(sorted(self.n.wanted_markets("IND", "DAL", dfn)),
                         ["player_reception_yds_alternate", "player_receptions_alternate",
                          "player_rush_reception_yds_alternate"],
                         "no passing or rushing ladder: neither side is bottom 10 there")


class TheRuleIsAceProps(NflCase):

    def judged(self):
        store, _ = self.n.update_players(self.get)
        g = self.n.espn_games(self.get)[1]
        ladders, _ = self.n.ladders_from(REPLY)
        return self.n.game_board(g, ladders, self.n.players(store), self.n.NFL.load()["positions"],
                                 self.n.defence(self.n.NFL.load()))

    def test_judged_by_ace_props_own_judge(self):
        rows = {(r["name"], r["market"]): r for r in self.judged()["players"]}
        lamb = rows[("CeeDee Lamb", "player_reception_yds_alternate")]
        self.assertEqual(lamb["opp"], "IND")
        self.assertEqual(lamb["best"]["line"], 100, "195 yds against IND: the +260 rung has the edge")
        q = self.n.AP.implied(260)
        self.assertEqual(lamb["best"]["p_used"], round(q + (1 - self.n.AP.SHRINK) * (100 - q), 1))
        self.assertIn("longer than +400", next(r for r in lamb["rungs"] if r["line"] == 125)["why"])
        # The same rule, one place: change ace-props.py's bar and this board follows.
        self.n.AP.GAP_MIN_PTS = 50
        self.assertIsNone({(r["name"], r["market"]): r for r in self.judged()["players"]}[
            ("CeeDee Lamb", "player_reception_yds_alternate")]["best"])

    def test_a_player_against_a_defence_not_in_the_bottom_ten_is_not_judged(self):
        rows = {(r["name"], r["market"]): r for r in self.judged()["players"]}
        downs = rows[("Josh Downs", "player_reception_yds_alternate")]
        self.assertIn("DAL is not bottom 10 against WRs", downs["why"])
        self.assertEqual(downs["rungs"], [])
        self.assertIn("not in either team", rows[("Michael Pittman Jr.", "player_receptions_alternate")]["why"])


class TheCredits(NflCase):

    def test_a_decision_keeps_the_reserve_and_the_week_a_close_may_use_the_reserve(self):
        u = {"remaining": 15, "weeks": {"w": 0}, "log": []}
        self.assertFalse(self.n.spend_ok(u, "w", 6), "15 - 6 leaves 9, under the 10 kept for closes")
        self.assertTrue(self.n.spend_ok(u, "w", 5))
        self.assertTrue(self.n.spend_ok(u, None, 6, close=True))
        u = {"remaining": 400, "weeks": {"w": 92}, "log": []}
        self.assertFalse(self.n.spend_ok(u, "w", 4), "92 + 4 is past the week's 95")

    def test_what_a_pull_cost_is_the_drop_in_credits(self):
        u = self.n.usage("w")
        u["remaining"] = 400
        self.assertEqual(self.n.note_spend(u, "w", "x", 397, 1), 3, "the account's own count beats the estimate")
        self.assertEqual(u["weeks"]["w"], 3)

    def test_never_aces_own_key(self):
        os.environ["ODDS_API_KEY"] = "ace-own"
        self.assertIsNone(self.n.api_key(), "Ace's 500 credits are his Pinnacle check's")
        os.environ["ODDS_API_KEY_NFL"] = "nfl-own"
        self.assertEqual(self.n.api_key(), "nfl-own")


class ARun(NflCase):

    def setUp(self):
        super().setUp()
        os.environ["ODDS_API_KEY_NFL"] = "k"
        self.odds()

    def test_prices_the_game_once_only_what_is_wanted_and_bets(self):
        out = self.n.run(get=self.get, now=NOW, say=lambda m: None)
        self.assertEqual(len(self.paid()), 1)
        self.assertEqual(sorted(self.paid()[0][1]["markets"].split(",")),
                         ["player_reception_yds_alternate", "player_receptions_alternate",
                          "player_rush_reception_yds_alternate"])
        self.assertEqual(self.paid()[0][1]["bookmakers"], "draftkings")
        open_bets, _s, _f = self.n.nfl_bets()
        self.assertEqual(len(open_bets), 1, out)      # only Lamb clears; one bet a player
        lamb = next(x for x in open_bets if x["athlete_id"] == LAMB)
        self.assertEqual((lamb["book"], lamb["line"], lamb["price"], lamb["odds_id"]),
                         ("props-nfl", 100, 260, "abc123"))
        self.assertEqual(lamb["selection"], "CeeDee Lamb 100+ rec yds")
        self.assertEqual(self.n.usage()["remaining"], 397)
        self.calls.clear()
        self.n.run(get=self.get, now=NOW + timedelta(minutes=30), say=lambda m: None)
        self.assertEqual(self.paid(), [], "a game is priced once a day")

    def test_no_credit_spent_once_the_days_bets_are_placed(self):
        for i in range(2):
            self.n.book().place({"book": "props-nfl", "sport": "nfl", "event_id": f"x{i}", "match": "A @ B",
                                 "selection": f"P{i} 50+ rec yds", "price": 100, "starts_utc": KICK},
                                now=NOW - timedelta(hours=1))
        out = self.n.run(get=self.get, now=NOW, say=lambda m: None)
        self.assertEqual(self.calls, [], "not even the free list")
        self.assertTrue(any("bets are placed" in line for line in out))

    def test_a_dry_run_bets_nothing(self):
        out = self.n.run(dry=True, get=self.get, now=NOW, say=lambda m: None)
        self.assertTrue(any(line.startswith("would bet CeeDee Lamb") for line in out), out)
        self.assertEqual(self.n.nfl_bets()[0], [])

    def test_a_game_too_far_off_is_left_for_a_later_run(self):
        self.n.run(get=self.get, now=NOW - timedelta(hours=4), say=lambda m: None)
        self.assertEqual(self.paid(), [], "18:00 kick-off is past 3.5 hours from 14:00")


class TheBookItKeeps(NflCase):

    def bet(self, **kw):
        f = dict({"book": "props-nfl", "sport": "nfl", "event_id": "401873040", "odds_id": "abc123",
                  "match": "DAL @ IND", "selection": "CeeDee Lamb 100+ rec yds", "athlete_id": LAMB,
                  "athlete": "CeeDee Lamb", "market": "player_reception_yds_alternate", "stat": "receiving_yards",
                  "line": 100, "price": 260, "starts_utc": "2026-11-08T18:00Z"}, **kw)
        return self.n.book().place(f, now=datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc))

    def test_graded_by_his_stat_and_voided_if_he_is_not_in_the_box_score(self):
        store, _ = self.n.update_players(self.get)
        later = datetime(2026, 11, 9, tzinfo=timezone.utc)
        won = self.n.grade(self.bet(), store, later)
        self.assertEqual(won[:2], ("won", 260.0))
        self.assertIn("195 rec yds", won[2])
        self.assertEqual(self.n.grade(self.bet(line=200, selection="x"), store, later)[0], "lost")
        self.assertEqual(self.n.grade(self.bet(athlete_id="1", selection="y"), store, later)[0], "void")
        rr = self.bet(athlete_id=WILLIAMS, stat="rush_rec_yards", market="player_rush_reception_yds_alternate",
                      line=120, selection="z")
        self.assertEqual(self.n.grade(rr, store, later)[0], "won", "124 rush + rec yards")

    def test_the_close_is_one_pull_of_the_bets_market_just_before_kick_off(self):
        os.environ["ODDS_API_KEY_NFL"] = "k"
        self.odds(remaining=(399,))
        b = self.bet(starts_utc=KICK)
        self.n.record_closes(self.n.nfl_bets()[0], now=NOW, say=lambda m: None)
        self.assertEqual(self.calls, [], "two hours out: not yet")
        self.n.record_closes(self.n.nfl_bets()[0], now=NOW + timedelta(minutes=100), say=lambda m: None)
        self.assertEqual([c[1]["markets"] for c in self.calls], ["player_reception_yds_alternate"])
        self.n.record_closes(self.n.nfl_bets()[0], now=NOW + timedelta(minutes=110), say=lambda m: None)
        self.assertEqual(len(self.calls), 1, "one close a bet")
        row = self.n.board_json()["open"][0]
        self.assertEqual(row["close_price"], 260)
        self.assertEqual(row["clv_pct"], 0.0, "taken at the price it closed at")
        self.assertEqual(b["id"], row["id"])

    def test_its_own_stop(self):
        self.assertIsNone(self.n.stop_reason([{"profit": -900}]))
        self.assertIn("$1,000 stop", self.n.stop_reason([{"profit": -1000}]))


class TheOtherBooksIgnoreIt(NflCase):

    def setUp(self):
        super().setUp()
        b = self.n.book()
        b.place({"book": "props-nfl", "sport": "nfl", "event_id": "1", "match": "A @ B",
                 "selection": "P 50+ rec yds", "price": 100, "starts_utc": KICK}, now=NOW)
        b.place({"book": "props", "sport": "nba", "event_id": "2", "match": "C @ D",
                 "selection": "Q 20+ pts", "price": 100, "starts_utc": KICK}, now=NOW)
        self.b = b

    def test_out_of_aces_measures_and_in_his_money(self):
        f = self.b.fold(self.b.events())
        self.assertTrue(all(self.b.is_props(x) for x in f["open"]))
        doc = json.loads((self.root / "agents/ace/state/bankroll.json").read_text())
        self.assertEqual(doc["open_bets"], [])
        self.assertEqual(sorted(x["book"] for x in doc["props_open_bets"]), ["props", "props-nfl"],
                         "each keeps its own book name")
        self.assertEqual(doc["bankroll"], 9800.0)

    def test_nba_props_and_nfl_props_never_grade_each_other(self):
        ap = self.n.AP
        self.assertEqual([x["book"] for x in ap.props_bets(self.b)[0]], ["props"])
        self.assertEqual([x["book"] for x in self.n.nfl_bets(self.b)[0]], ["props-nfl"])


class WhereItShowsAndWhenItRuns(unittest.TestCase):

    def test_the_props_page_has_an_nfl_side(self):
        js = (ROOT / "mission-control-api" / "ace-props.js").read_text()
        self.assertIn("ace-props-nfl.py", js)
        page = (ROOT / "mission-control-api" / "public" / "ace-props.html").read_text()
        self.assertIn("sport=nfl", page)

    def test_the_timers(self):
        t = (ROOT / "deploy" / "ace-props-nfl.timer").read_text()
        for at in ("08:45", "11:45", "15:15", "17:45"):
            self.assertIn(f"OnCalendar=*-*-* {at} America/New_York", t)
        self.assertIn("ace-props-nfl.py run", (ROOT / "deploy" / "ace-props-nfl.service").read_text())
        self.assertIn("ace-props-nfl.py close", (ROOT / "deploy" / "ace-props-nfl-close.service").read_text())

    def test_logged_in_the_preregistration(self):
        self.assertIn("2026-11-11 — not a rule change: Ace Props NFL",
                      (ROOT / "agents" / "ace" / "PREREGISTRATION.md").read_text())


if __name__ == "__main__":
    unittest.main()
