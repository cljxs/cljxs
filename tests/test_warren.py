"""Warren, the stock analyst (scripts/warren.py).

    python3 -m unittest tests.test_warren -b

Every input here is a real capture (tests/fixtures/warren-*): SEC
companyfacts for Kinder Morgan (December year) and Apple (September year),
Yahoo's chart with dividend events, Nasdaq's summary and analysts' forecast.
Each test names the mistake it guards against.
"""

import importlib.util
import json
import time
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"


def load(tmp):
    os.environ["ECOSYSTEM_ROOT"] = tmp
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("warren_t", ROOT / "scripts" / "warren.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    w.time.sleep = lambda s: None
    return w


def fx(name):
    return (FIX / name).read_text()


def get(url, ua=None, headers=None):
    if "company_tickers" in url:
        return fx("warren-sec-company-tickers.json")
    if "companyfacts/CIK0001506307" in url:
        return fx("warren-sec-companyfacts-kmi.json")
    if "companyfacts/CIK0000320193" in url:
        return fx("warren-sec-companyfacts-aapl.json")
    if "companyfacts/CIK0001773751" in url:
        return fx("warren-sec-companyfacts-hims.json")
    if "companyfacts/CIK0001780312" in url:
        return fx("warren-sec-companyfacts-asts.json")
    for sym in ("HIMS", "ASTS"):
        if sym in url.upper() and "sec.gov" not in url:
            kind = "yahoo-chart" if "/chart/" in url else "nasdaq-summary" if "/summary" in url else "nasdaq-forecast"
            return fx(f"warren-{kind}-{sym.lower()}.json")
    if "/chart/" in url:
        return fx("warren-yahoo-chart-kmi-divs.json")
    if "/summary" in url:
        return fx("warren-nasdaq-summary-kmi.json")
    if "earnings-forecast" in url:
        return fx("warren-nasdaq-forecast-kmi.json")
    raise IOError(f"no fixture for {url}")


ANSWER = {"business": "It owns natural gas pipelines and storage. Customers pay fees to move gas.",
          "customers": "Pipeline space, bought by gas producers, utilities and export terminals.",
          "sector": "Energy", "capital_heavy": True, "cyclical": False,
          "cyclical_why": "Most income is fixed fees.", "peers": ["WMB", "OKE"],
          "commodity_boost": "partly", "commodity_why": "Export volumes rose a little in the war.",
          "risks": ["Gas demand could fall.", "Projects could run late.", "Rates could rise."],
          "sell_rules": ["It cuts the dividend.", "Debt climbs past its target.", "Gas volumes keep falling."],
          "buy_more": "A lower price with the business unchanged.", "debt_note": "Mostly new pipelines."}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.w = load(self.tmp.name)
        self.tickers = json.loads(fx("warren-sec-company-tickers.json"))
        self.kmi = json.loads(fx("warren-sec-companyfacts-kmi.json"))
        self.aapl = json.loads(fx("warren-sec-companyfacts-aapl.json"))

    def tearDown(self):
        self.tmp.cleanup()


class TheSecArithmetic(Base):

    def test_last_twelve_months_is_the_year_plus_this_year_less_last(self):
        # KMI: 2025 5,917 + 9 months 2026 5,041 - 9 months 2025 4,225 = 6,733;
        # Yahoo's own figure that week was 6,733,000,192. A TTM that took the
        # quarter rows (3 months) instead of the year-to-date ones is far off.
        self.assertEqual(self.w.ttm(self.kmi, "ocf"), (6733000000, "2026-09-30"))

    def test_a_september_year_is_still_a_year(self):
        # Apple's year ends late September; picking by calendar frame or by
        # December would drop it or mix quarters in.
        a = self.w.annual(self.aapl, "EarningsPerShareDiluted")
        self.assertEqual(a[-1], ("2026-09-26", 8.98))
        self.assertTrue(all(e[5:7] == "09" for e, _ in a[-3:]))

    def test_interest_follows_the_tag_the_company_uses_now(self):
        # KMI reported InterestExpense until 2014, then a net figure (negative
        # = an expense). The 2014 tag would be twelve years stale.
        s, tag = self.w.series(self.kmi, "interest")
        self.assertEqual(tag, "InterestIncomeExpenseNonoperatingNet")
        self.assertEqual(s[-1], ("2025-12-31", 1801000000))

    def test_debt_counts_the_current_part_once(self):
        # Apple reports LongTermDebt AND its current and noncurrent parts.
        d = self.w.debt_at(self.aapl)
        end = max(d)
        by = {t: dict(self.w.instants(self.aapl, t)) for t in self.w.DEBT_TAGS}
        expect = (by["LongTermDebtNoncurrent"][end] + by["LongTermDebtCurrent"].get(end, 0)
                  + by["CommercialPaper"].get(end, 0))
        self.assertEqual(d[end], expect)
        self.assertLess(d[end], expect + by["LongTermDebt"].get(end, 0))

    def test_outflows_are_positive_amounts(self):
        # Older KMI 10-Qs filed dividends with a minus sign.
        self.assertTrue(all(v > 0 for _, v in self.w.series(self.kmi, "dividends")[0]))


class ThePriceAndTheForecast(Base):

    def test_the_yield_comes_from_dividends_actually_paid(self):
        # Nasdaq has no dividend history for NYSE stocks (a real reply), so the
        # yield is Yahoo's last 365 days of dividend events over the price.
        nas = json.loads(fx("warren-nasdaq-dividends-nyse.json"))
        self.assertIn("Non-Nasdaq", nas["message"])
        p = self.w.price_facts(json.loads(fx("warren-yahoo-chart-kmi-divs.json")))
        self.assertEqual(p["div_per_share_ttm"], 1.19)

    def test_the_per_share_dividend_is_not_overwritten_by_the_sec_total(self):
        # Bug while building: Yahoo's per-share figure and the SEC's dividends
        # series shared a name, and the yield read 0%.
        n = self.w.numbers("KMI", get, self.tickers)
        self.assertEqual(n["yield"], 3.75)
        self.assertEqual(n["dividends_ttm"], 2638000000)

    def test_forward_eps_is_the_next_four_quarters(self):
        f = self.w.forecast_facts(json.loads(fx("warren-nasdaq-forecast-kmi.json")))
        self.assertEqual(f["eps_ntm"], 1.45)
        self.assertLess(f["eps_growth"], 0)

    def test_no_peg_when_earnings_are_not_expected_to_grow(self):
        # A PEG on negative growth is a negative number that reads as "cheap".
        n = self.w.numbers("KMI", get, self.tickers)
        self.assertIsNone(n["peg"])
        self.assertEqual(n["pe_forward"], 21.9)


class CodeChecksOpus(Base):

    def test_a_good_answer_passes(self):
        a, problems = self.w.read_answer("Sure: " + json.dumps(ANSWER), "KMI", self.tickers)
        self.assertEqual(problems, [])
        self.assertEqual(a["peers"], ["WMB", "OKE"])

    def test_a_number_the_code_did_not_compute_is_refused(self):
        bad = dict(ANSWER, business="It earns 19.6% margins on $18 billion of sales.")
        a, problems = self.w.read_answer(json.dumps(bad), "KMI", self.tickers)
        self.assertIsNone(a, "with business blanked the answer is not usable")
        self.assertTrue(any("quotes a number" in p for p in problems))

    def test_rivals_must_be_real_listed_companies(self):
        bad = dict(ANSWER, peers=["KMI", "PIPELINECO"])
        a, problems = self.w.read_answer(json.dumps(bad), "KMI", self.tickers)
        self.assertEqual(a["peers"], [])
        self.assertTrue(any("rivals" in p for p in problems))

    def test_the_sector_is_one_of_the_eleven(self):
        a, problems = self.w.read_answer(json.dumps(dict(ANSWER, sector="Pipelines")), "KMI", self.tickers)
        self.assertIsNone(a)

    def test_the_daily_cap_stops_the_question_before_it_is_asked(self):
        asked = []
        for _ in range(self.w.RUNS_PER_DAY):
            self.w.runs_today(bump=True)
        a, note = self.w.ai_part({"symbol": "KMI"}, {}, self.tickers, ask=lambda q: asked.append(q))
        self.assertIsNone(a)
        self.assertEqual(asked, [])
        self.assertIn("are used", note)


class TheChecklistRules(Base):

    def step(self, rating):
        return {"rating": rating, "rows": []}

    def test_step_3_or_4_red_is_skip(self):
        s = {k: self.step("Green") for k in ("business", "growth", "cash", "debt", "price", "risk")}
        s["debt"] = self.step("Red")
        self.assertEqual(self.w.decide(s)[0], "Skip")

    def test_only_the_portfolio_red_says_how_to_turn_it_into_a_buy(self):
        s = {k: self.step("Green") for k in ("business", "growth", "cash", "debt", "price", "risk")}
        s["risk"] = self.step("Red")
        d, why = self.w.decide(s)
        self.assertEqual(d, "Wait")
        self.assertIn("half your money", why)

    def test_three_yellows_wait_two_buy(self):
        s = {k: self.step("Green") for k in ("business", "growth", "cash", "debt", "price", "risk")}
        s["cash"] = s["price"] = self.step("Yellow")
        self.assertEqual(self.w.decide(s)[0], "Buy")
        s["growth"] = self.step("Yellow")
        self.assertEqual(self.w.decide(s)[0], "Wait")

    def test_a_dividend_paid_on_a_loan_is_red(self):
        n = {"fy": {"ocf": 100, "capex": 50, "dividends": 150, "buybacks": 0, "net_borrowing": 120}}
        self.assertEqual(self.w.debt_purpose(n)[:2], ("dividend on a loan", "Red"))

    def test_concentration_counts_the_new_buy(self):
        hs = [{"symbol": "CVX", "amount": 125, "sector": "Energy"}, {"symbol": "XLK", "amount": 250, "sector": "Technology"}]
        c = self.w.concentration(hs, "KMI", 125, "Energy")
        self.assertEqual((c["sector_pct"], c["stock_pct"], c["total"]), (50.0, 25.0, 500.0))
        self.assertIsNone(self.w.concentration([], "KMI", 125, "Energy"), "nothing to measure against")

    def test_growth_reads_the_last_three_years_only(self):
        # KMI's 2022 revenue (19.2B, a gas-price spike) made 2023-2025 look like
        # a decline; the checklist asks for three years.
        n = self.w.numbers("KMI", get, self.tickers)
        self.assertEqual(self.w.rate_growth(n, None)["rows"][0]["rating"], "Green")


class AWholeCheck(Base):

    def test_kmi_end_to_end_saves_and_reads_back(self):
        peer = lambda p, g, t: dict(self.w.numbers("KMI", get, t), symbol=p)
        c = self.w.build("KMI", {"amount": 125, "months": 12, "goal": "both", "why": "income", "hold_drop": "yes"},
                         get, lambda q: (True, json.dumps(ANSWER)), peer)
        self.assertEqual(set(c["steps"]), {"business", "growth", "cash", "debt", "price", "risk"})
        self.assertIn(c["decision"], ("Buy", "Wait", "Skip"))
        self.assertEqual(c["steps"]["debt"]["quick_test"], "$3.31B")
        self.w.save(c)
        self.assertEqual(self.w.latest("kmi")["symbol"], "KMI")


class ALossMakerIsPricedOnSales(Base):
    """The first live check, HIMS (2026-11-19): losing money, all its debt in
    convertible notes. Warren read the notes as no debt at all, said the debt
    "did not grow" after it borrowed $970M, tried a P/E on a loss, and showed
    Reds with no reason. Real SEC, Yahoo and Nasdaq captures for HIMS."""

    def setUp(self):
        super().setUp()
        self.n = self.w.numbers("HIMS", get)

    def test_convertible_notes_are_debt(self):
        self.assertEqual((self.n["company"], self.n["price"]), ("Hims & Hers Health, Inc.", 16.51), "HIMS's own chart")
        self.assertEqual((self.n["debt"], self.n["debt_as_of"]), (1367393000, "2026-09-30"))
        self.assertEqual(self.n["debt_year_ago"], 971023000)
        self.assertEqual(self.n["fy"]["net_borrowing"], 970000000)

    def test_a_company_with_a_loan_and_a_profit_still_reads_its_loan_first(self):
        # KMI reports LongTermDebt; its components must not be added twice.
        k = self.w.numbers("KMI", get)
        self.assertNotIn("Convertible", json.dumps({t: 1 for t in self.kmi["facts"]["us-gaap"]}))
        self.assertGreater(k["debt"], 0)

    def test_a_loss_is_priced_on_sales_not_earnings(self):
        self.assertTrue(self.n["unprofitable"])
        self.assertIsNone(self.n["pe_forward"])
        self.assertEqual(self.n["ps"], 1.35)          # Nasdaq market cap / revenue, last 12 months
        p = self.w.rate_price(self.n, [{"symbol": "X", "ps": 1.0}], "Health Care", False)
        checks = {r["check"]: r for r in p["rows"]}
        self.assertNotIn("Forward P/E", checks)
        self.assertEqual(checks["Price/Sales (P/S)"]["rating"], "Red", "35% dearer than the rival")
        self.assertIn("not making a profit", checks["Price/Sales (P/S)"]["why"])
        self.assertEqual(checks["PEG"]["rating"], "n/a")
        self.assertEqual(checks["EV/EBITDA"]["rating"], "n/a")
        self.assertIn("P/S", [r[0] for r in p["table"]["rows"]])

    def test_a_profit_is_still_priced_on_earnings(self):
        k = self.w.numbers("KMI", get)
        self.assertFalse(k["unprofitable"])
        rows = [r["check"] for r in self.w.rate_price(k, [], "Energy", False)["rows"]]
        self.assertIn("Forward P/E", rows)
        self.assertNotIn("Price/Sales (P/S)", rows)

    def test_debt_with_negative_earnings_is_red_not_unknown(self):
        self.assertIsNone(self.n["leverage"], "a negative EBITDA makes a negative ratio, not a low one")
        lev = self.w.rate_debt(self.n)["rows"][0]
        self.assertEqual(lev["rating"], "Red")
        self.assertIn("negative", lev["why"])

    def test_every_row_that_is_not_green_says_why(self):
        for sym in ("HIMS", "KMI"):
            n = self.n if sym == "HIMS" else self.w.numbers("KMI", get)
            conc = {"stock_pct": 60, "sector_pct": 70, "total": 1000}
            steps = {"growth": self.w.rate_growth(n, ANSWER), "cash": self.w.rate_cash(n), "debt": self.w.rate_debt(n),
                     "price": self.w.rate_price(n, [], "Energy", False),
                     "risk": self.w.rate_risk({}, conc, {"hold_drop": "no"}, "Energy")}
            for k, st in steps.items():
                for r in st["rows"]:
                    if r["rating"] != "Green":
                        self.assertTrue(r["why"], f"{sym} {k}: {r['check']} is {r['rating']} with no reason")

    def test_the_decision_names_its_reasons(self):
        c = self.w.build("HIMS", {"amount": 150, "months": 8, "goal": "growth", "why": "", "hold_drop": "yes"},
                         get, lambda q: (True, json.dumps(ANSWER)), lambda p, g, t: 1 / 0)
        self.assertEqual(c["decision"], "Skip")
        self.assertIn("Step 4 (debt) - Leverage (net debt / EBITDA): it owes more than it holds in cash",
                      "\n".join(c["decision_reasons"]))
        self.assertNotIn("dividend", c["decision_why"], "HIMS pays no dividend")
        why = c["steps"]["debt"]["rows"][-1]["why"]
        self.assertNotIn("dividend", why)


class YourBreakeven(Base):
    """The owner asked (2026-11-19): add the average price paid, so Warren
    knows the breakeven. The arithmetic is code's, checked by hand here."""

    def test_breakeven_gain_and_the_new_average_after_buying_more(self):
        p = self.w.position({"symbol": "KMI", "amount": 100, "avg_price": 20}, 25, 50)
        self.assertEqual((p["breakeven"], p["shares"], p["value"], p["gain"], p["gain_pct"]), (20, 5, 125, 25, 25.0))
        # 5 shares for $100, then 2 more for $50: 7 shares for $150 -> $21.43 each
        self.assertEqual(p["after"], {"shares": 7, "paid": 150, "breakeven": 21.43})

    def test_a_loss_is_negative_and_buying_lower_brings_breakeven_down(self):
        p = self.w.position({"amount": 100, "avg_price": 20}, 15, 60)
        self.assertEqual((p["gain"], p["gain_pct"]), (-25, -25.0))
        self.assertEqual(p["after"]["breakeven"], 17.78)       # 9 shares for $160

    def test_no_price_paid_means_no_breakeven_not_a_guess(self):
        p = self.w.position({"amount": 100}, 25, 50)
        self.assertEqual(p, {"amount": 100, "avg_price": None, "breakeven": None})
        self.assertIsNone(self.w.position(None, 25))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "warren.py"), "holdings", *args],
                              capture_output=True, text=True, env={**os.environ, "ECOSYSTEM_ROOT": self.tmp.name}, timeout=60)

    def test_updating_the_amount_keeps_the_price_paid(self):
        self.run_cli("add", "KMI", "100", "--sector=Energy", "--avg-price=20")
        r = self.run_cli("add", "KMI", "150")
        h = json.loads(r.stdout)["holdings"][0]
        self.assertEqual((h["amount"], h["avg_price"], h["sector"]), (150, 20, "Energy"))
        self.assertEqual(self.run_cli("add", "KMI", "150", "--avg-price=0").returncode, 1)

    def test_a_check_of_a_stock_you_own_shows_your_position(self):
        self.w.write_json(self.w.HOLDINGS, {"holdings": [{"symbol": "KMI", "amount": 100, "sector": "Energy", "avg_price": 25}]})
        c = self.w.build("KMI", {"amount": 50, "months": 12, "goal": "both", "why": "", "hold_drop": "yes"},
                         get, lambda q: (True, json.dumps(ANSWER)), lambda p, g, t: 1 / 0)
        price = c["numbers"]["price"]
        self.assertEqual(c["position"]["breakeven"], 25)
        self.assertAlmostEqual(c["position"]["gain"], 100 * price / 25 - 100, places=1)
        self.assertIsNone(self.w.build("HIMS", {"amount": 50, "months": 12, "goal": "both", "why": "", "hold_drop": "yes"},
                                       get, lambda q: (True, json.dumps(ANSWER)), lambda p, g, t: 1 / 0)["position"])


