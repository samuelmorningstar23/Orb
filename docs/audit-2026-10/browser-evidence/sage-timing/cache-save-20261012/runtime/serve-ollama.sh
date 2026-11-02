#!/usr/bin/env bash
# Start an Orb-owned local Ollama daemon. Never starts/stops the desktop app,
# pulls models, edits the model cache, or changes another daemon's environment.
set -euo pipefail

fail() { printf 'Orb Ollama: %s\n' "$*" >&2; exit 1; }
info() { printf 'Orb Ollama: %s\n' "$*" >&2; }

case "${1:-}" in
  -h|--help)
    cat <<'EOF'
Usage: scripts/serve-ollama.sh

Executes `ollama serve` on loopback, with cloud features disabled, startup
pruning disabled, one request per model, and f16 KV cache. No model is pulled.
ORB_OLLAMA_SWA_FULL=auto|on|off (default auto): full SWA cache is qualified only
for Ollama 0.35.1 on Apple Silicon with at least 64 GiB unified memory. The
qualification covers Orb's 8192-token interactive context and cached 27B model.
The qualified profile keeps one inference slot, separate KV buffers, an 8192
MiB RAM prompt cache, and prompt similarity 0.6 in Orb's narrow config.ini.
Existing user/system runner configuration is preserved: auto skips this profile
and on refuses. Orb binds XDG_CONFIG_HOME only for a daemon it starts.
Explicit LLAMA_ARG_SWA_FULL is respected or a conflicting setting is rejected.
Off means this launcher does not enable full SWA; llama.cpp config.ini can
independently enable it. An already-running daemon must be configured separately.
EOF
    exit 0 ;;
  '') ;;
  *) fail 'No arguments are accepted; use --help.' ;;
esac
[ "$#" -le 1 ] || fail 'No arguments are accepted; use --help.'

profile="${ORB_OLLAMA_SWA_FULL:-auto}"
case "$profile" in auto|on|off) ;; *) fail 'ORB_OLLAMA_SWA_FULL must be auto, on, or off.' ;; esac

# Validate before --version: that command contacts the configured daemon.
host="${OLLAMA_HOST:-127.0.0.1:11434}"
if [[ "$host" =~ ^(http://)?(127[.]0[.]0[.]1|localhost|\[::1\])(:([0-9]+))?/?$ ]]; then
  port="${BASH_REMATCH[4]:-11434}"
  [ "${#port}" -le 5 ] || fail 'OLLAMA_HOST has an invalid port.'
  [ "$((10#$port))" -ge 1 ] && [ "$((10#$port))" -le 65535 ] || fail 'OLLAMA_HOST has an invalid port.'
else
  fail 'OLLAMA_HOST must be a loopback HTTP address (127.0.0.1, localhost, or [::1]).'
fi
export OLLAMA_HOST="$host"
export OLLAMA_NO_CLOUD=1 OLLAMA_NOPRUNE=1 OLLAMA_NUM_PARALLEL=1
case "${OLLAMA_KV_CACHE_TYPE:-f16}" in
  f16) export OLLAMA_KV_CACHE_TYPE=f16 ;;
  *) fail 'This launcher requires f16 KV cache; remove the conflicting OLLAMA_KV_CACHE_TYPE.' ;;
esac

ollama_bin="$(command -v ollama)" || fail 'ollama is not on PATH.'
version_output="$("$ollama_bin" --version 2>&1)" || fail 'Cannot inspect the Ollama executable version.'
client_version=''
server_version=''
while IFS= read -r line; do
  if [[ "$line" =~ ^Warning:\ client\ version\ is\ ([^[:space:]]+)$ ]]; then
    client_version="${BASH_REMATCH[1]}"
  elif [[ "$line" =~ ^ollama\ version\ is\ ([^[:space:]]+)$ ]]; then
    server_version="${BASH_REMATCH[1]}"
  fi
done <<< "$version_output"
version="${client_version:-$server_version}"
[[ "$version" =~ ^[0-9]+[.][0-9]+[.][0-9]+([-+][[:alnum:].-]+)?$ ]] || fail 'Cannot determine the Ollama executable version.'

qualified=0
if [ "$version" = 0.35.1 ] && [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ]; then
  memory_bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
  # Limit the integer length before shell arithmetic; memory detection is fail closed.
  if [[ "$memory_bytes" =~ ^[0-9]+$ ]] && [ "${#memory_bytes}" -le 15 ] && [ "$((10#$memory_bytes))" -ge 68719476736 ]; then
    qualified=1
  fi
fi

explicit='unset'
if [ "${LLAMA_ARG_SWA_FULL+x}" = x ]; then
  case "$LLAMA_ARG_SWA_FULL" in
    1|true|on|enabled) explicit=on ;;
    0|false|off|disabled) explicit=off ;;
    *) fail 'LLAMA_ARG_SWA_FULL must be 1/true/on/enabled or 0/false/off/disabled.' ;;
  esac
