"""The schema-derived erase plan never reaches an append-only table.

routes/admin_routes._build_erase_plan purged every table with a patient_id
column except audit_logs. shadow_events, consent_records, consent_withdrawals,
erasure_decisions and erasure_executions all carry BEFORE DELETE triggers that
RAISE ABORT, so the plan aborted its whole transaction for any patient the
ingestion lane had written a shadow row for. The one-click route that ran it is
retired (test_legacy_erase_retired.py), so the plan is exercised directly,
inside a transaction that is rolled back: nothing here persists.
"""
import re
import uuid
from datetime import datetime, timezone

from database import db_session
from routes.admin_routes import (_ERASE_KEEP_TABLES, _ERASE_RETAINED,
                                 _ERASE_UNLINKED_DELETES, _build_erase_plan)


class _Rollback(Exception):
    pass


def _delete_guarded_tables(db) -> set[str]:
    """Every table a trigger refuses DELETE on, read from the live schema."""
    out = set()
    for row in db.execute(
            "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall():
        sql = (row["sql"] or "").upper()
        if re.search(r"BEFORE\s+DELETE\s+ON", sql) and "RAISE(ABORT" in sql.replace(" ", ""):
            out.add(row["tbl_name"])
    return out


def test_the_plan_never_deletes_from_an_append_only_table():
    with db_session() as db:
        guarded = _delete_guarded_tables(db)
        planned = {table for table, _, _ in _build_erase_plan(db)}
    assert {"shadow_events", "consent_records", "audit_logs"} <= guarded, \
        "the append-only triggers this test relies on are not installed"
    assert not (planned & guarded), \
        f"the erase plan deletes from append-only tables: {sorted(planned & guarded)}"


def test_every_retained_table_says_why():
    assert _ERASE_KEEP_TABLES == frozenset(_ERASE_RETAINED)
    for table in ("shadow_events", "consent_records", "consent_withdrawals",
                  "erasure_requests", "erasure_decisions", "erasure_executions",
                  "breach_notifications", "audit_logs"):
        assert _ERASE_RETAINED.get(table), f"{table} is kept with no stated reason"
    # The retained PHI is named, not hidden.
    assert "phi_json" in _ERASE_RETAINED["shadow_events"]


def test_the_plan_runs_for_a_patient_with_shadow_consent_and_withdrawal_rows():
    pid = f"ERPL{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    try:
        with db_session() as db:
            db.execute(
                "INSERT INTO patients (id, name, mrn, age, sex, diagnosis, status) "
                "VALUES (?, 'Erase Plan Fixture', ?, 61, 'M', 'Fixture', 'discharged')",
                (pid, f"MRN-{pid}"))
            db.execute(
                "INSERT INTO vitals (patient_id, heart_rate, timestamp, source) "
                "VALUES (?, 88, ?, 'manual')", (pid, now))
            # A real-lane shadow row: immutable at the storage layer.
            db.execute(
                "INSERT INTO shadow_events (episode_id, kind, occurred_at, recorded_at, "
                "lane, patient_id, payload_json, phi_json) "
                "VALUES (?, 'alert.fired', ?, ?, 'ingestion', ?, '{}', ?)",
                (f"EP-{pid}", now, now, pid, '{"note": "free text"}'))
            cid = f"CON-{pid}"
            db.execute(
                "INSERT INTO consent_records (id, patient_id, purpose, method, granted_at, "
                "recorded_by) VALUES (?, ?, 'treatment', 'written_signature', ?, 'X')",
                (cid, pid, now))
            db.execute(
                "INSERT INTO consent_withdrawals (id, consent_id, patient_id, withdrawn_at, "
                "method, recorded_by) VALUES (?, ?, ?, ?, 'verbal_witnessed', 'X')",
                (f"WD-{pid}", cid, pid, now))

            # Before the fix this raised on the first append-only table it met.
            for _table, sql, n in _ERASE_UNLINKED_DELETES + _build_erase_plan(db):
                db.execute(sql, (pid,) * n)

            def count(table: str) -> int:
                return db.execute(f"SELECT COUNT(*) c FROM {table} WHERE patient_id = ?",
                                  (pid,)).fetchone()["c"]

            assert count("vitals") == 0, "the plan no longer erases clinical rows"
            for table in ("shadow_events", "consent_records", "consent_withdrawals"):
                assert count(table) == 1, f"{table} should be retained, not erased"
            raise _Rollback
    except _Rollback:
        pass

    with db_session() as db:
        left = db.execute("SELECT COUNT(*) c FROM patients WHERE id = ?", (pid,)).fetchone()["c"]
    assert left == 0, "the fixture leaked out of its rolled-back transaction"
