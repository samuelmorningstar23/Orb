#!/usr/bin/env bash
#
# Orb Hospital OS — verified, timestamped database backup with rotation.
#
# WHY THIS SCRIPT WAS REWRITTEN (2026-07-30)
# ------------------------------------------
# The previous version snapshotted the database with the system `sqlite3` CLI:
#
#     sqlite3 "$DB_PATH" ".backup '$TMP_DB'" || die "sqlite3 .backup failed."
#
# That worked while orb.db was plaintext. Once the database was migrated to
# SQLCipher, the stock CLI can no longer open it — and the failure was SILENT
# and destructive in four compounding ways:
#
#   1. `sqlite3` printed "Error: file is not a database" but exited 0, so the
#      `|| die` guard never fired.
#   2. It left a ZERO-BYTE temp file behind.
#   3. `PRAGMA integrity_check` on a zero-byte file returns `ok` — an empty file
#      is a perfectly valid empty database — so the integrity gate passed too.
#   4. The script then encrypted the empty file, wrote it as a backup, and ran
#      rotation, PRUNING REAL BACKUPS to make room for empty ones.
#
# Fourteen scheduled runs would have silently destroyed every good backup while
# reporting success every time. That is the exact failure mode a backup exists
# to prevent, so this rewrite is built around one rule: NEVER rotate until the
# new backup has been proven to contain real data.
#
# WHAT IT DOES NOW
# ----------------
# Delegates the snapshot to server/scripts/backup_db.py, which uses SQLite's
# online backup API through the app's own codec-aware connection helper. That
# is consistent on a hot WAL-mode database AND SQLCipher-aware, and the copy it
# produces is itself SQLCipher-encrypted with ORB_DB_ENCRYPTION_KEY.
#
# The old outer `openssl enc` layer is gone. It used the same key as the
# database, so it added no protection an attacker could not already defeat, and
# it made restore a two-step process people got wrong under pressure. One layer,
# one key, one documented restore path.
#
# Usage:
#   set -a && . ./.env && set +a   # load ORB_DB_ENCRYPTION_KEY / ORB_DB_PATH
#   ./scripts/backup.sh
#   ./scripts/backup.sh --offsite /Volumes/OrbBackup   # also copy off the box
#
# Env vars:
#   ORB_DB_ENCRYPTION_KEY  (required) SQLCipher key for the live DB and backups.
#   ORB_DB_PATH            (optional) path to orb.db (default: <repo>/server/orb.db).
#   ORB_BACKUP_DIR         (optional) output dir  (default: <repo>/backups).
#   ORB_BACKUP_RETAIN      (optional) how many to keep (default: 14).
#   ORB_BACKUP_OFFSITE_DIR (optional) second destination; same as --offsite.
#   ORB_BACKUP_MIN_BYTES   (optional) sanity floor (default: 1048576 = 1 MiB).
#   ORB_PYTHON             (optional) absolute interpreter path (venv or CI runtime).
#   ORB_BACKUP_REQUIRE_NONEMPTY (optional) comma-separated module groups that
#                          must hold rows, e.g. "billing,payments" — set once a
#                          module has gone live. See scripts/lib/verify_restore.py.
#   ORB_FHIR_EXPORT_DIR    (optional) where the app writes the ward print packs
#                          (default: <repo>/server/downtime_exports).
#
# Every backup the verifier judges gets a receipt beside it,
# <stamp>.verified.json (schema orb.backup.verification/1), on PASS and on FAIL.
# The operations status page reads it: without one a backup can only ever be
# shown as "unchecked". Rotation counts and prunes VERIFIED backups only (a
# receipt that says ok), so a run of failed or unchecked files can never push a
# good backup out of the window.
#
# Exit codes: 0 ok · 1 precondition failed · 2 backup unverifiable (nothing rotated)
#
set -euo pipefail
umask 077  # Archives and directories must be private from their first write.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DB_PATH="${ORB_DB_PATH:-$REPO_ROOT/server/orb.db}"
BACKUP_DIR="${ORB_BACKUP_DIR:-$REPO_ROOT/backups}"
RETAIN="${ORB_BACKUP_RETAIN:-14}"
MIN_BYTES="${ORB_BACKUP_MIN_BYTES:-1048576}"
OFFSITE_DIR="${ORB_BACKUP_OFFSITE_DIR:-}"
PREFIX="orb-backup-"
SUFFIX=".db"