class BuyingAndSelling(Base):
    """The owner asked (2026-11-19) to change a holding when he buys or sells,
    and to see the gain on every one, not just the stocks Warren checked."""

    def test_buying_more_moves_the_average_to_everything_paid_over_every_share(self):
        h = self.w.bought({"symbol": "KMI", "amount": 100, "avg_price": 20, "sector": "Energy"}, 60, 15)
        self.assertEqual((h["amount"], h["avg_price"], h["sector"]), (160, 17.7778, "Energy"))   # 9 shares
        self.assertRaises(SystemExit, self.w.bought, {"symbol": "KMI", "amount": 100}, 60, 15)

    def test_selling_keeps_the_average_and_says_what_you_made(self):
        rest, sale = self.w.sold({"symbol": "KMI", "amount": 100, "avg_price": 20}, 2, 25)
        self.assertEqual((rest["amount"], rest["avg_price"]), (60, 20))                     # 3 shares left
        self.assertEqual((sale["proceeds"], sale["gain"], sale["gain_pct"]), (50, 10, 25.0))
        rest, sale = self.w.sold({"symbol": "KMI", "amount": 100, "avg_price": 20}, None, 18)
        self.assertEqual((rest, sale["shares"], sale["gain"]), (None, 5, -10))
        self.assertRaises(SystemExit, self.w.sold, {"symbol": "KMI", "amount": 100, "avg_price": 20}, 6, 25)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "warren.py"), "holdings", *args],
                              capture_output=True, text=True, env={**os.environ, "ECOSYSTEM_ROOT": self.tmp.name}, timeout=60)

    def test_a_sale_is_logged_and_selling_everything_removes_the_stock(self):
        self.run_cli("add", "KMI", "100", "--sector=Energy", "--avg-price=20")
        self.run_cli("sell", "KMI", "--shares=1", "--price=30")
        r = self.run_cli("sell", "KMI", "--all", "--price=30")
        self.assertEqual(json.loads(r.stdout)["holdings"], [])
        self.assertEqual([s["gain"] for s in self.w.read_json(self.w.SALES)], [10, 40])

    def test_prices_are_fetched_together_and_kept_for_fifteen_minutes(self):
        asked = []

        def chart(url, ua=None, headers=None):
            asked.append(url.split("/chart/")[1].split("?")[0])
            if "NOPE" in url:
                raise IOError("no such ticker")
            return fx("warren-yahoo-chart-hims.json")
        got = self.w.prices(["HIMS", "NOPE"], chart, now=1000)
        self.assertEqual((got["HIMS"]["price"], "NOPE" in got), (16.51, False))
        self.w.prices(["HIMS"], chart, now=1000 + 14 * 60)
        self.assertEqual(sorted(asked), ["HIMS", "NOPE"], "fresh for 15 minutes")
        self.w.prices(["HIMS"], chart, now=1000 + 16 * 60)
        self.assertEqual(asked.count("HIMS"), 2)

    def test_every_holding_shows_its_gain_not_only_the_checked_ones(self):
        import io
        from contextlib import redirect_stdout
        self.w.write_json(self.w.HOLDINGS, {"holdings": [{"symbol": "KMI", "amount": 100, "sector": "Energy", "avg_price": 25},
                                                         {"symbol": "NVDA", "amount": 100, "sector": "Technology", "avg_price": 150}]})
        t = time.time()
        self.w.write_json(self.w.PRICES, {"KMI": {"price": 30, "t": t, "as_of_utc": "x"}, "NVDA": {"price": 120, "t": t, "as_of_utc": "x"}})
        out = io.StringIO()
        with redirect_stdout(out):
            self.w.cmd_deck(None)
        pct = {h["symbol"]: h["position"]["gain_pct"] for h in json.loads(out.getvalue())["holdings"]}
        self.assertEqual(pct, {"KMI": 20.0, "NVDA": -20.0})


