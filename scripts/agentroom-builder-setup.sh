#!/bin/bash
# agentroom-builder-setup.sh - make the Agent Room builder's own account and
# tools. Run once as root (and again after updating Claude Code):
#
#     bash /root/ecosystem/scripts/agentroom-builder-setup.sh
#
# What it does, and nothing else:
#   1. an ordinary Linux account, agentbuilder, with a home folder only it and
#      root can see - it cannot read /root (the agents' keys, Belfort's book)
#   2. bubblewrap, which Claude Code needs to strip its login out of every
#      command it runs (CLAUDE_CODE_SUBPROCESS_ENV_SCRUB): the builder's own
#      copy, and an AppArmor rule letting that copy alone make namespaces
#   3. a copy of the claude program the builder can run: the installer put it
#      under /root, which the builder cannot reach
#   4. then `agentroom.py builder-check`, which tries each guard for real
#
# The names agentbuilder and /usr/local/lib/agentroom-builder are also in
# scripts/agentroom.py (BUILDER_USER, BUILDER_CLAUDE, BUILDER_LIB); change both
# together. Nothing here touches the agents, their timers or their logins.
set -u
ROOT="${ECOSYSTEM_ROOT:-/root/ecosystem}"
USER_NAME=agentbuilder
LIB=/usr/local/lib/agentroom-builder

[ "$(id -u)" = 0 ] || { echo "run this as root"; exit 1; }

echo "== 1. the builder's account"
if id "$USER_NAME" >/dev/null 2>&1; then
  echo "   $USER_NAME exists"
else
  useradd --create-home --shell /bin/bash --comment "Agent Room builder" "$USER_NAME" && echo "   made $USER_NAME"
fi
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
mkdir -p "$HOME_DIR/builds"
chown -R "$USER_NAME:$USER_NAME" "$HOME_DIR"
chmod 750 "$HOME_DIR"
chmod 700 /root

echo "== 2. bubblewrap"
if command -v bwrap >/dev/null 2>&1; then
  echo "   installed"
else
  apt-get install -y bubblewrap >/dev/null && echo "   installed bubblewrap" || { echo "   could not install bubblewrap"; exit 1; }
fi

# Ubuntu 24.04 stops ordinary accounts making the namespaces bubblewrap needs
# ("bwrap: setting up uid map: Permission denied"). Rather than lift that for
# the whole machine, the builder gets its own copy of bwrap, which only root
# and the builder can run, and AppArmor allows namespaces for that path alone.
# The guard refuses bwrap and unshare in the AI's own commands, and tests run
# with namespaces switched off (RestrictNamespaces).
mkdir -p "$LIB/bin"
chmod 755 "$LIB" "$LIB/bin"
install -o root -g "$USER_NAME" -m 750 "$(command -v bwrap)" "$LIB/bin/bwrap"
echo "   the builder's copy: $LIB/bin/bwrap"
LOCK=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
if [ -r "$LOCK" ] && [ "$(cat "$LOCK")" = 1 ]; then
  PROFILE=/etc/apparmor.d/agentroom-bwrap
  printf '%s\n' "# Written by agentroom-builder-setup.sh: lets the Agent Room builder's own" \
    "# bubblewrap (and nothing else) make user namespaces, so Claude Code can hide" \
    "# its login from the commands it runs." \
    "abi <abi/4.0>," "include <tunables/global>" "" \
    "profile agentroom-bwrap $LIB/bin/bwrap flags=(unconfined) {" "  userns," "}" > "$PROFILE"
  if apparmor_parser -r "$PROFILE"; then
    echo "   AppArmor allows namespaces for that copy only"
  else
    echo "   could not load the AppArmor rule $PROFILE"; exit 1
  fi
else
  echo "   this system does not lock namespaces - no AppArmor rule needed"
fi

echo "== 3. the claude program, for the builder"
SRC="$(command -v claude || true)"
[ -n "$SRC" ] || SRC=/root/.local/bin/claude
SRC="$(readlink -f "$SRC")"
if [ ! -x "$SRC" ]; then
  echo "   claude is not installed - see $ROOT/plan/README.md"; exit 1
fi
install -o root -g root -m 755 "$SRC" "$LIB/claude"
echo "   $LIB/claude - $("$LIB/claude" --version 2>/dev/null)"

echo "== 4. the builder's own login"
if grep -q '^AGENTROOM_BUILDER_TOKEN=' "$ROOT/agentroom/state/credentials.env" 2>/dev/null; then
  echo "   set"
else
  echo "   not set yet. Make a SEPARATE login for the builder (not your agents' one):"
  echo "     $SRC setup-token"
  echo "   then save it (it will not show as you paste):"
  echo "     python3 $ROOT/scripts/set-credential.py agentroom AGENTROOM_BUILDER_TOKEN"
fi

echo "== 5. trying every guard"
python3 "$ROOT/scripts/agentroom.py" builder-check
