"""Ward scoping on the nurse OBSERVATION WRITE — /api/nurse/patients/{id}/*.

THE FINDING (routes/nurse_routes.py:121, pre-fix). Every READ surface in the app
was ward-scoped over 2026-08-05/06, and the drift guard installed to keep them
that way — tests/test_patient_scope_routes.py:522 — enumerates only routes where
``"GET" in methods``. So the sweep that closed the read leak walked straight past
the two POSTs the nurse tablet actually uses, and left this:

    POST /api/nurse/patients/{ANY_PATIENT}/observations
        → 200. A vitals row, a risk_scores row, a NEWS2, and a Vigil escalation
          on a patient the same nurse is correctly 404'd from READING.

Write-without-read is the sharper half of the hole. Two distinct harms:

  1. MODIFICATION OF AN INVISIBLE RECORD. The caller cannot open the chart, so
     she cannot see what she just did to it, and neither can she undo it.
  2. A FABRICATED DETERIORATION ON A STRANGER. record_observation computes NEWS2
     and, since 2026-08-06, dispatches a Vigil alert to the patient's Relay case
     room. Peri-arrest values keyed onto the wrong patient_id summon a clinical
     response for someone who is fine — and bury the ward in an escalation whose
     origin nobody on that ward can account for.

And the score-only preview (``/news2-preview``, persists nothing) is not
harmless either: it answers two questions about a stranger. See
``test_the_preview_is_an_existence_oracle_for_a_stranger`` and
``test_the_preview_discloses_the_spo2_scale_of_a_stranger`` below.

THE WRONG-COLUMN TRAP. A patient's department lives on ``admissions.department``
(a name — "General Medicine"); ``patients.ward`` is a free-text bed label
("GM-1") that nothing constrains. A fix that scopes the wrong column passes a
naive fixture and silently stops scoping. So the fixtures here deliberately
CROSS the two: the out-of-ward patient's ``patients.ward`` is set to the
caller's OWN department, and the caller's own patient has the other ward's name
in ``patients.ward``. Scoping ``patients.ward`` therefore fails in both
directions at once — it lets the cross-ward write through AND blocks the
legitimate one.
"""
import inspect
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from routes.nurse_routes import router as nurse_router


# Distinctive names so this module cannot collide with another module's fixtures
# in the shared session DB.
DEPT_A = "Nurse Write Scope Cardiology"
DEPT_B = "Nurse Write Scope Oncology"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nurse_router)
    return TestClient(app)


def _auth(uid, role="nurse"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(role, department):
    uid = f"NWS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Nurse Write Scope {role}", role, department),
        )
    return uid


