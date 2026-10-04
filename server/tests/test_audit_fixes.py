"""Regression tests for the Tier-1 adversarial-audit fixes.

Covers: orders authorization (C1/C2), create-order status guard (M1), audit-chain
NULL-hash tamper evasion (FR-1), discharge PRN reconciliation (CLOSE-PRN), WHO
checklist legacy preservation (WHO-LEGACY), relay bot provenance (RELAY-PROV), and
the messages/surgeries migration FK preservation + crash-safety (MIG-1/MIG-3).
"""
import json
import sqlite3
import uuid

import pytest

from database import db_session, get_db
from auth import hash_password


def _seed_terminology_once():
    from services import terminology_service
    terminology_service.seed_terminology()


_SEEDED: list[str] = []

# SEED NAMES. Every name this module writes to the shared session DB is an
# invented multi-token word that no clinical sentence contains. The reason is a
# second cross-module hazard, found 2026-09-04, that the discharge teardown
# below does not cover: a NAME outlives a discharge, and Sage's name matchers
# (services/sage_service.py: _match_patients, _match_out_of_scope,
# _full_name_match) read discharged patients too. The WHO-checklist seed used to
# be a patient literally named "S". For the General Medicine doctor in
# tests/test_sage_department_scope.py that row sat on another ward (Surg) and a
# one-letter name is a substring of almost every sentence, so once this module
# had run she was denied her OWN patients as OUT_OF_SCOPE and the security audit
# row for a real cross-ward reach carried no patient_id: 18 failures in a module
# that passed alone and beside any other neighbour. "PRN Pat" was the same
# hazard one step away (prn is ward vocabulary).
#
# Rules for a new seed: 3+ letters per token, not a word, not a drug, not a
# ward, not a department. The Auditfix prefix keeps the row attributable when
# someone dumps the test DB.
_NAME_PZ = "Auditfix Zorvantel"
_NAME_PX = "Auditfix Quelmirrane"
_NAME_PRN = "Auditfix Vrostagne"
_NAME_WHO = "Auditfix Xylophande"
_USER_NAMES = {
    "AUD-PT": "Auditfix Pativelle",
    "AUD-DR": "Auditfix Dokrenne",
    "AUD-SG": "Auditfix Sorgvennel",
}


def _seeded(pid: str) -> str:
    """Register a patient id this module admitted, so the autouse teardown below
    can discharge it. Call at every seed site."""
    _SEEDED.append(pid)
    return pid


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW: this module seeds four patients with an explicit
    status='admitted', and every one used to stay admitted in the shared session
    DB for the rest of the run. That is not a cosmetic leak. The FHIR
    self-export in services/downtime_service.py iterates
    `patients WHERE status='admitted'` and sleeps _PATIENT_SLEEP_S (250 ms)
    BETWEEN PATIENTS, so each leftover adds ~0.275 s to every export call in
    tests/test_downtime_fhir_export.py and to everything downstream of it.

    The seeds are inline in the tests rather than in a fixture, so each site
    wraps its id in `_seeded()` and this autouse teardown discharges the batch.
    Do not DELETE — the orders, medications, surgeries and audit rows written
    during the tests legitimately reference these ids, and the INSERT OR IGNORE
    seeds re-use the fixed ids 'PZ'/'PX' on later runs.

    Note the raw-sqlite3 seed in the MIG-1/MIG-3 migration test is NOT
    registered here: it writes to its own throwaway DB file in an isolated
    subprocess, not to the session DB."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?",
            [(p,) for p in _SEEDED],
        )
    _SEEDED.clear()


