"""The Agent Room: scripts/agentroom.py (idea -> plan -> reviews -> revised
plan -> the owner's Approve) and the Deck routes in agentroom.js (owner,
2026-10-14).

    python3 -m unittest tests.test_agentroom -b

No test here calls a model or spends anything: real-mode runs are driven
through a stand-in Room whose replies, prices and day count are given. The
API test needs node and the Deck's node_modules and skips without them.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
API = ROOT / "mission-control-api"
FIXTURES = ROOT / "tests" / "fixtures"


def load():
    spec = importlib.util.spec_from_file_location("agentroom_under_test", SCRIPTS / "agentroom.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GOOD_PLAN = {
    "summary": "A dinner planner.",
    "stack": "Python",
    "phases": [
        {"id": "1", "title": "Foundation", "goal": "g", "tasks": ["t"], "done_when": ["pytest passes"]},
        {"id": "2", "title": "Recipes", "goal": "g", "tasks": ["t"], "done_when": ["GET /recipes returns 200"]},
    ],
}
APPROVE = {"verdict": "approve", "summary": "fine", "concerns": []}
CHANGES = {"verdict": "changes", "summary": "no", "concerns": [{"severity": "high", "phase": "1", "text": "x"}]}


def reply(obj, cost=0.01):
    """An OpenRouter chat completion, in the shape this repo's other
    OpenRouter callers read (test_shop.py)."""
    usage = {} if cost is None else {"cost": cost}
    return {"choices": [{"message": {"content": "```json\n" + json.dumps(obj) + "\n```"},
                         "finish_reason": "stop"}], "usage": usage}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "home").mkdir()
        shutil.copy(ROOT / "agentroom" / "roles.json", d / "home" / "roles.json")
        (d / "limits.json").write_text(json.dumps({"agentroom_daily_ai": 3, "agentroom_run_max": 1}))
        self.env = unittest.mock.patch.dict(os.environ, {"AGENTROOM_HOME": str(d / "home"),
                                                         "AGENTROOM_BUILDS": str(d / "builds")})
        self.env.start()
        self.m = load()
        self.m.LIMITS = d / "limits.json"
        # The agents' own key is not on this machine; say so, so key_problem
        # never reads a real credentials file.
        self.agents_key = unittest.mock.patch.object(self.m.mod("preflight"), "openrouter_key",
                                                     return_value="sk-agents")
        self.agents_key.start()
        self.conn = self.m.connect()
        self.prices = self.m.prices_from(json.loads((FIXTURES / "openrouter-models-agentroom.json").read_text()))

    def tearDown(self):
        self.agents_key.stop()
        self.env.stop()
        self.conn.close()
        self.tmp.cleanup()

    def room(self, replies, day=0.0, prices=None):
        calls = []

        def call(model, system, user, max_tokens):
            calls.append(model)
            return replies.pop(0)
        return self.m.Room(key="sk-room", prices=prices or self.prices,
                           day_spent=lambda: day, call=call), calls

    def real_run(self):
        with unittest.mock.patch.object(self.m, "room_key", return_value="sk-room"):
            return self.m.new_run(self.conn, "Dinner", "An app that plans family dinners for a week.", dry=False)


class ReadingReplies(Base):
    def test_a_phase_with_no_check_is_refused(self):
        # Guards: "phase done" meaning "the AI said so". A plan whose phase
        # has no done_when cannot be judged by code, so code refuses it.
        bad = json.loads(json.dumps(GOOD_PLAN))
        bad["phases"][1]["done_when"] = []
        plan, problems = self.m.check_plan(bad)
        self.assertTrue(any("phase 2 has no done_when" in p for p in problems), problems)
        self.assertEqual(self.m.check_plan(GOOD_PLAN)[1], [])

    def test_json_is_found_in_a_fence_or_after_a_sentence(self):
        # Guards: a model that wraps its JSON in ```json fences, or opens with
        # "Here is the plan:", failing the run as "not JSON".
        self.assertEqual(self.m.extract_json("Here:\n```json\n{\"a\": 1}\n```")["a"], 1)
        self.assertEqual(self.m.extract_json("Sure! {\"a\": 2} Hope that helps.")["a"], 2)
        self.assertIsNone(self.m.extract_json("no json at all"))

    def test_an_unreadable_review_is_not_an_approval(self):
        # Guards: skipping the revision because a reviewer's reply could not
        # be read and so raised no concerns.
        self.assertTrue(self.m.needs_revision([self.m.read_review(APPROVE), None]))
        self.assertFalse(self.m.needs_revision([self.m.read_review(APPROVE)]))
        low = {"verdict": "approve", "summary": "", "concerns": [{"severity": "medium", "text": "x"}]}
        self.assertTrue(self.m.needs_revision([self.m.read_review(low)]))

    def test_prices_read_from_a_real_model_list(self):
        # Fixture: OpenRouter's /api/v1/models, fetched 2026-10-14, trimmed to
        # the models roles.json names. Prices arrive as strings per token.
        p_in, p_out = self.prices["moonshotai/kimi-k2.6"]
        self.assertAlmostEqual(p_in * 1e6, 0.65)
        self.assertAlmostEqual(p_out * 1e6, 3.41)
        cfg = self.m.roles()
        for part in [cfg["planner"]] + cfg["reviewers"]:
            self.assertIn(part["model"], self.prices, f"roles.json names {part['model']}, which is not in the fixture")


class Money(Base):
    def test_the_caps_refuse_before_the_call(self):
        part = {"model": "m", "max_tokens": 1000}
        caps = {"agentroom_daily_ai": 3.0, "agentroom_run_max": 1.0}
        ok = self.m.money_check
        self.assertTrue(ok({"spent": 0.5}, part, 0.4, 1.0, caps)[0])
        self.assertFalse(ok({"spent": 0.7}, part, 0.4, 1.0, caps)[0], "run cap")
        self.assertFalse(ok({"spent": 0.0}, part, 0.4, 2.8, caps)[0], "day cap")
        # Guards: a network blip read as "nothing spent today".
        self.assertFalse(ok({"spent": 0.0}, part, 0.4, None, caps)[0])
        # Guards: a model with no published price treated as free.
        self.assertFalse(ok({"spent": 0.0}, part, None, 0.0, caps)[0])

    def test_an_unreported_cost_is_counted_as_the_worst_case(self):
        # Guards: a reply with no cost in it recorded as $0, which would let a
        # run spend without limit.
        rid = self.real_run()
        room, calls = self.room([reply(GOOD_PLAN, cost=None)])
        self.m.step(self.conn, rid, room)
        run = self.m.get_run(self.conn, rid)
        self.assertEqual(len(calls), 1)
        self.assertGreater(run["spent"], 0.0)
        self.assertFalse(run["spent_known"])

    def test_the_agents_key_is_refused(self):
        # Guards: the Agent Room spending out of the agents' $1.50 a day,
        # which budget.py would then report as the agents' day being spent.
        self.assertIn("agents already use", self.m.key_problem("sk-agents"))
        self.assertIsNone(self.m.key_problem("sk-room"))
        with unittest.mock.patch.object(self.m, "room_key", return_value="sk-agents"):
            with self.assertRaises(ValueError):
                self.m.new_run(self.conn, "Dinner", "An app that plans family dinners for a week.", dry=False)

    def test_an_agents_own_key_is_refused_too(self):
        # Guards: the Agent Room given Paul's key (his own $2 a day, owner
        # 2026-10-14) - not the shared key, but still the agents' money.
        root = Path(self.tmp.name) / "eco"
        (root / "agents" / "paul" / "state").mkdir(parents=True)
        (root / "agents" / "paul" / "state" / "credentials.env").write_text("PAUL_OPENROUTER_KEY=sk-paul\n")
        self.m.ROOT = root
        self.assertIn("agents already use", self.m.key_problem("sk-paul"))
        self.assertIsNone(self.m.key_problem("sk-room"))

    def test_the_day_cap_stops_a_run_without_calling(self):
        rid = self.real_run()
        room, calls = self.room([reply(GOOD_PLAN)], day=2.99)
        self.assertEqual(self.m.step(self.conn, rid, room), "failed")
        self.assertEqual(calls, [])
        self.assertIn("today", self.m.get_run(self.conn, rid)["error"])

    def test_a_model_not_on_openrouter_fails_before_calling(self):
        # Guards: a wrong model name in roles.json, which fails silently
        # hours later everywhere else in this repo.
        rid = self.real_run()
        prices = {k: v for k, v in self.prices.items() if k != "moonshotai/kimi-k2.6"}
        room, calls = self.room([reply(GOOD_PLAN)], prices=prices)
        self.assertEqual(self.m.step(self.conn, rid, room), "failed")
        self.assertEqual(calls, [])
        self.assertIn("not a model OpenRouter has", self.m.get_run(self.conn, rid)["error"])

    def test_the_call_cap_stops_a_loop(self):
        rid = self.real_run()
        self.m.set_run(self.conn, rid, calls=self.m.roles()["max_calls_per_run"])
        room, calls = self.room([reply(GOOD_PLAN)])
        self.assertEqual(self.m.step(self.conn, rid, room), "failed")
        self.assertEqual(calls, [])

    def test_no_retry_on_payment_refused_but_retry_on_a_5xx(self):
        # Guards: asking again after a 402 - each try is priced, and asking
        # again cannot add credit.
        tries = []

        def post(req, code):
            tries.append(code)
            raise RuntimeError(f"HTTP {code}: nope")
        with self.assertRaises(RuntimeError):
            self.m.openrouter_call("k", "m", "s", "u", 100, post=lambda r: post(r, 402), wait=lambda s: None)
        self.assertEqual(tries, [402])
        tries.clear()
        with self.assertRaises(RuntimeError):
            self.m.openrouter_call("k", "m", "s", "u", 100, post=lambda r: post(r, 503), wait=lambda s: None)
        self.assertEqual(len(tries), 3)


class ThePriceCeiling(Base):
    def test_every_call_refuses_a_host_above_the_ceiling(self):
        # Guards 2026-10-15: OpenRouter sent DeepSeek to a host charging ~4x
        # the listed price, and a reply budgeted at $0.007 cost $0.018.
        sent = []

        def post(req):
            sent.append(json.loads(req.data))
            return reply(GOOD_PLAN)
        p_in, p_out = self.prices["deepseek/deepseek-v4-pro"]
        self.m.openrouter_call("k", "deepseek/deepseek-v4-pro", "s", "u", 100, post=post,
                               max_price=self.m.ceiling(self.prices, "deepseek/deepseek-v4-pro"))
        prov = sent[0]["provider"]
        self.assertEqual(prov["sort"], "price")
        self.assertAlmostEqual(prov["max_price"]["completion"], p_out * 1e6 * self.m.orc.PRICE_HEADROOM, places=4)
        self.assertAlmostEqual(prov["max_price"]["prompt"], p_in * 1e6 * self.m.orc.PRICE_HEADROOM, places=4)

    def test_the_worst_case_is_worked_out_at_the_ceiling(self):
        p_in, p_out = self.prices["moonshotai/kimi-k2.6"]
        h = self.m.orc.PRICE_HEADROOM
        w = self.m.worst_case(self.prices, "moonshotai/kimi-k2.6", "x" * 300, 1000)
        self.assertAlmostEqual(w, 100 * p_in * h + 1000 * p_out * h)

    def test_a_real_room_call_sends_the_ceiling(self):
        sent = []
        room = self.m.Room(key="sk-room", prices=self.prices, day_spent=lambda: 0.0)
        with unittest.mock.patch.object(self.m, "openrouter_call",
                                        side_effect=lambda *a, **k: sent.append(k) or reply(GOOD_PLAN)):
            room.call("moonshotai/kimi-k2.6", "s", "u", 100)
        self.assertEqual(sent[0]["max_price"], self.m.ceiling(self.prices, "moonshotai/kimi-k2.6"))


class Retry(Base):
    def test_retry_carries_on_without_paying_for_the_plan_again(self):
        # Guards: a reviewer failing and the only way on being a new run,
        # which pays the planner again.
        rid = self.real_run()
        empty = {"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "usage": {"cost": 0.018}}
        room, calls = self.room([reply(GOOD_PLAN), empty])
        self.m.step(self.conn, rid, room)
        self.assertEqual(self.m.step(self.conn, rid, room), "failed")
        run = self.m.get_run(self.conn, rid)
        self.assertIn("whole allowance thinking", run["error"])
        self.assertEqual(run["failed_in"], "reviewing")
        self.assertEqual(self.m.retry(self.conn, rid), "reviewing")
        room, calls = self.room([reply(APPROVE), reply(APPROVE)])
        self.assertEqual(self.m.step(self.conn, rid, room), "awaiting_approval")
        self.assertEqual(calls, ["deepseek/deepseek-v4-pro", "openai/gpt-5.3-codex"], "no planner call")
        with self.assertRaises(ValueError):
            self.m.retry(self.conn, rid)

    def test_a_room_made_before_retry_existed_still_works(self):
        # The droplet's room.db was made without failed_in; connect() adds it.
        conn = self.m.sqlite3.connect(str(Path(self.tmp.name) / "old.db"))
        conn.executescript(self.m.SCHEMA.replace(",\n  failed_in TEXT", ""))
        conn.close()
        new = self.m.connect(Path(self.tmp.name) / "old.db")
        self.addCleanup(new.close)
        cols = {r["name"] for r in new.execute("PRAGMA table_info(runs)")}
        self.assertIn("failed_in", cols)


class TheStateMachine(Base):
    def test_a_real_run_goes_all_the_way_to_the_owner(self):
        rid = self.real_run()
        room, calls = self.room([reply(GOOD_PLAN), reply(CHANGES), reply(APPROVE), reply(GOOD_PLAN)])
        states = [self.m.step(self.conn, rid, room) for _ in range(3)]
        self.assertEqual(states, ["reviewing", "revising", "awaiting_approval"])
        self.assertEqual(calls, ["moonshotai/kimi-k2.6", "deepseek/deepseek-v4-pro",
                                 "openai/gpt-5.3-codex", "moonshotai/kimi-k2.6"])
        run = self.m.get_run(self.conn, rid)
        self.assertAlmostEqual(run["spent"], 0.04)
        self.assertEqual(self.m.latest_plan(self.conn, rid)[0], 2)

    def test_a_restart_does_not_pay_for_a_review_twice(self):
        # Guards: the runner restarting mid-review and asking every reviewer
        # again - the room must resume, not repeat.
        rid = self.real_run()
        room, calls = self.room([reply(GOOD_PLAN)])
        self.m.step(self.conn, rid, room)
        self.conn.execute("INSERT INTO reviews (run_id, round, reviewer, verdict, json, text) VALUES (?, 1, 'DeepSeek', 'approve', ?, 'x')",
                          (rid, json.dumps(self.m.read_review(APPROVE))))
        room, calls = self.room([reply(APPROVE)])
        self.assertEqual(self.m.step(self.conn, rid, room), "awaiting_approval")
        self.assertEqual(calls, ["openai/gpt-5.3-codex"])

    def test_a_bad_plan_is_sent_back_once_then_the_run_stops(self):
        rid = self.real_run()
        bad = {"summary": "s", "phases": [{"id": "1", "title": "t"}]}
        room, calls = self.room([reply(bad), reply(bad)])
        self.assertEqual(self.m.step(self.conn, rid, room), "failed")
        self.assertEqual(len(calls), 2)

    def test_stop_means_no_more_calls(self):
        rid = self.real_run()
        self.m.stop(self.conn, rid)
        room, calls = self.room([reply(GOOD_PLAN)])
        self.assertEqual(self.m.step(self.conn, rid, room), "stopped")
        self.assertEqual(calls, [])

    def test_stop_pressed_during_a_reply_stops_after_it(self):
        # Guards: Stop pressed while the planner is writing, and the run
        # carrying on to pay both reviewers once the reply lands.
        rid = self.real_run()

        def call(model, system, user, max_tokens):
            self.m.stop(self.conn, rid)
            return reply(GOOD_PLAN)
        calls = []
        room = self.m.Room(key="sk-room", prices=self.prices, day_spent=lambda: 0.0,
                           call=lambda *a: calls.append(a[0]) or call(*a))
        self.assertEqual(self.m.step(self.conn, rid, room), "stopped")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.m.next_run(self.conn), None, "a stopped run is not picked up again")

    def test_practice_run_then_send_back_then_approve(self):
        rid = self.m.new_run(self.conn, "Dinner", "An app that plans family dinners for a week.", dry=True)
        while self.m.get_run(self.conn, rid)["state"] in self.m.WORKING:
            self.m.step(self.conn, rid)
        run = self.m.get_run(self.conn, rid)
        self.assertEqual(run["state"], "awaiting_approval")
        self.assertEqual(run["spent"], 0.0)
        self.m.send_back(self.conn, rid, "Add a shopping list.")
        self.assertEqual(self.m.step(self.conn, rid), "awaiting_approval")
        self.assertIsNone(self.m.get_run(self.conn, rid)["note"], "the note is used once")
        d = Path(self.m.approve(self.conn, rid))
        self.assertTrue((d / "PLAN.md").is_file())
        self.assertEqual(json.loads((d / "plan.json").read_text())["phases"][0]["title"], "Foundation")
        if shutil.which("git"):
            log = subprocess.run(["git", "-C", str(d), "log", "--oneline"], capture_output=True, text=True)
            self.assertIn("approved by the owner", log.stdout)
        with self.assertRaises(ValueError):
            self.m.approve(self.conn, rid)

    def test_approve_never_writes_over_a_folder(self):
        rid = self.m.new_run(self.conn, "Dinner", "An app that plans family dinners for a week.", dry=True)
        while self.m.get_run(self.conn, rid)["state"] in self.m.WORKING:
            self.m.step(self.conn, rid)
        d = self.m.builds() / self.m.get_run(self.conn, rid)["slug"]
        d.mkdir(parents=True)
        (d / "mine.txt").write_text("someone's work")
        with self.assertRaises(ValueError):
            self.m.approve(self.conn, rid)
        self.assertEqual((d / "mine.txt").read_text(), "someone's work")


class TheApi(unittest.TestCase):
    NODE = shutil.which("node")

    def test_routes(self):
        if not (self.NODE and (API / "node_modules" / "express").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            shutil.copy(ROOT / "agentroom" / "roles.json", home / "roles.json")
            js = (
                f"const express=require({json.dumps(str(API / 'node_modules' / 'express'))});"
                f"const room=require({json.dumps(str(API / 'agentroom.js'))});"
                "const http=require('http');const app=express();app.use(express.json());room.register(app);"
                "const srv=app.listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
                "const go=(m,p,b)=>new Promise(ok=>{const q=http.request({host:'127.0.0.1',port,path:p,method:m,"
                "headers:{'Content-Type':'application/json'}},r=>{let s='';r.on('data',c=>s+=c);r.on('end',()=>"
                "ok([r.statusCode,s]));});q.end(b?JSON.stringify(b):undefined);});"
                "const out={};"
                "out.dash=await go('POST','/api/agentroom/runs',{title:'Dash',idea:'--this-idea-starts-with-two-dashes',dry:true});"
                "out.deck=await go('GET','/api/agentroom');"
                "out.show=await go('GET','/api/agentroom/runs/1');"
                "out.badid=await go('GET','/api/agentroom/runs/..%2F1');"
                "out.badact=await go('POST','/api/agentroom/runs/1/delete',{});"
                "out.early=await go('POST','/api/agentroom/runs/1/approve',{});"
                "out.stop=await go('POST','/api/agentroom/runs/1/stop',{});"
                "out.retry=await go('POST','/api/agentroom/runs/1/retry',{});"
                "out.bnbad=await go('POST','/api/agentroom/build-now',{hours:9});"
                "out.bnopen=await go('POST','/api/agentroom/build-now',{hours:1});"
                "out.deck2=await go('GET','/api/agentroom');"
                "out.bnoff=await go('POST','/api/agentroom/build-now',{off:true});"
                "out.deck3=await go('GET','/api/agentroom');"
                "process.stdout.write(JSON.stringify(out));srv.close();});")
            r = subprocess.run([self.NODE, "-e", js], capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, AGENTROOM_HOME=str(home),
                                        AGENTROOM_BUILDS=str(Path(d) / "builds")))
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["dash"][0], 200, out["dash"])
            show = json.loads(out["show"][1])
            # Guards: an idea that starts with a dash read as a command-line flag.
            self.assertEqual(show["idea"], "--this-idea-starts-with-two-dashes")
            self.assertTrue(show["run"]["dry"])
            deck = json.loads(out["deck"][1])
            self.assertEqual(deck["counts"]["working"], 1)
            self.assertFalse(deck["runner"]["alive"])
            self.assertEqual(out["badid"][0], 400)
            self.assertEqual(out["badact"][0], 404)
            self.assertEqual(out["early"][0], 409, "approve before there is a plan is refused")
            self.assertEqual(out["stop"][0], 200)
            self.assertEqual(out["retry"][0], 409, "a run that has not failed is not retried")
            # The page's "Build now (1 hour)" button (owner, 2026-10-27).
            self.assertEqual(out["bnbad"][0], 400, "more than 4 hours is refused")
            self.assertEqual(out["bnopen"][0], 200, out["bnopen"])
            w = json.loads(out["deck2"][1])["build_window"]
            self.assertTrue(w["open"] and w["until_local"], w)
            self.assertEqual(out["bnoff"][0], 200)
            self.assertFalse(json.loads(out["deck3"][1])["build_window"]["daytime"], "closed again")


class TheOwnersDevice(unittest.TestCase):
    def test_every_ai_that_shapes_a_build_knows_the_owner_has_only_an_ipad(self):
        # Guards: a plan for something the owner cannot try - a Firefox
        # extension built over four days, then unusable from an iPad
        # (owner, 2026-10-19) - and plans that use the owner's real accounts.
        m = load()
        for name in ("PLANNER_SYSTEM", "REVIEWER_SYSTEM", "BUILDER_RULES"):
            self.assertIn("iPad", getattr(m, name), name)
        self.assertIn("Never a browser extension", m.PLANNER_SYSTEM)
        self.assertIn("real accounts", m.PLANNER_SYSTEM)
        spec = importlib.util.spec_from_file_location("stan_under_test", SCRIPTS / "stan.py")
        stan = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stan)
        self.assertIn("iPad", stan.SYSTEM, "Stan scores what can be built for it")


if __name__ == "__main__":
    unittest.main()
