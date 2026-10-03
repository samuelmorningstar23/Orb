#!/usr/bin/env bash
#
# Orb Hospital OS — nightly warm-standby restore.
#
# WHY THIS EXISTS
# ---------------
# The hospital BUYS the appliance. Orb REPLACES their HIS. So when the Mac
# Studio dies — PSU, SSD, a dropped-in-transit logic board, a botched macOS
# update — there is no legacy system underneath to fall back to. Two wards lose
# their charts, their orders and their eMAR at once, and the answer cannot be
# "we have backups, give us a day".
#
# scripts/backup.sh already produces a verified, encrypted snapshot every night,
# and scripts/restore-drill.sh already proves a snapshot is restorable. What
# neither gives you is a SECOND MACHINE that is already 24 hours from live. That
# is what this script builds, and the distinction is the whole point:
#
#   backup   = the bytes exist somewhere.
#   drill    = the bytes are provably restorable.
#   STANDBY  = the bytes are ALREADY RESTORED, on hardware that is plugged in,
#              running the same Orb build, and needs one documented action to
#              start seeing patients.
#
# Recovery time is the metric. Restoring 270 MB of SQLCipher onto a bare Mac —
# install Xcode CLT, python, the venv, sqlcipher3, Ollama, pull a 17 GB model,
# build the frontend — is most of a day. Doing it nightly in advance turns that
# into the switchover procedure in docs/WARM_STANDBY.md, which is minutes.
#
# WHAT IT DOES, IN ORDER
#   1. Finds the newest backup in the sync directory (the share/USB/rsync target
#      the primary writes to via `backup.sh --offsite`).
#   2. Refuses to run if this machine looks like the PRIMARY (an Orb server
#      answering on the configured port). Restoring over a live database is the
#      single most destructive thing in this repo, and a script scheduled at
#      03:00 must not be one typo away from doing it.
#   3. Verifies the candidate BEFORE touching anything: size floor, then
#      scripts/lib/verify_restore.py — the SAME verifier scripts/backup.sh and
#      scripts/restore-drill.sh run, so "identical checks to restore-drill.sh"
#      is now enforced by there being one copy of them rather than asserted by
#      a comment. It decrypts, runs integrity_check, asserts the schema version
#      recorded in the backup's own ledger and the presence of every migrated
#      table, asserts row floors on the clinical tables and on the reference
#      rows the migrations write themselves, and walks the audit hash chain.
#      Plus --require-admin: this is the machine that would take over, and a
#      switchover that ends at a login screen nobody can pass is not a
#      switchover. The standby is allowed to be BEHIND, never to be corrupt and
#      never to be missing half the schema.
#   4. Installs it ATOMICALLY: verified copy to a temp name on the SAME
#      filesystem, then `mv` into place. A power cut mid-copy leaves the previous
#      standby database intact rather than a half-written one.
#   5. Keeps ONE generation back (orb.db.previous) so a bad night is reversible.
#   6. Unpacks the matching uploads tar, if present.
#   7. Writes a machine-readable receipt to server/ops/standby/last-restore.json,
#      which services/heartbeat_service.py reads and reports as `standby.*`. A
#      standby that silently stopped restoring is a cold standby wearing a warm
#      standby's clothes; the receipt is how that becomes a page.
#
# Usage:
#   set -a && . ./.env && set +a
#   ./scripts/standby-restore.sh                          # newest from $ORB_STANDBY_SOURCE_DIR
#   ./scripts/standby-restore.sh --source /Volumes/OrbSync
#   ./scripts/standby-restore.sh --backup /Volumes/OrbSync/orb-backup-....db
#   ./scripts/standby-restore.sh --check-only             # verify, install nothing
#
# Env vars:
#   ORB_DB_ENCRYPTION_KEY    (required) SQLCipher key. Same key as the primary.
#   ORB_STANDBY_SOURCE_DIR   where the primary's backups land (default <repo>/backups)
#   ORB_DB_PATH              standby database path (default <repo>/server/orb.db)
#   ORB_STANDBY_RECEIPT      receipt path (default <repo>/server/ops/standby/last-restore.json)
#   ORB_BACKUP_MIN_BYTES     size floor (default 1048576 = 1 MiB)
#   ORB_PORT                 port probed to detect a live Orb (default 8000)
#   ORB_STANDBY_FORCE=1      restore even if an Orb server is answering (DANGEROUS)
#
# Exit codes: 0 restored (or --check-only passed) · 1 precondition failed ·
#             2 the candidate backup failed verification (NOTHING was installed)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SOURCE_DIR="${ORB_STANDBY_SOURCE_DIR:-$REPO_ROOT/backups}"
DB_PATH="${ORB_DB_PATH:-$REPO_ROOT/server/orb.db}"
RECEIPT="${ORB_STANDBY_RECEIPT:-$REPO_ROOT/server/ops/standby/last-restore.json}"
MIN_BYTES="${ORB_BACKUP_MIN_BYTES:-1048576}"
PORT="${ORB_PORT:-8000}"
UPLOADS_DIR="${ORB_UPLOADS_DIR:-$REPO_ROOT/server/uploads}"
PREFIX="orb-backup-"
SUFFIX=".db"

