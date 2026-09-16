#!/usr/bin/env python3
"""
signoff.py <agent> — print the end-of-cycle sign-off from the files on disk.

Ace ended a cycle with this, verbatim:

    **STATE:** `<path>` `cycle_count=1`
    **REPORT:** `<path>`
    **MEMORY:** `No significant betting opportunities met the criteria...`
    ### Files Written
    - Database updates and cycle logs generated as per the requirements.

It had written nothing at all. `<path>` was a placeholder in its own
instructions, copied out literally, and "database updates generated as per
the requirements" is what a sign-off looks like when there is nothing behind
it. That is not the model being dishonest so much as being handed a form to
fill in and filling it in.

So the form is gone. This reads the actual files and prints what is actually
there, and it prints MISSING where nothing was written - which the agent sees
*before* it finishes, in time to go back and write it.

    python3 ../../scripts/signoff.py ace

Standard library only.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
CANDIDATES = ROOT / "agents" / "ace" / "data" / "candidates.json"


def jkey(r):
    """Exactly the key mission-control-api/ace.js joins on."""
    return f"{str(r.get('selection') or '').lower()}|{str(r.get('match') or '').lower()}"

# What each agent must have produced, in the order it should be reported.
# (label, kind, relative path, extra)
AGENTS = {
    "ace": [
        ("STATE", "json", "state/bankroll.json", "cycle_count"),
        ("LEDGER", "ledger", "state/ledger.json", None),
        ("REPORT", "report", "reports", None),
        ("MEMORY", "memory", "MEMORY.md", None),
    ],
    "belfort": [
        ("STATE", "json", "state/portfolio.json", "cycle_count"),
        ("REPORT", "report", "reports", None),
        ("MEMORY", "memory", "MEMORY.md", None),
    ],
    "scout": [
        ("STATE", "json", "state/ideas.json", None),
        ("REPORT", "report", "reports", None),
        ("MEMORY", "memory", "state/last-run.txt", None),
    ],
    "fury": [
        ("REPORT", "report", "reports", None),
        ("MEMORY", "memory", "state/last-run.txt", None),
    ],
    "emily": [
        ("REPORT", "report", "reports", None),
        ("MEMORY", "memory", "MEMORY.md", None),
    ],
    "timmy": [
        ("REPORT", "report", "reports", None),
        ("MEMORY", "memory", "MEMORY.md", None),
    ],
}


def age(path):
    secs = (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime)
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    return f"{secs / 3600:.1f}h ago"


def newest_report(d, started):
    """The report this run wrote. A file from an earlier cycle is not it - that
    is how a run that wrote nothing inherits the last one's paperwork."""
    if not d.is_dir():
        return None, "the reports/ folder does not exist"
    files = sorted(d.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return None, "no report files at all"
    newest = files[0]
    if started and newest.stat().st_mtime < started:
        return None, (f"the newest report is {newest.name}, written {age(newest)} - "
                      f"before this cycle started. This run has not written one.")
    return newest, None


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in AGENTS:
        print("usage: signoff.py <agent>   one of: " + ", ".join(sorted(AGENTS)),
              file=sys.stderr)
        return 2
    agent = sys.argv[1]
    base = ROOT / "agents" / agent
    # Optional: epoch this cycle started, so files left by an earlier run are
    # reported as missing rather than counted.
    started = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    lines, missing = [], []
    for label, kind, rel, extra in AGENTS[agent]:
        path = base / rel

        if kind == "report":
            found, why = newest_report(path, started)
            if found:
                words = len(found.read_text().split())
                lines.append(f"{label}: reports/{found.name}  ({words} words, {age(found)})")
            else:
                lines.append(f"{label}: MISSING - {why}")
                missing.append(label)
            continue

        if not path.exists():
            lines.append(f"{label}: MISSING - {rel} does not exist")
            missing.append(label)
            continue

        if kind == "json":
            try:
                doc = json.loads(path.read_text())
            except Exception as exc:
                lines.append(f"{label}: BROKEN - {rel} does not parse ({exc})")
                missing.append(label)
                continue
            bit = f"  {extra}={doc.get(extra)}" if extra else ""
            lines.append(f"{label}: {rel}{bit}  ({age(path)})")

        elif kind == "ledger":
            try:
                doc = json.loads(path.read_text())
            except Exception as exc:
                lines.append(f"{label}: BROKEN - {rel} does not parse ({exc}). "
                             f"Prices go in as plain numbers: 104, never +104.")
                missing.append(label)
                continue
            rows = doc if isinstance(doc, list) else (doc.get("candidates") or [])
            judged = [r for r in rows if r.get("status") in ("passed", "bet")]

            # Say WHAT IS WRONG, not just that something is. A bare "MISSING"
            # sent one cycle into 43 turns of rewriting this file in different
            # shapes, 36 tool failures and $0.19, ending with a proposal to
            # "reformat it in a way that may trick the system into recognizing
            # the change". It was never going to guess its way out.
            if not rows:
                lines.append(f"{label}: EMPTY - {rel} parses but has no rows. "
                             f"Add one with: python3 ../../scripts/ace-judge.py "
                             f"pass 1 --my-pct 55 --why \"your reason\"")
                missing.append(label)
            elif not judged:
                seen = sorted({str(r.get("status")) for r in rows})
                lines.append(
                    f"{label}: NOT COUNTED - {rel} has {len(rows)} rows, but none has "
                    f"status \"passed\" or \"bet\". Found: {', '.join(seen)}. "
                    f"Do not hand-edit this file - use "
                    f"`python3 ../../scripts/ace-judge.py pass <n> --my-pct <n> "
                    f"--why \"...\"`, which writes the row correctly.")
                missing.append(label)
            else:
                # The join is the thing nobody can see by looking at the file.
                # NB: not `base` - that is the agent directory in this loop.
                joined, board = None, []
                try:
                    doc2 = json.loads(CANDIDATES.read_text())
                    board = doc2 if isinstance(doc2, list) else (doc2.get("candidates") or [])
                    bk = {jkey(r) for r in board}
                    joined = sum(1 for r in judged if jkey(r) in bk)
                except Exception:
                    pass
                if joined == 0:
                    bad = judged[0]
                    ex = board[0] if board else {}
                    lines.append(
                        f"{label}: WILL NOT SHOW - {len(judged)} rows, none matching "
                        f"candidates.json, so the board renders every game as unjudged.\n"
                        f"        yours:    selection={bad.get('selection')!r} "
                        f"match={bad.get('match')!r}\n"
                        f"        expected: selection={ex.get('selection')!r} "
                        f"match={ex.get('match')!r}\n"
                        f"        `selection` is the pick, `match` is the fixture - they are "
                        f"not the same string. ace-judge.py copies both for you.")
                    missing.append(label)
                else:
                    extra = f", {joined} on the board" if joined is not None else ""
                    lines.append(f"{label}: {rel}  judged={len(judged)} of {len(rows)} rows"
                                 f"{extra}  ({age(path)})")

        elif kind == "memory":
            text = path.read_text().strip()
            last = text.splitlines()[-1].strip() if text else ""
            if not last:
                lines.append(f"{label}: MISSING - {rel} is empty")
                missing.append(label)
            else:
                lines.append(f"{label}: {last[:160]}  ({age(path)})")

    print("\n".join(lines))
    if missing:
        print()
        print(f"{len(missing)} of {len(AGENTS[agent])} deliverables are missing: "
              f"{', '.join(missing)}.")
        print("Go and write them, then run this again. Do not report a cycle as "
              "finished while this says MISSING - the cycle verifier reads the same "
              "files and will fail the run.")
        return 1
    print("\nAll deliverables present. Paste the lines above as your sign-off.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
