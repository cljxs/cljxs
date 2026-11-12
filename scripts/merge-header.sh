#!/bin/bash
# merge-header.sh <agent> [--yes] — rebuild an agent's AGENTS.md from its header.
#
# The install steps say:
#     cat _x-agents-header.md AGENTS.md > .a && mv .a AGENTS.md
# which is correct exactly once. Run it again after editing the header and
# AGENTS.md holds the old header, the new header AND the seeded base. Scout
# reached 388 lines with every rule stated twice, including two copies of
# rules that contradicted each other, and it was injected into the system
# prompt on every single wake.
#
# This rebuilds instead of prepending: AGENTS.md = header + base, every time.
# The header is wrapped in HTML comment markers so later runs find the seam
# exactly. The first run on a file that has no markers has to guess the seam,
# so it shows you the split and asks, unless you pass --yes.
set -eu
AGENT="${1:?usage: merge-header.sh <agent> [--yes]}"
ASSUME_YES="${2:-}"
ROOT="${ECOSYSTEM_ROOT:-/root/ecosystem}"
DIR="$ROOT/agents/$AGENT"
HEADER="$DIR/_${AGENT}-agents-header.md"
# Some agents keep a short tools-first preamble in front of the header. It is
# a separate file on purpose; check it is not simply a copy of the header's
# opening before adding one, which is how Scout ended up stating every rule
# twice in a file injected on every wake.
FIRST="$DIR/_${AGENT}-tools-first.md"
[ -f "$FIRST" ] || FIRST=""
OUT="$DIR/AGENTS.md"

[ -f "$HEADER" ] || { echo "no header at $HEADER" >&2; exit 1; }
[ -f "$OUT" ]    || { echo "no $OUT - register the agent with 'openclaw agents add' first" >&2; exit 1; }

python3 - "$AGENT" "$HEADER" "$OUT" "$ASSUME_YES" "$FIRST" <<'PY'
import sys
from pathlib import Path

agent, header_p, out_p, yes = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
first_p = Path(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None
BEGIN = f"<!-- BEGIN {agent}-header — generated from _{agent}-agents-header.md, edit that file -->"
END   = f"<!-- END {agent}-header -->"

header = header_p.read_text(encoding="utf-8").strip("\n")
if first_p:
    header = first_p.read_text(encoding="utf-8").strip("\n") + "\n\n" + header
current = out_p.read_text(encoding="utf-8")

if BEGIN in current and END in current:
    # The normal path once this script has run once: an exact seam.
    base = current.split(END, 1)[1].lstrip("\n")
    print(f"  found the markers - replacing the header in place")
else:
    # No markers. The file is either a clean openclaw seed or a hand-merged
    # header sitting on top of one. Both start with a top-level heading, so
    # the seam is the (H+1)-th '# ' line, where H is how many the header has.
    h_count = sum(1 for ln in header.splitlines() if ln.startswith("# "))
    lines = current.splitlines(keepends=True)
    tops = [i for i, ln in enumerate(lines) if ln.startswith("# ")]
    if len(tops) <= h_count:
        base, seam = current.lstrip("\n"), None      # looks like a clean seed
    else:
        seam = tops[h_count]
        base = "".join(lines[seam:])
    if seam is None:
        print(f"  no markers and no embedded header - treating all "
              f"{len(lines)} lines as the seeded base")
    else:
        print(f"  no markers. Guessing the seam at line {seam + 1}: dropping the "
              f"{seam} lines above it as an old copy of the header, keeping "
              f"{len(lines) - seam} lines as the base.")
        print("  the base would start:")
        for ln in base.splitlines()[:4]:
            print(f"    | {ln}")
        if yes != "--yes":
            print(f"\n  If that first line is openclaw's seeded AGENTS.md and not part of\n"
                  f"  {agent}'s own instructions, re-run with --yes. If it is wrong, the\n"
                  f"  seam is wrong - say so rather than letting it cut.", file=sys.stderr)
            sys.exit(2)

merged = f"{BEGIN}\n{header}\n{END}\n\n{base.rstrip()}\n"
out_p.write_text(merged, encoding="utf-8")
hl, bl, ml = len(header.splitlines()), len(base.strip().splitlines()), len(merged.splitlines())
print(f"{agent}: AGENTS.md rebuilt - {hl} lines of header + {bl} of base = {ml} lines")
PY
