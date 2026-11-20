"""Warren, the stock analyst (scripts/warren.py).

    python3 -m unittest tests.test_warren -b

Every input here is a real capture (tests/fixtures/warren-*): SEC
companyfacts for Kinder Morgan (December year) and Apple (September year),
Yahoo's chart with dividend events, Nasdaq's summary and analysts' forecast.
Each test names the mistake it guards against.
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
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
    if "/financials" in url:
        return fx(f"warren-nasdaq-financials-quarterly-{url.split('/company/')[1].split('/')[0].lower()}.json")
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


class ASecondSourceChecksTheFigures(Base):
    """The owner asked (2026-11-19) for a checker. Warren's key SEC figures
    are compared with Nasdaq's copy of the statements (a different data
    vendor); each figure links to its filing; and figures that cannot all be
    true raise an alarm. Built so each bug found so far would have shown."""

    def asts(self):
        return self.w.numbers("ASTS", get, verify=True)

    def test_nasdaqs_four_quarters_are_read_in_thousands(self):
        o = self.w.second_source(json.loads(fx("warren-nasdaq-financials-quarterly-asts.json")))
        self.assertEqual(o["period_end"], "2026-09-30")
        self.assertEqual(o["revenue_ttm"], (42320 + 31520 + 14735 + 54305) * 1000)
        self.assertEqual(o["debt"], (4083709 + 8788) * 1000)
        self.assertEqual(o["capex_ttm"], (294981 + 597616 + 261599 + 395696) * 1000)

    def test_fixed_warren_and_nasdaq_agree_on_asts_and_kmi(self):
        for sym in ("ASTS", "KMI"):
            ss = self.w.numbers(sym, get, verify=True)["second_source"]
            self.assertEqual((ss["status"], ss["matched"], ss["compared"]), ("match", 7, 7), sym)

    def test_the_bugs_found_so_far_would_each_have_been_caught(self):
        other = self.w.second_source(json.loads(fx("warren-nasdaq-financials-quarterly-asts.json")))
        n = self.asts()
        old = dict(n, revenue_ttm=4418000)                          # ASTS's revenue, read from the old tag
        self.assertIn("revenue (12 months)", self.w.cross_check(old, other)["disagree"])
        h = self.w.numbers("HIMS", get, verify=True)
        hother = self.w.second_source(json.loads(fx("warren-nasdaq-financials-quarterly-hims.json")))
        self.assertIn("total debt", self.w.cross_check(dict(h, debt=None), hother)["disagree"], "HIMS's notes read as no debt")
        self.assertTrue(any("no debt" in a for a in self.w.alarms(dict(h, debt=None))))
        self.assertTrue(any("-1000" in a.replace(",", "") or "tiny next to the loss" in a
                            for a in self.w.alarms(dict(n, margin_ttm=-14278.8))))

    def test_a_real_difference_is_shown_both_ways_not_hidden(self):
        # HIMS: Warren counts capitalised software as capex; Nasdaq does not.
        ss = self.w.numbers("HIMS", get, verify=True)["second_source"]
        self.assertEqual(ss["disagree"], ["capex (12 months)"])
        c = self.w.build("HIMS", {"amount": 100, "months": 12, "goal": "growth", "why": "", "hold_drop": "yes"},
                         get, lambda q: (True, json.dumps(ANSWER)), lambda p, g, t: 1 / 0)
        fcf = c["steps"]["cash"]["rows"][0]
        self.assertEqual(fcf["checked"]["status"], "disagree")
        self.assertIn("capex", c["caution"][0])

    def test_a_quarter_nasdaq_has_not_caught_up_with_is_not_checked(self):
        other = self.w.second_source(json.loads(fx("warren-nasdaq-financials-quarterly-asts.json")))
        ss = self.w.cross_check(dict(self.asts(), ocf_ttm_end="2026-12-31"), dict(other))
        self.assertEqual(ss["status"], "not checked")
        self.assertEqual(self.w.cross_check(self.asts(), {"error": "403"})["status"], "not checked")

    def test_each_figure_links_to_its_filing(self):
        c = self.w.build("ASTS", {"amount": 100, "months": 12, "goal": "growth", "why": "", "hold_drop": "yes"},
                         get, lambda q: (True, json.dumps(ANSWER)), lambda p, g, t: 1 / 0)
        rev = c["steps"]["growth"]["rows"][0]
        self.assertEqual([l["label"] for l in rev["sources"]], ["10-Q for 2026-09-30", "10-K for 2025-12-31", "10-Q for 2025-09-30"])
        self.assertTrue(all(l["url"].startswith("https://www.sec.gov/Archives/edgar/data/1780312/") for l in rev["sources"]))
        self.assertEqual(rev["checked"]["status"], "match")

    def test_a_period_links_to_the_report_that_first_gave_it(self):
        # A 10-Q repeats last December's balance sheet beside its own; the
        # December figure's filing is still the 10-K.
        facts = {"facts": {"us-gaap": {"X": {"units": {"USD": [
            {"end": "2025-12-31", "val": 1, "accn": "K", "form": "10-K", "filed": "2026-02-20"},
            {"end": "2025-12-31", "val": 1, "accn": "Q", "form": "10-Q", "filed": "2026-05-05"}]}},
            "Y": {"units": {"USD": [{"end": "2025-12-31", "val": 2, "accn": "Q", "form": "10-Q", "filed": "2026-05-05"}]}}}}}
        self.assertEqual(self.w.filings(facts)["2025-12-31"]["accn"], "K")

    def test_peers_are_not_checked_twice(self):
        self.assertNotIn("second_source", self.w.numbers("KMI", get))


class NewsOnWhatYouOwn(Base):
    """The owner asked (2026-11-19): when impactful news, good or bad, hits a
    stock he owns, tell him whether it is a reason to consider selling or
    buying more - using what Belfort already collects. The headlines below
    are made up for the test (Belfort's captured feed is redacted); the
    feed's parsing is tested on the real capture in test_parsers."""

    NOW = datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc)

    def head(self, sym, hid, title, days=0):
        when = (self.NOW - timedelta(days=days)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        return {"symbol": sym, "id": hid, "title": title, "publisher": "example.invalid",
                "published": when, "link": f"https://example.invalid/{hid}"}

    def setUp(self):
        super().setUp()
        self.w.write_json(self.w.HOLDINGS, {"holdings": [
            {"symbol": "KMI", "amount": 100, "sector": "Energy", "avg_price": 25},
            {"symbol": "HIMS", "amount": 150, "sector": "Health Care", "avg_price": 21.8}]})
        self.w.save({"symbol": "HIMS", "company": "Hims & Hers Health, Inc.", "when_utc": "2026-11-19T15:00:00+00:00",
                     "decision": "Skip", "steps": {"risk": {"rating": "Green", "rows": [], "buy_more": "if it turns a profit"}},
                     "ai": {"sell_rules": ["I would sell if a regulator stops its compounded drugs.",
                                           "I would sell if subscribers start to fall.",
                                           "I would sell if it borrows heavily again."]}})
        self.fetched = []

    def belfort(self, heads, hours_old=1, symbols=("KMI", "OKE")):
        p = self.w.BELFORT_NEWS
        p.parent.mkdir(parents=True, exist_ok=True)
        asof = (self.NOW - timedelta(hours=hours_old)).strftime("%Y-%m-%d %H:%M:%S")
        p.write_text(json.dumps({"asof_utc": asof, "symbols": list(symbols), "headlines": heads}))

    def fetch(self, heads):
        def f(syms):
            self.fetched.append(list(syms))
            return [h for h in heads if h["symbol"] in syms]
        return f

    def test_belforts_headlines_are_used_and_only_the_rest_fetched(self):
        self.belfort([self.head("KMI", "aaaa0001", "Kinder Morgan wins pipeline contract"),
                      self.head("OKE", "aaaa0002", "ONEOK news")])
        heads, where = self.w.holding_headlines(["KMI", "HIMS"], self.NOW,
                                                self.fetch([self.head("HIMS", "bbbb0001", "FDA letter to Hims & Hers")]))
        self.assertEqual(self.fetched, [["HIMS"]])
        self.assertEqual(sorted(h["id"] for h in heads), ["aaaa0001", "bbbb0001"], "OKE is not owned")
        self.assertEqual(where, {"from_belfort": ["KMI"], "fetched": ["HIMS"]})

    def test_a_stale_belfort_file_is_not_trusted_and_old_news_is_not_news(self):
        self.belfort([self.head("KMI", "aaaa0001", "old Belfort copy")], hours_old=5)
        heads, _ = self.w.holding_headlines(["KMI"], self.NOW, self.fetch([
            self.head("KMI", "cccc0001", "fresh"), self.head("KMI", "cccc0002", "last week", days=7)]))
        self.assertEqual(self.fetched, [["KMI"]])
        self.assertEqual([h["id"] for h in heads], ["cccc0001"])

    def test_the_question_carries_each_stocks_own_rules(self):
        q = self.w.news_question([{"symbol": "HIMS", "company": "Hims & Hers Health, Inc.",
                                   "sell_rules": ["I would sell if a regulator stops its compounded drugs."],
                                   "buy_more": "if it turns a profit",
                                   "headlines": [self.head("HIMS", "bbbb0001", "FDA letter to Hims & Hers")]}])
        self.assertIn("1. I would sell if a regulator stops its compounded drugs.", q)
        self.assertIn("Buy more if: if it turns a profit", q)
        self.assertIn("[bbbb0001] FDA letter to Hims & Hers", q)

    def test_only_answers_code_can_stand_behind_become_alerts(self):
        by = {h["id"]: h for h in (self.head("HIMS", "bbbb0001", "FDA warns Hims & Hers over 2 drugs"),
                                     self.head("KMI", "aaaa0001", "Kinder Morgan wins pipeline contract"))}
        rules = {"HIMS": ["r1", "r2", "r3"], "KMI": []}
        ans = json.dumps({"alerts": [
            {"id": "bbbb0001", "symbol": "HIMS", "signal": "sell", "rule": 1, "why": "A regulator warned it about 2 of its drugs."},
            {"id": "aaaa0001", "symbol": "KMI", "signal": "buy", "rule": 0, "why": "A big new contract."},
            {"id": "zzzz9999", "symbol": "KMI", "signal": "buy", "rule": 0, "why": "made up headline"},
            {"id": "aaaa0001", "symbol": "HIMS", "signal": "sell", "rule": 0, "why": "wrong stock"},
            {"id": "bbbb0001", "symbol": "HIMS", "signal": "hold", "rule": 0, "why": "not a signal"},
            {"id": "bbbb0001", "symbol": "HIMS", "signal": "sell", "rule": 7, "why": "no such rule"},
            {"id": "aaaa0001", "symbol": "KMI", "signal": "buy", "rule": 0, "why": "It is worth 40% more now."}]})
        got = self.w.read_news_answer("```json\n" + ans + "\n```", by, rules)
        self.assertEqual([(x["id"], x["signal"], x["rule"]) for x in got],
                         [("bbbb0001", "sell", "r1"), ("aaaa0001", "buy", None)])
        self.assertIsNone(self.w.read_news_answer("I could not decide", by, rules))

    def run_watch(self, heads, answer=None, ok=True, searched=()):
        asked = []

        def ask(q):
            asked.append(q)
            return ok, json.dumps(answer or {"alerts": []})
        st = self.w.watch_news(self.NOW, self.fetch(heads), ask,
                               search=lambda sym, company, rt: [h for h in searched if h["symbol"] == sym])
        return st, asked

    def test_a_run_asks_once_saves_alerts_and_does_not_repeat_them(self):
        heads = [self.head("HIMS", "bbbb0001", "FDA warns Hims & Hers"), self.head("KMI", "aaaa0001", "Kinder Morgan quarterly dividend")]
        one = {"id": "bbbb0001", "symbol": "HIMS", "signal": "sell", "rule": 1, "why": "A regulator warning touches rule one."}
        ans = {"alerts": [one, dict(one)]}                       # the same alert twice in one answer
        st, asked = self.run_watch(heads, ans)
        self.assertEqual((st["new"], st["alerts"], len(asked)), (2, 1, 1))
        b = self.w.news_board()
        self.assertEqual((b["unseen"], b["sell"], b["buy"]), (1, 1, 0))
        self.assertEqual(b["alerts"][0]["rule"], "I would sell if a regulator stops its compounded drugs.")
        st, asked = self.run_watch(heads, ans)
        self.assertEqual((st["new"], len(asked)), (0, 0), "nothing new: no question")
        self.assertEqual(self.w.news_board()["unseen"], 1)

    def test_an_unanswered_question_is_asked_again_next_run(self):
        heads = [self.head("HIMS", "bbbb0001", "FDA warns Hims & Hers")]
        st, _ = self.run_watch(heads, ok=False)
        self.assertIn("asked again", st["note"])
        st, asked = self.run_watch(heads)
        self.assertEqual((st["new"], len(asked)), (1, 1))

    def test_questions_stop_at_the_days_cap(self):
        for i in range(self.w.NEWS_ASKS_PER_DAY):
            self.run_watch([self.head("KMI", f"dddd{i:04d}", f"news {i}")])
        st, asked = self.run_watch([self.head("KMI", "eeee0001", "one more")])
        self.assertEqual(len(asked), 0)
        self.assertIn("already today", st["note"])

    def test_dismissing_and_the_village_badge(self):
        heads = [self.head("HIMS", "bbbb0001", "FDA warns Hims & Hers"), self.head("KMI", "aaaa0001", "Kinder Morgan contract")]
        self.run_watch(heads, {"alerts": [
            {"id": "bbbb0001", "symbol": "HIMS", "signal": "sell", "rule": 1, "why": "Regulator."},
            {"id": "aaaa0001", "symbol": "KMI", "signal": "buy", "rule": 0, "why": "Contract."}]})
        spec = importlib.util.spec_from_file_location("att_w", ROOT / "scripts" / "attention.py")
        att = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(att)
        att._load = lambda name, file: self.w
        a = att.warren()
        self.assertEqual((a["n"], a["tone"]), (2, "alert"))
        self.w.cmd_alerts(type("A", (), {"id": "bbbb0001", "symbol": "HIMS"})())
        a = att.warren()
        self.assertEqual((a["n"], a["tone"]), (1, "todo"))


class WhatEachNumberMeans(Base):
    """The owner asked (2026-11-20): the equations show how a number is made,
    but what does it mean - is it good or bad, for this stock? Each ratio
    gets a plain sentence built from the stock's own numbers, and the scale
    it is judged on; each comparison line says which way is better and where
    this stock sits."""

    def build(self, sym):
        return self.w.build(sym, {"amount": 100, "months": 12, "goal": "both", "why": "", "hold_drop": "yes"},
                            get, lambda q: (True, json.dumps(ANSWER)), lambda p, g, t: 1 / 0)

    def test_every_ratio_row_with_a_value_says_what_it_means(self):
        c = self.build("KMI")
        n = c["numbers"]
        rows = {r["check"]: r for st in c["steps"].values() for r in st["rows"]}
        self.assertEqual(rows["Dividend yield"]["means"],
                         f"Every $100 you invest pays about ${n['yield']:.2f} a year in dividends.")
        self.assertIn("Healthy: 2-5%", rows["Dividend yield"]["scale"])
        self.assertTrue(rows["Forward P/E"]["means"].startswith(f"You pay ${n['pe_forward']:g} for every $1 of profit"))
        self.assertIn("Normal for Energy: 9-14", rows["Forward P/E"]["scale"])
        for r in rows.values():
            if r.get("formula") and r["answer"] not in ("n/a", "not reported", "doesn't apply"):
                self.assertTrue(r.get("means"), f"{r['check']} has an equation but no meaning")

    def test_more_cash_than_debt_is_explained_as_the_good_kind(self):
        n = {"net_debt": -4.03e9, "ebitda_ttm": 9.64e8, "leverage": -4.2}
        says = self.w.plain_words("leverage", n)["says"]
        self.assertTrue(says.startswith("It has $4.03B more cash than debt"))
        self.assertIn("negative leverage number is the good kind", says)

    def test_a_low_pe_is_compared_with_rivals_and_warned_about(self):
        n = {"pe_forward": 9.4}
        says = self.w.plain_words("pe_forward", n, [{"pe_forward": 13.0}, {"pe_forward": 24.1}], "Health Care")["says"]
        self.assertIn("Its rivals average 18.6, so it is 49% cheaper than them.", says)   # OSCR, 2026-11-20
        self.assertIn("A very low P/E can be a bargain", says)

    def test_the_table_says_which_way_is_better_and_where_it_sits(self):
        t = {"columns": ["OSCR", "CNC", "MOH"],
             "rows": [["Forward P/E", "9.4", "13.0", "24.1"], ["PEG", "", "0.96", "0.41"],
                      ["Leverage", "-4.2x", "-2.3x", "-2.1x"], ["Profit margin", "4.9%", "0.7%", "-0.1%"],
                      ["1-year price change", "+94.6%", "+86.2%", "+50.7%"]]}
        notes = self.w.table_notes(t)
        self.assertEqual(notes[0], "Lower is cheaper. OSCR is the cheapest of the 3.")
        self.assertEqual(notes[1], "Lower is cheaper for its growth.", "OSCR has no PEG: no place to give")
        self.assertEqual(notes[2], "Lower is safer; below zero means more cash than debt. OSCR is the safest of the 3.")
        self.assertEqual(notes[3], "Higher keeps more of each sale. OSCR is the most profitable of the 3.")
        self.assertTrue(notes[4].startswith("How far the price moved - not good or bad by itself"))
        t["rows"][0] = ["Forward P/E", "30", "13.0", "24.1"]
        self.assertEqual(self.w.table_notes(t)[0], "Lower is cheaper. OSCR is the most expensive of the 3.")


class YourNotesOnAStock(Base):
    """The owner asked (2026-11-20) for a notes section of his own on each
    stock: dated notes he can add, edit and delete, kept as he wrote them."""

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "warren.py"), "notes", *args],
                              capture_output=True, text=True, env={**os.environ, "ECOSYSTEM_ROOT": self.tmp.name}, timeout=60)

    def test_add_edit_and_delete_keep_his_words(self):
        text = "Bought for the peptides market.\nRe-check after Q4 - and if it drops below $14, think again."
        r = self.run_cli("add", "hims", f"--text={text}")
        note = json.loads(r.stdout)["notes"][0]
        self.assertEqual((note["text"], len(note["id"])), (text, 8))
        self.run_cli("add", "HIMS", "--text=second")
        self.run_cli("edit", "HIMS", note["id"], "--text=first, changed")
        got = self.w.notes_of("HIMS")
        self.assertEqual([n["text"] for n in got], ["first, changed", "second"])
        self.assertIn("edited_utc", got[0])
        self.run_cli("remove", "HIMS", note["id"])
        self.run_cli("remove", "HIMS", got[1]["id"])
        self.assertEqual(self.w.notes_of(), {}, "a stock with no notes left is not kept")

    def test_what_is_refused(self):
        self.assertEqual(self.run_cli("add", "HIMS", "--text=   ").returncode, 1)
        self.assertEqual(self.run_cli("add", "HIMS", "--text=" + "x" * 4001).returncode, 1)
        self.assertEqual(self.run_cli("edit", "HIMS", "deadbeef", "--text=x").returncode, 1)
        self.assertEqual(self.run_cli("add", "../etc", "--text=x").returncode, 1)


