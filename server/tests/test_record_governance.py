"""Custody concurrency, audit atomicity and hold enforcement without module enablement."""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import consent_service as cs, record_governance_service as svc
from services.offline_workflows import WorkflowError


@pytest.fixture
def records():
    tag = uuid.uuid4().hex
    actor = {"id": "RG-A-" + tag, "role": "admin", "department": None}
    patient = "RG-P-" + tag
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,password_hash) VALUES (?,'Records test','admin','disabled')", (actor["id"],))
        db.execute("INSERT INTO patients(id,name,mrn,ward,age) VALUES (?,'Records test',?,'General Medicine',50)", (patient, patient))
    yield actor, patient


def hold(actor, patient, categories=None):
    return svc.create("hold", {"patient_id": patient, "reason": "Preserve for pending review", "authority_reference": "Hospital order TEST-1", "categories": categories or []}, actor=actor)


def request(patient, actor, category="ai_interaction_transcript"):
    return cs.record_erasure_request(patient_id=patient, requested_by=actor["id"], categories=[category])["id"]


def test_hold_refuses_erasure_when_module_is_off(records, monkeypatch):
    actor, patient = records
    monkeypatch.setattr(config, "RECORD_GOVERNANCE_ENABLED", False)
    hold(actor, patient)
    result = cs.adjudicate_erasure_request(request(patient, actor), decided_by=actor["id"])
    assert result["decisions"][0]["disposition"] == "refused"
    assert result["executable_count"] == 0


def test_hold_added_after_adjudication_blocks_entire_execution(records):
    actor, patient = records
    rid = request(patient, actor)
    assert cs.adjudicate_erasure_request(rid, decided_by=actor["id"])["executable_count"] == 1
    hold(actor, patient)
    with pytest.raises(ValueError, match="Nothing was erased"):
        cs.execute_erasure_request(rid, executed_by=actor["id"])
    with db_session() as db:
        assert db.execute("SELECT status FROM erasure_requests WHERE id=?", (rid,)).fetchone()[0] == "decided"
        assert db.execute("SELECT COUNT(*) FROM erasure_executions WHERE request_id=?", (rid,)).fetchone()[0] == 0


def test_hold_release_requires_reference_and_new_revision(records):
    actor, patient = records
    row = hold(actor, patient, ["ai_interaction_transcript"])
    with pytest.raises(WorkflowError):
        svc.transition("hold", row["id"], actor=actor, version=1, action="release", reason="Review completed")
    released = svc.transition("hold", row["id"], actor=actor, version=1, action="release", reason="Review completed", reference="Hospital order TEST-2")
    assert released["version"] == 2
    with pytest.raises(WorkflowError, match="changed"):
        svc.transition("hold", row["id"], actor=actor, version=1, action="release", reason="Stale retry", reference="TEST-2")
    assert cs.adjudicate_erasure_request(request(patient, actor), decided_by=actor["id"])["executable_count"] == 1


def test_retention_excludes_held_records_and_reports_hold_count(records):
    actor, patient = records
    with db_session() as db:
        db.execute("INSERT INTO clinical_documents(patient_id,doc_type,title,content,created_at) VALUES (?,'note','Test','Test','2000-01-01')", (patient,))
    before = cs.retention_review(now=datetime(2026, 10, 11, tzinfo=timezone.utc), patient_id=patient)
    clinical = next(c for c in before["categories"] if c["category"] == "clinical_record")
    assert clinical["by_table"]["clinical_documents"] == 1
    hold(actor, patient, ["clinical_record"])
    after = cs.retention_review(now=datetime(2026, 10, 11, tzinfo=timezone.utc), patient_id=patient)
    clinical = next(c for c in after["categories"] if c["category"] == "clinical_record")
    assert clinical["by_table"]["clinical_documents"] == 0
    assert clinical["held_legal_hold"] == 1
    assert after["deleted_anything"] is False


def test_audit_failure_rolls_back_file_and_event(records, monkeypatch):
    actor, patient = records
    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(svc, "audit", fail)
    with pytest.raises(RuntimeError):
        svc.create("file", {"patient_id": patient, "file_number": patient, "medium": "paper", "location": "Archive", "custodian": "Records desk"}, actor=actor)
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM mrd_files WHERE patient_id=?", (patient,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM record_governance_events WHERE patient_id=?", (patient,)).fetchone()[0] == 0


def test_release_has_explicit_approval_and_handover(records):
    actor, patient = records
    row = svc.create("release", {"patient_id": patient, "purpose": "Requested copy", "recipient": "Verified recipient", "documents": ["Paper note 1"], "authority_reference": "Consent TEST-3"}, actor=actor)
    with pytest.raises(WorkflowError):
        svc.transition("release", row["id"], actor=actor, version=1, action="released", reason="Attempted shortcut", reference="TEST-4")
    approved = svc.transition("release", row["id"], actor=actor, version=1, action="approved", reason="Authority checked")
    released = svc.transition("release", row["id"], actor=actor, version=approved["version"], action="released", reason="Copy handed over", reference="Recipient verification TEST-4")
    assert released["released_by"] == actor["id"]
    assert len(svc.detail("release", row["id"], actor=actor)["events"]) == 3