while [ $# -gt 0 ]; do
    case "$1" in
        --offsite) OFFSITE_DIR="${2:-}"; shift 2 ;;
        --retain)  RETAIN="${2:-14}";    shift 2 ;;
        -h|--help) sed -n '1,66p' "$0"; exit 0 ;;
        *) printf '[backup][fail] unknown argument: %s\n' "$1" >&2; exit 1 ;;
    esac
done

log()  { printf '[backup] %s\n' "$*"; }
die()  { printf '[backup][fail] %s\n' "$*" >&2; exit "${2:-1}"; }

# How many backups exist right now. The `|| true` is load-bearing: under
# `set -euo pipefail`, an empty glob makes `ls` exit 1, which aborts the whole
# script at the assignment — on the very first run, when the directory is
# legitimately empty and there is nothing wrong at all.
list_backups() { ls -1t "$BACKUP_DIR"/${PREFIX}*${SUFFIX} 2>/dev/null || true; }
count_backups() { list_backups | wc -l | tr -d ' '; }

# <dir>/orb-backup-STAMP.db -> <dir>/orb-backup-STAMP.verified.json
receipt_for() { printf '%s.verified.json' "${1%$SUFFIX}"; }
# True only for a passing receipt bound to this backup's name and size.
is_verified() { "$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); from scripts.backup_db import passing_receipt; from pathlib import Path; sys.exit(0 if passing_receipt(Path(sys.argv[2])) else 1)' "$REPO_ROOT/server" "$1"; }
# Always exits 0 (an `if`, not `&&`): under pipefail a non-zero loop would abort
# the assignment that counts these, and with it the run.
list_verified() {
    list_backups | while IFS= read -r f; do
        if is_verified "$f"; then printf '%s\n' "$f"; fi
    done
}

# ── Preconditions ────────────────────────────────────────────────────────────
PYTHON="${ORB_PYTHON:-$REPO_ROOT/server/.venv/bin/python}"
if [ -z "${ORB_PYTHON:-}" ] && [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3 || true)"
fi
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || die "no usable python interpreter found."

# The venv matters: it is the only interpreter with sqlcipher3 installed. A bare
# python3 will fail to open an encrypted DB, which we would rather surface here
# than three steps later.
"$PYTHON" -c 'import sqlcipher3' 2>/dev/null || \
    die "interpreter '$PYTHON' has no sqlcipher3 — run server/.venv/bin/python, or pip install sqlcipher3-wheels."

# The shared verifier. Checked BEFORE the snapshot: this script's founding rule
# is that nothing rotates until the new backup is proven, and a missing verifier
# means nothing can ever be proven — better to fail before writing a file that
# would sit there looking like a good backup.
VERIFIER="$REPO_ROOT/scripts/lib/verify_restore.py"
[ -f "$VERIFIER" ] || die "missing $VERIFIER — refusing to take a backup that cannot be verified."

[ -f "$DB_PATH" ] || die "database not found at: $DB_PATH"

if [ -z "${ORB_DB_ENCRYPTION_KEY:-}" ]; then
    die "ORB_DB_ENCRYPTION_KEY is not set. Load it from .env first (set -a && . ./.env && set +a)."
fi

mkdir -p "$BACKUP_DIR"

# ── Snapshot (SQLCipher-aware, consistent on a hot WAL database) ──────────────
# Hold the shared producer lock through verification, media/offsite copy and
# rotation. CLI and admin producers use this same lock; macOS has no flock CLI.
export ORB_BACKUP_RETAIN="$RETAIN" ORB_BACKUP_OFFSITE_DIR="$OFFSITE_DIR"
if [ "${ORB_BACKUP_LOCK_HELD:-}" != "$BACKUP_DIR" ]; then
    exec "$PYTHON" -c 'import os,sys; sys.path.insert(0,sys.argv[1]); from pathlib import Path; from scripts.backup_db import backup_set_lock; import subprocess
with backup_set_lock(Path(sys.argv[2])):
    env=dict(os.environ, ORB_BACKUP_LOCK_HELD=sys.argv[2])
    raise SystemExit(subprocess.call(["/bin/bash",sys.argv[3],*sys.argv[4:]],env=env))' "$REPO_ROOT/server" "$BACKUP_DIR" "$0" "$@"
fi
case "$RETAIN" in ''|*[!0-9]*) die "retain must be a positive integer." ;; esac
[ "$RETAIN" -ge 1 ] || die "retain must keep at least one verified backup."

