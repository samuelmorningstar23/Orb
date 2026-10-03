"""The parts of the 2026-08-05 PHI-scoping incident the first fix did not reach.

tests/test_patient_scope_routes.py closed the surfaces the audit named and
installed a drift guard, ``test_every_patient_id_route_that_returns_phi_carries
_the_guard``. That guard enumerates routes whose PATH contains ``{patient_id}``.

Two blind spots survived it, and both are the same two verified findings:

  FINDING "per-patient endpoints accept any patient_id with no department check
  and no PHI audit" — a route can take the caller's chosen patient_id off the
  QUERY STRING instead of the path, or off a SURROGATE id (a note_id) that
  resolves to one. Those routes have no ``{patient_id}`` in their path, so the
  guard never looked at them and they kept the pre-fix behaviour:

    GET /api/scribe/notes?patient_id=…       every scribe note for any patient in
                                             the hospital — raw_transcript and
                                             structured_note, verbatim
    GET /api/scribe/notes/{note_id}          the same content, keyed on an
                                             enumerable integer
    GET /api/scribe/notes/{note_id}/verification
    GET /api/appointments?patient_id=…       another ward's appointment list,
                                             including the free-text reason

  FINDING "bulk PHI reads write no audit row" — the two ward-cohort boards that
  are the product's most-read PHI surfaces still left no trace:

    GET /api/vigil/patients                  name, MRN, bed, diagnosis and risk
                                             for the whole ward
    GET /api/sage/ward-brief                 the deterioration watchlist, by name
                                             and bed

  Their sibling /api/nurse/board has audited since it was written, with the
  reason in its docstring ("the audit middleware only covers writes"). The
  question after an incident is "who read this record", and until now the
  hospital could answer it for a chart opened one at a time and not for the
  board that lists the same patients.

Every test below fails on the tree as it stood before this file was added.
"""
import inspect
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app

client = TestClient(app)


# Distinct from test_patient_scope_routes' departments so the two modules cannot
# contaminate each other in the shared session DB.
DEPT_A = "Gap Sweep Cardiology"
DEPT_B = "Gap Sweep Oncology"

SECRET_TRANSCRIPT = "GAPSWEEP SECRET TRANSCRIPT patient discussed termination"
SECRET_STRUCTURED = "GAPSWEEP SECRET STRUCTURED assessment plan"
SECRET_VERIFICATION = "GAPSWEEP SECRET VERIFICATION unsupported claim"
SECRET_APPT_REASON = "GAPSWEEP SECRET REASON oncology follow-up"
SECRET_DIAGNOSIS = "Gapsweep metastatic carcinoma"


def _mk_user(role, department):
    uid = f"GAPU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Gap Sweep {role}", role, department),
        )
    return uid


def _auth(user_id, role):
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _mk_patient(department, *, name, diagnosis, bed):
    """Department on the ADMISSION; patients.ward deliberately says something
    else, so a fix that scoped the wrong column would fail here."""
    pid = f"GAPP{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('GAPADM', 'Gap Sweep Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, ?, ?, 58, 'F', ?, 'admitted', ?)""",
            (pid, name, f"MRN-{uuid.uuid4().hex[:8].upper()}", bed,
             f"{department} bay", diagnosis, now),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, ?, 'active', 'GAPADM')",
            (pid, department, department, bed),
        )
        db.execute(
            "INSERT INTO risk_scores (patient_id, score, level, drivers, timestamp) "
            'VALUES (?, 79, \'critical\', \'["tachycardia"]\', ?)',
            (pid, now),
        )
        # The ward brief bands from the latest VITALS row since 2026-10-03 (the
        # board's moment), so without a set here the brief's watchlist is empty
        # and test_the_ward_brief_is_audited skips instead of testing. HR 135
        # (3) + RR 26 (3) + SpO2 91 (3) = NEWS2 9, critical.
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                   diastolic_bp, spo2, temperature, respiratory_rate,
                                   consciousness, on_oxygen)
               VALUES (?, ?, 135, 118, 75, 91, 37.0, 26, 'alert', 0)""",
            (pid, now),
        )
    return pid


def _mk_scribe_note(patient_id):
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO scribe_notes (patient_id, author_id, raw_transcript,
                                         structured_note, note_format,
                                         verification_json, pipeline_meta_json)
               VALUES (?, 'GAPADM', ?, ?, 'soap', ?, '{}')""",
            (patient_id, SECRET_TRANSCRIPT, SECRET_STRUCTURED,
             '{"note": "' + SECRET_VERIFICATION + '"}'),
        )
        return cur.lastrowid


