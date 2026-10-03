#!/usr/bin/env bash
#
# Orb Hospital OS — restore drill.
#
# WHY THIS EXISTS
# ---------------
# "We have backups" is a claim. "We restored a backup on the 1st of the month and
# it opened with real patients and an intact audit chain" is a fact. This script
# is how the claim becomes the fact, and it is deliberately the ONLY backup tool
# an operator is asked to run by hand.
#
# It proves a backup is restorable WITHOUT touching the live database:
#
#   1. Picks the newest backup (or an explicit path).
#   2. COPIES it to a scratch directory and works only on the copy. The copy is
#      not politeness — opening a SQLCipher database creates -wal/-shm sidecars
#      next to it and can write to the file itself. A drill that mutates the
#      artefact it is validating is not a drill.
#   3. Opens the copy with ORB_DB_ENCRYPTION_KEY (the real restore step, minus
#      the swap) and hands it to scripts/lib/verify_restore.py — the single
#      verifier this script, scripts/backup.sh and scripts/standby-restore.sh
#      all share, so the three can no longer drift apart.
#
# What that verifier asserts, and why each one is here:
#   * integrity_check, and that the key actually decrypts the file;
#   * the SCHEMA VERSION in the backup's own schema_migrations ledger, against
#     the highest migration this checkout ships;
#   * that every table any migration creates is PRESENT — the check that still
#     works when the ledger is missing or lying;
#   * row floors on patients / vitals / audit_logs / users (an empty-but-valid
#     database must fail) and on the reference rows the migrations write
#     themselves, where zero can only mean loss;
#   * audit hash-chain LINKAGE: each row's prev_hash must equal the previous
#     row's row_hash. This is the compliance artefact; a backup whose chain is
#     broken restores into something indistinguishable from a tampered record.
#
# THE 2026-08-14 CORRECTION. Until that date this drill checked four tables —
# patients, vitals, audit_logs, users — which is the entire pre-spine schema.
# Migrations 20-26 added 62 more (billing, payments, claims, procurement, front
# desk, biomedical, housekeeping, ABDM, workforce, dietary, NABH). A backup
# taken before migration 20 passed every check and printed RESULT: PASS while
# containing none of them, and F4 in docs/INSTALL_RUNBOOK.md actively walks an
# operator BACKWARDS through the generations until one passes — straight onto
# such a file. The schema-version and table-presence checks close that.
#
# The live database is never opened, never locked, never written. Safe to run
# while the server is up and seeing patients.
#
# Usage:
#   set -a && . ./.env && set +a          # load ORB_DB_ENCRYPTION_KEY
#   ./scripts/restore-drill.sh                              # newest backup
#   ./scripts/restore-drill.sh backups/orb-backup-2026....db # a specific one
#   ./scripts/restore-drill.sh --keep-scratch                # leave the copy behind
#
# Env vars:
#   ORB_DB_ENCRYPTION_KEY  (required) SQLCipher key the backup was written with.
#   ORB_BACKUP_DIR         (optional) where to look (default: <repo>/backups).
#   ORB_DB_PATH            (optional) live DB, used ONLY to refuse to drill on it.
#   ORB_BACKUP_MIN_BYTES   (optional) sanity floor (default: 1048576 = 1 MiB).
#   ORB_BACKUP_REQUIRE_NONEMPTY (optional) comma-separated module groups that
#                          MUST hold rows, e.g. "billing,payments". Set this
#                          once a module goes live: until then an empty module
#                          is normal, and Orb refuses to guess which it is.
#
# Exit codes: 0 PASS · 1 precondition failed (could not run the drill) · 2 FAIL
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BACKUP_DIR="${ORB_BACKUP_DIR:-$REPO_ROOT/backups}"
DB_PATH="${ORB_DB_PATH:-$REPO_ROOT/server/orb.db}"
MIN_BYTES="${ORB_BACKUP_MIN_BYTES:-1048576}"
PREFIX="orb-backup-"
SUFFIX=".db"

TARGET=""
SCRATCH_ROOT="${TMPDIR:-/tmp}"
KEEP_SCRATCH=0

