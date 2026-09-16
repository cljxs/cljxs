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

# "scheduled cycle" was the entire message. Handed that, and a toolset that
# includes progress_card and sessions_spawn, the agent posted a progress card
# listing three tasks, spawned a child session, announced that the cycle "has
# started" and that you could "follow the progress as the tasks are
# completed", and exited after 11 seconds. Nothing was ever going to complete:
# the spawn was fire-and-forget.
#
# So the message says what a cycle is. No delegating, no starting - doing.
openclaw agent --agent ace \
  --message "Scheduled cycle. Do the whole cycle yourself, now, in this session.

Do NOT spawn a session, delegate to a subagent, open a dashboard or post a
progress card. There is nobody watching this run and nothing will pick up work
you hand off - a spawned session's output is discarded. Announcing that the
cycle has started is not doing it.

Work through the steps in AGENTS.md in order, writing each file as you go,
and finish by running: python3 ../../scripts/signoff.py ace" \
  --session-id "wake-ace-$STARTED" \
  --timeout 600 --json
# The agent's exit code is deliberately not checked. It exits 0 for "I said
# some words", which is the thing that cannot be trusted. The verifier judges.

exec python3 "$ROOT/scripts/ace-verify.py" "$MEM_BEFORE" "$CYCLES_BEFORE" "$STARTED"
