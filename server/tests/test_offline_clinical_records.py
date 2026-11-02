"""Synthetic charts exercise real authorization, workflow, transactions and history."""
from copy import deepcopy
import uuid
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import config
from auth import get_current_user
from database import db_session
from services import facility_service as FS, offline_clinical_records as R

T0 = "2026-10-11T08:00:00+05:30"
T1 = "2026-10-11T08:30:00+05:30"
T2 = "2026-10-11T09:00:00+05:30"
T3 = "2026-10-11T09:30:00+05:30"
BASE = {"radiology": {"modality": "X-ray", "body_site": "Chest"}, "care-plans": {"problem": "Mobility", "goal": "Document assistance", "tasks": []}, "emergency": {"arrival_at": T0, "complaint": "Recorded symptom"}, "perioperative": {"record_type": "anaesthesia"}, "dialysis": {"access": "Recorded access"}}

@pytest.fixture(autouse=True)
def enabled(isolated_module_database, monkeypatch):
    for spec in R.DOMAINS.values():
        monkeypatch.setattr(config, spec.flag, True)
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()

@pytest.fixture
def people():
    suffix = uuid.uuid4().hex[:10]
    ids = {key: f"CL-{key}-{suffix}" for key in ("doctor", "nurse", "admin", "foreign", "theatre", "patient", "other", "registered", "admission", "other_admission", "case", "other_case", "order")}
    with db_session() as db:
        for key, role, dept in (("doctor", "doctor", "Clinical test ward"), ("nurse", "nurse", "Clinical test ward"), ("admin", "admin", None), ("foreign", "doctor", "Other test ward"), ("theatre", "doctor", "Anesthesiology")):
            db.execute("INSERT INTO users(id,name,role,department,password_hash) VALUES(?,?,?,?,?)", (ids[key], f"Synthetic {key}", role, dept, "not-a-login"))
        for key, status in (("patient", "admitted"), ("other", "admitted"), ("registered", "registered")):
            db.execute("INSERT INTO patients(id,name,mrn,status,registered_facility_id) VALUES(?,?,?,?,1)", (ids[key], f"Synthetic {key}", ids[key], status))
        for patient, admission, department, doctor in (("patient", "admission", "Clinical test ward", "doctor"), ("other", "other_admission", "Other test ward", "foreign")):
            db.execute("INSERT INTO admissions(id,patient_id,admitted_by,department,facility_id) VALUES(?,?,?,?,1)", (ids[admission], ids[patient], ids[doctor], department))
        for case, patient, doctor in (("case", "patient", "doctor"), ("other_case", "other", "foreign")):
            db.execute("INSERT INTO surgeries(id,patient_id,surgeon_id,procedure_name,diagnosis,scheduled_date,scheduled_time) VALUES(?,?,?,?,?,?,?)", (ids[case], ids[patient], ids[doctor], "Recorded procedure", "Recorded diagnosis", "2026-10-11", "08:00"))
        db.execute("INSERT INTO orders(id,patient_id,ordered_by,order_type,description,status) VALUES(?,?,?,?,?,?)", (ids["order"], ids["patient"], ids["doctor"], "imaging", "Synthetic imaging order", "active"))
    return ids

def actor(p, key="doctor"):
    return {"id": p[key], "role": "admin", "department": None}  # stored row must win

def make(p, domain, *, who="doctor", patient="patient", data=None, key=None, **extra):
    body = dict(patient_id=p[patient], title="Synthetic record", data=deepcopy(BASE[domain] if data is None else data), **extra)
    if domain == "perioperative" and "surgery_id" not in body:
        body["surgery_id"] = p["case"]
    return R.create(domain, R.CreateRecord(**body), actor(p, who), key)

def revise(p, domain, record, state=None, data=None, who="doctor", **extra):
    return R.update(domain, record["id"], R.UpdateRecord(version=record["version"], title=record["title"], state=state or record["state"], data=deepcopy(record["data"] if data is None else data), **extra), actor(p, who))