class AFigureACompanyStoppedReporting(Base):
    """ASTS, the owner's own holding (2026-11-19): it moved revenue to a new
    tag in 2025, stopped tagging operating income after 2024 and EPS after
    2022. Warren read $4M of revenue (2024's), an EBITDA that was its
    depreciation alone (leverage 16.9x for a company losing $700M a year),
    no EPS, a margin of -14,279%, and 'borrowing for buybacks' for a company
    that buys back nothing. Real SEC, Yahoo and Nasdaq captures for ASTS."""

    def setUp(self):
        super().setUp()
        self.n = self.w.numbers("ASTS", get)

    def test_revenue_is_read_from_the_tag_it_uses_now(self):
        self.assertEqual(self.n["revenue_years"][-1], ["2025-12-31", 70918000])
        self.assertEqual(self.n["revenue_ttm"], 142880000)      # 70.9M + 88.6M (2026 to Sept) - 16.6M (2025 to Sept)

    def test_old_operating_income_is_not_this_years(self):
        self.assertEqual(self.n["op_income_from"], "pre-tax income plus interest")
        self.assertLess(self.n["ebitda_ttm"], 0)
        self.assertIsNone(self.n["leverage"])
        self.assertEqual(self.w.rate_debt(self.n)["rows"][0]["rating"], "Red")

    def test_a_stale_figure_is_dropped(self):
        n = {"ocf_ttm_end": "2026-09-30", "net_income_ttm_end": "2026-09-30", "op_income_ttm": 5, "op_income_ttm_end": "2024-12-31",
             "pretax_ttm": -100, "pretax_ttm_end": "2026-09-30", "interest_ttm": 10, "op_income_years": [["2024-12-31", 5]]}
        self.w.stale_figures(n)
        self.assertEqual((n["op_income_ttm"], n["op_income_years"]), (-90, []))
        self.assertIn("op_income", n["stale"])

    def test_eps_is_worked_out_when_the_company_stopped_reporting_it(self):
        self.assertEqual(self.n["eps_from"], "net income over shares")
        sh = dict(self.n["shares_years"])
        self.assertEqual(self.n["eps_years"][-1], ["2025-12-31", round(-341940000 / sh["2025-12-31"], 2)])

    def test_the_words_match_a_company_that_burns_cash(self):
        g = {r["check"]: r for r in self.w.rate_growth(self.n, None)["rows"]}
        self.assertFalse(g["Profit margin"]["answer"].startswith(";"))
        self.assertEqual(self.n["margin_years"][-1], ["2025-12-31", -482.2])       # -$341.9M on $70.9M
        self.assertEqual(g["Revenue, last 3 years"]["rating"], "Green")
        c = {r["check"]: r for r in self.w.rate_cash(self.n)["rows"]}
        self.assertIn("burns about $17.20", c["FCF yield (FCF / market cap)"]["why"])
        what, rating, why = self.w.debt_purpose(self.n)
        self.assertEqual((what, rating), ("borrowing to cover losses and building", "Yellow"), "it buys back no stock")