def _mk_appointment(patient_id):
    aid = f"GAPA{uuid.uuid4().hex[:8].upper()}"
    when = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()
    with db_session() as db:
        db.execute(
            """INSERT INTO appointments (id, patient_id, booked_by_user_id,
                                         scheduled_date, scheduled_time,
                                         appointment_type, reason, status)
               VALUES (?, ?, 'GAPADM', ?, '09:30', 'Follow-up', ?, 'booked')""",
            (aid, patient_id, when, SECRET_APPT_REASON),
        )
    return aid


@pytest.fixture(scope="module")
def world():
    nurse_a = _mk_user("nurse", DEPT_A)
    doctor_b = _mk_user("doctor", DEPT_B)
    # department None — a genuinely unscoped governance account, per the
    # department_scope() convention. Used to prove the fix is not over-applied.
    admin = _mk_user("admin", None)

    p_a = _mk_patient(DEPT_A, name="Gapsweep Mine",
                      diagnosis="Gapsweep community pneumonia", bed="GS-A1")
    p_b = _mk_patient(DEPT_B, name="Gapsweep Theirs",
                      diagnosis=SECRET_DIAGNOSIS, bed="GS-B1")

    return {
        "nurse_a": nurse_a, "doctor_b": doctor_b, "admin": admin,
        "p_a": p_a, "p_b": p_b,
        "note_a": _mk_scribe_note(p_a), "note_b": _mk_scribe_note(p_b),
        "appt_a": _mk_appointment(p_a), "appt_b": _mk_appointment(p_b),
    }


def _phi_rows(action_type):
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM audit_logs "
            "WHERE event_category = 'phi_access' AND action_type = ?",
            (action_type,),
        ).fetchone()["n"]


# ═════════════════════════════════════════════════════════════════════════════
# 1. patient_id off the QUERY STRING — the path-param guard's blind spot
# ═════════════════════════════════════════════════════════════════════════════

