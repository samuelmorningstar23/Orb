"""The surgical module's cross-ward PHI disclosure, and the rule that closed it.

THE DEFECT (found 2026-08-14, pre-pilot dry run). Six read endpoints —
``/schedule``, ``/emergency-queue``, ``/cases``, ``/{id}``, ``/{id}/checklist``
and ``/{id}/risk`` — JOINed `patients` and selected name, MRN, age, sex, bed,
diagnosis, comorbidities and allergies with NO ward scope and NO ``phi_access``
audit row. `surgery.read` is held by EVERY doctor, EVERY nurse and admin
(permissions.ROLE_PERMISSIONS), so "any authenticated clinician" is the real
audience, and the list endpoints hand out surgery_ids, so reaching the detail
routes required no guessing. It was also unprovable after the fact: the audit
middleware fires on writes only, so a Cardiology nurse reading every ward's
operating list left no trace at all.

WHY THE OBVIOUS FIX WOULD HAVE BEEN WRONG. Bolting on the plain "patient is on
your ward" filter breaks the theatre: a General Surgery surgeon operates on a
General Medicine patient and is never in that patient's department, and an
anaesthetist covers every ward while belonging to one that admits nobody. The
rule this file pins is therefore a union — governance / perioperative / owning
ward / named case team — and it is stated in full in
services/surgery_scope.py and summarised in the header of
routes/surgery_routes.py.

WHAT EACH TEST HOLDS DOWN, so a regression reads as a regression:

  LEAK        an out-of-department clinician sees no other ward's case, in any
              list, and gets an indistinguishable 404 on every detail route.
  THEATRE     perioperative staff still read the WHOLE list — the half of this
              that a security-only fix would have silently destroyed.
  WARD        the owning ward still sees its own patient going to theatre.
  TEAM        an out-of-department consultant ON the roster can still work.
  AUDIT       every named-patient surgical read writes a fail-closed
              ``phi_access`` row stamped with WHICH clause allowed it.
  MUTATIONS   the same gate on the writes, including the one that authors a
              post-op note into the patient's chart.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services.hospital_time import hospital_today

client = TestClient(app)
# Fail-closed behaviour is observed as a 500 with no PHI in it, so the harness
# must hand the response back instead of re-raising the server exception.
lenient = TestClient(app, raise_server_exceptions=False)


# Department names deliberately prefixed "ScopeTest" — a prefix that contains
# none of the perioperative tokens ("surg", "anesthes", "theatre", …), so the
# medical wards below cannot be mis-detected as theatre departments by accident.
# test_department_names_in_this_file_are_not_accidentally_perioperative pins it.
DEPT_MED = "ScopeTest General Medicine"
DEPT_CARD = "ScopeTest Cardiology"
DEPT_SURG = "ScopeTest General Surgery"
DEPT_ANAES = "ScopeTest Anesthesiology"

SECRET_NAME = "Surgscope Leaked Patientname"
SECRET_DIAGNOSIS = "Surgscope perforated viscus"

_READ_PATHS = ("detail", "checklist", "risk")


# ── fixtures ────────────────────────────────────────────────────────────────

def _mk_user(role, department, tag):
    uid = f"SGU{tag}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Surgscope {tag}", role, department),
        )
    return uid


def _auth(user_id, role="doctor"):
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _mk_patient(department, name, mrn_tag, diagnosis):
    """Department on the ADMISSION. `patients.ward` deliberately says something
    else, so a fix that scoped the wrong column (the bed-prefix `ward`, not
    `admissions.department`) fails here rather than silently stopping scoping."""
    pid = f"SGP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('SGADM', 'Surgscope Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status)
               VALUES (?, ?, ?, 'B-9', 'bay-9', 61, 'M', ?, 'admitted')""",
            (pid, name, f"MRN-SG-{mrn_tag}-{uuid.uuid4().hex[:8].upper()}", diagnosis),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'bay-9', 'B-9', 'active', 'SGADM')",
            (pid, department),
        )
    return pid


