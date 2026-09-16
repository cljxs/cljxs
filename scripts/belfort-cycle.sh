#!/bin/bash
# Belfort's scheduled cycle: snapshot, wake, verify.
#
# This lives in a script rather than inline in the unit file because the
# ExecStart= it replaces needed nested single quotes, escaped double quotes
# and systemd's own %% escaping all in one line. That is three quoting rules
# stacked on top of each other, and the failure mode is silent: the shell gets
# a mangled command, the snapshot reads empty, and the verifier compares
# against 0 and passes everything.
set -u
ROOT="${ECOSYSTEM_ROOT:-/root/ecosystem}"
AGENT="$ROOT/agents/belfort"

MEM_BEFORE=$(stat -c %s "$AGENT/MEMORY.md" 2>/dev/null || echo 0)
CYCLES_BEFORE=$(python3 - "$AGENT/state/portfolio.json" <<'PY' 2>/dev/null || echo -1
import json, sys
try:
    print(int(json.load(open(sys.argv[1])).get("cycle_count") or 0))
except Exception:
    print(-1)
PY
)
STARTED=$(date +%s)
# signoff.py reads this so it applies the same "written by THIS run" rule
# the verifier does. Without it the two disagreed, and the agent believed
# the one that told it there was nothing left to do.
mkdir -p "$AGENT/state" && printf '%s\n' "$STARTED" > "$AGENT/state/.cycle-started"

openclaw agent --agent belfort \
  --message "scheduled cycle" \
  --session-id "wake-belfort-$STARTED" \
  --timeout 600 --json
# The agent's own exit code is deliberately not checked here. It exits 0 for
# "I said some words", which is exactly the thing that cannot be trusted.
# The verifier is the judge.

exec python3 "$ROOT/scripts/belfort-verify.py" "$MEM_BEFORE" "$CYCLES_BEFORE" "$STARTED"