class EachAbbreviationShowsItsEquation(Base):
    """The owner asked (2026-11-19) for the math beside each abbreviation.
    Each ratio row carries its equation and this stock's numbers in it, and
    the worked answer is the same number the row shows."""

    def build(self, sym):
        return self.w.build(sym, {"amount": 100, "months": 12, "goal": "both", "why": "", "hold_drop": "yes"},
                            get, lambda q: (True, json.dumps(ANSWER)), lambda p, g, t: 1 / 0)

    def test_every_abbreviation_has_its_equation(self):
        for sym in ("KMI", "ASTS"):
            c = self.build(sym)
            for st in c["steps"].values():
                for r in st["rows"]:
                    if any(a in r["check"] for a in ("FCF", "EBITDA", "P/E", "PEG", "P/S", "EPS", "yield", "coverage", "margin")):
                        self.assertTrue(r.get("formula"), f"{sym}: {r['check']} has no equation")
            t = c["steps"]["price"]["table"]
            self.assertEqual(len(t["formulas"]), len(t["rows"]))
            self.assertIn("market cap ÷ revenue", t["formulas"][[r[0] for r in t["rows"]].index("P/S")])

    def test_the_worked_numbers_end_in_the_number_shown(self):
        c = self.build("ASTS")
        price = {r["check"]: r for r in c["steps"]["price"]["rows"]}
        self.assertEqual(price["Price/Sales (P/S)"]["math"], "$10.48B ÷ $143M = 73.33")
        self.assertTrue(price["Price/Sales (P/S)"]["answer"].startswith("73.33"))
        lev = c["steps"]["debt"]["rows"][0]
        self.assertIn("net debt = $4.09B − $2.80B = $1.29B", lev["math"])
        self.assertIn("EBITDA = -$729M + $79M = -$650M", lev["math"])
        k = self.build("KMI")
        cash = {r["check"]: r for r in k["steps"]["cash"]["rows"]}
        y = cash["Dividend yield"]
        self.assertTrue(y["math"].endswith(f"= {y['answer']}"), y)


