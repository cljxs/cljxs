#!/usr/bin/env python3
"""
ace-focus.py — point Ace at one sport, and a slate he can finish, for today only.

    python3 scripts/ace-focus.py                 what today's focus is, if any
    python3 scripts/ace-focus.py today cfb       college football only, 4 games, today
    python3 scripts/ace-focus.py today cfb --games 3
    python3 scripts/ace-focus.py clear           back to the normal slate now

WHY. Ace is told to study three or four games a cycle and sweep the rest with
one reason. On a normal eight-game slate that means most games get no
estimate at all - on 2026-09-26 he studied three baseball games and swept
all four college football games the owner wanted. A focus day makes the
slate the size of what he studies: one sport, a handful of games chosen by
code (freshest injury news first, then soonest), and every one of them gets
its own estimate - ace-judge.py refuses a sweep while a focus stands.

FOR ONE DAY. The focus carries its Eastern date and stops applying at
midnight Eastern by itself - a setting
that stays until somebody remembers to undo it is the forgotten-edit failure
this repo keeps naming. `clear` ends it sooner.

The file: agents/ace/state/focus.json. ace-fetch.py and ace-judge.py both
read it through today() here, so they cannot disagree about whether today
is a focus day.

Standard library only.
"""

import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import et_time  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", SCRIPTS.parent))
FOCUS = ROOT / "agents" / "ace" / "state" / "focus.json"

GAMES = 4            # what Ace's instructions let him study in one cycle
GAMES_MAX = 6

# The sports ace-fetch.py knows. Read from there rather than listed here, so a
# sport added to the fetcher can be focused on without touching this file.
def known_sports():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ace_fetch_sports", SCRIPTS / "ace-fetch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SPORTS


def today(day=None, path=None):
    """Today's focus as {"sport", "games", "day"}, or None. A focus written for
    another Eastern day is no focus at all."""
    day = day or et_time.day()
    try:
        d = json.loads(Path(path or FOCUS).read_text())
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("day") != day:
        return None
    sport = str(d.get("sport") or "").strip().lower()
    try:
        games = int(d.get("games"))
    except (TypeError, ValueError):
        return None
    if not sport or not 1 <= games <= GAMES_MAX:
        return None
    return {"sport": sport, "games": games, "day": day}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        f = today()
        print(f"Ace's focus today ({et_time.day()}): {f['sport']}, {f['games']} games"
              if f else "No focus today - Ace takes the normal slate.")
        return 0
    if argv[0] == "clear":
        FOCUS.unlink(missing_ok=True)
        print("Focus cleared - Ace takes the normal slate from his next fetch.")
        return 0
    if argv[0] == "today" and len(argv) >= 2:
        sport = argv[1].strip().lower()
        sports = known_sports()
        if sport not in sports:
            print(f"unknown sport {sport!r} - one of: {', '.join(sports)}", file=sys.stderr)
            return 2
        games = GAMES
        if "--games" in argv:
            try:
                games = int(argv[argv.index("--games") + 1])
            except (IndexError, ValueError):
                print("--games needs a number", file=sys.stderr)
                return 2
        if not 1 <= games <= GAMES_MAX:
            print(f"--games must be 1 to {GAMES_MAX}: Ace studies three or four a cycle",
                  file=sys.stderr)
            return 2
        FOCUS.parent.mkdir(parents=True, exist_ok=True)
        FOCUS.write_text(json.dumps({"day": et_time.day(), "sport": sport, "games": games}) + "\n")
        print(f"Ace's focus for {et_time.day()} (Eastern): {sport} only, {games} games, every "
              f"one estimated. It ends at midnight Eastern, or: python3 scripts/ace-focus.py clear")
        print("Now refresh his slate:  python3 scripts/ace-fetch.py")
        return 0
    print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
