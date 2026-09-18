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

# Instruction changes only reach the agent when AGENTS.md is rebuilt from the
# header, which happens in deploy.sh. Pull without it and the agent keeps
# running last week's rules while the repo says otherwise - which is exactly
# how ace spent a cycle hand-writing a file its instructions had already
# told it to stop writing. So: if the header is newer, rebuild it here.
# Only when AGENTS.md already carries the markers, so no seam has to be
# guessed unattended.
HEADER="$AGENT/_ace-agents-header.md"
if [ -f "$HEADER" ] && [ "$HEADER" -nt "$AGENT/AGENTS.md" ]; then
  if grep -q "BEGIN ace-header" "$AGENT/AGENTS.md" 2>/dev/null; then
    echo "ace-cycle: instructions changed, rebuilding AGENTS.md"
    "$ROOT/scripts/merge-header.sh" ace || echo "  !! rebuild failed - the agent is running stale instructions"
  else
    echo "  !! $HEADER is newer than AGENTS.md and AGENTS.md has no markers."
    echo "     The agent is running stale instructions. Run once, by hand:"
    echo "       $ROOT/scripts/merge-header.sh ace"
  fi
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
# signoff.py reads this so it applies the same "written by THIS run" rule
# the verifier does. Without it the two disagreed, and the agent believed
# the one that told it there was nothing left to do.
mkdir -p "$AGENT/state" && printf '%s\n' "$STARTED" > "$AGENT/state/.cycle-started"
# And the cycle count at wake. The verifier gets this on argv; signoff had
# no way to know it, so it checked that cycle_count EXISTS while the
# verifier checked that it ADVANCED. Ace did all its work, was told "All
# deliverables present", and was failed for never running mark.
printf '%s\n' "$CYCLES_BEFORE" > "$AGENT/state/.cycle-before"

# "scheduled cycle" was the entire message. Handed that, and a toolset that
# includes progress_card and sessions_spawn, the agent posted a progress card
# listing three tasks, spawned a child session, announced that the cycle "has
# started" and that you could "follow the progress as the tasks are
# completed", and exited after 11 seconds. Nothing was ever going to complete:
# the spawn was fire-and-forget.
#
# So the message says what a cycle is. No delegating, no starting - doing.
LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT
openclaw agent --agent ace \
  --message "Scheduled cycle. Do the whole cycle yourself, now, in this session.

Do NOT spawn a session, delegate to a subagent, open a dashboard or post a
progress card. There is nobody watching this run and nothing will pick up work
you hand off - a spawned session's output is discarded. Announcing that the
cycle has started is not doing it.

Work through the steps in AGENTS.md in order, writing each file as you go,
and finish by running: python3 ../../scripts/signoff.py ace" \
  --session-id "wake-ace-$STARTED" \
  --timeout 300 --json 2>&1 | tee "$LOG"
# 300, not 600. A healthy cycle is 20-60 seconds. One runaway ran 153 seconds
# and cost $0.19 - forty times normal - rewriting the same file 43 times. The
# cause of that loop is fixed, but a timeout is the only bound that does not
# depend on the model noticing it is stuck.
# The agent's exit code is deliberately not checked. It exits 0 for "I said
# some words", which is the thing that cannot be trusted. The verifier judges.

# A billing or auth failure means no model ran at all. Say so and stop:
# the verifier would otherwise report missing files, which are a
# consequence of that and send the next hour down the wrong path.
if ! python3 "$ROOT/scripts/check-provider-error.py" "$LOG"; then
  exit 1
fi

exec python3 "$ROOT/scripts/ace-verify.py" "$MEM_BEFORE" "$CYCLES_BEFORE" "$STARTED"
