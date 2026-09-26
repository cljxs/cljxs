#!/usr/bin/env python3
"""
emily-finish.py — turn a finished build into a Printify draft.

Run by the dispatcher after Emily completes a task, before the verifier.

Emily was told to create the Printify product and did not: she wrote
status "ready_local" instead, which her instructions reserve for the case
where no token is set. Creating a product takes a blueprint id, a print
provider and variant ids - three dependent lookups before the create - and
multi-step tool work is the thing cheap models quietly skip. Her artwork was
moved into code for the same reason.

Nothing here needs judgement: the listing copy and the artwork are already on
disk, and the blueprint was chosen once by a human.

    emily-finish.py <build_dir>

Exits 0 even when it cannot draft. A local build is still a real build - the
status stays "ready_local", the gallery shows LOCAL ONLY, and the user is told
why. Failing the whole task over the last mile would throw away good work.

"Told why" was a lie until now. The refusal was printed here and went into the
dispatcher log, which nobody reads; the card said LOCAL ONLY and stopped. A
build sat like that with a listing.json full of malformed JSON, the drafter
said so on two separate attempts, and the only way to find out was to ask.
So the reason is written onto build.json as `draft_blocked` and drawn on the
card. Cleared the moment a draft succeeds, because a stale reason is worse
than none.

Standard library only.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))


# Enough to name the problem on a card, not so much that the card becomes a
# log. Every refusal in emily-printify.py draft leads with its own one-line
# summary, so the first lines are the useful ones.
REASON_CHARS = 400


def reason_from(res):
    """The drafter's own words for why it refused.

    Its refusals go to stderr and its progress to stdout, so stderr is the
    reason and stdout is the fallback for a failure that never got that far.
    Not reworded here: a second phrasing of the same refusal is a second
    opinion about what went wrong, and the two drift.
    """
    text = (res.stderr or "").strip() or (res.stdout or "").strip()
    # A refusal leads with its message; a crash leads with forty lines of
    # stack and ends with the only line that says anything. Truncating from
    # the front puts "Traceback (most recent call last):" on the card and
    # throws the exception away, which is the wrong half.
    if text.startswith("Traceback (most recent call last):"):
        last = [ln for ln in text.splitlines() if ln.strip()][-1]
        text = f"the drafter crashed: {last.strip()}"
    if len(text) > REASON_CHARS:
        text = text[:REASON_CHARS].rstrip() + " ..."
    return text or f"the drafter exited {res.returncode} without saying why"


def note_draft(d, blocked):
    """Record - or clear - why the Printify draft is missing.

    Read-modify-write, and it gives up rather than rewrite a build.json it
    cannot parse: overwriting Emily's own account of what she made, to explain
    that something else would not parse, would destroy the more valuable file
    of the two.
    """
    path = d / "build.json"
    try:
        build = json.loads(path.read_text())
        if not isinstance(build, dict):
            raise ValueError("build.json is not an object")
    except Exception as exc:
        print(f"emily-finish: not recording the reason - {path.name} "
              f"will not parse ({type(exc).__name__})")
        return
    if blocked:
        build["draft_blocked"] = blocked
    elif "draft_blocked" not in build:
        return
    else:
        build.pop("draft_blocked")
    path.write_text(json.dumps(build, indent=1) + "\n")


def main():
    if len(sys.argv) < 2:
        print("usage: emily-finish.py <build_dir>")
        return 0

    raw = sys.argv[1]
    agent_dir = ROOT / "agents" / "emily"
    d = next((c for c in ([Path(raw)] if Path(raw).is_absolute() else
                          [agent_dir / raw, agent_dir / "builds" / raw, ROOT / raw])
              if c.is_dir()), None)
    if d is None:
        print(f"emily-finish: no build folder for {raw} - nothing to draft")
        return 0

    try:
        build = json.loads((d / "build.json").read_text())
    except Exception:
        build = {}
    if build.get("printify_product_id"):
        print(f"emily-finish: {d.name} already has Printify product "
              f"{build['printify_product_id']} - leaving it alone")
        return 0

    res = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "emily-printify.py"), "draft", str(d)],
        capture_output=True, text=True, timeout=300,
    )
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    print(out)

    if res.returncode == 0:
        note_draft(d, None)
        print("emily-finish: drafted in Printify, UNPUBLISHED. "
              "Press Publish there when you are happy with it.")
    else:
        note_draft(d, {
            "reason": reason_from(res),
            "exit": res.returncode,
            "when_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        })
        print(f"emily-finish: could not create the Printify draft "
              f"(exit {res.returncode}). The build stays 'ready_local', and "
              "the reason is now\non the build so the gallery can show it "
              "instead of an unexplained LOCAL ONLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