while [ $# -gt 0 ]; do
    case "$1" in
        --backup)       TARGET="${2:-}";       shift 2 ;;
        --scratch-dir)  SCRATCH_ROOT="${2:-}"; shift 2 ;;
        --keep-scratch) KEEP_SCRATCH=1;        shift ;;
        -h|--help)      sed -n '1,66p' "$0"; exit 0 ;;
        -*)             printf '[drill][fail] unknown argument: %s\n' "$1" >&2; exit 1 ;;
        *)              TARGET="$1";           shift ;;
    esac
done

log()  { printf '[drill] %s\n' "$*"; }
die()  { printf '[drill][fail] %s\n' "$*" >&2; exit "${2:-1}"; }

# Same `|| true` as backup.sh: under `set -euo pipefail` an empty glob makes
# `ls` exit 1 and would abort the script at the assignment.
list_backups() { ls -1t "$BACKUP_DIR"/${PREFIX}*${SUFFIX} 2>/dev/null || true; }

# ── Preconditions ────────────────────────────────────────────────────────────
PYTHON="$REPO_ROOT/server/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || die "no usable python interpreter found."

# The venv is the only interpreter with sqlcipher3. Surface that here rather
# than as a mysterious "file is not a database" three steps later.
"$PYTHON" -c 'import sqlcipher3' 2>/dev/null || \
    die "interpreter '$PYTHON' has no sqlcipher3 — use server/.venv/bin/python, or pip install sqlcipher3-wheels."

# The shared verifier. Checked here rather than discovered mid-drill: a drill
# that cannot run its own checks must say so before it copies PHI to scratch.
VERIFIER="$REPO_ROOT/scripts/lib/verify_restore.py"
[ -f "$VERIFIER" ] || die "missing $VERIFIER — the drill cannot verify anything without it."

if [ -z "${ORB_DB_ENCRYPTION_KEY:-}" ]; then
    die "ORB_DB_ENCRYPTION_KEY is not set. Load it from .env first (set -a && . ./.env && set +a).
        Note: if the key is genuinely lost, no drill and no restore is possible — see docs/BACKUP_AND_RESTORE.md."
fi

if [ -z "$TARGET" ]; then
    TARGET="$(list_backups | head -n1)"
    [ -n "$TARGET" ] || die "no backups found in $BACKUP_DIR — there is nothing to drill.
        Run ./scripts/backup.sh (or install the schedule with ./scripts/install-backup-schedule.sh)."
fi
[ -f "$TARGET" ] || die "backup not found: $TARGET"

# Absolute-path both sides before comparing: a relative --backup argument that
# happens to resolve to the live database would otherwise slip through, and this
# script must never be pointed at production.
ABS_TARGET="$(cd "$(dirname "$TARGET")" && pwd)/$(basename "$TARGET")"
if [ -f "$DB_PATH" ]; then
    ABS_DB="$(cd "$(dirname "$DB_PATH")" && pwd)/$(basename "$DB_PATH")"
    [ "$ABS_TARGET" != "$ABS_DB" ] || die "refusing to drill against the LIVE database ($ABS_DB). Point this at a backup."
fi

BYTES="$(wc -c < "$ABS_TARGET" | tr -d ' ')"
AGE_DAYS="$(( ( $(date +%s) - $(stat -f %m "$ABS_TARGET") ) / 86400 ))"

log "Backup under test : $ABS_TARGET"
log "Size              : $(du -h "$ABS_TARGET" | awk '{print $1}') (${BYTES} bytes)"
log "Age               : ${AGE_DAYS} day(s) old"

# ── Scratch copy ─────────────────────────────────────────────────────────────
mkdir -p "$SCRATCH_ROOT" 2>/dev/null || true
[ -d "$SCRATCH_ROOT" ] && [ -w "$SCRATCH_ROOT" ] || die "scratch dir not writable: $SCRATCH_ROOT"

# Refuse rather than fill the appliance's disk mid-drill. 2x the file: the copy
# plus headroom for the WAL/journal the open will create.
AVAIL_KB="$(df -k "$SCRATCH_ROOT" | tail -1 | awk '{print $4}')"
NEED_KB="$(( (BYTES / 1024) * 2 ))"
[ "$AVAIL_KB" -gt "$NEED_KB" ] || \
    die "not enough space in $SCRATCH_ROOT: need ~${NEED_KB}KB, have ${AVAIL_KB}KB. Use --scratch-dir on a bigger volume."

