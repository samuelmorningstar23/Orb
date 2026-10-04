"""Migration 51: the patient movement ledger as a numbered migration (adt-2).

What this defends:
  * the three shapes the live table can be in (absent, the front desk's old
    lazy shape, the new shape) all end in one new shape, and a second run
    changes nothing;
  * the old-shape rebuild keeps every row, every value and every ROWID: the
    front desk's history and the bed-day billing break ties on rowid, so a copy
    that renumbered rows would silently reorder two movements recorded in the
    same instant;
  * the movement vocabulary is enforced by triggers, on INSERT and on UPDATE,
    and refuses an unknown type;
  * the bed-day billing reprices a day only from a move between wards, so the
    admission, leave and bed-link rows the ADT work writes keep the old prices;
  * the front desk's API returns movements exactly as before.
"""
import sqlite3
import uuid

import pytest

import migrations
from database import db_session
from db_backend import DBIntegrityError
from migrations_his import m0051_adt_movement_ledger as m51
from services import billing_masters, billing_service
from services import frontdesk_service as fd
from services.money import rupees

# The front desk's lazy CREATE as it stood before migration 51 (3 October 2026).
OLD_LEDGER_DDL = """CREATE TABLE patient_movements (
        id TEXT PRIMARY KEY,
        patient_id TEXT NOT NULL,
        admission_id TEXT,
        movement_type TEXT NOT NULL DEFAULT 'transfer' CHECK (movement_type IN (
            'transfer','admission','discharge')),
        from_department TEXT,
        from_ward TEXT,
        from_bed TEXT,
        to_department TEXT,
        to_ward TEXT,
        to_bed TEXT,
        reason TEXT NOT NULL,
        moved_at TIMESTAMP NOT NULL,
        moved_by TEXT,
        moved_by_name TEXT,
        notes TEXT,
        idempotency_key TEXT
    )"""
OLD_INDEXES = (
    "CREATE INDEX idx_movements_patient ON patient_movements(patient_id, moved_at DESC)",
    "CREATE INDEX idx_movements_admission ON patient_movements(admission_id, moved_at DESC)",
    "CREATE UNIQUE INDEX idx_movements_idempotency ON patient_movements(idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
)


def _schema(conn) -> list:
    return sorted(tuple(r) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall())


def _apply_in_runner_txn(conn):
    """Exactly as run_pending_migrations holds it: inside BEGIN IMMEDIATE."""
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    m51.apply(conn)
    assert conn.in_transaction
    conn.execute("COMMIT")


def _old_shape_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(OLD_LEDGER_DDL)
    for statement in OLD_INDEXES:
        conn.execute(statement)
    # Rowids chosen out of insertion order, and two pairs of movements in the
    # same instant, so only an explicit rowid copy keeps the tiebreak order.
    rows = [
        (7, "MOV-A", "P1", "ADM1", "transfer", "Cardiology", "CCU", "C-1",
         "Medicine", "GEN", "G-4", "Stepped down", "2026-08-12T08:30:00+00:00",
         "D1", "Dr A", None, "key-a"),
        (3, "MOV-B", "P1", "ADM1", "transfer", "Medicine", "GEN", "G-4",
         "Cardiology", "CCU", "C-2", "Back up", "2026-08-12T08:30:00+00:00",
         "D1", "Dr A", "same instant as A", None),
        (12, "MOV-C", "P2", "ADM2", "admission", None, None, None,
         "Surgery", "SUR", "S-1", "Admitted", "2026-08-11T02:00:00+00:00",
         None, None, None, "key-c"),
        (5, "MOV-D", "P2", "ADM2", "discharge", "Surgery", "SUR", "S-1",
         None, None, None, "Home", "2026-08-11T02:00:00+00:00",
         None, None, None, None),
    ]
    conn.executemany(
        "INSERT INTO patient_movements (rowid, id, patient_id, admission_id, movement_type, "
        "from_department, from_ward, from_bed, to_department, to_ward, to_bed, reason, "
        "moved_at, moved_by, moved_by_name, notes, idempotency_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return conn


def _legacy_projection(conn):
    cols = ", ".join(m51.LEGACY_COLUMNS)
    return conn.execute(
        f"SELECT rowid, {cols} FROM patient_movements ORDER BY rowid").fetchall()


# ── The three shapes ─────────────────────────────────────────────────────────

def test_absent_table_is_created_in_the_new_shape_and_a_rerun_is_a_no_op(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "absent.db"))
    _apply_in_runner_txn(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(patient_movements)")]
    assert cols == list(m51.CANONICAL_COLUMNS)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE tbl_name='patient_movements' "
        "AND type IN ('index', 'trigger') AND name NOT LIKE 'sqlite_%'")}
    assert names == set(m51.INDEX_NAMES) | set(m51.TRIGGER_NAMES)
    assert m51.is_new_shape(conn)
    before = _schema(conn)
    _apply_in_runner_txn(conn)
    assert _schema(conn) == before
    conn.close()