# ── C1/C2: orders routes require order.* permission (no patient/BOLA access) ──
def test_orders_routes_reject_patient_role():
    from fastapi.testclient import TestClient
    from main import app
    from auth import create_token
    _seed_terminology_once()
    client = TestClient(app)

    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
                   ("AUD-PT", _USER_NAMES["AUD-PT"], "patient", hash_password("x")))
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
                   ("AUD-DR", _USER_NAMES["AUD-DR"], "doctor", hash_password("x")))
        db.execute("INSERT OR IGNORE INTO patients (id, name, age, sex, mrn, diagnosis, ward, bed, status) "
                   "VALUES ('PZ',?,60,'F','MRNPZ','Dx','Med','M1','admitted')", (_NAME_PZ,))
    _seeded("PZ")
    pt = create_token("AUD-PT", "patient")
    dr = create_token("AUD-DR", "doctor")

    # Patient CANNOT create an order or read another patient's orders.
    r = client.post("/api/orders", headers={"Authorization": f"Bearer {pt}"},
                    json={"patient_id": "PZ", "order_type": "lab", "description": "CBC",
                          "code": "58410-2", "code_system": "LOINC"})
    assert r.status_code == 403, r.text
    r2 = client.get("/api/orders/patient/PZ", headers={"Authorization": f"Bearer {pt}"})
    assert r2.status_code == 403
    # Doctor CAN (has order.write). 58410-2 is a seeded LOINC code.
    r3 = client.post("/api/orders", headers={"Authorization": f"Bearer {dr}"},
                     json={"patient_id": "PZ", "order_type": "lab", "description": "CBC",
                           "code": "58410-2", "code_system": "LOINC"})
    assert r3.status_code == 200, r3.text


# ── M1: create_order cannot be born into a terminal/forbidden state ──
def test_create_order_rejects_terminal_status():
    from services import orders_service
    _seed_terminology_once()
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO patients (id, name, age, sex, mrn, diagnosis, ward, bed, status) "
                   "VALUES ('PX',?,50,'M','MRNPX','Dx','Med','M2','admitted')", (_NAME_PX,))
    _seeded("PX")
    for bad in ("completed", "resulted", "cancelled", "in_progress"):
        with pytest.raises(ValueError):
            orders_service.create_order(
                patient_id="PX", order_type="lab", description="CBC",
                code="58410-2", code_system="LOINC", status=bad,
            )
    # draft/active are allowed
    o = orders_service.create_order(patient_id="PX", order_type="lab", description="CBC",
                                    code="58410-2", code_system="LOINC", status="active")
    assert o["status"] == "active"


# ── FR-1: nulling a sealed row's hash + editing content is caught ──
def test_audit_chain_null_hash_evasion_detected():
    from services.audit_service import log_audit_event
    from services import flight_recorder_service as fr
    # Seed a few sealed audit rows.
    for i in range(4):
        log_audit_event(correlation_id=str(uuid.uuid4()), event_category="test",
                        action_type="probe", outcome="success",
                        summary_message=f"row {i}", actor_user_id="AUD-DR")
    assert fr.verify_chain()["ok"] is True

    # Tamper at the FILE level: the append-only triggers block a normal UPDATE
    # (that is the point), so simulate disk tampering by temporarily dropping the
    # no-update trigger, nulling a sealed row's hash + editing content, verifying
    # detection, then restoring the trigger AND the row so the shared test DB is
    # left intact for other tests.
    with db_session() as db:
        row = db.execute(
            "SELECT id, summary_message, row_hash FROM audit_logs "
            "WHERE row_hash IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        tid, orig_msg, orig_hash = row["id"], row["summary_message"], row["row_hash"]
        trg = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_audit_logs_no_update'"
        ).fetchone()
        trg_sql = trg["sql"] if trg else None
    try:
        with db_session() as db:
            if trg_sql:
                db.execute("DROP TRIGGER trg_audit_logs_no_update")
            db.execute("UPDATE audit_logs SET summary_message='EVIL', row_hash=NULL WHERE id=?", (tid,))
        res = fr.verify_chain()
        assert res["ok"] is False, "nulling a sealed hash must be detected, not silently accepted"
    finally:
        # Restore the row and the tamper-prevention trigger.
        with db_session() as db:
            db.execute("UPDATE audit_logs SET summary_message=?, row_hash=? WHERE id=?",
                       (orig_msg, orig_hash, tid))
            if trg_sql:
                db.execute(trg_sql)


