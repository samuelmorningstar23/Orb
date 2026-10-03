#!/usr/bin/env bash
#
# Orb Hospital OS: the operator command.
#
# The status page (server/services/ops_health_service.py, Admin and the
# terminal) ends every problem with the command to run, and every one of those
# commands is `./scripts/orb.sh <something>`. Until 2026-10-02 this file did not
# exist, so a hospital IT person following the page's own advice got "command
# not found". It is a thin dispatcher over scripts that already exist; it adds
# no behaviour of its own except `restore --list`.
#
# Usage:
#   ./scripts/orb.sh status [--json] [--deep]   the status page, in the terminal
#   ./scripts/orb.sh backup [--offsite DIR]     take and verify a backup now
#   ./scripts/orb.sh install-backup-schedule    nightly backup (launchd)
#   ./scripts/orb.sh restore --list             backups on this machine, newest first
#   ./scripts/orb.sh restore                    how to restore (prints the steps)
#   ./scripts/orb.sh encrypt-database [--check] encrypt the database at rest
#   ./scripts/orb.sh restart                    stop Orb, then start it in this terminal
#   ./scripts/orb.sh accounts                   staff accounts, incl. shipped passwords
#   ./scripts/orb.sh create-admin               create an administrator at this keyboard
#   ./scripts/orb.sh help
#
# What each check means and what to do: docs/OPERATIONS.md.
#
# Settings come from <repo>/.env, loaded here the way run-orb.sh loads them.
# ORB_SH_DRY_RUN=1 prints the command a subcommand would run and runs nothing;
# server/tests/test_ops_orb_sh.py uses it to prove every command the status page
# names is one this file understands.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ORB_PYTHON:-$REPO_ROOT/server/.venv/bin/python}"
DRY="${ORB_SH_DRY_RUN:-}"

die() { printf '[orb][fail] %s\n' "$*" >&2; exit 1; }

usage() { sed -n '12,24p' "$0" | sed 's/^# \{0,1\}//'; }

load_env() {
    # Not under a dry run: the test must not depend on this machine's secrets.
    [ -n "$DRY" ] && return 0
    if [ -f "$REPO_ROOT/.env" ]; then
        set -a
        # shellcheck disable=SC1091
        . "$REPO_ROOT/.env"
        set +a
    fi
}

need_python() {
    [ -n "$DRY" ] && return 0
    [ -x "$PY" ] || die "no Orb Python at $PY. Run ./setup-mac.sh first, or set ORB_PYTHON."
}

# Run (or, under ORB_SH_DRY_RUN, only print) one command, replacing this shell.
run() {
    if [ -n "$DRY" ]; then
        printf 'would run:'; printf ' %q' "$@"; printf '\n'
        exit 0
    fi
    exec "$@"
}

backup_dir() { printf '%s' "${ORB_BACKUP_DIR:-$REPO_ROOT/backups}"; }

list_backups() {
    local dir; dir="$(backup_dir)"
    if [ -n "$DRY" ]; then printf 'would list: %s\n' "$dir"; exit 0; fi
    [ -d "$dir" ] || { printf 'No backup folder at %s. There is no backup on this machine.\n' "$dir"; exit 0; }
    local files
    files="$(ls -1t "$dir"/orb-backup-*.db 2>/dev/null || true)"
    if [ -z "$files" ]; then
        printf 'No backups in %s. Take one now: ./scripts/orb.sh backup\n' "$dir"
        exit 0
    fi
    printf 'Backups in %s, newest first:\n\n' "$dir"
    printf '  %-36s %-17s %8s  %-10s %s\n' "FILE" "TAKEN" "SIZE" "CHECK" "SCANS"
    printf '%s\n' "$files" | while IFS= read -r f; do
        local receipt check media
        receipt="${f%.db}.verified.json"
        if grep -Eq '"ok":[[:space:]]*true' "$receipt" 2>/dev/null; then check="verified"
        elif grep -Eq '"ok":[[:space:]]*false' "$receipt" 2>/dev/null; then check="FAILED"
        else check="unchecked"; fi
        if [ -f "${f%.db}-uploads.tar" ]; then media="included"; else media="missing"; fi
        printf '  %-36s %-17s %8s  %-10s %s\n' "$(basename "$f")" \
            "$(date -r "$f" '+%Y-%m-%d %H:%M')" "$(du -h "$f" | awk '{print $1}')" "$check" "$media"
    done
    printf '\nRestore only a "verified" backup. To prove one restores before you need it:\n'
    printf '  ./scripts/restore-drill.sh --backup <file>\n'
}

restore_steps() {
    if [ -n "$DRY" ]; then printf 'would print: restore steps\n'; exit 0; fi
    cat <<EOF
Restoring replaces this hospital's live data with an older copy. Do it only
with a "verified" backup, and only after taking a backup of the current state.

  1. ./scripts/orb.sh restore --list        pick the newest "verified" backup
  2. ./scripts/orb.sh backup                keep a copy of the state you are replacing
  3. ./stop-orb.sh                          stop Orb
  4. cp backups/<file>.db server/orb.db     and delete server/orb.db-wal and -shm
     tar -xf backups/<file>-uploads.tar -C server   (scans and recordings)
  5. ./scripts/orb.sh restart               start Orb with the SAME ORB_DB_ENCRYPTION_KEY

The full procedure, including what to do when the newest backup fails its
check, is in docs/INSTALL_RUNBOOK.md (F4) and docs/OPERATIONS.md.
EOF
}

cmd="${1:-help}"
[ $# -gt 0 ] && shift

case "$cmd" in
    status)
        load_env; need_python
        cd "$REPO_ROOT/server"
        # As a module from server/, so the checks can import the app's own code.
        # Exit code: 0 working, 1 needs attention, 2 urgent.
        run "$PY" -m services.ops_health_service "$@"
        ;;
    backup)
        load_env
        run "$REPO_ROOT/scripts/backup.sh" "$@"
        ;;
    install-backup-schedule)
        run "$REPO_ROOT/scripts/install-backup-schedule.sh" "$@"
        ;;
    restore)
        load_env
        case "${1:-}" in
            --list|list) list_backups ;;
            "") restore_steps ;;
            *) die "restore takes --list, or nothing for the steps. Got: $1" ;;
        esac
        ;;
    encrypt-database)
        load_env; need_python
        cd "$REPO_ROOT/server"
        # --migrate unless the operator asked for something else (e.g. --check).
        if [ $# -eq 0 ]; then set -- --migrate; fi
        run "$PY" scripts/encrypt_db.py "$@"
        ;;
    restart)
        if [ -n "$DRY" ]; then
            printf 'would run: %q then %q\n' "$REPO_ROOT/stop-orb.sh" "$REPO_ROOT/run-orb.sh"
            exit 0
        fi
        "$REPO_ROOT/stop-orb.sh"
        # In the foreground, so the start-up messages are on this screen.
        exec "$REPO_ROOT/run-orb.sh" "$@"
        ;;
    accounts)
        load_env; need_python
        cd "$REPO_ROOT/server"
        # --deep includes the sweep for passwords Orb ships with, which is what
        # the status page sends people here to find. It is slow by design.
        run "$PY" scripts/access_review.py --deep "$@"
        ;;
    create-admin)
        load_env; need_python
        cd "$REPO_ROOT/server"
        run "$PY" scripts/create_admin.py "$@"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        printf '[orb][fail] unknown command: %s\n\n' "$cmd" >&2
        usage >&2
        exit 1
        ;;
esac
