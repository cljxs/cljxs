#!/bin/bash
# deploy.sh [agent ...] [--yes-headers] — pull, rebuild instructions, install
# units, restart timers, and check the result.
#
# This is one command because every multi-line paste in this project has been
# flattened by the terminal at least once, and a half-applied deploy is worse
# than none: the units point at scripts that were never pulled.
#
# Safe to re-run. It rebuilds rather than appends, copies units rather than
# editing them, and only repairs state that is provably broken.
set -u
ROOT="${ECOSYSTEM_ROOT:-/root/ecosystem}"
YES=""
AGENTS=()
for arg in "$@"; do
  case "$arg" in
    --yes-headers) YES="--yes" ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) AGENTS+=("$arg") ;;
  esac
done
[ ${#AGENTS[@]} -gt 0 ] || AGENTS=(belfort ace)

FAILED=0
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '   !! %s\n' "$*"; FAILED=1; }
ok()   { printf '   -- %s\n' "$*"; }

step "pulling $ROOT"
if ! git -C "$ROOT" pull --ff-only; then
  echo
  echo "The pull failed. Do NOT continue - the units below would point at" >&2
  echo "scripts that were never updated. Usually this is a local edit or a" >&2
  echo "generated file that got committed; 'git -C $ROOT status' will say." >&2
  exit 1
fi

for AGENT in "${AGENTS[@]}"; do
  step "$AGENT: rebuilding AGENTS.md"
  if ! "$ROOT/scripts/merge-header.sh" "$AGENT" $YES; then
    echo
    echo "Read the seam above. If the lines it wants to keep are openclaw's" >&2
    echo "seeded text and not $AGENT's own rules, re-run this with" >&2
    echo "  $0 ${AGENTS[*]} --yes-headers" >&2
    exit 1
  fi

  step "$AGENT: installing units"
  for f in "$ROOT"/deploy/"$AGENT"-*.service "$ROOT"/deploy/"$AGENT"-*.timer; do
    [ -e "$f" ] || continue
    install -m 644 "$f" /etc/systemd/system/ && ok "$(basename "$f")"
  done
done

step "reloading systemd"
systemctl daemon-reload && ok "daemon-reload"
for AGENT in "${AGENTS[@]}"; do
  for t in "$ROOT"/deploy/"$AGENT"-*.timer; do
    [ -e "$t" ] || continue
    n=$(basename "$t")
    # restart, not just reload: a timer keeps its old schedule otherwise.
    systemctl enable --now "$n" >/dev/null 2>&1
    systemctl restart "$n" && ok "$n restarted"
  done
done

# ---- per-agent state repair, only where it is provably needed -------------

if printf '%s\n' "${AGENTS[@]}" | grep -qx belfort; then
  step "belfort: checking the book"
  if python3 "$ROOT/scripts/belfort-trade.py" check; then
    ok "nothing to repair"
  else
    echo "   The book does not balance, so the percentage it reports is fiction."
    echo "   Archiving it and restarting the paper account at starting_cash."
    python3 "$ROOT/scripts/belfort-trade.py" reset --confirm \
      --reason "accounting repair: sale proceeds were never credited to cash" \
      || warn "reset failed"
    python3 "$ROOT/scripts/belfort-trade.py" check >/dev/null \
      && ok "book balances now" || warn "still does not balance - stop and look"
  fi
  python3 "$ROOT/scripts/belfort-trade.py" show
fi

if printf '%s\n' "${AGENTS[@]}" | grep -qx ace; then
  step "ace: refreshing data"
  B="$ROOT/agents/ace/state/bankroll.json"
  if [ ! -f "$B" ]; then
    cp "$ROOT/agents/ace/state/bankroll.seed.json" "$B" && ok "seeded bankroll.json (it was missing)"
  else
    ok "bankroll.json present"
  fi
  python3 "$ROOT/scripts/ace-fetch.py" || warn "ace-fetch failed"
  python3 - "$ROOT/agents/ace/data/candidates.json" <<'PY'
import json, sys
try:
    rows = json.load(open(sys.argv[1])).get("candidates") or []
except Exception as exc:
    print(f"   !! cannot read candidates.json: {exc}"); raise SystemExit(0)
bad = [r for r in rows if "{" in str(r.get("selection"))]
null = [r for r in rows if r.get("price") is None]
print(f"   -- {len(rows)} candidates, {len(bad)} malformed names, {len(null)} without a price")
for r in rows[:3]:
    print(f"      {r.get('selection')}  {r.get('match')}  price={r.get('price')}")
if bad or (null and len(null) == len(rows)):
    print("   !! the fetcher fix did not land - is the pull actually applied?")
PY
fi

step "next wakes"
for AGENT in "${AGENTS[@]}"; do
  systemctl list-timers --no-pager "$AGENT-*" 2>/dev/null
done

echo
if [ "$FAILED" -eq 0 ]; then
  echo "Deploy finished. Nothing above needs your attention."
else
  echo "Deploy finished WITH PROBLEMS - the lines marked !! above."
fi
exit "$FAILED"
