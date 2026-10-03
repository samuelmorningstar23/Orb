"""The restore pipeline: scripts/standby-restore.sh, scripts/restore-drill.sh,
and the verifier they share with scripts/backup.sh (scripts/lib/verify_restore.py).

These tests build their OWN SQLCipher databases in tmp_path and treat them as
backups, so they are hermetic: they never read the real backups/ directory,
never need the real ORB_DB_ENCRYPTION_KEY, and cannot damage anything. The one
test that does touch a real encrypted backup is opt-in and read-only, and it
skips unless both the key and a backup are present.

The properties that matter, and why:
  * a bad candidate installs NOTHING — the standby is allowed to be behind, never
    to be corrupt. Verification runs on a scratch copy BEFORE anything is touched;
  * an empty-but-valid database FAILS. That is the zero-byte-backup lesson
    (scripts/backup.sh's header) generalised: "it opened" is not "it has data";
  * a backup that PREDATES THE SCHEMA fails. Added 2026-08-14, and it is the
    reason these tests grew: until then every verifier in the repo asserted row
    counts in four tables — patients, vitals, audit_logs, users — which is the
    entire pre-spine schema. Migrations 20-26 added 62 more (billing, payments,
    claims, procurement, front desk, biomedical, housekeeping, ABDM, workforce,
    dietary, NABH), and a backup taken before migration 20 passed every check
    and printed `RESULT: PASS` while containing none of them.
    test_pre_spine_backup_passes_every_legacy_check_and_still_fails pins exactly
    that: the four old checks all still say PASS on such a file, and only the
    new ones catch it;
  * a broken audit hash chain FAILS. A standby whose chain is broken restores
    into something indistinguishable from a tampered record, which is worse than
    no standby because it would be trusted;
  * the job REFUSES to run where an Orb server is answering. It overwrites
    orb.db; scheduled at 03:30 on the primary it would destroy a day of ward
    charting every night;
  * every run leaves a receipt, success or failure, because a standby that
    silently stopped restoring is a cold standby wearing a warm standby's clothes.

WHAT THESE TESTS DO NOT COVER, stated plainly because a green suite here has
certified a live bug in this repo before:
  * scripts/backup.sh is NOT executed end to end. It snapshots a live database
    through server/scripts/backup_db.py, rotates real files and writes to
    <repo>/backups/, none of which belongs in a unit test. Its *verification
    policy* is covered — the --schema-policy warn behaviour it depends on is
    asserted directly against the shared verifier — but the snapshot, rotation
    and offsite-copy paths remain manually verified;
  * the launchd schedules (scripts/install-*-schedule.sh) are not exercised;
  * the real 200 MB+ encrypted appliance database is only touched by the opt-in
    read-only test at the bottom, which skips in normal runs.
"""
import ast
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "standby-restore.sh"
DRILL = REPO_ROOT / "scripts" / "restore-drill.sh"
VERIFIER = REPO_ROOT / "scripts" / "lib" / "verify_restore.py"
VENV_PY = REPO_ROOT / "server" / ".venv" / "bin" / "python"

TEST_KEY = "standby-test-key-not-the-real-one"

sqlcipher = pytest.importorskip("sqlcipher3", reason="standby restore requires the SQLCipher driver")

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists() or not VENV_PY.exists(),
    reason="needs scripts/standby-restore.sh and the server venv",
)

# The shared verifier is imported directly as well as driven through the shell,
# so its manifest is the same object the fixtures build from and the scripts run.
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
import verify_restore as vr  # noqa: E402

CURRENT_SCHEMA_VERSION = vr.expected_schema_version()

# Both of the files that number came from, digested at COLLECTION time.
#
# Every fixture backup below is built to CURRENT_SCHEMA_VERSION and to
# vr.ALL_MIGRATED_TABLES, both frozen here at import. But each standby-restore.sh
# / restore-drill.sh / verify_restore.py subprocess re-derives them from disk when
# it RUNS — expected_schema_version() deliberately loads server/migrations.py on
# every single call, so that a stale constant can never certify an old backup.
# The cost of that (correct) choice is that the parent and the child can disagree
# if the file moves between them, and in a full-suite run this file executes
# roughly forty minutes after collection.
#
# On 2026-08-15 server/migrations.py was saved mid-run and all sixteen
# script-driven tests here failed at once, every one of them reporting a fixture
# as "a backup that predates the schema" — which is the exact failure this file
# exists to detect, so the report was maximally misleading. Name the real cause.
_MANIFEST_SOURCES = (REPO_ROOT / "server" / "migrations.py", VERIFIER)
_MANIFEST_DIGESTS_AT_COLLECTION = {
    p: hashlib.sha256(p.read_bytes()).hexdigest() for p in _MANIFEST_SOURCES if p.exists()
}


@pytest.fixture(autouse=True)
def _refuse_if_the_schema_manifest_moved():
    def _check(when: str) -> None:
        for path, expected in _MANIFEST_DIGESTS_AT_COLLECTION.items():
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() == expected:
                continue
            pytest.fail(
                f"\n{path.relative_to(REPO_ROOT)} changed on disk {when}.\n"
                f"\n"
                f"The fixture backups in this file were built to schema version "
                f"{CURRENT_SCHEMA_VERSION} and to the table manifest read at collection, "
                f"but every script and verifier subprocess re-derives both from that file "
                f"when it runs. They now disagree, so a perfectly good fixture looks like "
                f"a backup that predates the schema.\n"
                f"\n"
                f"This is a moving working tree, NOT a bad backup and NOT a broken "
                f"restore path — nothing in this run tells you whether the standby is "
                f"healthy. Re-run against a quiescent checkout.\n"
            )

    _check("before this test ran")
    yield
    _check("while this test was running")


