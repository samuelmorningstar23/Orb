#!/usr/bin/env bash
#
# Orb Hospital OS — start the stack.
#
#   ./run-orb.sh          # backend serves the built SPA on http://localhost:8000
#   ./run-orb.sh --dev    # backend + Vite hot-reload on http://localhost:5173
#   ./run-orb.sh --lan    # also reachable from other devices on this network
#                         # (combine with --dev); prints the URL to open
#
# --lan is for testing a second device (a ward tablet) against this machine. It
# is plain http: never use it with real patient data on a shared network.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_ROOT/server/.venv"
DEV_FRONTEND=0
LAN=0
for arg in "$@"; do
  case "$arg" in
    --dev) DEV_FRONTEND=1 ;;
    --lan) LAN=1 ;;
    *) printf 'Unknown option: %s (use --dev and/or --lan)\n' "$arg" >&2; exit 2 ;;
  esac
done

info() { printf '\033[0;34m[info]\033[0m  %s\n' "$*"; }
err()  { printf '\033[0;31m[fail]\033[0m  %s\n' "$*" >&2; }

[ -f "$REPO_ROOT/.env" ] || { err "No .env — run ./setup-mac.sh first."; exit 1; }
[ -x "$VENV/bin/python" ] || { err "No venv — run ./setup-mac.sh first."; exit 1; }

set -a
# shellcheck disable=SC1091
. "$REPO_ROOT/.env"
set +a

LAN_IP=""
if [ "$LAN" -eq 1 ]; then
  # macOS first (Wi-Fi, then Ethernet), then Linux.
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  if [ -z "$LAN_IP" ] && command -v hostname >/dev/null 2>&1; then
    LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  [ -n "$LAN_IP" ] || { err "Could not find this machine's LAN address."; exit 1; }
  # For THIS run only: the origin the other device will load from joins the
  # allow-list, and the backend listens beyond loopback when it serves the SPA
  # itself (in --dev, Vite already listens on the LAN and proxies to it).
  if [ "$DEV_FRONTEND" -eq 1 ]; then
    LAN_ORIGIN="http://$LAN_IP:5173"
  else
    LAN_ORIGIN="http://$LAN_IP:${ORB_PORT:-8000}"
    export ORB_HOST=0.0.0.0
  fi
  export ORB_CORS_ORIGINS="${ORB_CORS_ORIGINS:+$ORB_CORS_ORIGINS,}$LAN_ORIGIN"
fi

# Ollama is optional at boot; the app degrades to errors on AI calls without it.
if command -v ollama >/dev/null 2>&1 && ! curl -sf -m 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
  info "Starting Ollama"
  nohup ollama serve > /tmp/ollama-serve.log 2>&1 &
  sleep 3
fi

PIDS=()
cleanup() {
  info "Shutting down"
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

info "Backend  → http://localhost:${ORB_PORT:-8000}"
( cd "$REPO_ROOT/server" && exec "$VENV/bin/python" main.py ) &
PIDS+=($!)

if [ "$DEV_FRONTEND" -eq 1 ]; then
  sleep 2
  info "Frontend → http://localhost:5173 (hot reload)"
  ( cd "$REPO_ROOT" && exec npm run dev ) &
  PIDS+=($!)
else
  info "SPA served by the backend at http://localhost:${ORB_PORT:-8000}"
fi

if [ "$LAN" -eq 1 ]; then
  info "On another device on this network, open: $LAN_ORIGIN"
  info "Plain http on the LAN: test data only."
fi
info "Sign in: DOC001 / password   (also NUR001, PHA001, ADM001)"
info "Ctrl-C to stop everything."
wait
