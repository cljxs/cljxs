"""Warren's paper book (scripts/warren-book.py).

    python3 -m unittest tests.test_warren_book -b

The Warren checks here are real: built by warren.py from the captured SEC,
Yahoo and Nasdaq fixtures (tests/fixtures/warren-*), with Opus's part
answered by test_warren's ANSWER. The panel is a stand-in that answers by
role. Each test names the mistake it guards against.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import test_warren as tw  # noqa: E402

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
PRICES = {"KMI": 27.5, "TSLA": 350.0, "HIMS": 40.0, "ASTS": 60.0}
GOOD = "The pipelines still earn their fees and the business is unchanged."


def votes(**by_role):
    """A panel that answers by role: {'bull': 'buy', ...}, round 2 the same
    unless given as 'bull2'. A value may be a whole dict to send."""
    asked = []

    def ask(q):
        asked.append(q)
        role = next(r for r, name in (("bull", "Bull"), ("bear", "Bear"), ("risk", "Risk manager"))
                    if f"Your role: {name}." in q)
        r2 = "ROUND 2" in q
        v = by_role.get(role + "2", by_role[role]) if r2 else by_role[role]
        if isinstance(v, dict):
            return True, json.dumps(v)
        line = "buy" if v == "buy" else "none"
        return True, json.dumps({"vote": v, "line": line, "confidence": 4, "why": GOOD})
    ask.asked = asked
    return ask


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tw.load(self.tmp.name)
        os.environ["ECOSYSTEM_ROOT"] = self.tmp.name
        spec = importlib.util.spec_from_file_location("warren_book_t", ROOT / "scripts" / "warren-book.py")
        self.b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.b)
        self.W = self.b.W
        self.W.time.sleep = lambda s: None
        self.W.write_json(self.W.HOLDINGS, {"holdings": [
            {"symbol": "KMI", "amount": 100, "avg_price": 25.0},
            {"symbol": "TSLA", "amount": 200, "avg_price": 400.0},
            {"symbol": "XOM", "amount": 50}]})
        self.built = []

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, sym="KMI", answer=None, when=None):
        c = self.W.build(sym, {"amount": 50, "months": 12, "goal": "both", "why": "", "hold_drop": "yes"}, tw.get,
                         lambda q: (True, json.dumps(answer or tw.ANSWER)),
                         lambda p, g, t: dict(self.W.numbers("KMI", tw.get, t), symbol=p))
        if when:
            c["when_utc"] = when.isoformat(timespec="seconds")
        self.W.save(c)
        return c

    def build(self, sym, inputs):
        self.built.append((sym, inputs))
        c = self.W.build(sym, inputs, tw.get, lambda q: (True, json.dumps(tw.ANSWER)),
                         lambda p, g, t: dict(self.W.numbers("KMI", tw.get, t), symbol=p))
        c["when_utc"] = NOW.isoformat(timespec="seconds")
        return c

    def alert(self, sym="KMI", imp=5, aid="abcd1234", age_h=1, path=None):
        doc = self.W.read_json(path or self.W.ALERTS, {}) or {}
        doc.setdefault("alerts", []).append(
            {"id": aid, "symbol": sym, "signal": "sell", "importance": imp, "why": "It cut its dividend.",
             "title": "Kinder Morgan cuts its dividend", "publisher": "Reuters",
             "found_utc": (NOW - timedelta(hours=age_h)).isoformat(timespec="seconds")})
        self.W.write_json(path or self.W.ALERTS, doc)

    def tick(self, ask, **kw):
        kw.setdefault("news", lambda *a, **k: {"note": "no news fetched in tests"})
        kw.setdefault("new_report", lambda c: False)
        kw.setdefault("price_fn", lambda syms: {s: {"price": PRICES[s]} for s in syms if s in PRICES})
        kw.setdefault("build", self.build)
        return self.b.tick(NOW, ask=ask, **kw)

    def decisions(self):
        return (self.W.read_json(self.b.DECISIONS, {}) or {}).get("decisions") or []


class ItStartsAsYourPortfolio(Base):
    def test_same_shares_same_average_plus_the_cash(self):
        book = self.b.ensure_book(NOW)
        self.assertEqual({p["symbol"]: (p["shares"], p["cost"]) for p in book["positions"]},
                         {"KMI": (4.0, 100.0), "TSLA": (0.5, 200.0)})
        self.assertEqual(book["cash"], 500.0)
        self.assertEqual(book["mode"], "PAPER")
        self.assertEqual(self.b.problems(book), [])

    def test_a_holding_with_no_average_price_is_left_out_and_said(self):
        notes = self.b.ensure_book(NOW)["seed_notes"]
        self.assertEqual(len(notes), 1)
        self.assertIn("XOM", notes[0])

    def test_your_real_trades_afterwards_do_not_change_it(self):
        self.b.ensure_book(NOW)
        self.W.write_json(self.W.HOLDINGS, {"holdings": [{"symbol": "KMI", "amount": 900, "avg_price": 30.0}]})
        book = self.b.ensure_book(NOW)
        self.assertEqual(self.b.held(book, "KMI")["shares"], 4.0)

    def test_doing_nothing_is_the_yardstick(self):
        book = self.b.ensure_book(NOW)
        book["marks"] = dict(PRICES)
        self.assertEqual(self.b.value(book), self.b.baseline(book))
        self.assertEqual(self.b.value(book), 500 + 4 * 27.5 + 0.5 * 350)


class OnlyAUnanimousVoteTrades(Base):
    def test_all_three_buy_and_code_sizes_it(self):
        self.check()
        self.alert()
        st = self.tick(votes(bull="buy", bear="buy", risk="buy"))
        self.assertEqual(st["trades"], 1)
        book = self.b.load_book()
        t = book["trades"][-1]
        # 10% of the $785 book is $78.50, but KMI is already $110 of it - the 20% limit leaves $47.
        self.assertEqual((t["side"], t["notional"], t["price"]), ("buy", 47.0, 27.5))
        self.assertEqual(self.b.problems(book), [])

    def test_two_to_one_is_a_hold(self):
        self.check()
        self.alert()
        self.tick(votes(bull="buy", bear="hold", risk="buy"))
        d = self.decisions()[-1]
        self.assertEqual((d["outcome"], d["trade"]), ("hold", None))
        self.assertIn("split 2 buy, 1 hold", d["note"])
        self.assertEqual(self.b.load_book()["trades"], [])

    def test_a_member_who_changes_his_mind_in_round_two_counts(self):
        self.check()
        self.alert()
        self.tick(votes(bull="buy", bear="hold", bear2="buy", risk="buy"))
        self.assertEqual(self.decisions()[-1]["outcome"], "buy")

    def test_a_reason_with_a_number_of_its_own_counts_as_hold(self):
        self.check()
        self.alert()
        made_up = {"vote": "buy", "line": "buy", "confidence": 5, "why": "It will earn $9 a share next year."}
        self.tick(votes(bull=made_up, bear="buy", risk="buy"))
        d = self.decisions()[-1]
        self.assertTrue(d["round2"]["bull"].get("failed"))
        self.assertEqual(d["outcome"], "hold")

    def test_a_number_from_the_recap_is_allowed(self):
        self.check()
        self.alert()
        fair = {"vote": "buy", "line": "buy", "confidence": 3, "why": "Leverage is 4.1x, in its industry's range."}
        self.tick(votes(bull=fair, bear="buy", risk="buy"))
        self.assertEqual(self.decisions()[-1]["outcome"], "buy")

    def test_naming_a_rule_by_its_number_is_not_a_made_up_figure(self):
        self.check()
        self.alert()
        named = {"vote": "sell", "line": "sell 1", "confidence": 4, "why": "This is exactly sell rule 1 - the dividend."}
        self.tick(votes(bull=named, bear="sell", risk="sell"))
        self.assertEqual(self.decisions()[-1]["outcome"], "sell")

    def test_a_sell_line_that_does_not_exist_counts_as_hold(self):
        self.check()
        self.alert()
        bad = {"vote": "sell", "line": "sell 7", "confidence": 3, "why": GOOD}
        self.tick(votes(bull=bad, bear="sell", risk="sell"))
        self.assertEqual(self.decisions()[-1]["outcome"], "hold")

    def test_round_one_is_alone_round_two_sees_the_others(self):
        self.check()
        self.alert()
        ask = votes(bull="buy", bear="sell", risk="hold")
        self.tick(ask)
        r1, r2 = ask.asked[:3], ask.asked[3:]
        self.assertTrue(all("ROUND 1" in q and "answered round 1" not in q for q in r1))
        bull2 = next(q for q in r2 if "Your role: Bull." in q)
        self.assertIn("Bear: sell", bull2)
        self.assertIn("Risk manager: hold", bull2)
        self.assertNotIn("Bull: buy", bull2)


class WarrensChecklistComesFirst(Base):
    def test_every_question_carries_the_buy_and_sell_lines_and_the_recap(self):
        self.check()
        self.alert()
        ask = votes(bull="hold", bear="hold", risk="hold")
        self.tick(ask)
        q = ask.asked[0]
        self.assertIn("What would make me buy: A lower price with the business unchanged.", q)
        self.assertIn("sell 1. I would sell if It cuts the dividend.", q)
        self.assertIn("free cash flow, 12 months $3.08B", q)
        self.assertIn("Kinder Morgan cuts its dividend", q)
        d = self.decisions()[-1]
        self.assertEqual(d["lines"]["sell"][0], "It cuts the dividend.")

    def test_a_saved_recent_check_is_used(self):
        self.check(when=NOW - timedelta(days=3))
        self.alert()
        self.tick(votes(bull="hold", bear="hold", risk="hold"))
        self.assertEqual(self.built, [])
        self.assertIn("Warren's check of", self.decisions()[-1]["check"]["how"])

    def test_an_old_check_is_run_again(self):
        self.check(when=NOW - timedelta(days=45))
        self.alert()
        self.tick(votes(bull="hold", bear="hold", risk="hold"))
        self.assertEqual([s for s, _ in self.built], ["KMI"])
        self.assertIn("fresh", self.decisions()[-1]["check"]["how"])

    def test_a_new_filing_means_a_fresh_check(self):
        self.check(when=NOW - timedelta(days=3))
        self.alert()
        self.tick(votes(bull="hold", bear="hold", risk="hold"), new_report=lambda c: True)
        self.assertEqual(len(self.built), 1)

    def test_a_stock_never_checked_gets_checked_before_the_panel_meets(self):
        self.b.add_watch("HIMS", "", "owner", NOW)
        self.alert("HIMS")
        self.tick(votes(bull="hold", bear="hold", risk="hold"))
        self.assertEqual([s for s, _ in self.built], ["HIMS"])
        self.assertEqual(self.W.latest("HIMS")["by"], "paper book")

    def test_no_check_to_be_had_means_no_meeting_and_it_is_tried_again(self):
        self.b.add_watch("HIMS", "", "owner", NOW)
        self.alert("HIMS")
        for _ in range(self.b.CHECKS_PER_DAY):
            self.b.count("checks", NOW, bump=True)
        ask = votes(bull="buy", bear="buy", risk="buy")
        st = self.tick(ask)
        self.assertEqual((st["meetings"], ask.asked), (0, []))
        self.assertIn("fresh checks today are used", st["notes"][0])
        self.assertEqual(len(self.b.important_alerts(self.b.load_book(), NOW)), 1)    # still waiting

    def test_warren_saying_skip_blocks_a_buy(self):
        c = self.check()
        c["decision"] = "Skip"
        self.W.save(c)
        self.alert()
        self.tick(votes(bull="buy", bear="buy", risk="buy"))
        d = self.decisions()[-1]
        self.assertEqual((d["outcome"], d["trade"]), ("buy", None))
        self.assertIn("Skip", d["result"])


class CodeSizesEveryTrade(Base):
    def test_a_stock_already_over_twenty_percent_is_kept_but_not_added_to(self):
        book = self.b.ensure_book(NOW)
        book["marks"] = dict(PRICES)
        self.assertGreater(self.b.pct(0.5 * 350, self.b.value(book)), 20)
        spend, why = self.b.size_buy(book, "TSLA")
        self.assertEqual(spend, 0)
        self.assertIn("limit is 20%", why)

    def test_a_new_stock_gets_ten_percent(self):
        book = self.b.ensure_book(NOW)
        book["marks"] = dict(PRICES)
        self.assertEqual(self.b.size_buy(book, "HIMS")[0], round(785 * 0.10, 2))

    def test_never_more_than_the_cash(self):
        book = self.b.ensure_book(NOW)
        book["marks"] = dict(PRICES)
        book["cash"] = 20.0
        self.assertEqual(self.b.size_buy(book, "HIMS")[0], 20.0)

    def test_a_sell_is_half_and_all_of_it_when_warren_says_skip(self):
        self.check()
        self.alert()
        self.tick(votes(bull="sell", bear="sell", risk="sell"))
        self.assertEqual(self.b.held(self.b.load_book(), "KMI")["shares"], 2.0)
        c = self.W.latest("KMI")
        c["decision"] = "Skip"
        self.W.save(c)
        self.alert(aid="beef0001")
        self.tick(votes(bull="sell", bear="sell", risk="sell"))
        book = self.b.load_book()
        self.assertIsNone(self.b.held(book, "KMI"))
        self.assertEqual(self.b.problems(book), [])

    def test_the_gain_on_a_sale_is_from_the_average_cost(self):
        self.check()
        self.alert()
        self.tick(votes(bull="sell", bear="sell", risk="sell"))
        t = self.b.load_book()["trades"][-1]
        self.assertEqual((t["cost"], t["notional"], t["gain"]), (50.0, 55.0, 5.0))

    def test_trades_a_day_are_capped(self):
        self.check()
        for _ in range(self.b.TRADES_PER_DAY):
            self.b.count("trades", NOW, bump=True)
        self.alert()
        self.tick(votes(bull="sell", bear="sell", risk="sell"))
        self.assertIn("trades a day are used", self.decisions()[-1]["result"])

    def test_the_day_is_the_eastern_day(self):
        # 22:17 ET on Nov 23 is already Nov 24 in UTC: still Nov 23's caps.
        late = datetime(2026, 11, 24, 3, 17, tzinfo=timezone.utc)
        self.b.count("trades", late, bump=True)
        self.assertEqual(self.W.read_json(self.b.COUNTS)["day"], "2026-11-23")
        self.assertEqual(self.b.count("trades", datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc)), 1)

    def test_a_ledger_that_does_not_add_up_is_caught(self):
        book = self.b.ensure_book(NOW)
        book["cash"] += 10
        self.assertIn("cash is", self.b.problems(book)[0])


class OnlyImportantNewsCallsThePanel(Base):
    def test_medium_news_does_not(self):
        self.check()
        self.alert(imp=3)
        ask = votes(bull="buy", bear="buy", risk="buy")
        self.assertEqual(self.tick(ask)["meetings"], 0)

    def test_old_news_does_not(self):
        self.check()
        self.alert(age_h=72)
        self.assertEqual(self.tick(votes(bull="hold", bear="hold", risk="hold"))["meetings"], 0)

    def test_a_headline_is_put_to_the_panel_once(self):
        self.check()
        self.alert()
        self.tick(votes(bull="hold", bear="hold", risk="hold"))
        self.assertEqual(self.tick(votes(bull="hold", bear="hold", risk="hold"))["meetings"], 0)

    def test_news_on_a_stock_it_does_not_hold_or_watch_is_ignored(self):
        self.alert("ASTS")
        self.assertEqual(self.tick(votes(bull="buy", bear="buy", risk="buy"))["meetings"], 0)

    def test_its_own_news_watch_covers_only_what_warrens_does_not(self):
        self.b.add_watch("HIMS", "", "owner", NOW)
        got = []
        self.tick(votes(bull="hold", bear="hold", risk="hold"),
                  news=lambda now, **k: got.append(k) or {"note": "ok"})
        self.assertEqual(got[0]["symbols"], ["HIMS"])                  # KMI and TSLA are Warren's already
        self.assertEqual(got[0]["alerts_path"], self.b.BOOK_ALERTS)

    def test_the_books_own_alerts_count_too(self):
        self.b.add_watch("HIMS", "", "owner", NOW)
        self.alert("HIMS", path=self.b.BOOK_ALERTS)
        self.assertEqual(self.tick(votes(bull="hold", bear="hold", risk="hold"))["meetings"], 1)

    def test_warrens_watcher_writes_where_it_is_told(self):
        # the one news watcher, run for the book: its alerts and seen-list in the book's files
        st = self.W.watch_news(NOW, fetch=lambda syms, now: [], ask=lambda q: (True, '{"alerts": []}'),
                               search=lambda *a: [], symbols=[], alerts_path=self.b.BOOK_ALERTS,
                               seen_path=self.b.BOOK_NEWS_SEEN)
        self.assertEqual(st["note"], "no stocks to watch")
        self.assertTrue(self.b.BOOK_NEWS_SEEN.exists())
        self.assertFalse(self.W.NEWS_SEEN.exists())


class ItsOwnSearchForIdeas(Base):
    def tickers(self):
        return json.loads(tw.fx("warren-sec-company-tickers.json"))

    def test_only_real_new_tickers_with_plain_reasons(self):
        text = json.dumps({"ideas": [{"symbol": "HIMS", "why": "Growing telehealth with real cash flow."},
                                     {"symbol": "ZZZZQ", "why": "Made up."},
                                     {"symbol": "KMI", "why": "Pipelines."},
                                     {"symbol": "ASTS", "why": "Revenue up 1500% last year."}]})
        got, problems = self.b.read_ideas(text, self.tickers(), {"KMI"})
        self.assertEqual([x["symbol"] for x in got], ["HIMS"])
        self.assertEqual(len(problems), 3)

    def test_an_idea_is_watched_checked_and_put_to_the_panel(self):
        self.b.ensure_book(NOW)
        pitch = json.dumps({"ideas": [{"symbol": "HIMS", "why": "Growing telehealth with real cash flow."}]})
        panel = votes(bull="buy", bear="buy", risk="buy")
        run = self.b.ideas(NOW, ask=lambda q: (True, pitch), panel=panel, build=self.build,
                           new_report=lambda c: False, get=tw.get,
                           price_fn=lambda syms: {s: {"price": PRICES[s]} for s in syms if s in PRICES})
        self.assertEqual([x["symbol"] for x in self.b.watchlist()], ["HIMS"])
        self.assertEqual([s for s, _ in self.built], ["HIMS"])
        self.assertIn("Opus suggested HIMS", panel.asked[0])
        self.assertTrue(run["ideas"][0]["result"].startswith("bought") or "Skip" in run["ideas"][0]["result"])

    def test_the_search_counts_against_warrens_opus_runs(self):
        for _ in range(self.W.RUNS_PER_DAY):
            self.W.runs_today(bump=True)
        run = self.b.ideas(NOW, ask=lambda q: 1 / 0)
        self.assertIn("Opus runs today are used", run["note"])


class ThePageAndTheBadge(Base):
    def test_the_board_shows_lines_weights_and_the_yardstick(self):
        self.check()
        self.alert()
        self.tick(votes(bull="buy", bear="buy", risk="buy"))
        bd = self.b.board()
        kmi = next(p for p in bd["positions"] if p["symbol"] == "KMI")
        self.assertEqual(kmi["lines"]["buy"], "A lower price with the business unchanged.")
        self.assertEqual(kmi["weight"], 20.0)
        self.assertTrue(next(p for p in bd["positions"] if p["symbol"] == "TSLA")["over_limit"])
        self.assertEqual(bd["value"], bd["baseline"])                  # bought at the mark: no gain or loss yet
        self.assertEqual((bd["unseen"], bd["problems"]), (1, []))
        self.assertEqual(bd["decisions"][0]["symbol"], "KMI")

    def test_a_new_trade_badges_the_village_until_seen(self):
        self.check()
        self.alert()
        self.tick(votes(bull="buy", bear="buy", risk="buy"))
        att = load_attention(self.tmp.name)
        w = att.warren()
        self.assertIn("1 paper-book trade to look at", w["why"])
        self.b.mark_seen()
        self.assertNotIn("1 paper-book trade to look at", att.warren()["why"])

    def test_it_never_places_a_real_order(self):
        src = (ROOT / "scripts" / "warren-book.py").read_text()
        for word in ("alpaca", "robinhood", "schwab", "place_order", "urlopen("):
            self.assertNotIn(word, src.lower())


class TheApi(Base):
    def test_routes(self):
        import shutil
        import subprocess
        api, node = ROOT / "mission-control-api", shutil.which("node")
        if not (node and (api / "node_modules" / "express").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        root = Path(self.tmp.name)
        (root / "scripts").symlink_to(ROOT / "scripts")
        js = (
            f"const express=require({json.dumps(str(api / 'node_modules' / 'express'))});"
            f"const w=require({json.dumps(str(api / 'warren.js'))});"
            "const http=require('http');const app=express();app.use(express.json());w.register(app);"
            "const srv=app.listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
            "const go=(m,p,b)=>new Promise(ok=>{const q=http.request({host:'127.0.0.1',port,path:p,method:m,"
            "headers:{'Content-Type':'application/json'}},r=>{let s='';r.on('data',c=>s+=c);r.on('end',()=>"
            "ok([r.statusCode,s]));});q.end(b?JSON.stringify(b):undefined);});"
            "const out={};"
            "out.before=await go('GET','/api/warren/book');"
            "out.bad=await go('POST','/api/warren/book/watch',{action:'add',symbol:'../etc'});"
            "out.add=await go('POST','/api/warren/book/watch',{action:'add',symbol:'hims',why:'--all \\n of it'});"
            "out.rm=await go('POST','/api/warren/book/watch',{action:'remove',symbol:'HIMS'});"
            "out.seen=await go('POST','/api/warren/book/seen');"
            "process.stdout.write(JSON.stringify(out));srv.close();});")
        r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, ECOSYSTEM_ROOT=str(root)))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(json.loads(out["before"][1])["started"], False, "the page never starts the book")
        self.assertEqual(out["bad"][0], 400, "a ticker never reaches the command line unchecked")
        added = json.loads(out["add"][1])["watchlist"]
        self.assertEqual((added[0]["symbol"], added[0]["why"]), ("HIMS", "--all of it"))
        self.assertEqual(json.loads(out["rm"][1])["watchlist"], [])
        self.assertEqual(out["seen"][0], 200)


def load_attention(tmp):
    os.environ["ECOSYSTEM_ROOT"] = tmp
    spec = importlib.util.spec_from_file_location("attention_wb", ROOT / "scripts" / "attention.py")
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    return a


if __name__ == "__main__":
    unittest.main()