def _mk_surgery(patient_id, surgeon_id, status="scheduled"):
    sid = f"SGS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis,
                                      scheduled_date, scheduled_time, status)
               VALUES (?, ?, ?, 'Laparotomy', 'Perforation', ?, '09:00', ?)""",
            (sid, patient_id, surgeon_id, hospital_today().isoformat(), status),
        )
    return sid


def _mk_case_room(patient_id):
    """`/alert-team` 404s on a missing case room too. Give the General Medicine
    patient a real one so the 404 in the mutation test can only be the SCOPE
    check — a test that passes because a channel is absent proves nothing."""
    cid = f"chan_sg_{uuid.uuid4().hex[:12]}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES ('system', 'Orb Sage', 'admin', NULL, 'system_hash')"
        )
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) "
            "VALUES (?, 'Case Room', 'case_room', ?)",
            (cid, patient_id),
        )
    return cid


def _assign(surgery_id, user_id, status="alerted", role_name="Anesthetist"):
    with db_session() as db:
        db.execute(
            "INSERT INTO surgery_team_assignments (surgery_id, user_id, role_name, status) "
            "VALUES (?, ?, ?, ?)",
            (surgery_id, user_id, role_name, status),
        )


@pytest.fixture()
def world():
    """A General Medicine patient operated on by a General Surgery surgeon —
    the exact cross-departmental case a naive ward filter would break — plus a
    Cardiology patient who is nothing to do with them.

    Function-scoped on purpose: other modules in this suite wipe `surgeries` in
    their own setup, and a module-scoped world would silently evaporate.
    """
    surgeon = _mk_user("doctor", DEPT_SURG, "SRG")
    anaesthetist = _mk_user("doctor", DEPT_ANAES, "ANS")
    med_doc = _mk_user("doctor", DEPT_MED, "MED")
    card_doc = _mk_user("doctor", DEPT_CARD, "CRD")
    card_nurse = _mk_user("nurse", DEPT_CARD, "CRN")
    card_consultant = _mk_user("doctor", DEPT_CARD, "CON")
    governance = _mk_user("admin", None, "GOV")

    med_patient = _mk_patient(DEPT_MED, SECRET_NAME, "MED", SECRET_DIAGNOSIS)
    card_patient = _mk_patient(DEPT_CARD, "Surgscope Cardiac Patient", "CRD",
                               "Surgscope stable angina")

    med_case = _mk_surgery(med_patient, surgeon)
    med_emergency = _mk_surgery(med_patient, surgeon, status="emergency_pending")
    card_case = _mk_surgery(card_patient, surgeon)
    # The out-of-department consultant is on THIS case's roster and no other.
    _assign(med_case, card_consultant, role_name="Cardiology")
    med_room = _mk_case_room(med_patient)

    w = {
        "surgeon": surgeon, "anaesthetist": anaesthetist, "med_doc": med_doc,
        "card_doc": card_doc, "card_nurse": card_nurse,
        "card_consultant": card_consultant, "governance": governance,
        "med_patient": med_patient, "card_patient": card_patient,
        "med_case": med_case, "med_emergency": med_emergency,
        "card_case": card_case, "med_room": med_room,
    }
    yield w

    with db_session() as db:
        for sid in (med_case, med_emergency, card_case):
            db.execute("DELETE FROM surgery_team_assignments WHERE surgery_id = ?", (sid,))
            db.execute("DELETE FROM surgery_prep_requests WHERE surgery_id = ?", (sid,))
            db.execute("DELETE FROM surgeries WHERE id = ?", (sid,))
        db.execute("DELETE FROM messages WHERE channel_id = ?", (med_room,))
        db.execute("DELETE FROM channels WHERE id = ?", (med_room,))


# ── helpers ─────────────────────────────────────────────────────────────────

def _schedule_ids(headers):
    resp = client.get("/api/surgery/schedule?days=2", headers=headers)
    assert resp.status_code == 200, resp.text
    return {s["id"] for day in resp.json() for s in day["surgeries"]}, resp.text


def _queue_ids(headers):
    resp = client.get("/api/surgery/emergency-queue", headers=headers)
    assert resp.status_code == 200, resp.text
    return {r["id"] for r in resp.json()}, resp.text


def _case_ids(headers):
    resp = client.get("/api/surgery/cases", headers=headers)
    assert resp.status_code == 200, resp.text
    return {r["id"] for r in resp.json()}, resp.text


def _max_audit_id():
    with db_session() as db:
        row = db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM audit_logs").fetchone()
        return row["m"]


def _phi_rows_since(marker, actor_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE id > ? AND actor_user_id = ? "
            "AND event_category = 'phi_access' ORDER BY id ASC",
            (marker, actor_id),
        ).fetchall()]


def _stub_risk_stream(monkeypatch):
    """Replace the SSE generator — there is no Ollama in the test env and what is
    under test is the gate in front of it, not the model."""
    import routes.surgery_routes as sr

    async def _fake(surgery_id, detail=None):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(sr, "generate_risk_assessment", _fake)


# ═══════════════════════════════════════════════════════════════════════════
#  THE RULE ITSELF
# ═══════════════════════════════════════════════════════════════════════════

def test_department_names_in_this_file_are_not_accidentally_perioperative():
    """Guards the fixtures, not the product: if "ScopeTest" ever grew a token
    like "surg", every ward in this file would silently become theatre-wide and
    every leak assertion below would pass for the wrong reason."""
    from services.surgery_scope import is_perioperative_department
    assert is_perioperative_department(DEPT_MED) is False
    assert is_perioperative_department(DEPT_CARD) is False
    assert is_perioperative_department(DEPT_SURG) is True
    assert is_perioperative_department(DEPT_ANAES) is True


def test_perioperative_detection_covers_the_real_spellings_and_can_be_pinned(monkeypatch):
    """Clause (B) is a substring match on a free-text department name, so its
    edges are asserted rather than assumed — and the escape hatch for a site
    whose theatre is named something else is asserted too."""
    from services.surgery_scope import is_perioperative_department as periop

    for name in ("General Surgery", "Neurosurgery", "Cardiothoracic Surgery",
                 "Anesthesiology", "Anaesthetics", "Operating Theatre",
                 "Main Operating Room"):
        assert periop(name) is True, name
    for name in ("General Medicine", "Cardiology", "Oncology", "Pharmacy",
                 None, "", "   "):
        assert periop(name) is False, name

    # The env override REPLACES the token match — a site that needs to be exact
    # must be able to be, including excluding a name the tokens would match.
    monkeypatch.setenv("ORB_PERIOPERATIVE_DEPARTMENTS", "OT Complex, Day Surgery Unit")
    assert periop("OT Complex") is True
    assert periop("ot complex") is True
    assert periop("Anesthesiology") is False, "override must not be additive"


def test_scope_verdict_comes_from_the_user_row(world):
    from services.surgery_scope import (SCOPE_DEPARTMENT, SCOPE_HOSPITAL_WIDE,
                                        SCOPE_PERIOPERATIVE, surgical_scope)
    assert surgical_scope({"department": None})[0] == SCOPE_HOSPITAL_WIDE
    assert surgical_scope({"department": DEPT_SURG})[0] == SCOPE_PERIOPERATIVE
    assert surgical_scope({"department": DEPT_ANAES})[0] == SCOPE_PERIOPERATIVE
    assert surgical_scope({"department": DEPT_CARD})[0] == SCOPE_DEPARTMENT


# ═══════════════════════════════════════════════════════════════════════════
#  LEAK — the defect
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("actor,role", [("card_doc", "doctor"), ("card_nurse", "nurse")])
def test_out_of_department_clinician_sees_no_other_wards_case_in_any_list(world, actor, role):
    """THE DEFECT. A Cardiology doctor/nurse holds `surgery.read` — every clinical
    role does — and before the fix all three list endpoints returned every ward's
    operations with name, MRN, age, sex, bed and diagnosis attached."""
    headers = _auth(world[actor], role)

    sched, body = _schedule_ids(headers)
    assert world["med_case"] not in sched
    assert world["med_emergency"] not in sched
    assert world["card_case"] in sched, "their own ward's case must still be listed"
    assert SECRET_NAME not in body
    assert SECRET_DIAGNOSIS not in body

    queue, body = _queue_ids(headers)
    assert world["med_emergency"] not in queue
    assert SECRET_NAME not in body
    assert SECRET_DIAGNOSIS not in body

    cases, body = _case_ids(headers)
    assert world["med_case"] not in cases
    assert world["med_emergency"] not in cases
    assert world["card_case"] in cases
    assert SECRET_NAME not in body
    assert SECRET_DIAGNOSIS not in body


def test_out_of_department_clinician_is_404_on_every_detail_route(world, monkeypatch):
    """The list endpoints hand out surgery_ids, so before the fix the detail
    routes needed no guessing at all — and /{id} is the richest payload in the
    module (diagnosis, comorbidities AND allergies)."""
    _stub_risk_stream(monkeypatch)
    headers = _auth(world["card_doc"])
    sid = world["med_case"]

    for path in (f"/api/surgery/{sid}",
                 f"/api/surgery/{sid}/checklist",
                 f"/api/surgery/{sid}/risk"):
        resp = client.get(path, headers=headers)
        assert resp.status_code == 404, f"{path} -> {resp.status_code} {resp.text}"
        assert SECRET_NAME not in resp.text
        assert SECRET_DIAGNOSIS not in resp.text


def test_the_404_is_indistinguishable_from_an_unknown_case(world, monkeypatch):
    """404, never 403, and byte-identical to a genuinely unknown id: a 403 — or a
    different detail string — confirms the case, and therefore the patient,
    exists. Same rule as permissions.enforce_patient_scope."""
    _stub_risk_stream(monkeypatch)
    headers = _auth(world["card_doc"])

    for suffix in ("", "/checklist", "/risk"):
        out_of_scope = client.get(f"/api/surgery/{world['med_case']}{suffix}", headers=headers)
        unknown = client.get(f"/api/surgery/SGS-NO-SUCH-CASE{suffix}", headers=headers)
        assert out_of_scope.status_code == unknown.status_code == 404
        assert out_of_scope.json() == unknown.json(), suffix


def test_scope_cannot_be_widened_by_anything_the_caller_says(world):
    """A scope the caller can name is not a scope. There is no `?department=`,
    no `?all=`, no header — the derivation reads users.department and nothing
    else."""
    headers = _auth(world["card_doc"])
    headers["X-Department"] = DEPT_MED
    for qs in (f"?department={DEPT_MED}", "?all_departments=true", "?days=2&all=1"):
        resp = client.get(f"/api/surgery/schedule{qs}", headers=headers)
        assert resp.status_code in (200, 422), resp.text
        if resp.status_code == 200:
            ids = {s["id"] for day in resp.json() for s in day["surgeries"]}
            assert world["med_case"] not in ids
            assert SECRET_NAME not in resp.text


def test_a_role_without_surgical_rights_never_reaches_the_scope_check(world):
    """`pharmacist` holds neither surgery.read nor surgery.write, so
    require_permission 403s first. Pinned because the fix must not have
    accidentally converted a permission denial into a 404 (which would hide
    misconfiguration from an operator)."""
    pharmacist = _mk_user("pharmacist", DEPT_CARD, "PHA")
    headers = _auth(pharmacist, "pharmacist")
    assert client.get("/api/surgery/schedule", headers=headers).status_code == 403
    assert client.get(f"/api/surgery/{world['med_case']}", headers=headers).status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
#  THEATRE — the half a security-only fix would have destroyed
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("actor", ["surgeon", "anaesthetist"])
def test_surgical_staff_still_see_the_whole_theatre_list(world, actor):
    """A General Surgery surgeon operates on a General Medicine patient, and an
    anaesthetist covers every ward from a department that admits nobody. Ward-
    scoping either of them would have emptied the operating list — the screen's
    entire purpose. Hospital-wide by role, and that is a property of the USER
    ROW (users.department), not something the request can ask for."""
    headers = _auth(world[actor])

    sched, _ = _schedule_ids(headers)
    assert {world["med_case"], world["med_emergency"], world["card_case"]} <= sched

    queue, _ = _queue_ids(headers)
    assert world["med_emergency"] in queue

    cases, _ = _case_ids(headers)
    assert {world["med_case"], world["card_case"]} <= cases

    detail = client.get(f"/api/surgery/{world['med_case']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["patient_name"] == SECRET_NAME


def test_governance_account_with_no_department_stays_hospital_wide(world):
    """The pre-existing convention (permissions.department_scope): an account
    with no department on record is not ward-scoped anywhere. Surgery does not
    invent an exception to it."""
    headers = _auth(world["governance"], "admin")
    sched, _ = _schedule_ids(headers)
    assert {world["med_case"], world["card_case"]} <= sched


def test_the_owning_ward_still_sees_its_own_patient_going_to_theatre(world):
    """Clause (C). The General Medicine team must see that their own patient is
    booked for theatre — losing that would be a clinical-safety regression
    dressed up as a security win."""
    headers = _auth(world["med_doc"])
    sched, _ = _schedule_ids(headers)
    assert {world["med_case"], world["med_emergency"]} <= sched
    assert world["card_case"] not in sched

    detail = client.get(f"/api/surgery/{world['med_case']}", headers=headers)
    assert detail.status_code == 200, detail.text


def test_an_out_of_department_consultant_on_the_roster_can_still_work(world):
    """Clause (D). A cardiologist pulled onto a GM patient's case must be able to
    open the case they were alerted to and answer the alert — and must NOT
    thereby gain the rest of that ward's operating list."""
    headers = _auth(world["card_consultant"])

    assert client.get(f"/api/surgery/{world['med_case']}",
                      headers=headers).status_code == 200
    resp = client.post(f"/api/surgery/{world['med_case']}/team-response",
                       json={"action": "confirm"}, headers=headers)
    assert resp.status_code == 200, resp.text

    sched, _ = _schedule_ids(headers)
    assert world["med_case"] in sched
    assert world["med_emergency"] not in sched, "the roster grants ONE case, not the ward"


