"""The trading coach: a journal, a daily analysis that only observes, a
weekly review that only proposes (scripts/coach.py).

    python3 -m unittest tests.test_coach -b

Owner, 2026-10-19: "Create and maintain a daily trading journal ... Do NOT
change any trading rules or strategies during the daily analysis ... Every
Sunday ... propose specific, actionable changes."
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
        rows, a = self.c.run_daily("2026-10-19", "belfort")
        after = {p for p in self.root.rglob("*") if p.is_file() and "scripts" not in p.parts}
        self.assertEqual({p.relative_to(self.root).as_posix() for p in after - before},
                         {"coach/belfort/journal/2026-10-19.md", "coach/belfort/journal/2026-10-19.json"},
                         "the daily routine changes no rule and no book")
        md = (self.root / "coach/belfort/journal/2026-10-19.md").read_text()
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
        review = self.c.run_weekly("2026-10-25", "bolt", ask=lambda *a: self.fail("asked on too little data"))
        self.assertIn("no rule change is proposed", review)

    def test_the_week_and_the_rules_go_to_the_coach_and_come_back_as_proposals(self):
        self.week(self.c.MIN_WEEK_TRADES)
        asked = []

        def ask(agent, question, timeout):
            asked.append((agent, question))
            return True, "## STOP\n- x\n## START\n- y\n## CONTINUE\n- z\n## UPDATED RULES FOR NEXT WEEK\nno change"
        self.c.run_weekly("2026-10-25", "bolt", ask=ask)
        agent, q = asked[0]
        self.assertEqual(agent, "coach")
        self.assertIn("Use ONLY the data below", q)
        self.assertIn("You are Bolt's performance coach", q)
        b = load("bolt_rules", "bolt.py")
        self.assertIn(f"trailing stop {b.TRAIL_PCT:g}% under the best price", q, "the rules as the script holds them")
        self.assertIn('"summary_by_agent"', q)
        self.assertIn("Say whether the news check helped", q)
        saved = json.loads((self.root / "coach/bolt/weekly/2026-10-25.json").read_text())
        self.assertEqual((saved["status"], saved["start"], saved["coach"]), ("proposed", "2026-10-19", "bolt"))
        self.assertIn("Nothing here changes a rule until you approve it",
                      (self.root / "coach/bolt/weekly/2026-10-25.md").read_text())

    def test_the_coach_is_one_sonnet_question_on_the_plan_that_never_falls_back(self):
        s = json.loads((ROOT / "tasks" / "engines.json").read_text())["agents"]["coach"]
        self.assertEqual((s["engine"], s["model"], s["max_turns"], s["fallback"]), ("claude", "sonnet", 1, "none"))
        self.assertIn("if not workspace.is_dir():", (SCRIPTS / "wake.py").read_text(),
                      "wake.py test does not try to load instructions it has none of")


class AceHasACoach(CoachCase):
    """Owner, 2026-10-22: "Can i get a coach for ace like how belfort has,
    with similar rules?" - option A: his bets in the journal and the daily
    look now, and a Sunday review that only comments on him until his
    preregistration ends. The bets are his real ones (tests/fixtures/
    ace-board), the price is the real DraftKings line on BKN @ MIA."""

    BANK = json.loads((ROOT / "tests/fixtures/ace-board/bankroll.json").read_text())
    CTX = json.loads((ROOT / "tests/fixtures/ace-board/context-nba-401898404.json").read_text())

    def setUp(self):
        super().setUp()
        (self.root / "agents/ace/state").mkdir(parents=True)
        self.clv = load("ace_clv_test", "ace-clv.py")

    def bets(self, settled=None):
        lines = []
        for b in settled if settled is not None else self.BANK["settled_bets"]:
            bet = {k: v for k, v in b.items() if k not in ("result", "profit", "settled_utc", "detail")}
            lines.append(dict(bet, kind="bet"))
            lines.append({"kind": "settle", "id": b["id"], "utc": b["settled_utc"], "result": b["result"],
                          "profit": b["profit"], "detail": b["detail"]})
        (self.root / "agents/ace/state/bets.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))

    def close_at(self, away_ml, home_ml, away_pct):
        """The real line when he bet, then a close moved to these numbers."""
        before = datetime(2026, 10, 14, 18, 30, tzinfo=timezone.utc)
        self.clv.record_lines([self.CTX], now=before, path=self.root / "agents/ace/state/lines.json")
        moved = json.loads(json.dumps(self.CTX))
        moved["odds"].update(moneyline_away=away_ml, moneyline_home=home_ml,
                             novig_away_pct=away_pct, novig_home_pct=round(100 - away_pct, 1))
        self.clv.record_lines([moved], now=before + timedelta(hours=4.5),
                              path=self.root / "agents/ace/state/lines.json")

    def ace(self, day="2026-10-14"):
        return [r for r in self.c.rows_between(day, day) if r["agent"] == "Ace"]

    def test_his_settled_bets_are_journaled_on_their_eastern_day(self):
        # Settled 03:10 UTC on the 15th is 23:10 ET on the 14th.
        self.bets()
        rows = {r["symbol"]: r for r in self.ace()}
        self.assertEqual(sorted(rows), ["BKN ML", "MOST ML"])
        bkn = rows["BKN ML"]
        self.assertEqual((bkn["asset"], bkn["side"], bkn["entry_price"], bkn["size_usd"], bkn["pnl_usd"],
                          bkn["pnl_pct"]), ("NBA bet", "away", 220, 100.0, 220.0, 220.0))
        self.assertIn("edge +6.0 pts vs DraftKings 30.0%", bkn["setup"], "ace-judge.py's own bar")
        self.assertIn("math rule: blind estimate 45%", bkn["entry_reason"])
        self.assertTrue(bkn["exit_reason"].startswith("won: BKN 108 @ MIA 101"))
        self.assertEqual(self.c.ace_rows()[0]["exit_price"], "-", "no closing line kept: none claimed")

    def test_the_close_grades_the_bet(self):
        self.bets()
        self.close_at(200, -245, 32.1)                 # the market moved his way
        bkn = next(r for r in self.ace() if r["symbol"] == "BKN ML")
        self.assertEqual((bkn["exit_price"], bkn["clv_book"]), (200, "DraftKings"))
        self.assertEqual(bkn["clv_pct"], self.clv.clv_at(220, 32.1))
        self.assertIn("closed at 200, DraftKings CLV +2.7%", bkn["exit_reason"])
        self.assertEqual([m for m in self.c.mistakes(bkn)], [])

    def test_a_price_the_market_beat_is_the_mistake_not_a_loss(self):
        self.bets()
        self.close_at(250, -310, 27.5)                 # he won, but the close beat his price
        rows = {r["symbol"]: r for r in self.ace()}
        self.assertEqual(self.c.mistakes(rows["BKN ML"]),
                         [("beaten by the close", "took 220, closed 250: DraftKings CLV -12.0%")])
        self.assertEqual(self.c.mistakes(rows["MOST ML"]), [], "lost, on his bar: not a mistake")

    def test_a_bet_off_his_bar_is_named(self):
        b = dict(self.BANK["settled_bets"][0], my_pct=32.0)    # counts as 30.8% against 30.0%
        self.bets([b])
        kinds = [k for k, _ in self.c.mistakes(self.ace()[0])]
        self.assertEqual(kinds, ["bet under the bar"])

    def test_the_daily_look_measures_him_by_the_close(self):
        self.bets()
        self.close_at(200, -245, 32.1)
        rows, a = self.c.run_daily("2026-10-14", "ace")
        s = a["ace"]
        self.assertEqual((s["bets"], s["with_close"], s["beat_close"], s["frozen_until"]),
                         (2, 1, 1, "2026-12-01"))
        self.assertEqual(set(s["by sport"]), {"NBA", "CFB"})
        self.assertEqual(set(s["favourite or underdog"]), {"underdog"})
        md = (self.root / "coach/ace/journal/2026-10-14.md").read_text()
        self.assertIn("closing-line value +2.70% a bet over 1, beat the close on 1", md)
        self.assertIn("Rules frozen until 2026-12-01", md)

    def week_of_bets(self, n):
        base = self.BANK["settled_bets"][0]
        self.bets([dict(base, id=f"b{i}", settled_utc=f"2026-10-2{i % 5} 03:00:00",
                        event_id=str(i)) for i in range(n)])

    def ask_week(self, end):
        asked = []
        self.c.run_weekly(end, "ace", ask=lambda agent, q, t: asked.append(q) or (True, "## STOP\n- x"))
        return asked

    def test_until_the_preregistration_ends_the_review_proposes_nothing_for_him(self):
        self.week_of_bets(self.c.MIN_WEEK_BETS)
        (q,) = self.ask_week("2026-10-25")
        self.assertIn("You are Ace's performance coach - Ace is a paper sports bettor", q)
        self.assertIn("HIS RULES ARE FROZEN until 2026-12-01", q)
        self.assertIn("propose NO change", q)
        self.assertIn("## NOTES FOR THE 2026-12-01 READ-OUT", q)
        self.assertIn('"status": "FROZEN until 2026-12-01 by his preregistration"', q)
        self.assertIn(f"pulled {load('aj', 'ace-judge.py').SHRINK:.0%} of the way", q, "rules from ace-judge.py")
        for other in ("Belfort", "Bolt"):
            self.assertNotIn(other, q, "his coach sees his numbers and rules alone")
        md = (self.root / "coach/ace/weekly/2026-10-25.md").read_text()
        self.assertIn("**Ace**: 10 bet(s) settled", md)

    def test_after_it_he_is_reviewed_like_the_others(self):
        base = self.BANK["settled_bets"][0]
        self.bets([dict(base, id=f"b{i}", settled_utc=f"2026-12-0{1 + i % 5} 03:00:00", event_id=str(i))
                   for i in range(self.c.MIN_WEEK_BETS)])
        (q,) = self.ask_week("2026-12-06")
        self.assertNotIn("FROZEN until", q.split("CURRENT RULES")[0])
        self.assertIn("Judge him on closing-line value, not wins", q)

    def test_too_few_bets_and_trades_and_no_question_is_asked(self):
        self.week_of_bets(self.c.MIN_WEEK_BETS - 1)
        review = self.c.run_weekly("2026-10-25", "ace", ask=lambda *a: self.fail("asked on too little data"))
        self.assertIn("9 settled bet(s) this week", review)
        self.assertIn("no rule change is proposed", review)

    def test_the_freeze_date_is_the_preregistrations(self):
        aj = load("aj_frozen", "ace-judge.py")
        prereg = (ROOT / "agents/ace/PREREGISTRATION.md").read_text()
        self.assertIn(f"before **{aj.FROZEN_UNTIL}**", prereg)

    def test_his_backfill_touches_his_journal_alone(self):
        self.bets()
        with unittest.mock.patch.object(self.c.et_time, "eastern_now", lambda: datetime(2026, 10, 15, 9, 0)):
            self.assertEqual(self.c.main(["backfill", "--book", "ace", "--since", "2026-10-14"]), 0)
        saved = json.loads((self.root / "coach/ace/journal/2026-10-14.json").read_text())
        self.assertEqual([r["symbol"] for r in saved["rows"]], ["BKN ML", "MOST ML"])
        self.assertFalse((self.root / "coach/bolt").exists())
        self.assertFalse((self.root / "coach/belfort").exists())

    def test_the_page_names_him(self):
        page = (ROOT / "mission-control-api/public/coach.html").read_text()
        self.assertIn("Ace (sports bets)", page)
        self.assertIn("a.ace", page)
        self.assertIn("(w.data || {}).ace", page, "his Sunday card too")


class ThreeCoachesNeverMixed(CoachCase):
    """Owner, 2026-10-23: "make bolt, belfort and aces coach all separate from
    each other so me and the data [don't] get confused". Each coach keeps its
    own files and asks its own question, which sees only its agent."""

    def setUp(self):
        super().setUp()
        (self.root / "agents/ace/state").mkdir(parents=True)

    def sol(self, day="2026-10-19", n=1):
        self.bolt([("SOL", f"{day}T10:00:00Z", 100.0, f"{day}T18:00:00Z", 104.0, 4.0, 9.0, 8,
                    "trailing stop (8% off its best, +9.0%)")] * n)

    def test_each_coach_journals_only_its_own_agent(self):
        self.belfort(self.BEL)
        self.sol()
        with unittest.mock.patch.object(self.c.et_time, "eastern_now", lambda: datetime(2026, 10, 20, 0, 30)):
            self.assertEqual(self.c.main(["daily"]), 0)
        got = {c: [r["agent"] for r in json.loads((self.root / f"coach/{c}/journal/2026-10-19.json").read_text())["rows"]]
               for c in ("belfort", "bolt", "ace")}
        self.assertEqual(got, {"belfort": ["Belfort"], "bolt": ["Bolt"], "ace": []})
        shown = self.c.show()["coaches"]
        self.assertEqual(list(shown), ["belfort", "bolt", "ace"])
        self.assertEqual(shown["bolt"]["daily"][0]["analysis"]["books"].keys(), {"Bolt"})
        self.assertIn("# Belfort's journal - 2026-10-19",
                      (self.root / "coach/belfort/journal/2026-10-19.md").read_text())

    def test_three_questions_each_with_its_own_rules_only(self):
        trades = []
        for sym in ("NVDA", "AMD", "MU", "TSM", "ARM"):
            trades += [dict(t, symbol=sym) for t in self.BEL]
        self.belfort(trades)
        self.sol(n=self.c.MIN_WEEK_TRADES)
        asked = []
        ask = lambda agent, q, t: asked.append(q) or (True, "## STOP\n- x")
        real = self.c._load
        with unittest.mock.patch.object(self.c, "_load",
                                        lambda name, file: types_ask(ask) if file == "wake.py" else real(name, file)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.c.main(["weekly", "--end", "2026-10-25"])
        self.assertEqual(len(asked), 2, "Belfort and Bolt had a week's data; Ace had none and is not asked")
        mine = {"Belfort": '"Belfort (stocks)"', "Bolt": '"Bolt (crypto, both books)"', "Ace": '"Ace (sports bets)"'}
        for q, who in zip(asked, ("Belfort", "Bolt")):
            self.assertIn(f"You are {who}'s performance coach", q)
            self.assertEqual([k for k, rules in mine.items() if rules in q], [who], "its own rules only")
        for c in ("belfort", "bolt", "ace"):
            self.assertTrue((self.root / f"coach/{c}/weekly/2026-10-25.json").is_file(), c)
        self.assertIn("0 settled bet(s) this week", (self.root / "coach/ace/weekly/2026-10-25.md").read_text())

    def test_the_combined_journal_is_split_once_and_kept(self):
        old = self.root / "coach/journal"
        old.mkdir(parents=True)
        (self.root / "coach/weekly").mkdir()
        (self.root / "coach/weekly/2026-10-19.json").write_text("{}")
        self.belfort(self.BEL)
        self.sol()
        rows = self.c.rows_between("2026-10-19", "2026-10-19")
        rows[1]["chased"] = 0.95                    # saved when Bolt's hourly prices were fresh
        (old / "2026-10-19.json").write_text(json.dumps({"day": "2026-10-19", "rows": rows}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.c.main(["split"])
        self.assertIn("split the combined journal: 1 day(s)", out.getvalue())
        bolt = json.loads((self.root / "coach/bolt/journal/2026-10-19.json").read_text())
        self.assertEqual([(r["agent"], r["chased"]) for r in bolt["rows"]], [("Bolt", 0.95)])
        self.assertEqual([r["agent"] for r in json.loads(
            (self.root / "coach/belfort/journal/2026-10-19.json").read_text())["rows"]], ["Belfort"])
        self.assertFalse(old.exists())
        self.assertTrue((self.root / "coach/combined-before-split/journal/2026-10-19.json").is_file())
        self.assertTrue((self.root / "coach/combined-before-split/weekly/2026-10-19.json").is_file())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.c.main(["split"])
        self.assertIn("nothing left to split", out.getvalue())

    def test_a_backfill_keeps_what_the_other_book_saved(self):
        # Bolt's chase reading needs hourly prices only a week old: a backfill
        # of his news book must not wipe it from his plain book's older days.
        journal = self.root / "coach/bolt/journal"
        self.sol("2026-10-14")
        self.c.run_daily("2026-10-14", "bolt")
        saved = json.loads((journal / "2026-10-14.json").read_text())
        saved["rows"][0]["chased"] = 0.95
        (journal / "2026-10-14.json").write_text(json.dumps(saved))
        self.bolt([("ETH", "2026-10-14T11:00:00Z", 100.0, "2026-10-14T19:00:00Z", 103.0, 3.0, 5.0, 8,
                    "trailing stop (8% off its best, +5.0%)")], news=True)
        with unittest.mock.patch.object(self.c.et_time, "eastern_now", lambda: datetime(2026, 10, 15, 9, 0)):
            self.assertEqual(self.c.main(["backfill", "--book", "bolt-news", "--since", "2026-10-14"]), 0)
        saved = json.loads((journal / "2026-10-14.json").read_text())
        self.assertEqual([(r["agent"], r.get("chased")) for r in saved["rows"]],
                         [("Bolt", 0.95), ("Bolt + news", None)])
        self.assertIn("chased", [m["kind"] for m in saved["analysis"]["mistakes"]])

    def test_the_page_switches_between_coaches(self):
        page = (ROOT / "mission-control-api/public/coach.html").read_text()
        self.assertIn("data-coach=", page)
        self.assertIn("['belfort', 'Belfort', 'stocks'], ['bolt', 'Bolt', 'crypto, both books'], ['ace', 'Ace', 'sports bets']",
                      page)
        self.assertIn("((D || {}).coaches || {})[coach]", page)
        self.assertNotIn("D.daily", page)
        self.assertNotIn("D.weekly", page)


def types_ask(ask):
    import types
    return types.SimpleNamespace(ask=ask)


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
        self.assertIn("\ncoach/\n", (ROOT / ".gitignore").read_text())
        self.assertIn("TimeoutStartSec=1200", (ROOT / "deploy" / "coach-weekly.service").read_text(),
                      "three questions of up to 300 seconds in turn")

    def test_the_books_carry_what_the_journal_needs(self):
        self.assertIn('"best_pct": round(pos.get("high_water_pct") or 0.0, 2)', (SCRIPTS / "bolt.py").read_text())
        self.assertIn('"exit_reason": t.get("reason") or "", "entry_reason": entry.get("reason") or ""',
                      (SCRIPTS / "belfort-trade.py").read_text())


if __name__ == "__main__":
    unittest.main()
