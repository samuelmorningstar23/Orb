#!/usr/bin/env bash
#
# Orb Hospital OS — install the nightly warm-standby restore on the SECOND Mac.
#
# Sibling of install-backup-schedule.sh, and it makes the same LaunchAgent
# (not LaunchDaemon) choice for the same reasons — the job needs
# ORB_DB_ENCRYPTION_KEY out of a chmod-600 .env and read/write access to a
# user-owned repo, and a root daemon reading that file would concentrate the DB
# key and the ciphertext it protects under one compromise. Read the long
# rationale at the top of install-backup-schedule.sh; it applies verbatim.
#
# ONE DIFFERENCE THAT MATTERS, and it is the reason this is a separate script:
# this job REFUSES TO INSTALL ON THE PRIMARY. `standby-restore.sh` overwrites
# server/orb.db. Scheduling that on the appliance that is seeing patients would
# destroy a working day of a ward's charting every night at 03:30, and it would
# be discovered by a nurse. So the installer probes for a live Orb and stops.
#
# Usage:
#   ./scripts/install-standby-schedule.sh                       # daily 03:30
#   ./scripts/install-standby-schedule.sh --hour 4 --minute 0
#   ./scripts/install-standby-schedule.sh --source /Volumes/OrbSync
#   ./scripts/install-standby-schedule.sh --run-now
#   ./scripts/install-standby-schedule.sh --uninstall
#
# Schedule it AFTER the primary's backup (02:30 by default) so it collects the
# same night's snapshot rather than the previous one.
#
# Exit codes: 0 ok · 1 precondition failed (incl. "this looks like the primary")
#             2 the job did not load
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LABEL="com.orbhospital.standby-restore"
AGENT_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="${ORB_LOG_DIR:-$REPO_ROOT/logs}"

HOUR=3
MINUTE=30
SOURCE_DIR="${ORB_STANDBY_SOURCE_DIR:-}"
RUN_NOW=0
UNINSTALL=0
FORCE_PRIMARY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --hour)      HOUR="${2:-3}";       shift 2 ;;
        --minute)    MINUTE="${2:-30}";    shift 2 ;;
        --source)    SOURCE_DIR="${2:-}";  shift 2 ;;
        --log-dir)   LOG_DIR="${2:-}";     shift 2 ;;
        --run-now)   RUN_NOW=1;            shift ;;
        --uninstall) UNINSTALL=1;          shift ;;
        --i-know-this-is-the-primary) FORCE_PRIMARY=1; shift ;;
        -h|--help)   sed -n '1,35p' "$0"; exit 0 ;;
        *) printf '[standby-schedule][fail] unknown argument: %s\n' "$1" >&2; exit 1 ;;
    esac
done

log()  { printf '[standby-schedule] %s\n' "$*"; }
warn() { printf '[standby-schedule][warn] %s\n' "$*" >&2; }
die()  { printf '[standby-schedule][fail] %s\n' "$*" >&2; exit "${2:-1}"; }

UID_NUM="$(id -u)"
GUI_TARGET="gui/$UID_NUM"
PLIST="$AGENT_DIR/$LABEL.plist"

job_loaded() { launchctl list "$1" >/dev/null 2>&1; }
job_unload() {
    launchctl bootout "$GUI_TARGET/$1" >/dev/null 2>&1 || \
        launchctl unload -w "$2" >/dev/null 2>&1 || true
}

if [ "$UNINSTALL" -eq 1 ]; then
    if job_loaded "$LABEL" || [ -f "$PLIST" ]; then
        job_unload "$LABEL" "$PLIST"
        rm -f "$PLIST"
        job_loaded "$LABEL" && die "$LABEL still loaded — launchctl print $GUI_TARGET/$LABEL" 2
        log "Removed $LABEL"
    else
        log "Nothing installed — nothing to remove."
    fi
    warn "This machine is no longer maintained as a warm standby. Its copy of the"
    warn "  database will drift further behind the primary every day."
    exit 0
fi

# ── Preconditions ────────────────────────────────────────────────────────────
[ -x "$REPO_ROOT/scripts/standby-restore.sh" ] || \
    die "scripts/standby-restore.sh missing or not executable (chmod +x it)."
[ -f "$REPO_ROOT/.env" ] || die ".env not found at $REPO_ROOT/.env — the job reads the keys from it."

grep -q '^ORB_DB_ENCRYPTION_KEY=' "$REPO_ROOT/.env" || \
    die "ORB_DB_ENCRYPTION_KEY is not in $REPO_ROOT/.env.
       The standby needs the SAME key as the primary; without it every backup is
       unreadable here and the job would fail every night."

grep -q '^ORB_UPLOAD_ENCRYPTION_KEY=' "$REPO_ROOT/.env" || \
    warn "ORB_UPLOAD_ENCRYPTION_KEY is not in .env. The database will restore, but
       Lens images and Scribe audio are encrypted under a DIFFERENT key and will be
       unreadable on this machine. Copy it from the primary before go-live."

