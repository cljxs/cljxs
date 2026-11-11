"""The Line Shopper: Pinnacle's true odds against nine US books
(scripts/ace-shop.py).

    python3 -m unittest tests.test_ace_shop -b

Owner, 2026-11-11, from WinnerOdds' True Odds Calculator: bet a US price
worth +2% over Pinnacle's margin-free price, the margin taken out by every
method on that page, on its own paper $10,000. The margin methods are pinned
to the page's own worked example (Real Madrid 2.09 / 3.59 / 3.77: 2.1230 by
the log method, +1.27% for 2.15). The Odds API reply is the shape of the
documented one already in tests/fixtures; `check nba` reads a real one.
Each test names the way the book could fool itself.
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
NOW = datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)        # 10:00 ET
START = NOW + timedelta(hours=3)


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def event(eid, home, away, books, start=START):
    """books: {key: (home decimal, away decimal, minutes old)}"""
    return {"id": eid, "sport_key": "basketball_nba", "commence_time": iso(start),
            "home_team": home, "away_team": away,
            "bookmakers": [{"key": k, "last_update": iso(NOW - timedelta(minutes=m)),
                            "markets": [{"key": "h2h", "last_update": iso(NOW - timedelta(minutes=m)),
                                         "outcomes": [{"name": home, "price": h}, {"name": away, "price": a}]}]}
                           for k, (h, a, m) in books.items()]}


class ShopCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "agents/ace/state").mkdir(parents=True)
        (self.root / "agents/ace/data/context").mkdir(parents=True)
        (self.root / "scripts").symlink_to(SCRIPTS)
        # Ace started with $20,000 here: a shopper reading his file would stake $200, not $100.
        self.ace_bank = json.dumps({"starting_bankroll": 20000.0, "bankroll": 19100.0})
        (self.root / "agents/ace/state/bankroll.json").write_text(self.ace_bank)
        (self.root / "agents/ace/state/bets.jsonl").write_text("")
        env = unittest.mock.patch.dict(os.environ, {"ECOSYSTEM_ROOT": str(self.root), "ODDS_API_KEY": "",
                                                    "ODDS_API_KEY_NFL": "k", "ODDS_API_KEY_SHOP": ""})
        env.start()
        self.addCleanup(env.stop)
        self.s = load(f"ace_shop_t{id(self)}", "ace-shop.py")
        self.calls = []
        self.events = []

    def ctx(self, eid, home, away, short, start=START, sport="nba"):
        c = {"sport": sport, "event_id": eid, "short": short, "start_utc": start.strftime("%Y-%m-%dT%H:%MZ"),
             "status": "STATUS_SCHEDULED", "home": {"abbr": short.split(" @ ")[1], "name": home},
             "away": {"abbr": short.split(" @ ")[0], "name": away}}
        (self.root / f"agents/ace/data/context/{sport}-{eid}.json").write_text(json.dumps(c))
        return c

    def fetcher(self, sport, key, books):
        self.calls.append((sport, tuple(books)))
        return self.events, 400 - len(self.calls)

    def a_game(self, eid="1", home_price=2.30, fanduel_age=5, pinnacle_age=5):
        """BOS @ NYK: Pinnacle 1.67 / 2.31, DraftKings and BetMGM near it;
        FanDuel - the slow book - pays the Knicks `home_price`."""
        self.ctx(eid, "New York Knicks", "Boston Celtics", "BOS @ NYK")
        return event(f"o{eid}", "New York Knicks", "Boston Celtics",
                     {"pinnacle": (2.31, 1.67, pinnacle_age), "draftkings": (2.24, 1.68, 5),
                      "betmgm": (2.25, 1.66, 5), "fanduel": (home_price, 1.62, fanduel_age)})


class TheTrueOddsAreTheirs(ShopCase):

    def test_the_pages_own_example(self):
        c = self.s.true_chances((2.09, 3.59, 3.77))
        self.assertAlmostEqual(1 / c["log"][0], 2.1230, places=4, msg="WinnerOdds: 2.1230 by the log method")
        self.assertEqual(self.s.worth(2.15, c, 0)["log"], 1.27, "and 2.15 is worth 2.15/2.123 - 1 = +1.27%")
        for m, v in c.items():
            self.assertAlmostEqual(sum(v), 1.0, places=9, msg=m)
        self.assertEqual(set(c), set(self.s.METHODS))

    def test_equal_margin_flatters_the_underdog(self):
        c = self.s.true_chances((1.0 + 100 / 150, 2.30))          # Pinnacle -150 / +130
        w = self.s.worth(2.40, c, 1)                                # +140 elsewhere
        self.assertGreater(w["equal margin"], 0.5)
        self.assertLess(w["log"], 0, "the same price is worth nothing by the log method")


class TheVerdictTakesTheWorstMethod(ShopCase):

    def test_a_price_worth_two_percent_under_every_method_clears(self):
        g = self.s.judge_game(self.ctx("1", "New York Knicks", "Boston Celtics", "BOS @ NYK"),
                              self.a_game(home_price=2.50), NOW)
        self.assertEqual(g["bet"], "home")
        r = g["sides"]["home"]
        self.assertEqual((r["book"], r["price"]), ("fanduel", 150))
        self.assertEqual(r["worst_pct"], min(r["worth"].values()))
        self.assertGreaterEqual(r["worst_pct"], 2.0)

    def test_one_method_short_of_two_percent_is_no_bet(self):
        # 2.44 is worth +2.38% by equal margin, +1.46% by the log method.
        g = self.s.judge_game(self.ctx("1", "New York Knicks", "Boston Celtics", "BOS @ NYK"),
                              self.a_game(home_price=2.44), NOW)
        w = g["sides"]["home"]["worth"]
        self.assertGreaterEqual(w["equal margin"], 2.0)
        self.assertLess(min(w.values()), 2.0)
        self.assertIsNone(g["bet"])

    def test_a_slow_us_price_counts_however_old(self):
        # Owner's option B, 2026-11-11: a listed US price is on offer - its age is the edge, not a fault.
        c = self.ctx("1", "New York Knicks", "Boston Celtics", "BOS @ NYK")
        g = self.s.judge_game(c, self.a_game(home_price=2.50, fanduel_age=180), NOW)
        self.assertEqual((g["bet"], g["sides"]["home"]["book"]), ("home", "fanduel"))

    def test_a_stale_pinnacle_is_not_used(self):
        c = self.ctx("1", "New York Knicks", "Boston Celtics", "BOS @ NYK")
        g = self.s.judge_game(c, self.a_game(home_price=2.50, pinnacle_age=75), NOW)
        self.assertIn("75 minutes old - over 60", g["why"])
        self.assertIsNone(g["bet"])
        self.assertEqual(self.s.judge_game(c, self.a_game(home_price=2.50, pinnacle_age=50), NOW)["bet"], "home")


class TheCrowdCheck(ShopCase):
    """Owner, 2026-11-11: "if his price disagrees with every other book means no bet"."""

    def test_pinnacle_against_every_us_book_is_no_bet(self):
        # The Knicks have moved to about 47% everywhere but Pinnacle, which still says 42%:
        # every US book "pays too much" - a stale Pinnacle, not five gifts.
        c = self.ctx("1", "New York Knicks", "Boston Celtics", "BOS @ NYK")
        e = event("o1", "New York Knicks", "Boston Celtics",
                  {"pinnacle": (2.31, 1.67, 5), "draftkings": (2.05, 1.80, 5), "fanduel": (2.08, 1.78, 5),
                   "betmgm": (2.05, 1.78, 5), "betrivers": (2.10, 1.75, 5)})
        g = self.s.judge_game(c, e, NOW)
        self.assertIsNone(g["bet"])
        self.assertIn("probably stale: no bet", g["why"])

    def test_one_slow_book_cannot_pull_the_middle(self):
        # FanDuel alone is far off; the median of four books barely moves, so the check passes.
        c = self.ctx("1", "New York Knicks", "Boston Celtics", "BOS @ NYK")
        e = self.a_game(home_price=2.55)
        e["bookmakers"].append(event("x", "New York Knicks", "Boston Celtics",
                                     {"betrivers": (2.28, 1.66, 5)})["bookmakers"][0])
        g = self.s.judge_game(c, e, NOW)
        homes = sorted(self.s.SHARP.novig(h, a)[0] for h, a in ((2.24, 1.68), (2.25, 1.66), (2.55, 1.62), (2.28, 1.66)))
        self.assertEqual(g["crowd"]["us_median_home_pct"], (homes[1] + homes[2]) / 2, "the middle, not the average")
        self.assertLessEqual(abs(g["crowd"]["pinnacle_home_pct"] - g["crowd"]["us_median_home_pct"]), 3.0)
        self.assertEqual(g["bet"], "home")

    def test_too_few_books_to_ask(self):
        c = self.ctx("1", "New York Knicks", "Boston Celtics", "BOS @ NYK")
        e = event("o1", "New York Knicks", "Boston Celtics", {"pinnacle": (2.31, 1.67, 5), "fanduel": (2.50, 1.62, 5)})
        self.assertIn("too few to check Pinnacle against", self.s.judge_game(c, e, NOW)["why"])

    def test_check_shows_how_old_each_books_prices_are(self):
        lines = self.s.book_ages([self.a_game(home_price=2.50, fanduel_age=40)], NOW)
        fd = next(l for l in lines if "fanduel" in l)
        self.assertIn("40 / 40 / 40", fd)
        self.assertIn("no prices on this plan", next(l for l in lines if "fliff" in l))


    def test_too_good_to_be_true_and_out_of_range(self):
        c = self.ctx("1", "New York Knicks", "Boston Celtics", "BOS @ NYK")
        g = self.s.judge_game(c, self.a_game(home_price=2.80), NOW)
        self.assertIn("stale or wrong line", g["sides"]["home"]["why"])
        long_shot = event("o1", "New York Knicks", "Boston Celtics",
                          {"pinnacle": (6.5, 1.13, 5), "draftkings": (6.2, 1.12, 5), "fanduel": (7.5, 1.10, 5)})
        self.assertIn("outside 20-80%", self.s.judge_game(c, long_shot, NOW)["sides"]["home"]["why"])

    def test_only_its_nine_books_count(self):
        c = self.ctx("1", "New York Knicks", "Boston Celtics", "BOS @ NYK")
        e = event("o1", "New York Knicks", "Boston Celtics",
                  {"pinnacle": (2.31, 1.67, 5), "draftkings": (2.24, 1.68, 5), "betmgm": (2.25, 1.66, 5),
                   "betonlineag": (2.50, 1.60, 5)})
        self.assertEqual(self.s.judge_game(c, e, NOW)["sides"]["home"]["book"], "betmgm")


class ARun(ShopCase):

    def test_bets_on_its_own_book_never_aces(self):
        self.events = [self.a_game(home_price=2.50)]
        out = self.s.run(now=NOW, fetcher=self.fetcher)
        self.assertEqual(len(self.calls), 1, out)
        sport, books = self.calls[0]
        self.assertEqual(books[0], "pinnacle")
        self.assertEqual(len(books), 10, "ten bookmakers: one credit")
        bets = [json.loads(l) for l in (self.root / "agents/ace/state/shop/bets.jsonl").read_text().splitlines()]
        self.assertEqual(len(bets), 1)
        self.assertEqual((bets[0]["selection"], bets[0]["price"], bets[0]["at_book"], bets[0]["stake"]),
                         ("NYK ML", 150, "fanduel", 100.0))
        self.assertEqual((self.root / "agents/ace/state/bets.jsonl").read_text(), "", "Ace's book untouched")
        self.assertEqual((self.root / "agents/ace/state/bankroll.json").read_text(), self.ace_bank)
        self.assertEqual(json.loads((self.root / "agents/ace/state/shop/bankroll.json").read_text())["bankroll"],
                         9900.0)
        self.calls.clear()
        self.s.run(now=NOW + timedelta(hours=1), fetcher=self.fetcher)
        self.assertEqual(self.calls, [], "a game bet is not priced again")

    def test_five_a_day_best_first_and_no_credit_after(self):
        self.events = []
        for i in range(7):
            self.ctx(str(i), f"Home{i}", f"Away{i}", f"A{i} @ H{i}")
            self.events.append(event(f"o{i}", f"Home{i}", f"Away{i}",
                                     {"pinnacle": (2.31, 1.67, 5), "draftkings": (2.24, 1.68, 5),
                                      "betmgm": (2.25, 1.66, 5), "fanduel": (2.46 + i / 100, 1.62, 5)}))
        self.s.run(now=NOW, fetcher=self.fetcher)
        f = self.s.book().fold(self.s.book().events(self.s.BETS))
        self.assertEqual(len(f["open"]), 5)
        self.assertEqual(sorted(x["event_id"] for x in f["open"]), ["2", "3", "4", "5", "6"], "the best five")
        self.calls.clear()
        self.ctx("9", "Home9", "Away9", "A9 @ H9")
        self.s.run(now=NOW + timedelta(minutes=30), fetcher=self.fetcher)
        self.assertEqual(self.calls, [], "the day's five are placed: no credit spent")

    def test_a_dry_run_bets_nothing(self):
        self.events = [self.a_game(home_price=2.50)]
        out = self.s.run(now=NOW, fetcher=self.fetcher, dry=True)
        self.assertTrue(any(l.startswith("would bet NYK ML") for l in out), out)
        self.assertFalse(self.s.BETS.exists())

    def test_nothing_starting_soon_spends_nothing(self):
        self.ctx("1", "New York Knicks", "Boston Celtics", "BOS @ NYK", start=NOW + timedelta(hours=20))
        self.s.run(now=NOW, fetcher=self.fetcher)
        self.assertEqual(self.calls, [])


class TheKeyAndTheCredits(ShopCase):

    def test_the_second_account_never_aces(self):
        os.environ["ODDS_API_KEY"] = "ace"
        os.environ["ODDS_API_KEY_NFL"] = ""
        self.assertIsNone(self.s.api_key())
        os.environ["ODDS_API_KEY_NFL"] = "second"
        self.assertEqual(self.s.api_key(), "second")

    def test_the_reserve_is_for_closes(self):
        u = {"calls": 0, "remaining": 15}
        self.assertFalse(self.s.may_spend(u))
        self.assertTrue(self.s.may_spend(u, close=True))
        self.assertFalse(self.s.may_spend({"calls": 18, "remaining": 400}), "the day's calls are capped")


class TheCloseAndTheResult(ShopCase):

    def test_graded_by_pinnacles_close_the_worst_way_and_settled_by_espn(self):
        self.events = [self.a_game(home_price=2.50)]
        self.s.run(now=NOW, fetcher=self.fetcher)
        self.calls.clear()
        self.s.close(now=START - timedelta(minutes=60), fetcher=self.fetcher)
        self.assertEqual(self.calls, [], "an hour out: not yet")
        # Pinnacle closes the Knicks at 2.20: the 2.50 taken beat it.
        self.events = [event("o1", "New York Knicks", "Boston Celtics", {"pinnacle": (2.20, 1.74, 2)})]
        self.s.close(now=START - timedelta(minutes=10), fetcher=self.fetcher)
        self.assertEqual(self.calls, [("nba", ("pinnacle",))], "Pinnacle only, one credit")
        self.s.close(now=START - timedelta(minutes=5), fetcher=self.fetcher)
        self.assertEqual(len(self.calls), 1, "one close call a sport every 15 minutes")
        x = self.s.board_json()["open"][0]
        c = self.s.true_chances((2.20, 1.74))
        self.assertEqual(x["clv_pct"], round(min((2.50 * v[0] - 1) * 100 for v in c.values()), 2))
        self.assertGreater(x["clv_pct"], 0)
        (self.root / "agents/ace/state/results.json").write_text(json.dumps(
            {"nba|1": {"status": "STATUS_FINAL", "winner": "home", "home": "NYK", "away": "BOS",
                       "home_score": 110, "away_score": 101}}))
        out = self.s.close(now=START + timedelta(hours=3), fetcher=self.fetcher)
        self.assertTrue(any("NYK ML won +150.00" in l for l in out), out)
        r = self.s.board_json()["record"]
        self.assertEqual((r["won"], r["profit"], r["bankroll"]), (1, 150.0, 10150.0))
        self.assertEqual((self.root / "agents/ace/state/bets.jsonl").read_text(), "", "Ace never sees it")


class WhereItShowsAndWhenItRuns(unittest.TestCase):

    def test_a_tab_on_aces_page(self):
        js = (ROOT / "mission-control-api" / "ace.js").read_text()
        self.assertIn("'ace-shop.py'), 'board'", js)
        self.assertIn("d.strategies.shop = shop", js)
        self.assertIn("shop: 'Line Shopper'", (ROOT / "mission-control-api" / "public" / "ace.html").read_text())

    def test_the_timers(self):
        t = (ROOT / "deploy" / "ace-shop.timer").read_text()
        for at in ("10:30", "13:30", "16:30", "18:45"):
            self.assertIn(f"OnCalendar=*-*-* {at} America/New_York", t)
        self.assertIn("ace-shop.py run", (ROOT / "deploy" / "ace-shop.service").read_text())
        self.assertIn("ace-shop.py close", (ROOT / "deploy" / "ace-shop-close.service").read_text())


if __name__ == "__main__":
    unittest.main()
