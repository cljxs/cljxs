#!/bin/bash
# tailscale-setup.sh — put the Command Deck on your phone, privately.
#
# Tailscale builds a small private network out of your own devices. The droplet
# and the phone join it; nothing else can see them. No ports are opened on the
# public internet, no certificates, no domain.
#
# That matters here specifically: the API has unauthenticated write endpoints -
# POST /tasks queues agent work and POST /api/scout/ideas/N/approve starts an
# Emily build. Both spend money. Reachable from the internet, a portscan would
# be enough to run up a bill.
#
# Safe to re-run.
set -u
ROOT="${ECOSYSTEM_ROOT:-/root/ecosystem}"
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "1. Tailscale"
if command -v tailscale >/dev/null 2>&1; then
  echo "   already installed: $(tailscale version | head -1)"
else
  echo "   installing ..."
  curl -fsSL https://tailscale.com/install.sh | sh || {
    echo "install failed. See https://tailscale.com/download/linux" >&2; exit 1; }
fi
systemctl enable --now tailscaled >/dev/null 2>&1 || true

step "2. Joining your tailnet"
if tailscale ip -4 >/dev/null 2>&1 && [ -n "$(tailscale ip -4 2>/dev/null)" ]; then
  echo "   already connected as $(tailscale ip -4 | head -1)"
else
  cat <<'NOTE'
   A URL will print below. Open it on your phone or laptop and sign in - use
   the SAME account you will use in the Tailscale app on your phone, or the
   two devices land on different networks and cannot see each other.
NOTE
  tailscale up --hostname=ecosystem || { echo "tailscale up failed" >&2; exit 1; }
fi

IP="$(tailscale ip -4 2>/dev/null | head -1)"
NAME="$(tailscale status --json 2>/dev/null \
        | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null)"

step "3. Pointing the dashboard at it"
install -m 644 "$ROOT/deploy/mission-control-api.service" /etc/systemd/system/
systemctl daemon-reload
systemctl restart mission-control-api
sleep 2
if systemctl is-active --quiet mission-control-api; then
  echo "   mission-control-api restarted"
else
  echo "   !! mission-control-api did not come back:" >&2
  journalctl -u mission-control-api -n 15 --no-pager >&2
  exit 1
fi

step "4. Checking nothing is listening in public"
ss -ltnp 2>/dev/null | awk 'NR==1 || /:3001/'
if ss -ltn 2>/dev/null | grep -qE '0\.0\.0\.0:3001|\[::\]:3001'; then
  echo "   !! port 3001 is bound to ALL interfaces - that is public. Stop and check." >&2
  exit 1
fi
echo "   good: 3001 is not on a public interface"

step "On your phone"
cat <<EOF
   1. Install "Tailscale" from the App Store and sign in with the same account.
   2. Turn it on. iOS allows one VPN at a time, so this replaces your current
      one while it is running.
   3. Open:

        http://${NAME:-$IP}:3001/dashboard
        http://${NAME:-$IP}:3001/village

   Add it to your home screen from the share sheet and it behaves like an app.
   Nothing here is reachable from the internet - only from your own devices.
EOF
