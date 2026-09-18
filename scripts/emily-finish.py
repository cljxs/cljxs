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

Standard library only.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))


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
        print("emily-finish: drafted in Printify, UNPUBLISHED. "
              "Press Publish there when you are happy with it.")
    elif "required disclosure" in out:
        # Not a last-mile failure. The artwork is fine and the listing is not:
        # Etsy needs the production partner and the AI use stated, and a draft
        # without them is a policy problem waiting to be found by Etsy.
        print("emily-finish: NOT drafted - the listing is missing a disclosure "
              "Etsy requires. Add the line(s) above to the description in "
              f"{d.name}/listing.json and re-run:\n"
              f"  python3 scripts/emily-finish.py {d.name}")
    else:
        print(f"emily-finish: could not create the Printify draft "
              f"(exit {res.returncode}). The build stays 'ready_local' - the "
              "artwork and listing are fine, only the draft is missing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
