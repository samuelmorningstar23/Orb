"""An audit row's hash covers what the row STORES, whatever type the caller passed.

THE DEFECT (found 2026-10-03, fixed 2026-10-04). `compute_audit_row_hash`
JSON-encodes the row as `log_audit_event` was called. `audit_logs.target_id` is
TEXT. `ingestion_service._check_ingested_alert` passed `target_id=relay_msg_id`,
a cursor.lastrowid int, so the hash covered the JSON number 119 while the
column stored the text "119". `verify_audit_chain` recomputes from the stored
row, got a different hash, and reported the row as TAMPERED. Every monitor-feed
Relay alert wrote one. In the suite it showed up as four unrelated chain checks
failing after the first test that drove a monitor alert into a case room, each
passing alone.

The fix is in `audit_service.bound_audit_fields`, the one step every row goes
through before it is hashed and inserted: a TEXT column receives text, and the
INTEGER `source_event_id` receives the integer SQLite will store. These tests
write rows with the wrong types on purpose and check each one the way
`verify_audit_chain` does, from what came back out of the table.
"""
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import db_session, verify_audit_chain                   # noqa: E402
from services.audit_service import (compute_audit_row_hash,            # noqa: E402
                                    log_audit_event)


def _write(**fields) -> dict:
    """Write one audit row and return it exactly as the table stores it."""
    marker = f"AUDTYPE-{uuid.uuid4().hex}"
    log_audit_event(
        correlation_id=marker, event_category="ingestion",
        action_type="audit_type_probe", outcome="success", **fields)
    with db_session() as db:
        row = db.execute("SELECT * FROM audit_logs WHERE correlation_id = ?",
                         (marker,)).fetchone()
    assert row is not None, "the probe row was not written"
    return dict(row)


def _verifies(row: dict) -> bool:
    """The row check verify_audit_chain makes, on one row."""
    return compute_audit_row_hash(row, row.get("prev_hash")) == row["row_hash"]


@pytest.mark.parametrize("field,value,stored", [
    # The live defect: a cursor.lastrowid into the TEXT target_id.
    ("target_id", 119, "119"),
    ("target_id", 12.5, "12.5"),
    ("target_id", True, "1"),          # what SQLite itself stored for a bool
    ("patient_id", 42, "42"),
    ("encounter_id", 3.0, "3.0"),
    ("actor_user_id", 7, "7"),
    ("session_id", 99, "99"),
    ("summary_message", 1234, "1234"),
])
def test_a_non_text_value_in_a_text_column_is_hashed_as_the_text_stored(field, value, stored):
    row = _write(**{field: value})
    assert row[field] == stored, row[field]
    assert _verifies(row), f"{field}={value!r} wrote a row that verifies as tampered"


@pytest.mark.parametrize("value,stored", [
    (119, 119),
    ("119", 119),          # SQLite's INTEGER affinity stores the number
    (" 119 ", 119),
    ("+7", 7),
    (119.0, 119),
    (True, 1),
    ("abc", "abc"),        # not numeric: stored as given, and still verifies
    (12.5, 12.5),
])
def test_source_event_id_is_hashed_as_the_integer_column_stores_it(value, stored):
    row = _write(source_event_id=value)
    assert row["source_event_id"] == stored, row["source_event_id"]
    assert _verifies(row), f"source_event_id={value!r} wrote a row that verifies as tampered"


def test_ordinary_string_ids_are_unchanged():
    row = _write(target_id="msg-119", patient_id="P001", source_event_id=5)
    assert (row["target_id"], row["patient_id"], row["source_event_id"]) == ("msg-119", "P001", 5)
    assert _verifies(row)


# ═════════════════════════════════════════════════════════════════════════════
#  The lane that wrote the false alarms
# ═════════════════════════════════════════════════════════════════════════════
_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    yield
    if not _CREATED:
        return
    marks = ",".join("?" * len(_CREATED))
    with db_session() as db:
        db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                   tuple(_CREATED))
        db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})",
                   tuple(_CREATED))
    _CREATED.clear()


def _patient_with_case_room() -> str:
    pid = f"AUDT{uuid.uuid4().hex[:10].upper()}"
    _CREATED.append(pid)
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('AUDTADM', 'Audit Type Admitter', 'doctor', 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, "
            "status, admission_date) VALUES (?, ?, ?, 'AT-1', 'Audit Type Ward', 70, "
            "'M', 'Pneumonia', 'admitted', ?)",
            (pid, f"Audittype {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:10]}", now))
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, 'Audit Type Medicine', 'Audit Type Ward', 'AT-1', 'active', 'AUDTADM')",
            (pid,))
        db.execute("INSERT OR REPLACE INTO channels (id, name, type, patient_id) "
                   "VALUES (?, ?, 'case_room', ?)",
                   (f"audt-room-{pid}", f"Audit Type Room {pid}", pid))
    return pid


async def test_a_monitor_feed_relay_alert_leaves_the_audit_chain_intact(monkeypatch):
    """The real lane, end to end: an ingested monitor set raises a Relay alert in
    the case room, and the audit row naming that message verifies."""
    from services import bot_service, ingestion_service
    from services.vigil_service import compute_news2
    monkeypatch.setattr(bot_service, "publish_vigil_critical", lambda *a, **k: True)
    pid = _patient_with_case_room()
    payload = {"heart_rate": 125, "systolic_bp": 88, "diastolic_bp": 60,
               "respiratory_rate": 26, "temperature": 38.5, "spo2": 91,
               "source": "ingested_json"}
    risk = compute_news2(125, 88, 26, 38.5, 91)

    await ingestion_service._check_ingested_alert(pid, payload, risk)

    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE patient_id = ? AND target_type = 'message' "
            "ORDER BY id", (pid,)).fetchall()]
    assert rows, "the alert wrote no audit row naming its Relay message"
    for row in rows:
        assert row["target_id"] and row["target_id"].isdigit(), row["target_id"]
        assert _verifies(row), (
            f"the Relay alert's audit row (target_id {row['target_id']!r}) verifies "
            f"as tampered: {json.dumps({k: row[k] for k in ('id', 'action_type')})}")

    chain = verify_audit_chain()
    assert chain["ok"], f"audit chain broken: {chain['broken_row_ids']}"