# ═══════════════════════════════════════════════════════════════════════════
#  AUDIT — the disclosure has to be provable afterwards
# ═══════════════════════════════════════════════════════════════════════════

def test_phi_access_is_a_fail_closed_category():
    """The whole promise of the wrapper rests on this membership."""
    from services.audit_service import _FAIL_CLOSED_CATEGORIES
    assert "phi_access" in _FAIL_CLOSED_CATEGORIES


@pytest.mark.parametrize("path_kind,action_type", [
    ("schedule", "surgery_schedule_viewed"),
    ("queue", "surgery_emergency_queue_viewed"),
    ("cases", "surgery_cases_viewed"),
    ("detail", "surgery_detail_viewed"),
    ("checklist", "surgery_checklist_viewed"),
    ("risk", "surgery_risk_assessment_viewed"),
])
def test_every_surgical_read_writes_a_phi_access_row(world, monkeypatch,
                                                    path_kind, action_type):
    """Was: none of the six wrote anything. The audit middleware fires on
    POST/PUT/PATCH/DELETE only, so the hospital could answer "who changed this
    case" and not "who read this patient's operation" — the question an incident
    actually asks."""
    _stub_risk_stream(monkeypatch)
    actor = world["surgeon"]
    headers = _auth(actor)
    sid = world["med_case"]
    url = {
        "schedule": "/api/surgery/schedule?days=2",
        "queue": "/api/surgery/emergency-queue",
        "cases": "/api/surgery/cases",
        "detail": f"/api/surgery/{sid}",
        "checklist": f"/api/surgery/{sid}/checklist",
        "risk": f"/api/surgery/{sid}/risk",
    }[path_kind]

    marker = _max_audit_id()
    assert client.get(url, headers=headers).status_code == 200
    rows = [r for r in _phi_rows_since(marker, actor) if r["action_type"] == action_type]
    assert rows, f"no phi_access row for {action_type}"

    row = rows[-1]
    assert row["outcome"] == "success"
    assert row["actor_user_id"] == actor
    meta = json.loads(row["metadata_json"])
    # WHICH CLAUSE let the read through. Without this the trail can say a read
    # happened but not why it was allowed, and the hospital-wide clause would be
    # exactly as invisible as the leak it replaces.
    assert meta["surgical_scope"] == "perioperative"
    assert meta["scope_source"] == "authenticated_user"
    if path_kind in _READ_PATHS:
        assert row["patient_id"] == world["med_patient"]
        assert row["target_id"] == sid