def _mk_patient(department, *, ward_label, on_scale_2=0):
    """An admitted patient whose department lives on the ADMISSION.

    ``ward_label`` goes into ``patients.ward`` and is deliberately NOT the
    department — see the module docstring's wrong-column trap.
    """
    pid = f"NWSP{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('NWSADM', 'Nurse Write Scope Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date, on_scale_2)
               VALUES (?, ?, ?, 'B-4', ?, 67, 'F', 'Nurse write scope fixture',
                       'admitted', ?, ?)""",
            (pid, f"NWS {department}", f"MRN-{uuid.uuid4().hex[:8].upper()}",
             ward_label, now, on_scale_2),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, 'B-4', 'active', 'NWSADM')",
            (pid, department, ward_label),
        )
    return pid


@pytest.fixture()
def world():
    """Two wards, crossed labels, and the four accounts the boundary needs.

    THE TRAP, explicitly:
      p_b.admissions.department = DEPT_B  but  p_b.patients.ward = DEPT_A
      p_a.admissions.department = DEPT_A  but  p_a.patients.ward = DEPT_B

    A fix that scoped ``patients.ward`` would let nurse_a write to p_b (wrong
    direction 1) and refuse her own p_a (wrong direction 2).
    """
    return {
        # nurse_a's own patient — ward label says the OTHER department.
        "p_a": _mk_patient(DEPT_A, ward_label=DEPT_B),
        # The stranger — ward label says nurse_a's OWN department.
        "p_b": _mk_patient(DEPT_B, ward_label=DEPT_A, on_scale_2=1),
        "nurse_a": _mk_user("nurse", DEPT_A),
        "doctor_a": _mk_user("doctor", DEPT_A),
        # A genuinely UNSCOPED account: department is NULL on the user row, which
        # only an administrator can set. `None` means "not ward-scoped", NOT
        # "scope unknown" — permissions.department_scope's standing convention.
        "unscoped_doctor": _mk_user("doctor", None),
        "unscoped_admin": _mk_user("admin", None),
    }


_PERI_ARREST = {
    "heart_rate": 138, "systolic_bp": 78, "diastolic_bp": 44,
    "respiratory_rate": 32, "temperature": 39.4, "spo2": 86,
    "consciousness": "voice", "on_oxygen": True,
}
_BENIGN = {"heart_rate": 74, "systolic_bp": 122, "diastolic_bp": 78,
           "respiratory_rate": 16, "temperature": 36.8, "spo2": 98,
           "consciousness": "alert", "on_oxygen": False}


def _charted(patient_id):
    """Every nurse-entered vitals row on this patient."""
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM vitals WHERE patient_id = ? AND source = ?",
            (patient_id, "nurse_manual"),
        ).fetchone()["n"]


def _risk_rows(patient_id):
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM risk_scores WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()["n"]


def _audit_count(action_type, patient_id):
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM audit_logs "
            "WHERE action_type = ? AND patient_id = ?",
            (action_type, patient_id),
        ).fetchone()["n"]


# ═════════════════════════════════════════════════════════════════════════════
# 1. The write itself
# ═════════════════════════════════════════════════════════════════════════════

def test_a_nurse_cannot_chart_observations_on_another_wards_patient(world, client):
    """The headline. DEPT_A nurse, DEPT_B patient, peri-arrest values."""
    before_vitals, before_risk = _charted(world["p_b"]), _risk_rows(world["p_b"])

    resp = client.post(
        f"/api/nurse/patients/{world['p_b']}/observations",
        json={**_PERI_ARREST, "taken_at": datetime.now(timezone.utc).isoformat()},
        headers=_auth(world["nurse_a"]),
    )

    assert resp.status_code == 404, (
        "a ward nurse charted a full observation set onto a patient on another "
        f"ward — one she is correctly 404'd from reading: {resp.text}"
    )
    assert _charted(world["p_b"]) == before_vitals, (
        "the refusal was cosmetic — a vitals row landed on the stranger's chart"
    )
    assert _risk_rows(world["p_b"]) == before_risk, (
        "a NEWS2 was computed and stored for a patient nobody intended: this is "
        "the fabricated-deterioration path, and it summons a clinical response"
    )


def test_the_refusal_is_404_and_never_403(world, client):
    """404, not 403. A 403 confirms the patient exists, which is the disclosure
    the read paths were fixed to stop making — the write must not reopen it via
    its status code. A scoped caller cannot tell 'no such patient' from 'not
    your ward'."""
    real = client.post(
        f"/api/nurse/patients/{world['p_b']}/observations",
        json={**_BENIGN, "taken_at": datetime.now(timezone.utc).isoformat()},
        headers=_auth(world["nurse_a"]),
    )
    invented = client.post(
        "/api/nurse/patients/NWS-NO-SUCH-PATIENT/observations",
        json={**_BENIGN, "taken_at": datetime.now(timezone.utc).isoformat()},
        headers=_auth(world["nurse_a"]),
    )
    assert real.status_code == 404, real.text
    assert real.status_code == invented.status_code, (
        "a real out-of-ward patient and an invented id answer differently — the "
        "status code is an existence oracle for the whole census"
    )


def test_she_can_still_chart_on_her_own_ward(world, client):
    """The fix must not turn the nurse's core job off.

    This is also half the wrong-column trap: p_a's ``patients.ward`` holds
    DEPT_B, so a fix that scoped ``ward`` instead of ``admissions.department``
    would 404 a nurse charting her OWN patient.
    """
    resp = client.post(
        f"/api/nurse/patients/{world['p_a']}/observations",
        json={**_BENIGN, "taken_at": datetime.now(timezone.utc).isoformat()},
        headers=_auth(world["nurse_a"]),
    )
    assert resp.status_code == 200, (
        f"the ward's own nurse can no longer chart her own patient: {resp.text}"
    )
    assert resp.json()["news2"]["raw_news2"] == 0
    assert _charted(world["p_a"]) == 1