class TheSitesSayWhoRefused(Base):
    """The first live check (HIMS, 2026-11-19) died on a bare 'HTTP Error
    403: Forbidden' traceback: the SEC refuses a User-Agent with no contact
    email, and the page could not say which site had refused."""

    def setUp(self):
        super().setUp()
        os.environ.pop("WARREN_SEC_UA", None)

    def test_no_contact_set_says_how_to_set_one(self):
        with self.assertRaises(self.w.FetchError) as e:
            self.w.sec_ua()
        self.assertIn("sec-contact", str(e.exception))

    def test_the_contact_goes_in_the_sec_user_agent(self):
        self.w.SEC_CONTACT.parent.mkdir(parents=True)
        self.w.SEC_CONTACT.write_text("owner@example.com\n")
        self.assertEqual(self.w.sec_ua(), "cljxs-ecosystem warren owner@example.com")
        self.w.SEC_CONTACT.write_text("not an address\n")
        self.assertRaises(self.w.FetchError, self.w.sec_ua)

    def test_a_403_names_the_site(self):
        import urllib.error

        def refuse(url, ua=None, headers=None):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        with self.assertRaises(self.w.FetchError) as e:
            self.w.get_json("https://data.sec.gov/api/xbrl/companyfacts/CIK0001506307.json", refuse)
        self.assertEqual(str(e.exception), "the SEC refused the request (HTTP 403)")

    def test_the_command_prints_one_line_not_a_traceback(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "warren.py"), "check", "KMI",
                            "--amount", "100", "--months", "12"], capture_output=True, text=True,
                           env={**os.environ, "ECOSYSTEM_ROOT": self.tmp.name}, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("sec-contact", r.stderr)

    def test_nasdaq_is_asked_as_its_own_page_would_ask(self):
        # The droplet's Nasdaq lines failed with Belfort's plain User-Agent.
        seen = {}

        def record(url, ua=None, headers=None):
            seen[url.split("/")[2]] = headers or {}
            return get(url, ua)
        self.w.get_json(self.w.SUMMARY_URL.format(sym="KMI"), record)
        self.w.get_json(self.w.CHART_URL.format(sym="KMI"), record)
        nas = seen["api.nasdaq.com"]
        self.assertTrue(nas["User-Agent"].startswith("Mozilla/5.0 ("))
        self.assertEqual(nas["Origin"], "https://www.nasdaq.com")
        self.assertNotIn("Accept-Encoding", nas, "urllib would hand back gzip bytes")
        self.assertEqual(seen["query1.finance.yahoo.com"], {}, "Yahoo keeps Belfort's working request")

    def test_belforts_fetcher_sends_the_extra_headers(self):
        bf = self.w._load("bf_headers_t", "belfort-fetch.py")
        sent = {}

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"

        def fake_open(req, timeout=None, context=None):
            sent.update(req.headers)
            return R()
        bf.urllib.request.urlopen, real = fake_open, bf.urllib.request.urlopen
        try:
            bf.http_get("https://api.nasdaq.com/x", headers={"Origin": "https://www.nasdaq.com"})
        finally:
            bf.urllib.request.urlopen = real
        self.assertEqual(sent.get("Origin"), "https://www.nasdaq.com")
        self.assertIn("belfort-fetch", sent.get("User-agent", ""))

    def test_sources_says_which_site_answers(self):
        import io
        from contextlib import redirect_stdout

        def no_nasdaq(url, ua=None, headers=None):
            if "nasdaq" in url:
                raise IOError("blocked")
            return get(url, ua)
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.w.cmd_sources(type("A", (), {"symbol": "kmi"})(), no_nasdaq)
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 0, "Nasdaq is optional")
        self.assertEqual([l.split()[0] for l in lines], ["OK", "OK", "OK", "FAIL", "FAIL"])
        self.assertIn("SEC filings", lines[1])


