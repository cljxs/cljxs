"""Work killed half way by a time limit or a restart (2026-10-19).

    python3 -m unittest tests.test_restarts -b

The audit the owner asked for, narrowed to the two failures that have
actually happened here: a step stopped part way that leaves nothing saying
so, and a health warning about an agent that was fine.
"""

import importlib.util
import os
import re
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RestartingTheDeckWaitsForItsJobs(unittest.TestCase):
    """`systemctl restart mission-control-api` stops every process the Deck
    started. The "pond side reaction frog sticker" was an approval killed
    while drawing: its order written, no picture, no record."""

    def setUp(self):
        self.m = load("restart_deck", "restart-deck.py")

    def test_the_deck_itself_is_not_a_job(self):
        # cgroup.procs is one PID per line; the MainPID is node.
        self.assertEqual(self.m.children("812", "812\n"), [])
        self.assertEqual(self.m.children("812", "812\n4410\n4415\n"), ["4410", "4415"])

    def test_it_restarts_at_the_first_moment_nothing_runs(self):
        reads = iter([[("4410", "python3 scripts/scout-review.py approve 7")], []])
        slept, said = [], []
        ok = self.m.wait_idle(read=lambda: next(reads), sleep=slept.append,
                              clock=lambda: 0, say=said.append)
        self.assertTrue(ok)
        self.assertEqual(len(slept), 1)
        self.assertIn("scout-review.py approve 7", "\n".join(said), "says what it is waiting for")

    def test_a_job_that_outlasts_the_wait_is_left_alone(self):
        t = iter(range(0, 10_000, 30))
        ok = self.m.wait_idle(read=lambda: [("4410", "emily-finish.py --retry x")], sleep=lambda s: None,
                              clock=lambda: next(t), say=lambda s: None, max_s=600)
        self.assertFalse(ok, "never restarts over a running job unless told --now")

    def test_the_wait_covers_the_longest_deck_job(self):
        js = (ROOT / "mission-control-api" / "emily.js").read_text()
        retry_ms = re.search(r"'--retry', slug\], \{ cwd: ROOT, timeout: (\d+) \* (\d+) \* (\d+) \}", js)
        self.assertIsNotNone(retry_ms, "Try again's limit moved - recheck MAX_WAIT_S")
        self.assertGreaterEqual(self.m.MAX_WAIT_S * 1000, eval("*".join(retry_ms.groups())))

    def test_the_rules_say_to_use_it(self):
        self.assertIn("scripts/restart-deck.py", (ROOT / "CLAUDE.md").read_text())


class TheRedrawHasTimeForItsOwnWork(unittest.TestCase):
    """redraw-art.service allowed 1800s while its work could take 3240s: its
    comment still said drafts take 300s after they were given 900."""

    def test_the_limit_fits_three_drawings_and_three_drafts_inside_the_hour(self):
        fin = load("emily_finish_limits", "emily-finish.py")
        nb = load("emily_new_build_limits", "emily-new-build.py")
        need = fin.REDRAW_PER_RUN * (nb.ART_TIMEOUT_S + fin.DRAFT_TIMEOUT)
        unit = (ROOT / "deploy" / "redraw-art.service").read_text()
        limit = int(re.search(r"^TimeoutStartSec=(\d+)$", unit, re.M).group(1))
        self.assertGreaterEqual(limit, need)
        self.assertLess(limit, 3600, "the timer is hourly - a run must end before the next")

    def test_the_drawing_limit_is_the_named_one(self):
        src = (SCRIPTS / "emily-new-build.py").read_text()
        self.assertIn("timeout=ART_TIMEOUT_S", src)
        self.assertNotIn("timeout=180", src)


class AceIsNotCalledWrongForRecordingPrices(unittest.TestCase):
    """2026-10-19: "ace: disk says 1m ago, the Deck says 10.8h ago". The Deck
    was right - ace-fetch --close writes lines.json every 5 minutes."""

    def test_the_price_recorders_files_are_not_a_run(self):
        hc = load("health_check_ace", "health-check.py")
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            old = time.time() - 10 * 3600
            for f in ("bankroll.json", "lines.json", "sharp-usage.json"):
                (state / f).write_text("{}")
            os.utime(state / "bankroll.json", (old, old))
            got = hc.newest(state, "*.json", skip=hc.not_a_run("ace"))
            self.assertAlmostEqual(got, old, delta=1, msg="the cycle's own file, not the recorder's")
            self.assertGreater(hc.newest(state, "*.json", skip=hc.not_a_run("belfort")), old + 3600,
                               "no other agent loses a file")


if __name__ == "__main__":
    unittest.main()
