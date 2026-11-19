"""Warren, the stock analyst (scripts/warren.py).

    python3 -m unittest tests.test_warren -b

Every input here is a real capture (tests/fixtures/warren-*): SEC
companyfacts for Kinder Morgan (December year) and Apple (September year),
Yahoo's chart with dividend events, Nasdaq's summary and analysts' forecast.
Each test names the mistake it guards against.
"""

import importlib.util
import json
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


def get(url, ua=None):
    if "company_tickers" in url:
        return fx("warren-sec-company-tickers.json")
    if "companyfacts/CIK0001506307" in url:
        return fx("warren-sec-companyfacts-kmi.json")
    if "companyfacts/CIK0000320193" in url:
        return fx("warren-sec-companyfacts-aapl.json")
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

        def refuse(url, ua=None):
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

    def test_sources_says_which_site_answers(self):
        import io
        from contextlib import redirect_stdout

        def no_nasdaq(url, ua=None):
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
            "out.add=await go('POST','/api/warren/holdings',{action:'add',symbol:'kmi',amount:125,sector:'Energy'});"
            "out.deck=await go('GET','/api/warren');"
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
        self.assertEqual(deck["holdings"], [{"symbol": "KMI", "amount": 125.0, "sector": "Energy"}])
        self.assertEqual(json.loads(out["rm"][1])["holdings"], [])
        self.assertEqual(out["show"][0], 404)


if __name__ == "__main__":
    unittest.main()