log "Snapshotting $DB_PATH -> $BACKUP_DIR ..."
# Capture THIS producer's path. A parallel writer or a same-second run must
# never make us verify, remove or copy somebody else's newest file.
NEW_FILE="$("$PYTHON" "$REPO_ROOT/server/scripts/backup_db.py" \
    --db-path "$DB_PATH" --out-dir "$BACKUP_DIR" --retain-days 0 --snapshot-only --print-path)" \
    || die "snapshot failed — nothing rotated." 2
[ -n "$NEW_FILE" ] && [ -f "$NEW_FILE" ] || die "snapshot reported success but produced no file." 2

# ── Verify BEFORE trusting or rotating ───────────────────────────────────────
# Size floor first: it is the cheap check that would have caught the zero-byte
# regression on its own.
BYTES="$(wc -c < "$NEW_FILE" | tr -d ' ')"
if [ "$BYTES" -lt "$MIN_BYTES" ]; then
    rm -f "$NEW_FILE"
    die "backup is only ${BYTES} bytes (floor ${MIN_BYTES}) — deleted, nothing rotated." 2
fi

# Then prove it is a real, decryptable database with real clinical rows in it.
# An empty-but-valid database must FAIL this, which is why row counts are
# asserted rather than just "does it open".
log "Verifying $(basename "$NEW_FILE") ..."
# The checks live in scripts/lib/verify_restore.py — the same verifier
# scripts/restore-drill.sh and scripts/standby-restore.sh run. Each of the three
# used to carry its own inline copy, and all three drifted: every one of them
# claimed its 4-table row-count floor was "kept in agreement with"
# encrypt_db.py::VERIFY_TABLES, which has fourteen. One file now, so agreement is
# structural instead of aspirational.
#
# --schema-policy warn, and this is the one place that differs from the other
# two, deliberately. The drill and the standby are deciding whether a file is a
# good thing to RESTORE; this script is deciding whether to keep a snapshot it
# just took of the LIVE database. A snapshot of a behind-schema live database is
# still the best copy of that database in existence, and failing here means no
# backup is kept AND no rotation runs — inventing a fresh way to have nothing.
# So a schema that is behind, or missing migrated tables, is shouted about and
# the backup is still kept. What still FAILS here: it will not decrypt, it is
# corrupt, it holds no patients, its audit chain is broken, or a reference table
# that a migration filled has been emptied.
#
# --receipt leaves the verdict beside the file for the status page, PASS or FAIL.
# A failed backup is kept with its failing receipt (the page then says URGENT
# rather than quietly grading last night's file), and rotation below never
# counts it.
RECEIPT="$(receipt_for "$NEW_FILE")"
"$PYTHON" "$VERIFIER" --db "$NEW_FILE" --prefix backup --schema-policy warn \
    --receipt "$RECEIPT" --backup-file "$(basename "$NEW_FILE")" \
    || die "verification FAILED: backup is unusable (see $(basename "$RECEIPT")). Nothing rotated." 2
is_verified "$NEW_FILE" || die "verifier did not leave a matching passing receipt — nothing rotated." 2

log "Verified: $(du -h "$NEW_FILE" | awk '{print $1}') encrypted backup with real clinical rows."
chmod 600 "$NEW_FILE"

