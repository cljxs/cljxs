"""The Command Deck's tab badges: attention.py says what is waiting on each tab.

    python3 -m unittest tests.test_attention -b

Owner, 2026-10-16: "Can we add some type of notification to these categories
when they need my attention?" Each count comes from the script that owns the
page, so a badge and the page it opens agree. Stan and Bolt run their real
code against a scratch folder; the Shop's two sources are stood in for, since
store-board needs Emily's whole catalogue.
"""

import importlib.util
import json
import os
import tempfile
import types
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def load(name, file, root):
    old = os.environ.get("ECOSYSTEM_ROOT")
    os.environ["ECOSYSTEM_ROOT"] = str(root)
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            os.environ.pop("ECOSYSTEM_ROOT", None)
        else:
            os.environ["ECOSYSTEM_ROOT"] = old


class WhatNeedsTheOwner(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "scripts").symlink_to(SCRIPTS)
        self.env = unittest.mock.patch.dict(os.environ, {"ECOSYSTEM_ROOT": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.att = load("attention_test", "attention.py", self.root)

    def fake(self, kits, cards):
        """tiktok-kit and store-board as the Shop page reads them."""
        real = self.att._load

        def _load(name, file):
            if file == "tiktok-kit.py":
                return types.SimpleNamespace(read_kits=lambda: kits,
                                             et_time=types.SimpleNamespace(day=lambda: "2026-10-16"))
            if file == "store-board.py":
                return types.SimpleNamespace(board=lambda: {"cards": cards})
            return real(name, file)
        self.att._load = _load
        self.addCleanup(setattr, self.att, "_load", real)

    def test_the_shop_counts_kits_to_check_todays_kit_and_listings(self):
        self.fake([{"day": "2026-10-16", "status": "needs_review"},
                   {"day": "2026-10-16", "status": "ready"},
                   {"day": "2026-10-15", "status": "ready"},           # an old kit is not nagged about
                   {"day": "2026-10-16", "status": "posted"}],
                  [{"published": False, "approved_utc": None},
                   {"published": False, "approved_utc": "2026-10-16T10:00:00Z"},   # decided
                   {"published": True, "approved_utc": None}])                     # live
        got = self.att.shop()
        self.assertEqual((got["n"], got["tone"]), (3, "todo"))
        self.assertEqual(got["why"], ["1 TikTok kit with AI scenes to check",
                                      "today's TikTok kit is ready to post", "1 listing to review"])

    def test_stan_counts_problems_waiting_for_a_call(self):
        st = self.root / "agents" / "stan" / "state"
        st.mkdir(parents=True)
        (self.root / "agents" / "stan" / "watchlist.json").write_text(
            (ROOT / "agents" / "stan" / "watchlist.json").read_text())
        (st / "problems.json").write_text(json.dumps({"problems": [
            {"id": "a", "status": "pending"}, {"id": "b", "status": "pending"}, {"id": "c", "status": "approved"}]}))
        got = self.att.stan()
        self.assertEqual((got["n"], got["why"]), (2, ["2 problems to approve or reject"]))

    def book(self, **over):
        b = {"mode": "PAPER", "starting_cash": 10000.0, "cash": 10000.0, "positions": [], "trades": [],
             "marks": {}, "halt": {"active": False, "reference": 10000.0},
             "last_tick_utc": "2026-10-16T17:55:00Z"}
        b.update(over)
        p = self.root / "agents" / "bolt" / "state" / "portfolio.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(b))

    def test_bolt_goes_red_only_when_something_is_wrong(self):
        now = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)
        self.book()
        self.assertEqual(self.att.bolt(now)["n"], 0, "a balanced book with fresh prices is quiet")
        self.book(cash=9000.0)
        got = self.att.bolt(now)
        self.assertEqual((got["n"], got["tone"]), (1, "alert"))
        self.assertIn("does not balance", got["why"][0])
        self.book()
        late = self.att.bolt(now + timedelta(minutes=40))
        self.assertIn("no prices since 17:55 UTC", late["why"][0])

    def test_a_source_that_cannot_be_read_counts_nothing_and_says_so(self):
        real = self.att.SOURCES["stan"]
        self.att.SOURCES["stan"] = lambda: 1 / 0
        self.addCleanup(self.att.SOURCES.__setitem__, "stan", real)
        self.fake([], [])
        out = self.att.attention()
        self.assertEqual(out["stan"]["n"], 0)
        self.assertIn("ZeroDivisionError", out["stan"]["error"])
        self.assertIn("shop", out, "one broken source never hides the others")

    def test_the_deck_draws_the_badges(self):
        page = (ROOT / "mission-control-api" / "public" / "dashboard.html").read_text()
        for tab in ("overview", "ace", "markets", "bolt", "shop", "stan", "agentroom"):
            self.assertIn(f'data-nb="{tab}"', page)
        self.assertIn("fetch('/api/attention'", page)
        self.assertIn("const STALE_TAB = { ace: 'ace', belfort: 'markets', bolt: 'bolt' };", page)
        self.assertIn('id="mkdot"', page, "the unread-review dot stays")
        server = (ROOT / "mission-control-api" / "server.js").read_text()
        self.assertIn("attentionRoutes.register(app);", server)


if __name__ == "__main__":
    unittest.main()
