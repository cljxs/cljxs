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
REPLIES = "Bull, Bear and Risk manager: fair points, weighed above."


NAMES = {"bull": "Bull", "bear": "Bear", "risk": "Risk manager"}


def votes(**by_role):
    """A panel that answers by role: {'bull': 'buy', ...}. Round 2 (the
    cross-examination) asks the next member a plain question unless
    'bullq' gives one; round 3 votes as round 1 unless 'bull2' says
    otherwise, answering the others by name and any question put to it.
    A value may be a whole dict to send."""
    asked, roles = [], []

    def ask(q, role=None):
        asked.append(q)
        roles.append(role)
        role = role or next(r for r, name in NAMES.items() if f"Your role: {name}." in q)
        if "ROUND 2 - CROSS-EXAMINATION. Do not vote now" in q:
            nxt = {"bull": "bear", "bear": "risk", "risk": "bull"}[role]
            v = by_role.get(role + "q", {"to": NAMES[nxt], "question": "What would make you change your vote here?"})
            return True, json.dumps(v)
        final = "ROUND 3 - ANSWERS AND THE FINAL VOTE." in q
        v = by_role.get(role + "2", by_role[role]) if final else by_role[role]
        if not isinstance(v, dict):
            v = {"vote": v, "line": "buy" if v == "buy" else "none", "confidence": 4, "why": GOOD}
        if final:
            v = dict({"answers": REPLIES, "replies": "Nothing in that question changes my view."}, **v)
        return True, json.dumps(v)
    ask.asked, ask.roles = asked, roles
    return ask


def fake_get(url, ua=None, headers=None):
    """The droplet's fetches, answered from real captured pages."""
    for part, name in (("chart/KMI", "warren-yahoo-chart-kmi-divs.json"), ("chart/HIMS", "warren-yahoo-chart-hims.json"),
                       ("chart/ASTS", "warren-yahoo-chart-asts.json"), ("insider-trades", "warren-nasdaq-insider-rklb.json"),
                       ("short-interest", "warren-nasdaq-short-rklb.json"), ("targetprice", "warren-nasdaq-target-rklb.json"),
                       ("earnings-date", "warren-nasdaq-earnings-date-rklb.json"),
                       ("articles/", "warren-article-yahoo-rklb.html")):
        if part in url:
            return tw.fx(name).encode()
    return tw.get(url, ua=ua, headers=headers)


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
        kw.setdefault("get", fake_get)
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
        self.assertTrue(d["final"]["bull"].get("failed"))
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
        r1, r3 = ask.asked[:3], ask.asked[6:]
        self.assertTrue(all("ROUND 1. Answer on your own" in q and "what each member said" not in q for q in r1))
        bull2 = next(q for q in r3 if "Your role: Bull." in q)
        self.assertIn("Bear: sell", bull2)
        self.assertIn("Risk manager: hold", bull2)


class EachSeatHasItsOwnJob(Base):
    """The owner (2026-11-24): "the three ai models are basically just
    repeating the same things". Sharper roles, round 2 must answer the others
    by name, and the Bear sits on a model from another family."""

    def meeting(self, ask, aid="abcd1234"):
        self.check()
        self.alert(aid=aid)
        self.tick(ask)
        return self.decisions()[-1]

    def test_each_role_is_told_its_own_job(self):
        ask = votes(bull="hold", bear="hold", risk="hold")
        self.meeting(ask)
        q = {r: next(x for x in ask.asked if f"Your role: {n}." in x)
             for r, n in (("bull", "Bull"), ("bear", "Bear"), ("risk", "Risk manager"))}
        self.assertIn("the case FOR acting", q["bull"])
        self.assertIn("never buy", q["bear"])
        self.assertIn('"One report is not enough" is not by itself such a reason', q["bear"])
        self.assertIn("You judge only the book", q["risk"])
        self.assertTrue(all("never suggest amounts, steps, timing, limit orders" in x for x in ask.asked))

    def test_round_two_must_answer_the_other_two_by_name(self):
        ask = votes(bull="hold", bear="hold", risk="hold")
        self.meeting(ask)
        bear3 = next(x for x in ask.asked[6:] if "Your role: Bear." in x)
        self.assertIn("Answer the strongest point each of Bull and Risk manager made, by name", bear3)
        self.assertNotIn('"answers"', ask.asked[0])

    def test_a_round_two_that_ignores_someone_counts_as_hold(self):
        lazy = {"vote": "buy", "line": "buy", "confidence": 4, "why": GOOD, "answers": "Bear is right."}
        d = self.meeting(votes(bull="buy", bull2=lazy, bear="buy", risk="buy"))
        self.assertTrue(d["final"]["bull"].get("failed"))           # never answered the Risk manager
        self.assertEqual(d["outcome"], "hold")

    def test_the_replies_are_kept_and_checked_for_numbers(self):
        d = self.meeting(votes(bull="hold", bear="hold", risk="hold"))
        self.assertEqual(d["final"]["bear"]["answers"], REPLIES)
        made_up = {"vote": "hold", "line": "none", "confidence": 3, "why": GOOD,
                   "answers": "Bull: it could earn $77 a share. Risk manager: agreed."}
        d = self.meeting(votes(bull="hold", bear="hold", bear2=made_up, risk="hold"), aid="beef0002")
        self.assertTrue(d["final"]["bear"].get("failed"))

    def test_each_seat_is_asked_as_itself(self):
        ask = votes(bull="hold", bear="hold", risk="hold")
        self.meeting(ask)
        self.assertEqual(ask.roles, ["bull", "bear", "risk"] * 3)


