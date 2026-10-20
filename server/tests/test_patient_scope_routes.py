"""Ward scoping and read auditing for every PHI-returning REST surface.

THE INCIDENT (security audit, 2026-08-05). `permissions.department_scope` was
imported by exactly four route/service clusters, so every other PHI-returning
surface derived no ward scope at all. Verified by execution against an isolated
DB, a nurse whose users row said department='Cardiology' received:

  GET /api/search/patients?q=..        the whole admitted census (id, name, MRN,
                                       bed, ward, diagnosis) — the DISCOVERY half
  GET /api/vigil/patients/{id}         the full chart of the id she just found,
                                       including DOB, phone and address
  GET /api/vigil/patients/{id}/notes   free-text notes from another ward
  GET /api/vigil/discharged            every patient the hospital EVER discharged,
                                       with `p.*` — DOB, phone, address, next of kin
  GET /api/forecast/discharge-board    the whole admitted census, name + diagnosis
  GET /api/sage/world/patient/{id}     diagnosis + risk trajectory — and to a
                                       PHARMACIST, who is 403'd by Vigil
  GET /api/lens/analyses               the 50 most recent imaging analyses, house-wide
  GET /api/ipass/{id}/draft            the handoff narrative with MRN

…and across 23 PHI-returning GETs, THREE audit rows were written in total. The
audit middleware fires on POST/PUT/PATCH/DELETE only, so the leaks were not
merely exploitable, they were unprovable afterwards.

This is the same defect class as the two leaks fixed the same morning (Sage's
ward-brief, Relay's channel list). The pattern that produced all of them: scope
was applied to surfaces that look like they SELECT something, and not to the
ones that merely LIST or FETCH-BY-ID.

Every test below fails on the pre-fix tree. They assert three things:
  1. a departmented caller never receives a patient outside their department;
  2. an UNSCOPED account (department None — a governance account, by the
     department_scope convention) still reads hospital-wide, so the fix is not
     over-applied;
  3. a PHI read leaves a row on the hash chain.
"""
from tests.source_contract import contract_source
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app

client = TestClient(app)


# Distinctive department names so this module cannot collide with any other
# test module's fixtures in the shared session DB.
DEPT_A = "Scope Route Cardiology"
DEPT_B = "Scope Route Oncology"

# Deliberately unusual strings: an assertion that merely greps the response body
# must not be satisfiable by some other module's fixture patient.
SECRET_NOTE = "SCOPEROUTE SECRET NOTE palliative discussion"
SECRET_DIAGNOSIS = "Scoperoute metastatic carcinoma"
SECRET_PHONE = "9995550042"
SECRET_ADDRESS = "42 Scoperoute Lane"
SECRET_DOB = "1961-04-02"
SECRET_IMAGING = "SCOPEROUTE SECRET IMAGING hepatic mets"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, department):
    uid = f"SCRT{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Scope Route {role}", role, department),
        )
    return uid


def _auth(user_id, role):
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


