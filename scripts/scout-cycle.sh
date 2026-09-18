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
IDEAS="$AGENT/state/ideas.json"
MEM="$AGENT/MEMORY.md"

MESSAGE="scheduled idea run"
if [ "$#" -gt 0 ]; then
  MESSAGE="Focused idea run. Every idea you propose must be for: $*

Propose nothing for any other product type this run - not stickers, not mugs,
not prints. If you cannot find enough that fit, propose fewer. A short list
that fits is the job; padding it out with something else is not.

Set \"product\" on each idea to the product type it is for, so approving one
drafts against the right catalogue entry.

Everything else is unchanged: read the files first, write state/last-run.txt
whatever you decide, and append to state/ideas.json rather than replacing it."
  echo "== focused run: $*"
fi

T=$(stat -c %Y "$LAST" 2>/dev/null || echo 0)
I=$(stat -c %Y "$IDEAS" 2>/dev/null || echo 0)

cd "$AGENT" || exit 1
openclaw agent --agent scout --message "$MESSAGE" \
  --session-id "wake-scout-$(date +%s)" --timeout 600 --json

U=$(stat -c %Y "$LAST" 2>/dev/null || echo 0)
J=$(stat -c %Y "$IDEAS" 2>/dev/null || echo 0)

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

if [ "$J" -gt "$I" ]; then
  echo "scout wrote ideas.json; logged to MEMORY.md: $LINE"
else
  echo "scout proposed no new ideas - a pass; logged to MEMORY.md: $LINE"
fi
