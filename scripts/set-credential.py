#!/usr/bin/env python3
"""
set-credential.py — put a secret into credentials.env without an editor.

    python3 scripts/set-credential.py                    # asks for each one it needs
    python3 scripts/set-credential.py PRINTIFY_SHOP_ID   # just that one
    python3 scripts/set-credential.py --agent scout OPENROUTER_API_KEY

Why this exists: nano is hard to escape on a phone keyboard, a flattened
heredoc silently does nothing, and a value typed as a shell argument lands in
your history. This prompts for the value, strips the stray whitespace a paste
brings with it, writes it on the correct line, and confirms with a character
count and four characters from each end. It never echoes the secret and never
prints it back.

Standard library only.
"""

import getpass
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))

WANTED = {
    "OPENROUTER_API_KEY": "your existing OpenRouter key - the same one the other agents use",
    "PRINTIFY_API_TOKEN": "from printify.com/app/account/api",
    "PRINTIFY_SHOP_ID": "a number - run emily-printify.py check to get it (not a secret)",
}
# The shop id is an account number, not a credential, so show it as typed.
NOT_SECRET = {"PRINTIFY_SHOP_ID"}


def mask(v):
    return f"{len(v)} chars  {v[:4]}...{v[-4:]}" if len(v) > 10 else f"{len(v)} chars"


def set_key(path, key, value):
    lines = path.read_text().splitlines() if path.is_file() else []
    hit = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            hit = True
            break
    if not hit:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def main():
    args = [a for a in sys.argv[1:]]
    agent = "emily"
    if "--agent" in args:
        i = args.index("--agent")
        agent = args[i + 1]
        del args[i:i + 2]

    path = ROOT / "agents" / agent / "state" / "credentials.env"
    if not path.is_file():
        example = path.parent / "credentials.env.example"
        if example.is_file():
            path.write_text(example.read_text())
            os.chmod(path, 0o600)
            print(f"created {path} from the example\n")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
            os.chmod(path, 0o600)

    keys = args or list(WANTED)
    print(f"Writing into {path}")
    print("Paste the value and press Return. Leave it blank to skip.\n")

    for key in keys:
        hint = WANTED.get(key, "")
        label = f"{key}" + (f"  ({hint})" if hint else "")
        print(label)
        try:
            raw = (input("  value: ") if key in NOT_SECRET
                   else getpass.getpass("  value (hidden): "))
        except (EOFError, KeyboardInterrupt):
            print("\nstopped.")
            return 1
        # A paste off a phone arrives with stray spaces, newlines, and
        # sometimes the quotes from a config example.
        v = raw.strip().strip('"').strip("'").strip()
        if not v:
            print("  skipped\n")
            continue
        if " " in v:
            print("  that value contains a space - almost certainly a bad paste. Not saved.\n")
            continue
        set_key(path, key, v)
        print(f"  saved: {mask(v)}\n")

    print("Now check it:  python3 scripts/check-credentials.py"
          + (f" {agent}" if agent != "emily" else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