class TheCrossExamination(Base):
    """The owner (2026-11-25): round 2 used to be three polite agreements.
    Now round 2 is one pointed question each, round 3 answers and votes."""

    def meeting(self, ask, aid="abcd1234"):
        self.check()
        self.alert(aid=aid)
        self.tick(ask)
        return self.decisions()[-1]

    def test_round_two_asks_one_question_and_does_not_vote(self):
        ask = votes(bull="buy", bear="sell", risk="hold")
        d = self.meeting(ask)
        q2 = next(x for x in ask.asked[3:6] if "Your role: Bear." in x)
        self.assertIn("Put ONE pointed question to ONE other member - Bull or Risk manager", q2)
        self.assertIn("Bull: buy - " + GOOD, q2)                       # it sees round 1
        self.assertEqual(d["questions"]["bear"], {"to": "risk", "question": "What would make you change your vote here?"})
        self.assertNotIn("vote", d["questions"]["bear"])

    def test_round_three_sees_the_questions_and_must_answer_its_own(self):
        ask = votes(bull="hold", bear="hold", risk="hold",
                    bullq={"to": "Bear", "question": "Which sell line does this news actually hit?"})
        d = self.meeting(ask)
        bear3 = next(x for x in ask.asked[6:] if "Your role: Bear." in x)
        self.assertIn("Bull asked Bear: Which sell line does this news actually hit?", bear3)
        self.assertIn('"replies"', bear3)
        self.assertEqual(d["final"]["bear"]["replies"], "Nothing in that question changes my view.")

    def test_a_question_put_to_you_and_left_unanswered_counts_as_hold(self):
        silent = {"vote": "buy", "line": "buy", "confidence": 4, "why": GOOD, "answers": REPLIES, "replies": ""}
        d = self.meeting(votes(bull="buy", bear="buy", bear2=silent, risk="buy",
                               bullq={"to": "Bear", "question": "Why buy a stock Warren rates this way?"}))
        self.assertTrue(d["final"]["bear"].get("failed"))
        self.assertEqual(d["outcome"], "hold")

    def test_a_question_to_yourself_or_with_a_made_up_figure_is_dropped(self):
        d = self.meeting(votes(bull="hold", bear="hold", risk="hold",
                               bullq={"to": "Bull", "question": "Am I right to be so sure here?"},
                               bearq={"to": "Bull", "question": "Would you still buy if it fell to $613?"}))
        self.assertTrue(d["questions"]["bull"].get("failed"))
        self.assertTrue(d["questions"]["bear"].get("failed"))

    def test_only_round_three_counts(self):
        d = self.meeting(votes(bull="hold", bull2="buy", bear="hold", bear2="buy", risk="hold", risk2="buy"))
        self.assertEqual((d["round1"]["bull"]["vote"], d["outcome"]), ("hold", "buy"))