def test_old_shape_with_rows_keeps_every_row_value_and_rowid(tmp_path):
    conn = _old_shape_db(tmp_path / "old.db")
    before = _legacy_projection(conn)
    history_before = conn.execute(
        "SELECT id FROM patient_movements WHERE patient_id = 'P1' "
        "ORDER BY moved_at DESC, rowid DESC").fetchall()
    billing_before = conn.execute(
        "SELECT id FROM patient_movements WHERE admission_id = 'ADM2' "
        "ORDER BY julianday(moved_at) ASC, rowid ASC").fetchall()

    _apply_in_runner_txn(conn)

    assert m51.is_new_shape(conn)
    assert [r[1] for r in conn.execute("PRAGMA table_info(patient_movements)")] == list(
        m51.CANONICAL_COLUMNS)
    assert _legacy_projection(conn) == before, "a row, a value or a rowid changed"
    assert conn.execute(
        "SELECT id FROM patient_movements WHERE patient_id = 'P1' "
        "ORDER BY moved_at DESC, rowid DESC").fetchall() == history_before
    # MOV-A holds the higher rowid (7 against 3), so it leads the same-instant
    # tie. The exact rowids themselves are compared in _legacy_projection above,
    # which a renumbering copy (1, 2, 3, 4) fails.
    assert history_before == [("MOV-A",), ("MOV-B",)]
    assert conn.execute(
        "SELECT id FROM patient_movements WHERE admission_id = 'ADM2' "
        "ORDER BY julianday(moved_at) ASC, rowid ASC").fetchall() == billing_before
    # Every new column is NULL on a copied row.
    for col in m51.ADDED_COLUMNS:
        assert conn.execute(
            f"SELECT COUNT(*) FROM patient_movements WHERE {col} IS NOT NULL").fetchone()[0] == 0
    # The idempotency index survived and still refuses a reused key.
    with pytest.raises(DBIntegrityError):
        conn.execute(
            "INSERT INTO patient_movements (id, patient_id, movement_type, reason, moved_at, "
            "idempotency_key) VALUES ('MOV-X', 'P1', 'transfer', 'r', '2026-08-13', 'key-a')")
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='patient_movements_aside'"
                        ).fetchone() is None
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # Second run: nothing changes.
    snapshot = _schema(conn)
    _apply_in_runner_txn(conn)
    assert _schema(conn) == snapshot
    assert _legacy_projection(conn) == before
    conn.close()


def test_new_shape_is_left_alone(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "new.db"))
    _apply_in_runner_txn(conn)
    conn.execute(
        "INSERT INTO patient_movements (id, patient_id, movement_type, reason, moved_at, "
        "to_bed_id, recorded_at) VALUES ('MOV-N', 'P1', 'bed_assigned', 'r', "
        "'2026-08-13T00:00:00+00:00', 'HKU-1', '2026-08-13T00:00:01+00:00')")
    rowid = conn.execute("SELECT rowid FROM patient_movements WHERE id='MOV-N'").fetchone()[0]
    _apply_in_runner_txn(conn)
    assert conn.execute(
        "SELECT rowid, to_bed_id FROM patient_movements WHERE id='MOV-N'").fetchone() == (
        rowid, "HKU-1")
    conn.close()


def test_an_unknown_live_column_aborts_the_rebuild_before_anything_changes(tmp_path):
    conn = _old_shape_db(tmp_path / "odd.db")
    conn.execute("ALTER TABLE patient_movements ADD COLUMN site_note TEXT")
    conn.commit()
    before = _schema(conn)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError, match="site_note"):
        m51.apply(conn)
    conn.execute("ROLLBACK")
    assert _schema(conn) == before
    conn.close()


def test_an_unknown_index_aborts_the_rebuild(tmp_path):
    conn = _old_shape_db(tmp_path / "idx.db")
    conn.execute("CREATE INDEX idx_site_own ON patient_movements(moved_by)")
    conn.commit()
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError, match="idx_site_own"):
        m51.apply(conn)
    conn.execute("ROLLBACK")
    conn.close()


