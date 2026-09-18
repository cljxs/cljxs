#!/usr/bin/env python3
"""
disclosures.py — the disclosures every Etsy listing here must carry.

    python3 scripts/disclosures.py show      what is required, and where it lives
    python3 scripts/disclosures.py check <build_dir>

Two things Etsy requires of this shop, both of which currently depend on
someone remembering:

  * that a production partner makes the item (Printify), and
  * that AI was used in creating the design.

Both are deterministic text. Forgetting one is reportedly treated in the same
category as selling a prohibited item, and it would be found by Etsy rather
than by us - so `draft` refuses a listing that is missing either, the same way
knockout refuses art it cannot cut.

THE WORDING IS YOURS. It lives in agents/emily/state/disclosures.json, not in
this file, because it is a legal statement about your shop and no script
should freeze one on your behalf. The defaults below are a starting point to
edit, not advice - read Etsy's Creativity Standards and Production Partner
pages and word these yourself.

The check is substring containment, so rewording the file reworks the check
with it: there is one copy of each sentence and the listing must carry it.

Standard library only.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ECOSYSTEM_ROOT", Path(__file__).resolve().parent.parent))
FILE = ROOT / "agents" / "emily" / "state" / "disclosures.json"

# A starting point. Edit the file, not this.
DEFAULTS = {
    "production_partner": {
        "required": True,
        "text": "Made for me by my production partner, Printify.",
        "why": "Etsy requires disclosing any third party that produces your "
               "physical items. Printify prints and ships; you are the designer.",
    },
    "ai": {
        "required": True,
        "text": "This design was created with the help of AI image generation, "
                "directed and selected by me.",
        "why": "Etsy requires disclosing AI use in the listing, and the item "
               "must be based on your own creative direction.",
    },
}


def load():
    """The disclosures, writing the defaults out on first use."""
    if not FILE.is_file():
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(DEFAULTS, indent=1) + "\n")
        print(f"wrote starting disclosures to {FILE} - read them and make them "
              f"yours before you list anything.", file=sys.stderr)
    return json.loads(FILE.read_text())


def missing_from(description, rules=None):
    """Which required disclosures are absent from this description."""
    text = " ".join(str(description or "").split()).lower()
    out = []
    for name, rule in (rules or load()).items():
        if not rule.get("required", True):
            continue
        want = " ".join(str(rule.get("text") or "").split()).lower()
        if want and want not in text:
            out.append((name, rule))
    return out


def report(description, rules=None):
    """(ok, printable complaint)."""
    gaps = missing_from(description, rules)
    if not gaps:
        return True, ""
    lines = [f"the listing is missing {len(gaps)} required disclosure(s):"]
    for name, rule in gaps:
        lines.append(f"\n  {name}")
        lines.append(f"    add this line to the description:")
        lines.append(f"      {rule.get('text')}")
        if rule.get("why"):
            lines.append(f"    why: {rule['why']}")
    lines.append(f"\n  The wording is yours - edit {FILE.relative_to(ROOT)} "
                 f"and this check follows it.")
    return False, "\n".join(lines)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "show":
        rules = load()
        print(f"{FILE}\n")
        for name, rule in rules.items():
            flag = "required" if rule.get("required", True) else "optional"
            print(f"  {name}  ({flag})")
            print(f"    {rule.get('text')}")
        return 0
    if cmd == "check":
        if len(sys.argv) < 3:
            print("usage: disclosures.py check <build_dir>", file=sys.stderr)
            return 2
        d = Path(sys.argv[2])
        try:
            listing = json.loads((d / "listing.json").read_text())
        except Exception as exc:
            print(f"cannot read {d}/listing.json: {exc}", file=sys.stderr)
            return 2
        ok, complaint = report(listing.get("description"))
        if ok:
            print("all required disclosures present")
            return 0
        print(f"disclosures: {complaint}", file=sys.stderr)
        return 1
    print("usage: disclosures.py show|check <build_dir>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