class EachSeatHasItsOwnEvidence(Base):
    """The owner (2026-11-25): "they ... could use access to some more tools
    for more informed and better debates." Code gathers it, from real
    captured pages here; each seat has its own share."""

    def meeting(self, link=None):
        self.check()
        self.alert()
        if link:
            doc = self.W.read_json(self.W.ALERTS)
            doc["alerts"][-1]["link"] = link
            self.W.write_json(self.W.ALERTS, doc)
        ask = votes(bull="hold", bear="hold", risk="hold")
        self.tick(ask)
        return ask, self.decisions()[-1]

    def q(self, ask, name, stage=0):
        return next(x for x in ask.asked[stage * 3:stage * 3 + 3] if f"Your role: {name}." in x)

    def test_the_article_itself_is_read(self):
        ask, d = self.meeting("https://finance.yahoo.com/markets/stocks/articles/why-rocket-lab-rklb-9-021346011.html")
        q = self.q(ask, "Bull")
        self.assertIn("Rocket Lab secured more than US$515 million in Neutron launch contracts", q)
        self.assertNotIn("broker-dealer", q)                           # the site's own boilerplate is dropped
        self.assertNotIn("not intended to be financial advice", q)

    def test_a_google_news_link_says_why_there_is_no_text(self):
        ask, _ = self.meeting("https://news.google.com/rss/articles/ARTICLE01")
        self.assertIn("a Google News link - the article itself cannot be opened from it", self.q(ask, "Bear"))

    def test_the_bull_gets_the_analysts_and_the_bear_the_insiders_and_short_sellers(self):
        ask, _ = self.meeting()
        bull, bear, risk = self.q(ask, "Bull"), self.q(ask, "Bear"), self.q(ask, "Risk manager")
        self.assertIn("analysts' average price target $92.33 (low $40.00, high $132.00); 15 buy, 3 hold, 0 sell", bull)
        self.assertNotIn("analysts' average price target", bear)
        self.assertIn("insiders, last 3 months: 2 open-market buys, 7 sells, net -467,616 shares", bear)
        self.assertIn("a pre-planned (10b5-1) sale", bear)
        self.assertIn("short interest 45,460,649 shares on 11/13/2026, 2.1 days to cover", bear)
        self.assertNotIn("short interest", bull)
        self.assertIn("risk: ", bear)                                   # Warren's own risk list
        self.assertNotIn("insiders, last 3 months", risk)

    def test_the_risk_manager_gets_how_the_holdings_move_together(self):
        self.W.write_json(self.W.HOLDINGS, {"holdings": [{"symbol": "HIMS", "amount": 100, "avg_price": 25.0},
                                                         {"symbol": "ASTS", "amount": 200, "avg_price": 40.0}]})
        self.check("HIMS")
        self.alert("HIMS")
        ask = votes(bull="hold", bear="hold", risk="hold")
        self.tick(ask)
        risk = self.q(ask, "Risk manager")
        c = self.b.correlation(self.b.closes_of(json.loads(tw.fx("warren-yahoo-chart-hims.json"))),
                               self.b.closes_of(json.loads(tw.fx("warren-yahoo-chart-asts.json"))))
        self.assertEqual(c, 0.42)
        self.assertIn("ASTS: +0.42 - often move together", risk)
        self.assertIn("THE BOOK BY SECTOR", risk)
        self.assertEqual(self.b.correlation([("d%03d" % i, 100 + i) for i in range(50)],
                                            [("d%03d" % i, 100 + i) for i in range(50)]), 1.0)

    def test_the_panels_record_and_the_price_since(self):
        self.check()
        self.alert()
        self.tick(votes(bull="hold", bear="hold", risk="hold"))
        self.alert(aid="beef0003")
        ask = votes(bull="hold", bear="hold", risk="hold")
        self.tick(ask, price_fn=lambda syms: {s: {"price": {"KMI": 30.25}.get(s, PRICES.get(s))} for s in syms})
        self.assertIn("the price was $27.50 then and is $30.25 now (+10.0%)", self.q(ask, "Bull"))

    def test_the_news_history_shows_the_price_reaction(self):
        closes = [("2026-11-18", 100.0), ("2026-11-19", 110.0), ("2026-11-20", 121.0)]
        self.alert(age_h=30)
        doc = self.W.read_json(self.W.ALERTS)
        doc["alerts"][-1]["published"] = "Thu, 19 Nov 2026 14:00:00 GMT"
        self.W.write_json(self.W.ALERTS, doc)
        lines = self.b.news_history("KMI", NOW, closes)
        self.assertIn("the stock moved +10.0% that day and +21.0% since", lines[0])

    def test_every_figure_the_evidence_holds_may_be_quoted(self):
        self.check()
        self.alert()
        cite = {"vote": "hold", "line": "none", "confidence": 3,
                "why": "Short interest is 45,460,649 shares and insiders sold on balance."}
        self.tick(votes(bull="hold", bear=cite, risk="hold"))
        self.assertFalse(self.decisions()[-1]["round1"]["bear"].get("failed"))

    def test_a_missing_piece_is_named_and_the_debate_goes_on(self):
        self.check()
        self.alert()
        st = self.tick(votes(bull="hold", bear="hold", risk="hold"),
                       get=lambda url, ua=None, headers=None: (_ for _ in ()).throw(OSError("offline")))
        d = self.decisions()[-1]
        self.assertEqual(st["meetings"], 1)
        self.assertTrue(any(m.startswith("insider trades (could not reach Nasdaq") for m in d["evidence"]["missing"]))

    def test_the_market_check_is_belforts(self):
        bt = self.b._load("t_belfort_trade", "belfort-trade.py")
        hist = [["2026-10-%02d" % (i % 28 + 1), 100 + i * 0.5] for i in range(40)]
        doc = {"sectors": {"XLE": {"price": 120.0, "sma50": 100.0, "history": hist}}}
        bt.quotes_doc = lambda: doc
        self.b._load = lambda name, file: bt if file == "belfort-trade.py" else None
        lines = self.b.market_mood("Energy")
        self.assertIn("QQQ: unknown", lines[0])
        self.assertIn("its sector, energy (XLE): $120.00, 20.0% above its 50-day average", lines[-1])