def deny(status, work):
    with pytest.raises(HTTPException) as exc:
        work()
    assert exc.value.status_code == status

@pytest.mark.parametrize("domain", list(BASE))
def test_create_scope_history_and_idempotency(people, domain):
    p = people
    first = make(p, domain, key="retry-key")
    assert make(p, domain, key="retry-key")["id"] == first["id"]
    assert first["version"] == 1 and first["created_by"] == p["doctor"] and first["facility_id"] == 1
    assert "request_fingerprint" not in first and "idempotency_key" not in first
    assert [r["id"] for r in R.list_records(domain, actor(p), p["patient"])["records"]] == [first["id"]]
    deny(404, lambda: R.detail(domain, first["id"], actor(p, "foreign")))
    assert R.list_records(domain, actor(p, "foreign"), p["patient"], 1)["records"] == []
    foreign_link = {"surgery_id": p["other_case"]} if domain == "perioperative" else {}
    deny(404, lambda: make(p, domain, patient="other", **foreign_link))
    with db_session() as db:
        assert db.execute(f"SELECT COUNT(*) FROM {R.DOMAINS[domain].table} WHERE patient_id=?", (p["other"],)).fetchone()[0] == 0
    assert [(e["version"], e["kind"]) for e in R.detail(domain, first["id"], actor(p))["history"]] == [(1, "created")]
    changed = {**BASE[domain], {"radiology":"body_site", "care-plans":"problem", "emergency":"complaint", "perioperative":"assessment", "dialysis":"access"}[domain]: "Different"}
    deny(409, lambda: make(p, domain, data=changed, key="retry-key"))
    with db_session() as db:
        assert db.execute(f"SELECT COUNT(*) FROM {R.DOMAINS[domain].table}_events WHERE record_id=?", (first["id"],)).fetchone()[0] == 1

@pytest.mark.parametrize("domain", list(BASE))
def test_disable_admin_read_only_stored_inactive(people, monkeypatch, domain):
    p = people
    item = make(p, domain)
    assert R.detail(domain, item["id"], actor(p, "admin"))["id"] == item["id"]
    assert R.meta(domain, actor(p, "admin"))["can_write"] is False
    deny(403, lambda: make(p, domain, who="admin"))
    with db_session() as db:
        db.execute("UPDATE users SET is_active=0 WHERE id=?", (p["doctor"],))
    deny(403, lambda: R.detail(domain, item["id"], actor(p)))
    monkeypatch.setattr(config, R.DOMAINS[domain].flag, False)
    deny(404, lambda: R.list_records(domain, actor(p, "admin")))

@pytest.mark.parametrize("domain", list(BASE))
def test_version_lock_atomic_audit_rollback_immutable_history(people, monkeypatch, domain):
    p = people
    item = make(p, domain)
    current = revise(p, domain, item)
    assert current["version"] == 2
    deny(409, lambda: revise(p, domain, item))
    def audit_failure(**kwargs):
        raise RuntimeError("synthetic audit storage failure")
    monkeypatch.setattr(R, "log_audit_event", audit_failure)
    with pytest.raises(RuntimeError, match="synthetic audit"):
        revise(p, domain, current)
    with db_session() as db:
        assert db.execute(f"SELECT version FROM {R.DOMAINS[domain].table} WHERE id=?", (item["id"],)).fetchone()[0] == 2
        assert db.execute(f"SELECT COUNT(*) FROM {R.DOMAINS[domain].table}_events WHERE record_id=?", (item["id"],)).fetchone()[0] == 2
        with pytest.raises(Exception, match="Clinical history cannot be edited"):
            db.execute(f"UPDATE {R.DOMAINS[domain].table}_events SET reason='altered' WHERE record_id=?", (item["id"],))

