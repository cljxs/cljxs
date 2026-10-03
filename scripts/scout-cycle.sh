#!/bin/bash
# Scout's idea run. With no arguments this is the scheduled daily run; with
# arguments, those words become the focus for this run only.
#
#   scripts/scout-cycle.sh                       the daily run
#   scripts/scout-cycle.sh "the Gildan 18500 hoodie"   one-off, focused
#
# The focus is not written to any file and nothing remembers it. Editing the
# instructions to steer one run means remembering to edit them back, and a
# forgotten edit is how an agent ends up running last week's rules - which is
# what preflight.py reports as stale/broken.
#
# This used to live inline in scout-cycle.service: nested single quotes,
# escaped double quotes and systemd %% escaping in one line. ace-cycle.sh was
# extracted from the same mess after it failed silently, and scout was the last
# one left in it.
set -u
ROOT="${ECOSYSTEM_ROOT:-/root/ecosystem}"
AGENT="$ROOT/agents/scout"
LAST="$AGENT/state/last-run.txt"
MEM="$AGENT/MEMORY.md"

IDEAS="python3 $ROOT/scripts/scout-ideas.py"

# Two kinds of focus. Words on the command line steer THIS run only. The
# standing focus - `scout-ideas.py focus tote` - holds until it is cleared,
# is printed at the top of every run, and is enforced by the intake, which
# refuses any other product whatever the model writes.
FOCUS_ON="$*"
STANDING=$($IDEAS focus --word 2>/dev/null)
if [ -z "$FOCUS_ON" ] && [ -n "$STANDING" ]; then
  FOCUS_ON="$STANDING"
  echo "== standing focus: $STANDING  (clear it: scripts/scout-ideas.py focus --clear)"
elif [ -n "$FOCUS_ON" ]; then
  echo "== focused run: $FOCUS_ON"
fi

# Whether to wake the model at all is decided HERE. Scout used to count its
# own pending ideas; it said "4 pending" on a morning when the log held none
# and proposed nothing on the strength of it. Code counts, and a run with
# nothing to do does not wake a model that costs money to say so.
if ! WHY=$($IDEAS should-run); then
  LINE="$(date -u '+%Y-%m-%d %H:%M UTC') | held by code, model not woken - $WHY"
  printf '%s\n' "$LINE" > "$LAST"
  printf '%s\n' "$LINE" >> "$MEM"
  echo "$LINE"
  exit 0
fi

MESSAGE="scheduled idea run"
if [ -n "$FOCUS_ON" ]; then
  MESSAGE="Focused idea run. Every idea you propose must be for: $FOCUS_ON

Propose nothing for any other product type - code refuses it. If you cannot
find enough that fit, propose fewer. A short list that fits is the job;
padding it out with something else is not.

Everything else is unchanged: read the files first, write state/last-run.txt
whatever you decide, and write your ideas to state/drafts.txt, one per line:
phrase | title | product | angle"
fi

# The facts the run starts from - how many ideas are waiting, the focus, and
# every measured phrase it may use - computed now and handed over, so the
# model spends its turns on ideas rather than on counting.
MESSAGE="$MESSAGE

$($IDEAS brief)"

T=$(stat -c %Y "$LAST" 2>/dev/null || echo 0)

cd "$AGENT" || exit 1
openclaw agent --agent scout --message "$MESSAGE" \
  --session-id "wake-scout-$(date +%s)" --timeout 600 --json

U=$(stat -c %Y "$LAST" 2>/dev/null || echo 0)

# "Wrote no ideas" and "did nothing" stay different things. Scout is told to
# propose nothing when ideas are already piling up, and those runs pass - but
# every run writes its one line, so a missing line is a run that fell over.
if [ "$U" -le "$T" ]; then
  echo "SCOUT RECORDED NOTHING: state/last-run.txt not written. Every run writes"
  echo "one line there, including a run that deliberately proposes no ideas."
  exit 1
fi

LINE=$(head -c 400 "$LAST" | tr -d '\n' | sed 's/[[:space:]]\+/ /g')
if [ -z "$LINE" ]; then
  echo "SCOUT RECORDED NOTHING: state/last-run.txt is empty."
  exit 1
fi

# Scout was asked twice, in two wordings, to append to MEMORY.md rather than
# replace it. It replaced it both times and destroyed the log. Appending a line
# is deterministic, so code does it and Scout owns a scratch file it may
# overwrite freely.
printf '%s\n' "$LINE" >> "$MEM"

# Scout proposes into state/proposals.json, which it owns and may overwrite.
# Code folds those into ideas.json, because Scout cleared that file on
# 2026-09-18 after being told in bold to keep every existing entry - the same
# way it destroyed MEMORY.md twice. Nothing here removes an idea.
echo "-- merging proposals"
python3 "$ROOT/scripts/scout-ideas.py" intake
python3 "$ROOT/scripts/scout-ideas.py" merge
echo "logged to MEMORY.md: $LINE"
