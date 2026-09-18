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

# Instruction changes only reach the agent when AGENTS.md is rebuilt from the
# header, which happens in deploy.sh. Pull without it and the agent keeps
# running last week's rules while the repo says otherwise - which is exactly
# how belfort spent a cycle hand-writing a file its instructions had already
# told it to stop writing. So: if the header is newer, rebuild it here.
# Only when AGENTS.md already carries the markers, so no seam has to be
# guessed unattended.
HEADER="$AGENT/_belfort-agents-header.md"
if [ -f "$HEADER" ] && [ "$HEADER" -nt "$AGENT/AGENTS.md" ]; then
  if grep -q "BEGIN belfort-header" "$AGENT/AGENTS.md" 2>/dev/null; then
    echo "belfort-cycle: instructions changed, rebuilding AGENTS.md"
    "$ROOT/scripts/merge-header.sh" belfort || echo "  !! rebuild failed - the agent is running stale instructions"
  else
    echo "  !! $HEADER is newer than AGENTS.md and AGENTS.md has no markers."
    echo "     The agent is running stale instructions. Run once, by hand:"
    echo "       $ROOT/scripts/merge-header.sh belfort"
  fi
fi

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
# And the cycle count at wake. The verifier gets this on argv; signoff had
# no way to know it, so it checked that cycle_count EXISTS while the
# verifier checked that it ADVANCED. Ace did all its work, was told "All
# deliverables present", and was failed for never running mark.
printf '%s\n' "$CYCLES_BEFORE" > "$AGENT/state/.cycle-before"

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT
openclaw agent --agent belfort \
  --message "scheduled cycle" \
  --session-id "wake-belfort-$STARTED" \
  --timeout 600 --json 2>&1 | tee "$LOG"
# The agent's own exit code is deliberately not checked here. It exits 0 for
# "I said some words", which is exactly the thing that cannot be trusted.
# The verifier is the judge.

# A billing or auth failure means no model ran at all. Say so and stop:
# the verifier would otherwise report missing files, which are a
# consequence of that and send the next hour down the wrong path.
if ! python3 "$ROOT/scripts/check-provider-error.py" "$LOG"; then
  exit 1
fi

exec python3 "$ROOT/scripts/belfort-verify.py" "$MEM_BEFORE" "$CYCLES_BEFORE" "$STARTED"