TARGET=""
CHECK_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --source)     SOURCE_DIR="${2:-}"; shift 2 ;;
        --backup)     TARGET="${2:-}";     shift 2 ;;
        --db-path)    DB_PATH="${2:-}";    shift 2 ;;
        --receipt)    RECEIPT="${2:-}";    shift 2 ;;
        --check-only) CHECK_ONLY=1;        shift ;;
        -h|--help)    sed -n '1,76p' "$0"; exit 0 ;;
        -*)           printf '[standby][fail] unknown argument: %s\n' "$1" >&2; exit 1 ;;
        *)            TARGET="$1";         shift ;;
    esac
done

log()  { printf '[standby] %s\n' "$*"; }
warn() { printf '[standby][warn] %s\n' "$*" >&2; }

STARTED_AT="$(date +%s)"
RESULT_STAGE="init"

# The receipt is written on EVERY exit path, success or failure. A missing
# receipt and a failed receipt must look different to the heartbeat: the first
# means "this box never ran the job", the second means "it ran and could not".
write_receipt() {
    local ok="$1" stage="$2" detail="$3" source_file="${4:-}"
    mkdir -p "$(dirname "$RECEIPT")" 2>/dev/null || return 0
    cat > "$RECEIPT" <<JSON
{
  "ok": $ok,
  "stage": "$stage",
  "detail": "$detail",
  "source_basename": "$(basename "${source_file:-none}")",
  "started_unixtime": $STARTED_AT,
  "finished_unixtime": $(date +%s),
  "finished_utc": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "db_path": "$DB_PATH",
  "check_only": $CHECK_ONLY
}
JSON
    chmod 600 "$RECEIPT" 2>/dev/null || true
}

die() {
    local msg="$1" code="${2:-1}"
    printf '[standby][fail] %s\n' "$msg" >&2
    write_receipt false "$RESULT_STAGE" "failed" "${TARGET:-}"
    exit "$code"
}

list_backups() { ls -1t "$SOURCE_DIR"/${PREFIX}*${SUFFIX} 2>/dev/null || true; }

# ── Preconditions ────────────────────────────────────────────────────────────
RESULT_STAGE="preconditions"

PYTHON="$REPO_ROOT/server/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || die "no usable python interpreter found."
"$PYTHON" -c 'import sqlcipher3' 2>/dev/null || \
    die "interpreter '$PYTHON' has no sqlcipher3 — use server/.venv/bin/python, or pip install sqlcipher3-wheels.
        On a fresh standby Mac this usually means the venv was never built: run ./install.sh here first."

# The shared verifier, and the migration list it reads its schema expectation
# from. Both live in the repo checkout on THIS machine, which is also the thing
# that tells us what schema a switchover would need — a standby running an old
# checkout should judge candidates by that old checkout, because that is the
# code that would come up.
VERIFIER="$REPO_ROOT/scripts/lib/verify_restore.py"
[ -f "$VERIFIER" ] || die "missing $VERIFIER — cannot verify a candidate without it."

[ -n "${ORB_DB_ENCRYPTION_KEY:-}" ] || \
    die "ORB_DB_ENCRYPTION_KEY is not set. Load it: set -a && . ./.env && set +a
        The standby needs the SAME key as the primary — an encrypted backup is
        unreadable without it, and no restore of any kind is possible if it is lost."

[ -d "$SOURCE_DIR" ] || die "source directory not found: $SOURCE_DIR
        This is where the primary's backups should be landing (backup.sh --offsite).
        If the share is not mounted, the standby has been drifting since it unmounted."

