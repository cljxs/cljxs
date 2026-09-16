#!/bin/bash
# Ace's scheduled cycle: snapshot, wake, verify.
#
# Inline in the unit file this needed nested single quotes, escaped double
# quotes and systemd's %% escaping in one line - three quoting rules stacked,
# failing silently into a snapshot of 0 when any of them goes wrong.
set -u
ROOT="${ECOSYSTEM_ROOT:-/root/ecosystem}"
AGENT="$ROOT/agents/ace"

# A missing bankroll.json is not something the agent can recover from: there is
# nothing to read and nothing to update. Seed it here rather than letting the
# wake burn a model call on a file that is not there.
if [ ! -f "$AGENT/state/bankroll.json" ] && [ -f "$AGENT/state/bankroll.seed.json" ]; then
  cp "$AGENT/state/bankroll.seed.json" "$AGENT/state/bankroll.json"
  echo "ace-cycle: seeded state/bankroll.json (it was missing)"
fi

MEM_BEFORE=$(stat -c %s "$AGENT/MEMORY.md" 2>/dev/null || echo 0)
CYCLES_BEFORE=$(python3 - "$AGENT/state/bankroll.json" <<'PY' 2>/dev/null || echo -1
import json, sys
try:
    print(int(json.load(open(sys.argv[1])).get("cycle_count") or 0))
except Exception:
    print(-1)
PY
)
STARTED=$(date +%s)

openclaw agent --agent ace \
  --message "scheduled cycle" \
  --session-id "wake-ace-$STARTED" \
  --timeout 600 --json
# The agent's exit code is deliberately not checked. It exits 0 for "I said
# some words", which is the thing that cannot be trusted. The verifier judges.

exec python3 "$ROOT/scripts/ace-verify.py" "$MEM_BEFORE" "$CYCLES_BEFORE" "$STARTED"