def _key_hex(key: str) -> str:
    """Mirror database.py::_sqlcipher_key_hex — an explicit raw key, no KDF."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _make_backup(path: Path, *, key: str = TEST_KEY, rows: int = 4,
                 break_chain: bool = False, pad_to: int = 1_200_000,
                 schema_version: int | None = None, no_ledger: bool = False,
                 drop_tables: tuple[str, ...] = (),
                 empty_reference: tuple[str, ...] = ()) -> Path:
    """Write a SQLCipher database shaped like a real Orb backup.

    Only the tables and columns the verifier reads: the row-count floor
    (patients / vitals / audit_logs / users), the audit hash chain, the
    login-capable-admin check, the `schema_migrations` ledger, and every table
    server/migrations.py creates. `pad_to` inflates the file past the 1 MiB size
    floor the way a real 270 MB database trivially would.

    The knobs exist to build the databases that must FAIL, and each one models a
    real artefact rather than a synthetic one:
      * `schema_version=19` + `drop_tables` — a backup from before the admin
        spine shipped. This is the shape the drill used to certify as PASS;
      * `schema_version` alone — a ledger claiming a version whose tables are
        present, which isolates the version check;
      * `drop_tables` alone — the ledger says current but the tables are gone: a
        half-applied migration, or a hand-repaired file. Isolates the presence
        check, and proves it does not lean on the ledger being honest;
      * `no_ledger` — a file predating the migration framework entirely;
      * `empty_reference` — a table the migration created AND filled, found
        empty. That can only be loss, and is the one post-spine row-count
        assertion that is honest to make.
    """
    from sqlcipher3 import dbapi2 as sq

    conn = sq.connect(str(path))
    conn.execute(f"PRAGMA key = \"x'{_key_hex(key)}'\"")
    conn.executescript(
        """
        CREATE TABLE patients (id TEXT PRIMARY KEY, name TEXT, blob TEXT);
        CREATE TABLE vitals (id INTEGER PRIMARY KEY, patient_id TEXT, hr INTEGER);
        CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT, role TEXT,
                            password_hash TEXT, is_active INTEGER DEFAULT 1);
        CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, prev_hash TEXT, row_hash TEXT);
        """
    )

    # The post-baseline schema: every table any migration in server/migrations.py
    # creates. Built from the verifier's own manifest, which
    # test_manifest_covers_every_migrated_table independently pins to
    # migrations.py — so a fixture that passes is a fixture that is complete.
    for table in vr.ALL_MIGRATED_TABLES:
        if table in drop_tables:
            continue
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, v TEXT)")
    # Rows the migrations themselves write. Absent from `empty_reference` tables,
    # which is what "somebody deleted the document number series" looks like.
    for table in vr.SEEDED_REFERENCE:
        if table in drop_tables or table in empty_reference:
            continue
        conn.execute(f"INSERT INTO {table} (v) VALUES ('seeded-by-migration')")

    if not no_ledger:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, "
            "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        top = CURRENT_SCHEMA_VERSION if schema_version is None else schema_version
        for v in range(1, top + 1):
            conn.execute("INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                         (v, f"m{v}"))

    if rows:
        conn.execute("INSERT INTO users VALUES ('system','Orb Sage','admin','system_hash',1)")
        conn.execute("INSERT INTO users VALUES ('ADM001','Admin','admin','$2b$fake',1)")
        for i in range(rows):
            conn.execute("INSERT INTO patients VALUES (?,?,?)", (f"P{i:03d}", f"Patient {i}", "x" * 64))
            conn.execute("INSERT INTO vitals VALUES (?,?,?)", (i, f"P{i:03d}", 80 + i))
        prev = None
        for i in range(rows):
            row_hash = hashlib.sha256(f"row{i}".encode()).hexdigest()
            conn.execute("INSERT INTO audit_logs VALUES (?,?,?)", (i + 1, prev, row_hash))
            prev = row_hash
        if break_chain:
            conn.execute("UPDATE audit_logs SET prev_hash = 'tampered' WHERE id = 2")
    # Pad past the size floor. A real backup is hundreds of megabytes; the floor
    # exists to catch the zero-byte case, and the fixture must clear it honestly.
    conn.execute("CREATE TABLE padding (id INTEGER PRIMARY KEY, blob BLOB)")
    chunk = os.urandom(64 * 1024)
    for i in range(max(1, pad_to // len(chunk))):
        conn.execute("INSERT INTO padding VALUES (?,?)", (i, chunk))
    conn.commit()
    conn.close()
    # Fold the WAL back in so the single .db file is self-contained, as the
    # online-backup API's output is.
    for side in ("-wal", "-shm"):
        Path(str(path) + side).unlink(missing_ok=True)
    return path


def _run(args, env_extra=None, *, port_free=True):
    """Invoke the script with a hermetic environment."""
    env = dict(os.environ)
    env["ORB_DB_ENCRYPTION_KEY"] = TEST_KEY
    if port_free:
        # Point the primary-guard probe at a port nothing is listening on, so the
        # guard passes honestly rather than being switched off.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            env["ORB_PORT"] = str(s.getsockname()[1])
    env.update(env_extra or {})
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=180,
    )


@pytest.fixture
def standby(tmp_path):
    """A source directory with one good backup, and an empty target."""
    source = tmp_path / "sync"
    source.mkdir()
    _make_backup(source / "orb-backup-20260801-020000.db")
    target = tmp_path / "standby" / "orb.db"
    target.parent.mkdir(parents=True)
    receipt = tmp_path / "ops" / "last-restore.json"
    return {
        "source": source, "target": target, "receipt": receipt,
        "env": {
            "ORB_STANDBY_SOURCE_DIR": str(source),
            "ORB_DB_PATH": str(target),
            "ORB_STANDBY_RECEIPT": str(receipt),
            "ORB_UPLOADS_DIR": str(tmp_path / "uploads"),
        },
    }


# ── The happy path ───────────────────────────────────────────────────────────
def test_restores_a_good_backup_end_to_end(standby):
    result = _run([], standby["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert standby["target"].exists()
    for check in ("decrypt+open", "integrity_check", "audit chain", "admin present"):
        assert check in result.stdout, result.stdout
    # The installed file is the backup, byte for byte.
    src = standby["source"] / "orb-backup-20260801-020000.db"
    assert standby["target"].stat().st_size == src.stat().st_size


def test_the_source_backup_is_never_modified(standby):
    """Verification opens a SCRATCH COPY. Opening a SQLCipher database creates
    -wal/-shm beside it and can write to the file; a drill that mutates the
    artefact it is validating is not a drill — and on a shared sync volume it
    would be mutating the primary's backup."""
    src = standby["source"] / "orb-backup-20260801-020000.db"
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    assert _run([], standby["env"]).returncode == 0
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    assert not (standby["source"] / "orb-backup-20260801-020000.db-wal").exists()


