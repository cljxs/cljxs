#!/bin/bash
# set-agent-tools.sh <agent> [--apply] — cut an agent's tool surface.
#
# Ace was carrying ~60 tools on every wake: 40,788 characters of JSON schema,
# including music_generate, video_generate, image_generate, notion, browser
# automation and a dozen session-management tools. It is a betting analyst. It
# reads four kinds of file, writes four, and runs one script.
#
# That is fuel on every call, and it is noise in a small model's context: the
# run that read 19 files and never wrote one had all of that in front of it.
#
# The config path differs between OpenClaw versions - the docs use
# `agents.entries.<name>.tools`, this project's own install notes use
# `agents.list[N].model`. So this LOOKS first and tells you what it found
# rather than guessing, and it changes nothing without --apply.
set -u
AGENT="${1:?usage: set-agent-tools.sh <agent> [--apply]}"
APPLY="${2:-}"

# read  - the data and state files
# write - bankroll.json, ledger.json, the report, MEMORY.md
# edit  - appending one line to MEMORY.md
# exec / process - running signoff.py and the helper scripts
#
# apply_patch is kept because it is how some builds perform an edit; dropping
# a tool the agent needs costs a whole cycle, and six tools is already a 90%
# cut. Trim further once a few cycles have passed cleanly.
KEEP="read,write,edit,apply_patch,exec,process"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

command -v openclaw >/dev/null || { echo "openclaw not on PATH" >&2; exit 2; }

say "what openclaw reports now"
echo "--- openclaw agents list ---"
openclaw agents list 2>&1 | sed 's/^/  /'

PATHS=(
  "agents.entries.$AGENT.tools"
  "agents.$AGENT.tools"
)
FOUND=""
SETTABLE=""
say "looking for the tools key"
for k in "${PATHS[@]}"; do
  out=$(openclaw config get "$k" 2>&1)
  rc=$?
  printf '  %-34s ' "$k"
  if [ $rc -eq 0 ]; then
    echo "EXISTS -> ${out:-(empty)}"
    FOUND="$k"
    break
  fi
  # openclaw tells "valid but unset" apart from "unknown path", and only the
  # second means the key is unsupported. This used to lump them together and
  # report that the two cases "look identical from here" - they do not, and
  # believing they did left the tool surface untrimmed for a week.
  case "$out" in
    *"valid but unset"*)
      echo "SETTABLE (in the schema, no value yet)"
      FOUND="$k"
      SETTABLE=yes
      break ;;
    *) echo "no (${out:0:60})" ;;
  esac
done

if [ -z "$FOUND" ]; then
  # The entries-style key does not exist yet. On this schema it is usually
  # still settable - an absent key and an unsupported one look the same to
  # `config get`, which is exactly why this stops rather than guessing.
  FOUND="agents.entries.$AGENT.tools"
  say "no existing tools key"
  cat <<EOF
  Neither path is present. That means one of two things, and they look
  identical from here:

    (a) the key is supported and simply unset - setting it will work
    (b) this OpenClaw uses the older agents.list[N] shape and the command
        below will be rejected

  Run this to see which, then re-run with --apply if it prints a usable shape:

    openclaw config get agents | head -40
    openclaw agents list --bindings
EOF
fi

CMD_ALLOW="openclaw config set '$FOUND.allow' '[\"${KEEP//,/\",\"}\"]'"

say "the change"
echo "  keep only: $KEEP"
echo
echo "  $CMD_ALLOW"

if [ "$APPLY" != "--apply" ]; then
  echo
  echo "Nothing was changed. Re-run with --apply to do it:"
  echo "  $0 $AGENT --apply"
  exit 0
fi

say "applying"
if eval "$CMD_ALLOW"; then
  echo "  set ok"
else
  echo "  REFUSED - this OpenClaw does not accept that key. Nothing changed." >&2
  echo "  Send me the output of: openclaw config get agents | head -40" >&2
  exit 1
fi

say "reading it back"
openclaw config get "$FOUND" 2>&1 | sed 's/^/  /'
echo
echo "Restart the gateway so the next wake picks it up:"
echo "  openclaw gateway restart"
echo
echo "Then confirm on the next cycle - last-run.py prints the tool count:"
echo "  /root/ecosystem/scripts/last-run.py $AGENT"