def test_a_doctor_is_scoped_the_same_way(world, client):
    """vitals.write is held by nurse AND doctor. Scoping only the role everyone
    was looking at is how the next hole gets made."""
    resp = client.post(
        f"/api/nurse/patients/{world['p_b']}/observations",
        json={**_BENIGN, "taken_at": datetime.now(timezone.utc).isoformat()},
        headers=_auth(world["doctor_a"], "doctor"),
    )
    assert resp.status_code == 404, resp.text
    assert _charted(world["p_b"]) == 0


def test_an_unscoped_account_still_charts_anywhere(world, client):
    """None from department_scope means a genuinely UNSCOPED account, not
    'scope unknown'. The fix must not turn a hospital-wide clinical account into
    someone who can write nothing.

    Uses a DOCTOR with no department, not an admin: `vitals.write` is a
    nurse/doctor permission, so an admin is 403'd by the role gate before the
    ward check is reached and would prove nothing about scoping.
    """
    hdr = _auth(world["unscoped_doctor"], "doctor")
    for pid in (world["p_a"], world["p_b"]):
        resp = client.post(
            f"/api/nurse/patients/{pid}/observations",
            json={**_BENIGN, "taken_at": datetime.now(timezone.utc).isoformat()},
            headers=hdr,
        )
        assert resp.status_code == 200, (
            f"an unscoped account was refused patient {pid}: {resp.text}"
        )
        assert _charted(pid) == 1


def test_the_role_gate_still_fires_before_the_ward_check(world, client):
    """A pharmacist has neither vitals.write nor vitals.read. She must get 403
    (wrong role), not 404 — the ward check is not a substitute for the role
    check, and ordering them the other way would hide role errors as 404s."""
    pharm = _mk_user("pharmacist", DEPT_A)
    hdr = _auth(pharm, "pharmacist")
    assert client.post(f"/api/nurse/patients/{world['p_a']}/observations",
                       json=_BENIGN, headers=hdr).status_code == 403
    assert client.post(f"/api/nurse/patients/{world['p_a']}/news2-preview",
                       json=_BENIGN, headers=hdr).status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
# 2. The score-only preview — persists nothing, discloses two things
# ═════════════════════════════════════════════════════════════════════════════

def test_the_preview_is_an_existence_oracle_for_a_stranger(world, client):
    """`/news2-preview` writes nothing, so it looks harmless. It is not.

    Pre-fix it answered 200 for a patient that exists and 422 ("patient … not
    found", raised by _on_scale_2) for one that does not — so a nurse could
    enumerate the hospital's entire patient-id space from a route that only
    claims to do arithmetic. That is the DISCOVERY half of the 2026-08-05 leak,
    rebuilt out of a status code.
    """
    stranger = client.post(
        f"/api/nurse/patients/{world['p_b']}/news2-preview",
        json=_BENIGN, headers=_auth(world["nurse_a"]),
    )
    invented = client.post(
        "/api/nurse/patients/NWS-NO-SUCH-PATIENT/news2-preview",
        json=_BENIGN, headers=_auth(world["nurse_a"]),
    )
    assert stranger.status_code == 404, (
        f"the preview confirmed an out-of-ward patient exists: {stranger.text}"
    )
    assert stranger.status_code == invented.status_code, (
        "'exists, other ward' and 'does not exist' are distinguishable through "
        "the preview — the route is an id oracle"
    )


def test_the_preview_discloses_the_spo2_scale_of_a_stranger(world, client):
    """The second, quieter leak: the RESPONSE carries a fact about the patient.

    score_observation reads ``patients.on_scale_2`` and scores accordingly, and
    returns ``news2.spo2_scale``. SpO2 Scale 2 is charted only for a patient in
    chronic hypercapnic respiratory failure (target 88-92%) — so submitting an
    arbitrary probe value against a stranger's id and reading the scale back
    discloses a diagnosis, from a route that stores nothing. p_b is on Scale 2;
    the caller must not learn that.
    """
    resp = client.post(
        f"/api/nurse/patients/{world['p_b']}/news2-preview",
        json={"spo2": 90, "respiratory_rate": 18},
        headers=_auth(world["nurse_a"]),
    )
    assert resp.status_code == 404, resp.text
    assert "spo2_scale" not in resp.text, (
        "the preview handed back a clinical property of a patient the caller "
        f"cannot read: {resp.text}"
    )