def test_previous_generation_is_kept(standby):
    """A standby that restores a subtly bad night over the only good one has
    made things worse."""
    assert _run([], standby["env"]).returncode == 0
    first = standby["target"].read_bytes()[:64]
    _make_backup(standby["source"] / "orb-backup-20260802-020000.db", rows=6)
    assert _run([], standby["env"]).returncode == 0
    previous = Path(str(standby["target"]) + ".previous")
    assert previous.exists()
    assert previous.read_bytes()[:64] == first


def test_stale_sidecars_are_removed(standby):
    """A -wal left from the database that used to be here describes a different
    file, and SQLite will happily read it."""
    import time
    standby["target"].write_bytes(b"old")
    for side in ("-wal", "-shm"):
        path = Path(str(standby["target"]) + side)
        path.write_bytes(b"stale")
    # Age the -wal past the 5-minute activity window, or the (correct) "something
    # is writing to this database" guard fires on our own fixture.
    old = time.time() - 3600
    os.utime(str(standby["target"]) + "-wal", (old, old))
    assert _run([], standby["env"]).returncode == 0
    assert not Path(str(standby["target"]) + "-wal").exists()
    assert not Path(str(standby["target"]) + "-shm").exists()


def test_check_only_verifies_and_installs_nothing(standby):
    result = _run(["--check-only"], standby["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert not standby["target"].exists()
    assert json.loads(standby["receipt"].read_text())["stage"] == "check_only"


# ── Refusals: nothing is installed ───────────────────────────────────────────
def test_wrong_key_fails_and_installs_nothing(standby, tmp_path):
    _make_backup(standby["source"] / "orb-backup-20260802-020000.db", key="a-different-key")
    result = _run([], standby["env"])
    assert result.returncode == 2
    assert not standby["target"].exists()
    assert "ORB_DB_ENCRYPTION_KEY" in (result.stdout + result.stderr)
    assert json.loads(standby["receipt"].read_text())["ok"] is False


def test_empty_but_valid_database_fails(standby):
    """"It opened" is not "it has data". This is the zero-byte-backup lesson."""
    _make_backup(standby["source"] / "orb-backup-20260802-020000.db", rows=0)
    result = _run([], standby["env"])
    assert result.returncode == 2
    assert "rows:patients" in result.stdout and "FAIL" in result.stdout
    assert not standby["target"].exists()


def test_broken_audit_chain_fails(standby):
    """A restored record whose hash chain does not verify is legally worse than
    a gap, because it looks like tampering."""
    _make_backup(standby["source"] / "orb-backup-20260802-020000.db", break_chain=True)
    result = _run([], standby["env"])
    assert result.returncode == 2
    assert "audit chain" in result.stdout and "broken link" in result.stdout
    assert not standby["target"].exists()


def test_implausibly_small_file_fails_before_anything_opens(standby):
    small = standby["source"] / "orb-backup-20260802-020000.db"
    small.write_bytes(b"not a database")
    result = _run([], standby["env"])
    assert result.returncode == 2
    assert "bytes" in (result.stdout + result.stderr)
    assert not standby["target"].exists()


def test_a_good_previous_standby_survives_a_bad_night(standby):
    """The whole point of verifying before installing."""
    assert _run([], standby["env"]).returncode == 0
    good = standby["target"].read_bytes()
    _make_backup(standby["source"] / "orb-backup-20260802-020000.db", rows=0)
    assert _run([], standby["env"]).returncode == 2
    assert standby["target"].read_bytes() == good, "a failed restore must not disturb the standby"


def test_no_backups_at_all_is_a_precondition_failure(tmp_path):
    empty = tmp_path / "sync"
    empty.mkdir()
    result = _run([], {
        "ORB_STANDBY_SOURCE_DIR": str(empty),
        "ORB_DB_PATH": str(tmp_path / "orb.db"),
        "ORB_STANDBY_RECEIPT": str(tmp_path / "receipt.json"),
    })
    assert result.returncode == 1
    assert "no backups found" in result.stderr


def test_missing_key_refuses_with_the_escrow_warning(standby):
    env = dict(standby["env"])
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        capture_output=True, text=True, timeout=60,
        env={**{k: v for k, v in os.environ.items() if k != "ORB_DB_ENCRYPTION_KEY"}, **env},
    )
    assert result.returncode == 1
    assert "ORB_DB_ENCRYPTION_KEY is not set" in result.stderr


# ── The primary guard ────────────────────────────────────────────────────────
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args):
        pass


