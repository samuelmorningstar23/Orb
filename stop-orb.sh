#!/usr/bin/env bash
#
# Orb Hospital OS: stop the stack and unload the models.
#
#   ./stop-orb.sh            # stop run-orb.sh, the backend, the Vite dev server, then unload every model Ollama has loaded
#   ./stop-orb.sh --ollama   # ...and quit the Ollama server/app as well
#
# Why a script: the backend pins medgemma:27b and medgemma:4b with keep_alive=-1 and
# re-warms them every 5 minutes, so "ollama stop" on its own is undone within one
# cycle. The models only stay unloaded once the backend is gone.
#
set -uo pipefail

QUIT_OLLAMA=0
[ "${1:-}" = "--ollama" ] && QUIT_OLLAMA=1

info() { printf '\033[0;34m[info]\033[0m  %s\n' "$*"; }
warn() { printf '\033[0;33m[warn]\033[0m  %s\n' "$*"; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. The wrapper first (its cleanup trap forwards the signal to its children).
for pid in $(pgrep -f "run-orb.sh" 2>/dev/null); do
  info "Stopping run-orb.sh (pid $pid)"
  kill -INT "$pid" 2>/dev/null || true
done

# 2. The backend and the Vite dev server, in case they were started by hand.
for pattern in "$REPO_ROOT/server/.venv/bin/python main.py" "python main.py" "vite" "npm run dev"; do
  for pid in $(pgrep -f "$pattern" 2>/dev/null); do
    # only touch processes that belong to this checkout
    if ps -o command= -p "$pid" 2>/dev/null | grep -q "Orb"; then
      info "Stopping pid $pid ($pattern)"
      kill -INT "$pid" 2>/dev/null || true
    fi
  done
done

# 3. Wait up to 20 s for a clean exit, then escalate.
for _ in $(seq 1 20); do
  pgrep -f "python main.py" >/dev/null 2>&1 || break
  sleep 1
done
if pgrep -f "python main.py" >/dev/null 2>&1; then
  warn "Backend did not exit within 20 s; sending TERM"
  pkill -TERM -f "python main.py" 2>/dev/null || true
  sleep 5
fi
if pgrep -f "python main.py" >/dev/null 2>&1; then
  warn "Backend still alive; sending KILL"
  pkill -KILL -f "python main.py" 2>/dev/null || true
fi

# 4. Unload every model Ollama currently has in memory.
if command -v ollama >/dev/null 2>&1 && curl -sf -m 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
  loaded="$(ollama ps 2>/dev/null | awk 'NR>1 && $1 != "" {print $1}')"
  if [ -z "$loaded" ]; then
    info "No models loaded"
  else
    for m in $loaded; do
      info "Unloading $m"
      ollama stop "$m" >/dev/null 2>&1 || warn "could not unload $m"
    done
  fi
  if [ "$QUIT_OLLAMA" -eq 1 ]; then
    info "Quitting Ollama"
    osascript -e 'quit app "Ollama"' >/dev/null 2>&1 || true
    pkill -INT -f "ollama serve" 2>/dev/null || true
  fi
else
  info "Ollama is not running"
fi

info "Done. Port 8000: $(lsof -nP -iTCP:8000 -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $1" (pid "$2")"}' | head -1)${IFS# }"
[ -z "$(lsof -nP -iTCP:8000 -sTCP:LISTEN 2>/dev/null | tail -n +2)" ] && info "Port 8000 is free"
# Only call the CLI while the server is up: on macOS "ollama ps" relaunches Ollama.app when it is not running.
if curl -sf -m 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
  ollama ps 2>/dev/null | sed 's/^/        /'
else
  info "Ollama is stopped"
fi