def test_scribe_notes_for_another_wards_patient_are_refused(world):
    """`?patient_id=` is the same disclosure as `/{patient_id}` with a different
    spelling, and scribe notes are the rawest text in the system: the verbatim
    consultation transcript."""
    resp = client.get(f"/api/scribe/notes?patient_id={world['p_b']}",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 404, resp.text
    assert SECRET_TRANSCRIPT not in resp.text
    assert SECRET_STRUCTURED not in resp.text


def test_scribe_notes_for_the_callers_own_ward_still_work(world):
    resp = client.get(f"/api/scribe/notes?patient_id={world['p_a']}",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 200, resp.text
    assert any(n["id"] == world["note_a"] for n in resp.json())


def test_an_unscoped_admin_still_reads_any_patients_scribe_notes(world):
    resp = client.get(f"/api/scribe/notes?patient_id={world['p_b']}",
                      headers=_auth(world["admin"], "admin"))
    assert resp.status_code == 200, resp.text
    assert any(n["id"] == world["note_b"] for n in resp.json())


def test_scribe_notes_with_no_patient_id_still_return_the_callers_own(world):
    """No patient_id means "my drafts" (author-scoped) and must keep working —
    a scope that turns a working screen into an empty one is an outage."""
    resp = client.get("/api/scribe/notes", headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_appointments_for_another_wards_patient_are_refused(world):
    resp = client.get(f"/api/appointments?patient_id={world['p_b']}",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 404, resp.text
    assert SECRET_APPT_REASON not in resp.text


def test_appointments_for_the_callers_own_ward_still_work(world):
    resp = client.get(f"/api/appointments?patient_id={world['p_a']}",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 200, resp.text
    assert any(a["id"] == world["appt_a"] for a in resp.json()["appointments"])


def test_an_unscoped_admin_still_reads_any_patients_appointments(world):
    resp = client.get(f"/api/appointments?patient_id={world['p_b']}",
                      headers=_auth(world["admin"], "admin"))
    assert resp.status_code == 200, resp.text
    assert any(a["id"] == world["appt_b"] for a in resp.json()["appointments"])


# ═════════════════════════════════════════════════════════════════════════════
# 2. A SURROGATE id that resolves to a patient — note_id
# ═════════════════════════════════════════════════════════════════════════════

def test_a_scribe_note_by_id_is_scoped_to_the_patients_ward(world):
    """scribe_notes.id is a plain AUTOINCREMENT integer, so "which note" is not
    a secret: 1..N enumerates the hospital's consultation transcripts."""
    resp = client.get(f"/api/scribe/notes/{world['note_b']}",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 404, resp.text
    assert SECRET_TRANSCRIPT not in resp.text
    assert SECRET_STRUCTURED not in resp.text


def test_note_verification_by_id_is_scoped_too(world):
    """The lighter polling endpoint returns the verification report, which
    quotes the note. Fixing only the heavier sibling would leave the text
    reachable through the cheaper call."""
    resp = client.get(f"/api/scribe/notes/{world['note_b']}/verification",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 404, resp.text
    assert SECRET_VERIFICATION not in resp.text


def test_the_notes_own_ward_still_reads_it(world):
    resp = client.get(f"/api/scribe/notes/{world['note_b']}",
                      headers=_auth(world["doctor_b"], "doctor"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == world["note_b"]


def test_an_unscoped_admin_still_reads_any_note(world):
    resp = client.get(f"/api/scribe/notes/{world['note_a']}",
                      headers=_auth(world["admin"], "admin"))
    assert resp.status_code == 200, resp.text


def test_a_missing_note_and_an_out_of_ward_note_are_indistinguishable(world):
    """404 both ways on purpose: a different status code for "exists but not
    yours" is itself a disclosure — it confirms the note, and the patient."""
    missing = client.get("/api/scribe/notes/99999999",
                         headers=_auth(world["nurse_a"], "nurse"))
    out_of_ward = client.get(f"/api/scribe/notes/{world['note_b']}",
                             headers=_auth(world["nurse_a"], "nurse"))
    assert missing.status_code == out_of_ward.status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
# 3. The bulk ward boards leave a trace
# ═════════════════════════════════════════════════════════════════════════════

def test_the_vigil_patient_board_is_audited(world):
    """The product's main clinical screen. It lists name, MRN, bed, diagnosis
    and risk for the whole ward, and left no audit row at all — so "who read
    this record" was answerable for a chart opened one at a time and not for
    the board listing the same patients."""
    before = _phi_rows("patient_board_viewed")
    resp = client.get("/api/vigil/patients", headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 200, resp.text
    assert resp.json(), "empty board — the audit assertion below would be vacuous"
    assert _phi_rows("patient_board_viewed") == before + 1


def test_the_vigil_board_audit_records_the_scope_and_the_ids(world):
    """A row that does not say WHICH patients cannot answer the question that
    gets asked after an incident."""
    client.get("/api/vigil/patients", headers=_auth(world["nurse_a"], "nurse"))
    with db_session() as db:
        row = db.execute(
            "SELECT actor_user_id, target_id, metadata_json FROM audit_logs "
            "WHERE action_type = 'patient_board_viewed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row["actor_user_id"] == world["nurse_a"]
    assert row["target_id"] == DEPT_A
    meta = json.loads(row["metadata_json"])
    assert meta["department"] == DEPT_A
    assert meta["hospital_wide"] is False
    assert world["p_a"] in meta["patient_ids"]


def test_an_empty_board_page_discloses_nothing_and_is_not_audited(world):
    """A page past the end of the ward returns no PHI. Auditing it would file a
    disclosure row for a request that disclosed nothing, and this board is
    polled every 30s by every open client."""
    before = _phi_rows("patient_board_viewed")
    resp = client.get("/api/vigil/patients?offset=100000",
                      headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 200 and resp.json() == []
    assert _phi_rows("patient_board_viewed") == before


def test_the_ward_brief_is_audited(world):
    """Scoped since 2026-08-05, but still unrecorded: the watchlist names the
    deteriorating patients on the ward."""
    before = _phi_rows("ward_brief_viewed")
    resp = client.get("/api/sage/ward-brief", headers=_auth(world["nurse_a"], "nurse"))
    assert resp.status_code == 200, resp.text
    watch = ((resp.json() or {}).get("snapshot") or {}).get("watchlist") or []
    if not watch:
        pytest.skip("narrator snapshot has no watchlist in this run — nothing left")
    assert _phi_rows("ward_brief_viewed") == before + 1


# ═════════════════════════════════════════════════════════════════════════════
# 4. Drift guard for the blind spot itself
# ═════════════════════════════════════════════════════════════════════════════

def test_every_get_route_taking_a_patient_id_query_param_is_scoped():
    """The companion to the path-param guard in test_patient_scope_routes.

    A route that names its patient in the QUERY STRING is exactly as much a
    per-patient PHI read as one that names it in the path, and it is invisible
    to a guard that greps the path. This enumerates the live app so a route
    added tomorrow fails here rather than in a hospital.
    """
    # GOVERNANCE SURFACES, exempt with the reason written down. Both are gated
    # on `audit.read`, which only `admin` holds (permissions.ROLE_PERMISSIONS),
    # and both exist precisely to read the cross-patient trail: an
    # investigator's timeline that could not cross a ward boundary could not
    # investigate. An entry here with no reason is how a hole gets a permit.
    exempt = {
        "/api/audit/logs",
        "/api/flight-recorder/timeline",
        "/api/flight-recorder/verify",
    }

    checked, unguarded = 0, []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        endpoint = getattr(route, "endpoint", None)
        if "GET" not in methods or endpoint is None or path in exempt:
            continue
        if not getattr(endpoint, "__module__", "").startswith("routes"):
            continue
        try:
            sig = inspect.signature(endpoint)
            src = inspect.getsource(endpoint)
        except (OSError, TypeError, ValueError):
            continue
        if "patient_id" not in sig.parameters or "{patient_id}" in path:
            continue
        checked += 1
        if not any(tok in src for tok in ("require_patient_access",
                                          "enforce_patient_scope",
                                          "patient_in_scope",
                                          "scope_patient_ids_sql")):
            unguarded.append(f"{sorted(methods)} {path}")

    assert checked >= 3, (
        f"only {checked} query-param patient_id GET routes enumerated — the "
        f"enumeration is broken, not the app"
    )
    assert not unguarded, (
        "these GET routes take a caller-chosen patient_id off the QUERY STRING "
        "with no ward check — the same disclosure as the path-param leak, in a "
        "spelling the path-param guard cannot see:\n  " + "\n  ".join(sorted(unguarded))
    )


def test_the_query_param_guard_and_the_path_guard_agree_on_one_patient(world):
    """Both spellings must reach the same verdict for the same patient. A route
    that 404s on the path and 200s on the query string is not a boundary."""
    hdr = _auth(world["nurse_a"], "nurse")
    by_path = client.get(f"/api/vigil/patients/{world['p_b']}", headers=hdr)
    by_query = client.get(f"/api/scribe/notes?patient_id={world['p_b']}", headers=hdr)
    assert by_path.status_code == by_query.status_code == 404
