#!/usr/bin/env bash
#
# Orb Hospital OS — install the scheduled backup (and optional restore drill).
#
# A backup script nobody runs is not a backup strategy. This installs
# scripts/backup.sh as a launchd job on the appliance so it happens whether or
# not anyone remembers.
#
#
# WHY A LaunchAgent AND NOT A LaunchDaemon  ← the decision, and the trade-off
# ─────────────────────────────────────────────────────────────────────────────
# The job needs two things: ORB_DB_ENCRYPTION_KEY, and read access to the repo
# (including server/.venv, the only interpreter with sqlcipher3).
#
#   LaunchDaemon — runs as root, at boot, before and without any login.
#     + Survives a reboot with nobody at the keyboard. Backups keep happening on
#       an unattended appliance in a server cupboard.
#     - Runs as root. To reach ORB_DB_ENCRYPTION_KEY it must read .env, a
#       chmod-600 file owned by the operator account. So the DB key becomes
#       readable by a root-owned job — every root-level compromise now reaches
#       the key AND the ciphertext it protects, on one machine. That is the
#       exact concentration encryption at rest exists to avoid.
#     - Writes root-owned backup files into a user-owned repo. The operator then
#       cannot rotate, copy or delete them without sudo, and scripts/backup.sh's
#       own rotation breaks the first time it runs as anyone else.
#     - macOS TCC: a root daemon touching ~/Documents needs Full Disk Access
#       granted to the daemon binary, and it fails silently-ish when it is not.
#
#   LaunchAgent — runs as the logged-in operator, in their session.
#     + Same user, same permissions, same files as when a human runs
#       ./scripts/backup.sh by hand. What you test is what runs at 02:30.
#     + The key stays confined to one unprivileged account.
#     - Only runs while that user is logged in.
#
# CHOSEN: LaunchAgent. The deciding argument is that the "-" on the agent is
# largely theoretical HERE while the "-" on the daemon is concrete. On this
# appliance the Orb server itself runs in the operator's user session on :8000.
# If that session is not logged in, Orb is not running, no patient data is being
# written, and the thing the backup protects is idle. The failure domains match:
# an agent is down exactly when there is nothing new to lose.
#
# What this REQUIRES you to do, and it is not optional:
#   → Configure the Mac Studio for automatic login as the operator account
#     (System Settings ▸ Users & Groups ▸ Automatic login), so an unattended
#     reboot — power cut, macOS update — comes back to a logged-in session with
#     both Orb and this job running. Verify it after every OS update.
#   → If you ever move Orb itself to run as a root LaunchDaemon (headless, no
#     auto-login), move this job with it. Then the daemon trade-offs above are
#     the ones to solve — a dedicated service account and the key in the System
#     keychain rather than .env, not root reading a user's dotfile.
#
# The key is NOT written into the plist. The job shells out to `. ./.env`, so
# ORB_DB_ENCRYPTION_KEY lives in exactly one place on disk. A plist is
# world-readable by default; a second copy of the DB key there would be a real
# regression, not a convenience.
#
# Usage:
#   ./scripts/install-backup-schedule.sh                      # daily 02:30
#   ./scripts/install-backup-schedule.sh --hour 3 --minute 15
#   ./scripts/install-backup-schedule.sh --offsite /Volumes/OrbBackup
#   ./scripts/install-backup-schedule.sh --with-drill         # + monthly restore drill
#   ./scripts/install-backup-schedule.sh --run-now            # install, then fire once
#   ./scripts/install-backup-schedule.sh --uninstall
#
# Idempotent: re-running rewrites the plist and reloads the job. Safe twice.
#
# Exit codes: 0 ok · 1 precondition failed · 2 the job did not load
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LABEL="com.orbhospital.backup"
DRILL_LABEL="com.orbhospital.restore-drill"
AGENT_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="${ORB_LOG_DIR:-$REPO_ROOT/logs}"

HOUR=2
MINUTE=30
DRILL_DAY=1
DRILL_HOUR=4
OFFSITE_DIR="${ORB_BACKUP_OFFSITE_DIR:-}"
RETAIN=""
WITH_DRILL=0
RUN_NOW=0
UNINSTALL=0
SKIP_PREFLIGHT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --hour)       HOUR="${2:-2}";       shift 2 ;;
        --minute)     MINUTE="${2:-30}";    shift 2 ;;
        --offsite)    OFFSITE_DIR="${2:-}"; shift 2 ;;
        --retain)     RETAIN="${2:-}";      shift 2 ;;
        --log-dir)    LOG_DIR="${2:-}";     shift 2 ;;
        --drill-day)  DRILL_DAY="${2:-1}";  shift 2 ;;
        --with-drill) WITH_DRILL=1;         shift ;;
        --run-now)    RUN_NOW=1;            shift ;;
        --uninstall)  UNINSTALL=1;          shift ;;
        --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
        -h|--help)    sed -n '1,95p' "$0"; exit 0 ;;
        *) printf '[schedule][fail] unknown argument: %s\n' "$1" >&2; exit 1 ;;
    esac