# ── Refuse to run on the primary ─────────────────────────────────────────────
# This script overwrites a database. On the primary that is data loss measured in
# a full working day of a ward's charting, and it would be discovered by a nurse.
# Two independent guards: a live HTTP listener, and a WAL sidecar that is being
# actively written. Either one is enough to stop.
RESULT_STAGE="primary_guard"
if [ "${ORB_STANDBY_FORCE:-0}" != "1" ]; then
    if command -v curl >/dev/null 2>&1 && \
       curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        die "an Orb server is answering on 127.0.0.1:${PORT} — this machine looks like the PRIMARY.
        Restoring here would overwrite the live database. If this really is the standby
        and something else is on that port, stop it, or set ORB_STANDBY_FORCE=1."
    fi
    if [ -f "$DB_PATH-wal" ]; then
        WAL_AGE=$(( $(date +%s) - $(stat -f %m "$DB_PATH-wal" 2>/dev/null || echo 0) ))
        if [ "$WAL_AGE" -lt 300 ]; then
            die "$DB_PATH-wal was modified ${WAL_AGE}s ago — something is writing to this database.
        Refusing to restore over an active database. Stop Orb on this machine first."
        fi
    fi
fi

# ── Choose the candidate ─────────────────────────────────────────────────────
RESULT_STAGE="select"
if [ -z "$TARGET" ]; then
    TARGET="$(list_backups | head -n1)"
    [ -n "$TARGET" ] || die "no backups found in $SOURCE_DIR.
        The standby has nothing to restore. Check the primary's schedule and the
        --offsite destination: ./scripts/install-backup-schedule.sh --offsite <dir>"
fi
[ -f "$TARGET" ] || die "backup not found: $TARGET"

ABS_TARGET="$(cd "$(dirname "$TARGET")" && pwd)/$(basename "$TARGET")"
ABS_DB="$(cd "$(dirname "$DB_PATH")" 2>/dev/null && pwd)/$(basename "$DB_PATH")" || ABS_DB="$DB_PATH"
[ "$ABS_TARGET" != "$ABS_DB" ] || die "the chosen backup IS the standby database. Refusing."

BYTES="$(wc -c < "$ABS_TARGET" | tr -d ' ')"
AGE_S=$(( $(date +%s) - $(stat -f %m "$ABS_TARGET") ))

log "Standby restore on $(hostname -s)"
log "Source   : $ABS_TARGET"
log "Size     : $(du -h "$ABS_TARGET" | awk '{print $1}') (${BYTES} bytes)"
log "Age      : $(( AGE_S / 3600 ))h $(( (AGE_S % 3600) / 60 ))m behind the primary"
log "Target   : $ABS_DB"

# A backup older than ~2 days means the primary's schedule is broken. Restore it
# anyway — stale data beats no data — but say so loudly, because the standby's
# freshness is silently capped by whatever the primary last managed to write.
if [ "$AGE_S" -gt 172800 ]; then
    warn "newest available backup is $(( AGE_S / 86400 )) days old. The PRIMARY's backup"
    warn "  schedule is probably broken — fix that before trusting this standby."
fi

# ── Verify BEFORE installing ─────────────────────────────────────────────────
RESULT_STAGE="verify"
[ "$BYTES" -ge "$MIN_BYTES" ] || \
    die "backup is only ${BYTES} bytes (floor ${MIN_BYTES}) — implausible. Nothing installed." 2

# Verify a COPY, not the artefact: opening a SQLCipher database creates -wal/-shm
# sidecars beside it and can write to the file. Verifying in place would mutate
# the primary's backup on a shared volume.
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/orb-standby.XXXXXX")"
cleanup() { rm -rf "$SCRATCH"; }
trap cleanup EXIT INT TERM

AVAIL_KB="$(df -k "$SCRATCH" | tail -1 | awk '{print $4}')"
NEED_KB="$(( (BYTES / 1024) * 2 ))"
[ "$AVAIL_KB" -gt "$NEED_KB" ] || \
    die "not enough scratch space: need ~${NEED_KB}KB, have ${AVAIL_KB}KB in $(dirname "$SCRATCH")." 1

CANDIDATE="$SCRATCH/candidate.db"
log "Verifying a scratch copy (the artefact itself is never opened) ..."
cp -p "$ABS_TARGET" "$CANDIDATE"
chmod 600 "$CANDIDATE"

VERIFY_RC=0
# The checks live in scripts/lib/verify_restore.py, shared with backup.sh and
# restore-drill.sh. They used to be an inline heredoc here and a near-identical
# one in each of those; three copies drifted, which is what three copies do.
#
# --schema-policy fail: a candidate whose schema_migrations ledger is behind the
# checkout on THIS machine would come up missing whole modules after a
# switchover, and the standby must not install it silently.
# --require-admin: this is the machine that takes over. A restored database with
# no login-capable admin is a switchover that ends at a login screen.
"$PYTHON" "$VERIFIER" --db "$CANDIDATE" --prefix standby \
    --schema-policy fail --require-admin || VERIFY_RC=$?

