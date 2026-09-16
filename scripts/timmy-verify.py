#!/usr/bin/env python3
"""
timmy-verify.py — did the cycle actually do the work?

systemd only knows whether a process exited 0. It cannot tell a cycle that
wrote four reports from one that wrote a paragraph into the chat and quit.
This ecosystem has already shipped that failure twice (Belfort's portfolio
went stale for three cycles; Scout printed six ideas and saved none), and
both were logged as successes.

So this runs after every Timmy cycle and fails loudly if the deliverables
are not on disk. A missing report is a broken run, not a quiet one.

Checks, against today's date in US/Eastern:
  * MEMORY.md gained a line
  * one report file exists per ticker that fetched OK, WRITTEN BY THIS RUN -
    a file left by an earlier cycle today does not count. Without that check
    a run that did nothing at all inherits the previous run's files and
    passes, which is the exact failure this script exists to prevent.
  * each report is long enough to be a real report, not a stub
  * each report names a lean

Usage:  timmy-verify.py <memory-bytes-before> <run-started-epoch>

Standard library only.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
AGENT = ROOT / "agents" / "timmy"
MIN_WORDS = 200          # the spec asks for 200-600 - hold that line.
                         # This was 150 "for slack", and the slack is exactly
                         # what a 156-word skeleton walked through.

# Anchored to the "Lean:" line on purpose. A bare search for BUY|HOLD|SELL
# matches "To BUY:" in the What-would-flip-me section and reports the wrong
# call - which it did, on the first report this was run against.
LEAN = re.compile(r"^\W*\**\s*Lean\s*:?\**\s*\**\s*(BUY|HOLD|SELL)\b",
                  re.IGNORECASE | re.MULTILINE)


def eastern_today():
    """US market date without pulling in a tz library. Eastern is UTC-4 or
    UTC-5; -5 is the safe choice for 'which trading day is it' because the
    only hours it gets wrong are after 19:00 ET, when no cycle runs."""
    return (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d")


def main():
    before = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    # Epoch when the cycle started. Anything older than this on disk was not
    # written by this run. 0 disables the check (a manual invocation).
    started = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    problems, notes = [], []

    memory = AGENT / "MEMORY.md"
    after = memory.stat().st_size if memory.exists() else 0
    if after <= before:
        problems.append(f"MEMORY.md did not grow ({before} -> {after} bytes) "
                        "- the cycle recorded nothing")

    try:
        meta = json.loads((AGENT / "data" / "_meta.json").read_text())
    except Exception as exc:
        print(f"timmy-verify: cannot read _meta.json ({exc}) - "
              "cannot tell which tickers were expected")
        return 1

    # A cycle that correctly skipped on stale data writes no reports, and that
    # is a pass, not a failure - the memory line is the whole deliverable.
    asof = meta.get("asof_utc", "")
    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.strptime(asof, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                 ).total_seconds() / 3600
    except Exception:
        age_h = 999
    if age_h > meta.get("stale_after_hours", 24):
        if problems:
            print("timmy-verify: FAILED\n  - " + "\n  - ".join(problems))
            return 1
        print(f"timmy-verify: data was stale ({age_h:.1f}h) - reports correctly "
              "skipped, memory line written. PASS")
        return 0

    day = eastern_today()
    expected = meta.get("fetched_ok") or []
    if not expected:
        problems.append("no tickers fetched OK - the fetcher is broken, not the agent")

    for sym in expected:
        path = AGENT / "reports" / f"{day}-{sym}.md"
        if not path.exists():
            problems.append(f"no report for {sym} (expected reports/{path.name})")
            continue
        if started and path.stat().st_mtime < started:
            age = int(time.time() - path.stat().st_mtime)
            problems.append(f"{sym}: reports/{path.name} is stale - last written "
                            f"{age}s ago, before this cycle started. This run did "
                            "not write it; an earlier one did.")
            continue
        text = path.read_text()
        words = len(text.split())
        if words < MIN_WORDS:
            problems.append(f"{sym}: report is {words} words - too short to be a real report")
        # The comparison against the previous read is the one thing a reader
        # cannot get from the data file, and it was being skipped whenever the
        # lean happened not to move.
        if "since last time" not in text.lower():
            problems.append(f"{sym}: no '### Since last time' block - the "
                            "comparison against the previous read is required "
                            "every cycle, not only when the lean moves")
        leans = LEAN.findall(text)
        if not leans:
            problems.append(f"{sym}: no 'Lean: BUY|HOLD|SELL' line - "
                            "the report must state its call in that exact form")
        else:
            # Later cycles append a section to the same file, so the last
            # Lean line is the current one.
            notes.append(f"{sym}: {words}w, lean {leans[-1].upper()}"
                         + (f" ({len(leans)} cycles today)" if len(leans) > 1 else ""))

    if problems:
        print("timmy-verify: FAILED - the cycle did not produce its deliverables")
        for p in problems:
            print(f"  - {p}")
        print("\nThis run is marked failed on purpose. A cycle that skips its "
              "files is not a success, and silently passing it is how the last "
              "two agents drifted for days without anyone noticing.")
        return 1

    print(f"timmy-verify: PASS - {len(expected)} reports for {day}, memory +{after - before}B")
    for n in notes:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
