#!/bin/bash
# Start the Mission Control API bound to this machine's Tailscale address.
#
# Binding to the tailnet IP rather than 0.0.0.0 means the socket never exists
# on the public interface at all - there is no firewall rule to get wrong and
# no window between boot and the rule being applied. The API has unauthenticated
# write endpoints (POST /tasks queues agent work, POST /api/scout/ideas/N/approve
# starts an Emily build) so "reachable from the internet" would mean "anyone who
# portscans this box can spend your OpenRouter credit".
#
# If Tailscale is not up, it falls back to loopback and says so, rather than
# failing to start - a dashboard you can still reach over SSH beats no service.
set -u
ROOT="${ECOSYSTEM_ROOT:-/root/ecosystem}"
export PORT="${PORT:-3001}"

ts_ip() { tailscale ip -4 2>/dev/null | head -1; }

HOST=""
if command -v tailscale >/dev/null 2>&1; then
  # tailscaled can still be coming up when this unit starts.
  for _ in $(seq 1 20); do
    HOST="$(ts_ip)"
    [ -n "$HOST" ] && break
    sleep 1
  done
fi

if [ -n "$HOST" ]; then
  NAME="$(tailscale status --json 2>/dev/null \
          | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null)"
  echo "binding to Tailscale address $HOST (not reachable from the public internet)"
  [ -n "$NAME" ] && echo "on your phone:  http://$NAME:$PORT/dashboard"
  echo "or by IP:       http://$HOST:$PORT/dashboard"
else
  HOST=127.0.0.1
  echo "TAILSCALE IS NOT UP - falling back to $HOST, so this is reachable only"
  echo "from the droplet itself. Run: /root/ecosystem/scripts/tailscale-setup.sh"
fi

export HOST
exec /usr/bin/node "$ROOT/mission-control-api/server.js"