def test_the_audit_row_names_the_clause_that_allowed_the_read(world):
    """Three callers, three different clauses, all distinguishable afterwards."""
    for actor, role, expected in (("surgeon", "doctor", "perioperative"),
                                  ("med_doc", "doctor", "department"),
                                  ("governance", "admin", "hospital_wide")):
        marker = _max_audit_id()
        resp = client.get(f"/api/surgery/{world['med_case']}",
                          headers=_auth(world[actor], role))
        assert resp.status_code == 200, resp.text
        rows = [r for r in _phi_rows_since(marker, world[actor])
                if r["action_type"] == "surgery_detail_viewed"]
        assert rows, actor
        assert json.loads(rows[-1]["metadata_json"])["surgical_scope"] == expected, actor


def test_a_refused_read_files_no_phi_access_row(world):
    """A 404 disclosed nothing, so it must not be recorded as a disclosure —
    otherwise the phi_access table stops meaning "PHI left the building"."""
    actor = world["card_doc"]
    marker = _max_audit_id()
    assert client.get(f"/api/surgery/{world['med_case']}",
                      headers=_auth(actor)).status_code == 404
    assert _phi_rows_since(marker, actor) == []


def test_the_phi_audit_is_fail_closed_no_row_means_no_phi(world, monkeypatch):
    """`phi_access` is fail-closed: if the row cannot be persisted the request
    must fail and the PHI must NOT be returned. Asserted by breaking the audit
    writer, not by trusting the category list."""
    import permissions

    def _explode(*a, **kw):
        raise RuntimeError("audit chain unavailable")

    monkeypatch.setattr(permissions, "log_audit_event", _explode)

    resp = lenient.get(f"/api/surgery/{world['med_case']}",
                       headers=_auth(world["surgeon"]))
    assert resp.status_code >= 500, resp.text
    assert SECRET_NAME not in resp.text
    assert SECRET_DIAGNOSIS not in resp.text


