#!/usr/bin/env python3
"""
health-check.py — is the dashboard wrong, or are the agents not running?

Those look identical from the Command Deck and have opposite fixes. Scout was
called broken this morning on the strength of a dashboard reading; it had been
working correctly for days and the dashboard was reading the wrong file.

So this compares three independent sources for every agent and says where they
disagree:

  TIMER     what systemd last did and will do next
  DISK      the files the agent actually wrote, by mtime
  DECK      what /api/dashboard is reporting

Agreement means the picture is real. Disagreement names which of the three is
odd, which is the thing you cannot see from the dashboard alone.

Standard library only. Read-only: it changes nothing.
"""

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
AGENTS = ROOT / "agents"
API = os.environ.get("DECK_URL", "http://127.0.0.1:3001")


def ago(ts):
    if not ts:
        return "never"
    mins = (datetime.now(timezone.utc).timestamp() - ts) / 60
    if mins < 1:
        return "just now"
    if mins < 90:
        return f"{int(mins)}m ago"
    if mins < 2880:
        return f"{mins/60:.1f}h ago"
    return f"{int(mins/1440)}d ago"


HAVE_SYSTEMD = None


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""


def parse_stamp(v):
    """systemd prints 'Wed 2026-09-16 18:55:37 UTC'. Empty means never ran."""
    if not v or v.strip() in ("", "n/a"):
        return None
    try:
        return datetime.strptime(" ".join(v.split()[1:3]), "%Y-%m-%d %H:%M:%S") \
            .replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def unit_facts(unit):
    out = sh("systemctl", "show", unit, "--no-pager",
             "-p", "Result", "-p", "ExecMainStartTimestamp", "-p", "LoadState")
    f = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    return (f.get("Result", "?"), parse_stamp(f.get("ExecMainStartTimestamp", "")),
            f.get("LoadState", "?"))


def timer_next(unit):
    for line in sh("systemctl", "list-timers", "--all", "--no-pager", unit).splitlines():
        if unit in line:
            return " ".join(line.split()[:3])
    return ""


def newest(path, pattern="*"):
    """mtime of the newest matching file, or None."""
    try:
        if path.is_file():
            return path.stat().st_mtime
        files = [f for f in path.glob(pattern) if f.is_file()]
        return max(f.stat().st_mtime for f in files) if files else None
    except Exception:
        return None


def deck():
    try:
        with urllib.request.urlopen(f"{API}/api/dashboard", timeout=10) as r:
            return {a["name"]: a for a in (json.loads(r.read()).get("agents") or [])}
    except Exception as exc:
        print(f"!! cannot reach the Command Deck at {API} ({exc})")
        print("   Is it running?   systemctl status mission-control-api\n")
        return None


def main():
    global HAVE_SYSTEMD
    # `offline` means the binary exists but no system manager is running - a
    # container, usually. Treating that as "every timer is missing" would fill
    # the report with four alarming and entirely false warnings.
    state = sh("systemctl", "is-system-running").strip()
    HAVE_SYSTEMD = state not in ("", "offline", "unknown")
    if not HAVE_SYSTEMD:
        print("!! systemctl is not answering - the TIMER column and the timer")
        print("   warnings below are unavailable, not evidence of anything.\n")
    seen = deck()
    names = sorted(d.name for d in AGENTS.iterdir()
                   if d.is_dir() and not d.name.startswith("."))

    print(f"{'agent':<10}{'state':<11}{'timer last':<13}{'disk last':<13}"
          f"{'deck says':<13}{'next wake':<22}")
    print("-" * 82)

    problems = []
    for name in names:
        adir = AGENTS / name
        if (adir / "RETIRED").is_file():
            print(f"{name:<10}{'retired':<9}{'-':<13}{'-':<13}{'-':<13}{'-':<22}")
            continue

        # Not every agent is timer-driven. Emily is woken by the task queue
        # when you approve one of Scout's ideas, so she has no cycle unit at
        # all - reporting that as "cannot run" is a false alarm about the one
        # agent that has actually shipped a product.
        scheduled = (ROOT / "deploy" / f"{name}-cycle.service").is_file()
        result, ran_at, load = unit_facts(f"{name}-cycle.service")
        nxt = timer_next(f"{name}-cycle.timer")

        # The newest thing the agent wrote, whatever kind of file it is.
        disk = max((t for t in (
            newest(adir / "reports", "*.md"),
            newest(adir / "state", "*.json"),
            newest(adir / "state" / "last-run.txt"),
            newest(adir / "MEMORY.md"),
        ) if t), default=None)

        d = (seen or {}).get(name)
        deck_age = None
        if d and d.get("last_run_age_min") is not None:
            deck_age = datetime.now(timezone.utc).timestamp() - d["last_run_age_min"] * 60

        state = "ok" if result == "success" else (result or "?")
        if not scheduled:
            state = "on demand"
        elif load == "not-found":
            state = "no unit"

        print(f"{name:<10}{state:<11}{ago(ran_at):<13}{ago(disk):<13}"
              f"{(ago(deck_age) if d else 'absent'):<13}{nxt:<22}")

        # --- where the three disagree -------------------------------------
        if seen is not None and not d:
            problems.append(f"{name}: wrote files {ago(disk)} but the Deck does not list it at all")
        elif d and disk and deck_age and abs(disk - deck_age) > 3600:
            problems.append(f"{name}: disk says {ago(disk)}, the Deck says {ago(deck_age)} - "
                            f"the Deck is reading something other than the newest file")
        if HAVE_SYSTEMD and scheduled and result not in ("success", "?", ""):
            problems.append(f"{name}: last cycle ended '{result}' - "
                            f"scripts/last-run.py {name} says why")
        if not HAVE_SYSTEMD or not scheduled:
            pass
        elif load == "not-found":
            problems.append(f"{name}: {name}-cycle.service is not installed - "
                            f"it cannot run at all. scripts/deploy.sh {name}")
        elif not nxt:
            problems.append(f"{name}: no active timer, so nothing will wake it. "
                            f"systemctl enable --now {name}-cycle.timer")

    print()
    if problems:
        print("DISAGREEMENTS")
        for p in problems:
            print(f"  - {p}")
    elif seen is None:
        print("Could not reach the Deck, so nothing was compared against it.")
        print("The DISK column above is still real - it is the files on disk.")
    elif not HAVE_SYSTEMD:
        print("Disk and Deck agree. The timers were not checked.")
    else:
        print("All three sources agree. The Deck is showing the truth.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