class TheBearIsAnotherModel(Base):
    PRICES = None

    def setUp(self):
        super().setUp()
        self.orc = self.b._load("t_or_chat", "or_chat.py")
        self.prices = self.orc.prices_from(json.loads(tw.fx("openrouter-models-agentroom.json")))
        self.b.LIMITS = ROOT / "tasks" / "limits.json"
        self.sonnet = []

    def ask(self, role, reply="{}", left=5.0, prices=None, key="k", cost=0.004, fail=None):
        def call():
            if fail:
                raise RuntimeError(fail)
            return {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}], "usage": {"cost": cost}}
        return self.b.panel_ask("the question", role, NOW, or_call=call, key=key,
                                prices=self.prices if prices is None else prices,
                                budget_read=lambda: {"ai_left_today": left},
                                sonnet=lambda q: self.sonnet.append(q) or (True, "sonnet says"))

    def test_the_bear_goes_to_openrouter_and_its_cost_is_counted(self):
        ok, text, who = self.ask("bear", reply='{"vote": "hold"}')
        self.assertEqual((ok, text, who), (True, '{"vote": "hold"}', "openai/gpt-5.4-mini"))
        self.assertEqual(self.sonnet, [])
        self.assertEqual(self.b.or_spent("2026-11-24"), 0.004)

    def test_the_bull_and_the_risk_manager_stay_on_the_plan(self):
        self.assertEqual(self.ask("bull")[2], "Sonnet")
        self.assertEqual(self.ask("risk")[2], "Sonnet")
        self.assertEqual(len(self.sonnet), 2)

    def test_over_its_own_cap_sonnet_stands_in_and_says_so(self):
        self.W.write_json(self.b.SPEND, {"2026-11-24": 0.249})
        ok, text, who = self.ask("bear")
        self.assertEqual((ok, text), (True, "sonnet says"))
        self.assertIn("Sonnet (standing in: the book's $0.25 a day on OpenRouter is used)", who)

    def test_a_low_shared_day_or_a_missing_model_or_key_stands_in(self):
        self.assertIn("shared OpenRouter day is too low", self.ask("bear", left=0.4)[2])
        self.assertIn("is not a model OpenRouter has", self.ask("bear", prices={})[2])
        self.assertIn("no OpenRouter key", self.ask("bear", key="")[2])
        self.assertIn("OpenRouter: HTTP 402", self.ask("bear", fail="HTTP 402 no credit")[2])
        self.assertEqual(len(self.sonnet), 4)

    def test_an_unknown_cost_counts_as_the_worst_case(self):
        self.ask("bear", cost=None)
        self.assertGreater(self.b.or_spent("2026-11-24"), 0.004)

    def test_the_seat_is_shown_on_each_vote(self):
        self.check()
        self.alert()
        panel = votes(bull="hold", bear="hold", risk="hold")
        ask = lambda q, role=None: tuple(panel(q, role)) + ("openai/gpt-5.4-mini" if role == "bear" else "Sonnet",)
        self.tick(ask)
        d = self.decisions()[-1]
        self.assertEqual(d["round1"]["bear"]["model"], "openai/gpt-5.4-mini")
        self.assertEqual(d["final"]["bull"]["model"], "Sonnet")

    def test_the_limits_file_carries_the_books_caps(self):
        caps = json.loads((ROOT / "tasks" / "limits.json").read_text())
        self.assertEqual((caps["warren_book_daily_ai"], caps["warren_book_leave_for_others"]), (0.25, 0.5))


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

    def test_the_panel_and_the_search_know_shares_can_be_partial(self):
        # The owner (2026-11-24): "They don't have to buy a whole share."
        self.check()
        self.alert()
        ask = votes(bull="hold", bear="hold", risk="hold")
        self.tick(ask)
        self.assertTrue(all("Fractional shares are allowed" in q for q in ask.asked))
        self.assertIn("Fractional shares are allowed", self.b.ideas_question(self.b.load_book()))
        book = self.b.load_book()
        book["marks"]["LLY"], book["cash"] = 1360.0, 100.0
        spend = self.b.size_buy(book, "LLY")[0]
        self.assertEqual(spend, 38.5)                                    # 10% of a $385 book - of a $1,360 share
        t = self.b.buy(book, "LLY", spend, 1360.0, "2026-11-24T15:00:00+00:00", "x", "test")
        self.assertEqual((t["shares"], book["cash"]), (round(38.5 / 1360, 6), 61.5))

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