class EachAlertSaysHowMuchItMatters(Base):
    """The owner asked (2026-11-20) to rate how important each piece of news
    is: 1 Minor to 5 Major, most important shown first."""

    def head(self, hid, title="AST SpaceMobile files to offer 10M shares"):
        return {"symbol": "ASTS", "id": hid, "title": title, "publisher": "x", "published": "Thu, 19 Nov 2026 15:00:00 +0000"}

    def test_the_rating_is_read_and_a_bad_one_is_medium(self):
        by = {h["id"]: h for h in (self.head("a0000001"), self.head("a0000002"), self.head("a0000003"))}
        ans = json.dumps({"alerts": [
            {"id": "a0000001", "symbol": "ASTS", "signal": "sell", "rule": 0, "importance": 5, "why": "A share sale."},
            {"id": "a0000002", "symbol": "ASTS", "signal": "watch", "rule": 0, "importance": 9, "why": "Off the scale."},
            {"id": "a0000003", "symbol": "ASTS", "signal": "watch", "rule": 0, "why": "No rating given."}]})
        got = self.w.read_news_answer(ans, by, {"ASTS": []})
        self.assertEqual([x["importance"] for x in got], [5, 3, 3])

    def test_the_question_explains_the_scale(self):
        q = self.w.news_question([{"symbol": "ASTS", "company": "AST", "sell_rules": [], "buy_more": "",
                                   "headlines": [self.head("a0000001")]}])
        self.assertIn("5 Major", q)
        self.assertIn('"importance": 1 to 5', q)
        self.assertIn("same story, answer only the clearest one", q)

    def test_most_important_first_and_the_badge_turns_red(self):
        self.w.write_json(self.w.ALERTS, {"alerts": [
            dict(self.head("a0000001"), signal="watch", why="w", importance=2, seen=False),
            dict(self.head("a0000002"), signal="watch", why="w", importance=5, seen=False),
            dict(self.head("a0000003"), signal="watch", why="w", seen=False),           # from before the rating
            dict(self.head("a0000004"), signal="watch", why="w", importance=4, seen=True)]})
        b = self.w.news_board()
        self.assertEqual([x["id"] for x in b["alerts"]], ["a0000002", "a0000003", "a0000001", "a0000004"])
        self.assertEqual(b["important"], 1, "a dismissed one does not count")
        spec = importlib.util.spec_from_file_location("att_imp", ROOT / "scripts" / "attention.py")
        att = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(att)
        att._load = lambda name, file: self.w
        a = att.warren()
        self.assertEqual(a["tone"], "alert", "a Major headline is an alert even with no sell signal")
        self.assertTrue(a["why"][0].startswith("1 headline rated High or Major"))