def test_disabled_module_and_unauthorised_role(records, monkeypatch):
    actor, _ = records
    client = TestClient(app)
    monkeypatch.setattr(config, "RECORD_GOVERNANCE_ENABLED", False)
    headers = {"Authorization": "Bearer " + create_token(actor["id"], "admin")}
    assert client.get("/api/record-governance/hold", headers=headers).status_code == 404
    monkeypatch.setattr(config, "RECORD_GOVERNANCE_ENABLED", True)
    nurse = {"Authorization": "Bearer " + create_token("N001", "nurse")}
    assert client.get("/api/record-governance/hold", headers=nurse).status_code == 403


def test_service_rechecks_stored_role_and_active_status(records):
    actor, patient = records
    forged = {"id": "N001", "role": "admin"}
    with pytest.raises(WorkflowError) as denied:
        hold(forged, patient)
    assert denied.value.status == 403
    with db_session() as db:
        db.execute("UPDATE users SET is_active=0 WHERE id=?", (actor["id"],))
    with pytest.raises(WorkflowError) as inactive:
        hold(actor, patient)
    assert inactive.value.status == 401


def test_delegated_creation_and_query_reject_foreign_patient_before_any_write(records):
    actor, own = records
    ward, foreign = "RG ward " + uuid.uuid4().hex, "RG-P-" + uuid.uuid4().hex
    with db_session() as db:
        db.execute("UPDATE users SET department=? WHERE id=?", (ward, actor["id"]))
        db.execute("INSERT INTO patients(id,name,mrn,status) VALUES (?,'Synthetic foreign record',?,'admitted')", (foreign, foreign))
        for pid, department in ((own, ward), (foreign, ward + " other")):
            db.execute("INSERT INTO admissions(id,patient_id,department,status,admitted_by) VALUES (?,?,?,'active',?)",
                       ("RG-A-" + uuid.uuid4().hex, pid, department, actor["id"]))
    first = hold(actor, own)  # stale caller department=None cannot override DB scope
    assert svc.list_records("hold", actor=actor, patient_id=own)["records"][0]["id"] == first["id"]
    for operation in (
        lambda: hold(actor, foreign),
        lambda: svc.create("file", {"patient_id": foreign, "file_number": foreign, "medium": "paper", "location": "Test", "custodian": "Test"}, actor=actor),
        lambda: svc.create("release", {"patient_id": foreign, "purpose": "Test", "recipient": "Test", "documents": ["Test"], "authority_reference": "Test"}, actor=actor),
        lambda: svc.list_records("hold", actor=actor, patient_id=foreign),
    ):
        with pytest.raises(WorkflowError) as denied:
            operation()
        assert denied.value.status == 404
    with db_session() as db:
        for table in ("record_legal_holds", "mrd_files", "mrd_release_requests", "record_governance_events"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE patient_id=?", (foreign,)).fetchone()[0] == 0


@pytest.mark.parametrize("categories_json", ["[ ]", "{}", '["clinical_record",8]', "not-json"])
def test_retention_hold_probe_matches_fail_closed_execution_semantics(records, categories_json):
    actor, patient = records
    with db_session() as db:
        db.execute("INSERT INTO clinical_documents(patient_id,doc_type,title,content,created_at) VALUES (?,'note','Synthetic','Synthetic','2000-01-01')", (patient,))
    row = hold(actor, patient)
    with db_session() as db:
        db.execute("UPDATE record_legal_holds SET categories_json=? WHERE id=?", (categories_json, row["id"]))
        assert svc.active_hold(db, patient, "clinical_record") is True
    reviewed = cs.retention_review(now=datetime(2026, 10, 11, tzinfo=timezone.utc), patient_id=patient)
    clinical = next(c for c in reviewed["categories"] if c["category"] == "clinical_record")
    assert clinical["by_table"]["clinical_documents"] == 0
    assert clinical["held_legal_hold"] == 1
    assert "clinical_documents" not in clinical.get("probe_errors", [])


def test_editing_local_clinical_records_does_not_restart_retention_clock(records):
    actor, patient = records
    tables = ("ris_studies", "care_plans", "emergency_visits", "perioperative_records", "dialysis_sessions")
    with db_session() as db:
        for table in tables:
            db.execute(f"""INSERT INTO {table}
                (id,patient_id,facility_id,title,state,payload_json,created_by,updated_by,created_at,updated_at,request_fingerprint)
                VALUES (?,?,1,'Synthetic old record','draft','{{}}',?,?,'2000-01-01','2026-10-10','synthetic')""",
                (f"{table}-{patient}", patient, actor["id"], actor["id"]))

    def counts():
        review = cs.retention_review(now=datetime(2026, 10, 11, tzinfo=timezone.utc), patient_id=patient)
        clinical = next(c for c in review["categories"] if c["category"] == "clinical_record")
        return {table: clinical["by_table"][table] for table in tables}

    assert counts() == dict.fromkeys(tables, 1)
    with db_session() as db:
        for table in tables:
            db.execute(f"UPDATE {table} SET title='Edited synthetic record',updated_at='2026-10-11',version=version+1 WHERE patient_id=?", (patient,))
    assert counts() == dict.fromkeys(tables, 1)
