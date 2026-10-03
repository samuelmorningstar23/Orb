"""A8 Flight Recorder v1 — service tests.

Seeds audit_logs (via the real hash-chained writer), event_log and order_events,
then asserts:
  * get_timeline merges all three sources in time order
  * verify_chain reports ok=True on an intact chain
  * verify_chain detects a break when an audit row's hashed payload is tampered
"""
import json
import uuid

import pytest

from database import db_session
from services.audit_service import log_audit_event
from services.flight_recorder_service import (
    get_event_detail,
    get_timeline,
    verify_chain,
    _parse_ts,
)

PID = "P-FR-1"


def _drop_audit_triggers(db):
    db.execute("DROP TRIGGER IF EXISTS trg_audit_logs_no_update")
    db.execute("DROP TRIGGER IF EXISTS trg_audit_logs_no_delete")


def _restore_audit_triggers(db):
    db.execute("CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_update "
               "BEFORE UPDATE ON audit_logs BEGIN "
               "SELECT RAISE(ABORT, 'audit_logs is append-only: UPDATE is forbidden'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_delete "
               "BEFORE DELETE ON audit_logs BEGIN "
               "SELECT RAISE(ABORT, 'audit_logs is append-only: DELETE is forbidden'); END")


@pytest.fixture(autouse=True)
def clean_tables():
    """Isolate each test: wipe the three source tables (audit_logs is append-only,
    so drop its guard triggers for the reset, then restore them)."""
    with db_session() as db:
        _drop_audit_triggers(db)
        db.execute("DELETE FROM audit_logs")
        db.execute("DELETE FROM event_log")
        db.execute("DELETE FROM order_events")
        _restore_audit_triggers(db)
        # Seed a patient + order so order_events FK/join has something to resolve.
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('U-FR-1', 'FR Patient', 'patient', 'x')")
        db.execute("INSERT OR IGNORE INTO patients (id, user_id, name, mrn) "
                   "VALUES (?, 'U-FR-1', 'FR Patient', 'MRN-FR-1')", (PID,))
        db.execute(
            "INSERT OR IGNORE INTO orders (id, patient_id, order_type, description, status) "
            "VALUES ('O-FR-1', ?, 'lab', 'CBC panel', 'active')", (PID,))
    yield
    with db_session() as db:
        _drop_audit_triggers(db)
        db.execute("DELETE FROM audit_logs")
        db.execute("DELETE FROM event_log")
        db.execute("DELETE FROM order_events")
        _restore_audit_triggers(db)


def _seed_audit(n=3):
    for i in range(n):
        log_audit_event(
            correlation_id=f"corr-fr-{i}",
            event_category="clinical_record",
            action_type=f"fr_action_{i}",
            outcome="success",
            summary_message=f"flight recorder seed row {i}",
            actor_user_id="D001",
            actor_role="doctor",
            patient_id=PID,
        )


def _seed_event():
    with db_session() as db:
        db.execute(
            "INSERT INTO event_log (event_id, topic, source_node, event_type, patient_id, payload, timestamp, correlation_id) "
            "VALUES (?, 'vigil.deterioration', 'vigil', 'patient_deteriorated', ?, ?, ?, ?)",
            (str(uuid.uuid4()), PID, json.dumps({"news2": 7, "trend": "up"}),
             "2026-07-04T10:00:00+00:00", "corr-fr-evt"),
        )


def _seed_order_event():
    with db_session() as db:
        db.execute(
            "INSERT INTO order_events (order_id, prior_status, new_status, actor_id, reason, created_at) "
            "VALUES ('O-FR-1', 'draft', 'active', 'D001', 'clinician activated', ?)",
            ("2026-07-04T10:05:00+00:00",),
        )


def test_timeline_merges_sources_time_ordered():
    _seed_audit(2)
    _seed_event()
    _seed_order_event()

    events = get_timeline(patient_id=PID)
    sources = {e["source"] for e in events}
    assert sources == {"audit", "event", "order"}, sources
    # At least the 2 audit + 1 event + 1 order rows we seeded.
    assert len(events) >= 4

    # Strictly non-decreasing by parsed timestamp (the merge is time-ordered).
    keys = [_parse_ts(e["ts"]) for e in events]
    assert keys == sorted(keys)

    # Audit rows on an intact chain are flagged chain_ok=True; non-audit rows None.
    audit_events = [e for e in events if e["source"] == "audit"]
    assert all(e["chain_ok"] is True for e in audit_events)
    assert all(e["chain_ok"] is None for e in events if e["source"] != "audit")