def test_the_lazy_fallback_never_rebuilds_an_old_shape(tmp_path):
    conn = _old_shape_db(tmp_path / "lazy.db")
    before = _schema(conn)
    m51.ensure_ledger_on(conn, rebuild=False)
    conn.commit()
    assert _schema(conn) == before
    assert not m51.is_new_shape(conn)
    conn.close()


def test_the_rebuild_refuses_to_run_in_autocommit(tmp_path):
    conn = _old_shape_db(tmp_path / "auto.db")
    conn.isolation_level = None
    assert not conn.in_transaction
    with pytest.raises(RuntimeError, match="open transaction"):
        m51.apply(conn)
    assert not m51.is_new_shape(conn)
    conn.close()


# ── The vocabulary trigger ───────────────────────────────────────────────────

def test_every_listed_type_is_accepted_and_an_unknown_type_is_refused(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "vocab.db"))
    _apply_in_runner_txn(conn)
    for i, kind in enumerate(m51.MOVEMENT_TYPES):
        conn.execute(
            "INSERT INTO patient_movements (id, patient_id, movement_type, reason, moved_at) "
            "VALUES (?, 'P1', ?, 'r', '2026-08-13')", (f"MOV-{i}", kind))
    with pytest.raises(DBIntegrityError, match="movement_type"):
        conn.execute(
            "INSERT INTO patient_movements (id, patient_id, movement_type, reason, moved_at) "
            "VALUES ('MOV-T', 'P1', 'teleport', 'r', '2026-08-13')")
    with pytest.raises(DBIntegrityError, match="movement_type"):
        conn.execute("UPDATE patient_movements SET movement_type = 'teleport' WHERE id = 'MOV-0'")
    # The default still applies when a writer names no type (the front desk does).
    conn.execute(
        "INSERT INTO patient_movements (id, patient_id, reason, moved_at) "
        "VALUES ('MOV-DEF', 'P1', 'r', '2026-08-13')")
    assert conn.execute(
        "SELECT movement_type FROM patient_movements WHERE id='MOV-DEF'").fetchone()[0] == "transfer"
    conn.close()


def test_the_session_database_has_the_new_ledger_and_its_trigger():
    with db_session() as db:
        assert m51.is_new_shape(db)
        assert 51 in {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
        with pytest.raises(Exception, match="movement_type"):
            db.execute(
                "INSERT INTO patient_movements (id, patient_id, movement_type, reason, moved_at) "
                "VALUES (?, 'P1', 'teleport', 'r', '2026-08-13')", (f"MOV-{uuid.uuid4().hex[:8]}",))


def test_the_migration_ledger_has_no_duplicate_version():
    versions = [m.version for m in migrations.MIGRATIONS]
    assert len(versions) == len(set(versions))
    names = {m.version: m.name for m in migrations.MIGRATIONS}
    assert names[50] == "registration_opd_foundation"
    assert names[51] == "adt_movement_ledger"
    assert names[52] == "patients_status_registered"


def test_the_front_desk_and_the_migration_share_one_ddl():
    assert fd.ensure_ledger_on is m51.ensure_ledger_on
    assert fd._MOVEMENT_COLUMNS_NOT_IN_API == frozenset(m51.ADDED_COLUMNS)


# ── Billing reprices only from moves between wards ──────────────────────────

def _bed_patient_and_admission(ward: str) -> tuple[str, str]:
    pid = f"PLED{uuid.uuid4().hex[:8].upper()}"
    aid = f"ADMLED{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, status) "
            "VALUES (?, 'Ledger Bed Day', ?, ?, '3', 'discharged')", (pid, f"MRN-{pid}", ward))
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('LEDDOC', 'Dr Ledger', 'doctor', 'x')")
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status, "
            "created_at) VALUES (?, ?, 'LEDDOC', 'General Medicine', ?, '3', 'active', "
            "'2026-08-10 03:30:00')", (aid, pid, ward))
    return pid, aid


def _move(pid, aid, kind, from_ward, to_ward, moved_at):
    with db_session() as db:
        db.execute(
            "INSERT INTO patient_movements (id, patient_id, admission_id, movement_type, "
            "from_ward, to_ward, reason, moved_at) VALUES (?, ?, ?, ?, ?, ?, 'test', ?)",
            (f"MOV-{uuid.uuid4().hex[:8]}", pid, aid, kind, from_ward, to_ward, moved_at))