done

log()  { printf '[schedule] %s\n' "$*"; }
warn() { printf '[schedule][warn] %s\n' "$*" >&2; }
die()  { printf '[schedule][fail] %s\n' "$*" >&2; exit "${2:-1}"; }

UID_NUM="$(id -u)"
GUI_TARGET="gui/$UID_NUM"
PLIST="$AGENT_DIR/$LABEL.plist"
DRILL_PLIST="$AGENT_DIR/$DRILL_LABEL.plist"

# ── launchctl compatibility ──────────────────────────────────────────────────
# `bootstrap`/`bootout` are the modern (10.11+) interface; `load -w`/`unload -w`
# are the legacy one. Modern macOS still accepts both, but bootstrap gives real
# error messages instead of silent no-ops, so prefer it and fall back.
job_loaded() { launchctl list "$1" >/dev/null 2>&1; }

job_unload() {
    # Always best-effort: "not loaded" is the desired end state, not an error.
    launchctl bootout "$GUI_TARGET/$1" >/dev/null 2>&1 || \
        launchctl unload -w "$2" >/dev/null 2>&1 || true
}

job_load() {
    local plist="$1"
    launchctl bootstrap "$GUI_TARGET" "$plist" 2>/dev/null || launchctl load -w "$plist"
}

# ── Uninstall ────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" -eq 1 ]; then
    REMOVED=0
    # The .preflight entry is normally torn down by the installer itself; it is
    # listed here so an interrupted install cannot strand a job behind.
    for pair in "$LABEL:$PLIST" "$DRILL_LABEL:$DRILL_PLIST" \
                "$LABEL.preflight:$AGENT_DIR/$LABEL.preflight.plist"; do
        lbl="${pair%%:*}"; plist="${pair#*:}"
        if job_loaded "$lbl" || [ -f "$plist" ]; then
            job_unload "$lbl" "$plist"
            rm -f "$plist"
            if job_loaded "$lbl"; then
                die "$lbl is still loaded after bootout — inspect: launchctl print $GUI_TARGET/$lbl" 2
            fi
            log "Removed $lbl"
            REMOVED=$((REMOVED + 1))
        fi
    done
    [ "$REMOVED" -gt 0 ] || log "Nothing installed — nothing to remove."
    log "Logs are left in place at $LOG_DIR (delete them yourself if you want them gone)."
    warn "Scheduled backups are now OFF. Nothing is protecting this appliance's data."
    exit 0
fi

# ── Preconditions ────────────────────────────────────────────────────────────
[ -x "$REPO_ROOT/scripts/backup.sh" ] || die "scripts/backup.sh missing or not executable (chmod +x it)."
[ -f "$REPO_ROOT/.env" ] || die ".env not found at $REPO_ROOT/.env — the job reads ORB_DB_ENCRYPTION_KEY from it."

grep -q '^ORB_DB_ENCRYPTION_KEY=' "$REPO_ROOT/.env" || \
    die "ORB_DB_ENCRYPTION_KEY is not set in $REPO_ROOT/.env — the job would fail every night."

# The plist embeds a /bin/bash -c command string with the repo path inside single
# quotes. A path containing a single quote would break out of that quoting and
# produce a subtly broken job. Refuse loudly rather than install something wrong.
case "$REPO_ROOT$LOG_DIR$OFFSITE_DIR" in
    *\'*) die "paths must not contain a single quote — refusing to generate a fragile plist." ;;
esac

case "$HOUR" in ''|*[!0-9]*) die "--hour must be 0-23, got: $HOUR" ;; esac
case "$MINUTE" in ''|*[!0-9]*) die "--minute must be 0-59, got: $MINUTE" ;; esac
[ "$HOUR" -ge 0 ] && [ "$HOUR" -le 23 ] || die "--hour must be 0-23, got: $HOUR"
[ "$MINUTE" -ge 0 ] && [ "$MINUTE" -le 59 ] || die "--minute must be 0-59, got: $MINUTE"
[ "$DRILL_DAY" -ge 1 ] && [ "$DRILL_DAY" -le 28 ] || \
    die "--drill-day must be 1-28 (29-31 do not exist in every month), got: $DRILL_DAY"