# ═══════════════════════════════════════════════════════════════════════════
#  MUTATIONS — the same gate on the writes
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("verb,suffix,body", [
    ("post", "/start", None),
    ("post", "/complete", None),
    ("post", "/prep", None),
    ("post", "/checklist", {"item_id": "si_identity", "checked": True}),
    ("post", "/alert-team", {"team": [{"user_id": "SGADM", "role": "Surgeon"}]}),
    ("post", "/team-response", {"action": "confirm"}),
])
def test_out_of_department_clinician_cannot_act_on_another_wards_case(
        world, verb, suffix, body):
    """The reads were the reported defect; the writes are the same hole. In
    particular `/complete` AUTHORS A POST-OP NOTE INTO THE PATIENT'S CHART, so
    unscoped it let any authenticated clinician write into another ward's
    record. 404, same as the reads.

    The case is `scheduled`, has a real case room and a real team member, so
    every one of these calls would otherwise reach its own success or business
    error — the 404 asserted here can only be the scope gate.
    """
    headers = _auth(world["card_doc"])
    resp = getattr(client, verb)(f"/api/surgery/{world['med_case']}{suffix}",
                                 json=body, headers=headers)
    assert resp.status_code == 404, f"{suffix} -> {resp.status_code} {resp.text}"
    assert resp.json()["detail"] == "Surgery not found"

    # And the case was not moved, and nothing was written into the case room.
    with db_session() as db:
        status = db.execute("SELECT status FROM surgeries WHERE id = ?",
                            (world["med_case"],)).fetchone()["status"]
        posted = db.execute("SELECT COUNT(*) AS n FROM messages WHERE channel_id = ?",
                            (world["med_room"],)).fetchone()["n"]
    assert status == "scheduled"
    assert posted == 0


