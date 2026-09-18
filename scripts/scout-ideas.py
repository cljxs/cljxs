#!/usr/bin/env python3
"""
scout-ideas.py merge — fold Scout's new proposals into the idea log.

    python3 scripts/scout-ideas.py merge        # called by scout-cycle.sh
    python3 scripts/scout-ideas.py show

Scout was told, in bold, at line 18 of its own instructions: "WRITE
state/ideas.json - every existing entry kept, yours appended". On
2026-09-18 it wrote "Cleared existing ideas to reflect no new proposals" and
emptied the file. Nothing was lost because nothing was pending, which is luck,
not a safeguard.

It is the second time: the comment in scout-cycle.service records that Scout
destroyed MEMORY.md twice the same way, and the fix there was to take the job
off it. Scout writes a scratch file it may overwrite freely, and code does the
append. Same fix here.

So Scout writes state/proposals.json - only the new ideas, no ids, no status -
and this merges them. Existing entries cannot be lost, because nothing here
removes one. Ids are assigned here too: Scout was computing max(existing)+1,
which is another thing it cannot get wrong if it never does it.

Standard library only.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
STATE = ROOT / "agents" / "scout" / "state"
IDEAS = STATE / "ideas.json"
PROPOSALS = STATE / "proposals.json"

KEEP = ("title", "product", "angle", "brief")


def load_ideas():
    """The log, always as {"ideas": [...]}. A missing or broken file reads as
    empty rather than raising - but see merge(), which refuses to write over
    a file it could not read."""
    try:
        d = json.loads(IDEAS.read_text())
    except Exception:
        return {"ideas": []}, False
    if isinstance(d, list):
        return {"ideas": d}, True
    d.setdefault("ideas", [])
    return d, True


def load_proposals():
    try:
        d = json.loads(PROPOSALS.read_text())
    except Exception:
        return []
    rows = d if isinstance(d, list) else (d.get("proposals") or d.get("ideas") or [])
    return [r for r in rows if isinstance(r, dict) and str(r.get("title") or "").strip()]


def cmd_merge():
    existing, readable = load_ideas()
    if IDEAS.is_file() and not readable:
        print(f"{IDEAS} does not parse. Not touching it - a merge into a file "
              f"I cannot read would destroy whatever is in there.", file=sys.stderr)
        return 1

    new = load_proposals()
    if not new:
        print(f"no new proposals ({len(existing['ideas'])} idea(s) in the log, unchanged)")
        return 0

    have = {str(i.get("title", "")).strip().lower() for i in existing["ideas"]}
    next_id = max([int(i.get("id") or 0) for i in existing["ideas"]] or [0]) + 1

    added, skipped = [], []
    for row in new:
        title = str(row["title"]).strip()
        if title.lower() in have:
            skipped.append(title)
            continue
        entry = {"id": next_id, "status": "pending",
                 "proposed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        entry.update({k: row.get(k) for k in KEEP if row.get(k) is not None})
        entry["title"] = title
        existing["ideas"].append(entry)
        have.add(title.lower())
        added.append(f"{next_id}  {title}")
        next_id += 1

    IDEAS.parent.mkdir(parents=True, exist_ok=True)
    IDEAS.write_text(json.dumps(existing, indent=1) + "\n")
    # Scout owns this file and may overwrite it; emptying it here means a rerun
    # cannot add the same ideas twice.
    PROPOSALS.write_text("[]\n")

    for line in added:
        print(f"  + {line}")
    for t in skipped:
        print(f"  = already proposed: {t}")
    print(f"{len(added)} added, {len(existing['ideas'])} idea(s) in the log")
    return 0


def cmd_show():
    d, _ = load_ideas()
    for i in d["ideas"]:
        print(f"  [{i.get('status','pending')}] {i.get('id')}  {i.get('title')} "
              f"({i.get('product')})")
    print(f"{len(d['ideas'])} idea(s)")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "merge":
        return cmd_merge()
    if cmd == "show":
        return cmd_show()
    print("usage: scout-ideas.py merge|show", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