# ── Uploaded media ───────────────────────────────────────────────────────────
# orb.db holds the note that says "see the chest X-ray"; server/uploads/ holds
# the X-ray. Restoring only the database gives you a clinical record full of
# references to images and dictation that no longer exist, which is a quiet kind
# of data loss — everything looks present until someone opens a study.
#
# The files are ALREADY encrypted individually (AES-256-GCM under
# ORB_UPLOAD_ENCRYPTION_KEY — a DIFFERENT key from the database), so a plain tar
# preserves that protection and adds nothing by re-encrypting. Note the two-key
# consequence: escrowing the DB key alone still loses the media.
UPLOADS_DIR="${ORB_UPLOADS_DIR:-$REPO_ROOT/server/uploads}"
MEDIA_FILE=""
if [ -d "$UPLOADS_DIR" ]; then
    STAMP="$(basename "$NEW_FILE" "$SUFFIX")"          # orb-backup-YYYYmmdd-HHMMSS
    MEDIA_FILE="$BACKUP_DIR/${STAMP}-uploads.tar"
    if tar -cf "$MEDIA_FILE" -C "$(dirname "$UPLOADS_DIR")" "$(basename "$UPLOADS_DIR")" 2>/dev/null; then
        chmod 600 "$MEDIA_FILE"
        log "Media: $(du -h "$MEDIA_FILE" | awk '{print $1}') from $UPLOADS_DIR"
    else
        # Non-fatal by design: a verified database backup is still worth keeping,
        # and failing the whole run would tempt an operator to disable the job.
        printf '[backup][warn] could not archive %s — DATABASE BACKUP IS COMPLETE, MEDIA IS NOT.\n' "$UPLOADS_DIR" >&2
        rm -f "$MEDIA_FILE"; MEDIA_FILE=""
    fi
else
    printf '[backup][warn] no uploads directory at %s — skipping media.\n' "$UPLOADS_DIR" >&2
fi

# ── Ward print packs ─────────────────────────────────────────────────────────
# The app keeps one printable downtime pack per ward (ward-pack.*.latest.html)
# in its export directory, for the day the appliance cannot be used. A pack
# that lives only on that appliance is lost with it, so the latest packs ride
# in this set. They are plaintext PHI and every other file here is encrypted,
# so they go in as an openssl archive under the same ORB_DB_ENCRYPTION_KEY,
# never as plaintext. Readable plaintext copies off the box are the job of the
# app's ORB_WARD_PACK_MIRROR_DIR, which an operator sets on purpose.
EXPORT_DIR="$(printf '%s' "${ORB_FHIR_EXPORT_DIR:-}" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
[ -n "$EXPORT_DIR" ] || EXPORT_DIR="$REPO_ROOT/server/downtime_exports"
PACKS_FILE=""
PACKS="$(ls -1 "$EXPORT_DIR"/ward-pack.*.latest.html 2>/dev/null || true)"
if [ -z "$PACKS" ]; then
    printf '[backup][warn] no ward print packs in %s: skipping packs.\n' "$EXPORT_DIR" >&2
elif ! command -v openssl >/dev/null 2>&1; then
    printf '[backup][warn] openssl not found: ward print packs NOT backed up (they are never stored unencrypted).\n' >&2
else
    PACKS_FILE="$BACKUP_DIR/$(basename "$NEW_FILE" "$SUFFIX")-ward-packs.tar.enc"
    # -pass env: keeps the key off the command line (and out of `ps`).
    if ( cd "$EXPORT_DIR" && tar -cf - ward-pack.*.latest.html ) 2>/dev/null \
        | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
              -pass env:ORB_DB_ENCRYPTION_KEY -out "$PACKS_FILE" 2>/dev/null; then
        chmod 600 "$PACKS_FILE"
        log "Ward packs: $(printf '%s\n' "$PACKS" | wc -l | tr -d ' ') latest pack(s), encrypted, from $EXPORT_DIR"
    else
        # Non-fatal, like media: the verified database backup still stands.
        printf '[backup][warn] could not archive the ward print packs from %s: DATABASE BACKUP IS COMPLETE, PACKS ARE NOT.\n' "$EXPORT_DIR" >&2
        rm -f "$PACKS_FILE"; PACKS_FILE=""
    fi
fi