def test_emergency_acknowledge_is_scoped(world):
    headers = _auth(world["card_doc"])
    resp = client.post(f"/api/surgery/emergency/{world['med_emergency']}/acknowledge",
                       headers=headers)
    assert resp.status_code == 404, resp.text
    with db_session() as db:
        status = db.execute("SELECT status FROM surgeries WHERE id = ?",
                            (world["med_emergency"],)).fetchone()["status"]
    assert status == "emergency_pending"


def test_surgical_staff_can_still_drive_the_case(world):
    """The mutation gate must not have broken the theatre either."""
    headers = _auth(world["surgeon"])
    resp = client.post(f"/api/surgery/{world['med_case']}/checklist",
                       json={"item_id": "si_identity", "checked": True},
                       headers=headers)
    assert resp.status_code == 200, resp.text
    resp = client.post(f"/api/surgery/emergency/{world['med_emergency']}/acknowledge",
                       headers=headers)
    assert resp.status_code == 200, resp.text


# ═══════════════════════════════════════════════════════════════════════════
#  DRIFT GUARD
# ═══════════════════════════════════════════════════════════════════════════

def test_no_surgery_route_reads_patients_without_going_through_the_scope_module():
    """A ratchet. Every route in routes/surgery_routes.py that JOINs `patients`
    must derive scope from services/surgery_scope.py — the leak happened because
    six queries grew a `JOIN patients` and nothing else. A new one added later
    fails here instead of shipping.
    """
    import inspect
    import routes.surgery_routes as sr

    src = inspect.getsource(sr)
    assert "surgical_list_filter" in src
    assert "enforce_surgery_scope" in src

    for name, fn in vars(sr).items():
        if not callable(fn) or not hasattr(fn, "__code__"):
            continue
        if getattr(fn, "__module__", None) != sr.__name__:
            continue
        try:
            body = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if "JOIN patients" not in body:
            continue
        assert any(tok in body for tok in ("surgical_list_filter",
                                           "enforce_surgery_scope",
                                           "surgery_in_scope_blocking")), (
            f"{name} JOINs patients with no surgical scope derivation")