mkdir -p "$AGENT_DIR" "$LOG_DIR"

if [ -n "$OFFSITE_DIR" ] && [ ! -d "$OFFSITE_DIR" ]; then
    warn "offsite dir does not exist yet: $OFFSITE_DIR"
    warn "  backup.sh will warn and keep the local copy only until that volume is mounted."
fi

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

# ── Build the command the job runs ───────────────────────────────────────────
# `set -a && . ./.env && set +a` is exactly what an operator types by hand, and
# is why the key never has to appear in the plist. `exec` so launchd's PID is
# the backup itself, which makes `launchctl list` and Activity Monitor honest.
BACKUP_ARGS=""
[ -n "$OFFSITE_DIR" ] && BACKUP_ARGS="$BACKUP_ARGS --offsite '$OFFSITE_DIR'"
[ -n "$RETAIN" ] && BACKUP_ARGS="$BACKUP_ARGS --retain '$RETAIN'"

BACKUP_CMD="cd '$REPO_ROOT' && set -a && . ./.env && set +a && exec ./scripts/backup.sh$BACKUP_ARGS"
DRILL_CMD="cd '$REPO_ROOT' && set -a && . ./.env && set +a && exec ./scripts/restore-drill.sh"

BACKUP_LOG="$LOG_DIR/orb-backup.log"
DRILL_LOG="$LOG_DIR/orb-restore-drill.log"

# ── Plist writer ─────────────────────────────────────────────────────────────
# $1 label · $2 plist path · $3 command · $4 log path · $5 calendar interval XML
write_plist() {
    local _label="$1" _path="$2" _cmd="$3" _log="$4" _cal="$5"
    cat > "$_path" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$(xml_escape "$_label")</string>

    <!-- Generated by scripts/install-backup-schedule.sh. Re-run that script to
         change the schedule; hand-edits here are overwritten. -->
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>$(xml_escape "$_cmd")</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$(xml_escape "$REPO_ROOT")</string>

$_cal

    <!-- RunAtLoad is false on purpose: installing the schedule should not fire a
         backup at install time or on every login. Use --run-now for that. -->
    <key>RunAtLoad</key>
    <false/>

    <!-- launchd hands the job a minimal environment; be explicit so du/awk/wc
         and the venv python resolve the same way they do in a real shell. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <!-- The appliance is serving patients while this runs. Copying a multi-GB
         encrypted database must not compete with clinical I/O. -->
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
    <key>Nice</key>
    <integer>5</integer>

    <key>StandardOutPath</key>
    <string>$(xml_escape "$_log")</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "$_log")</string>
</dict>
</plist>
PLIST
    chmod 644 "$_path"
}

daily_cal() {
    cat <<CAL
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$HOUR</integer>
        <key>Minute</key>
        <integer>$MINUTE</integer>
    </dict>
CAL
}

monthly_cal() {
    cat <<CAL
    <key>StartCalendarInterval</key>
    <dict>
        <key>Day</key>
        <integer>$DRILL_DAY</integer>
        <key>Hour</key>
        <integer>$DRILL_HOUR</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
CAL
}

# ── Install ──────────────────────────────────────────────────────────────────
install_job() {
    local _label="$1" _path="$2" _cmd="$3" _log="$4" _cal="$5" _desc="$6"

    write_plist "$_label" "$_path" "$_cmd" "$_log" "$_cal"

    # Validate before loading. A malformed plist otherwise fails at load time
    # with a far less useful message.
    plutil -lint "$_path" >/dev/null || die "generated plist is invalid: $_path"

    # Idempotency: unload any previous copy first. bootstrap on an
    # already-loaded label errors with "service already loaded".
    job_unload "$_label" "$_path"
    job_load "$_path"

    job_loaded "$_label" || die "$_label did not load. Inspect: launchctl print $GUI_TARGET/$_label" 2
    log "Loaded $_label — $_desc"
}

install_job "$LABEL" "$PLIST" "$BACKUP_CMD" "$BACKUP_LOG" "$(daily_cal)" \
    "daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE")"

