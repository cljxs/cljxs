#!/usr/bin/env python3
"""
check-secrets.py — would a push put a credential in a public repo?

    check-secrets.py              # everything git is tracking
    check-secrets.py --worktree   # also name stray backups sitting on disk
    check-secrets.py --path FILE  # just these, for tests

WHY THIS EXISTS. .gitignore covered `credentials.env` and `**/credentials.env`
and nothing else. nano writes `credentials.env.save` and
`credentials.env.save.1` when it is killed mid-edit, and check-credentials.py
opens with advice about pasting a long token into nano on a phone - so those
backups were always going to appear. Three of them were sitting untracked in
agents/emily/state/, holding a live Printify token, one `git add -A` away
from this repo. Nothing would have stopped it: the CI scan only ever looked
at tests/fixtures/, and by the time CI runs the push has already happened.

The patterns live here and nowhere else. The workflow calls this script
rather than carrying its own grep, because a second copy of a secret pattern
is a second answer to "is this a credential", and the two drift.

Exits 1 if anything tracked looks like a secret. Stray on-disk backups are
reported but do not fail: on the droplet they exist until someone deletes
them, and a check that cries wolf gets ignored.

Standard library only.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))

# What a credential looks like in a file. The last one is deliberately broad -
# a 40-character run of base64-ish characters is a token far more often than
# it is anything else - and the exceptions are handled by not committing
# binaries rather than by narrowing it.
# What a credential looks like inside a file, in two tiers. The broad tier
# only runs inside content_scope(): repo-wide it matched thousands of long
# test method names and npm hashes, and a check that cries wolf is a check
# people learn to skip, which is worse than not having one.
# Precise enough to run on every tracked file. A JWT is here because the
# Printify token is one, and the broad rule below misses short ones: a JWT is
# dot-separated, so the run it sees is one segment rather than the whole
# token. Every JWT starts "eyJ" - it is base64 of `{"`.
PRECISE_SHAPES = [
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "an sk- API key"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}"), "a bearer token"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "a JWT"),
]

# Only inside content_scope(). Run everywhere it matched every long test
# method name and every npm integrity hash.
BROAD_SHAPES = [
    (re.compile(r"\b[A-Za-z0-9_-]{40,}\b"), "a 40+ character token-shaped run"),
]

# Where a credential could plausibly end up, and so where the broad pattern is
# allowed to run: fixtures, which are captured from the live droplet, and any
# agent state directory, which is where every credential in this ecosystem
# lives. Everywhere else, only the two precise patterns apply.
def content_scope(rel):
    return rel.startswith("tests/fixtures/") or "/state/" in f"/{rel}"

# What a credential FILE looks like, whatever is inside it. An empty
# credentials.env is still a file that must never be tracked, because the next
# person to fill it in will not think to check.
SECRET_NAMES = [
    re.compile(r"(^|/)credentials\.env($|\.)"),
    re.compile(r"(^|/)\.env($|\.)"),
    re.compile(r"(^|/)[^/]*\.pem$"),
    re.compile(r"(^|/)id_(rsa|ed25519)$"),
]

# ...except the ones that exist to be read. credentials.env.example is tracked
# on purpose and holds key names with no values; flagging it would train
# everyone to ignore this check on its only true positive.
TEMPLATE_SUFFIXES = (".example", ".sample", ".template")

# Editor debris that holds a copy of whatever was being edited. Worth naming
# even when it is not tracked, because the file it shadows usually is secret.
BACKUP_NAMES = [
    re.compile(r"\.save(\.\d+)?$"),
    re.compile(r"\.sw[a-z]$"),
    re.compile(r"~$"),
    re.compile(r"\.bak$"),
]

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv"}
MAX_BYTES = 2_000_000


def tracked():
    """Exactly what a push would carry."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, text=True, timeout=60)
    except Exception as exc:
        print(f"cannot ask git what is tracked: {exc}", file=sys.stderr)
        return []
    return [ROOT / p for p in out.stdout.split("\0") if p]


def matches_any(patterns, text):
    for pat in patterns:
        if pat.search(text):
            return pat
    return None


def content_findings(path, shapes):
    """Credential-shaped strings inside one file, with line numbers."""
    try:
        if path.stat().st_size > MAX_BYTES:
            return []
        text = path.read_text(errors="strict")
    except (OSError, UnicodeDecodeError):
        return []                       # binary or unreadable: nothing to read
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for pat, what in shapes:
            m = pat.search(line)
            if m:
                # The match itself is never printed. Saying where it is, is
                # enough to fix it; printing it would put the secret in a log.
                out.append((n, what, len(m.group(0))))
                break
    return out


def scan(paths):
    """(named, contented) - tracked files that are, or that hold, a secret."""
    named, contented = [], []
    for p in paths:
        rel = str(p.relative_to(ROOT)) if str(p).startswith(str(ROOT)) else str(p)
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        if matches_any(SECRET_NAMES, rel) and not rel.endswith(TEMPLATE_SUFFIXES):
            named.append(rel)
            continue                    # no need to read it too
        shapes = PRECISE_SHAPES + (BROAD_SHAPES if content_scope(rel) else [])
        found = content_findings(p, shapes)
        if found:
            contented.append((rel, found))
    return named, contented


def strays():
    """Editor backups on disk, tracked or not, next to anything secret."""
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if matches_any(BACKUP_NAMES, name):
                out.append(str(Path(dirpath).relative_to(ROOT) / name))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(prog="check-secrets.py")
    ap.add_argument("--worktree", action="store_true",
                    help="also name editor backups sitting on disk")
    ap.add_argument("--path", action="append", default=[],
                    help="scan these instead of what git tracks")
    a = ap.parse_args()

    # --path uses the same rules as a real scan. Making it scan harder would
    # mean the thing you can run by hand answers a different question from
    # the thing CI runs, which is how you end up trusting the wrong one.
    paths = [Path(p).resolve() for p in a.path] if a.path else tracked()
    named, contented = scan(paths)

    if named:
        print("TRACKED CREDENTIAL FILES - these would be public on the next push:")
        for rel in named:
            print(f"  {rel}")
    if contented:
        print("TRACKED FILES HOLDING SOMETHING CREDENTIAL-SHAPED:")
        for rel, found in contented:
            for n, what, length in found:
                print(f"  {rel}:{n}  {what}, {length} characters")

    if a.worktree:
        stray = strays()
        if stray:
            print("\nEditor backups on disk (not tracked, not failing this "
                  "check, but\nthey hold a copy of whatever was being edited "
                  "- delete the ones\nshadowing a credentials file):")
            for rel in stray:
                print(f"  {rel}")

    if named or contented:
        print(f"\n{len(named) + len(contented)} problem(s). Nothing is printed "
              f"from inside those files\non purpose - fix them, do not paste "
              f"them.")
        return 1
    print(f"{len(paths)} tracked file(s) scanned, nothing credential-shaped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