@pytest.fixture()
def ward_priced_bed_code():
    """A bed item priced per ward class (CCU 8,000, GEN 2,500) on a default plan
    of this test's own; the previous default plan is restored afterwards."""
    code = f"TST-LED{uuid.uuid4().hex[:6].upper()}"
    billing_masters.upsert_service_item(code=code, name="Bed day", category="bed")
    plan = int(billing_service.upsert_tariff_plan(code=f"TP{uuid.uuid4().hex[:6].upper()}",
                                                  name="Ledger bed plan")["id"])
    billing_masters.set_tariff_rate(tariff_plan_id=plan, service_code=code,
                                    rate_paise=rupees("8000"), ward_class="CCU",
                                    effective_from="2026-01-01")
    billing_masters.set_tariff_rate(tariff_plan_id=plan, service_code=code,
                                    rate_paise=rupees("2500"), ward_class="GEN",
                                    effective_from="2026-01-01")
    with db_session() as db:
        previous = [r[0] for r in db.execute(
            "SELECT id FROM tariff_plans WHERE is_default = 1").fetchall()]
        db.execute("UPDATE tariff_plans SET is_default = 0")
        db.execute("UPDATE tariff_plans SET is_default = 1 WHERE id = ?", (plan,))
    yield code
    with db_session() as db:
        db.execute("UPDATE tariff_plans SET is_default = 0")
        for pid in previous:
            db.execute("UPDATE tariff_plans SET is_default = 1 WHERE id = ?", (pid,))


EXPECTED_TRANSFER_PRICES = {
    "2026-08-10": rupees("8000"),   # CCU
    "2026-08-11": rupees("8000"),   # CCU
    "2026-08-12": rupees("2500"),   # moved to GEN that afternoon
    "2026-08-13": rupees("2500"),
}


def test_admission_leave_and_link_rows_never_reprice_a_bed_day(ward_priced_bed_code):
    pid, aid = _bed_patient_and_admission("GEN")
    # The one real move: CCU to GEN on 12 Aug at 14:00 IST.
    _move(pid, aid, "transfer", "CCU", "GEN", "2026-08-12T08:30:00+00:00")
    # Rows that do not move the patient between wards. Read as moves they
    # would reprice 10, 11 and 13 Aug at other wards.
    _move(pid, aid, "admission", None, "GEN", "2026-08-10T03:30:00+00:00")
    _move(pid, aid, "location_linked", "GEN", "GEN", "2026-08-10T06:00:00+00:00")
    _move(pid, aid, "leave_start", "CCU", "GEN", "2026-08-11T05:00:00+00:00")
    _move(pid, aid, "leave_end", "GEN", "CCU", "2026-08-11T06:00:00+00:00")
    _move(pid, aid, "bed_assigned", "GEN", "CCU", "2026-08-13T05:00:00+00:00")

    result = billing_service.post_bed_day_charges(
        admission_id=aid, service_code=ward_priced_bed_code, through="2026-08-13")
    by_day = {c["service_date"]: c["total_paise"] for c in result["posted"]}
    assert by_day == EXPECTED_TRANSFER_PRICES


def test_a_bed_change_or_swap_still_moves_the_priced_ward(ward_priced_bed_code):
    pid, aid = _bed_patient_and_admission("GEN")
    _move(pid, aid, "bed_change", "CCU", "GEN", "2026-08-12T08:30:00+00:00")
    result = billing_service.post_bed_day_charges(
        admission_id=aid, service_code=ward_priced_bed_code, through="2026-08-13")
    by_day = {c["service_date"]: c["total_paise"] for c in result["posted"]}
    assert by_day == EXPECTED_TRANSFER_PRICES


# ── The front desk's API is unchanged ────────────────────────────────────────

def test_front_desk_history_returns_the_old_keys_in_the_old_order():
    pid, aid = _bed_patient_and_admission("GEN")
    _move(pid, aid, "transfer", "CCU", "GEN", "2026-08-12T08:30:00+00:00")
    _move(pid, aid, "transfer", "GEN", "CCU", "2026-08-12T08:30:00+00:00")
    history = fd.list_patient_movements(pid)
    assert [list(m) for m in history] == [list(m51.LEGACY_COLUMNS)] * 2
    # Same instant: the later rowid (the second write) comes first, as before.
    assert [m["to_ward"] for m in history] == ["CCU", "GEN"]
