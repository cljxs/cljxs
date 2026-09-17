#!/usr/bin/env python3
"""
remember.py <agent> <text> — append one line to an agent's MEMORY.md.

    python3 scripts/remember.py ace "No bets - every game passed on price."

Ace completed a clean cycle on 2026-09-17: 32 tool calls, no failures, all
four deliverables written. The unit still reported failure, because MEMORY.md
had gone from 258 bytes to 175. The agent had been told:

    Append ONE short line to MEMORY.md. Trim oldest lines past ~2KB.

and the verifier was checking:

    if mem_after <= mem_before:  FAILED

Those cannot both be satisfied. The instruction licenses a shrink and the
check forbids one, so the first legitimate trim would have failed every cycle
from then on - for ace, belfort and emily alike. A third rule lived in
signoff.py, which only asked whether the last line was non-empty.

Appending a line and trimming at a cap is exactly the sort of deterministic
work that does not belong in a prompt. Doing it here means the agent cannot
overwrite its own history by accident, and `written_this_cycle` below is the
single check that replaces all three.

The 2KB cap stays: MEMORY.md is read into the prompt at every wake, so an
uncapped file raises the cost of every future cycle. Trimming is loss, but
unbounded growth is a bill.

Standard library only.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import et_time  # noqa: E402

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
MAX_BYTES = 2048


def memory_path(agent_dir):
    return Path(agent_dir) / "MEMORY.md"


def remember(agent_dir, text, now=None):
    """Append one dated line, trimming the oldest to stay under the cap.

    Returns (path, trimmed_lines). The line just added is never trimmed, no
    matter how long it is - losing the thing you were asked to record would be
    a worse failure than exceeding the cap.
    """
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("refusing to record an empty line")

    p = memory_path(agent_dir)
    line = f"- {et_time.day(now)} {text}"
    existing = p.read_text().rstrip("\n").splitlines() if p.is_file() else []
    lines = existing + [line]

    trimmed = 0
    # Drop from the top, oldest first, but never the new line.
    while len(lines) > 1 and len("\n".join(lines).encode()) > MAX_BYTES:
        lines.pop(0)
        trimmed += 1

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")
    return p, trimmed


def written_this_cycle(path, started):
    """Did THIS run record something? (ok, why_not)

    `path` is the file the agent records into - MEMORY.md for ace, belfort and
    emily, state/last-run.txt for scout and fury. This is the one check; it
    replaces the byte-growth test in ace-verify.py and belfort-verify.py and
    the non-empty test in signoff.py, which disagreed with each other and with
    the instructions.

    Growth is deliberately not the test. A trim at the cap shrinks the file
    while recording perfectly well, and that is the case that turned a healthy
    cycle into a failure.
    """
    p = Path(path)
    if not p.is_file():
        return False, f"{p.name} does not exist"
    if not p.read_text().strip():
        return False, f"{p.name} is empty"
    if started and p.stat().st_mtime < started:
        mins = (started - p.stat().st_mtime) / 60
        return False, (f"{p.name} was last written {mins:.0f} min before this cycle "
                       f"started - this run recorded nothing")
    return True, ""


def main():
    if len(sys.argv) < 3:
        print("usage: remember.py <agent> <one short line>", file=sys.stderr)
        return 2
    agent = sys.argv[1]
    agent_dir = ROOT / "agents" / agent
    if not agent_dir.is_dir():
        print(f"no such agent: {agent}", file=sys.stderr)
        return 2
    try:
        p, trimmed = remember(agent_dir, " ".join(sys.argv[2:]))
    except ValueError as exc:
        print(f"remember: {exc}", file=sys.stderr)
        return 2
    note = f", trimmed {trimmed} old line(s) to stay under {MAX_BYTES}B" if trimmed else ""
    print(f"recorded in {p.relative_to(ROOT)}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
