#!/usr/bin/env python3
"""
emily-verify.py — did the build actually produce a product?

Run by the dispatcher after Emily completes a task. Emily marks her own task
done through the API, which means "complete" only ever meant "she said so".
Four agents in this ecosystem have completed a run having written nothing;
Emily is the one that spends money on image generation while doing it, so the
claim gets checked.

    emily-verify.py <build_dir>       # e.g. agents/emily/builds/cosy-cabin

Exits non-zero, with the reason on stdout, if the build is not real.
Standard library only.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))

# A PNG smaller than this is not artwork. emily-assets.py never writes a blank
# file, so anything this small means something else produced it.
MIN_IMAGE_BYTES = 2000


def main():
    if len(sys.argv) < 2:
        print("usage: emily-verify.py <build_dir>")
        return 2

    raw = sys.argv[1]
    problems, notes = [], []

    # build_dir in the task payload is "builds/<slug>" - relative to EMILY'S
    # WORKSPACE, not to the ecosystem root. Resolving it against the root
    # looked for /root/ecosystem/builds/<slug> and called a finished build
    # missing. Try the bases that could be meant, in order of likelihood.
    agent_dir = ROOT / "agents" / "emily"
    candidates = [Path(raw)] if Path(raw).is_absolute() else [
        agent_dir / raw,                 # builds/<slug> - what the payload holds
        agent_dir / "builds" / raw,      # a bare slug
        ROOT / raw,                      # already fully qualified from the root
    ]
    d = next((c for c in candidates if c.is_dir()), None)

    if d is None:
        print("emily-verify: FAILED - no build folder found. "
              "The task was marked complete but nothing was created.")
        print("  looked in:")
        for c in candidates:
            print(f"    {c}")
        return 1

    design = d / "design.png"
    if not design.is_file():
        problems.append("no design.png - there is no artwork")
    elif design.stat().st_size < MIN_IMAGE_BYTES:
        problems.append(f"design.png is {design.stat().st_size} bytes - too small to be artwork")
    else:
        notes.append(f"design.png {design.stat().st_size // 1024} KB")

    listing = d / "listing.json"
    if not listing.is_file():
        problems.append("no listing.json - no title, tags or price")
    else:
        try:
            j = json.loads(listing.read_text())
            missing = [k for k in ("title", "description", "tags") if not j.get(k)]
            if missing:
                problems.append(f"listing.json is missing {', '.join(missing)}")
            tags = j.get("tags") or []
            if len(tags) > 13:
                problems.append(f"listing.json has {len(tags)} tags - Etsy allows 13")
            long_tags = [t for t in tags if len(str(t)) > 20]
            if long_tags:
                problems.append(f"tags over 20 characters: {', '.join(map(str, long_tags[:3]))}")
            if not problems:
                notes.append(f"listing '{str(j.get('title'))[:40]}' - {len(tags)} tags")
        except Exception as exc:
            problems.append(f"listing.json will not parse ({type(exc).__name__})")

    build = d / "build.json"
    if not build.is_file():
        problems.append("no build.json - Emily's own account of what she made")
    else:
        try:
            b = json.loads(build.read_text())
            status = str(b.get("status") or "")
            if not status:
                problems.append("build.json has no status")
            elif status not in ("ready_local", "ready_for_review", "published"):
                notes.append(f"status '{status}' (expected ready_local or ready_for_review)")
            else:
                notes.append(f"status {status}")
            mode = str(b.get("art_mode") or b.get("mode") or "").lower()
            if "placeholder" in mode:
                # Not a failure. A placeholder build is a legitimate outcome
                # when no image key is set - it just is not sellable, and the
                # user needs to be told rather than find out after publishing.
                notes.append("PLACEHOLDER ART - not sellable, check OPENROUTER_API_KEY")
        except Exception as exc:
            problems.append(f"build.json will not parse ({type(exc).__name__})")

    if problems:
        print(f"emily-verify: FAILED - {d.name} is not a finished build")
        for p in problems:
            print(f"  - {p}")
        print("\nThe task is marked failed on purpose. Emily reported success; "
              "the files say otherwise, and a build nobody checks is how a "
              "placeholder ends up on sale.")
        return 1

    print(f"emily-verify: PASS - {d.name}")
    for n in notes:
        print(f"  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