# Authenticate database/media identity and exact bytes with the escrow key.
SET_MANIFEST="${NEW_FILE%$SUFFIX}.set.json"
if [ -n "$MEDIA_FILE" ]; then
    "$PYTHON" "$REPO_ROOT/scripts/lib/backup_set.py" seal --backup "$NEW_FILE" --media "$MEDIA_FILE"
else
    "$PYTHON" "$REPO_ROOT/scripts/lib/backup_set.py" seal --backup "$NEW_FILE"
fi

# ── Off-box copy ─────────────────────────────────────────────────────────────
# An on-appliance backup does not survive the appliance. This is the step that
# turns "we have backups" into "we can come back from a dead Mac Studio".
if [ -n "$OFFSITE_DIR" ]; then
    if [ -d "$OFFSITE_DIR" ]; then
        copy_artifact() { "$PYTHON" -c 'import sys; sys.path.insert(0,sys.argv[1]); from pathlib import Path; from scripts.backup_db import copy_artifact; copy_artifact(Path(sys.argv[2]),Path(sys.argv[3]))' "$REPO_ROOT/server" "$1" "$OFFSITE_DIR"; }
        copy_artifact "$NEW_FILE"
        log "Copied off-box -> $OFFSITE_DIR/$(basename "$NEW_FILE")"
        if [ -n "$MEDIA_FILE" ]; then
            copy_artifact "$MEDIA_FILE"
            log "Copied off-box -> $OFFSITE_DIR/$(basename "$MEDIA_FILE")"
        fi
        if [ -n "$PACKS_FILE" ]; then
            copy_artifact "$PACKS_FILE"
            log "Copied off-box -> $OFFSITE_DIR/$(basename "$PACKS_FILE")"
        fi
        copy_artifact "$SET_MANIFEST"
        # Publish the verdict last, after every available member copied.
        copy_artifact "$RECEIPT"
    else
        # Loud, but not fatal: the local verified backup still exists.
        printf '[backup][warn] offsite dir not present (%s) — LOCAL COPY ONLY.\n' "$OFFSITE_DIR" >&2
    fi
else
    printf '[backup][warn] no offsite destination set — this backup lives only on the appliance it protects.\n' >&2
fi

# ── Rotation: only now, and only ever among VERIFIED backups ─────────────────
# The shared Python rotation works on macOS's bash 3.2 and validates receipts
# again while holding the producer lock. Only verified sets are counted/pruned.
# Counting every
# file let failed snapshots (kept for the status page) fill the window and push
# the last good backups out. A file with no receipt or a failing one is never
# pruned here: delete it by hand once a newer verified backup exists.
"$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from scripts.backup_db import rotate_backups; n=rotate_backups(Path(sys.argv[2]),new_backup=Path(sys.argv[3]),retain_count=int(sys.argv[4])); print("[backup] Rotation: pruned",n,"verified set(s).")' "$REPO_ROOT/server" "$BACKUP_DIR" "$NEW_FILE" "$RETAIN"
COUNT="$(list_verified | wc -l | tr -d ' ')"
UNVERIFIED="$(( $(count_backups) - COUNT ))"
if [ "$UNVERIFIED" -gt 0 ]; then
    printf '[backup][warn] %s backup file(s) have no passing receipt and are never rotated; review them with ./scripts/orb.sh restore --list.\n' "$UNVERIFIED" >&2
fi

log "Done."
cat <<EOF

  To restore:
    1. Stop the server.
    2. cp "$NEW_FILE" server/orb.db     (and delete any stale -wal / -shm)
    3. Start the server with the SAME ORB_DB_ENCRYPTION_KEY.

  The backup is SQLCipher-encrypted with ORB_DB_ENCRYPTION_KEY. That key is not
  stored in the backup. If it is lost, every backup is permanently unreadable —
  keep an escrow copy somewhere that does not depend on this appliance.
EOF
if [ -n "$PACKS_FILE" ]; then
cat <<EOF

  To read the ward print packs without Orb (any machine with openssl and the key):
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:ORB_DB_ENCRYPTION_KEY \\
        -in "$PACKS_FILE" | tar -xf -
    then open each ward-pack.*.latest.html in a browser and print it.
EOF
fi