def test_ris_arrival_performed_report_sign_addendum(people):
    p = people
    item = make(p, "radiology", data={**BASE["radiology"], "order_id": p["order"], "accession": "LOCAL-001"})
    deny(422, lambda: revise(p, "radiology", item, "reported"))
    data = {**item["data"], "scheduled_at": T0, "arrived_at": T1, "performed_at": T2, "performer": "Staff", "findings": "Staff findings", "impression": "Staff impression"}
    for stage in ("scheduled", "arrived", "performed", "reported"):
        item = revise(p, "radiology", item, stage, data)
    deny(403, lambda: R.sign("radiology", item["id"], item["version"], actor(p, "nurse")))
    signed = R.sign("radiology", item["id"], item["version"], actor(p))
    assert signed["signed_by"] == p["doctor"] and signed["signed_at"]
    deny(409, lambda: revise(p, "radiology", signed))
    amended = R.addendum("radiology", signed["id"], R.AddendumIn(version=signed["version"], text="Staff correction", reason="Clarification"), actor(p))
    assert amended["data"] == signed["data"] and amended["signed_at"] == signed["signed_at"]
    history = R.detail("radiology", signed["id"], actor(p))["history"]
    assert [e["kind"] for e in history] == ["addendum", "signed", "updated", "updated", "updated", "updated", "created"]
    assert history[0]["snapshot"]["text"] == "Staff correction"

def test_care_due_owner_outcomes_closure(people):
    p = people
    task = {"id": "task-1", "description": "Assist activity", "owner": "Assigned nurse", "due_at": T1, "state": "planned"}
    item = make(p, "care-plans", who="nurse", data={**BASE["care-plans"], "tasks": [task], "review_at": T2})
    item = revise(p, "care-plans", item, "active", who="nurse")
    deny(422, lambda: revise(p, "care-plans", item, "completed", {**item["data"], "evaluation": "Review", "closure_reason": "Complete"}, who="nurse"))
    task.update(state="done", completed_at=T2, evaluation="Assistance documented")
    item = revise(p, "care-plans", item, "completed", {**item["data"], "tasks": [task], "evaluation": "Goal reviewed", "closure_reason": "Documented closure"}, who="nurse")
    assert item["data"]["tasks"][0]["owner"] == "Assigned nurse"
    deny(409, lambda: revise(p, "care-plans", item, "active", who="nurse"))
    assert R.addendum("care-plans", item["id"], R.AddendumIn(version=item["version"], text="Follow-up entered", reason="Late entry"), actor(p, "nurse"))["version"] == 4

def test_ed_registered_scope_does_not_grant_chart_access_or_mutate_adt(people):
    from permissions import patient_in_scope
    p = people
    with db_session() as db:
        assert not patient_in_scope(db, p["registered"], "Clinical test ward")
    deny(404, lambda: make(p, "emergency", who="nurse", patient="registered"))
    item = make(p, "emergency", patient="registered")
    assert item["department"] == "Clinical test ward" and item["admission_id"] is None
    assert R.detail("emergency", item["id"], actor(p, "nurse"))["id"] == item["id"]
    assert [r["id"] for r in R.list_records("emergency", actor(p, "nurse"), p["registered"])["records"]] == [item["id"]]
    deny(404, lambda: R.detail("emergency", item["id"], actor(p, "foreign")))
    assert R.list_records("emergency", actor(p, "foreign"), p["registered"])["records"] == []
    deny(404, lambda: make(p, "care-plans", patient="registered"))
    deny(404, lambda: make(p, "radiology", patient="registered"))
    data = {**item["data"], "triage_category": "Staff category", "triaged_at": T1, "seen_at": T2, "clinician": "Treating team", "disposition": "admission_requested", "disposition_at": T3, "disposition_note": "Admission requested separately"}
    for stage in ("triaged", "seen", "record_closed"):
        item = revise(p, "emergency", item, stage, data, who="nurse")
    with db_session() as db:
        assert db.execute("SELECT status FROM patients WHERE id=?", (p["registered"],)).fetchone()[0] == "registered"
        assert not db.execute("SELECT 1 FROM admissions WHERE patient_id=?", (p["registered"],)).fetchone()
        assert not patient_in_scope(db, p["registered"], "Clinical test ward")