fi
if { [ "$profile" = on ] && [ "$explicit" = off ]; } || { [ "$profile" = off ] && [ "$explicit" = on ]; }; then
  fail 'ORB_OLLAMA_SWA_FULL conflicts with explicit LLAMA_ARG_SWA_FULL.'
fi

enable_full=0
if [ "$profile" = on ] || { [ "$profile" = auto ] && [ "$explicit" = on ]; }; then
  [ "$qualified" -eq 1 ] || fail 'Full SWA requires tested Ollama 0.35.1, Darwin arm64, and at least 64 GiB memory.'
  enable_full=1
elif [ "$profile" = auto ] && [ "$explicit" = unset ] && [ "$qualified" -eq 1 ]; then
  enable_full=1
fi

if [ "$enable_full" -eq 1 ]; then
  # Inspect the original config location before changing XDG_CONFIG_HOME. No
  # user/system configuration is read, copied, overwritten, or merged.
  config_problem=''
  original_config_root="${XDG_CONFIG_HOME:-${HOME:+$HOME/.config}}"
  if [[ "$original_config_root" != /* ]]; then
    config_problem='the original runner config root is missing or relative'
  fi
  system_config='/etc/llama.cpp/config.ini'
  config_parent_inspectable() {
    local parent="$1"
    while :; do
      if [ -e "$parent" ] || [ -L "$parent" ]; then
        [ -d "$parent" ] && [ -x "$parent" ] || return 1
      fi
      [ "$parent" = / ] && return 0
      parent="${parent%/*}"
      [ -n "$parent" ] || parent=/
    done
  }
  if [ -z "$config_problem" ]; then
    for config_file in "$original_config_root/llama.cpp/config.ini" "$system_config"; do
      if [ -e "$config_file" ] || [ -L "$config_file" ]; then
        config_problem='existing user/system llama.cpp config.ini must remain in control'
        break
      fi
      if ! config_parent_inspectable "${config_file%/*}"; then
        config_problem='the original runner config path cannot be inspected safely'
        break
      fi
    done
  fi
  if [ -n "$config_problem" ]; then
    [ "$profile" != on ] || fail "Cannot enable the optimized cache profile: $config_problem."
    info "Optimized cache profile skipped: $config_problem; inherited configuration is unchanged."
    enable_full=0
  fi
fi

if [ "$enable_full" -eq 1 ]; then
  case "${LLAMA_ARG_KV_UNIFIED-false}" in
    0|false|off|disabled) ;;
    1|true|on|enabled) fail 'The optimized cache profile requires LLAMA_ARG_KV_UNIFIED=false; explicit unified KV conflicts.' ;;
    *) fail 'For the optimized cache profile, LLAMA_ARG_KV_UNIFIED must be 0/false/off/disabled.' ;;
  esac
  [ "${LLAMA_ARG_CACHE_RAM-8192}" = 8192 ] || fail 'The optimized cache profile requires LLAMA_ARG_CACHE_RAM=8192 MiB.'
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
  owned_config_root="$repo_root/config/ollama-xdg"
  owned_config="$owned_config_root/llama.cpp/config.ini"
  [ -f "$owned_config" ] && [ -r "$owned_config" ] && [ ! -L "$owned_config" ] || fail 'The required Orb runner config.ini is missing, unreadable, or a symlink.'
  owned_content="$(cat "$owned_config")" || fail 'Cannot read the required Orb runner config.ini.'
  [ "$owned_content" = $'[default]\nslot-prompt-similarity=0.6' ] || fail 'The required Orb runner config.ini has unexpected content.'
  export XDG_CONFIG_HOME="$owned_config_root" LLAMA_ARG_KV_UNIFIED=false LLAMA_ARG_CACHE_RAM=8192
  if [ "$explicit" = unset ]; then export LLAMA_ARG_SWA_FULL=1; fi
  info 'Full SWA cache enabled for the qualified 64 GiB Apple Silicon profile (Ollama 0.35.1).'
  info 'One inference slot; prompt similarity=0.6, RAM cache=8192 MiB, separate KV; Orb interactive context=8192 tokens.'
else
  info 'Full SWA is not enabled by this launcher; runner config.ini may independently enable it.'
fi
info "Executing Ollama $version on $OLLAMA_HOST; parallel=1, KV=f16, cloud disabled."
exec "$ollama_bin" serve