# `Verify PASSED` is the line docs/WARM_STANDBY.md and the standby tests look
# for. Printed here from the exit code so it stays true regardless of how the
# shared verifier words its own summary.
[ "$VERIFY_RC" -eq 0 ] && printf '[standby] Verify PASSED — candidate accepted; see the checks above for what that covers.\n'

if [ "$VERIFY_RC" -ne 0 ]; then
    printf '[standby][fail] candidate FAILED verification — NOTHING was installed.\n' >&2
    PREVIOUS="$(list_backups | sed -n '2p')"
    if [ -n "$PREVIOUS" ]; then
        printf '[standby][fail] Try the previous generation:\n' >&2
        printf '[standby][fail]   ./scripts/standby-restore.sh %s\n' "$PREVIOUS" >&2
    else
        printf '[standby][fail] This is the ONLY backup available. There is no fallback.\n' >&2
    fi
    write_receipt false "verify" "candidate_failed_verification" "$ABS_TARGET"
    exit 2
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    log "--check-only: verification passed, nothing installed."
    write_receipt true "check_only" "verified" "$ABS_TARGET"
    exit 0
fi

# ── Install atomically ───────────────────────────────────────────────────────
RESULT_STAGE="install"
mkdir -p "$(dirname "$ABS_DB")"

# The staged copy MUST be on the same filesystem as the destination for `mv` to
# be an atomic rename rather than a copy. Staging in $TMPDIR (a different volume
# on macOS) would reintroduce the torn-write window this step exists to close.
STAGED="$ABS_DB.incoming.$$"
log "Staging verified copy alongside the target ..."
cp -p "$ABS_TARGET" "$STAGED"
chmod 600 "$STAGED"

# One generation back. A standby that restores a subtly bad night over the only
# good one is a standby that made things worse.
if [ -f "$ABS_DB" ]; then
    mv -f "$ABS_DB" "$ABS_DB.previous"
    log "Kept the prior standby database as $(basename "$ABS_DB").previous"
fi
mv -f "$STAGED" "$ABS_DB"

# Stale sidecars belong to the database that was here before. Leaving them can
# make SQLite read a WAL that describes a different file.
rm -f "$ABS_DB-wal" "$ABS_DB-shm"
log "Installed $(basename "$ABS_TARGET") -> $ABS_DB"

# ── Uploaded media ───────────────────────────────────────────────────────────
# orb.db holds "see the chest X-ray"; uploads/ holds the X-ray. A standby with
# only the database gives a clinician a chart full of references to images that
# do not exist — a quiet kind of data loss that looks fine until somebody opens
# a study. The tar's contents are already AES-256-GCM encrypted under
# ORB_UPLOAD_ENCRYPTION_KEY (a DIFFERENT key from the database — the standby
# needs BOTH).
RESULT_STAGE="media"
MEDIA_TAR="${ABS_TARGET%$SUFFIX}-uploads.tar"
if [ -f "$MEDIA_TAR" ]; then
    if tar -xf "$MEDIA_TAR" -C "$(dirname "$UPLOADS_DIR")" 2>/dev/null; then
        log "Media restored from $(basename "$MEDIA_TAR")"
    else
        warn "could not unpack $MEDIA_TAR — DATABASE IS RESTORED, MEDIA IS NOT."
    fi
else
    warn "no matching uploads archive beside the backup — images/audio will be MISSING"
    warn "  on this standby. Check the primary is running backup.sh (not backup_db.py alone)."
fi

RESULT_STAGE="done"
write_receipt true "done" "restored" "$ABS_TARGET"
log "Receipt written to $RECEIPT (the heartbeat reads this — a standby that stops"
log "  restoring is otherwise indistinguishable from one that is working)."

cat <<EOF

[standby] Warm standby updated $(date '+%Y-%m-%d %H:%M:%S').
          This machine is now $(( AGE_S / 3600 ))h behind the primary and is NOT serving.

  To take over, follow docs/WARM_STANDBY.md. The short version:
    1. Confirm the primary is really down (do not run two Orbs on one LAN).
    2. cd $REPO_ROOT && set -a && . ./.env && set +a
    3. server/.venv/bin/python server/scripts/access_review.py     # somebody can log in
    4. Start Orb, then repoint the ward's DNS/IP to this machine.
    5. Tell the wards what window of charting is missing (up to $(( AGE_S / 3600 ))h).

EOF
