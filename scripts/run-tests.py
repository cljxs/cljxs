#!/usr/bin/env python3
"""The test suite, several test classes at a time.

    python3 scripts/run-tests.py                 # everything
    python3 scripts/run-tests.py --changed       # only what this branch's changes touch
    python3 scripts/run-tests.py test_paul test_shop.KitOnDisk    # just these
    python3 scripts/run-tests.py --changed --list                 # what it would run, and why

It prints its own time at the end and keeps each class's time in
tests/.timings.json (not tracked); that file, not a number written in a doc,
is how long the suite takes.

Why it exists (2026-11-03): `unittest discover` ran 2,764 tests one after
another in 31 minutes, and most replies waited on it. Classes go to
--workers processes, longest first by their last time, each worker taking
the next class as it frees up. The last lines are unittest's own ("Ran N
tests", then OK or FAILED), so `| tail -3` reads the verdict as before.

By default the run is in a frozen copy of the working tree (uncommitted
changes included), so files edited while it runs cannot fail it half way -
that happened twice on 2026-10-30. --here runs in the tree itself (CI,
which has nothing editing it).

--changed picks test files from ROUTING.md: every table row naming a changed
path gives its test files, and so does any test file that names the changed
file. test_parsers (the cross-cutting rules) and test_routing always run. A
changed file that neither finds runs everything, and says which file. It is
for a reply's quick check; CI runs everything on every push.
"""

import argparse
import concurrent.futures
import importlib
import io
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = "tests"
TIMINGS = "tests/.timings.json"
ROUTING = "ROUTING.md"
ALWAYS = ("test_parsers", "test_routing")
UNKNOWN_CLASS_SECONDS = 2.0        # a class with no time yet; big files are split by class anyway
CLASS_RE = re.compile(r"^class (\w+)\(", re.M)
DOC_SUFFIXES = (".md", ".txt")
LINE = "=" * 70
DASH = "-" * 70


# ------------------------------------------------------------- what to run

def modules(root):
    return sorted(p.stem for p in (Path(root) / TESTS).glob("test_*.py"))


def classes_in(root, module):
    """Every top-level class in the file, by reading it - importing every file
    to list them would cost the parent what the workers pay anyway. The
    workers drop what is not a TestCase, and report any TestCase this list
    missed (see missed())."""
    text = (Path(root) / TESTS / f"{module}.py").read_text()
    return [f"{module}.{c}" for c in CLASS_RE.findall(text)]


def chosen(root, names):
    """test_x -> its classes; test_x.Class as it is."""
    out = []
    for n in names:
        n = n.removesuffix(".py").split("/")[-1]
        out += [n] if "." in n else classes_in(root, n)
    return out


