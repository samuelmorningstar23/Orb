"""Local operational state machines, custody allocation, scope and audit rollback."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import cssd_service as cssd, feedback_service as feedback, mortuary_service as mortuary
from services import local_operations_common as common
from services.audit_service import AuditWriteError

client = TestClient(app)


def token():
    return uuid.uuid4().hex


def user(role="admin", department=None):
    ident = "LOC-U-" + token()
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,department,password_hash,is_active) VALUES(?,?,?,?,?,1)",
                   (ident, "Synthetic operations account", role, department, "not-a-login-password"))
    return {"id": ident, "role": role, "department": department}


def patient(department):
    ident = "LOC-P-" + token()
    admitter = user()["id"]
    with db_session() as db:
        db.execute("INSERT INTO patients(id,name,mrn,status) VALUES(?,?,?,'admitted')", (ident, "Synthetic operations patient", ident))
        db.execute("INSERT INTO admissions(id,patient_id,department,status,admitted_at,admitted_by) VALUES(?,?,?,'active',?,?)",
                   ("LOC-A-" + token(), ident, department, common.now(), admitter))
    return ident


@pytest.fixture
def admin():
    return user()


def fb_data(**extra):
    return {"kind": "complaint", "channel": "in_person", "subject": "Synthetic complaint", "statement": "The desk was difficult to find.", **extra}


def slot(admin):
    return mortuary.add_slot({"code": token(), "name": "Synthetic storage"}, actor=admin)


def intake(admin, position, **extra):
    return mortuary.intake({"body_tag": token(), "identity_label": "Synthetic custody identity",
                            "authority_reference": "Existing paper authority TEST", "slot_id": position["id"], **extra}, actor=admin, key=token())


def pack(admin):
    definition = cssd.create_set({"code": token(), "name": "Synthetic set", "items": [{"name": "Forceps", "quantity": 2}]}, actor=admin)
    row = cssd.create_pack({"set_id": definition["id"], "tag": token()}, actor=admin)
    for action in ("clean", "pack"):
        row = cssd.pack_action(row["id"], {"action": action, "version": row["version"], "note": "Operator recorded step"}, actor=admin)
    return row


def cycle(admin, physical="pass", chemical="pass", biological="pass", **extra):
    physical_pack = pack(admin)
    row = cssd.start_cycle({"reference": token(), "sterilizer_reference": "Device TEST", "sop_reference": "Hospital SOP TEST",
                            "method": "Operator-entered method", "pack_ids": [physical_pack["id"]]}, actor=admin, key=token())
    row = cssd.cycle_action(row["id"], {"action": "complete", "version": row["version"], "note": "Operator recorded end",
                                        "physical_result": physical, "chemical_result": chemical, "biological_result": biological,
                                        "measurements": "Paper cycle log TEST", **extra}, actor=admin)
    return row, cssd.pack_detail(physical_pack["id"], actor=admin)


def release(admin, row):
    return cssd.cycle_action(row["id"], {"action": "release", "version": row["version"], "note": "Operator reviewed SOP and results",
                                        "release_reference": "Signed release TEST",
                                        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}, actor=admin)


def test_feedback_creation_replay_and_key_conflict(admin):
    key = token()
    row = feedback.register(fb_data(), actor=admin, key=key)
    assert feedback.register(fb_data(), actor=admin, key=key)["id"] == row["id"]
    with pytest.raises(common.OperationsError, match="different information"):
        feedback.register(fb_data(statement="Changed original statement"), actor=admin, key=key)
    assert len(feedback.detail(row["id"], actor=admin)["events"]) == 1


def test_feedback_resolution_close_reopen_and_stale_revision(admin):
    row = feedback.register(fb_data(), actor=admin, key=token())
    with pytest.raises(common.OperationsError, match="not available"):
        feedback.act(row["id"], {"action": "close", "version": 1, "note": "Close prematurely"}, actor=admin)
    resolved = feedback.act(row["id"], {"action": "resolve", "version": 1, "note": "A new sign was installed."}, actor=admin)
    with pytest.raises(common.OperationsError, match="changed"):
        feedback.act(row["id"], {"action": "close", "version": 1, "note": "Stale close"}, actor=admin)
    closed = feedback.act(row["id"], {"action": "close", "version": resolved["version"], "note": "Response documented"}, actor=admin)
    reopened = feedback.act(row["id"], {"action": "reopen", "version": closed["version"], "note": "Reporter requests further review"}, actor=admin)
    assert reopened["status"] == "investigating" and reopened["resolution"] is None
    detail = feedback.detail(row["id"], actor=admin)
    assert detail["statement"] == fb_data()["statement"]
    assert len(detail["events"]) == 4


@pytest.mark.parametrize("changes", [{"rating": 0}, {"rating": True}, {"subject": " "}, {"statement": "bad\x00text"}])
def test_feedback_validation(admin, changes):
    with pytest.raises(common.OperationsError):
        feedback.register(fb_data(**changes), actor=admin, key=token())


def test_patient_scope_applies_to_creation_detail_list_and_event_ownership():
    department = "Local department " + token()
    own, other = user(department=department), user(department="Other " + token())
    pid = patient(department)
    row = feedback.register(fb_data(patient_id=pid), actor=own, key=token())
    assert feedback.detail(row["id"], actor=own)["events"][0]["patient_id"] == pid
    with pytest.raises(common.OperationsError) as exc:
        feedback.register(fb_data(patient_id=pid), actor=other, key=token())
    assert exc.value.status == 404
    with pytest.raises(common.OperationsError):
        feedback.detail(row["id"], actor=other)
    assert row["id"] not in {item["id"] for item in feedback.list_cases(actor=other)["cases"]}
    # A subsequent ward transfer invalidates the old department's linked read.
    with db_session() as db:
        db.execute("UPDATE admissions SET department=? WHERE patient_id=?", ("Moved " + token(), pid))
    with pytest.raises(common.OperationsError):
        feedback.detail(row["id"], actor=own)
    assert feedback.list_cases(actor=own)["cases"] == []


def test_mortuary_linked_patient_scope_precedes_allocation_and_replay():
    ward = "Mortuary scope " + token()
    actor, own, foreign = user(department=ward), patient(ward), patient(ward + " other")
    position = slot(actor)
    data = {"body_tag": token(), "identity_label": "Synthetic custody identity",
            "authority_reference": "Recorded authority TEST", "slot_id": position["id"], "patient_id": own}
    request_key = token()
    row = mortuary.intake(data, actor=actor, key=request_key)
    with pytest.raises(common.OperationsError) as denied:
        mortuary.intake({**data, "body_tag": token(), "patient_id": foreign}, actor=actor, key=token())
    assert denied.value.status == 404  # not the occupied-position conflict
    with db_session() as db:
        db.execute("UPDATE admissions SET department=? WHERE patient_id=?", (ward + " transferred", own))
    with pytest.raises(common.OperationsError) as replay:
        mortuary.intake(data, actor=actor, key=request_key)
    assert replay.value.status == 404
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM mortuary_cases WHERE patient_id=?", (foreign,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM mortuary_events WHERE case_id=?", (row["id"],)).fetchone()[0] == 1


def test_mortuary_custody_hold_clearance_and_final_handover(admin):
    position, second = slot(admin), slot(admin)
    row = intake(admin, position)
    row = mortuary.act(row["id"], {"action": "hold", "version": row["version"], "note": "Awaiting existing authority clearance"}, actor=admin)
    with pytest.raises(common.OperationsError, match="Clear the recorded hold"):
        mortuary.act(row["id"], {"action": "release", "version": row["version"], "note": "Attempted handover"}, actor=admin)
    row = mortuary.act(row["id"], {"action": "move", "version": row["version"], "slot_id": second["id"], "note": "Physical custody movement"}, actor=admin)
    with pytest.raises(common.OperationsError):
        mortuary.act(row["id"], {"action": "clear_hold", "version": row["version"], "note": "No clearance reference"}, actor=admin)
    row = mortuary.act(row["id"], {"action": "clear_hold", "version": row["version"], "note": "Clearance received", "authority_reference": "Signed clearance TEST"}, actor=admin)
    witness = user()
    released = mortuary.act(row["id"], {"action": "release", "version": row["version"], "note": "Custody handed over",
                                        "release_authority_reference": "Signed handover TEST", "recipient_name": "Synthetic recipient",
                                        "recipient_relationship": "Recorded relation", "identity_check_reference": "Check log TEST", "witness_id": witness["id"]}, actor=admin)
    assert released["status"] == "released" and released["slot_id"] is None
    assert not next(x for x in mortuary.register(actor=admin)["slots"] if x["id"] == second["id"])["occupied"]
    with pytest.raises(common.OperationsError, match="cannot be changed"):
        mortuary.act(row["id"], {"action": "hold", "version": released["version"], "note": "Too late"}, actor=admin)


def test_concurrent_mortuary_intakes_allocate_slot_once(admin):
    position = slot(admin)
    def attempt(_):
        try:
            return intake(admin, position)["id"]
        except common.OperationsError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert results.count("slot_occupied") == 1
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM mortuary_cases WHERE slot_id=?", (position["id"],)).fetchone()[0] == 1


@pytest.mark.parametrize("field,value", [("received_at", "2020-01-01T12:00:00"), ("received_at", "2999-01-01T00:00:00Z"), ("authority_reference", "")])
def test_mortuary_does_not_guess_time_or_authority(admin, field, value):
    with pytest.raises(common.OperationsError):
        intake(admin, slot(admin), **{field: value})


@pytest.mark.parametrize("physical,chemical,biological", [("fail", "pass", "pass"), ("pass", "pending", "pass"), ("pass", "pass", "pending")])
def test_cssd_failed_or_pending_checks_never_release(admin, physical, chemical, biological):
    row, physical_pack = cycle(admin, physical, chemical, biological)
    assert physical_pack["status"] == "quarantined" and not physical_pack["available_to_issue"]
    with pytest.raises(common.OperationsError, match="completed documented checks"):
        release(admin, row)


def test_cssd_biological_exemption_needs_explicit_hospital_basis(admin):
    with pytest.raises(common.OperationsError, match="basis"):
        cycle(admin, biological="not_required")
    row, _ = cycle(admin, biological="not_required", biological_basis="Hospital SOP TEST explicitly records this basis")
    assert release(admin, row)["status"] == "released"


def test_cssd_pending_check_can_arrive_later_with_append_only_history(admin):
    row, physical_pack = cycle(admin, biological="pending")
    row = cssd.cycle_action(row["id"], {"action": "record_checks", "version": row["version"], "note": "Later biological result recorded",
                                       "physical_result": "pass", "chemical_result": "pass", "biological_result": "pass", "measurements": "Later laboratory log TEST"}, actor=admin)
    release(admin, row)
    assert cssd.pack_detail(physical_pack["id"], actor=admin)["available_to_issue"]
    events = cssd.cycle_detail(row["id"], actor=admin)["events"]
    assert any('"biological": "pending"' in event["note"] for event in events)
    assert any('"biological": "pass"' in event["note"] for event in events)


def test_cssd_recall_tracks_issued_pack_until_actual_return(admin):
    row, physical_pack = cycle(admin)
    released = release(admin, row)
    physical_pack = cssd.pack_detail(physical_pack["id"], actor=admin)
    issued = cssd.pack_action(physical_pack["id"], {"action": "issue", "version": physical_pack["version"], "note": "Custody issue",
                                                   "destination": "Theatre TEST", "reference": "Issue log TEST"}, actor=admin)
    cssd.cycle_action(row["id"], {"action": "recall", "version": released["version"], "note": "Operator records recall"}, actor=admin)
    recalled = cssd.pack_detail(issued["id"], actor=admin)
    assert recalled["status"] == "recall_pending" and recalled["issues"][0]["returned_at"] is None
    returned = cssd.pack_action(recalled["id"], {"action": "return", "version": recalled["version"], "note": "Physical return recorded"}, actor=admin)
    assert returned["status"] == "received" and returned["cycle_id"] is None and returned["expires_at"] is None


def test_cssd_expiry_is_read_time_boundary_and_cannot_be_issued(admin, monkeypatch):
    row, physical_pack = cycle(admin)
    release(admin, row)
    physical_pack = cssd.pack_detail(physical_pack["id"], actor=admin)
    monkeypatch.setattr(common, "now", lambda: physical_pack["expires_at"])
    assert not cssd.pack_detail(physical_pack["id"], actor=admin)["available_to_issue"]
    with pytest.raises(common.OperationsError, match="expiry has passed"):
        cssd.pack_action(physical_pack["id"], {"action": "issue", "version": physical_pack["version"], "note": "At expiry",
                                             "destination": "Theatre TEST", "reference": "TEST"}, actor=admin)


def test_cssd_pack_cannot_enter_two_cycles(admin):
    physical_pack = pack(admin)
    payload = {"reference": token(), "sterilizer_reference": "Device TEST", "sop_reference": "SOP TEST", "method": "Recorded", "pack_ids": [physical_pack["id"]]}
    first = cssd.start_cycle(payload, actor=admin, key=token())
    with pytest.raises(common.OperationsError, match="outside another cycle"):
        cssd.start_cycle({**payload, "reference": token()}, actor=admin, key=token())
    assert cssd.pack_detail(physical_pack["id"], actor=admin)["cycle_id"] == first["id"]


@pytest.mark.parametrize("domain", ["feedback", "mortuary", "cssd"])
def test_audit_failure_rolls_back_record_and_events(admin, monkeypatch, domain):
    position = slot(admin) if domain == "mortuary" else None
    physical_pack = pack(admin) if domain == "cssd" else None
    def fail(**kwargs):
        raise AuditWriteError("Synthetic audit failure")
    monkeypatch.setattr(common, "log_audit_event", fail)
    ident = token()
    with pytest.raises(AuditWriteError):
        if domain == "feedback":
            feedback.register(fb_data(subject=ident), actor=admin, key=token())
        elif domain == "mortuary":
            intake(admin, position, body_tag=ident)
        else:
            cssd.start_cycle({"reference": ident, "sterilizer_reference": "Device", "sop_reference": "SOP", "method": "Recorded", "pack_ids": [physical_pack["id"]]}, actor=admin, key=token())
    with db_session() as db:
        table, field = {"feedback": ("feedback_cases", "subject"), "mortuary": ("mortuary_cases", "body_tag"), "cssd": ("cssd_cycles", "reference")}[domain]
        assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE {field}=?", (ident,)).fetchone()[0] == 0
        if physical_pack:
            assert db.execute("SELECT status FROM cssd_packs WHERE id=?", (physical_pack["id"],)).fetchone()[0] == "packed"


@pytest.mark.parametrize("path,flag", [("feedback", "FEEDBACK_ENABLED"), ("mortuary", "MORTUARY_ENABLED"), ("cssd", "CSSD_ENABLED")])
def test_routes_require_session_module_and_dedicated_permission(admin, monkeypatch, path, flag):
    assert client.get(f"/api/{path}").status_code == 401
    headers = {"Authorization": "Bearer " + create_token(admin["id"], "admin")}
    monkeypatch.setattr(config, flag, False)
    assert client.get(f"/api/{path}", headers=headers).status_code == 404
    monkeypatch.setattr(config, flag, True)
    assert client.get(f"/api/{path}", headers=headers).status_code == 200
    doctor = user("doctor", "Synthetic department")
    assert client.get(f"/api/{path}", headers={"Authorization": "Bearer " + create_token(doctor["id"], "doctor")}).status_code == 403


def test_direct_services_preserve_facility_fence(admin, monkeypatch):
    monkeypatch.setattr(common, "multi_facility_data", lambda **kwargs: True)
    for fn in (feedback.list_cases, mortuary.register, cssd.register):
        with pytest.raises(common.OperationsError) as exc:
            fn(actor=admin)
        assert exc.value.status == 503 and exc.value.code == "facility_fence"


@pytest.mark.parametrize('domain', ['feedback', 'mortuary', 'cssd'])
def test_direct_calls_reject_forged_and_stale_actor_roles(admin, domain):
    nurse = user('nurse')
    forged = {**nurse, 'role': 'admin', 'department': None}
    fn = {'feedback': feedback.list_cases, 'mortuary': mortuary.register, 'cssd': cssd.register}[domain]
    with pytest.raises(common.OperationsError) as exc:
        fn(actor=forged)
    assert exc.value.status == 403
    mutator = {
        'feedback': lambda actor: feedback.register(fb_data(), actor=actor, key=token()),
        'mortuary': lambda actor: mortuary.add_slot({'code': token(), 'name': 'Synthetic position'}, actor=actor),
        'cssd': lambda actor: cssd.create_set({'code': token(), 'name': 'Synthetic set', 'items': [{'name': 'Forceps', 'quantity': 1}]}, actor=actor),
    }[domain]
    with pytest.raises(common.OperationsError) as exc:
        mutator(forged)
    assert exc.value.status == 403
    with db_session() as db:
        db.execute('UPDATE users SET role=? WHERE id=?', ('nurse', admin['id']))
    with pytest.raises(common.OperationsError) as exc:
        mutator(admin)
    assert exc.value.status == 403


def test_cssd_case_link_is_derived_scoped_and_kept_on_return():
    department = 'Local case ' + token()
    actor = user(department=department)
    pid = patient(department)
    surgery_id = 'LOC-S-' + token()
    with db_session() as db:
        db.execute("INSERT INTO surgeries(id,patient_id,surgeon_id,procedure_name,diagnosis,scheduled_date,scheduled_time,status) VALUES(?,?,?,'Synthetic case','Synthetic diagnosis','2026-10-01','09:00','scheduled')", (surgery_id, pid, actor['id']))
    row, physical_pack = cycle(actor)
    release(actor, row)
    physical_pack = cssd.pack_detail(physical_pack['id'], actor=actor)
    issued = cssd.pack_action(physical_pack['id'], {'action': 'issue', 'version': physical_pack['version'], 'note': 'Physical issue', 'destination': 'Theatre TEST', 'reference': 'Custody TEST', 'surgery_id': surgery_id}, actor=actor)
    detail = cssd.pack_detail(issued['id'], actor=actor)
    assert detail['issues'][0]['patient_id'] == pid
    assert detail['issues'][0]['surgery_id'] == surgery_id
    returned = cssd.pack_action(issued['id'], {'action': 'return', 'version': issued['version'], 'note': 'Physical return'}, actor=actor)
    assert returned['status'] == 'received'
    assert all(event['patient_id'] == pid for event in cssd.pack_detail(issued['id'], actor=actor)['events'] if event['action'] in ('issue', 'return'))
    with db_session() as db:
        db.execute('UPDATE admissions SET department=? WHERE patient_id=?', ('Transferred ' + token(), pid))
    with pytest.raises(common.OperationsError) as exc:
        cssd.pack_detail(issued['id'], actor=actor)
    assert exc.value.status == 404
    visible = cssd.register(actor=actor)
    assert issued['id'] not in {p['id'] for p in visible['packs']}
    assert row['id'] not in {item['id'] for item in visible['cycles']}
    with pytest.raises(common.OperationsError):
        cssd.cycle_detail(row['id'], actor=actor)


def test_cssd_unknown_case_does_not_create_custody_issue(admin):
    row, physical_pack = cycle(admin)
    release(admin, row)
    physical_pack = cssd.pack_detail(physical_pack['id'], actor=admin)
    with pytest.raises(common.OperationsError) as exc:
        cssd.pack_action(physical_pack['id'], {'action': 'issue', 'version': physical_pack['version'], 'note': 'Physical issue', 'destination': 'Theatre TEST', 'reference': 'Custody TEST', 'surgery_id': 'MISSING'}, actor=admin)
    assert exc.value.status == 404
    assert cssd.pack_detail(physical_pack['id'], actor=admin)['issues'] == []


@pytest.mark.parametrize('domain', ['feedback', 'mortuary', 'cssd'])
def test_register_reads_fail_closed_when_audit_is_unavailable(admin, monkeypatch, domain):
    def fail(**kwargs):
        raise AuditWriteError('Synthetic audit failure')
    monkeypatch.setattr(common, 'log_audit_event', fail)
    with pytest.raises(AuditWriteError):
        {'feedback': feedback.list_cases, 'mortuary': mortuary.register, 'cssd': cssd.register}[domain](actor=admin)


def test_patient_visibility_is_applied_before_page_limit():
    department = 'Local page ' + token()
    actor = user(department=department)
    pid = patient(department)
    hidden = feedback.register(fb_data(patient_id=pid), actor=actor, key=token())
    visible = feedback.register(fb_data(subject='Visible page record'), actor=actor, key=token())
    with db_session() as db:
        db.execute('UPDATE admissions SET department=? WHERE patient_id=?', ('Moved ' + token(), pid))
        db.execute("UPDATE feedback_cases SET created_at='2999-01-01T00:00:00Z' WHERE id=?", (hidden['id'],))
    page = feedback.list_cases(actor=actor, limit=1)
    assert page['cases'][0]['id'] == visible['id']
    assert not page['has_more']


def test_migration_repeat_and_append_only_history(admin):
    from migrations_his.m0081_local_operations import apply
    row = feedback.register(fb_data(), actor=admin, key=token())
    with db_session() as db:
        apply(db)
        with pytest.raises(Exception, match='immutable|append.only'):
            db.execute("UPDATE feedback_events SET note='Changed' WHERE case_id=?", (row['id'],))