#: Every patient this module seeds, so teardown can put them back.
_CREATED: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW: this module seeds `status='admitted'` patients into a
    session DB shared by every other file.

    Left admitted they are not a cosmetic leak. `bot_service._generate_vitals`
    appends a reading to EVERY admitted patient on a simulation tick, so a
    leftover with one seeded vitals row grows a trend it was never meant to
    have and starts counting as `worsening` hospital-wide — carrying the
    `critical` risk score seeded below with it. That is how this file's
    patients turned up in another module's ward aggregate.

    Discharge rather than DELETE: rows written during the test legitimately
    reference these ids. Module-scoped, so teardown runs after this file's last
    test and its own assertions still see admitted patients.
    """
    yield
    if not _CREATED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?",
            [(p,) for p in _CREATED],
        )
    _CREATED.clear()


def _mk_patient(department, *, name, diagnosis, bed, status="admitted",
                secrets=False):
    """A patient whose department lives on the ADMISSION — never on patients.ward.

    Scoping the wrong column is how a scope silently stops scoping, so the
    fixture deliberately puts a DIFFERENT string in patients.ward than in
    admissions.department: a fix that scoped on `ward` would pass a naive test
    and fail this one.
    """
    pid = f"SCRTP{uuid.uuid4().hex[:8].upper()}"
    _CREATED.append(pid)
    now = datetime.now(timezone.utc).isoformat()
    admission_status = "active" if status == "admitted" else "discharged"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('SCRTADM', 'Scope Route Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date, date_of_birth, phone,
                                     address, emergency_contact_name,
                                     emergency_contact_phone)
               VALUES (?, ?, ?, ?, ?, 61, 'F', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pid, name, f"MRN-{uuid.uuid4().hex[:8].upper()}", bed,
             # NOT the department: a free-text bed label.
             f"{department} bay",
             diagnosis, status, now,
             SECRET_DOB if secrets else None,
             SECRET_PHONE if secrets else None,
             SECRET_ADDRESS if secrets else None,
             "Scoperoute Next Of Kin" if secrets else None,
             SECRET_PHONE if secrets else None),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, ?, ?, 'SCRTADM')",
            (pid, department, department, bed, admission_status),
        )
        db.execute(
            "INSERT INTO vitals (patient_id, heart_rate, systolic_bp, diastolic_bp, "
            "spo2, temperature, respiratory_rate, timestamp) "
            "VALUES (?, 118, 92, 58, 91, 38.6, 26, ?)",
            (pid, now),
        )
        db.execute(
            "INSERT INTO risk_scores (patient_id, score, level, drivers, timestamp) "
            # drivers is a JSON column: the chart route json.loads() it.
            'VALUES (?, 81, \'critical\', \'["tachycardia", "hypotension"]\', ?)',
            (pid, now),
        )
        if secrets:
            db.execute(
                "INSERT INTO clinical_documents (patient_id, doc_type, title, content, author_id) "
                "VALUES (?, 'Progress Note', 'Scope Route', ?, 'SCRTADM')",
                (pid, SECRET_NOTE),
            )
            db.execute(
                "INSERT INTO lens_analyses (patient_id, author_id, image_path, "
                "image_type, analysis) VALUES (?, 'SCRTADM', '/tmp/x.png', 'ct', ?)",
                (pid, SECRET_IMAGING),
            )
    return pid


@pytest.fixture(scope="module")
def world():
    """One nurse on ward A, one pharmacist, one unscoped admin, and patients on
    both wards — including a DISCHARGED one on B, which is the only way to
    exercise /api/vigil/discharged."""
    nurse_a = _mk_user("nurse", DEPT_A)
    doctor_a = _mk_user("doctor", DEPT_A)
    doctor_b = _mk_user("doctor", DEPT_B)
    pharmacist = _mk_user("pharmacist", "Scope Route Pharmacy")
    # department None is the documented "genuinely unscoped governance account"
    # convention — NOT "scope unknown".
    admin = _mk_user("admin", None)

    p_a = _mk_patient(DEPT_A, name="Scoperoute Ayyanar Mine",
                      diagnosis="Scoperoute community pneumonia", bed="SR-A1")
    p_b = _mk_patient(DEPT_B, name="Scoperoute Bhattacharya Theirs",
                      diagnosis=SECRET_DIAGNOSIS, bed="SR-B1", secrets=True)
    p_b_discharged = _mk_patient(DEPT_B, name="Scoperoute Discharged Theirs",
                                 diagnosis=SECRET_DIAGNOSIS, bed="SR-B2",
                                 status="discharged", secrets=True)
    return {
        "nurse_a": nurse_a, "doctor_a": doctor_a, "doctor_b": doctor_b,
        "pharmacist": pharmacist, "admin": admin,
        "p_a": p_a, "p_b": p_b, "p_b_discharged": p_b_discharged,
    }


def _audit_count(action_type=None, patient_id=None):
    sql = "SELECT COUNT(*) AS n FROM audit_logs WHERE event_category = 'phi_access'"
    params = []
    if action_type:
        sql += " AND action_type = ?"
        params.append(action_type)
    if patient_id:
        sql += " AND patient_id = ?"
        params.append(patient_id)
    with db_session() as db:
        return db.execute(sql, tuple(params)).fetchone()["n"]


# ═════════════════════════════════════════════════════════════════════════════
# 1. The DISCOVERY half — /api/search/patients
# ═════════════════════════════════════════════════════════════════════════════

def test_search_does_not_return_another_wards_patients(world):
    """This route's docstring claimed parity with the Vigil patient list while
    omitting the one line that makes that list safe."""
    hdr = _auth(world["nurse_a"], "nurse")
    resp = client.get("/api/search/patients?q=Scoperoute", headers=hdr)
    assert resp.status_code == 200, resp.text
    ids = {r["id"] for r in resp.json()}
    assert world["p_b"] not in ids, (
        "search returned a patient from another department — the enumeration "
        "vector that fed every other leak"
    )


def test_search_still_returns_the_callers_own_ward(world):
    """A scope that returns nothing is not a scope, it is an outage."""
    hdr = _auth(world["nurse_a"], "nurse")
    resp = client.get("/api/search/patients?q=Scoperoute", headers=hdr)
    assert world["p_a"] in {r["id"] for r in resp.json()}


def test_search_scopes_on_the_admission_not_on_patients_ward(world):
    """patients.ward is a free-text bed label; the department lives on the
    admission. The fixture puts different strings in each on purpose."""
    hdr = _auth(world["nurse_a"], "nurse")
    rows = client.get("/api/search/patients?q=Scoperoute", headers=hdr).json()
    assert rows, "no rows at all — cannot distinguish a working scope from an outage"
    for r in rows:
        assert r["ward"] == f"{DEPT_A} bay"


def test_an_unscoped_admin_still_searches_hospital_wide(world):
    resp = client.get("/api/search/patients?q=Scoperoute",
                      headers=_auth(world["admin"], "admin"))
    ids = {r["id"] for r in resp.json()}
    assert world["p_a"] in ids and world["p_b"] in ids, (
        "scoping was over-applied: an account with no department on record is a "
        "governance account, not 'scope unknown'"
    )


def test_a_search_that_discloses_phi_is_audited(world):
    before = _audit_count("patient_search")
    client.get("/api/search/patients?q=Scoperoute", headers=_auth(world["nurse_a"], "nurse"))
    assert _audit_count("patient_search") == before + 1


# ═════════════════════════════════════════════════════════════════════════════
# 2. The chart itself — /api/vigil/patients/{id} and friends
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("suffix", ["", "/notes", "/story", "/vitals",
                                    "/timeline-projection"])
def test_per_patient_vigil_routes_404_outside_the_ward(world, suffix):
    resp = client.get(f"/api/vigil/patients/{world['p_b']}{suffix}",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 404, (
        f"/api/vigil/patients/{{id}}{suffix} returned {resp.status_code} for a "
        f"patient on another ward: {resp.text[:400]}"
    )


def test_the_out_of_ward_chart_body_never_reaches_the_wire(world):
    """Belt and braces on the status code: assert the actual PHI is absent."""
    body = client.get(f"/api/vigil/patients/{world['p_b']}",
                      headers=_auth(world["nurse_a"], "nurse")).text
    for secret in (SECRET_PHONE, SECRET_ADDRESS, SECRET_DOB, SECRET_DIAGNOSIS):
        assert secret not in body


def test_out_of_ward_notes_do_not_reach_the_wire(world):
    body = client.get(f"/api/vigil/patients/{world['p_b']}/notes",
                      headers=_auth(world["nurse_a"], "nurse")).text
    assert SECRET_NOTE not in body


def test_the_ward_owner_still_reads_their_own_patient(world):
    resp = client.get(f"/api/vigil/patients/{world['p_b']}",
                      headers=_auth(world["doctor_b"], "doctor"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["patient"]["id"] == world["p_b"]


def test_an_unscoped_admin_still_reads_any_chart(world):
    resp = client.get(f"/api/vigil/patients/{world['p_b']}",
                      headers=_auth(world["admin"], "admin"))
    assert resp.status_code == 200, resp.text


def test_opening_a_chart_writes_a_phi_access_row(world):
    before = _audit_count("patient_chart_viewed", world["p_b"])
    client.get(f"/api/vigil/patients/{world['p_b']}",
               headers=_auth(world["doctor_b"], "doctor"))
    assert _audit_count("patient_chart_viewed", world["p_b"]) == before + 1, (
        "a chart read left no trace — the hospital cannot answer 'who read this "
        "record', which is the question that gets asked after an incident"
    )


def test_a_cross_ward_attempt_is_recorded_as_denied(world):
    """The refusal is the interesting event: a probe must be visible in the trail."""
    def denials():
        with db_session() as db:
            return db.execute(
                "SELECT COUNT(*) AS n FROM audit_logs "
                "WHERE action_type = 'patient_scope_denied' AND patient_id = ?",
                (world["p_b"],),
            ).fetchone()["n"]

    before = denials()
    client.get(f"/api/vigil/patients/{world['p_b']}",
               headers=_auth(world["nurse_a"], "nurse"))
    assert denials() == before + 1


def test_a_cross_ward_note_cannot_be_written_either(world):
    """Charting on another ward's patient is worse than reading them.

    Uses a DOCTOR: `patient.write` is a doctor permission, so a nurse would be
    stopped by the role gate and the ward check would never be reached — that
    test would pass on the broken tree and prove nothing."""
    resp = client.post(
        f"/api/vigil/patients/{world['p_b']}/notes",
        json={"content": "scoperoute cross-ward write attempt"},
        headers=_auth(world["doctor_a"], "doctor"),
    )
    assert resp.status_code == 404, resp.text
    with db_session() as db:
        n = db.execute(
            "SELECT COUNT(*) AS n FROM clinical_documents "
            "WHERE patient_id = ? AND content LIKE 'scoperoute cross-ward%'",
            (world["p_b"],),
        ).fetchone()["n"]
    assert n == 0


# ═════════════════════════════════════════════════════════════════════════════
# 3. /api/vigil/discharged — the highest-value row in the system
# ═════════════════════════════════════════════════════════════════════════════

def test_discharged_list_is_department_scoped(world):
    resp = client.get("/api/vigil/discharged", headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 200, resp.text
    assert world["p_b_discharged"] not in {r["id"] for r in resp.json()}


def test_discharged_list_never_carries_next_of_kin_or_home_address(world):
    """Projection, not just scoping: `p.*` put DOB, phone, address and next of
    kin on the wire. No discharged LIST view needs them, so they are off the
    wire for EVERY caller — including the unscoped admin."""
    resp = client.get("/api/vigil/discharged", headers=_auth(world["admin"], "admin"))
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert rows, "no discharged patients at all — the assertion below is vacuous"
    banned = {"date_of_birth", "phone", "address",
              "emergency_contact_name", "emergency_contact_phone"}
    leaked = banned.intersection(*(set(r) for r in rows)) if rows else set()
    assert not leaked, f"discharged list still returns {sorted(leaked)}"
    body = resp.text
    for secret in (SECRET_PHONE, SECRET_ADDRESS, SECRET_DOB):
        assert secret not in body


def test_discharged_list_is_paginated(world):
    resp = client.get("/api/vigil/discharged?limit=1", headers=_auth(world["admin"], "admin"))
    assert len(resp.json()) <= 1
    assert "X-Total-Count" in resp.headers, (
        "unbounded: the whole historical set could leave in one response"
    )


def test_reading_the_discharged_list_is_audited(world):
    before = _audit_count("discharged_list_viewed")
    client.get("/api/vigil/discharged", headers=_auth(world["admin"], "admin"))
    assert _audit_count("discharged_list_viewed") == before + 1


# ═════════════════════════════════════════════════════════════════════════════
# 4. /api/sage/world/patient/{id} — the sibling of the ward-brief fix
# ═════════════════════════════════════════════════════════════════════════════

def test_sage_world_patient_is_department_scoped(world):
    resp = client.get(f"/api/sage/world/patient/{world['p_b']}",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 404, resp.text
    assert SECRET_DIAGNOSIS not in resp.text


def test_a_pharmacist_cannot_reach_the_patient_world(world):
    """`sage.query` is granted to pharmacist, who deliberately has NO
    patient.read_all and is 403'd by /api/vigil/patients/{id}. Ward scoping
    alone does not close this: a pharmacy account with a blank department scopes
    to None and would sail through. The permission gate is what closes it."""
    hdr = _auth(world["pharmacist"], "pharmacist")
    resp = client.get(f"/api/sage/world/patient/{world['p_b']}", headers=hdr)
    assert resp.status_code == 403, resp.text
    assert SECRET_DIAGNOSIS not in resp.text
    # …and the escalation is real: the same token is already refused by Vigil.
    assert client.get(f"/api/vigil/patients/{world['p_b']}",
                      headers=hdr).status_code == 403


def test_the_ward_owner_still_reads_the_patient_world(world):
    resp = client.get(f"/api/sage/world/patient/{world['p_b']}",
                      headers=_auth(world["doctor_b"], "doctor"))
    assert resp.status_code == 200, resp.text


# ═════════════════════════════════════════════════════════════════════════════
# 5. Dashboards — forecast discharge board and the command center
# ═════════════════════════════════════════════════════════════════════════════

def test_discharge_board_is_department_scoped(world):
    resp = client.get("/api/forecast/discharge-board",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 200, resp.text
    ids = {r.get("patient_id") for r in resp.json()}
    assert world["p_b"] not in ids, (
        "the discharge-readiness board is a dashboard, not a search — which is "
        "exactly why nobody checked it"
    )
    assert SECRET_DIAGNOSIS not in resp.text


def test_discharge_board_still_shows_the_callers_own_ward(world):
    resp = client.get("/api/forecast/discharge-board",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert world["p_a"] in {r.get("patient_id") for r in resp.json()}


def test_an_unscoped_admin_still_sees_the_whole_discharge_board(world):
    resp = client.get("/api/forecast/discharge-board",
                      headers=_auth(world["admin"], "admin"))
    ids = {r.get("patient_id") for r in resp.json()}
    assert world["p_a"] in ids and world["p_b"] in ids


def test_command_center_deterioration_list_is_department_scoped(world):
    """`forecast.read` is held by nurses, so the house-wide named deterioration
    list was a PHI disclosure on an executive dashboard."""
    resp = client.get("/api/command-center", headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 200, resp.text
    ids = {r.get("patient_id") for r in resp.json().get("deteriorations") or []}
    assert world["p_b"] not in ids
    assert SECRET_DIAGNOSIS not in resp.text


# ═════════════════════════════════════════════════════════════════════════════
# 6. The long tail: lens, ipass, emar
# ═════════════════════════════════════════════════════════════════════════════

def test_lens_analyses_without_a_patient_id_are_department_scoped(world):
    resp = client.get("/api/lens/analyses", headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 200, resp.text
    assert SECRET_IMAGING not in resp.text
    assert world["p_b"] not in {r.get("patient_id") for r in resp.json()}


def test_lens_analyses_for_a_named_out_of_ward_patient_404(world):
    resp = client.get(f"/api/lens/analyses?patient_id={world['p_b']}",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 404, resp.text
    assert SECRET_IMAGING not in resp.text


def test_an_unscoped_admin_still_lists_lens_analyses(world):
    resp = client.get("/api/lens/analyses", headers=_auth(world["admin"], "admin"))
    assert resp.status_code == 200, resp.text
    assert SECRET_IMAGING in resp.text


def test_ipass_draft_is_department_scoped(world):
    resp = client.get(f"/api/ipass/{world['p_b']}/draft",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 404, resp.text
    assert "Scoperoute Bhattacharya" not in resp.text


def test_emar_administrations_are_department_scoped(world):
    resp = client.get(f"/api/emar/patients/{world['p_b']}/administrations",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 404, resp.text


def test_fhir_export_of_an_out_of_ward_patient_is_refused(world):
    """The one surface where PHI leaves the building entirely."""
    resp = client.get(f"/api/fhir/Patient/{world['p_b']}",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 404, resp.text
    resp = client.get(f"/api/fhir/patient/{world['p_b']}/$everything",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 404, resp.text
    assert SECRET_DIAGNOSIS not in resp.text


# ═════════════════════════════════════════════════════════════════════════════
# 7. Drift guards
# ═════════════════════════════════════════════════════════════════════════════

def test_the_route_scope_and_the_sage_scope_agree(world):
    """permissions.patient_in_scope and services.sage_service._patient_in_scope
    implement the SAME derivation in two modules. If one is changed without the
    other, a clinician gets 404 on one surface and 200 on another for the same
    patient — which reads as data loss, not as a boundary. This test is the
    tripwire; it is cheaper than merging them across an ownership line."""
    from permissions import patient_in_scope
    from services.sage_service import _patient_in_scope

    cases = [
        (world["p_a"], DEPT_A), (world["p_a"], DEPT_B),
        (world["p_b"], DEPT_A), (world["p_b"], DEPT_B),
        (world["p_b_discharged"], DEPT_A), (world["p_b_discharged"], DEPT_B),
        (world["p_a"], None), ("NO-SUCH-PATIENT", DEPT_A),
    ]
    with db_session() as db:
        for pid, dept in cases:
            assert patient_in_scope(db, pid, dept) == _patient_in_scope(db, pid, dept), (
                f"scope derivations disagree for patient={pid} dept={dept}"
            )


def test_scope_is_never_a_request_parameter(world):
    """department_scope's contract: a scope the caller can name is not a scope.
    /api/nurse/board once took `?department=` and `?all_departments=true`;
    unknown query params are ignored by FastAPI, so an attempt must be inert."""
    hdr = _auth(world["nurse_a"], "nurse")
    for qs in (f"&department={DEPT_B}", "&all_departments=true", "&scope=all"):
        rows = client.get(f"/api/search/patients?q=Scoperoute{qs}", headers=hdr).json()
        assert world["p_b"] not in {r["id"] for r in rows}, (
            f"query string {qs!r} widened the scope"
        )


def _source_really_belongs_to(endpoint, src: str) -> bool:
    """Did inspect.getsource() actually hand back THIS function's body?

    inspect.getsource() reads the file at CALL time but trusts the code object's
    co_firstlineno, which was fixed when the module was IMPORTED. If the file was
    edited in between, findsource() walks backwards from a stale line number and
    silently returns whatever `def` it lands on first — a different function's
    body, with no exception raised.

    That is not hypothetical. On 2026-08-15 routes/emar_routes.py was saved while
    a full-suite run was in flight; get_patient_administrations moved from line
    116 to line 197, and getsource() returned administer_medication instead. Its
    body contains no ward check, so this test reported a PHI route as unguarded
    — the single most alarming failure this suite can produce, from an entirely
    healthy app. Verify identity rather than trusting the read.
    """
    name = getattr(endpoint, "__name__", None)
    if not name:
        return True
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("def ", "async def ")):
            return stripped.split("def ", 1)[1].split("(")[0].strip() == name
    return True


def test_every_patient_id_route_that_returns_phi_carries_the_guard():
    """A route added tomorrow must be either wearing the guard or visibly bare
    in review. This enumerates the live app rather than a hand-kept list, so a
    new unguarded /{patient_id} GET fails here instead of in a hospital.
    """
    import inspect
    from main import app as live_app

    # Routes whose scoping is deliberately elsewhere. Currently EMPTY — every
    # /{patient_id} GET in the app wears the guard. Add here only with the
    # reason written down; an entry with no reason is how a hole gets a permit.
    exempt: set[str] = set()

    checked, unguarded, drifted = 0, [], []
    for route in live_app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "{patient_id}" not in path or "GET" not in methods or path in exempt:
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        try:
            src = contract_source(endpoint)
        except (OSError, TypeError):
            continue
        if not _source_really_belongs_to(endpoint, src):
            code = getattr(endpoint, "__code__", None)
            drifted.append(
                f"{path} -> {getattr(endpoint, '__module__', '?')}."
                f"{getattr(endpoint, '__qualname__', '?')} "
                f"(code says {getattr(code, 'co_filename', '?')}:"
                f"{getattr(code, 'co_firstlineno', '?')})"
            )
            continue
        checked += 1
        if "require_patient_access" not in src and "_authorize_patient_access" not in src:
            unguarded.append(f"{sorted(methods)} {path}")

    # Checked FIRST, and separately from `unguarded`, because a stale line number
    # makes a guarded route look bare. Reporting that as a PHI leak sends someone
    # hunting a hole that does not exist; reporting it as a moving checkout sends
    # them to the actual problem.
    assert not drifted, (
        "inspect.getsource() returned a DIFFERENT function's body than the one "
        "registered on these routes, so their guard status could not be read. The "
        "source file was edited after the module was imported — the code objects' "
        "line numbers are stale. This is a moving working tree, not an unguarded "
        "route: nothing here says anything about whether the app is safe. Re-run "
        "against a quiescent checkout.\n  " + "\n  ".join(sorted(drifted))
    )
    # Non-vacuity: if the enumeration ever stops finding routes (a Starlette
    # change, a refactor to sub-apps), this test would silently pass forever.
    assert checked >= 20, (
        f"only {checked} /{{patient_id}} GET routes enumerated — the audit found "
        f"25. The enumeration is broken, not the app."
    )
    assert not unguarded, (
        "these /{patient_id} GET routes take an id off the path with no ward "
        "check — the exact shape of the 2026-08-05 leak:\n  "
        + "\n  ".join(sorted(unguarded))
    )