def routing_rows(text):
    """(paths, test modules) for each table row of ROUTING.md."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        ticks = re.findall(r"`([^`\s]+)`", line)
        paths = [t for t in ticks if "/" in t or "." in t]
        tests = sorted(set(re.findall(r"\b(test_\w+)\b", line)))
        if paths and tests:
            rows.append((paths, tests))
    return rows


def _names(path, row_path):
    return path == row_path or (row_path.endswith("/") and path.startswith(row_path))


def tests_for(root, changed):
    """{module: why} for the changed files, or (None, file) when one of them
    maps to nothing and everything has to run."""
    root = Path(root)
    have = set(modules(root))
    rows = routing_rows((root / ROUTING).read_text()) if (root / ROUTING).is_file() else []
    sources = {m: (root / TESTS / f"{m}.py").read_text() for m in have}
    picked = {m: "always" for m in ALWAYS if m in have}
    for f in changed:
        p = Path(f)
        hits = set()
        if p.parent.as_posix() == TESTS and p.stem in have:
            hits.add(p.stem)
        for paths, tests in rows:
            if any(_names(f, rp) for rp in paths):
                hits |= {t for t in tests if t in have}
        if p.name and not (p.parent.as_posix() == TESTS and p.stem in have):
            hits |= {m for m, src in sources.items() if p.name in src}
        if not hits and not f.endswith(DOC_SUFFIXES):
            return None, f
        for m in sorted(hits):
            picked.setdefault(m, f)
    return picked, None


def changed_files(root, base=None):
    """What differs from base (default: this branch's upstream, so unpushed
    commits count), plus files git does not track yet."""
    def git(*a):
        r = subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)
        return r.stdout.split("\n") if r.returncode == 0 else None
    out = None
    for ref in ([base] if base else ["@{upstream}", "HEAD"]):
        out = git("diff", "--name-only", ref)
        if out is not None:
            break
    if out is None:
        raise SystemExit(f"run-tests: cannot diff against {base or 'the upstream or HEAD'}")
    out += git("ls-files", "-o", "--exclude-standard") or []
    return sorted({f for f in out if f})


# ------------------------------------------------------------ the frozen copy

def frozen_copy(root, dest):
    """The working tree as it is now - tracked files and new ones, edits
    included - so the run does not see a file change under it. The Deck's
    installed packages are linked, not copied."""
    r = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root,
                       capture_output=True, check=True)
    for rel in filter(None, r.stdout.decode().split("\0")):
        src, out = Path(root) / rel, Path(dest) / rel
        if not src.is_file() and not src.is_symlink():
            continue                      # deleted in the tree, not yet in git
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out, follow_symlinks=False)
    # A repository of its own, with the copied files added: some tests ask git
    # what is tracked or ignored (the credential guards), and a plain folder
    # failed 7 of them (2026-11-03).
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(cmd, cwd=dest, check=True, capture_output=True)
    for nm in Path(root).glob("*/node_modules"):
        link = Path(dest) / nm.relative_to(root)
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(nm.resolve(), target_is_directory=True)
    return Path(dest)


# ----------------------------------------------------------------- a worker

class _Timed(unittest.TextTestResult):
    def startTest(self, test):
        self._t = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        self.times.append((test.id(), time.perf_counter() - self._t))
        super().stopTest(test)


def _init(root):
    os.chdir(root)
    sys.path.insert(0, str(Path(root) / TESTS))
    sys.dont_write_bytecode = True


def run_class(cid):
    """One class in this worker process. Returns plain data: ids, times and
    the failure text unittest would have printed."""
    mod, _, cls = cid.partition(".")
    t0 = time.perf_counter()
    res = {"id": cid, "times": [], "fail": [], "error": [], "skipped": 0, "run": 0, "defined": []}
    try:
        m = importlib.import_module(mod)
    except Exception:
        res["error"].append((f"{cid} (import)", traceback.format_exc()))
        res["seconds"] = time.perf_counter() - t0
        return res
    res["defined"] = sorted(f"{mod}.{n}" for n, o in vars(m).items()
                            if isinstance(o, type) and issubclass(o, unittest.TestCase)
                            and o.__module__ == m.__name__
                            and unittest.defaultTestLoader.getTestCaseNames(o))
    obj = getattr(m, cls, None)
    if not (isinstance(obj, type) and issubclass(obj, unittest.TestCase)):
        res["seconds"] = time.perf_counter() - t0
        return res
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(obj)
    runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0, buffer=True, resultclass=_Timed)
    _Timed.times = res["times"]
    r = runner.run(suite)
    res["run"] = r.testsRun
    res["skipped"] = len(r.skipped)
    res["fail"] = [(str(t), tb) for t, tb in r.failures + [(t, "unexpected success") for t in r.unexpectedSuccesses]]
    res["error"] = [(str(t), tb) for t, tb in r.errors]
    res["seconds"] = time.perf_counter() - t0
    return res


# ------------------------------------------------------------------ the run

def load_timings(root):
    try:
        return json.loads((Path(root) / TIMINGS).read_text())
    except (OSError, ValueError):
        return {}


def missed(scheduled, results, whole):
    """TestCase classes, in a file asked for whole, that a worker found and no
    one was given - a class this script's reading of the file did not see.
    Reported as a failure: a test that silently never runs is the thing to
    avoid. (A file asked for by one class is not judged on the rest.)"""
    defined = set()
    for r in results:
        defined |= set(r["defined"])
    return sorted(c for c in defined - set(scheduled) if c.partition(".")[0] in whole)


def run(root, ids, workers, out=sys.stdout, timings_root=None, whole=None):
    """Run ids (module.Class) in workers processes. Returns the exit code.
    whole: the files asked for entire (default: every file in ids)."""
    whole = set(whole if whole is not None else (c.partition(".")[0] for c in ids))
    timings = load_timings(timings_root or root)
    order = sorted(ids, key=lambda c: -timings.get(c, UNKNOWN_CLASS_SECONDS))
    t0 = time.perf_counter()
    results, broken = [], []
    ctx = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(workers, mp_context=ctx, initializer=_init,
                                                initargs=(str(root),)) as pool:
        futs = {pool.submit(run_class, c): c for c in order}
        for f in concurrent.futures.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:                       # a worker died (os._exit, a crash)
                broken.append((futs[f], f"{type(e).__name__}: {e}"))
    wall = time.perf_counter() - t0
    n = sum(r["run"] for r in results)
    fails = [x for r in results for x in r["fail"]]
    errors = [x for r in results for x in r["error"]] + [(f"{c} (worker)", why) for c, why in broken]
    errors += [(f"{c} (never scheduled)", "a TestCase class run-tests.py did not find; nothing of it ran")
               for c in missed(ids, results, whole)]
    skipped = sum(r["skipped"] for r in results)
    for kind, items in (("ERROR", errors), ("FAIL", fails)):
        for name, tb in items:
            print(LINE, f"{kind}: {name}", DASH, tb, sep="\n", file=out)
    if timings_root is not False:
        new = dict(timings)
        new.update({r["id"]: round(r["seconds"], 2) for r in results if r["run"]})
        path = Path(timings_root or root) / TIMINGS
        try:
            path.write_text(json.dumps(dict(sorted(new.items())), indent=0) + "\n")
        except OSError:
            pass
    slow = sorted(results, key=lambda r: -r["seconds"])[:5]
    print(DASH, file=out)
    print("slowest: " + ", ".join(f"{r['id']} {r['seconds']:.0f}s" for r in slow), file=out)
    print(f"Ran {n} tests in {wall:.1f}s ({workers} at a time)", file=out)
    print("", file=out)
    extra = f"skipped={skipped}" if skipped else ""
    if fails or errors:
        bits = ", ".join(b for b in (f"failures={len(fails)}" if fails else "",
                                     f"errors={len(errors)}" if errors else "", extra) if b)
        print(f"FAILED ({bits})", file=out)
        return 1
    print(f"OK ({extra})" if extra else "OK", file=out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the test suite several classes at a time.")
    ap.add_argument("names", nargs="*", help="test_x or test_x.Class; default: everything")
    ap.add_argument("--changed", action="store_true", help="only the tests this branch's changes touch")
    ap.add_argument("--base", help="with --changed: diff against this ref instead of the upstream")
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--here", action="store_true", help="run in the working tree, not a frozen copy")
    ap.add_argument("--list", action="store_true", help="say what would run and why; run nothing")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)   # tests: a tree of their own
    a = ap.parse_args(argv)
    root = a.root.resolve()

    if a.changed:
        files = changed_files(root, a.base)
        picked, unknown = tests_for(root, files)
        if picked is None:
            print(f"run-tests: {unknown} is in no ROUTING.md row and no test names it - running everything")
            names = modules(root)
        else:
            names = sorted(picked)
            print(f"run-tests: {len(files)} changed file(s) -> " + ", ".join(names))
            if a.list:
                for m in names:
                    print(f"  {m:24} {picked[m]}")
    else:
        names = a.names or modules(root)
    ids = chosen(root, names)
    whole = {n.removesuffix(".py").split("/")[-1] for n in names if "." not in n.removesuffix(".py")}
    if a.list:
        print(f"{len(ids)} classes in {len(names)} file(s)")
        return 0
    if a.here:
        return run(root, ids, a.workers, whole=whole)
    with tempfile.TemporaryDirectory(prefix="run-tests-") as d:
        copy = frozen_copy(root, d)
        print(f"run-tests: a frozen copy of the tree, {a.workers} at a time", flush=True)
        return run(copy, ids, a.workers, timings_root=root, whole=whole)


if __name__ == "__main__":
    sys.exit(main())