@pytest.mark.parametrize("record_type", ["anaesthesia", "pacu", "implant"])
def test_periop_case_union_traceability_no_surgery_mutation(people, record_type):
    p = people
    data = {"record_type": record_type, "observations": [{"at": T1, "blood_pressure": "120/80", "pulse": 70}]}
    if record_type == "implant":
        data.update(device_name="Synthetic device", manufacturer="Recorded maker", lot="LOT-001", site="Recorded site", implanted_at=T1)
    else:
        data.update(started_at=T0, ended_at=T2, handover="Staff handover")
    item = make(p, "perioperative", who="theatre", patient="other", surgery_id=p["other_case"], data=data)
    assert p["other_case"] in {c["id"] for c in R.list_perioperative_cases(actor(p, "theatre"))["cases"]}
    assert p["other_case"] not in {c["id"] for c in R.list_perioperative_cases(actor(p))["cases"]}
    deny(404, lambda: R.detail("perioperative", item["id"], actor(p)))
    item = revise(p, "perioperative", item, "completed", who="theatre")
    assert R.sign("perioperative", item["id"], item["version"], actor(p, "theatre"))["state"] == "signed"
    with db_session() as db:
        assert db.execute("SELECT status FROM surgeries WHERE id=?", (p["other_case"],)).fetchone()[0] == "scheduled"
        db.execute("INSERT INTO surgery_team_assignments(surgery_id,user_id,role_name) VALUES(?,?,?)", (p["other_case"], p["doctor"], "consultant"))
    assert R.detail("perioperative", item["id"], actor(p))["id"] == item["id"]

def test_dialysis_prescriber_separation_prescribed_vs_actual(people):
    p = people
    prescription = {"modality": "Clinician entered", "duration_minutes": 180, "target_fluid_ml": 1000}
    deny(403, lambda: make(p, "dialysis", who="nurse", data={**BASE["dialysis"], "prescription": prescription}))
    item = make(p, "dialysis", data={**BASE["dialysis"], "prescription": prescription})
    altered = deepcopy(item["data"]); altered["prescription"]["duration_minutes"] = 240
    deny(403, lambda: revise(p, "dialysis", item, data=altered, who="nurse"))
    deny(403, lambda: revise(p, "dialysis", item, data={**item["data"], "prescription": None}, who="nurse"))
    actual = {**item["data"], "started_at": T0, "ended_at": T3, "actual_duration_minutes": 90, "actual_fluid_ml": 600, "complications": "Staff event and response", "handover": "Completed by team", "observations": [{"at": T1, "pulse": 75}]}
    item = revise(p, "dialysis", item, "in_progress", actual, who="nurse")
    item = revise(p, "dialysis", item, "completed", who="nurse")
    assert item["data"]["prescription"]["duration_minutes"] == 180 and item["data"]["actual_duration_minutes"] == 90
    deny(403, lambda: R.sign("dialysis", item["id"], item["version"], actor(p, "nurse")))
    assert R.sign("dialysis", item["id"], item["version"], actor(p))["state"] == "signed"

@pytest.mark.parametrize("domain,data", [
    ("radiology", {"modality": "X-ray", "body_site": "Chest", "scheduled_at": "2026-10-11T08:00:00"}),
    ("radiology", {"modality": "X-ray", "body_site": "Chest", "arrived_at": T2, "performed_at": T0}),
    ("emergency", {"arrival_at": T0, "complaint": "   "}),
    ("emergency", {"arrival_at": T2, "complaint": "Recorded", "triaged_at": T0}),
    ("emergency", {"arrival_at": T0, "complaint": "Recorded", "observations": [{"at": T1, "pulse": float("nan")}]}),
    ("care-plans", {"problem": "Focus", "goal": "Goal", "tasks": [{"id": "1", "description": "Action", "owner": "Nurse", "state": "done"}]}),
    ("perioperative", {"record_type": "implant", "device_name": "Device", "site": "Site"}),
    ("dialysis", {"access": "Access", "prescription": {"modality": "Entered", "duration_minutes": True}}),
    ("dialysis", {"access": "Access", "actual_fluid_ml": -1}),
])
def test_typed_bounded_validation(people, domain, data):
    deny(422, lambda: make(people, domain, data=data))

