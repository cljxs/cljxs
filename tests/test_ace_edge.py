"""Where an edge in the betting might be (scripts/ace-edge.py).

    python3 -m unittest tests.test_ace_edge -b

Owner, 2026-11-06, after Ace's evidence stop: "analyze that data to generate
ideas for finding a new edge or identifying commonalities with strategies
that are winning" - the sports bettors only; winning is profit, believed only
when the close or both halves back it up. Each test pins one way the report
could mislead: a bet valued at a price nobody could have had, a move read
the wrong way round, a props bet counted as a game bet, or an idea called on
too few bets or on one lucky half.
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


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T0 = datetime(2026, 10, 10, 23, 0, tzinfo=timezone.utc)


def stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def snap(at, home_pct, sharp=None):
    to_ml = lambda p: round(-100 * p / (1 - p)) if p >= .5 else round(100 * (1 - p) / p)
    s = {"at": stamp(at), "moneyline_home": to_ml(home_pct / 100 * 1.0225),
         "moneyline_away": to_ml((100 - home_pct) / 100 * 1.0225),
         "novig_home_pct": home_pct, "novig_away_pct": round(100 - home_pct, 1)}
    if sharp is not None:
        s.update(sharp_home_pct=sharp, sharp_away_pct=round(100 - sharp, 1))
    return s


class EdgeCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "agents/ace/state"
        self.state.mkdir(parents=True)
        (self.root / "agents/ace/data").mkdir(parents=True)
        (self.root / "scripts").symlink_to(SCRIPTS)
        (self.state / "bankroll.json").write_text(json.dumps({"starting_bankroll": 10000, "bankroll": 10000}))
        env = unittest.mock.patch.dict(os.environ, {"ECOSYSTEM_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.e = load(f"ace_edge_t{id(self)}", "ace-edge.py")
        self.study, self.book, self.clv, self.base = self.e.mods()

    def game(self, i, history, winner):
        start = T0 + timedelta(days=i)
        key = f"nba|A{i} @ H{i}|{start:%Y-%m-%dT%H:%MZ}"
        hist = [snap(start - timedelta(hours=h), p, s) for h, p, s in history]
        entry = {"sport": "nba", "match": f"A{i} @ H{i}", "start_utc": f"{start:%Y-%m-%dT%H:%MZ}",
                 "event_id": str(i), "first": hist[0], "last": hist[-1], "history": hist}
        return key, entry, {f"nba|{i}": {"winner": winner, "status": "STATUS_FINAL"}}


class EachBetIsValuedAtWhatWasKnownThen(EdgeCase):

    def test_price_close_move_and_pinnacle(self):
        # Home 40% eight hours out, 42% two hours earlier (moved against home),
        # 45% at the close; Pinnacle had home at 43% when the bet was placed.
        key, entry, res = self.game(1, [(10, 42.0, None), (8, 40.0, 43.0), (0.25, 45.0, None)], "home")
        at = T0 + timedelta(days=1) - timedelta(hours=8)
        price = entry["history"][1]["moneyline_home"]
        d = self.e.decision("x", entry, "home", price, 40.0, at, res, self.clv, self.book, 43.0)
        self.assertEqual(d["moved_pts"], -2.0, "40% now against 42% two hours before")
        self.assertEqual(d["clv_pct"], self.clv.clv_at(price, 45.0), "valued at the close after the bet")
        self.assertTrue(d["won"])
        self.assertAlmostEqual(d["profit"], 100 * (self.book.decimal_odds(price) - 1))
        self.assertEqual(self.e.sharp_gap(d), "Pinnacle 1.5+ pts higher")
        self.assertEqual(self.e.moved(d), "moved against it 1+ pts")
        self.assertEqual(self.e.band(d), "underdog (35-50%)")
        self.assertIsNone(self.e.decision("x", entry, "home", price, 40.0, at, {}, self.clv, self.book),
                          "no result, no grade")

    def test_the_market_is_priced_six_hours_out_not_at_the_close(self):
        key, entry, res = self.game(2, [(9, 50.0, None), (6.5, 55.0, None), (1, 70.0, None)], "away")
        rows = self.e.market_decisions(self.clv, self.book, {key: entry}, res)
        home = next(d for d in rows if d["side"] == "home")
        self.assertEqual(home["pct_then"], 55.0, "the last price at or before 6 hours out")
        self.assertGreater(home["clv_pct"], 0, "the home side's price then beat its 70% close")
        self.assertEqual(len(rows), 2, "both sides")


class ProfitIsBelievedOnlyWithBacking(EdgeCase):

    def rows(self, n, profit, clv, first_half_profit=None):
        out = []
        for i in range(n):
            p = first_half_profit if first_half_profit is not None and i < n // 2 else profit
            out.append({"who": "x", "sport": "NBA", "side": "home", "price": 100, "pct_then": 48.0,
                        "sharp_then": None, "hours_before": 5.0, "moved_pts": 0.0, "clv_pct": clv,
                        "won": p > 0, "profit": p, "at": f"2026-10-{1 + i // 4:02d} {i % 24:02d}:00"})
        return out

    def test_an_idea_needs_thirty_bets_the_close_and_both_halves(self):
        e = self.e
        good = e.summarise(self.rows(40, 10.0, 1.5), self.study.ci)
        self.assertTrue(good["both_halves"])
        sections = {"s": {"f": {"good": good,
                                "few": e.summarise(self.rows(20, 10.0, 1.5), self.study.ci),
                                "no close": e.summarise(self.rows(40, 10.0, -0.5), self.study.ci),
                                "one half": e.summarise(self.rows(40, 30.0, 1.5, first_half_profit=-20.0),
                                                        self.study.ci)}}}
        found, tested = e.ideas(sections)
        self.assertEqual([g for _w, _f, g, _s in found], ["good"])
        self.assertEqual(tested, 4)


class TheWholeReport(EdgeCase):

    def test_runs_reads_only_and_keeps_props_out(self):
        lines, results = {}, {}
        for i in range(8):
            k, entry, r = self.game(i, [(9, 45.0, None), (6, 44.0, None), (0.2, 46.0, None)],
                                    "home" if i % 2 else "away")
            lines[k], results = entry, {**results, **r}
            if i < 4:
                at = T0 + timedelta(days=i) - timedelta(hours=6)
                e = self.book.place({"sport": "nba", "event_id": str(i), "match": entry["match"],
                                     "selection": f"H{i} ML", "side": "home", "starts_utc": entry["start_utc"],
                                     "price": entry["history"][1]["moneyline_home"], "novig_pct": 44.0},
                                    now=at)
                self.book.append({"kind": "settle", "id": e["id"], "utc": stamp(at), "result": "won" if i % 2 else "lost",
                                  "profit": 120.0 if i % 2 else -100.0, "detail": "x"})
        k0 = next(iter(lines))
        p = self.book.place({"book": "props", "sport": "nba", "event_id": "0", "match": "A0 @ H0",
                             "selection": "P 20+", "side": "home", "starts_utc": lines[k0]["start_utc"],
                             "price": 150, "novig_pct": 40.0}, now=T0 - timedelta(hours=6))   # as ace-props.py writes
        self.book.append({"kind": "settle", "id": p["id"], "utc": stamp(T0), "result": "won", "profit": 150.0,
                          "detail": "x"})
        (self.state / "lines.json").write_text(json.dumps(lines))
        (self.state / "results.json").write_text(json.dumps(results))
        before = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file() and "scripts" not in p.parts}
        e = self.e.edge()
        after = {p: p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file() and "scripts" not in p.parts}
        self.assertEqual(before, after, "it writes nothing")
        self.assertEqual(e["bettors"]["ace"]["bets"], 4, "game bets only - never the props bet")
        self.assertEqual(e["market_sides"], 16)
        out = self.e.text(e)
        for head in ("1. THE BETTORS", "2. EVERY BET, POOLED", "3. THE WHOLE MARKET, 6H BEFORE", "4. IDEAS"):
            self.assertIn(head, out)


if __name__ == "__main__":
    unittest.main()
