#!/bin/bash
# Paul's cycle: one business, once a day. With "dry" as the argument - or
# after `paul.py dry-next` - it is a dry cycle: the same work, built locally
# only, nothing deployed and nothing recorded as contacted.
#
#   scripts/paul-cycle.sh          the scheduled run (paul-cycle.timer)
#   scripts/paul-cycle.sh dry      a dry cycle, by hand
#
# The model writes four things and code does the rest - see paul.py's
# docstring for why. This file only puts the steps in order.
set -u
ROOT="${ECOSYSTEM_ROOT:-/root/ecosystem}"
AGENT="$ROOT/agents/paul"
PAUL="python3 $ROOT/scripts/paul.py"

DRY=""
if [ "${1:-}" = "dry" ] || $PAUL dry-next --take; then
  DRY="--dry"
  echo "== dry cycle: built locally only, nothing deployed or recorded as contacted"
fi

# Free housekeeping first, whatever happens next: previews past their 30
# days, or marked declined, come down. No model is involved.
$PAUL teardown || echo "!! teardown had problems - see above"

# Whether to wake the model at all is decided HERE, by code: missing sender
# details, no go-live yet, pitches piling up unsent, or not enough of today's
# AI allowance. A held run writes its line and costs nothing.
if ! $PAUL should-run $DRY --record; then
  exit 0
fi

FACTS=$($PAUL brief $DRY) || { echo "PAUL: brief failed - not waking the model"; exit 1; }
if [ -n "$DRY" ]; then MESSAGE="dry cycle"; else MESSAGE="scheduled cycle"; fi
MESSAGE="$MESSAGE

$FACTS"

cd "$AGENT" || exit 1
# 1800s: finding, checking and building one real site is the longest job any
# agent here does. The unit's TimeoutStartSec leaves room for deploy and
# verification after it.
openclaw agent --agent paul --message "$MESSAGE" \
  --session-id "wake-paul-$(date +%s)" --timeout 1800 --json

# Code files what the model left: checks it, deploys and verifies the preview
# (live), writes the records, the report and the MEMORY line. Its exit code is
# the cycle's.
$PAUL finish $DRY