# ── Audit chain survives CONCURRENT writes (BEGIN IMMEDIATE serialization) ──
def test_audit_chain_intact_under_concurrent_writes():
    from concurrent.futures import ThreadPoolExecutor
    from services.audit_service import log_audit_event
    from services import flight_recorder_service as fr

    def _write(i):
        log_audit_event(
            correlation_id=str(uuid.uuid4()), event_category="test",
            action_type="concurrent_probe", outcome="success",
            summary_message=f"concurrent {i}", actor_user_id="AUD-DR",
        )

    # Fire many audit writes concurrently. Before the fix, the prev-hash read +
    # insert raced and duplicated links; with BEGIN IMMEDIATE they serialize.
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_write, range(40)))

    res = fr.verify_chain()
    assert res["ok"] is True, f"concurrent audit writes broke the chain: {res}"


# ── CLOSE-PRN: PRN meds appear in the discharge reconciliation (not dropped) ──
def test_prn_med_in_reconciliation():
    from services.discharge_service import build_medication_reconciliation
    pid = _seeded(f"pat-{uuid.uuid4().hex[:8]}")
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, age, sex, mrn, diagnosis, ward, bed, status) "
                   "VALUES (?,?,?,?,?,?,?,?,'admitted')",
                   (pid, _NAME_PRN, 55, "M", f"MRN{uuid.uuid4().hex[:6]}", "Pain", "Med", "M9"))
        db.execute("INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source) "
                   "VALUES (?,?,?,?,?,?,?)", (pid, "Morphine", "5mg", "IV", "PRN", "prn", "clinician"))
    rec = build_medication_reconciliation(pid)
    names = {m["name"] for m in rec["continued"]}
    assert "Morphine" in names
    assert any(m.get("prn") for m in rec["continued"])


# ── WHO-LEGACY: legacy non-WHO checklist data is preserved on first WHO toggle ──
def test_who_checklist_preserves_legacy():
    from services import who_checklist
    sid = f"surg-{uuid.uuid4().hex[:8]}"
    pid = _seeded(f"pat-{uuid.uuid4().hex[:8]}")
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id,name,role,password_hash) VALUES ('AUD-SG',?,'doctor','h')",
                   (_USER_NAMES["AUD-SG"],))
        db.execute("INSERT INTO patients (id,name,age,sex,mrn,diagnosis,ward,bed,status) "
                   "VALUES (?,?,?,?,?,?,?,?, 'admitted')",
                   (pid, _NAME_WHO, 40, "M", f"MRN{uuid.uuid4().hex[:6]}", "Dx", "Surg", "S3"))
        db.execute("INSERT INTO surgeries (id,patient_id,surgeon_id,procedure_name,diagnosis,"
                   "scheduled_date,scheduled_time,status,surgical_checklist) VALUES (?,?,?,?,?,?,?,?,?)",
                   (sid, pid, "AUD-SG", "Proc", "Dx", "2026-03-01", "08:00", "scheduled",
                    json.dumps([{"item": "consent", "done": True}])))
    who_checklist.set_item(sid, "si_identity", True, "AUD-SG")
    with db_session() as db:
        row = db.execute("SELECT surgical_checklist, who_checklist FROM surgeries WHERE id=?",
                         (sid,)).fetchone()
    # The WHO state has its own column (migration 76); the prior checklist in
    # surgical_checklist is left exactly as it was, not carried or rewritten.
    assert json.loads(row["who_checklist"])["state"]["si_identity"]["checked"] is True
    assert json.loads(row["surgical_checklist"]) == [{"item": "consent", "done": True}]


# ── RELAY-PROV: send_message tags bot, and the columns exist for automated paths ──
def test_relay_provenance_columns_present():
    with db_session() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(messages)").fetchall()}
    assert "is_bot" in cols and "source" in cols


