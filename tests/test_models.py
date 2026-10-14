"""model-trial.py: each model's real runs, read from the journal OpenClaw
writes, so a model trial is judged on what the runs did and cost.

    python3 -m unittest tests.test_models -b
"""

import importlib.util
import json
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


if __name__ == "__main__":
    unittest.main()
