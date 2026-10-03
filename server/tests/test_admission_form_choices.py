"""The admission form's department, attending, bed and age (gap register F075).

The form took all four as free text. A typo in Department was written verbatim,
and permissions.department_scope matches that column exactly, so the admitted
patient was on nobody's board. A wrong attending id reached the foreign key and
came back as a raw 500. Two patients ended in one bed with no word said, and an
age of -4 was charted.

Now: the department must be one some doctor or nurse covers (stored in its
canonical spelling), the attending must be an active doctor on the list (400 in
plain words), an age outside 0 to 130 is a 422, and a bed already held by an
admitted patient is ADMITTED WITH A WARNING (block or warn is the founder's
call; warn until then). GET /api/admissions/options is what the form picks from.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services.admission_service import ATTENDING_NOT_LISTED, DEPARTMENT_NOT_COVERED

client = TestClient(app)

_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW (tests/test_admission_provenance.py): every patient
    admitted here is discharged again, on both tables."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _SEEDED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'",
                       [(p,) for p in _SEEDED])
    _SEEDED.clear()


def _auth(user_id="D001", role="doctor"):
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _ward():
    """A ward label no other test uses, so a held bed here is held by us."""
    return f"TW-{uuid.uuid4().hex[:6].upper()}"


def _payload(**over):
    body = {"name": f"Form Choice {uuid.uuid4().hex[:6]}", "age": 52, "sex": "F",
            "admission_source": "er", "admission_type": "inpatient",
            "department": "General Medicine", "ward": _ward(), "bed": "1",
            "attending_doctor_id": "D001", "chief_complaint": "Fever"}
    body.update(over)
    return body


def _admit(**over):
    r = client.post("/api/admissions/admit", headers=_auth(), json=_payload(**over))
    if r.status_code == 200:
        _SEEDED.append(r.json()["patient_id"])
    return r


def _count_named(name):
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM patients WHERE name = ?", (name,)).fetchone()[0]


# ── department ────────────────────────────────────────────────────────────────

def test_a_department_nobody_covers_is_refused_with_400_and_nothing_written():
    body = _payload(department="Cardiolgy")
    r = client.post("/api/admissions/admit", headers=_auth(), json=body)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == DEPARTMENT_NOT_COVERED
    assert _count_named(body["name"]) == 0


def test_a_department_typed_in_another_case_is_stored_in_its_covered_spelling():
    r = _admit(department="  general medicine ")
    assert r.status_code == 200, r.text
    with db_session() as db:
        dept = db.execute("SELECT department FROM admissions WHERE id = ?",
                          (r.json()["admission_id"],)).fetchone()["department"]
    assert dept == "General Medicine"


# ── attending doctor ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("attending", ["NOPE-404", "N001", ""])
def test_an_attending_not_on_the_list_is_a_400_not_a_raw_500(attending):
    # N001 is a real, active account, but a nurse is not an attending doctor.
    body = _payload(attending_doctor_id=attending)
    r = client.post("/api/admissions/admit", headers=_auth(), json=body)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == ATTENDING_NOT_LISTED
    assert _count_named(body["name"]) == 0


# ── age ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("age", [-1, 131])
def test_an_age_outside_0_to_130_is_refused(age):
    body = _payload(age=age)
    r = client.post("/api/admissions/admit", headers=_auth(), json=body)
    assert r.status_code == 422, r.text
    assert _count_named(body["name"]) == 0


@pytest.mark.parametrize("age", [0, 130])
def test_the_ends_of_the_age_range_are_admitted(age):
    assert _admit(age=age).status_code == 200


# ── bed ───────────────────────────────────────────────────────────────────────

def test_a_bed_already_held_is_admitted_with_a_warning():
    ward = _ward()
    first = _admit(ward=ward, bed="GM-101")
    assert first.status_code == 200, first.text
    assert first.json()["bed_warning"] is None

    # Same bed, typed differently: still the same bed.
    second = _admit(ward=f" {ward.lower()} ", bed="gm-101 ")
    assert second.status_code == 200, second.text
    warning = second.json()["bed_warning"]
    assert warning and "already held by another admitted patient" in warning


def test_a_bed_freed_by_discharge_carries_no_warning():
    ward = _ward()
    first = _admit(ward=ward, bed="7")
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?",
                   (first.json()["patient_id"],))
    assert _admit(ward=ward, bed="7").json()["bed_warning"] is None


# ── the options the form picks from ───────────────────────────────────────────

def test_options_list_covered_departments_doctors_and_held_beds_without_patient_data():
    ward = _ward()
    admitted = _admit(ward=ward, bed="3")
    with db_session() as db:
        patient_name = db.execute("SELECT name FROM patients WHERE id = ?",
                                  (admitted.json()["patient_id"],)).fetchone()["name"]

    r = client.get("/api/admissions/options", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert "General Medicine" in body["departments"]
    assert body["default_department"] == "General Medicine"
    doctors = {d["id"] for d in body["doctors"]}
    assert "D001" in doctors and "N001" not in doctors
    assert {"ward": ward, "bed": "3", "in_use": True} in body["beds"]
    assert any(w["ward"] == ward and "General Medicine" in w["departments"] for w in body["wards"])
    assert patient_name not in r.text and admitted.json()["patient_id"] not in r.text


def test_options_need_the_admitting_permission():
    assert client.get("/api/admissions/options").status_code == 401


# ── automation accounts are never an attending doctor ─────────────────────────

_FUTURE_BOT = "bot_test_future"


@pytest.fixture
def _bots_seeded_as_doctors():
    """bot_vigil exactly as bot_service seeds it (role 'doctor', no department),
    plus a bot_ id not yet in SERVICE_ACCOUNT_IDS. Both are active doctors by
    role, so only the service-account rule keeps them off the list."""
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('bot_vigil', 'Orb Vigil', 'doctor', 'system')")
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES (?, 'Orb Future Bot', 'doctor', 'system')", (_FUTURE_BOT,))
        row = db.execute("SELECT role, COALESCE(is_active, 1) AS active FROM users "
                         "WHERE id = 'bot_vigil'").fetchone()
    assert row["role"] == "doctor" and row["active"] == 1
    yield
    with db_session() as db:
        db.execute("DELETE FROM users WHERE id = ?", (_FUTURE_BOT,))


def test_options_never_offer_an_automation_account_as_attending(_bots_seeded_as_doctors):
    from database import SERVICE_ACCOUNT_IDS

    r = client.get("/api/admissions/options", headers=_auth())
    assert r.status_code == 200, r.text
    doctors = {d["id"] for d in r.json()["doctors"]}
    assert "D001" in doctors
    assert not doctors & SERVICE_ACCOUNT_IDS
    assert _FUTURE_BOT not in doctors
    assert "Orb Vigil" not in {d["name"] for d in r.json()["doctors"]}


@pytest.mark.parametrize("bot", ["bot_vigil", _FUTURE_BOT])
def test_admitting_with_a_bot_as_attending_is_a_400(_bots_seeded_as_doctors, bot):
    body = _payload(attending_doctor_id=bot)
    r = client.post("/api/admissions/admit", headers=_auth(), json=body)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == ATTENDING_NOT_LISTED
    assert _count_named(body["name"]) == 0


def test_scribe_quick_admission_refuses_a_bot_as_attending_too(_bots_seeded_as_doctors):
    # The Scribe quick admission picks from the same attending_doctors() list.
    opts = client.get("/api/scribe/admission-options", headers=_auth())
    assert opts.status_code == 200, opts.text
    assert "bot_vigil" not in {d["id"] for d in opts.json()["doctors"]}

    body = _payload(attending_doctor_id="bot_vigil")
    r = client.post("/api/scribe/quick-admit", headers=_auth(), json=body)
    assert r.status_code == 422, r.text
    assert _count_named(body["name"]) == 0