SCRATCH="$(mktemp -d "$SCRATCH_ROOT/orb-restore-drill.XXXXXX")"
cleanup() {
    if [ "$KEEP_SCRATCH" -eq 1 ]; then
        printf '[drill] scratch kept at %s — it contains DECRYPTABLE PHI. Delete it: rm -rf %s\n' "$SCRATCH" "$SCRATCH" >&2
    else
        rm -rf "$SCRATCH"
    fi
}
# Clean up on failure and on Ctrl-C too: a half-finished drill must not leave a
# copy of the patient record lying around in /tmp.
trap cleanup EXIT INT TERM

COPY="$SCRATCH/restored-orb.db"
log "Copying to scratch: $COPY"
cp -p "$ABS_TARGET" "$COPY"
chmod 600 "$COPY"

# ── Checks ───────────────────────────────────────────────────────────────────
if [ "$BYTES" -lt "$MIN_BYTES" ]; then
    printf '[drill]   size floor ....... FAIL (%s bytes < %s)\n' "$BYTES" "$MIN_BYTES"
    printf '\n[drill] RESULT: FAIL — backup is implausibly small; do not rely on it.\n' >&2
    exit 2
fi
log "  size floor ....... PASS (>= ${MIN_BYTES} bytes)"

# `|| DRILL_RC=$?` is load-bearing under `set -e`: without it a failing drill
# would abort the script right here and never print the guidance below. Same
# pattern scripts/backup.sh uses for its verification heredoc.
DRILL_RC=0
# The verification itself lives in scripts/lib/verify_restore.py, shared with
# scripts/backup.sh and scripts/standby-restore.sh. It used to be an inline
# heredoc here, and a near-identical one in each of those — three copies of the
# same checks, which drifted exactly as three copies do (all three carried a
# comment claiming their 4-table list was "kept in agreement with" a 14-table
# list in encrypt_db.py). One file, one manifest, one policy.
#
# --schema-policy fail: this script is deciding whether a file is a good thing
# to RESTORE, and a backup older than the shipped schema is not.
"$PYTHON" "$VERIFIER" --db "$COPY" --prefix drill --schema-policy fail || DRILL_RC=$?

# `RESULT: PASS` / `RESULT: FAIL` is a CONTRACT, not decoration. The install
# runbook's go-live checklist, F4's recovery loop, docs/BACKUP_AND_RESTORE.md and
# the printed key-escrow sheet (server/scripts/escrow_sheet.py) all tell a human
# to look for that exact line. Printed here, from the drill's own exit code, so
# it stays true no matter how the shared verifier phrases its own summary.
if [ "$DRILL_RC" -ne 0 ]; then
    printf '\n[drill] RESULT: FAIL — do not rely on this backup.\n' >&2
    printf '[drill][fail] DRILL FAILED for %s\n' "$(basename "$ABS_TARGET")" >&2
    PREVIOUS="$(list_backups | sed -n '2p')"
    if [ -n "$PREVIOUS" ]; then
        printf '[drill][fail] Do not assume the other backups are fine — drill the next one down too:\n' >&2
        printf '[drill][fail]   ./scripts/restore-drill.sh %s\n' "$PREVIOUS" >&2
        printf '[drill][fail] If it failed on `schema version` or `module tables`, walking further\n' >&2
        printf '[drill][fail] BACK will only make that worse — those failures mean too OLD, not corrupt.\n' >&2
    else
        printf '[drill][fail] This is the ONLY backup present. There is no fallback. Fix this today.\n' >&2
    fi
    exit 2
fi

cat <<EOF

[drill] RESULT: PASS — this backup decrypts, is internally consistent, carries the
[drill]   complete current schema, holds real clinical rows, and its audit chain
[drill]   is unbroken.

[drill] Drill passed on $(date '+%Y-%m-%d %H:%M:%S'). Record it: an untested backup is a rumour.

  A real restore, for when you need it (the drill is these steps minus the swap):
    1. Stop the Orb server.
    2. cp "$ABS_TARGET" "$DB_PATH"
    3. rm -f "$DB_PATH-wal" "$DB_PATH-shm"        # stale sidecars belong to the old file
    4. Start the server with the SAME ORB_DB_ENCRYPTION_KEY.

  Full runbook: docs/BACKUP_AND_RESTORE.md
EOF