def test_the_preview_still_works_on_the_callers_own_ward(world, client):
    """Live-as-you-type scoring is the whole point of the screen. p_a's
    ``patients.ward`` is the OTHER department — the wrong-column trap again."""
    resp = client.post(
        f"/api/nurse/patients/{world['p_a']}/news2-preview",
        json={"spo2": 90, "respiratory_rate": 18},
        headers=_auth(world["nurse_a"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["news2"]["spo2_scale"] == 1


def test_an_unscoped_admin_can_still_preview_anywhere(world, client):
    """An admin holds vitals.read but not vitals.write, so the preview is the
    only one of the two routes the ward check can be observed on for a
    governance account. Unscoped must still mean unscoped."""
    hdr = _auth(world["unscoped_admin"], "admin")
    for pid in (world["p_a"], world["p_b"]):
        resp = client.post(f"/api/nurse/patients/{pid}/news2-preview",
                           json=_BENIGN, headers=hdr)
        assert resp.status_code == 200, (
            f"an unscoped governance account was refused patient {pid}: {resp.text}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 3. The trail — a clinical write, and a refused one, must both be provable
# ═════════════════════════════════════════════════════════════════════════════

def test_a_cross_ward_write_attempt_is_recorded_as_denied(world, client):
    """The refusal is the interesting event.

    A nurse probing other wards' ids leaves nothing but generic middleware rows
    unless the scope denial is named. `patient_scope_denied` is what the read
    paths already write (permissions.require_patient_access), and the write path
    is held to the same standard rather than given a quieter rule of its own.
    """
    before = _audit_count("patient_scope_denied", world["p_b"])
    client.post(
        f"/api/nurse/patients/{world['p_b']}/observations",
        json={**_PERI_ARREST, "taken_at": datetime.now(timezone.utc).isoformat()},
        headers=_auth(world["nurse_a"]),
    )
    assert _audit_count("patient_scope_denied", world["p_b"]) == before + 1, (
        "a cross-ward charting attempt left no named trace — after an incident "
        "the hospital cannot answer 'who tried to write to this record'"
    )


def test_the_observation_write_itself_is_audited(world, client):
    """A clinical write with no audit trail is its own finding. It must name the
    actor, the patient, and the row it created."""
    before = _audit_count("observation_recorded", world["p_a"])
    resp = client.post(
        f"/api/nurse/patients/{world['p_a']}/observations",
        json={**_BENIGN, "taken_at": datetime.now(timezone.utc).isoformat()},
        headers=_auth(world["nurse_a"]),
    )
    assert resp.status_code == 200, resp.text
    assert _audit_count("observation_recorded", world["p_a"]) == before + 1

    with db_session() as db:
        row = db.execute(
            "SELECT actor_user_id, target_type, target_id, event_category "
            "FROM audit_logs WHERE action_type = 'observation_recorded' "
            "AND patient_id = ? ORDER BY id DESC LIMIT 1",
            (world["p_a"],),
        ).fetchone()
    assert row["actor_user_id"] == world["nurse_a"]
    assert row["target_type"] == "vitals"
    assert row["target_id"] == str(resp.json()["vitals_id"])
    # clinical_access is fail-closed (services/audit_service:_FAIL_CLOSED_CATEGORIES):
    # the reading and its audit row commit or roll back together.
    assert row["event_category"] == "clinical_access"


# ═════════════════════════════════════════════════════════════════════════════
# 4. The structural reason this survived — and a guard that would have caught it
# ═════════════════════════════════════════════════════════════════════════════

def test_the_existing_drift_guard_only_looks_at_GET():
    """Names the blind spot rather than leaving it implicit.

    tests/test_patient_scope_routes.py enumerates the live app for
    ``/{patient_id}`` routes and requires the guard — but filters on
    ``"GET" in methods``. That is why a two-line POST hole survived a sweep
    that was specifically looking for this defect class. If someone widens that
    guard to mutations, this test fails and should simply be deleted.

    READ FROM DISK, not via ``inspect.getsource``. getsource resolves a function
    through its code object's (filename, first line number), so it slices the
    file at offsets fixed when the module was imported. Edit that file while a
    28-minute suite is running — routine in this repo — and it hands back a
    DIFFERENT function's body with no error. It did exactly that on 2026-08-15,
    returning ``_source_really_belongs_to`` (the helper the same file keeps
    precisely because getsource mis-attributes) and failing this assertion while
    the guard it names was intact. A test that reports "the guard changed shape"
    when the guard has not changed is worse than no test: it spends the one
    signal it has.
    """
    target = "def test_every_patient_id_route_that_returns_phi_carries_the_guard():"
    text = (Path(__file__).parent / "test_patient_scope_routes.py").read_text()
    assert target in text, "the read drift guard was renamed or removed — re-check it"
    body = text.split(target, 1)[1]
    # Stop at the next top-level definition so this reads THAT function only.
    for marker in ("\ndef ", "\nclass "):
        body = body.split(marker, 1)[0]
    assert '"GET" not in methods' in body, (
        "the read drift guard changed shape — re-check whether mutations are "
        "now covered, and delete this test if they are"
    )


# Mutation routes that take a patient_id off the PATH and derive no ward scope.
# Inventory taken 2026-08-07 while fixing the nurse write hole. These live in
# route files this change does not own; each is reported to the caller rather
# than edited here. The assertion below is a RATCHET — a route may leave this
# set (someone fixed it) but a NEW bare mutation route fails the test.
_KNOWN_BARE_PATIENT_ID_MUTATIONS = {
    # admin.overview only; erasure is a governance action, but it still names a
    # patient off the path and derives no scope.
    ("POST", "/api/admin/patients/{patient_id}/erase"),
    # patient.write. Deactivates an allergy — the row the allergy interlock
    # matches on. A cross-ward delete here silently disarms a dosing block.
    ("DELETE", "/api/patients/{patient_id}/allergies/{allergy_id}"),
    # vitals.read. The two GETs beside it in downtime_routes.py wear
    # require_patient_access; the POST does not. Same read/write split as the
    # nurse hole.
    ("POST", "/api/downtime/capture/{patient_id}"),
    # (POST /api/vigil/patients/{patient_id}/discharge left this set on
    # 2026-10-03: it now wears require_patient_access. Asserted below so it can
    # never be re-admitted.)
}


def test_no_new_mutation_route_takes_a_patient_id_without_a_ward_check():
    """The guard the read sweep did not have.

    Enumerates the LIVE app for POST/PUT/PATCH/DELETE routes with
    ``{patient_id}`` on the path and asserts each either derives scope from the
    authenticated user or is on the written-down inventory above. A route added
    tomorrow is either wearing the guard or fails here — which is what the read
    guard does for GETs, and did not do for writes.
    """
    from main import app as live_app

    guards = ("require_patient_access", "_authorize_patient_access",
              "enforce_patient_scope", "patient_in_scope", "department_scope",
              "require_patient_self")
    mutating = {"POST", "PUT", "PATCH", "DELETE"}

    checked, bare = 0, set()
    for route in live_app.routes:
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", set()) or set()) & mutating
        if "{patient_id}" not in path or not methods:
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        try:
            src = inspect.getsource(endpoint)
        except (OSError, TypeError):
            continue
        checked += 1
        if not any(g in src for g in guards):
            bare |= {(m, path) for m in methods}

    # Non-vacuity: if the enumeration ever stops finding routes, this test would
    # pass forever while guarding nothing.
    assert checked >= 10, (
        f"only {checked} /{{patient_id}} mutation routes enumerated — the "
        f"enumeration is broken, not the app"
    )
    # The two routes this change owns must NOT be bare, stated separately so the
    # ratchet cannot ever be widened to re-admit them.
    for m, p in (("POST", "/api/nurse/patients/{patient_id}/observations"),
                 ("POST", "/api/nurse/patients/{patient_id}/news2-preview"),
                 ("POST", "/api/vigil/patients/{patient_id}/discharge"),
                 ("POST", "/api/vigil/patients/{patient_id}/discharge-summary/review")):
        assert (m, p) not in bare, f"{m} {p} lost its ward check"

    new = bare - _KNOWN_BARE_PATIENT_ID_MUTATIONS
    assert not new, (
        "these mutation routes take a patient id off the path and derive no "
        "ward scope from the caller — the exact shape of the nurse-observation "
        "hole:\n  " + "\n  ".join(f"{m} {p}" for m, p in sorted(new))
    )


# ═════════════════════════════════════════════════════════════════════════════
# 5. Writes keyed by a SURROGATE id: the blind spot of the guard above
# ═════════════════════════════════════════════════════════════════════════════
#
# The ratchet above only sees ``{patient_id}`` on the path. A write keyed by a
# medication, order or bundle id names exactly one patient just as surely, and
# POST /api/emar/medications/{med_id}/administer let any nurse chart a dose on
# any inpatient in the hospital (found 2026-10-01). Those routes are invisible
# to require_patient_access, which reads the path, so each must resolve its
# patient and call enforce_patient_scope (or an equivalent) itself.

#: Path parameters that resolve to ONE patient's clinical record.
_PATIENT_SURROGATE_PARAMS = {
    "med_id", "medication_id", "administration_id", "order_id", "bundle_id",
    "note_id", "surgery_id", "escalation_id", "insight_id", "analysis_id",
    "appointment_id", "visit_id",
}

# Surrogate-id writes whose ENDPOINT shows no scope check, inventoried
# 2026-10-03. Some may scope inside their service, which this source scan cannot
# see; each is in a file this change does not own and is reported, not edited.
# A RATCHET like the one above: an entry may leave (someone added the guard),
# but a NEW bare surrogate-id write fails the test.
_KNOWN_BARE_SURROGATE_MUTATIONS = {
    # The appointment cancel and complete left this list on 9 October 2026
    # (opd-16): both now derive department_scope and 404 another ward's
    # appointment (tests/test_appointments_slots.py probes it live).
    ("POST", "/api/bundles/{bundle_id}/cancel"),
    ("POST", "/api/bundles/{bundle_id}/complete/{element}"),
    ("POST", "/api/frontdesk/opd/visits/{visit_id}/cancel"),
    ("POST", "/api/frontdesk/opd/visits/{visit_id}/complete"),
    ("POST", "/api/frontdesk/opd/visits/{visit_id}/start"),
    ("POST", "/api/orders/{order_id}/result"),
    ("POST", "/api/orders/{order_id}/transition"),
    # Pharmacists hold no department by design (migration 36): these are
    # hospital-wide for them, which is the point of the pharmacy desk.
    ("POST", "/api/pharmacy/{med_id}/reject"),
    ("POST", "/api/pharmacy/{med_id}/verify"),
    ("PATCH", "/api/scribe/notes/{note_id}"),
    ("POST", "/api/scribe/notes/{note_id}/reject"),
    ("POST", "/api/scribe/notes/{note_id}/revise"),
    ("POST", "/api/scribe/notes/{note_id}/sign"),
    ("POST", "/api/scribe/notes/{note_id}/verify"),
    ("POST", "/api/sage/insights/{insight_id}/act"),
    ("POST", "/api/sage/insights/{insight_id}/transition"),
    ("POST", "/api/surgery/emergency/{surgery_id}/acknowledge"),
    ("POST", "/api/surgery/{surgery_id}/alert-team"),
    ("POST", "/api/surgery/{surgery_id}/checklist"),
    ("POST", "/api/surgery/{surgery_id}/complete"),
    ("POST", "/api/surgery/{surgery_id}/prep"),
    ("POST", "/api/surgery/{surgery_id}/start"),
    ("POST", "/api/surgery/{surgery_id}/team-response"),
    ("POST", "/api/vigil/escalations/{escalation_id}/acknowledge"),
    ("POST", "/api/vigil/escalations/{escalation_id}/resolve"),
}


def _surrogate_write_routes():
    """Every live POST/PUT/PATCH/DELETE keyed by a patient surrogate id, as
    {(method, path): endpoint source}."""
    import re
    from main import app as live_app

    mutating = {"POST", "PUT", "PATCH", "DELETE"}
    found = {}
    for route in live_app.routes:
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", set()) or set()) & mutating
        params = set(re.findall(r"\{(\w+)(?::\w+)?\}", path))
        if not methods or "patient_id" in params or not (params & _PATIENT_SURROGATE_PARAMS):
            continue
        endpoint = getattr(route, "endpoint", None)
        try:
            src = inspect.getsource(endpoint)
        except (OSError, TypeError):
            continue
        for m in methods:
            found[(m, path)] = src
    return found


def test_no_new_surrogate_id_write_derives_no_ward_scope():
    """Lists every surrogate-id write route and holds the bare ones to the
    inventory above. The eMAR administer route must never be among them."""
    guards = ("require_patient_access", "_authorize_patient_access",
              "enforce_patient_scope", "patient_in_scope", "department_scope",
              "require_patient_self", "_enforce_medication_scope")
    routes = _surrogate_write_routes()
    assert len(routes) >= 20, (
        f"only {len(routes)} surrogate-id write routes enumerated: the "
        "enumeration is broken, not the app"
    )
    bare = {key for key, src in routes.items() if not any(g in src for g in guards)}

    assert ("POST", "/api/emar/medications/{med_id}/administer") in routes, (
        "the eMAR administer route moved; re-point this guard at it"
    )
    assert ("POST", "/api/emar/medications/{med_id}/administer") not in bare, (
        "the eMAR administer route lost its ward check"
    )
    new = bare - _KNOWN_BARE_SURROGATE_MUTATIONS
    assert not new, (
        "these writes are keyed by a patient's medication/order/bundle/note id "
        "and derive no ward scope from the caller:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in sorted(new))
    )


# ═════════════════════════════════════════════════════════════════════════════
# 6. Live cross-ward probes on the two writes fixed 2026-10-03
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def clinical_client():
    from routes.emar_routes import router as emar_router
    from routes.vigil_routes import router as vigil_router
    app = FastAPI()
    app.include_router(vigil_router)
    app.include_router(emar_router)
    return TestClient(app)


def _mk_med(patient_id):
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, prescribed_by, source)
               VALUES (?, 'Paracetamol', '500 mg', 'Oral', 'QID', 'active', 'NWSADM', 'clinician')""",
            (patient_id,),
        )
        return cur.lastrowid


def _administrations(med_id):
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM medication_administrations WHERE medication_id = ?",
            (med_id,),
        ).fetchone()["n"]


def test_a_nurse_cannot_chart_a_dose_on_another_wards_patient(world, clinical_client):
    med = _mk_med(world["p_b"])
    resp = clinical_client.post(
        f"/api/emar/medications/{med}/administer",
        json={"status": "given", "confirmation_method": "typed"},
        headers=_auth(world["nurse_a"]),
    )
    assert resp.status_code == 404, (
        f"a nurse charted a dose on a patient on another ward: {resp.status_code} {resp.text}"
    )
    assert _administrations(med) == 0, "the refusal was cosmetic: a dose was charted"
    assert _audit_count("patient_scope_denied", world["p_b"]) >= 1, (
        "the cross-ward attempt left no trace in the audit trail"
    )


def test_she_can_still_chart_a_dose_on_her_own_ward(world, clinical_client):
    med = _mk_med(world["p_a"])
    resp = clinical_client.post(
        f"/api/emar/medications/{med}/administer",
        json={"status": "not_given", "not_given_reason": "Patient asleep",
              "confirmation_method": "typed"},
        headers=_auth(world["nurse_a"]),
    )
    assert resp.status_code == 200, resp.text
    assert _administrations(med) == 1


def test_a_doctor_cannot_discharge_another_wards_patient(world, clinical_client):
    resp = clinical_client.post(
        f"/api/vigil/patients/{world['p_b']}/discharge",
        headers=_auth(world["doctor_a"], "doctor"),
    )
    assert resp.status_code == 404, (
        f"a doctor discharged a patient on another ward: {resp.status_code} {resp.text}"
    )
    with db_session() as db:
        status = db.execute(
            "SELECT status FROM patients WHERE id = ?", (world["p_b"],)
        ).fetchone()["status"]
        summaries = db.execute(
            "SELECT COUNT(*) AS n FROM discharge_summaries WHERE patient_id = ?",
            (world["p_b"],),
        ).fetchone()["n"]
    assert status == "admitted", "the refusal was cosmetic: the patient was discharged"
    assert summaries == 0, "a discharge summary was written for a stranger"


def test_a_doctor_can_still_discharge_on_her_own_ward(world, clinical_client, monkeypatch):
    import services.discharge_service as ds

    async def _no_model(_pid):
        raise RuntimeError("drafting model offline in this test")

    monkeypatch.setattr(ds, "generate_discharge_summary", _no_model)
    resp = clinical_client.post(
        f"/api/vigil/patients/{world['p_a']}/discharge",
        headers=_auth(world["doctor_a"], "doctor"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert body["review_status"] == "draft"
    assert body["drafted_by"] == "unavailable"