def test_verify_chain_ok_on_intact_chain():
    _seed_audit(3)
    result = verify_chain()
    assert result["ok"] is True
    assert result["broken_at"] is None
    assert result["count"] == 3

    # patient-scoped count still reports the patient's hashed rows.
    scoped = verify_chain(patient_id=PID)
    assert scoped["ok"] is True
    assert scoped["count"] == 3


def test_verify_chain_detects_tamper():
    _seed_audit(3)
    with db_session() as db:
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM audit_logs ORDER BY id ASC").fetchall()]
    assert len(ids) == 3
    victim = ids[1]

    # Tamper: mutate a hashed column directly (row_hash left stale) so the
    # recomputed hash no longer matches — exactly what a file-level edit looks like.
    with db_session() as db:
        _drop_audit_triggers(db)
        db.execute("UPDATE audit_logs SET summary_message = 'TAMPERED' WHERE id = ?", (victim,))
        _restore_audit_triggers(db)

    result = verify_chain()
    assert result["ok"] is False
    assert result["broken_at"] == victim


def test_get_event_detail_roundtrip():
    _seed_audit(1)
    events = get_timeline(patient_id=PID)
    audit = next(e for e in events if e["source"] == "audit")
    detail = get_event_detail(audit["id"], "audit")
    assert detail is not None
    assert detail["source"] == "audit"
    assert detail["event"]["id"] == audit["id"]
    assert detail["event"]["chain_ok"] is True

    # Unknown ids return None rather than raising.
    assert get_event_detail(999999, "audit") is None


# ── The chain walk is cached and extended, never trusted blindly ─────────────
#
# Every timeline load, badge check and scrubber step used to re-hash the whole
# audit table (about 100k rows on a pilot box). The walk is now cached and only
# rows appended since are hashed; these pin that it is cheaper WITHOUT letting a
# tamper slip past the badge.

def _count_hashes(monkeypatch):
    from services import flight_recorder_service as fr
    calls = {"n": 0}
    real = fr.compute_audit_row_hash

    def counting(row, prev):
        calls["n"] += 1
        return real(row, prev)

    monkeypatch.setattr(fr, "compute_audit_row_hash", counting)
    return calls


def test_the_timeline_hashes_only_rows_appended_since_the_last_walk(monkeypatch):
    _seed_audit(3)
    verify_chain()
    calls = _count_hashes(monkeypatch)

    _seed_audit(2)
    events = get_timeline(patient_id=PID)

    assert calls["n"] == 2, f"re-hashed {calls['n']} rows, expected only the 2 new ones"
    assert all(e["chain_ok"] is True for e in events if e["source"] == "audit")


def test_the_badge_always_walks_the_full_chain_so_a_tamper_is_caught_at_once():
    _seed_audit(3)
    assert verify_chain()["ok"] is True
    with db_session() as db:
        victim = db.execute("SELECT id FROM audit_logs ORDER BY id ASC").fetchall()[0]["id"]
        _drop_audit_triggers(db)
        db.execute("UPDATE audit_logs SET summary_message = 'TAMPERED' WHERE id = ?", (victim,))
        _restore_audit_triggers(db)

    result = verify_chain()

    assert result["ok"] is False and result["broken_at"] == victim


def test_a_deleted_row_invalidates_the_cached_prefix():
    _seed_audit(3)
    verify_chain()
    with db_session() as db:
        ids = [r["id"] for r in db.execute("SELECT id FROM audit_logs ORDER BY id ASC").fetchall()]
        _drop_audit_triggers(db)
        db.execute("DELETE FROM audit_logs WHERE id = ?", (ids[1],))
        _restore_audit_triggers(db)

    events = get_timeline(patient_id=PID)

    flags = {e["id"]: e["chain_ok"] for e in events if e["source"] == "audit"}
    assert flags[ids[2]] is False, "the row after a deleted one must read as a chain break"


def test_the_cached_flags_expire(monkeypatch):
    from services import flight_recorder_service as fr
    _seed_audit(3)
    verify_chain()
    with db_session() as db:
        victim = db.execute("SELECT id FROM audit_logs ORDER BY id ASC").fetchall()[0]["id"]
        _drop_audit_triggers(db)
        db.execute("UPDATE audit_logs SET summary_message = 'TAMPERED' WHERE id = ?", (victim,))
        _restore_audit_triggers(db)
    monkeypatch.setattr(fr, "_CHAIN_CACHE_MAX_AGE_S", 0.0)

    events = get_timeline(patient_id=PID)

    flags = {e["id"]: e["chain_ok"] for e in events if e["source"] == "audit"}
    assert flags[victim] is False
