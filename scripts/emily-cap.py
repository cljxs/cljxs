#!/usr/bin/env python3
"""
emily-cap.py — raise Emily's daily build cap for today only.

    python3 scripts/emily-cap.py            what today's cap is, and how much is used
    python3 scripts/emily-cap.py today 6    allow 6 builds today (Eastern date)
    python3 scripts/emily-cap.py clear      back to the standing cap now

The raise is written with today's Eastern date and ignored from midnight
Eastern on, so there is nothing to remember to undo. emily-new-build.py owns
the cap and reads it; this only writes the file it reads.
"""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import et_time  # noqa: E402

_spec = importlib.util.spec_from_file_location("emily_new_build", SCRIPTS / "emily-new-build.py")
nb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nb)


def used_today():
    """Builds started today, or None if the Deck cannot be asked."""
    try:
        status, rows = nb.call("GET", "/tasks?assignee=emily")
    except SystemExit:
        return None
    return len(nb.started_today(rows)) if status == 200 else None


def show():
    cap, raised = nb.daily_cap()
    used = used_today()
    print(f"{et_time.day()} (Eastern): cap {cap}"
          f"{' - raised for today, back to ' + str(nb.DAILY_DRAFT_CAP) + ' at midnight Eastern' if raised else ''}"
          f", {'?' if used is None else used} started so far.")
    return 0


def main():
    args = sys.argv[1:]
    if not args:
        return show()
    if args[0] == "clear":
        nb.CAP_TODAY.unlink(missing_ok=True)
        print(f"cleared - the cap is {nb.DAILY_DRAFT_CAP} again.")
        return show()
    if args[0] == "today" and len(args) == 2 and args[1].isdigit() and int(args[1]) > 0:
        n = int(args[1])
        nb.CAP_TODAY.parent.mkdir(parents=True, exist_ok=True)
        nb.CAP_TODAY.write_text(json.dumps({"day": et_time.day(), "cap": n}) + "\n")
        print(f"raised to {n} for {et_time.day()} only.")
        return show()
    print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
