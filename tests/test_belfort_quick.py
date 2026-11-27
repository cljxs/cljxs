"""Belfort Quick: short swing trades on Belfort's stocks, by plain code
(scripts/belfort-quick.py).

    python3 -m unittest tests.test_belfort_quick -b

Owner, 2026-11-06: "a stock market trading bot similar to the Bolt quick
trading bot ... a 5% target gain and the same stop loss rules, but the entry
and exit rules must be different from Belforts". Chosen after a replay over
two years of real prices: Belfort's 79 stocks, up to 10 trading days, 15-30%
by score, a light SPY brake, one buy a day at 15:30 ET.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL = json.loads((FIXTURES / "yahoo-daily-nvda-spy-2026-11-06.json").read_text())


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class QuickCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for d in ("agents/belfort/state", "agents/belfort/data"):
            (self.root / d).mkdir(parents=True)
        (self.root / "scripts").symlink_to(SCRIPTS)
        env = unittest.mock.patch.dict(os.environ, {"ECOSYSTEM_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.q = load(f"belfort_quick_t{id(self)}", "belfort-quick.py")

    def x(self, **kw):
        """Features that score 10/10 and pass every filter, changed by kw."""
        base = {"price": 100.0, "change_pct": 1.0, "sma20": 99.0, "sma50": 95.0, "rsi14": 55.0,
                "atr_pct": 2.5, "volume_pace": 1.5, "vs_spy_5d": 1.0, "spy_change_pct": 0.2}
        base.update(kw)
        return base


class TheScore(QuickCase):
    """Buy a pause inside an uptrend, on the day it turns back up."""

    def test_each_point(self):
        self.assertEqual(self.q.score(self.x())[0], 10)
        for kw, lost in ((dict(sma50=99.5), 3), (dict(price=104.0, sma20=100.0), 2), (dict(change_pct=-0.2), 2),
                         (dict(volume_pace=1.1), 1), (dict(rsi14=70.0), 1), (dict(vs_spy_5d=-0.5), 1)):
            self.assertEqual(self.q.score(self.x(**kw))[0], 10 - lost, kw)

    def test_never(self):
        self.assertIsNone(self.q.refusal(self.x()))
        self.assertIn("not chasing", self.q.refusal(self.x(change_pct=7.2)))
        self.assertIn("1.2% a day", self.q.refusal(self.x(atr_pct=1.2)), "too calm for a 5% target")
        self.assertIn("6.5% a day", self.q.refusal(self.x(atr_pct=6.5)), "too wild for a 2.5% stop")
        self.assertIsNone(self.q.market_brake(-1.4))
        self.assertIn("SPY -1.6%", self.q.market_brake(-1.6))

    def test_sizes_by_score(self):
        book = self.q.seed(datetime(2026, 11, 6, tzinfo=timezone.utc))
        self.assertEqual([self.q.size_for(book, 10000.0, sc, 100.0) for sc in (7, 8, 9, 10)], [15, 20, 25, 30])
        book["cash"] = 1000.0
        self.assertEqual(self.q.size_for(book, 10000.0, 10, 100.0), 5, "never under the 5% cash floor")


class TheExits(QuickCase):
    """Bolt Quick's: stop 2.5% under the price paid, trailing 2.5% under the
    best; +5% sold; 10 trading days. A daily bar cannot say which came first,
    so a day that touched both the stop and the target is a loss."""

    def pos(self):
        return {"symbol": "X", "entry_price": 100.0, "best": 100.0}

    def test_stop_target_and_both(self):
        q = self.q
        self.assertEqual(q.day_exit(self.pos(), 100, 101, 97.6, 100, 1), None)
        self.assertEqual(q.day_exit(self.pos(), 100, 101, 97.4, 100, 1), (97.5, "stop"))
        self.assertEqual(q.day_exit(self.pos(), 100, 105.1, 99, 104, 1), (105.0, "target +5%"))
        self.assertEqual(q.day_exit(self.pos(), 100, 106, 97, 100, 1)[1], "stop", "both touched: a loss")
        self.assertEqual(q.day_exit(self.pos(), 96, 99, 95, 97, 1), (96, "stop (gapped through at the open)"))
        self.assertEqual(q.day_exit(self.pos(), 100, 101, 99, 100.5, 10), (100.5, "time limit (10 trading days)"))

    def test_up_two_and_a_half_the_stop_is_at_breakeven(self):
        p = self.pos()
        self.assertIsNone(self.q.day_exit(p, 100, 102.5, 99.5, 102, 1))
        self.assertAlmostEqual(self.q.stop_for(p), 99.9375)
        self.assertEqual(self.q.day_exit(p, 101, 101.5, 99.9, 100, 2), (self.q.stop_for(p), "trailing stop"))


class TheReplay(QuickCase):
    """On real Yahoo bars: the parse, and the features read off them."""

    def test_a_real_yahoo_reply(self):
        b = self.q.parse_chart(REAL["NVDA"])
        self.assertEqual(len(b["c"]), 120)
        self.assertEqual(b["dates"], sorted(b["dates"]))
        self.assertEqual(b["dates"][-1], "2026-11-06")
        spy = self.q.parse_chart(REAL["SPY"])
        x = self.q.features(b["c"], b["h"], b["l"], b["v"], spy["c"])
        self.assertAlmostEqual(x["sma20"], sum(b["c"][-20:]) / 20)
        self.assertAlmostEqual(x["change_pct"], (b["c"][-1] / b["c"][-2] - 1) * 100)
        self.assertTrue(0 < x["atr_pct"] < 20 and 0 <= x["rsi14"] <= 100, x)
        self.assertIsNone(self.q.parse_chart({"chart": {"result": None}}))

    def test_a_replay_balances_and_buys_once_a_day(self):
        # A made-up rising stock and a flat SPY: the replay must buy, sell by
        # the rules, balance, and never buy twice on one day.
        b = self.q.parse_chart(REAL["NVDA"])
        spy = self.q.parse_chart(REAL["SPY"])
        out = self.q.replay({"NVDA": b}, spy, lambda s: "semiconductors")
        book = out["book"]
        self.assertEqual(self.q.problems(book), [])
        days = [t["day"] for t in book["trades"] if t["side"] == "buy"]
        self.assertEqual(len(days), len(set(days)), "one buy a day")
        for t in book["trades"]:
            if t["side"] == "sell":
                self.assertTrue(t["day"] > next(x["day"] for x in reversed(book["trades"][:book["trades"].index(t)])
                                               if x["side"] == "buy" and x["symbol"] == t["symbol"]))


class TradingLive(QuickCase):
    """The tick on Belfort's own files: one buy at the 15:30 check, exits
    every tick, earnings and the SPY brake honoured, the book balanced."""

    def docs(self, last=None, spy_now=500.0, vol_last=2_000_000):
        # 80 days: +0.5% then -0.3%, ending on an up day - an uptrend near its
        # 20-day, RSI about 62, a 2% daily range, today's volume twice usual.
        closes, c = [], 100.0
        for i in range(80):
            c *= 1.005 if i % 2 == 1 else 0.997
            closes.append(round(c, 4))
        days = [(datetime(2026, 7, 1) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(80)]
        bars = [[d, x, x * 1.01, x * 0.99, x, 1_000_000] for d, x in zip(days, closes)]
        bars[-1][5] = vol_last
        price = last if last is not None else closes[-1]
        spy = [[d, 500.0] for d in days]
        return ({"quotes": {"NVDA": {"price": price}},
                 "benchmarks": {"SPY": {"price": spy_now, "history": spy}}},
                {"bars": {"NVDA": {"bars": bars}}})

    def at(self, hh, mm, day=6):
        return datetime(2026, 11, day, hh + 5, mm, tzinfo=timezone.utc)          # EST: UTC-5 in November

    def test_one_buy_at_the_check_sized_by_score(self):
        quotes, bars = self.docs()
        self.assertEqual(self.q.tick(self.at(12, 35), quotes, bars), [], "no buy before 15:30")
        lines = self.q.tick(self.at(15, 35), quotes, bars)
        self.assertIn("bought", lines[-1])
        self.assertIn("scored 10/10", lines[-1])
        book = json.loads(self.q.BOOK.read_text())
        self.assertAlmostEqual(book["trades"][0]["notional"], 3000.0, delta=book["trades"][0]["price"],
                               msg="10/10: 30% of $10,000")
        self.assertEqual(self.q.tick(self.at(15, 45), quotes, bars), [], "the check is done for the day")
        self.assertEqual(self.q.problems(book), [])

    def test_the_stop_sells_on_a_later_tick(self):
        quotes, bars = self.docs()
        self.q.tick(self.at(15, 35), quotes, bars)
        entry = json.loads(self.q.BOOK.read_text())["positions"][0]["entry_price"]
        quotes["quotes"]["NVDA"]["price"] = entry * 0.97
        lines = self.q.tick(self.at(10, 5, day=9), quotes, bars)
        self.assertIn("stop loss (-2.5%)", lines[0])
        self.assertEqual(json.loads(self.q.BOOK.read_text())["positions"], [])

    def test_no_buy_near_earnings_or_in_a_selloff(self):
        quotes, bars = self.docs()
        (self.root / "agents/belfort/data/earnings.json").write_text(json.dumps({"dates": {"NVDA": "2026-11-11"}}))
        lines = self.q.tick(self.at(15, 35), quotes, bars)
        self.assertIn("no buy", lines[-1], "earnings in 3 trading days")
        (self.root / "agents/belfort/data/earnings.json").unlink()
        quotes, bars = self.docs(spy_now=490.0)                                     # SPY -2% today
        lines = self.q.tick(self.at(15, 35, day=9), quotes, bars)
        self.assertIn("SPY -2.0% today", lines[-1])

    def test_out_the_day_before_earnings(self):
        quotes, bars = self.docs()
        self.q.tick(self.at(15, 35), quotes, bars)
        (self.root / "agents/belfort/data/earnings.json").write_text(json.dumps({"dates": {"NVDA": "2026-11-10"}}))
        lines = self.q.tick(self.at(10, 5, day=9), quotes, bars)
        self.assertIn("earnings 2026-11-10 - out the day before", lines[0])

    def test_ten_trading_days(self):
        self.assertEqual(self.q.trading_days_held("2026-11-06", datetime(2026, 11, 20).date()), 10)
        self.assertEqual(self.q.trading_days_held("2026-11-06", datetime(2026, 11, 9).date()), 1, "the weekend skipped")


class WiredIn(QuickCase):

    def test_timer_page_coach(self):
        unit = (ROOT / "deploy" / "belfort-exits.service").read_text()
        self.assertIn("scripts/belfort-quick.py tick", unit)
        self.assertLess(unit.index("belfort-trade.py exits"), unit.index("belfort-quick.py tick"))
        html = (ROOT / "mission-control-api" / "public" / "markets.html").read_text()
        self.assertIn('data-t="quick"', html)
        self.assertIn("/api/belfort/quick", html)
        self.assertIn("app.get('/api/belfort/quick'", (ROOT / "mission-control-api" / "belfort.js").read_text())
        coach = load("coach_quick", "coach.py")
        self.assertEqual(coach.BOOK_COACH["belfort-quick"], "belfort")
        self.assertIn("quick book", coach.rules_for("belfort")["Belfort (stocks)"])

    def test_its_sales_are_in_belforts_coach(self):
        when = "2026-11-06 20:30:00"
        book = self.q.seed(datetime(2026, 11, 6, tzinfo=timezone.utc))
        self.q.buy(book, "NVDA", 10, 100.0, 9, "scored 9/10: uptrend", "2026-11-06", when)
        self.q.sell(book, book["positions"][0], 105.0, "target +5%", "2026-11-09", "2026-11-09 15:00:00")
        self.q.BOOK.write_text(json.dumps(book))
        coach = load("coach_quick2", "coach.py")
        rows = coach.belfort_quick_rows()
        self.assertEqual([(r["agent"], r["symbol"], r["pnl_usd"], r["score"], r["stop_pct"]) for r in rows],
                         [("Belfort Quick", "NVDA", 50.0, 9, 2.5)])


if __name__ == "__main__":
    unittest.main()
