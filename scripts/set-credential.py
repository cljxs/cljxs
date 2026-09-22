#!/usr/bin/env python3
"""
set-credential.py — put a secret in an agent's credentials.env, safely.

    set-credential.py scout ETSY_API_KEY
    set-credential.py emily PRINTIFY_API_TOKEN

WHY THIS EXISTS. Three attempts to get one key onto this droplet produced:
a credentials.env full of malformed JSON's cousin, three nano .save files
holding a live token, and a 298-byte "key" that was the key plus the NEXT
command pasted after it - because the terminal joins a paste that has no
trailing newline onto whatever follows. The user's key then appeared in a
screenshot, because the prompt echoed it.

So: no editor, nothing to substitute into a command line, the value never
echoed, and the file validated before it is written rather than by whatever
fails first afterwards.

WHAT IT REFUSES. A value with a space in it, a newline, or a shell operator
is not a credential - it is a paste accident, and every one of those has
happened here. The check is deliberately blunt: refusing a real key costs one
retry, accepting a mangled one costs an afternoon of debugging a 403.

Existing keys in the file are kept. A credentials.env holds more than one
secret, and rewriting the whole file to change one line is how the other ones
disappear.

Standard library only.
"""

import getpass
import importlib.util
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
SCRIPTS = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("emily_assets", SCRIPTS / "emily-assets.py")
ea = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ea)

# What a mangled paste looks like. Not an exhaustive grammar of secrets - a
# list of things that are definitely NOT one.
NONSENSE = [
    (re.compile(r"\s"), "it has a space or newline in it - two things got pasted at once"),
    (re.compile(r"&&|\|\||;\s|\$\(|`"), "it contains a shell operator - a command got pasted in"),
    (re.compile(r"^-"), "it starts with a dash - that is a flag, not a value"),
]

# Keys we know the shape of. Everything else is accepted on the general checks
# alone, because guessing at a format we have not seen is how a real key gets
# refused at midnight.
SHAPES = {
    "ETSY_API_KEY": (re.compile(r"^[A-Za-z0-9]{8,64}:[A-Za-z0-9]{8,64}$"),
                     "Etsy wants BOTH values joined by a colon:\n"
                     "      keystring:shared_secret\n"
                     "    Both are on your app's page in the Etsy developer portal."),
}

MIN_LEN, MAX_LEN = 8, 400


def problems(name, value):
    """Everything wrong with this value, as a list of sentences."""
    out = []
    if not value:
        return ["it is empty"]
    if len(value) < MIN_LEN:
        out.append(f"it is only {len(value)} characters - that is not a credential")
    if len(value) > MAX_LEN:
        out.append(f"it is {len(value)} characters - something else got pasted "
                   f"in with it")
    for pattern, why in NONSENSE:
        if pattern.search(value):
            out.append(why)
    shape = SHAPES.get(name)
    if shape and not out and not shape[0].match(value):
        out.append(shape[1])
    return out


def masked(value):
    """Enough to spot a truncated paste, useless to anyone reading it."""
    def half(s):
        return f"{s[:3]}…{s[-2:]}" if len(s) > 6 else "…"
    if ":" in value:
        a, _, b = value.partition(":")
        return f"{half(a)}:{half(b)}  ({len(a)} + {len(b)} chars)"
    return f"{half(value)}  ({len(value)} chars)"


def write(path, name, value):
    """Set one key, keep the others, mode 600 before anything is in it."""
    existing = dict(ea.read_env_file(path))
    existing[name] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created empty with the right mode FIRST. Writing then chmod-ing leaves a
    # window where the secret is on disk world-readable.
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    path.write_text("".join(f"{k}={v}\n" for k, v in sorted(existing.items())))
    return len(existing)


def main():
    if len(sys.argv) < 3:
        print("usage: set-credential.py <agent> <KEY_NAME>\n"
              "  e.g. set-credential.py scout ETSY_API_KEY", file=sys.stderr)
        return 2
    agent, name = sys.argv[1], sys.argv[2]
    path = ROOT / "agents" / agent / "state" / "credentials.env"
    if not (ROOT / "agents" / agent).is_dir():
        have = sorted(p.name for p in (ROOT / "agents").iterdir() if p.is_dir()) \
            if (ROOT / "agents").is_dir() else []
        print(f"no agent called '{agent}'. There is: {', '.join(have) or 'none'}",
              file=sys.stderr)
        return 1

    # Hidden, because the last one ended up in a screenshot.
    try:
        value = getpass.getpass(f"paste {name} (it will not be shown), then Enter: ")
    except (EOFError, KeyboardInterrupt):
        print("\nnothing written.", file=sys.stderr)
        return 1
    value = value.strip()

    found = problems(name, value)
    if found:
        print(f"\nnot written - that does not look like a {name}:", file=sys.stderr)
        for p in found:
            print(f"    {p}", file=sys.stderr)
        print(f"\n  Nothing was saved and nothing was changed. Run it again.",
              file=sys.stderr)
        return 2

    count = write(path, name, value)
    print(f"\nwrote {name} to {path}")
    print(f"  {masked(value)}")
    print(f"  {count} key(s) in the file, mode {oct(path.stat().st_mode)[-3:]}")
    print(f"\nThe value was never echoed and is not in your shell history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
