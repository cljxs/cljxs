"""The trading coach: a journal, a daily analysis that only observes, a
weekly review that only proposes (scripts/coach.py).

    python3 -m unittest tests.test_coach -b

Owner, 2026-10-19: "Create and maintain a daily trading journal ... Do NOT
change any trading rules or strategies during the daily analysis ... Every
Sunday ... propose specific, actionable changes."
"""

import importlib.util
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
sys.path.insert(0, str(SCRIPTS))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CoachCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for d in ("agents/belfort/state", "agents/belfort/data", "agents/bolt/state", "agents/bolt/data", "tasks"):
            (self.root / d).mkdir(parents=True)
        (self.root / "scripts").symlink_to(SCRIPTS)
        shutil.copy(ROOT / "tasks" / "engines.json", self.root / "tasks")
        env = unittest.mock.patch.dict(os.environ, {"ECOSYSTEM_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.c = load("coach_test", "coach.py")

    def belfort(self, trades):
        (self.root / "agents/belfort/state/portfolio.json").write_text(json.dumps(
            {"cash": 9000, "starting_cash": 10000, "positions": [], "trades": trades}))

    def bolt(self, sells, news=False, marks=None):
        tr = []
        for sym, eu, ep, xu, xp, pct, best, score, why in sells:
            tr.append({"utc": eu, "symbol": sym, "side": "buy", "qty": 1500 / ep, "price": ep,
                       "notional": 1500, "fee": 1.5, "reason": f"scored {score}/10: uptrend"})
            tr.append({"utc": xu, "symbol": sym, "side": "sell", "qty": 1500 / ep, "price": xp,
                       "notional": 1500 * xp / ep, "fee": 1.5, "reason": why, "entry_price": ep,
                       "opened_utc": eu, "pnl": 1500 * pct / 100, "pnl_pct": pct, "best_pct": best})
        name = "news-portfolio.json" if news else "portfolio.json"
        (self.root / "agents/bolt/state" / name).write_text(json.dumps(
            {"starting_cash": 10000, "cash": 10000, "positions": [], "trades": tr, "marks": marks or {}}))

    BEL = [{"side": "BUY", "symbol": "NVDA", "shares": 10, "price": 100.0, "notional": 1000.0,
            "utc": "2026-10-14 14:00:00", "reason": "EARNINGS: beat and raise", "score": 8, "regime": "favorable"},
           {"side": "SELL", "symbol": "NVDA", "shares": 10, "price": 112.0, "notional": 1120.0,
            "realised_pnl": 120.0, "utc": "2026-10-19 15:00:00",
            "reason": "PROTECT GAIN: was up +14.0% (high $114.00), now +12.0%"}]


class TheJournal(CoachCase):

    def test_every_field_the_owner_listed(self):
        self.belfort(self.BEL)
        self.bolt([("SOL", "2026-10-17T10:00:00Z", 100.0, "2026-10-19T18:00:00Z", 104.0, 4.0, 9.0, 8,
                    "trailing stop (8% off its best, +9.0%)")], news=True)
        rows = self.c.rows_between("2026-10-19", "2026-10-19")
        self.assertEqual([r["agent"] for r in rows], ["Belfort", "Bolt + news"])
        nvda = rows[0]
        self.assertEqual((nvda["asset"], nvda["side"], nvda["entry_price"], nvda["exit_price"]),
                         ("stock", "long", 100.0, 112.0), "the entry price is worked out from his own record")
        self.assertEqual((nvda["size_usd"], nvda["pnl_usd"], nvda["pnl_pct"]), (1000.0, 120.0, 12.0))
        self.assertIn("score 8", nvda["setup"])
        self.assertEqual(nvda["entry_reason"], "EARNINGS: beat and raise")
        self.assertIn("PROTECT GAIN", nvda["exit_reason"])
        line = self.c.journal_line(nvda)
        self.assertIn("2026-10-19 11:00 ET", line, "Eastern, from a UTC record")
        self.assertIn("| win |", line)

    def test_a_day_is_an_eastern_day(self):
        # 02:00 UTC on the 20th is still the 19th in New York.
        self.bolt([("SOL", "2026-10-18T10:00:00Z", 100.0, "2026-10-20T02:00:00Z", 99.0, -1.0, 0.0, 7,
                    "stop loss (-8%)")])
        self.assertEqual(len(self.c.rows_between("2026-10-19", "2026-10-19")), 1)
        self.assertEqual(self.c.rows_between("2026-10-20", "2026-10-20"), [])


class TheDailyAnalysisOnlyObserves(CoachCase):

    def test_the_mistakes_a_script_can_see(self):
        m = self.c.mistakes
        base = {"pnl_pct": -2.0, "stop_pct": 8.0, "score": 7, "min_score": 7, "exit_rule": "time limit"}
        self.assertEqual(m(base), [])
        self.assertEqual(m(dict(base, pnl_pct=-10.5))[0][0], "loss past the stop")
        self.assertEqual(m(dict(base, score=6))[0][0], "bought under the bar")
        self.assertEqual(m(dict(base, chased=0.95))[0][0], "chased")
        self.assertEqual(m(dict(base, after_exit_pct=4.0))[0][0], "early exit")
        self.assertEqual(m(dict(base, after_exit_pct=4.0, exit_rule="stop loss")), [],
                         "a stop doing its job is not an early exit")
        self.assertEqual(m(dict(base, best_pct=6.0, pnl_pct=-1.0))[0][0], "gave back a gain")

    def test_belfort_is_judged_against_his_widest_stop(self):
        self.belfort(self.BEL[:1] + [dict(self.BEL[1], price=86.0, notional=860.0, realised_pnl=-140.0,
                                          reason="STOP LOSS: -14%")])
        r = self.c.belfort_rows()[0]
        self.assertEqual(r["stop_pct"], 15.0, "his per-stock stops go to 15%")
        self.assertEqual(self.c.mistakes(r), [], "-14% is inside a 15% stop")

    def test_it_writes_the_journal_and_nothing_else(self):
        self.belfort(self.BEL)
        before = {p for p in self.root.rglob("*") if p.is_file() and "scripts" not in p.parts}
        rows, a = self.c.run_daily("2026-10-19")
        after = {p for p in self.root.rglob("*") if p.is_file() and "scripts" not in p.parts}
        self.assertEqual({p.relative_to(self.root).as_posix() for p in after - before},
                         {"coach/journal/2026-10-19.md", "coach/journal/2026-10-19.json"},
                         "the daily routine changes no rule and no book")
        md = (self.root / "coach/journal/2026-10-19.md").read_text()
        for heading in ("What went well", "What went badly", "Mistakes", "Patterns", "Observation only"):
            self.assertIn(heading, md)

    def test_chasing_is_measured_from_bolts_hourly_prices(self):
        b = load("bolt_chase", "bolt.py")
        now = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
        spark = [100 + i * 0.1 for i in range(168)]            # rising all week
        b.MARKET.parent.mkdir(parents=True, exist_ok=True)
        b.MARKET.write_text(json.dumps({"fetched_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        "coins": {"SOL": {"spark": spark}}}))
        top = {"utc": (now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ"), "price": spark[-11]}
        self.assertGreaterEqual(self.c._chased(b, "SOL", top), 0.95, "bought at the top of the day")
        low = dict(top, price=min(spark[-34:-10]))      # the 24 hours before the buy
        self.assertEqual(self.c._chased(b, "SOL", low), 0.0)


class TheWeeklyReviewOnlyProposes(CoachCase):

    def week(self, n):
        sells = [("SOL", "2026-10-19T10:00:00Z", 100.0, f"2026-10-2{i % 5}T18:00:00Z", 101.0, 1.0, 2.0, 7,
                  "trailing stop (8% off its best, +2.0%)") for i in range(n)]
        self.bolt(sells)

    def test_too_few_trades_and_no_question_is_asked(self):
        self.week(self.c.MIN_WEEK_TRADES - 1)
        review = self.c.run_weekly("2026-10-25", ask=lambda *a: self.fail("asked on too little data"))
        self.assertIn("no rule change is proposed", review)

    def test_the_week_and_the_rules_go_to_the_coach_and_come_back_as_proposals(self):
        self.week(self.c.MIN_WEEK_TRADES)
        asked = []

        def ask(agent, question, timeout):
            asked.append((agent, question))
            return True, "## STOP\n- x\n## START\n- y\n## CONTINUE\n- z\n## UPDATED RULES FOR NEXT WEEK\nno change"
        self.c.run_weekly("2026-10-25", ask=ask)
        agent, q = asked[0]
        self.assertEqual(agent, "coach")
        self.assertIn("Use ONLY the data below", q)
        b = load("bolt_rules", "bolt.py")
        self.assertIn(f"trailing stop {b.TRAIL_PCT:g}% under the best price", q, "the rules as the script holds them")
        self.assertIn('"summary_by_agent"', q)
        saved = json.loads((self.root / "coach/weekly/2026-10-25.json").read_text())
        self.assertEqual((saved["status"], saved["start"]), ("proposed", "2026-10-19"))
        self.assertIn("Nothing here changes a rule until you approve it",
                      (self.root / "coach/weekly/2026-10-25.md").read_text())

    def test_the_coach_is_one_sonnet_question_on_the_plan_that_never_falls_back(self):
        s = json.loads((ROOT / "tasks" / "engines.json").read_text())["agents"]["coach"]
        self.assertEqual((s["engine"], s["model"], s["max_turns"], s["fallback"]), ("claude", "sonnet", 1, "none"))
        self.assertIn("if not workspace.is_dir():", (SCRIPTS / "wake.py").read_text(),
                      "wake.py test does not try to load instructions it has none of")


class WhereItShowsAndWhenItRuns(unittest.TestCase):

    def test_page_route_navigation_and_timers(self):
        self.assertIn("app.get('/api/coach'", (ROOT / "mission-control-api" / "coach.js").read_text())
        server = (ROOT / "mission-control-api" / "server.js").read_text()
        self.assertIn("coachRoutes.register(app);", server)
        self.assertIn("app.get('/coach'", server)
        village = (ROOT / "mission-control-api" / "public" / "village.html").read_text()
        self.assertIn("coach: '/coach?embed=1'", village)
        self.assertIn("['coach', '📓', 'Trading Coach']", village)
        self.assertIn("'coach-close'", village)
        self.assertIn("coach.py daily", (ROOT / "deploy" / "coach-daily.service").read_text())
        self.assertIn("OnCalendar=Sun", (ROOT / "deploy" / "coach-weekly.timer").read_text())
        self.assertIn("coach/journal/", (ROOT / ".gitignore").read_text())

    def test_the_books_carry_what_the_journal_needs(self):
        self.assertIn('"best_pct": round(pos.get("high_water_pct") or 0.0, 2)', (SCRIPTS / "bolt.py").read_text())
        self.assertIn('"exit_reason": t.get("reason") or "", "entry_reason": entry.get("reason") or ""',
                      (SCRIPTS / "belfort-trade.py").read_text())


if __name__ == "__main__":
    unittest.main()