def test_refuses_to_run_where_an_orb_is_answering(standby):
    """It overwrites orb.db. On the primary, scheduled nightly, that is a day of
    a ward's charting destroyed and discovered by a nurse."""
    srv = HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = dict(standby["env"])
        env["ORB_PORT"] = str(srv.server_address[1])
        result = _run([], env, port_free=False)
        assert result.returncode == 1
        assert "looks like the PRIMARY" in result.stderr
        assert not standby["target"].exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_refuses_when_the_target_wal_is_being_written(standby):
    """Second, independent guard: a database whose -wal was touched moments ago
    has a writer, whatever is or is not listening on a port."""
    standby["target"].write_bytes(b"live")
    Path(str(standby["target"]) + "-wal").write_bytes(b"active")
    result = _run([], standby["env"])
    assert result.returncode == 1
    assert "something is writing to this database" in result.stderr


def test_refuses_to_restore_a_backup_over_itself(standby):
    src = standby["source"] / "orb-backup-20260801-020000.db"
    env = dict(standby["env"])
    env["ORB_DB_PATH"] = str(src)
    result = _run(["--backup", str(src)], env)
    assert result.returncode == 1
    assert "Refusing" in result.stderr


# ── The receipt, and its consumer ────────────────────────────────────────────
def test_receipt_is_written_on_success_and_on_failure(standby):
    assert _run([], standby["env"]).returncode == 0
    ok = json.loads(standby["receipt"].read_text())
    assert ok["ok"] is True and ok["stage"] == "done"
    assert ok["source_basename"] == "orb-backup-20260801-020000.db"
    assert isinstance(ok["finished_unixtime"], (int, float))

    _make_backup(standby["source"] / "orb-backup-20260802-020000.db", rows=0)
    assert _run([], standby["env"]).returncode == 2
    bad = json.loads(standby["receipt"].read_text())
    assert bad["ok"] is False, "a failed restore must look different from one that never ran"


def test_heartbeat_reads_the_receipt(standby, monkeypatch):
    """End to end: the restore writes a receipt, the heartbeat turns it into a
    number that leaves the building. Without this link a standby that stopped
    restoring is invisible until the primary dies."""
    from services import heartbeat_service as hb

    assert _run([], standby["env"]).returncode == 0
    monkeypatch.setenv("ORB_STANDBY_RECEIPT", str(standby["receipt"]))
    payload = hb.build_payload()
    assert payload["standby"]["receipt_present"] is True
    assert payload["standby"]["last_restore_ok"] is True
    assert payload["standby"]["last_restore_age_s"] is not None
    assert payload["standby"]["last_restore_age_s"] < 300
    assert "standby_stale" not in payload["alerts"]


def test_media_archive_is_restored_alongside_the_database(standby, tmp_path):
    """orb.db holds "see the chest X-ray"; uploads/ holds the X-ray."""
    import tarfile
    uploads_src = tmp_path / "primary" / "uploads"
    uploads_src.mkdir(parents=True)
    (uploads_src / "scan.enc").write_bytes(b"ciphertext")
    tar_path = standby["source"] / "orb-backup-20260801-020000-uploads.tar"
    with tarfile.open(tar_path, "w") as tar:
        tar.add(str(uploads_src), arcname="uploads")

    assert _run([], standby["env"]).returncode == 0
    assert (Path(standby["env"]["ORB_UPLOADS_DIR"]) / "scan.enc").exists()


def test_missing_media_archive_warns_but_still_restores(standby):
    result = _run([], standby["env"])
    assert result.returncode == 0
    assert "images/audio will be MISSING" in result.stderr


# ── Opt-in: a real encrypted backup from this appliance ──────────────────────
# THE CANONICAL BACKUP DIRECTORY. One name, used by the skip guard and the test,
# so neither can drift onto a different directory from the scripts.
#
# This used to read `(REPO_ROOT / "server" / "backups", REPO_ROOT / "backups")`
# and return from the FIRST directory that held any files — server/backups won on
# both counts, and it was the stale one. Five schema-v4 artefacts from 2026-08-01
# sat there (written by server/scripts/backup_db.py, whose default out-dir was
# `<db parent>/backups`) while every real nightly backup landed in <repo>/backups.
# So this drill certified a file that predated 28 migrations, and would have gone
# on doing so for as long as those files existed. Not a hypothetical: F4 in
# docs/INSTALL_RUNBOOK.md walks an operator backwards through generations, and
# this is the directory they would have been walking.
REAL_BACKUP_DIR = REPO_ROOT / "backups"


