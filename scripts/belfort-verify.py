#!/usr/bin/env python3
"""
belfort-verify.py — did the cycle actually do the work, and does the book add up?

systemd only knows whether a process exited 0. It cannot tell a cycle that
closed two positions and filed a report from one that printed its reasoning
into the chat and quit. Belfort has already shipped that failure: state went
stale for three cycles and every one was logged as a success.

The reconciliation check is the important one. The portfolio once read -41%
when the real trading loss was under 4%, because three positions were closed
by zeroing their shares and the sale proceeds were never credited to cash.
$3,731.90 left the book and nothing noticed for days. Cash is a closed system:

    starting_cash - everything bought + everything sold == cash

If that identity does not hold, money was invented or destroyed, and no report
written on top of it means anything. That is a hard failure.

Usage:  belfort-verify.py <memory-bytes-before> <cycle-count-before> <run-started-epoch>

Standard library only.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import et_time  # noqa: E402
from et_time import eastern_now  # noqa: E402


def _shared(name):
    """belfort-trade.py has a hyphen in its name, so it cannot be imported by
    the normal statement. Load it by path instead - the point is that the
    balance identity has exactly one implementation."""
    import importlib.util
    path = Path(__file__).resolve().parent / "belfort-trade.py"
    spec = importlib.util.spec_from_file_location("belfort_trade", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, name)


book_gap = _shared("book_gap")

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
AGENT = ROOT / "agents" / "belfort"
STATE = AGENT / "state" / "portfolio.json"

# A cent of slack for float rounding across many trades. Anything larger is
# not rounding, it is a missing entry.
TOLERANCE = 0.01


def num(v, default=0.0):
    try:
        return float(str(v).replace("$", "").replace(",", "").lstrip("+"))
    except Exception:
        return default


def reconcile(p, problems):
    """Cash is a closed system. Prove it.

    The identity itself lives in belfort-trade.py and is imported, not
    restated: two copies of this formula that drifted apart would be worse
    than no check, because one of them would go on reporting fine."""
    gap, bought, sold, expected, actual, unknown = book_gap(p)
    for t in unknown:
        problems.append(f"trade with no recognisable side: {t!r}")

    if abs(gap) > TOLERANCE:
        start = num(p.get("starting_cash"), 10000.0)
        problems.append(
            f"THE BOOK DOES NOT BALANCE. starting ${start:,.2f} - bought "
            f"${bought:,.2f} + sold ${sold:,.2f} = ${expected:,.2f}, but cash "
            f"is ${actual:,.2f}. That is ${gap:+,.2f} of money "
            f"{'invented' if gap > 0 else 'destroyed'}. Every trade must go "
            f"through belfort-trade.py; editing portfolio.json by hand is how "
            f"this happens.")

    # Zero-share rows are the fingerprint of a position closed by editing
    # rather than selling - the exact shape of the bug above.
    ghosts = [x.get("symbol") for x in p.get("positions", [])
              if num(x.get("shares")) <= 0]
    if ghosts:
        problems.append(f"positions with 0 shares still on the book: "
                        f"{', '.join(str(g) for g in ghosts)} - a closed position "
                        f"is removed by `sell`, not zeroed")


def main():
    mem_before = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    cycles_before = int(sys.argv[2]) if len(sys.argv) > 2 else -1
    started = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    problems, notes = [], []

    try:
        p = json.loads(STATE.read_text())
    except Exception as exc:
        print(f"belfort-verify: cannot read {STATE} ({exc}) - "
              "there is no portfolio to check. FAILED")
        return 1

    reconcile(p, problems)

    cycles = int(num(p.get("cycle_count")))
    if cycles_before >= 0 and cycles <= cycles_before:
        problems.append(f"cycle_count did not advance ({cycles_before} -> {cycles}) "
                        "- the cycle never ran `belfort-trade.py mark`, so nothing "
                        "confirms it reached the end")

    if started and STATE.stat().st_mtime < started:
        age = int(time.time() - STATE.stat().st_mtime)
        problems.append(f"portfolio.json was last written {age}s ago, before this "
                        "cycle started - this run did not touch it")

    day = eastern_now().strftime("%Y-%m-%d")
    want = et_time.slot("belfort")
    report = AGENT / "reports" / f"{day}-{want}.md"
    if not report.exists():
        others = sorted(x.name for x in (AGENT / "reports").glob(f"{day}-*.md")) \
            if (AGENT / "reports").is_dir() else []
        extra = f" (found {', '.join(others)})" if others else ""
        problems.append(f"no reports/{report.name} for this wake{extra} - the "
                        f"{want} slot files as `-{want}.md`, and notes dropped in "
                        f"exit_log.txt or into the chat do not count")
    elif started and report.stat().st_mtime < started:
        problems.append(f"reports/{report.name} is left over from an earlier run, "
                        "not written by this one")
    else:
        words = len(report.read_text().split())
        if words < 60:
            problems.append(f"reports/{report.name} is {words} words - too short "
                            "to be a real report")
        else:
            notes.append(f"report {report.name}, {words}w")

    memory = AGENT / "MEMORY.md"
    mem_after = memory.stat().st_size if memory.exists() else 0
    if mem_after <= mem_before:
        problems.append(f"MEMORY.md did not grow ({mem_before} -> {mem_after} bytes) "
                        "- the cycle recorded nothing it will remember")

    if problems:
        print("belfort-verify: FAILED - the cycle did not produce its deliverables")
        for x in problems:
            print(f"  - {x}")
        print("\nThis run is marked failed on purpose. A cycle that skips its files "
              "is not a success, and silently passing it is how this account drifted "
              "for days without anyone noticing.")
        return 1

    held = sum(num(x.get("market_value")) for x in p.get("positions", []))
    value = num(p.get("cash")) + held
    start = num(p.get("starting_cash"), 10000.0)
    print(f"belfort-verify: PASS - cycle {cycles}, book balances, "
          f"value ${value:,.2f} ({(value / start - 1) * 100:+.2f}%), "
          f"{len(p.get('positions', []))} open, memory +{mem_after - mem_before}B")
    for n in notes:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
