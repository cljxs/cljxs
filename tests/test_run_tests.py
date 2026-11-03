"""scripts/run-tests.py - the suite, four classes at a time.

    python3 -m unittest tests.test_run_tests -b

Owner, 2026-11-03: the full suite took 5-30 minutes and most replies waited
on it. A faster runner is only worth having if it runs every test the slow
one did and reports a failure the same way, so that is what these hold it
to: a class it cannot see is reported rather than skipped, a failure ends
in unittest's own FAILED line (CI greps for it), --changed finds an area's
tests through ROUTING.md, and the frozen copy is the tree as edited.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("run_tests_t", ROOT / "scripts" / "run-tests.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = load()

ROUTING = """\
| Agent | Code | Tests |
|---|---|---|
| Shop | `scripts/shop.py`, `scripts/kit.py` | `tests/test_shop.py` |
| Clip | `clipper/` | `tests/test_clip.py`; test_parsers `Clip*` |
| Words only | `scripts/loose.py` | - |
"""


class ATree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "ROUTING.md").write_text(ROUTING)

    def file(self, rel, text=""):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
        return p


class ChangedFilesFindTheirTests(ATree):

    def setUp(self):
        super().setUp()
        for m in ("test_shop", "test_clip", "test_parsers", "test_routing", "test_other"):
            self.file(f"tests/{m}.py", "")
        self.file("tests/test_other.py", "WHERE = ROOT / 'scripts' / 'loose.py'\n")

    def test_a_row_gives_its_tests_and_the_fast_suite_always_runs(self):
        picked, unknown = R.tests_for(self.root, ["scripts/kit.py"])
        self.assertIsNone(unknown)
        self.assertEqual(sorted(picked), ["test_parsers", "test_routing", "test_shop"])
        self.assertEqual(picked["test_shop"], "scripts/kit.py", "--list says which file picked it")

    def test_a_folder_in_a_row_covers_the_files_in_it(self):
        picked, _ = R.tests_for(self.root, ["clipper/render.py"])
        self.assertIn("test_clip", picked)
        self.assertNotIn("test_shop", picked)

    def test_a_test_that_names_the_file_is_picked(self):
        # Guards: a file whose row has no test file, tested only by name.
        picked, unknown = R.tests_for(self.root, ["scripts/loose.py"])
        self.assertIsNone(unknown)
        self.assertIn("test_other", picked)

    def test_a_changed_test_file_runs_itself(self):
        picked, _ = R.tests_for(self.root, ["tests/test_other.py"])
        self.assertIn("test_other", picked)

    def test_words_alone_run_only_the_fast_suite(self):
        picked, unknown = R.tests_for(self.root, ["HANDOFF.md"])
        self.assertIsNone(unknown)
        self.assertEqual(sorted(picked), ["test_parsers", "test_routing"])

    def test_code_nothing_ties_to_runs_everything(self):
        # Guards: --changed quietly running nothing for a file it cannot place.
        picked, unknown = R.tests_for(self.root, ["scripts/kit.py", "scripts/brand-new.py"])
        self.assertIsNone(picked)
        self.assertEqual(unknown, "scripts/brand-new.py")


class TheRealRoutingTable(unittest.TestCase):
    """The rows this repo's areas are found through."""

    def test_each_area_finds_its_own_file(self):
        for changed, want in (("scripts/tiktok-kit.py", "test_shop"), ("clipper/render.py", "test_clipper"),
                              ("scripts/paul.py", "test_paul"), ("scripts/brain.py", "test_brain"),
                              ("mission-control-api/public/village.html", "test_village_frame"),
                              ("scripts/run-tests.py", "test_run_tests"), ("ROUTING.md", "test_routing")):
            picked, unknown = R.tests_for(ROOT, [changed])
            self.assertIsNone(unknown, changed)
            self.assertIn(want, picked, changed)


PASSING = """\
import unittest

class Fine(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    @unittest.skip("not here")
    def test_two(self):
        pass

class Helper:
    pass
"""

FAILING = """\
import unittest

class Broken(unittest.TestCase):
    def test_wrong(self):
        self.assertEqual(1, 2)

class  Hidden (unittest.TestCase):
    def test_never_seen_by_the_reading(self):
        pass
"""


