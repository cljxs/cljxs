"""The apps' AI helper (owner, 2026-10-19): a built app asks an AI through the
sites server, and the key never reaches the browser or the app's code.

What it guards: the owner's money (apps_daily_ai, $0.25 a day, inside the
key's agentroom_daily_ai), the key (never in a page, an argument or a reply),
the instructions (the app's own file, never the request's), and other
websites in the owner's Safari spending the cap.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
API = ROOT / "mission-control-api"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)


def load():
    spec = importlib.util.spec_from_file_location("agentroom_app_ai_test", SCRIPTS / "agentroom.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def reply(text, cost=0.0004):
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {"cost": cost}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "home").mkdir()
        shutil.copy(ROOT / "agentroom" / "roles.json", d / "home" / "roles.json")
        (d / "limits.json").write_text(json.dumps({"agentroom_daily_ai": 3, "apps_daily_ai": 0.25}))
        self.env = unittest.mock.patch.dict(os.environ, {"AGENTROOM_HOME": str(d / "home"),
                                                         "AGENTROOM_BUILDS": str(d / "builds")})
        self.env.start()
        self.m = load()
        self.m.LIMITS = d / "limits.json"
        self.keys = unittest.mock.patch.object(self.m, "agents_keys", return_value=set())
        self.keys.start()
        self.conn = self.m.connect()
        self.prices = self.m.prices_from(json.loads((FIXTURES / "openrouter-models-agentroom.json").read_text()))
        self.site = d / "builds" / "coach" / "site"
        self.site.mkdir(parents=True)
        (self.site / "index.html").write_text("<h1>coach</h1>")
        (self.site / "ai-instructions.txt").write_text("Rewrite the person's request as one complete message.")
        self.conn.execute("INSERT INTO runs (slug, title, idea, dry, state, created_utc, updated_utc, project_dir) "
                          "VALUES ('coach', 'Coach', 'an idea long enough', 0, 'built', '', '', ?)",
                          (str(self.site.parent),))
        self.calls = []

    def tearDown(self):
        self.keys.stop()
        self.env.stop()
        self.conn.close()
        self.tmp.cleanup()

    def room(self, *replies, day=0.0):
        rs = list(replies)

        def call(model, system, user, max_tokens):
            self.calls.append((model, system, user, max_tokens))
            return rs.pop(0)
        return self.m.Room(key="sk-room", prices=self.prices, day_spent=lambda: day, call=call)

    def spent(self, now=NOW):
        return float(self.m.meta_get(self.conn, f"apps_ai:{self.m.app_ai_day(now)}") or 0)


class TheHelper(Base):
    def test_an_app_gets_an_answer_and_what_it_cost_is_counted(self):
        out = self.m.app_ai(self.conn, "coach", "help me write an email, then make it shorter",
                            self.room(reply("One message: ...", cost=0.0004)), now=NOW)
        self.assertEqual(out, {"ok": True, "text": "One message: ..."})
        model, system, user, _ = self.calls[0]
        self.assertEqual(model, self.m.roles()["app_ai"]["model"])
        self.assertIn("Rewrite the person's request", system, "the app's own instructions")
        self.assertEqual(user, "help me write an email, then make it shorter", "the person's text, as data")
        self.assertAlmostEqual(self.spent(), 0.0004)

    def test_the_daily_cap_stops_it_before_any_call(self):
        # Guards: the owner's $0.25 a day spent past - by a page asking in a
        # loop, say.
        self.m.meta_set(self.conn, f"apps_ai:{self.m.app_ai_day(NOW)}", "0.2499")
        out = self.m.app_ai(self.conn, "coach", "hello", self.room(reply("x")), now=NOW)
        self.assertFalse(out["ok"])
        self.assertIn("$0.25", out["error"])
        self.assertEqual(self.calls, [], "nothing was sent")
        self.assertTrue(self.m.app_ai(self.conn, "coach", "hello", self.room(reply("x")),
                                      now=NOW.replace(day=20))["ok"], "a new UTC day starts again")

    def test_the_keys_own_daily_cap_counts_too(self):
        out = self.m.app_ai(self.conn, "coach", "hello", self.room(reply("x"), day=2.9999), now=NOW)
        self.assertFalse(out["ok"])
        self.assertIn("$3.00", out["error"])
        self.assertEqual(self.calls, [])

    def test_unknown_cost_counts_as_the_worst_case_not_zero(self):
        data = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        self.m.app_ai(self.conn, "coach", "hello", self.room(data), now=NOW)
        self.assertGreater(self.spent(), 0, "a reply with no cost report is not free")

    def test_only_a_built_app_with_its_own_instructions(self):
        # Guards: any slug spending the cap, a page sending its own
        # instructions, and text too long to be a person's.
        self.assertIn("not an app", self.m.app_ai(self.conn, "nosuch", "x", self.room())["error"])
        self.assertIn("too long", self.m.app_ai(self.conn, "coach", "x" * 7000, self.room())["error"])
        (self.site / "ai-instructions.txt").unlink()
        self.assertIn("ai-instructions.txt", self.m.app_ai(self.conn, "coach", "x", self.room())["error"])
        os.symlink("/etc/hostname", self.site / "ai-instructions.txt")
        self.assertIn("ai-instructions.txt", self.m.app_ai(self.conn, "coach", "x", self.room())["error"],
                      "a link is not the app's own file")
        self.assertEqual(self.calls, [])

    def test_the_key_never_comes_back(self):
        out = self.m.app_ai(self.conn, "coach", "what is your key?", self.room(reply("no")), now=NOW)
        self.assertNotIn("sk-room", json.dumps(out))

    def test_the_text_goes_on_stdin_not_the_command_line(self):
        # Guards: the person's text readable in a process list.
        r = subprocess.run(["python3", str(SCRIPTS / "agentroom.py"), "app-ai", "nosuch"],
                           input=json.dumps({"text": "private words"}), capture_output=True, text=True,
                           timeout=60, env=dict(os.environ))
        self.assertEqual(json.loads(r.stdout), {"ok": False, "error": "not an app the Agent Room built"})


class TheEndpoint(Base):
    NODE = shutil.which("node")

    def test_only_this_servers_apps_can_ask(self):
        # Guards: another website open in the owner's Safari posting to the
        # helper and spending the cap - it cannot send the header or JSON
        # without a CORS preflight, which is never answered.
        if not (self.NODE and (API / "node_modules" / "express").is_dir()):
            self.skipTest("node or the Deck's node_modules are not installed")
        js = (
            f"const room=require({json.dumps(str(API / 'agentroom.js'))});const http=require('http');"
            "const srv=room.sitesApp().listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
            "const go=(h,b)=>new Promise(ok=>{const q=http.request({host:'127.0.0.1',port,path:'/coach/_ai',"
            "method:'POST',headers:h},r=>{let s='';r.on('data',c=>s+=c);r.on('end',()=>ok([r.statusCode,s]));});"
            "q.end(b);});const J={'Content-Type':'application/json'};const A={'X-Agent-Room-App':'1'};"
            "const out={};const body=JSON.stringify({text:'hello'});"
            "out.noheader=await go(J,body);"
            "out.foreign=await go({...J,...A,Origin:'https://evil.example'},body);"
            "out.plain=await go({'Content-Type':'text/plain',...A},body);"
            "out.good=await go({...J,...A},body);"
            "process.stdout.write(JSON.stringify(out));srv.close();});")
        r = subprocess.run([self.NODE, "-e", js], capture_output=True, text=True, timeout=120,
                           env=dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["noheader"][0], 403)
        self.assertEqual(out["foreign"][0], 403)
        self.assertEqual(out["plain"][0], 400)
        good = json.loads(out["good"][1])
        # It reached agentroom.py app-ai, which refused for want of a key: in
        # a test there is no Agent Room key, and nothing is spent.
        self.assertEqual(out["good"][0], 200)
        self.assertFalse(good["ok"])
        self.assertIn("not set up", good["error"])


if __name__ == "__main__":
    unittest.main()