# ── Preflight: can the job actually REACH the repo? ──────────────────────────
# This is not paranoia, it is the failure this installer was caught by. macOS
# TCC protects ~/Documents, ~/Desktop, ~/Downloads and iCloud Drive from
# processes that have not been granted Full Disk Access — and a launchd job is
# exactly such a process. With the repo under ~/Documents the job loads, reports
# healthy in `launchctl list`, and then fails EVERY NIGHT with:
#
#     shell-init: error retrieving current directory: getcwd: ... Operation not permitted
#     /bin/bash: ./.env: Operation not permitted
#
# Nothing else is wrong. The plist is valid, the schedule is right, the script
# works when a human runs it. It is a silent, permanent, invisible failure — the
# same class of bug as the zero-byte backup that scripts/backup.sh was rewritten
# to prevent. So we find out NOW, at install time, by running the job's own
# access path THROUGH launchd, because only launchd sees what launchd sees.
PREFLIGHT_LABEL="$LABEL.preflight"
PREFLIGHT_PLIST="$AGENT_DIR/$PREFLIGHT_LABEL.plist"
PREFLIGHT_LOG="$LOG_DIR/orb-backup-preflight.log"

preflight_cleanup() {
    job_unload "$PREFLIGHT_LABEL" "$PREFLIGHT_PLIST"
    rm -f "$PREFLIGHT_PLIST" "$PREFLIGHT_LOG"
}

if [ "$SKIP_PREFLIGHT" -eq 1 ]; then
    warn "Preflight skipped (--skip-preflight). You have not verified the job can read the repo."
else
    log "Preflight: checking the scheduled job can actually reach the repo and the key ..."
    rm -f "$PREFLIGHT_LOG"

    # $REPO_ROOT expands now; $ORB_DB_ENCRYPTION_KEY / $ORB_DB_PATH must expand
    # inside the job, after .env is sourced — hence the escaping.
    PREFLIGHT_CMD="cd '$REPO_ROOT' && set -a && . ./.env && set +a"
    PREFLIGHT_CMD="$PREFLIGHT_CMD && [ -n \"\$ORB_DB_ENCRYPTION_KEY\" ]"
    PREFLIGHT_CMD="$PREFLIGHT_CMD && [ -r \"\${ORB_DB_PATH:-$REPO_ROOT/server/orb.db}\" ]"
    PREFLIGHT_CMD="$PREFLIGHT_CMD && [ -x ./scripts/backup.sh ] && echo ORB_PREFLIGHT_OK"

    write_plist "$PREFLIGHT_LABEL" "$PREFLIGHT_PLIST" "$PREFLIGHT_CMD" "$PREFLIGHT_LOG" ""
    plutil -lint "$PREFLIGHT_PLIST" >/dev/null || die "generated preflight plist is invalid."

    job_unload "$PREFLIGHT_LABEL" "$PREFLIGHT_PLIST"
    job_load "$PREFLIGHT_PLIST"
    launchctl kickstart "$GUI_TARGET/$PREFLIGHT_LABEL" >/dev/null 2>&1 || true

    # Bounded wait: poll until the job has no PID, up to ~15s. Never spin forever
    # on an appliance.
    WAITED=0
    while launchctl list "$PREFLIGHT_LABEL" 2>/dev/null | grep -q '"PID"'; do
        /bin/sleep 0.5
        WAITED=$((WAITED + 1))
        [ "$WAITED" -lt 30 ] || break
    done

    if grep -q 'ORB_PREFLIGHT_OK' "$PREFLIGHT_LOG" 2>/dev/null; then
        log "Preflight PASSED — the job can read the repo, .env and the database."
        preflight_cleanup
    else
        PREFLIGHT_OUT="$(cat "$PREFLIGHT_LOG" 2>/dev/null || true)"
        preflight_cleanup
        printf '\n' >&2
        printf '[schedule][fail] PREFLIGHT FAILED — the schedule is installed but WILL NOT WORK.\n' >&2
        printf '[schedule][fail] launchd ran the job and it could not reach the repo:\n\n' >&2
        printf '%s\n\n' "${PREFLIGHT_OUT:-(no output at all — the job produced nothing)}" >&2

        case "$PREFLIGHT_OUT" in
            *"Operation not permitted"*|*"not permitted"*)
                cat >&2 <<EOF
