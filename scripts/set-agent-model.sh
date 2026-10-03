#!/bin/bash
# set-agent-model.sh <agent> <model-slug> [--apply] — change an agent's model.
#
# Discovery first, like set-agent-tools.sh, because this project's install
# notes use `agents.list[N].model` while the running config uses
# `agents.entries.<name>.model.primary`, and guessing a config path has already
# cost a round trip once. Nothing changes without --apply.
#
# The slug is checked against OpenRouter's own catalogue before it is set. A
# wrong slug does not fail loudly - the agent simply stops working at its next
# wake, hours later, and reports "model not found". That has happened here.
set -u
AGENT="${1:?usage: set-agent-model.sh <agent> <model-slug> [--apply]}"
MODEL="${2:?usage: set-agent-model.sh <agent> <model-slug> [--apply]}"
APPLY="${3:-}"
KEY="agents.entries.$AGENT.model.primary"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
command -v openclaw >/dev/null || { echo "openclaw not on PATH" >&2; exit 2; }

say "checking the slug exists"
BARE="${MODEL#openrouter/}"
if curl -fsSL --max-time 30 https://openrouter.ai/api/v1/models -o /tmp/or-models.json 2>/dev/null; then
  if python3 - "$BARE" <<'PY'
import json, sys
ids = {m["id"] for m in json.load(open("/tmp/or-models.json"))["data"]}
want = sys.argv[1]
if want in ids:
    print(f"  {want} is in OpenRouter's catalogue")
    raise SystemExit(0)
near = sorted(i for i in ids if want.split("/")[-1][:8] in i)[:6]
print(f"  {want} is NOT in OpenRouter's catalogue.", file=sys.stderr)
if near:
    print("  did you mean:", file=sys.stderr)
    for n in near:
        print(f"    {n}", file=sys.stderr)
raise SystemExit(1)
PY
  then :; else exit 1; fi
else
  echo "  could not reach OpenRouter to check - continuing without verifying"
fi

say "what the agent runs now"
openclaw config get "agents.entries.$AGENT.model" 2>&1 | sed 's/^/  /'

say "the change"
echo "  $KEY  ->  $MODEL"
if [ "$APPLY" != "--apply" ]; then
  echo
  echo "Nothing was changed. Re-run with --apply:"
  echo "  $0 $AGENT $MODEL --apply"
  exit 0
fi

say "applying"
if openclaw config set "$KEY" "$MODEL"; then
  echo "  set ok"
else
  echo "  REFUSED - this OpenClaw does not accept that key. Nothing changed." >&2
  echo "  Send me: openclaw config get agents | head -40" >&2
  exit 1
fi

say "reading it back"
openclaw config get "agents.entries.$AGENT.model" 2>&1 | sed 's/^/  /'
echo
echo "Restart the gateway so the next wake uses it:"
echo "  openclaw gateway restart"