class TheOwnersOwnWordsAreSearchedFor(Base):
    """The owner asked (2026-11-19): if the news talks about what his sell
    rules or buy-more test watch for, he wants to know - as part of the whole
    search, not the only part. ASTS's real rules, and a real Google News feed
    for ASTS captured that day."""

    ASTS = {"symbol": "ASTS", "company": "AST SpaceMobile, Inc.", "when_utc": "2026-11-19T18:00:00+00:00", "decision": "Skip",
            "ai": {"sell_rules": [
                "I would sell if launches of new BlueBird satellites keep slipping and the company fails to reach the coverage needed for continuous commercial service in the US.",
                "I would sell if a major carrier partner such as AT&T or Verizon cuts back or ends its agreement, or if a rival like SpaceX's Starlink direct-to-cell service wins the carriers over.",
                "I would sell if the company loses or fails to secure the spectrum rights it needs, or if it has to raise money on terms that badly dilute shareholders before paying revenue starts flowing."]},
            "steps": {"risk": {"rating": "Green", "rows": [], "buy_more":
                "Buying more would make sense if the company starts continuous commercial service with its carrier partners, keeps launching on schedule, and revenue starts growing steadily from paying subscribers rather than one-off contracts."}}}

    def setUp(self):
        super().setUp()
        self.rt = self.w.rule_terms(self.ASTS)
        self.feed = fx("warren-google-news-asts.xml")

    def test_each_rule_gives_its_own_words(self):
        self.assertEqual(self.rt["sell"][0][:3], ["BlueBird", "launches", "satellites"])
        self.assertTrue({"AT&T", "Verizon", "SpaceX", "Starlink"} <= set(self.rt["sell"][1]))
        self.assertTrue({"spectrum", "offering", "dilution"} <= set(self.rt["sell"][2]), "dilute -> the words headlines use")
        own = self.w.watch_terms("if Hims & Hers loses its fight with the FDA", "Hims & Hers Health, Inc.")
        self.assertIn("FDA", own)
        self.assertFalse({"Hims", "Hers"} & set(own), "the company's own name would match every headline")
        self.assertIn("subscribers", self.rt["buy"])

    def test_the_search_is_the_name_and_the_rules_words(self):
        q = self.w.search_query(self.ASTS["company"], self.rt)
        self.assertTrue(q.startswith('"AST SpaceMobile" (BlueBird OR "AT&T" OR Verizon OR SpaceX OR Starlink'), q)
        self.assertTrue(q.endswith(") when:7d"))
        self.assertEqual(self.w.short_name("Hims & Hers Health, Inc."), "Hims & Hers")

    def test_a_real_feed_is_read_with_its_publishers(self):
        heads = self.w.search_headlines("ASTS", self.ASTS["company"], self.rt, get=lambda url: self.feed)
        self.assertEqual(len(heads), self.w.SEARCH_PER_NAME)
        offer = next(h for h in heads if "offer 10M shares" in h["title"])
        self.assertEqual((offer["title"], offer["publisher"]),
                         ("AST Spacemobile files to offer 10M shares as part of Firefly agreement", "TradingView"))
        carriers = next(h for h in heads if h["title"].startswith("When will AT&T"))
        self.assertIn("AT&T and Verizon", carriers["title"], "the feed's &amp; is read as &")

    def test_headlines_are_matched_to_the_rule_they_talk_about(self):
        hits = lambda t: [(k, n) for k, n, _ in self.w.rule_hits(t, self.rt)]
        self.assertIn(("sell", 3), hits("AST Spacemobile files to offer 10M shares as part of Firefly agreement"))
        self.assertIn(("sell", 2), hits("When will AT&T and Verizon phones use AST SpaceMobile satellites?"))
        self.assertIn(("sell", 1), hits("AST SpaceMobile Plans Its Own Rocket to Speed Satellite Launches and Cut Costs"))
        self.assertEqual(hits("AST SpaceMobile (ASTS) Takes a Beating. Here's What Happened"), [])
        verizon = self.w.rule_hits("AT&T and Verizon phones", self.rt)[0]
        self.assertTrue(self.w.strong(verizon))
        self.assertFalse(self.w.strong(("sell", 1, ["launches"])), "one plain word is the model's to judge")
        self.assertTrue(self.w.strong(("sell", 3, ["offering"])), "a word headlines use for 'dilute'")

    def test_both_halves_of_the_search_are_judged_together(self):
        self.w.write_json(self.w.HOLDINGS, {"holdings": [{"symbol": "ASTS", "amount": 1039, "sector": "Communication Services",
                                                          "avg_price": 68.57}]})
        self.w.save(dict(self.ASTS))
        now = datetime(2026, 11, 19, 20, 0, tzinfo=timezone.utc)
        yahoo = [{"symbol": "ASTS", "id": "y0000001", "title": "AST SpaceMobile shares rise", "publisher": "example.invalid",
                  "published": "Thu, 19 Nov 2026 15:00:00 +0000"}]
        asked = []

        def ask(q):
            asked.append(q)
            return True, json.dumps({"alerts": []})          # the model leaves everything out
        st = self.w.watch_news(now, lambda syms: [h for h in yahoo if h["symbol"] in syms], ask,
                               search=lambda sym, company, rt: self.w.search_headlines(sym, company, rt, get=lambda u: self.feed))
        q = asked[0]
        self.assertIn("[y0000001] AST SpaceMobile shares rise", q, "Yahoo's half is still there")
        self.assertIn("<- mentions sell rule 2: Starlink", q)
        self.assertIn("<- mentions sell rule 3: offering", q)
        self.assertNotIn("When will AT&T", q, "6 days old: older than NEWS_MAX_AGE_DAYS")
        self.assertEqual(st["searched"], ["ASTS"])
        b = self.w.news_board()
        self.assertEqual(sorted(x["title"][:40] for x in b["alerts"]),       # the two strong ones within 4 days
                         ["AST SpaceMobile: “We’re not like Musk or", "AST Spacemobile files to offer 10M share"])
        offer = next(x for x in b["alerts"] if "offer 10M shares" in x["title"])
        self.assertEqual((offer["signal"], offer["by"]), ("watch", "words"), "the model left it out; the words did not")
        self.assertEqual(offer["why"], "It mentions offering - words from your sell rule 3.")
        self.assertEqual(offer["importance"], self.w.WORDS_IMPORTANCE, "not judged by the model: Low")
        self.assertTrue(offer["rule"].startswith("I would sell if the company loses or fails to secure the spectrum"))

    def test_the_page_shows_the_words(self):
        self.w.save(dict(self.ASTS))
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            self.w.cmd_show(type("A", (), {"symbol": "ASTS"})())
        self.assertEqual(json.loads(out.getvalue())["watch_terms"], self.rt)


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
            "out.badalert=await go('POST','/api/warren/alerts',{id:'../x'});"
            "out.note=await go('POST','/api/warren/notes',{action:'add',symbol:'kmi',text:'--all \\n line two'});"
            "out.badnote=await go('POST','/api/warren/notes',{action:'edit',symbol:'KMI',id:'x;rm',text:'y'});"
            "out.emptynote=await go('POST','/api/warren/notes',{action:'add',symbol:'KMI',text:'  '});"
            "out.deck2=await go('GET','/api/warren');"
            "out.allseen=await go('POST','/api/warren/alerts',{id:'all'});"
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
        self.assertEqual(out["badalert"][0], 400, "an alert id never reaches the command line unchecked")
        self.assertEqual(json.loads(out["allseen"][1]), {"unseen": 0})
        self.assertEqual(deck["news"]["unseen"], 0)
        self.assertEqual(json.loads(out["note"][1])["notes"][0]["text"], "--all \n line two",
                         "a note that looks like an option is still just text")
        self.assertEqual((out["badnote"][0], out["emptynote"][0]), (400, 400))
        self.assertEqual(json.loads(out["deck2"][1])["notes"]["KMI"][0]["text"], "--all \n line two")
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