[schedule][fail] This is macOS TCC. The repo lives at:
[schedule][fail]     $REPO_ROOT
[schedule][fail] and macOS blocks launchd jobs from ~/Documents, ~/Desktop, ~/Downloads
[schedule][fail] and iCloud Drive unless the *interpreter* has Full Disk Access.
[schedule][fail]
[schedule][fail] Two fixes. Prefer the first on a real appliance:
[schedule][fail]
[schedule][fail]   1. MOVE THE REPO out of the protected directory. e.g. /opt/orb or
[schedule][fail]      ~/orb. An appliance that is the hospital's system of record should
[schedule][fail]      not keep its database in a user's Documents folder anyway — that
[schedule][fail]      path is also synced to iCloud on many Macs, which would quietly ship
[schedule][fail]      PHI off-box and break the no-cloud promise outright. Move it, then
[schedule][fail]      re-run this installer from the new location.
[schedule][fail]
[schedule][fail]   2. Grant Full Disk Access to /bin/bash (System Settings > Privacy &
[schedule][fail]      Security > Full Disk Access > + > Cmd-Shift-G > /bin/bash). This
[schedule][fail]      works, but it hands FDA to EVERY bash script this Mac ever runs, not
[schedule][fail]      just Orb's. That is a broad grant on a machine holding PHI.
EOF
                ;;
            *)
                printf '[schedule][fail] Check that .env is readable and ORB_DB_ENCRYPTION_KEY is set in it,\n' >&2
                printf '[schedule][fail] and that the database exists at ORB_DB_PATH.\n' >&2
                ;;
        esac
        printf '\n[schedule][fail] Fix the above and re-run this installer. To remove the job now:\n' >&2
        printf '[schedule][fail]     ./scripts/install-backup-schedule.sh --uninstall\n' >&2
        exit 2
    fi
fi

if [ "$WITH_DRILL" -eq 1 ]; then
    [ -x "$REPO_ROOT/scripts/restore-drill.sh" ] || die "scripts/restore-drill.sh missing or not executable."
    install_job "$DRILL_LABEL" "$DRILL_PLIST" "$DRILL_CMD" "$DRILL_LOG" "$(monthly_cal)" \
        "monthly on day $DRILL_DAY at $(printf '%02d:00' "$DRILL_HOUR")"
fi

if [ "$RUN_NOW" -eq 1 ]; then
    log "Kicking off one backup now (--run-now) ..."
    launchctl kickstart "$GUI_TARGET/$LABEL" >/dev/null 2>&1 || \
        warn "kickstart failed; run ./scripts/backup.sh by hand to test."
fi

# ── Report ───────────────────────────────────────────────────────────────────
cat <<EOF

[schedule] Installed.

  Job          $LABEL
  Plist        $PLIST
  Schedule     daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE")
  Command      ./scripts/backup.sh${BACKUP_ARGS:- (no --offsite: LOCAL COPY ONLY)}
  Log          $BACKUP_LOG
EOF

if [ "$WITH_DRILL" -eq 1 ]; then
    cat <<EOF
  Drill job    $DRILL_LABEL — day $DRILL_DAY of each month at $(printf '%02d:00' "$DRILL_HOUR")
  Drill log    $DRILL_LOG
EOF
fi

cat <<EOF

  Check it is registered:
    launchctl list | grep orbhospital
    launchctl print $GUI_TARGET/$LABEL

  Read the log (this is where a failed backup leaves its trace):
    tail -n 50 $BACKUP_LOG

  Did last night's backup actually work? Two questions, both cheap:
    grep -c '\[backup\]\[fail\]' $BACKUP_LOG     # 0 is what you want
    ls -lt $REPO_ROOT/backups | head -3          # newest file should be from last night

  Run it right now without waiting for the schedule:
    launchctl kickstart $GUI_TARGET/$LABEL

  Remove it:
    ./scripts/install-backup-schedule.sh --uninstall

EOF

warn "This is a LaunchAgent: it only runs while this user is logged in."
warn "  Turn ON automatic login for this account, or an unattended reboot silently"
warn "  stops backups (System Settings > Users & Groups > Automatic login)."
warn "If the log shows 'Operation not permitted', grant Full Disk Access to /bin/bash"
warn "  (System Settings > Privacy & Security > Full Disk Access) — macOS TCC blocks"
warn "  launchd jobs from parts of the home directory until you do."

if [ -z "$OFFSITE_DIR" ]; then
    warn "No --offsite destination: every backup will live on the same Mac Studio it"
    warn "  protects. A dead appliance takes the backups with it. Fix this before pilot."
fi
if [ "$WITH_DRILL" -eq 0 ]; then
    log "Not scheduled: the restore drill. Either run it monthly by hand"
    log "  (./scripts/restore-drill.sh) or re-run this installer with --with-drill."
fi
log "Backups are only real once a restore has been tested. See docs/BACKUP_AND_RESTORE.md."
