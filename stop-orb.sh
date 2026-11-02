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

# Process names alone do not establish ownership: another checkout can also
# run `python main.py`. Verify both command and working directory, and repeat
# that check before every signal in case a PID was reused during shutdown.
owned_process() {
  local pid="$1" command executable arguments cwd
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  [ "$pid" != "$$" ] || return 1
  command="$(ps -o command= -p "$pid" 2>/dev/null)" || return 1
  command="${command#"${command%%[![:space:]]*}"}"
  executable="${command%% *}"
  arguments="${command#* }"
  [ "$arguments" != "$command" ] || return 1
  cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
  # The first script argument must be ours. A path merely mentioned by another
  # program (for example --output /this/checkout/server/main.py) proves nothing.
  case "${executable##*/}" in
    bash|sh|zsh)
      case "$arguments" in
        "$REPO_ROOT/run-orb.sh"|"$REPO_ROOT/run-orb.sh "*) return 0 ;;
        run-orb.sh|run-orb.sh\ *|./run-orb.sh|./run-orb.sh\ *) [ "$cwd" = "$REPO_ROOT" ] ;;
        *) return 1 ;;
      esac ;;
    python|python[0-9]|python[0-9].[0-9]*)
      case "$arguments" in
        main.py|main.py\ *) [ "$cwd" = "$REPO_ROOT/server" ] ;;
        "$REPO_ROOT/server/main.py"|"$REPO_ROOT/server/main.py "*) return 0 ;;
        *) return 1 ;;
      esac ;;
    node)
      case "$arguments" in
        "$REPO_ROOT/node_modules/.bin/vite"|"$REPO_ROOT/node_modules/.bin/vite "*|\
        "$REPO_ROOT/node_modules/vite/bin/vite.js"|"$REPO_ROOT/node_modules/vite/bin/vite.js "*|\
        node_modules/.bin/vite|node_modules/.bin/vite\ *|node_modules/vite/bin/vite.js|node_modules/vite/bin/vite.js\ *)
          [ "$cwd" = "$REPO_ROOT" ] ;;
        *) return 1 ;;
      esac ;;
    npm)
      case "$arguments" in run\ dev|run\ dev\ *) [ "$cwd" = "$REPO_ROOT" ] ;; *) return 1 ;; esac ;;
    *) return 1 ;;
  esac
}

PIDS=()
for pid in $(pgrep -f 'run-orb[.]sh|python[^ ]* .*main[.]py|vite|npm run dev' 2>/dev/null); do
  if owned_process "$pid"; then PIDS+=("$pid"); fi
done

signal_owned() {
  local signal="$1" pid
  for pid in "${PIDS[@]:-}"; do
    if owned_process "$pid"; then
      info "Stopping checkout process $pid ($signal)"
      kill -"$signal" "$pid" 2>/dev/null || true
    fi
  done
}

any_owned_alive() {
  local pid
  for pid in "${PIDS[@]:-}"; do
    if owned_process "$pid"; then return 0; fi
  done
  return 1
}

signal_owned INT

# Wait up to 20 s for a clean exit, then escalate only the verified PIDs.
for _ in $(seq 1 20); do
  any_owned_alive || break
  sleep 1
done
if any_owned_alive; then
  warn "Checkout processes did not exit within 20 s; sending TERM"
  signal_owned TERM
  sleep 5
fi
if any_owned_alive; then
  warn "Checkout processes still alive; sending KILL"
  signal_owned KILL
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
