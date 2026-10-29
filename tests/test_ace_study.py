"""Ace's record, read for where profit could come from (scripts/ace-study.py).

    python3 -m unittest tests.test_ace_study -b

Owner, 2026-10-29: "find another strategy to run alongside ace to compare
them. What counts as better is more profit." The report is read-only and
its numbers decide what gets built next, so each test pins one way it
could mislead: noise read as skill, props counted as his, a replay priced
at a price he never had, or a rule that is not his own.

The games are generated - three hundred, six hours apart, in the shapes
ace-fetch.py and ace-judge.py write (lines.json snapshots, results.json,
blind.jsonl, bets.jsonl events) - because the questions are statistical:
what the report says when the truth is known.
"""

import importlib.util
import json
import math
import os
import random
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def moneyline(p):
    """An American price for chance p with a ~4.5% book cut."""
    q = p * 1.0225
    return round(-100 * q / (1 - q)) if q >= .5 else round(100 * (1 - q) / q)


def decimal(price):
    return 1 + (price / 100 if price > 0 else 100 / -price)


def world(state, seed, ace_of, games=300, bets_from=120, props=0):
    """Write Ace's state for `games` finished games. `ace_of(truth, market,
    rng)` is his blind home chance (0-100); the market's chance is the truth
    plus noise. His bets follow a simple 3-point gap on the first
    `bets_from` games; `props` adds that many Ace Props bets."""
    rng = random.Random(seed)
    lines, results, blind, ev = {}, {}, [], []
    base = datetime(2026, 10, 1, 23, 0, tzinfo=timezone.utc)
    for i in range(games):
        start = base + timedelta(hours=6 * i)
        truth = rng.uniform(28, 72)
        m = min(max(truth + rng.gauss(0, 6), 20), 80)
        ace = min(max(ace_of(truth, m, rng), 5), 95)
        y = 1 if rng.random() < truth / 100 else 0
        est, close_at = start - timedelta(hours=8), start - timedelta(minutes=20)

        def snap(at, p):
            return {"at": f"{at:%Y-%m-%dT%H:%M:%SZ}", "moneyline_home": moneyline(p / 100),
                    "moneyline_away": moneyline(1 - p / 100),
                    "novig_home_pct": round(p, 1), "novig_away_pct": round(100 - p, 1)}
        h = [snap(est - timedelta(minutes=30), m), snap(close_at, m + rng.gauss(0, 1.5))]
        key = f"nba|A{i} @ H{i}|{start:%Y-%m-%dT%H:%MZ}"
        lines[key] = {"sport": "nba", "match": f"A{i} @ H{i}", "start_utc": f"{start:%Y-%m-%dT%H:%MZ}",
                      "home": f"H{i}", "away": f"A{i}", "first": h[0], "last": h[1], "history": h,
                      "event_id": str(i)}
        results[f"nba|{i}"] = {"winner": "home" if y else "away", "status": "STATUS_FINAL"}
        blind.append({"utc": f"{est:%Y-%m-%dT%H:%M:%SZ}", "key": key, "sport": "nba", "event_id": str(i),
                      "starts_utc": f"{start:%Y-%m-%dT%H:%MZ}", "home_pct": round(ace)})
        if i >= bets_from or abs(ace - m) <= 3:
            continue
        side = "home" if ace > m else "away"
        q = round(m if side == "home" else 100 - m, 1)
        mine = ace if side == "home" else 100 - ace
        price = h[0][f"moneyline_{side}"]
        won = (y == 1) == (side == "home")
        ev.append({"kind": "bet", "id": f"b{i}", "sport": "nba", "event_id": str(i), "match": f"A{i} @ H{i}",
                   "selection": f"X{i} ML", "side": side, "starts_utc": f"{start:%Y-%m-%dT%H:%MZ}",
                   "price": price, "stake": 100.0, "novig_pct": q, "my_pct": mine,
                   "p_used": round(q + 0.4 * (mine - q), 1), "utc": f"{est:%Y-%m-%d %H:%M:%S}"})
        ev.append({"kind": "settle", "id": f"b{i}", "utc": f"{start:%Y-%m-%dT%H:%MZ}",
                   "result": "won" if won else "lost",
                   "profit": round(100 * (decimal(price) - 1), 2) if won else -100.0, "detail": "x"})
    for j in range(props):
        at = base + timedelta(hours=6 * j + 1)
        ev.append({"kind": "bet", "id": f"p{j}", "book": "props", "sport": "nba", "event_id": f"9{j}",
                   "match": f"P{j} 20+ points", "selection": f"P{j} 20+", "side": "over",
                   "starts_utc": f"{at:%Y-%m-%dT%H:%MZ}", "price": 150, "stake": 100.0,
                   "utc": f"{at:%Y-%m-%d %H:%M:%S}"})
        ev.append({"kind": "settle", "id": f"p{j}", "utc": f"{at:%Y-%m-%dT%H:%MZ}", "result": "won",
                   "profit": 150.0, "detail": "x"})
    (state / "lines.json").write_text(json.dumps(lines))
    (state / "results.json").write_text(json.dumps(results))
    (state / "blind.jsonl").write_text("".join(json.dumps(b) + "\n" for b in blind))
    (state / "bankroll.json").write_text(json.dumps({"starting_bankroll": 10000, "bankroll": 10000}))
    (state / "bets.jsonl").write_text("".join(json.dumps(x) + "\n" for x in ev))


class StudyCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "agents" / "ace" / "state"
        self.state.mkdir(parents=True)
        (self.root / "agents" / "ace" / "data").mkdir()
        (self.root / "scripts").symlink_to(SCRIPTS)
        env = unittest.mock.patch.dict(os.environ, {"ECOSYSTEM_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.s = load("ace_study_test", "ace-study.py")

    def study(self, seed, ace_of, **kw):
        world(self.state, seed, ace_of, **kw)
        return self.s.study()


class TheInformationTestTellsSkillFromNoise(StudyCase):
    """Section 3's weight on his disagreement with the market is the number
    a second strategy would be built on. On a smoke run with pure noise one
    seed of eight showed a "weight" that looked real - so the report must say
    'not distinguishable' for noise and 'yes' only when he truly knows more."""

    def test_noise_is_not_read_as_skill(self):
        # Ace = the market plus noise: he knows nothing the market does not.
        for seed in (1, 2, 3):
            fit = self.study(seed, lambda t, m, r: m + r.gauss(0, 6))["forecasts"]["fit"]
            self.assertLess(fit["ace_weight"] - 2 * fit["ace_se"], 0, (seed, fit))

    def test_real_knowledge_is_found(self):
        # Ace = the truth: the market's error is exactly what he adds.
        fit = self.study(4, lambda t, m, r: t, games=600)["forecasts"]["fit"]
        self.assertGreater(fit["ace_weight"] - 2 * fit["ace_se"], 0, fit)
        self.assertGreater(fit["keeps_of_ace"], 0)

    def test_the_fit_waits_for_enough_games(self):
        f = self.study(5, lambda t, m, r: t, games=MIN_GAMES - 1)["forecasts"]
        self.assertNotIn("fit", f)
        self.assertIn(f"needs {MIN_GAMES} games", self.s.text(self.s.study()))


MIN_GAMES = 30


class SqueezedTowardEvenIsSeen(StudyCase):
    """His Sunday review showed 17 of 19 bets on underdogs: the sign of
    chances pulled toward 50-50. The report must name that, and not name it
    when his chances spread as widely as the market's."""

    def test_squeezed(self):
        s = self.study(6, lambda t, m, r: 50 + 0.5 * (m - 50))
        f = s["forecasts"]
        self.assertLess(f["ace_from_even_pts"], f["market_from_even_pts"] - 1)
        self.assertIn("squeezed toward even", self.s.text(s))

    def test_not_squeezed(self):
        s = self.study(7, lambda t, m, r: m + r.gauss(0, 1))
        self.assertNotIn("squeezed toward even", self.s.text(s))


class GamesFarFromTheMarketAreNamed(StudyCase):
    """Owner, 2026-10-29: "Check those 3 games" - three games where Ace said
    about 20% for the home team and the market 66%. A home and away read the
    wrong way round looks exactly like that, so the report names every game
    30+ points from the market, with when he estimated it - and only those."""

    def test_named(self):
        # Game 0 swapped: he gives the home team the away team's chance.
        s = self.study(11, lambda t, m, r: (100 - m) if r.random() < 0.02 else m + r.gauss(0, 3))
        far = s["forecasts"]["far_apart"]
        self.assertTrue(far)
        for g in far:
            self.assertGreaterEqual(abs(g["ace_home"] - g["market_home"]), 30)
            self.assertIn(" @ ", g["game"])
        out = self.s.text(s)
        self.assertIn("a home/away mix-up looks like this", out)
        self.assertIn(far[0]["game"], out)
        self.assertIn(" ET)", out)

    def test_none_when_none(self):
        s = self.study(12, lambda t, m, r: m + r.gauss(0, 3))
        self.assertEqual(s["forecasts"]["far_apart"], [])
        self.assertNotIn("mix-up", self.s.text(s))


class ProfitIsWorthPlusLuck(StudyCase):
    """Section 2 splits profit into what the closing prices say the bets were
    worth and the luck on top. The two must add back to the profit, and Ace
    Props - Ace's money, none of his measures - must not be in either."""

    def test_adds_up(self):
        p = self.study(8, lambda t, m, r: m + r.gauss(0, 6))["profit"]
        self.assertEqual(p["graded"], p["bets"])
        self.assertAlmostEqual(p["worth"] + p["luck"], p["profit"], places=1)

    def test_props_are_not_his(self):
        without = self.study(9, lambda t, m, r: m + r.gauss(0, 6))["profit"]
        with_props = self.study(9, lambda t, m, r: m + r.gauss(0, 6), props=5)["profit"]
        self.assertEqual(without, with_props)


class TheReplayIsHisOwnRuleAtHisPrice(StudyCase):
    """Section 6 replays rules on every game he estimated. "His rule" must be
    ace-judge.py's own bar, not a copy that drifts, and every replay must be
    priced at DraftKings' price when he estimated - not the close, which he
    could never have had."""

    def game(self, ace_home, mkt_home, then_ml, y=1):
        return {"y": y, "blind": ace_home / 100, "then": mkt_home,
                "snap": {"moneyline_home": then_ml, "moneyline_away": -then_ml}}

    def test_his_bar_decides(self):
        judge = load("ace_judge_study_t", "ace-judge.py")
        book = load("ace_book_study_t", "ace-book.py")
        # 50 vs a 40% market: pulled 60% back he counts 44%, +4 pts - a bet.
        # 43 vs 40: counts 41.2%, +1.2 - under the 2-point bar.
        got = self.s.candidates([self.game(50, 40, 150), self.game(43, 40, 150)], judge, book)
        self.assertEqual(got["rules"]["his rule (60% pull)"]["bets"], 1)
        # The same games at a 100% pull toward the market: no bet at all.
        self.assertEqual(judge.bar(50, {"novig_pct": 40, "price": 150}, 1.0)["clears"], False)
        self.assertEqual(judge.bar(50, {"novig_pct": 40, "price": 150}),
                         judge.bar(50, {"novig_pct": 40, "price": 150}, judge.SHRINK))

    def test_priced_when_he_estimated(self):
        judge = load("ace_judge_study_t2", "ace-judge.py")
        book = load("ace_book_study_t2", "ace-book.py")
        got = self.s.candidates([self.game(50, 40, 150, y=1)], judge, book)
        self.assertEqual(got["rules"]["his rule (60% pull)"]["profit"], 150.0)

    def test_report_runs_whole_and_writes_nothing(self):
        world(self.state, 10, lambda t, m, r: m + r.gauss(0, 6))
        before = {p: p.stat().st_mtime_ns for p in (self.root / "agents").rglob("*") if p.is_file()}
        out = self.s.text(self.s.study())
        for n in range(1, 7):
            self.assertIn(f"\n{n}. ", "\n" + out)
        self.assertIn("ideas, not proof", out)
        after = {p: p.stat().st_mtime_ns for p in (self.root / "agents").rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