class TheRun(ATree):

    def go(self, *names):
        # As a program, the way it is used: its workers are separate processes
        # that import it by its own path.
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "run-tests.py"), "--root", str(self.root),
                            "--here", "--workers", "2", *names], capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def test_a_clean_run_ends_like_unittest(self):
        self.file("tests/test_a.py", PASSING)
        code, out = self.go("test_a")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], "OK (skipped=1)")
        self.assertRegex(out, r"Ran 2 tests in [\d.]+s \(2 at a time\)")

    def test_a_failure_is_printed_the_way_ci_reads_it(self):
        self.file("tests/test_b.py", FAILING)
        code, out = self.go("test_b")
        self.assertEqual(code, 1)
        self.assertIn("\nFAIL: test_wrong (test_b.Broken.test_wrong)\n", out)
        self.assertIn("AssertionError: 1 != 2", out)
        self.assertTrue(out.rstrip().endswith("FAILED (failures=1, errors=1)"), out[-200:])

    def test_a_class_the_reading_missed_is_reported_not_skipped(self):
        # Guards: a test that silently never runs. "class  Hidden (" is not
        # what classes_in() reads, so no worker is given it - the worker that
        # imported the file says it exists.
        self.file("tests/test_b.py", FAILING)
        self.assertEqual(R.chosen(self.root, ["test_b"]), ["test_b.Broken"])
        _code, out = self.go("test_b")
        self.assertIn("ERROR: test_b.Hidden (never scheduled)", out)

    def test_one_class_by_name(self):
        self.file("tests/test_b.py", FAILING)
        self.file("tests/test_a.py", PASSING)
        code, out = self.go("test_a.Fine", "test_b.Broken")
        self.assertEqual(code, 1)
        self.assertIn("Ran 3 tests", out)
        self.assertNotIn("never scheduled", out, "only the files imported for the asked classes are judged")

    def test_a_file_that_does_not_import_is_an_error(self):
        self.file("tests/test_c.py", "import nothing_by_this_name\n\nclass X(object):\n    pass\n")
        code, out = self.go("test_c")
        self.assertEqual(code, 1)
        self.assertIn("ERROR: test_c.X (import)", out)
        self.assertIn("ModuleNotFoundError", out)


class CIRunsEverything(unittest.TestCase):

    def test_every_push_runs_the_whole_suite_and_names_its_failures(self):
        # Guards: --changed (a reply's quick check) taking CI's place, which
        # would let a break outside the changed area reach the droplet; and
        # the failure step grepping for lines the runner no longer prints.
        wf = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
        step = wf.split("- name: tests\n", 1)[1].split("- name:", 1)[0]
        self.assertIn("python3 scripts/run-tests.py --here 2>&1 | tee test-output.txt", step)
        self.assertNotIn("--changed", step)
        self.assertIn('grep -E "^(FAIL|ERROR): " test-output.txt', wf)


class TheFrozenCopy(ATree):

    def test_it_is_the_tree_as_edited_and_nothing_ignored(self):
        def git(*a):
            subprocess.run(["git", *a], cwd=self.root, check=True, capture_output=True)
        git("init", "-q")
        self.file(".gitignore", "secret.env\n")
        self.file("scripts/a.py", "OLD = 1\n")
        git("add", "-A")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
        self.file("scripts/a.py", "NEW = 2\n")          # edited, not committed
        self.file("scripts/b.py", "B = 1\n")            # new, not added
        self.file("secret.env", "TOKEN=x\n")            # ignored
        (self.root / "ROUTING.md").unlink()             # deleted, not committed
        (self.root / "api" / "node_modules").mkdir(parents=True)
        with tempfile.TemporaryDirectory() as d:
            copy = R.frozen_copy(self.root, d)
            self.assertEqual((copy / "scripts" / "a.py").read_text(), "NEW = 2\n")
            self.assertTrue((copy / "scripts" / "b.py").is_file())
            self.assertFalse((copy / "secret.env").exists(), "an ignored file is not copied")
            self.assertFalse((copy / "ROUTING.md").exists())
            self.assertTrue((copy / "api" / "node_modules").is_symlink())
            self.file("scripts/a.py", "LATER = 3\n")
            self.assertEqual((copy / "scripts" / "a.py").read_text(), "NEW = 2\n", "frozen")


if __name__ == "__main__":
    unittest.main()
