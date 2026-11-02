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

PIDS=()
OLLAMA_PID=""
owned_ollama_running() {
  local job_pid
  for job_pid in $(jobs -pr); do
    [ "$job_pid" != "$OLLAMA_PID" ] || return 0
  done
  return 1
}
stop_owned_ollama() {
  [ -n "$OLLAMA_PID" ] || return 0
  if owned_ollama_running; then
    kill -TERM "$OLLAMA_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
      owned_ollama_running || break
      sleep 0.1
    done
    if owned_ollama_running; then kill -KILL "$OLLAMA_PID" 2>/dev/null || true; fi
  fi
  wait "$OLLAMA_PID" 2>/dev/null || true
  OLLAMA_PID=""
}
cleanup() {
  trap - EXIT INT TERM
  info "Shutting down"
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  stop_owned_ollama
  if [ "${PIDS[0]+present}" = present ]; then wait "${PIDS[@]}" 2>/dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_optional_ollama() {
  # Probe and start exactly the endpoint the backend will use. Never probe a
  # remote/malformed URL, or reconfigure a daemon we did not start.
  local base_url daemon_host port ollama_log ready
  base_url="${ORB_OLLAMA_BASE_URL:-http://localhost:11434}"
  base_url="${base_url%/}"
  if [[ "$base_url" =~ ^http://(127[.]0[.]0[.]1|localhost|\[::1\])(:([0-9]+))?$ ]]; then
    port="${BASH_REMATCH[3]:-80}"
    daemon_host="http://${BASH_REMATCH[1]}:$port"
    if [ "${#port}" -gt 5 ] || [ "$((10#$port))" -lt 1 ] || [ "$((10#$port))" -gt 65535 ]; then
      info "Optional AI startup skipped: invalid loopback port. Orb will continue."
      return 0
    fi
  else
    info "Optional AI startup skipped: the configured endpoint is not a supported loopback HTTP address. Orb will continue."
    return 0
  fi
  if curl -sf -m 2 "$base_url/api/tags" >/dev/null 2>&1; then
    info "Reusing the existing Ollama daemon; its cache profile is unaffected."
    return 0
  fi
  if ! command -v ollama >/dev/null 2>&1; then
    info "Optional AI startup skipped: Ollama is unavailable. Orb will continue."
    return 0
  fi
  if ! ollama_log="$(mktemp "${TMPDIR:-/tmp}/orb-ollama-serve.XXXXXX")"; then
    info "Optional AI startup skipped: a private log could not be created. Orb will continue."
    return 0
  fi
  info "Starting the Orb-owned Ollama launcher; private log: $ollama_log"
  OLLAMA_HOST="$daemon_host" nohup "$REPO_ROOT/scripts/serve-ollama.sh" > "$ollama_log" 2>&1 &
  OLLAMA_PID=$!
  ready=0
  for _ in 1 2 3; do
    owned_ollama_running || break
    if curl -sf -m 1 "$base_url/api/tags" >/dev/null 2>&1 &&
      [ "$(lsof -nP -a -p "$OLLAMA_PID" "-iTCP:$port" -sTCP:LISTEN -t 2>/dev/null || true)" = "$OLLAMA_PID" ]; then
      ready=1
      break
    fi
    sleep 1
  done
  if [ "$ready" -ne 1 ]; then
    stop_owned_ollama
    info "Optional AI engine did not become ready; Orb will continue. See $ollama_log"
  fi
}
start_optional_ollama

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
# The optional daemon must not keep the shell alive after the app exits.
wait "${PIDS[@]}"
