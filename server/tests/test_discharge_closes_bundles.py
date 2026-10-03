"""Discharge closes the patient's open care bundles (F050).

discharge_patient closed the admission and left any Sepsis Six bundle
'active', so the Command Center kept a discharged patient in its live bundle
count and compliance figures. The discharge now cancels them in the same
transaction, audited with the reason, and get_active_bundles lists admitted
patients only (which also covers bundles left open before this fix).
"""
import uuid

from database import db_session
from services import discharge_service
from services.sepsis_bundle_service import get_active_bundles, start_bundle


def _admitted_patient() -> str:
    pid = f"PBUN{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, status) "
            "VALUES (?, 'Bundle Discharge Test', ?, 'GM', 'B-4', 'Sepsis', 'admitted')",
            (pid, f"MRN-{pid}"),
        )
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('BUNDOC', 'Dr Bundle', 'doctor', 'x')")
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
            "VALUES (?, ?, 'BUNDOC', 'General Medicine', 'active')",
            (f"ADM-{pid}", pid),
        )
    return pid


def _bundle_status(bundle_id: int) -> str:
    with db_session() as db:
        return db.execute("SELECT status FROM care_bundles WHERE id = ?", (bundle_id,)).fetchone()["status"]


def test_discharge_cancels_the_active_bundle_and_audits_why():
    pid = _admitted_patient()
    bundle = start_bundle(pid, triggered_by="vigil_auto", trigger_reason="NEWS2 8 (>=7)")
    assert bundle["id"] in {b["id"] for b in get_active_bundles()}

    result = discharge_service.discharge_patient(pid, "BUNDOC", "Summary text", [])
    assert result["status"] == "success"

    assert _bundle_status(bundle["id"]) == "cancelled"
    assert bundle["id"] not in {b["id"] for b in get_active_bundles()}
    with db_session() as db:
        audit = db.execute(
            "SELECT actor_user_id, patient_id, metadata_json FROM audit_logs "
            "WHERE action_type = 'bundle_cancelled' AND target_id = ?",
            (str(bundle["id"]),),
        ).fetchone()
    assert audit is not None
    assert audit["actor_user_id"] == "BUNDOC" and audit["patient_id"] == pid
    assert discharge_service.DISCHARGE_BUNDLE_REASON in audit["metadata_json"]


def test_a_bundle_left_open_on_a_discharged_patient_is_not_counted():
    pid = _admitted_patient()
    bundle = start_bundle(pid, trigger_reason="Manual start")
    # Discharged by some path that did not close the bundle (or before this fix).
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
    assert _bundle_status(bundle["id"]) == "active"
    assert bundle["id"] not in {b["id"] for b in get_active_bundles()}


def test_the_command_center_compliance_excludes_the_discharged_patient():
    from services import command_center_service as cc

    pid = _admitted_patient()
    start_bundle(pid, trigger_reason="Manual start")
    before = cc._bundles()["active"]
    discharge_service.discharge_patient(pid, "BUNDOC", "Summary text", [])
    assert cc._bundles()["active"] == before - 1
    assert pid not in {b["patient_id"] for b in get_active_bundles()}