# ── The primary guard ────────────────────────────────────────────────────────
PORT="${ORB_PORT:-8000}"
if [ "$FORCE_PRIMARY" -eq 0 ] && command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        printf '\n' >&2
        die "An Orb server is answering on 127.0.0.1:${PORT} — THIS LOOKS LIKE THE PRIMARY.
       The standby job OVERWRITES server/orb.db. Installing it here would destroy a
       day of ward charting every night at $(printf '%02d:%02d' "$HOUR" "$MINUTE").
       Install this on the SECOND Mac, which must not be running Orb.
       (If this genuinely is the standby and something else holds that port, stop it,
       or re-run with --i-know-this-is-the-primary.)"
    fi
fi

case "$REPO_ROOT$LOG_DIR$SOURCE_DIR" in
    *\'*) die "paths must not contain a single quote — refusing to generate a fragile plist." ;;
esac
case "$HOUR" in ''|*[!0-9]*) die "--hour must be 0-23, got: $HOUR" ;; esac
case "$MINUTE" in ''|*[!0-9]*) die "--minute must be 0-59, got: $MINUTE" ;; esac
[ "$HOUR" -ge 0 ] && [ "$HOUR" -le 23 ] || die "--hour must be 0-23, got: $HOUR"
[ "$MINUTE" -ge 0 ] && [ "$MINUTE" -le 59 ] || die "--minute must be 0-59, got: $MINUTE"

mkdir -p "$AGENT_DIR" "$LOG_DIR"

if [ -n "$SOURCE_DIR" ] && [ ! -d "$SOURCE_DIR" ]; then
    warn "source dir does not exist yet: $SOURCE_DIR"
    warn "  Until the primary's backups land there, every run fails and this standby"
    warn "  silently stops advancing. Mount it before you rely on this."
fi

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'; }

ARGS=""
[ -n "$SOURCE_DIR" ] && ARGS=" --source '$SOURCE_DIR'"
# Same shape as the backup job: `. ./.env` keeps both keys in exactly one place
# on disk. A plist is world-readable by default; a copy of the DB key in there
# would be a real regression, not a convenience.
CMD="cd '$REPO_ROOT' && set -a && . ./.env && set +a && exec ./scripts/standby-restore.sh$ARGS"
JOB_LOG="$LOG_DIR/orb-standby-restore.log"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$(xml_escape "$LABEL")</string>

    <!-- Generated by scripts/install-standby-schedule.sh. Re-run that script to
         change the schedule; hand-edits here are overwritten. -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>$(xml_escape "$CMD")</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$(xml_escape "$REPO_ROOT")</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$HOUR</integer>
        <key>Minute</key>
        <integer>$MINUTE</integer>
    </dict>

    <!-- RunAtLoad false: installing a schedule must not overwrite this machine's
         database at install time or on every login. Use --run-now deliberately. -->
    <key>RunAtLoad</key>
    <false/>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
    <key>Nice</key>
    <integer>5</integer>

    <key>StandardOutPath</key>
    <string>$(xml_escape "$JOB_LOG")</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "$JOB_LOG")</string>
</dict>
</plist>
PLIST
chmod 644 "$PLIST"

plutil -lint "$PLIST" >/dev/null || die "generated plist is invalid: $PLIST"

job_unload "$LABEL" "$PLIST"
launchctl bootstrap "$GUI_TARGET" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
job_loaded "$LABEL" || die "$LABEL did not load. Inspect: launchctl print $GUI_TARGET/$LABEL" 2
log "Loaded $LABEL — daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE")"

if [ "$RUN_NOW" -eq 1 ]; then
    log "Running one restore now (--run-now) ..."
    launchctl kickstart "$GUI_TARGET/$LABEL" >/dev/null 2>&1 || \
        warn "kickstart failed; run ./scripts/standby-restore.sh by hand to test."
fi

cat <<EOF

[standby-schedule] Installed.

  Job        $LABEL
  Plist      $PLIST
  Schedule   daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE")
  Source     ${SOURCE_DIR:-<repo>/backups (set ORB_STANDBY_SOURCE_DIR or --source)}
  Log        $JOB_LOG

  Did last night's restore work? Two cheap questions:
    cat $REPO_ROOT/server/ops/standby/last-restore.json   # "ok": true, and how old
    grep -c '\[standby\]\[fail\]' $JOB_LOG                # 0 is what you want

  Verify without installing anything:
    ./scripts/standby-restore.sh --check-only

  Remove it:
    ./scripts/install-standby-schedule.sh --uninstall

  The switchover procedure — print it and tape it to the cupboard:
    docs/WARM_STANDBY.md

EOF

warn "This is a LaunchAgent: it only runs while this user is logged in. Turn ON"
warn "  automatic login for this account, or an unattended reboot silently stops the"
warn "  standby advancing and nobody finds out until the primary dies."
warn "Set ORB_HEARTBEAT_URL and ORB_HEARTBEAT_SITE on this machine too. The receipt"
warn "  this job writes is only an alarm if something reads it off the box."