def test_links_forward_state_and_cancellation_reason(people):
    p = people
    deny(422, lambda: make(p, "radiology", admission_id=p["other_admission"]))
    deny(404, lambda: make(p, "perioperative", surgery_id=p["other_case"]))
    deny(422, lambda: make(p, "radiology", data={**BASE["radiology"], "order_id": "missing"}))
    item = revise(p, "care-plans", make(p, "care-plans"), "active")
    deny(409, lambda: revise(p, "care-plans", item, "draft"))
    deny(422, lambda: revise(p, "care-plans", item, "cancelled"))
    assert revise(p, "care-plans", item, "cancelled", reason="Entered in error")["state"] == "cancelled"

@pytest.mark.parametrize("domain", list(BASE))
def test_multifacility_fence(people, monkeypatch, domain):
    monkeypatch.setattr(FS, "multi_facility_data", lambda **kwargs: True)
    deny(503, lambda: R.meta(domain, actor(people)))
    deny(503, lambda: make(people, domain))
    deny(503, lambda: R.list_records(domain, actor(people)))

def test_http_strict_contract_and_disabled_routes(people, monkeypatch):
    from routes import radiology_routes
    p = people
    app = FastAPI(); app.include_router(radiology_routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": p["doctor"], "role": "doctor", "department": "Clinical test ward"}
    client = TestClient(app)
    body = {"patient_id": p["patient"], "title": "HTTP study", "data": BASE["radiology"]}
    response = client.post("/api/radiology/records", json=body, headers={"Idempotency-Key": "http-key"})
    assert response.status_code == 201, response.text
    record = response.json()
    assert client.get(f"/api/radiology/records/{record['id']}").status_code == 200
    assert client.post("/api/radiology/records", json={**body, "data": {**BASE["radiology"], "inferred_diagnosis": "forbidden"}}).status_code == 422
    assert client.put(f"/api/radiology/records/{record['id']}", json={"version": True, "title": "Study", "state": "requested", "data": BASE["radiology"]}).status_code == 422
    monkeypatch.setattr(config, "RADIOLOGY_ENABLED", False)
    assert client.get("/api/radiology/records").status_code == 404

@pytest.mark.parametrize("domain", list(BASE))
def test_scope_rechecked_before_idempotent_replay_and_addendum(people, domain):
    p = people
    item = make(p, domain, key="scope-retry")
    item = revise(p, domain, item, "cancelled", reason="Synthetic correction")
    with db_session() as db:
        db.execute("UPDATE users SET department='Unrelated ward' WHERE id=?", (p["doctor"],))
        if domain == "perioperative":
            db.execute("UPDATE surgeries SET surgeon_id=? WHERE id=?", (p["foreign"], p["case"]))
    deny(404, lambda: make(p, domain, key="scope-retry"))
    deny(404, lambda: R.addendum(domain, item["id"], R.AddendumIn(version=item["version"], text="Late entry", reason="Clarification"), actor(p)))


def test_ed_registered_nursing_record_scope_rechecks_department_and_facility(people):
    p = people
    item = make(p, "emergency", patient="registered")
    assert R.detail("emergency", item["id"], actor(p, "nurse"))["patient_id"] == p["registered"]
    with db_session() as db:
        db.execute("UPDATE users SET department='Unrelated ward' WHERE id=?", (p["nurse"],))
    deny(404, lambda: R.detail("emergency", item["id"], actor(p, "nurse")))
    assert R.list_records("emergency", actor(p, "nurse"), p["registered"])["records"] == []
    with db_session() as db:
        db.execute("UPDATE patients SET registered_facility_id=77 WHERE id=?", (p["registered"],))
    deny(404, lambda: R.detail("emergency", item["id"], actor(p)))
    assert R.list_records("emergency", actor(p), p["registered"])["records"] == []


def test_worklist_scope_precedes_pagination(people):
    p = people
    own = make(p, "care-plans")
    make(p, "care-plans", who="foreign", patient="other")
    # Other tests retain synthetic charts in this module DB; constrain this doctor.
    with db_session() as db:
        db.execute("UPDATE users SET department=? WHERE id=?", (p["doctor"], p["doctor"]))
        db.execute("UPDATE admissions SET department=? WHERE id=?", (p["doctor"], p["admission"]))
    assert [row["id"] for row in R.list_records("care-plans", actor(p), limit=1)["records"]] == [own["id"]]


def test_read_audit_fails_closed_and_create_rolls_back(people, monkeypatch):
    p = people
    item = make(p, "care-plans")
    def audit_failure(**kwargs):
        raise RuntimeError("synthetic audit unavailable")
    monkeypatch.setattr(R, "log_audit_event", audit_failure)
    for operation in (lambda: R.detail("care-plans", item["id"], actor(p)), lambda: R.list_records("care-plans", actor(p)), lambda: make(p, "care-plans", key="audit-create")):
        with pytest.raises(RuntimeError, match="synthetic audit"):
            operation()
    with db_session() as db:
        assert not db.execute("SELECT 1 FROM care_plans WHERE idempotency_key='audit-create' AND created_by=?", (p["doctor"],)).fetchone()


def test_history_pages_keep_earliest_snapshot_accessible_and_scoped(people):
    p = people
    item = make(p, "care-plans")
    for index in range(101):
        item = revise(p, "care-plans", item, data={**item["data"], "evaluation": f"Synthetic revision {index}"})
    first = R.detail("care-plans", item["id"], actor(p))
    assert len(first["history"]) == 100 and first["history_next_offset"] == 100
    older = R.history("care-plans", item["id"], actor(p), 100)
    assert [event["version"] for event in older["history"]] == [2, 1]
    assert older["history"][-1]["snapshot"]["data"]["goal"] == BASE["care-plans"]["goal"]
    assert older["history_next_offset"] is None
    deny(404, lambda: R.history("care-plans", item["id"], actor(p, "foreign"), 100))


def test_surgical_case_search_scope_and_pagination(people):
    p = people
    assert R.list_perioperative_cases(actor(p), q=p["case"])["cases"][0]["id"] == p["case"]
    assert R.list_perioperative_cases(actor(p), q=p["other_case"])["cases"] == []
    assert R.list_perioperative_cases(actor(p), q=p["case"], offset=100)["cases"] == []
    assert R.list_perioperative_cases(actor(p, "theatre"), q=p["other_case"])["cases"][0]["id"] == p["other_case"]


def test_idempotent_phi_replay_is_audited_and_fails_closed(people, monkeypatch):
    p = people
    item = make(p, "care-plans", key="audited-retry")
    calls = []
    original = R.log_audit_event
    def recording_audit(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(R, "log_audit_event", recording_audit)
    assert make(p, "care-plans", key="audited-retry")["id"] == item["id"]
    assert len(calls) == 1 and calls[0]["event_category"] == "phi_access"
    assert calls[0]["patient_id"] == p["patient"]
    def unavailable(**kwargs): raise RuntimeError("synthetic replay audit failure")
    monkeypatch.setattr(R, "log_audit_event", unavailable)
    with pytest.raises(RuntimeError, match="replay audit"):
        make(p, "care-plans", key="audited-retry")
