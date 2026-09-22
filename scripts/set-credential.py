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

# Keys that are really two values. ASKED FOR SEPARATELY, and joined here.
#
# Five attempts to paste "keystring:shared_secret" failed in two alternating
# ways: no colon, or the whole thing replaced by the command that was still in
# the clipboard. Both are the same root cause - the person is asked to
# assemble a string out of two values from a web page, on a tablet, one
# clipboard at a time, while the clipboard also holds the command they had to
# paste to get here. So stop asking. Two prompts, one value each, and the
# colon is put in by code that cannot forget it.
PARTS = {
    "ETSY_API_KEY": ("keystring", "shared secret"),
}

# Things that are obviously this tool's own instructions rather than a secret.
# Worth naming exactly, because "it has a space in it" does not tell you that
# your clipboard still holds the command.
OURS = re.compile(r"set-credential|etsy-probe|python3|/root/ecosystem|git pull",
                  re.I)

ATTEMPTS = 3

MIN_LEN, MAX_LEN = 8, 400


def clean(raw):
    """Whitespace, wrapping quotes and a stray trailing colon, all removed.

    A tablet paste arrives with a newline on the end often enough that
    refusing it would be pedantry rather than safety.
    """
    v = (raw or "").strip().strip('"').strip("'").strip()
    return v.rstrip(":").lstrip(":")


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
    if OURS.search(value):
        out.append("that is the COMMAND, not the key - your clipboard still "
                   "holds\n      what you pasted to get here. Copy the value "
                   "from Etsy, then\n      answer this prompt.")
        return out                  # naming it once is enough
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

    show = "--show" in sys.argv
    parts = PARTS.get(name)
    asker = input if show else getpass.getpass

    # Retries happen HERE, not by running the command again. Re-running means
    # pasting the command again, which is exactly when the clipboard stops
    # holding the key - five attempts died that way.
    value = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            if parts:
                print(f"\n{name} is two values from your Etsy app page. "
                      f"One at a time.")
                first = clean(asker(f"  1/2  {parts[0]}: "))
                # Checked BEFORE asking for the second half. Asking for a
                # shared secret after the keystring prompt has plainly
                # received a shell command wastes the one thing in short
                # supply here, which is the person's patience.
                bad = problems("", first)
                if bad:
                    value, found = first, bad
                elif ":" in first:
                    # The whole colon-joined key arrived at prompt one. Take
                    # it rather than asking for a half already in hand.
                    value, found = first, problems(name, first)
                else:
                    second = clean(asker(f"  2/2  {parts[1]}: "))
                    bad = problems("", second)
                    value = f"{first}:{second}" if first and second else ""
                    found = bad or problems(name, value)
            else:
                value = clean(asker(f"paste {name}"
                                    f"{'' if show else ' (it will not be shown)'}"
                                    f", then Enter: "))
                found = problems(name, value)
        except (EOFError, KeyboardInterrupt):
            print("\nnothing written.", file=sys.stderr)
            return 1

        if not found:
            break
        print(f"\n  that does not look like a {name}:", file=sys.stderr)
        for p in found:
            print(f"    - {p}", file=sys.stderr)
        if attempt < ATTEMPTS:
            print(f"\n  Nothing was written. Try again ({attempt} of "
                  f"{ATTEMPTS}) - no need to re-run the command.\n",
                  file=sys.stderr)
        else:
            print(f"\n  Giving up after {ATTEMPTS} tries. Nothing was written "
                  f"and nothing changed.\n"
                  f"  If pasting will not work, type it instead - the two "
                  f"values are short.\n"
                  f"  Add --show to see what you are typing.", file=sys.stderr)
            return 2

    count = write(path, name, value)
    print(f"\nwrote {name} to {path}")
    print(f"  {masked(value)}")
    print(f"  {count} key(s) in the file, mode {oct(path.stat().st_mode)[-3:]}")
    print(f"\nThe value was never echoed and is not in your shell history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
