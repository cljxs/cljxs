#!/usr/bin/env python3
"""
check-credentials.py — is the credentials file actually usable?

    python3 scripts/check-credentials.py            # emily's, the usual case
    python3 scripts/check-credentials.py scout      # any agent

Prints each key with a character count and four characters from each end -
enough to tell a valid-looking token from an empty one or a mangled paste,
and useless to anyone who sees the screen. It never prints a secret.

Also repairs the paste mistake this file invites: pasting a long token into
nano on a phone often lands it on the line AFTER the "KEY=", which leaves the
key empty and the token orphaned on a line with no "=" that every reader
silently skips. That is a fifteen-minute mystery, so it is fixed here rather
than diagnosed again.

Standard library only.
"""

import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))

# Keys worth commenting on when they are missing, and what breaks without them.
MATTERS = {
    "OPENROUTER_API_KEY": "without it Emily writes a geometric placeholder, not art - the build passes and is not sellable",
    "PRINTIFY_API_TOKEN": "without it Emily builds locally but cannot create the product",
    "PRINTIFY_SHOP_ID": "needed to create the product; run emily-printify.py check to get it",
}


def main():
    agent = sys.argv[1] if len(sys.argv) > 1 else "emily"
    path = ROOT / "agents" / agent / "state" / "credentials.env"

    if not path.is_file():
        example = path.with_suffix(".env.example")
        print(f"{path} does not exist.")
        if example.is_file():
            print(f"\nCreate it with:\n  cp {example} {path} && chmod 600 {path}")
        return 1

    lines = path.read_text().splitlines()
    out, joined = [], 0
    for line in lines:
        prev = out[-1].rstrip() if out else ""
        orphan = line.strip() and "=" not in line and not line.lstrip().startswith("#")
        if prev.endswith("=") and orphan:
            out[-1] = prev + line.strip()
            joined += 1
        else:
            out.append(line)

    if joined:
        path.write_text("\n".join(out) + "\n")
        print(f"Repaired {joined} line(s) where the value had been pasted onto "
              f"the next line instead of after the '='.\n")

    mode = oct(path.stat().st_mode)[-3:]
    if mode != "600":
        print(f"NOTE: permissions are {mode}, not 600. Fix with: chmod 600 {path}\n")

    seen, missing = 0, []
    for line in out:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        seen += 1
        if not v:
            print(f"  {k:<22} EMPTY")
            if k in MATTERS:
                missing.append(k)
        else:
            shown = f"{v[:4]}...{v[-4:]}" if len(v) > 10 else "(short)"
            warn = ""
            if v != v.strip() or " " in v:
                warn = "   <-- contains a space, that is probably a bad paste"
            print(f"  {k:<22} {len(v)} chars  {shown}{warn}")

    if not seen:
        print("  (no keys set at all)")

    if missing:
        print()
        for k in missing:
            print(f"{k} is empty - {MATTERS[k]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
