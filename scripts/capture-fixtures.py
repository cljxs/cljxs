#!/usr/bin/env python3
"""
capture-fixtures.py — refresh tests/fixtures/ from the live droplet.

The tests are only worth having if they run against what the tools actually
print. Every parser bug here was a format assumption, so a fixture invented
from memory tests the memory, not the format.

    python3 scripts/capture-fixtures.py            # show what it would write
    python3 scripts/capture-fixtures.py --write    # write it

Run it on the droplet, then commit what it writes.

SAFETY: fixtures get committed to a git remote, so anything captured here
leaves the machine. Every captured byte goes through redact() first, which
masks known credential values literally - it reads every credentials.env and
blanks those exact strings wherever they appear - and then masks anything
else secret-shaped. --write refuses if a known credential value survives.

Standard library only.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
FIXTURES = ROOT / "tests" / "fixtures"

# What to capture: (filename, argv, must_match, why the tests need it)
#
# must_match is not decoration. A failed command still prints something, and
# writing that something over a good fixture replaces real captured output
# with an error message - `systemctl` in a container says "System has not been
# booted with systemd", which would have landed in systemd-show.txt and made
# the timestamp test meaningless. Each capture proves it is the thing it
# claims to be before it is allowed to overwrite anything.
CAPTURES = [
    ("config-unset.txt",
     ["openclaw", "config", "get", "agents.defaults.nothingIsSetHere"],
     r"unset|not set|no value",
     "how openclaw reports a path with no authored value"),
    ("config-timeout-300.txt",
     ["openclaw", "config", "get", "agents.defaults.timeoutSeconds"],
     r"^\s*\d+\s*$",
     "a set scalar"),
    ("config-model-primary.txt",
     ["openclaw", "config", "get", "agents.entries.ace.model.primary"],
     r"[\w.-]+/[\w.:-]+",
     "THE slug line - preflight's SLUG_RE parses exactly this, and the "
     "three-segment bug lived here"),
    ("systemd-show.txt",
     ["systemctl", "show", "ace-cycle.service", "--no-pager",
      "-p", "Result", "-p", "ExecMainStartTimestamp", "-p", "LoadState"],
     r"ExecMainStartTimestamp=",
     "the timestamp format health-check parses"),
]


def known_secrets():
    """Every credential value on this box, so they can be masked literally.

    Pattern-matching alone is not enough - a key that happens to look like a
    word would slip through. Knowing the exact strings is the only reliable
    guarantee, and it costs one read.
    """
    out = set()
    for f in sorted((ROOT / "agents").glob("*/state/credentials.env")):
        try:
            for line in f.read_text().splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                v = s.split("=", 1)[1].strip().strip('"').strip("'")
                if len(v) >= 8:          # short values are not credentials
                    out.add(v)
        except Exception:
            pass
    return out


SECRET_SHAPED = [
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})"), "sk-REDACTED"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._-]{8,}", re.I), r"\1REDACTED"),
    # A long unbroken run of key-ish characters, which no format string here
    # produces but every token does.
    (re.compile(r"\b[A-Za-z0-9_-]{32,}\b"), "REDACTED"),
    (re.compile(r"((?:key|token|secret|password)\"?\s*[:=]\s*\"?)[^\s\",]{8,}", re.I),
     r"\1REDACTED"),
]


def redact(text, secrets):
    for s in secrets:
        text = text.replace(s, "REDACTED")
    for pat, repl in SECRET_SHAPED:
        text = pat.sub(repl, text)
    return text


def normalize(argv):
    """iOS Smart Punctuation rewrites `--write` as `\u2014write` before it ever
    reaches the shell. The flag is then silently unrecognised and the script
    reports that it wrote nothing, which reads as a bug in the script. Accept
    the dash the phone actually sends."""
    out = []
    for a in argv:
        for dash in ("\u2014", "\u2013", "\u2212"):        # em, en, minus
            if a.startswith(dash):
                a = "--" + a[len(dash):]
                break
        out.append(a)
    return out


def main():
    argv = normalize(sys.argv[1:])
    write = "--write" in argv
    secrets = known_secrets()
    print(f"{len(secrets)} credential value(s) known and will be masked literally.\n")

    results = []
    for name, argv, must_match, why in CAPTURES:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            raw = (r.stdout or "") + (r.stderr or "")
        except FileNotFoundError:
            print(f"-- {name}: {argv[0]} is not on PATH, skipped")
            continue
        except Exception as exc:
            print(f"-- {name}: {exc}, skipped")
            continue

        clean = redact(raw, secrets)
        if not clean.strip():
            print(f"-- {name}: produced nothing, skipped")
            continue
        if not re.search(must_match, clean, re.M | re.I):
            print(f"-- {name}: output does not look like the real thing "
                  f"(wanted /{must_match}/), skipped so a good fixture is not")
            print(f"   overwritten with an error. It said: "
                  f"{clean.strip().splitlines()[0][:90]}")
            continue

        # Belt and braces: never write a file that still contains a real one.
        leaked = [s for s in secrets if s in clean]
        if leaked:
            print(f"!! {name}: a credential survived redaction - NOT writing it")
            continue

        results.append((name, clean))
        print(f"== {name}   ({why})")
        for line in clean.splitlines()[:4]:
            print(f"   {line[:150]}")
        print()

    if not write:
        print("Nothing written. Re-run with --write to save these.")
        return 0

    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, clean in results:
        (FIXTURES / name).write_text(clean)
        print(f"wrote tests/fixtures/{name}")
    print("\nNow run:  python3 -m unittest discover -s tests")
    print("Then commit tests/fixtures/. Check the diff before you push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