# ── MIG-1/MIG-3: legacy-shaped rebuild preserves the sender_id FK and survives drift ──
def test_migration_preserves_sender_fk_and_survives_column_drift(tmp_path):
    dbp = tmp_path / "legacy.db"
    # Hand-build a LEGACY DB: messages without source/is_bot and lacking the newer
    # CHECK tokens; surgeries missing emergency statuses AND with an EXTRA column.
    raw = sqlite3.connect(str(dbp))
    raw.executescript("""
        CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT, role TEXT, department TEXT, password_hash TEXT);
        CREATE TABLE channels (id TEXT PRIMARY KEY, name TEXT, type TEXT);
        CREATE TABLE patients (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL REFERENCES channels(id),
            sender_id TEXT NOT NULL REFERENCES users(id),
            sender_name TEXT NOT NULL, sender_role TEXT NOT NULL, content TEXT NOT NULL,
            message_type TEXT DEFAULT 'text' CHECK (message_type IN ('text','alert','system')),
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, read_by TEXT DEFAULT '[]'
        );
        CREATE TABLE surgeries (
            id TEXT PRIMARY KEY, patient_id TEXT NOT NULL REFERENCES patients(id),
            surgeon_id TEXT NOT NULL REFERENCES users(id), procedure_name TEXT NOT NULL,
            diagnosis TEXT NOT NULL, scheduled_date DATE NOT NULL, scheduled_time TEXT NOT NULL,
            estimated_duration_min INTEGER, operating_room TEXT,
            status TEXT DEFAULT 'scheduled' CHECK (status IN ('scheduled','in_progress','completed','cancelled')),
            co_morbidities TEXT, surgical_checklist TEXT, instruments_required TEXT,
            staff_required TEXT, risk_assessment TEXT, possible_complications TEXT,
            success_probability REAL, notes TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            legacy_extra TEXT
        );
        CREATE TABLE surgery_team_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, surgery_id TEXT REFERENCES surgeries(id), member TEXT
        );
        INSERT INTO users VALUES ('u1','U','doctor','Med','h'), ('system','Sys','admin','IT','h');
        INSERT INTO channels VALUES ('c1','Chan','department');
        INSERT INTO patients VALUES ('p1','P');
        INSERT INTO messages (channel_id,sender_id,sender_name,sender_role,content,message_type)
            VALUES ('c1','u1','U','doctor','hi','text');
        INSERT INTO surgeries (id,patient_id,surgeon_id,procedure_name,diagnosis,scheduled_date,scheduled_time,status,legacy_extra)
            VALUES ('s1','p1','u1','Proc','Dx','2026-01-01','09:00','scheduled','x');
        INSERT INTO surgery_team_assignments (surgery_id, member) VALUES ('s1','u1');
    """)
    raw.commit(); raw.close()

    # Run init_db in an ISOLATED subprocess so the shared test session's DB/module
    # state is untouched. Migrate twice to also assert idempotency.
    import os, subprocess, sys
    server_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (
        "import database; database.init_db(); database.init_db();"
        "import sqlite3; c=sqlite3.connect(r'%s');"
        # foreign_key_list columns: (id, seq, table, from, to, ...); r[3] is the
        # local 'from' column. Both channel_id and sender_id FKs must survive.
        "froms={r[3] for r in c.execute('PRAGMA foreign_key_list(messages)').fetchall()};"
        "assert 'sender_id' in froms, 'sender_id FK lost';"
        "assert 'channel_id' in froms, 'channel_id FK lost';"
        "assert c.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==1, 'messages data lost';"
        "assert c.execute('SELECT COUNT(*) FROM surgeries').fetchone()[0]==1, 'surgeries data lost';"
        "assert c.execute('PRAGMA foreign_key_check').fetchall()==[], 'dangling FK';"
        "c.execute(\"UPDATE surgeries SET status='emergency_pending' WHERE id='s1'\"); c.commit();"
        "print('MIGOK')"
    ) % str(dbp)
    env = {**os.environ, "ORB_DB_PATH": str(dbp), "ORB_DEPLOYMENT_PROFILE": "dev",
           "PYTHONPATH": server_dir}
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         env=env, cwd=server_dir)
    assert "MIGOK" in out.stdout, f"migration subprocess failed:\nSTDOUT:{out.stdout}\nSTDERR:{out.stderr}"
