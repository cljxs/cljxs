"""model-trial.py: each model's real runs, read from the journal OpenClaw
writes, so a model trial is judged on what the runs did and cost.

    python3 -m unittest tests.test_models -b
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load():
    spec = importlib.util.spec_from_file_location("model_trial_test", ROOT / "scripts" / "model-trial.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RunsFromTheRealJournal(unittest.TestCase):
    """The captured journal of Paul's dry cycle on xiaomi/mimo-v2.6-pro
    (2026-10-05). OpenClaw 2026.9 nests model, usage and turns two levels
    down; read flat, the first look at it said "model None" and no cost."""

    @classmethod
    def setUpClass(cls):
        cls.mt = load()
        cls.raw = (FIXTURES / "openclaw-agent-json-2026.9.log").read_text()

    def test_the_run_is_read_with_its_billed_cost(self):
        rows = self.mt.runs(self.raw)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["model"], r["result"], r["turns"]), ("xiaomi/mimo-v2.6-pro", "ok", 28))
        self.assertAlmostEqual(r["cost"], 0.0857, places=4)
        self.assertEqual(r["minutes"], 6.1)
        self.assertEqual((r["calls"], r["failures"]), (47, 4))

    def test_the_cached_share_is_read_from_the_runs_own_usage(self):
        # 1,914,752 of 155,712 + 1,914,752 tokens read came from the cache.
        # This is how a model trial tells whether caching works: Haiku's
        # price assumes it does, and without it a run costs about 4x.
        self.assertEqual(self.mt.runs(self.raw)[0]["cached"], 0.925)
        nocache = self.raw.replace('"cacheRead": 1914752', '"cacheRead": 0')
        self.assertEqual(self.mt.runs(nocache)[0]["cached"], 0.0)
        self.assertEqual(self.mt.by_model(self.mt.runs(self.raw))["xiaomi/mimo-v2.6-pro"]["cached"], 0.925)

    def test_models_are_compared_per_run(self):
        a = dict(self.mt.runs(self.raw)[0])
        b = dict(a, model="anthropic/claude-haiku-5.5", result="error", turns=12, cost=0.02, minutes=2.0, failures=0)
        c = dict(b, result="ok", turns=14, cost=0.04)
        got = self.mt.by_model([a, b, c])
        h = got["anthropic/claude-haiku-5.5"]
        self.assertEqual((h["runs"], h["ok"], h["turns"], h["cost"], h["cost_total"]), (2, 1, 13.0, 0.03, 0.06))
        self.assertEqual(got["xiaomi/mimo-v2.6-pro"]["tool_failures"], 4)

    def test_a_block_that_is_not_a_run_is_skipped(self):
        noise = json.dumps({"level": "info", "msg": "x" * 300, "detail": {"a": 1}})
        self.assertEqual(self.mt.runs(noise + "\n" + noise), [])


def verifier_says(script, setup):
    """What a real verifier prints, run against a scratch ecosystem - so the
    lines below are the verifiers' own words, not a copy that can drift."""
    with tempfile.TemporaryDirectory() as root:
        setup(Path(root))
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "0", "0", "0"],
                           capture_output=True, text=True, env=dict(os.environ, ECOSYSTEM_ROOT=root))
        return r.stdout


def belfort_unreadable(root):
    (root / "agents/belfort/state").mkdir(parents=True)
    (root / "agents/belfort/state/portfolio.json").write_text("{not json")


class EachRunCarriesItsVerdict(unittest.TestCase):
    """"ok" is OpenClaw saying the model stopped talking; a model on trial is
    judged on whether its work passed the verifier. Ace's missing-bankroll
    failure never says FAILED on its first line, and Belfort's unreadable
    portfolio says it at the end - reading only "verify: PASS|FAILED" up
    front counted both as unchecked, so a failing model looked clean."""

    @classmethod
    def setUpClass(cls):
        cls.mt = load()
        run = (FIXTURES / "openclaw-agent-json-2026.9.log").read_text()
        haiku = run.replace("xiaomi/mimo-v2.6-pro", "anthropic/claude-haiku-5.5")
        cls.failed = verifier_says("ace-verify.py", lambda root: None)
        cls.passed = "ace-verify: PASS - cycle 41, bankroll $10,212.50 (+2.12%), 2 open, memory +311B\n"
        cls.belfort = verifier_says("belfort-verify.py", belfort_unreadable)
        # a failed run, one whose verifier never ran (a provider error exits
        # first), then a passing one - the verdict belongs to the run just
        # before it, not to the first run since the last verdict
        cls.raw = run + cls.failed + haiku + haiku + cls.passed

    def test_the_verifiers_really_print_these(self):
        self.assertIn("does not exist", self.failed)
        self.assertTrue(self.belfort.startswith("belfort-verify: cannot read"), self.belfort)
        self.assertTrue(self.belfort.rstrip().endswith("FAILED"), self.belfort)

    def test_each_run_gets_the_verdict_after_it(self):
        self.assertEqual([r["verdict"] for r in self.mt.runs(self.raw)], ["FAILED", None, "PASS"])

    def test_passes_are_counted_over_checked_runs(self):
        got = self.mt.by_model(self.mt.runs(self.raw))
        h, m = got["anthropic/claude-haiku-5.5"], got["xiaomi/mimo-v2.6-pro"]
        self.assertEqual((h["runs"], h["checked"], h["passed"]), (2, 1, 1))
        self.assertEqual((m["runs"], m["checked"], m["passed"]), (1, 1, 0))

    def test_a_failure_said_at_the_end_is_a_failure(self):
        run = (FIXTURES / "openclaw-agent-json-2026.9.log").read_text()
        self.assertEqual(self.mt.runs(run + self.belfort)[0]["verdict"], "FAILED")


if __name__ == "__main__":
    unittest.main()
