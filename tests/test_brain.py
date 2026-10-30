"""The brain map (scripts/brain.py, /brain).

    python3 -m unittest tests.test_brain -b

Owner, 2026-10-30: "I've always wanted my agents to be able to talk to each
other ... a physical way to see it." The map draws what exists and nothing
else, so the tests hold it to the code: a standing wire must be a line the
test can find, every timer must belong to one agent, and traffic is counted
from the queue, the knowledge store and the GM's days exactly as they are
written.
"""

import importlib.util
import json
import os
import re
import sqlite3
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
API = ROOT / "mission-control-api"
NOW = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = unittest.mock.patch.dict(os.environ, {"ECOSYSTEM_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.b = load(f"brain_t{id(self)}", "brain.py")


class TheMapIsTheCode(Base):

    def test_every_wire_is_a_line_in_the_code(self):
        # Guards: a wire drawn for a connection the code no longer has.
        ids = set(self.b.node_ids())
        for a, b, label, f, needle in self.b.WIRES:
            self.assertIn(a, ids)
            self.assertIn(b, ids)
            self.assertIn(needle, (ROOT / f).read_text(), f"{a} -> {b} ({label}): {needle!r} is gone from {f}")

    def test_every_timer_belongs_to_one_agent(self):
        # Guards: a new agent's timer that the map never shows, and a node
        # pointing at a unit that does not exist.
        owner = {}
        for nid, _n, _w, units, _p, _xy in self.b.NODES:
            for u in units:
                self.assertTrue((ROOT / "deploy" / f"{u}.service").is_file(), f"{nid}: no deploy/{u}.service")
                self.assertNotIn(u, owner, f"{u} is drawn twice")
                owner[u] = nid
        for t in (ROOT / "deploy").glob("*.timer"):
            self.assertIn(t.stem, owner, f"deploy/{t.name} belongs to no node in brain.py")

    def test_each_page_is_a_real_route(self):
        server = (API / "server.js").read_text()
        for nid, _n, _w, _u, page, (x, y) in self.b.NODES:
            self.assertTrue(0 <= x <= 100 and 0 <= y <= 100)
            if page:
                self.assertIn(f"app.get('{page}'", server, f"{nid}: {page} is not a page")


class WhatRanAndWhatMoved(Base):

    def test_a_node_lights_from_its_timers(self):
        now = NOW.timestamp()
        facts = {"belfort-cycle.service": ("success", now - 600, "loaded"),
                 "belfort-fetch.service": ("success", now - 3600, "loaded"),
                 "bolt-cycle.service": ("exit-code", now - 60, "loaded"),
                 "ace-cycle.service": ("success", now - 3 * 86400, "loaded"),
                 "paul-cycle.service": ("?", None, "not-found")}
        r = self.b.runs(facts=lambda u: facts.get(u, ("?", None, "not-found")), now=NOW)
        self.assertEqual((r["belfort"]["state"], r["belfort"]["last_run_utc"]), ("fresh", "2026-10-30T14:50:00Z"),
                         "the newest of its timers")
        self.assertEqual((r["bolt"]["state"], r["bolt"]["failed"]), ("failed", ["bolt-cycle"]))
        self.assertEqual(r["ace"]["state"], "quiet")
        self.assertEqual(r["paul"]["state"], "unknown", "no systemd answer is not a run")
        self.assertEqual(r["you"]["state"], "unknown")

    def test_tasks_are_counted_from_the_queue(self):
        db = self.root / "q.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (created_at TEXT, created_by TEXT, assignee TEXT, status TEXT, payload TEXT)")
        rows = [("2026-10-29 10:00:00", "you", "emily", "done"), ("2026-10-28 10:00:00", None, "emily", "failed"),
                ("2026-10-27 10:00:00", "you", "emily", "done"), ("2026-10-01 10:00:00", "you", "emily", "done"),
                ("2026-10-29 11:00:00", "you", "nobody", "done")]
        con.executemany("INSERT INTO tasks VALUES (?,?,?,?,'{}')", rows)
        con.commit()
        con.close()
        self.assertEqual(self.b.task_traffic(db, NOW), [("you", "emily", "tasks handed over", 3, "2 done, 1 failed")],
                         "the last 7 days only, unknown agents left out")
        self.assertEqual(self.b.task_traffic(self.root / "none.db", NOW), [])

    def test_lessons_are_counted_by_who_added_them(self):
        k = load("knowledge_bt", "knowledge.py")
        db = self.root / "k.db"
        con = k.connect(db)
        for src, status, made in (("scout", "proposed", "2026-10-29T10:00:00Z"), ("scout", "accepted", "2026-10-28T10:00:00Z"),
                                  ("gm", "proposed", "2026-10-29T10:00:00Z"), ("scout", "accepted", "2026-09-01T10:00:00Z")):
            con.execute("INSERT INTO knowledge (dept, kind, title, body, confidence, source, status, created_at) "
                        "VALUES ('etsy','lesson','t','b','measured',?,?,?)", (src, status, made))
        con.commit()
        con.close()
        got = self.b.knowledge_traffic(db, NOW)
        self.assertEqual(got, [("gm", "knowledge", "lessons added", 1, "1 proposed"),
                               ("scout", "knowledge", "lessons added", 2, "1 accepted, 1 proposed")])

    def test_the_gms_proposals_and_your_answers(self):
        d = self.root / "gm"
        d.mkdir()
        (d / "2026-10-29.json").write_text(json.dumps({"proposals": [
            {"who": "Scout", "decision": {"verdict": "approve"}},
            {"who": "owner", "decision": None},
            {"who": "emily", "decision": {"verdict": "decline"}}]}))
        (d / "2026-10-01.json").write_text(json.dumps({"proposals": [{"who": "scout"}]}))
        self.assertEqual(self.b.gm_traffic(d, NOW), [("gm", "emily", "proposals", 1, "1 declined"),
                                                     ("gm", "scout", "proposals", 1, "1 approved"),
                                                     ("gm", "you", "proposals", 1, "1 waiting")])

    def test_what_waits_on_you(self):
        att = {"stan": {"n": 2, "tone": "todo", "why": ["2 problems to approve or reject"]},
               "bolt": {"n": 1, "tone": "alert", "why": ["no prices for 40 minutes"]},
               "shop": {"n": 0}, "agentroom": {"error": "x", "n": 0}}
        self.assertEqual(self.b.waiting(att), [("stan", "you", "waiting on you", 2, "2 problems to approve or reject"),
                                               ("bolt", "you", "needs a look", 1, "no prices for 40 minutes")])

    def test_the_whole_map_and_who_works_alone(self):
        b = self.b.brain(now=NOW, facts=lambda u: ("?", None, "not-found"), db=self.root / "none.db",
                         kdb=self.root / "none-k.db", gm_dir=self.root / "none", att={"stan": {"n": 1, "why": ["x"]}})
        self.assertEqual(len(b["nodes"]), len(self.b.NODES))
        self.assertEqual(sum(1 for e in b["edges"] if e["kind"] == "wire"), len(self.b.WIRES))
        self.assertIn("paul", b["alone"])
        self.assertIn("clip", b["alone"])
        self.assertNotIn("stan", b["alone"])
        self.assertNotIn("you", b["alone"])
        self.assertIn("Working alone: Paul, Clip, Peter", self.b.text(b))


class WhereItShows(unittest.TestCase):

    def test_route_api_village_and_deck(self):
        server = (API / "server.js").read_text()
        self.assertIn("const brainRoutes = require('./brain');", server)
        self.assertIn("brainRoutes.register(app);", server)
        self.assertIn("app.get('/brain'", server)
        self.assertIn("app.get('/api/brain'", (API / "brain.js").read_text())
        village = (API / "public" / "village.html").read_text()
        self.assertIn("brain: '/brain?embed=1'", village)
        self.assertIn("['brain', '🧠', 'Brain map']", village)
        self.assertIn("'brain-close'", village)
        self.assertIn('<a class="tabnav" href="/brain">Brain map</a>', (API / "public" / "dashboard.html").read_text())

    def test_the_page_reads_the_api_and_closes_into_the_village(self):
        page = (API / "public" / "brain.html").read_text()
        self.assertIn("fetch('/api/brain'", page)
        self.assertIn("type: 'brain-close'", page)
        self.assertIn('<script src="/shared/busy.js"></script>', page)
        self.assertIn("prefers-reduced-motion", page, "the moving lines stop for someone who asked for less motion")
        # Every name and label from the data goes through esc() before it is drawn.
        for m in re.finditer(r"\$\{(?:n|e|byId\[[^\]]+\])\.(name|label|detail|what)\}", page):
            self.fail(f"unescaped {m.group(0)}")


if __name__ == "__main__":
    unittest.main()