def _real_backup() -> Path | None:
    """Newest real backup in the canonical directory, or None."""
    if not REAL_BACKUP_DIR.is_dir():
        return None
    files = sorted(REAL_BACKUP_DIR.glob("orb-backup-*.db"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


@pytest.mark.skipif(
    not os.getenv("ORB_DB_ENCRYPTION_KEY") or _real_backup() is None,
    reason="needs the real ORB_DB_ENCRYPTION_KEY and a real backup on disk",
)
def test_real_encrypted_backup_verifies(tmp_path):
    """Read-only drill against this appliance's newest real backup.

    --check-only, so it opens a scratch copy and installs nothing: the files in
    <repo>/backups/ are production artefacts and are never written to.

    Note what is NOT passed: --backup. The script is left to run its own
    selection against ORB_STANDBY_SOURCE_DIR, because selection is the part that
    broke — the previous version handed the script an explicit path, so the one
    behaviour the nightly standby actually depends on ("pick the right file out
    of the right directory") was the one behaviour never exercised. The chosen
    file is asserted below.
    """
    newest = _real_backup()
    result = _run(
        ["--check-only"],
        {
            "ORB_DB_ENCRYPTION_KEY": os.environ["ORB_DB_ENCRYPTION_KEY"],
            "ORB_STANDBY_SOURCE_DIR": str(REAL_BACKUP_DIR),
            "ORB_DB_PATH": str(tmp_path / "unused.db"),
            "ORB_STANDBY_RECEIPT": str(tmp_path / "receipt.json"),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verify PASSED" in result.stdout
    assert not (tmp_path / "unused.db").exists()

    # The script picked the newest file in the canonical directory, not merely
    # *a* file that happened to verify.
    assert f"Source   : {newest}" in result.stdout, result.stdout

    # And it judged it against the schema this checkout ships. A backup that
    # predates a migration must fail here; --schema-policy fail is what the
    # standby installs under, so a PASS means this file is genuinely installable.
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["ok"] is True and receipt["stage"] == "check_only"
    assert receipt["source_basename"] == newest.name


# ═════════════════════════════════════════════════════════════════════════════
# Schema completeness — the 2026-08-14 silent-loss class
# ═════════════════════════════════════════════════════════════════════════════
# Everything above this line was already true on 2026-08-13, and a backup taken
# before migration 20 satisfied all of it. These are the checks that close that.

# The 62 tables migrations 20-26 added: the hospital admin spine and the modules
# built on it. Named by module group so the failure message says "billing" and
# not a list of table names.
ADMIN_SPINE_GROUPS = (
    "billing", "payments", "claims", "procurement", "front desk",
    "biomedical", "housekeeping", "abdm", "workforce", "dietary", "nabh",
)
ADMIN_SPINE_TABLES = tuple(
    t for group in ADMIN_SPINE_GROUPS for t in vr.MODULE_TABLES[group]
)

drill_only = pytest.mark.skipif(not DRILL.exists() or not VERIFIER.exists(),
                                reason="needs restore-drill.sh and the shared verifier")


def _run_drill(backup: Path, tmp_path: Path, env_extra=None):
    """Drive scripts/restore-drill.sh over a fixture backup.

    ORB_DB_PATH points at a file that does not exist, so the drill's
    refuse-to-touch-the-live-database guard is satisfied honestly rather than
    bypassed, and nothing in the real backups/ directory is read.
    """
    env = dict(os.environ)
    env["ORB_DB_ENCRYPTION_KEY"] = TEST_KEY
    env["ORB_BACKUP_DIR"] = str(backup.parent)
    env["ORB_DB_PATH"] = str(tmp_path / "no-such-live.db")
    env.pop("ORB_BACKUP_REQUIRE_NONEMPTY", None)
    env.update(env_extra or {})
    scratch = tmp_path / "drill-scratch"
    scratch.mkdir(exist_ok=True)
    return subprocess.run(
        ["/bin/bash", str(DRILL), "--backup", str(backup), "--scratch-dir", str(scratch)],
        capture_output=True, text=True, env=env, timeout=180,
    )


def _run_verifier(db: Path, *args, env_extra=None):
    """Drive scripts/lib/verify_restore.py directly — the level scripts/backup.sh
    calls it at, which is otherwise unreachable from a test (backup.sh itself
    snapshots a live database and rotates real files)."""
    env = dict(os.environ)
    env["ORB_DB_ENCRYPTION_KEY"] = TEST_KEY
    env.pop("ORB_BACKUP_REQUIRE_NONEMPTY", None)
    env.update(env_extra or {})
    return subprocess.run(
        [str(VENV_PY), str(VERIFIER), "--db", str(db), *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


def _pre_spine_backup(path: Path) -> Path:
    """A backup as it existed before migration 20: no spine tables, ledger at 19.

    This is not a synthetic mutation — it is the exact shape of every backup this
    appliance wrote before the admin spine shipped, and of any offsite copy that
    has not been refreshed since.
    """
    return _make_backup(path, schema_version=19, drop_tables=ADMIN_SPINE_TABLES)


# ── The manifest cannot silently fall behind the schema ──────────────────────
def _tables_created_by_migrations() -> set[str]:
    """Re-derive, from migrations.py's AST, every table a migration creates."""
    src = (REPO_ROOT / "server" / "migrations.py").read_text()
    tree = ast.parse(src)
    migration_fns = {
        node.args[2].id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Migration"
    }
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    consts: dict[str, ast.stmt] = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
            consts[n.targets[0].id] = n
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            consts[n.target.id] = n
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z_0-9]*)", re.I
    )
    found: set[str] = set()
    for name in migration_fns:
        segment = ast.get_source_segment(src, funcs[name]) or ""
        found |= set(pattern.findall(segment))
        # The bulk DDL lives in module-level constants (_M20_TABLES and friends)
        # that the migration functions iterate over.
        for const_name, node in consts.items():
            if re.search(r"\b" + re.escape(const_name) + r"\b", segment):
                found |= set(pattern.findall(ast.get_source_segment(src, node) or ""))
    return found - {"schema_migrations"}


def test_manifest_covers_every_migrated_table():
    """The verifier's manifest is explicit data; this is what stops it rotting.

    A new migration that adds a table without adding it here would leave that
    table unprotected by every backup path at once — the same hole migrations
    20-26 sat in for three weeks. Failing here is the cheap version of finding
    out on restore day.
    """
    derived = _tables_created_by_migrations()
    manifest = set(vr.ALL_MIGRATED_TABLES)
    missing = derived - manifest
    assert not missing, (
        "server/migrations.py creates tables that scripts/lib/verify_restore.py "
        f"does not verify: {sorted(missing)}. Add them to MODULE_TABLES."
    )
    stale = manifest - derived
    assert not stale, (
        f"verify_restore.py's manifest lists tables no migration creates: {sorted(stale)}"
    )
    assert len(ADMIN_SPINE_TABLES) == 62, (
        "migrations 20-26 are documented everywhere as adding 62 tables; the "
        f"manifest now groups {len(ADMIN_SPINE_TABLES)} of them into the spine"
    )


# ── The drill ────────────────────────────────────────────────────────────────
@drill_only
def test_drill_passes_a_complete_backup(tmp_path):
    backup = _make_backup(tmp_path / "orb-backup-20260814-020000.db")
    result = _run_drill(backup, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout
    # Derived, not a literal. A hand-typed count has to be edited by every
    # migration that adds a table, and the edit is invisible in review — which
    # is how a drill assertion silently stops covering the newest module. 88
    # since migration 29 added the three sage_assessment* tables.
    assert "module tables" in result.stdout
    assert f"all {len(vr.ALL_MIGRATED_TABLES)} present" in result.stdout


@drill_only
def test_drill_fails_a_backup_that_predates_the_admin_spine(tmp_path):
    """THE defect. This file used to print RESULT: PASS.

    Restoring it brings the appliance back with no invoices, no payments, no
    claim history, no stock, no incident register — and the artefact hospital IT
    signed off on said it was good.
    """
    backup = _pre_spine_backup(tmp_path / "orb-backup-20260701-020000.db")
    result = _run_drill(backup, tmp_path)
    assert result.returncode == 2
    assert "RESULT: PASS" not in result.stdout
    assert "RESULT: FAIL" in (result.stdout + result.stderr)
    assert "schema version" in result.stdout
    assert "v19" in result.stdout and f"v{CURRENT_SCHEMA_VERSION}" in result.stdout
    assert "module tables" in result.stdout
    # The operator is told which modules, not just that a count is wrong.
    assert "billing" in result.stdout and "nabh" in result.stdout


@drill_only
def test_pre_spine_backup_passes_every_legacy_check_and_still_fails(tmp_path):
    """The regression guard proper: pin that the OLD checks are still satisfied.

    If a future edit weakens the new checks, this test must not be rescued by
    the old ones — so it asserts positively that decrypt+open, integrity_check,
    the four clinical row floors and the audit chain ALL say PASS on this file.
    That was the entire verifier on 2026-08-13, and it is why the drill said PASS.
    """
    backup = _pre_spine_backup(tmp_path / "orb-backup-20260701-020000.db")
    result = _run_drill(backup, tmp_path)
    out = result.stdout
    for legacy in ("decrypt+open", "integrity_check", "rows:patients", "rows:vitals",
                   "rows:audit_logs", "rows:users", "audit chain"):
        line = next((l for l in out.splitlines() if legacy in l), None)
        assert line is not None, f"{legacy} was not checked at all:\n{out}"
        assert "PASS" in line, f"expected the legacy check {legacy} to still pass: {line}"
    # Only the new checks object.
    assert result.returncode == 2
    assert "RESULT: FAIL" in result.stderr
    assert "VERIFY FAILED" in out
    failed = out.rsplit("VERIFY FAILED", 1)[1]
    assert "schema version" in failed and "module tables" in failed
    assert "rows:" not in failed and "audit chain" not in failed


@drill_only
def test_drill_fails_when_tables_are_missing_though_the_ledger_says_current(tmp_path):
    """The presence check must not lean on the ledger being honest — a
    half-applied migration records nothing, and a hand-repaired file records
    whatever it was told to."""
    backup = _make_backup(tmp_path / "orb-backup-20260814-020000.db",
                          drop_tables=("invoices", "invoice_lines", "payments"))
    result = _run_drill(backup, tmp_path)
    assert result.returncode == 2
    assert "schema version" in result.stdout and "matching this build" in result.stdout
    assert "module tables" in result.stdout and "MISSING" in result.stdout
    assert "invoices" in result.stdout


@drill_only
def test_drill_fails_when_the_migration_ledger_is_absent(tmp_path):
    backup = _make_backup(tmp_path / "orb-backup-20250101-020000.db", no_ledger=True)
    result = _run_drill(backup, tmp_path)
    assert result.returncode == 2
    assert "predates the migration framework" in result.stdout


@drill_only
def test_drill_fails_when_migration_seeded_reference_rows_are_gone(tmp_path):
    """document_number_series is filled BY migrations 20/25/26. Zero rows there
    cannot mean "not in use yet" — it can only mean the rows were lost, and a
    restore would come back unable to number an invoice."""
    backup = _make_backup(tmp_path / "orb-backup-20260814-020000.db",
                          empty_reference=("document_number_series",))
    result = _run_drill(backup, tmp_path)
    assert result.returncode == 2
    assert "seeded reference" in result.stdout
    assert "document_number_series" in result.stdout


@drill_only
def test_drill_does_not_send_the_operator_further_back_on_a_too_old_backup(tmp_path):
    """F4 in the install runbook walks BACKWARDS through generations until one
    passes. On a schema failure that advice makes things worse, so the drill says
    so in the same breath."""
    backup = _pre_spine_backup(tmp_path / "orb-backup-20260701-020000.db")
    _make_backup(tmp_path / "orb-backup-20260601-020000.db")   # an older sibling
    result = _run_drill(backup, tmp_path)
    assert result.returncode == 2
    assert "walking further" in result.stderr and "worse" in result.stderr


# ── The standby ──────────────────────────────────────────────────────────────
def test_standby_refuses_a_candidate_that_predates_the_schema(standby):
    """The standby is allowed to be BEHIND — by a night. Not by seven migrations:
    that is a switchover into an appliance with no administrative spine."""
    _pre_spine_backup(standby["source"] / "orb-backup-20260802-020000.db")
    result = _run([], standby["env"])
    assert result.returncode == 2
    assert "schema version" in result.stdout and "FAIL" in result.stdout
    assert not standby["target"].exists()
    assert json.loads(standby["receipt"].read_text())["ok"] is False


def test_a_good_standby_survives_a_pre_spine_candidate(standby):
    """Same property as test_a_good_previous_standby_survives_a_bad_night, for
    the new failure class: the installed database must not be replaced by an
    older-schema one."""
    assert _run([], standby["env"]).returncode == 0
    good = standby["target"].read_bytes()
    _pre_spine_backup(standby["source"] / "orb-backup-20260802-020000.db")
    assert _run([], standby["env"]).returncode == 2
    assert standby["target"].read_bytes() == good


def test_standby_refuses_a_candidate_missing_module_tables(standby):
    _make_backup(standby["source"] / "orb-backup-20260802-020000.db",
                 drop_tables=("nabh_incidents", "claims"))
    result = _run([], standby["env"])
    assert result.returncode == 2
    assert "module tables" in result.stdout and "MISSING" in result.stdout
    assert not standby["target"].exists()


# ── The verifier's policies, at the level scripts/backup.sh uses ─────────────
def test_backup_policy_keeps_a_snapshot_of_a_behind_schema_database(tmp_path):
    """scripts/backup.sh runs --schema-policy warn, and that difference is
    deliberate. It is not judging a restore candidate; it is deciding whether to
    keep a snapshot of the LIVE database. A snapshot of a behind-schema live
    database is still the best copy of that database in existence, and failing
    would mean no backup kept AND no rotation run — a new way to have nothing."""
    db = _pre_spine_backup(tmp_path / "orb-backup-20260701-020000.db")
    warned = _run_verifier(db, "--prefix", "backup", "--schema-policy", "warn")
    assert warned.returncode == 0, warned.stdout + warned.stderr
    assert "WARN" in warned.stdout
    assert "schema version" in warned.stdout

    # And the summary must not overclaim. A closing line reading "schema
    # complete" on a file that just warned about its schema would be the same
    # species of lie this verifier exists to stop telling.
    assert "PASSED WITH WARNINGS" in warned.stdout
    assert "schema complete" not in warned.stdout

    # ... while the same file, judged as a restore candidate, fails.
    strict = _run_verifier(db, "--prefix", "drill", "--schema-policy", "fail")
    assert strict.returncode == 2


def test_backup_policy_still_fails_on_emptied_reference_rows(tmp_path):
    """The warn policy forgives a schema that is BEHIND. It must not forgive a
    table that the migration created and filled being found empty — that is a
    live database with a problem tonight, not a stale artefact."""
    db = _make_backup(tmp_path / "orb-backup-20260814-020000.db",
                      empty_reference=("duty_shift_types",))
    result = _run_verifier(db, "--prefix", "backup", "--schema-policy", "warn")
    assert result.returncode == 2
    assert "seeded reference" in result.stdout and "duty_shift_types" in result.stdout


def test_backup_policy_still_fails_on_an_empty_database(tmp_path):
    """The zero-byte-backup lesson is not weakened by the warn policy."""
    db = _make_backup(tmp_path / "orb-backup-20260814-020000.db", rows=0)
    assert _run_verifier(db, "--prefix", "backup", "--schema-policy", "warn").returncode == 2


# ── Honesty about module data ────────────────────────────────────────────────
def test_empty_module_data_is_reported_not_judged(tmp_path):
    """A module with no rows is exactly what a correct backup looks like before
    that module goes live, and exactly what a catastrophic one looks like after.
    Nothing in the file distinguishes them, so the verifier prints the counts and
    names the ambiguity instead of guessing."""
    db = _make_backup(tmp_path / "orb-backup-20260814-020000.db")
    result = _run_verifier(db, "--prefix", "drill")
    assert result.returncode == 0, result.stdout
    assert "Module data" in result.stdout
    assert "EMPTY" in result.stdout
    assert "REAL LOSS if it has" in result.stdout
    assert "ORB_BACKUP_REQUIRE_NONEMPTY" in result.stdout


def test_a_site_can_promote_a_live_module_to_a_hard_check(tmp_path):
    """Once billing is live, "billing: EMPTY" is a line of text nobody reads at
    03:30. This is how it becomes an exit code."""
    db = _make_backup(tmp_path / "orb-backup-20260814-020000.db")
    result = _run_verifier(db, "--prefix", "drill",
                           env_extra={"ORB_BACKUP_REQUIRE_NONEMPTY": "billing,payments"})
    assert result.returncode == 2
    assert "required:billing" in result.stdout
    assert "declared the module LIVE" in result.stdout


def test_promoting_a_module_passes_once_it_holds_rows(tmp_path):
    db = _make_backup(tmp_path / "orb-backup-20260814-020000.db")
    from sqlcipher3 import dbapi2 as sq
    conn = sq.connect(str(db))
    conn.execute(f"PRAGMA key = \"x'{_key_hex(TEST_KEY)}'\"")
    conn.execute("INSERT INTO invoices (v) VALUES ('INV-1')")
    conn.commit()
    conn.close()
    for side in ("-wal", "-shm"):
        Path(str(db) + side).unlink(missing_ok=True)
    result = _run_verifier(db, "--prefix", "drill",
                           env_extra={"ORB_BACKUP_REQUIRE_NONEMPTY": "billing"})
    assert result.returncode == 0, result.stdout
    assert "required:billing" in result.stdout and "1 rows" in result.stdout


def test_unknown_module_group_is_an_error_not_a_silent_pass(tmp_path):
    """A typo in ORB_BACKUP_REQUIRE_NONEMPTY must not quietly disable the check
    the site went out of its way to turn on."""
    db = _make_backup(tmp_path / "orb-backup-20260814-020000.db")
    result = _run_verifier(db, "--prefix", "drill",
                           env_extra={"ORB_BACKUP_REQUIRE_NONEMPTY": "billling"})
    assert result.returncode == 2
    assert "not a module group" in result.stdout


# ═════════════════════════════════════════════════════════════════════════════
# One backup directory — the 2026-08-15 diverged-directories class
# ═════════════════════════════════════════════════════════════════════════════
# Five places resolved "where do backups live?" independently and split two ways:
# the shell scripts said <repo>/backups, the Python said <db parent>/backups
# (= server/backups on the stock layout). Both directories existed for a
# fortnight. The nightly job wrote to one; the restore drill, the admin "create
# backup" button and the ops status page read the other.
#
# The consequences were all of the silent kind. A drill certified a schema-v4
# backup that predated 28 migrations and printed PASS. A backup taken from the
# admin UI landed where nothing that restores would ever look at it. The status
# page graded a directory nothing writes to — which can read as "no backup at
# all" with last night's backup one directory up, or, worse, as GREEN because
# someone once clicked the button, while the real directory rotted.
#
# So the agreement is asserted rather than commented. Each resolver is invoked
# the way its own callers invoke it, in a subprocess with a clean environment,
# because most of them read os.environ at import time.

BACKUP_DIR_RESOLVERS = {
    # symbol/callable -> the snippet that evaluates it from a clean interpreter
    "config.BACKUP_DIR":
        "import config; print(config.BACKUP_DIR)",
    "routes.backup_routes._out_dir":
        "from routes import backup_routes; print(backup_routes._out_dir())",
    "services.ops_health_service._backup_dir":
        "from services import ops_health_service as m; print(m._backup_dir())",
    "services.heartbeat_service._backup_dir":
        "from services import heartbeat_service as m; print(m._backup_dir())",
    "scripts.backup_db._resolve_out_dir":
        "import importlib.util, pathlib;"
        "spec = importlib.util.spec_from_file_location('bdb', 'scripts/backup_db.py');"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "print(m._resolve_out_dir(pathlib.Path('orb.db'), None))",
}


def _eval_in_server(snippet: str, env_extra=None) -> str:
    """Run a snippet with server/ as cwd and on sys.path, as the app runs."""
    env = dict(os.environ)
    env.pop("ORB_BACKUP_DIR", None)
    env.pop("ORB_DB_PATH", None)
    env.update(env_extra or {})
    result = subprocess.run(
        [str(VENV_PY), "-c", snippet],
        capture_output=True, text=True, timeout=120,
        cwd=str(REPO_ROOT / "server"), env=env,
    )
    assert result.returncode == 0, f"{snippet}\n{result.stdout}{result.stderr}"
    return result.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("name,snippet", sorted(BACKUP_DIR_RESOLVERS.items()))
def test_every_backup_dir_resolver_agrees(name, snippet):
    """Every resolver in the repo points at <repo>/backups, with no env set."""
    assert Path(_eval_in_server(snippet)).resolve() == REAL_BACKUP_DIR.resolve(), (
        f"{name} resolved somewhere other than {REAL_BACKUP_DIR}. All five "
        f"resolvers must agree — see config.BACKUP_DIR."
    )


@pytest.mark.parametrize("name,snippet", sorted(BACKUP_DIR_RESOLVERS.items()))
def test_orb_backup_dir_overrides_every_resolver(name, snippet, tmp_path):
    """ORB_BACKUP_DIR is the one documented escape hatch, so it must reach all
    of them. A resolver that ignores it sends an operator who relocated their
    backups to a directory they believe they stopped using."""
    override = tmp_path / "elsewhere"
    override.mkdir()
    got = _eval_in_server(snippet, env_extra={"ORB_BACKUP_DIR": str(override)})
    assert Path(got).resolve() == override.resolve(), f"{name} ignored ORB_BACKUP_DIR"


def test_moving_the_database_does_not_move_the_backup_dir(tmp_path):
    """ORB_DB_PATH must not drag the backup directory with it.

    This is the exact mechanism that split the two directories: resolvers
    anchored to `DB_PATH.parent / "backups"` follow the database wherever it
    goes, so pointing ORB_DB_PATH at a temp file (which every test does, and
    which a relocated appliance does permanently) silently relocated where
    backups were written and read. Anchoring to the repo root instead means the
    two decisions are independent, which is what an operator already assumes.
    """
    relocated = tmp_path / "elsewhere" / "orb.db"
    relocated.parent.mkdir(parents=True)
    for name, snippet in sorted(BACKUP_DIR_RESOLVERS.items()):
        got = _eval_in_server(snippet, env_extra={"ORB_DB_PATH": str(relocated)})
        assert Path(got).resolve() == REAL_BACKUP_DIR.resolve(), (
            f"{name} followed ORB_DB_PATH to {got}"
        )


def test_no_stale_backup_directory_shadows_the_canonical_one():
    """server/backups/ must not exist with backups in it.

    Nothing writes there any more, so anything found is a pre-2026-08-15
    artefact — and every one of them predates the admin spine. The failure this
    guards is not "a stale file exists" but "a stale file is SELECTABLE": an
    operator following the walk-backwards-through-generations procedure in
    docs/INSTALL_RUNBOOK.md F4, or a future resolver that regresses to
    <db parent>/backups, lands on a schema-v4 database that verifies as a
    perfectly good pre-spine backup.
    """
    stale = sorted((REPO_ROOT / "server" / "backups").glob("orb-backup-*.db"))
    assert not stale, (
        "server/backups/ holds backup artefacts:\n  "
        + "\n  ".join(p.name for p in stale)
        + "\nThis directory is not written to any more. Move these out of the "
          "search path (see stale-backups/README.md) or delete them — a restore "
          "drill must not be able to select them."
    )