class TheSecretScanReadsTheFixtures(Base):

    def test_an_xbrl_tag_is_not_a_token_but_a_token_still_is(self):
        # The SEC's tag names run past 40 letters and tripped check-secrets'
        # broad rule; a real token (letters and digits, irregular) must not.
        spec = importlib.util.spec_from_file_location("cs_w", ROOT / "scripts" / "check-secrets.py")
        cs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cs)
        f = Path(self.tmp.name) / "x.json"
        f.write_text('{"NetCashProvidedByUsedInOperatingActivities": 1}\n')
        self.assertEqual(cs.content_findings(f, cs.BROAD_SHAPES), [])
        f.write_text('{"k": "' + "q7Zr" * 11 + '"}\n')
        self.assertEqual(len(cs.content_findings(f, cs.BROAD_SHAPES)), 1)


class ThePageAndTheApi(Base):
    API = ROOT / "mission-control-api"
    NODE = shutil.which("node")

    def test_the_page_is_wired_into_the_deck_and_the_village(self):
        page = (self.API / "public" / "warren.html").read_text()
        self.assertIn("parent.postMessage({ type: 'warren-close' }", page)
        self.assertIn("warrenRoutes.register(app);", (self.API / "server.js").read_text())
        village = (self.API / "public" / "village.html").read_text()
        self.assertIn("warren: '/warren?embed=1'", village)
        self.assertIn("['warren',", village.split("const NAV = [", 1)[1].split("];", 1)[0])
        self.assertIn("'warren-close'", village)
        self.assertIn("if (name === 'warren') return openFrame('warren');", village)

    def test_routes(self):
        if not (self.NODE and (self.API / "node_modules" / "express").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        root = Path(self.tmp.name)
        (root / "scripts").symlink_to(ROOT / "scripts")
        # a fresh price, so the deck does not go to Yahoo from a test
        self.w.write_json(self.w.PRICES, {"KMI": {"price": 30.25, "t": time.time(), "as_of_utc": "2026-11-19T16:00+00:00"}})
        js = (
            f"const express=require({json.dumps(str(self.API / 'node_modules' / 'express'))});"
            f"const w=require({json.dumps(str(self.API / 'warren.js'))});"
            "const http=require('http');const app=express();app.use(express.json());w.register(app);"
            "const srv=app.listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
            "const go=(m,p,b)=>new Promise(ok=>{const q=http.request({host:'127.0.0.1',port,path:p,method:m,"
            "headers:{'Content-Type':'application/json'}},r=>{let s='';r.on('data',c=>s+=c);r.on('end',()=>"
            "ok([r.statusCode,s]));});q.end(b?JSON.stringify(b):undefined);});"
            "const out={};"
            "out.badcheck=await go('POST','/api/warren/check',{symbol:'../etc',amount:5,months:3});"
            "out.noamount=await go('POST','/api/warren/check',{symbol:'KMI',months:3});"
            "out.nosector=await go('POST','/api/warren/holdings',{action:'add',symbol:'KMI',amount:125});"
            "out.add=await go('POST','/api/warren/holdings',{action:'add',symbol:'kmi',amount:125,sector:'Energy',avg_price:'27.5'});"
            "out.badavg=await go('POST','/api/warren/holdings',{action:'add',symbol:'KMI',amount:125,avg_price:-3});"
            "out.deck=await go('GET','/api/warren');"
            "out.buybad=await go('POST','/api/warren/holdings',{action:'buy',symbol:'KMI',amount:55});"
            "out.buy=await go('POST','/api/warren/holdings',{action:'buy',symbol:'KMI',amount:55,price:27.5});"
            "out.sellbad=await go('POST','/api/warren/holdings',{action:'sell',symbol:'KMI',price:30});"
            "out.sell=await go('POST','/api/warren/holdings',{action:'sell',symbol:'KMI',price:30,all:true});"
            "out.rm=await go('POST','/api/warren/holdings',{action:'remove',symbol:'KMI'});"
            "out.show=await go('GET','/api/warren/check/KMI');"
            "process.stdout.write(JSON.stringify(out));srv.close();});")
        r = subprocess.run([self.NODE, "-e", js], capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, ECOSYSTEM_ROOT=str(root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["badcheck"][0], 400, "a ticker never reaches the command line unchecked")
        self.assertEqual(out["noamount"][0], 400)
        self.assertEqual(out["nosector"][0], 409, "a new holding with no check needs its sector")
        self.assertEqual(out["add"][0], 200, out["add"])
        deck = json.loads(out["deck"][1])
        self.assertTrue(deck["ok"], deck)
        self.assertEqual(out["badavg"][0], 400, "a negative price never reaches the command line")
        h = deck["holdings"][0]
        self.assertEqual({k: h[k] for k in ("symbol", "amount", "sector", "avg_price")},
                         {"symbol": "KMI", "amount": 125.0, "sector": "Energy", "avg_price": 27.5})
        self.assertEqual(h["position"]["breakeven"], 27.5)
        self.assertEqual((h["position"]["price"], h["live"]), (30.25, True))
        self.assertEqual(out["buybad"][0], 400, "a buy needs its price")
        self.assertEqual(json.loads(out["buy"][1])["holdings"][0]["amount"], 180)
        self.assertEqual(out["sellbad"][0], 400, "a sale needs its shares, or all")
        sale = json.loads(out["sell"][1])
        self.assertEqual((sale["sale"]["gain"], sale["holdings"]), (16.36, []))   # 6.5455 shares x $2.50
        self.assertEqual(json.loads(out["rm"][1])["holdings"], [])
        self.assertEqual(out["show"][0], 404)


if __name__ == "__main__":
    unittest.main()
