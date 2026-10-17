"""Ace's sportsbook page (/ace): /api/ace/board pairs every bet and pass with
its game, away @ home, from the files Ace's own scripts write.

    python3 -m unittest tests.test_ace_board -b

The fixtures in tests/fixtures/ace-board/ are a real slate: ace-fetch.py run
against ESPN on 2026-10-14, then ace-judge.py's blind estimates, passes and
`rest`. Only the two settle events in bankroll.json were written by hand, to
give the Results tab something to show. Needs node; skips without it.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = ROOT / "mission-control-api"
FIX = Path(__file__).resolve().parent / "fixtures" / "ace-board"
NODE = shutil.which("node")


def board(root):
    js = f"process.stdout.write(JSON.stringify(require({json.dumps(str(API / 'ace.js'))}).board()))"
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, ECOSYSTEM_ROOT=str(root)))
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class TheBoardPairsEachCallWithItsGame(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        ace = Path(cls.tmp.name) / "agents" / "ace"
        (ace / "data" / "context").mkdir(parents=True)
        (ace / "state").mkdir()
        shutil.copy(FIX / "candidates.json", ace / "data" / "candidates.json")
        shutil.copy(FIX / "context-nba-401898404.json", ace / "data" / "context" / "nba-401898404.json")
        shutil.copy(FIX / "ledger.json", ace / "state" / "ledger.json")
        shutil.copy(FIX / "bankroll.json", ace / "state" / "bankroll.json")
        cls.ctx = json.loads((FIX / "context-nba-401898404.json").read_text())
        cls.ledger = json.loads((FIX / "ledger.json").read_text())
        cls.bank = json.loads((FIX / "bankroll.json").read_text())
        cls.b = board(cls.tmp.name)
        cls.game = next(g for g in cls.b["games"] if g["event_id"] == cls.ctx["event_id"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_away_and_home_come_from_the_games_context_file(self):
        # The owner asked to see the matchup, home and away. The old panel
        # showed "BKN ML" and a match string; the names and records are in the
        # context file ace-fetch writes, and only there.
        m = self.game["matchup"]
        for side in ("away", "home"):
            self.assertEqual(m[side]["name"], self.ctx[side]["name"])
            self.assertEqual(m[side]["record"], self.ctx[side]["record"])
        self.assertEqual(m["venue"], self.ctx["venue"])

    def test_each_side_carries_his_call_and_his_reason(self):
        # "what he is betting and why, and vice versa": the bet side and the
        # passed side, each with the words ace-judge recorded for it.
        for row in self.ledger["candidates"]:
            if row["event_id"] != self.ctx["event_id"]:
                continue
            s = self.game["sides"][row["side"]]
            self.assertEqual(s["status"], row["status"])
            self.assertEqual(s["why"], row["why_not"])
        statuses = {s["status"] for s in self.game["sides"].values()}
        self.assertEqual(self.game["verdict"], "bet" if "bet" in statuses else "pass")

    def test_form_is_in_the_order_espn_gives_it(self):
        # ESPN lists lastFiveGames oldest first (checked against its gameDate,
        # 2026-10-14). The page labels the pips "oldest to latest", so the
        # board must not reorder them.
        abbr = self.ctx["away"]["abbr"]
        want = [g["result"][:1] for g in self.ctx["last_five"] if g["team"] == abbr][:5]
        self.assertEqual(self.game["matchup"]["away"]["form"], want)

    def test_a_game_without_a_context_file_still_has_both_teams(self):
        # Settled bets outlive their game's context file. ESPN writes the
        # match as "AWAY @ HOME", so the sides still come out right.
        g = next(g for g in self.b["games"] if g["event_id"] != self.ctx["event_id"])
        away, home = g["match"].split(" @ ")
        self.assertEqual((g["matchup"]["away"]["abbr"], g["matchup"]["home"]["abbr"]), (away, home))
        self.assertIsNone(g["matchup"]["away"]["name"])

    def test_bets_carry_their_matchup_and_reason(self):
        for got, row in zip(self.b["open_bets"], self.bank["open_bets"]):
            self.assertEqual(got["why"], row["why"])
            self.assertEqual(got["side"], row["side"])
            away, home = row["match"].split(" @ ")
            self.assertEqual((got["matchup"]["away"]["abbr"], got["matchup"]["home"]["abbr"]), (away, home))

    def test_the_record_and_money_in_play_are_the_books_own(self):
        settled = self.bank["settled_bets"]
        rec = self.b["bankroll"]["record"]
        for word in ("won", "lost", "push"):
            self.assertEqual(rec[word], sum(1 for s in settled if s["result"] == word))
        self.assertAlmostEqual(self.b["bankroll"]["at_risk"], sum(b["stake"] for b in self.bank["open_bets"]))
        self.assertAlmostEqual(self.b["bankroll"]["settled_profit"], sum(s["profit"] for s in settled))

    def test_a_context_path_cannot_leave_the_context_folder(self):
        # event_id and sport come from files an agent writes and become part
        # of a path. "nba-/../../x.json" would resolve to data/x.json; only
        # plain values are read, so the decoy there is never shown.
        with tempfile.TemporaryDirectory() as d:
            ace = Path(d) / "agents" / "ace"
            (ace / "data" / "context").mkdir(parents=True)
            (ace / "data" / "x.json").write_text(json.dumps(
                {"home": {"abbr": "DCY", "name": "Decoy"}, "away": {"abbr": "DCY", "name": "Decoy"}}))
            row = {"selection": "AAA ML", "sport": "nba", "match": "AAA @ BBB", "event_id": "/../../x",
                   "side": "away", "price": 120, "novig_pct": 44.0, "why_not": [], "status": "unjudged"}
            (ace / "data" / "candidates.json").write_text(json.dumps({"candidates": [row]}))
            g = board(d)["games"][0]
        self.assertIsNone(g["matchup"]["away"]["name"])
        self.assertEqual((g["matchup"]["away"]["abbr"], g["matchup"]["home"]["abbr"]), ("AAA", "BBB"))

if __name__ == "__main__":
    unittest.main()
