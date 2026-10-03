"""THE registration rule for a patient's name (services/patient_name_rule.py).

Why it exists: on 2026-09-04 a test seed literally named "S" showed that a
one-letter patient record makes Sage deny clinicians their OWN patients
(tests/test_sage_department_scope.py). The matchers now refuse to match a name
with fewer than two letters; this module pins that the same names cannot be
REGISTERED, through every door a record can come in: the Vigil create route,
the admission service and its route, the HIS roster importer and the history
importer. One predicate, four doors, and a parity check against the matcher
floor so the two cannot drift apart.

Every accepted name written here is discharged again by the autouse teardown,
and no rejected name may leave a row behind, which the tests assert directly.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services.patient_name_rule import (
    MIN_NAME_LETTERS, NAME_REQUIRED_MESSAGE, NAME_TOO_SHORT_MESSAGE,
    PatientNameError, name_letter_count, validate_patient_name,
)

client = TestClient(app)

# Real values a ward produces. Short mononyms and initials with a surname are
# names; a bare initial, a placeholder mark or a number is not.
ACCEPTED = ["Om", "Li", "Ed", "S. Kumar", "Meenakshi Varadarajan", "O'Shea",
            "Baby of Priya", "राम", "  Padmini Chidambaram  "]
TOO_SHORT = ["S", "X", "s", "S.", "A-", "12", "-", "K 3", "ॐ"]
BLANK = ["", "   ", None, 42]

_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW (see tests/test_admission_provenance.py): every
    patient this module admits is discharged again, on both tables."""
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


def _count_named(name):
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM patients WHERE name = ?", (name,)).fetchone()[0]


def _by_mrn(mrn):
    with db_session() as db:
        return db.execute("SELECT * FROM patients WHERE mrn = ?", (mrn,)).fetchone()


# ── the predicate ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ACCEPTED)
def test_real_names_are_accepted_and_only_stripped(name):
    assert validate_patient_name(name) == name.strip()
    assert name_letter_count(name) >= MIN_NAME_LETTERS


@pytest.mark.parametrize("name", TOO_SHORT)
def test_fewer_than_two_letters_is_refused_with_the_rules_message(name):
    with pytest.raises(PatientNameError) as e:
        validate_patient_name(name)
    assert str(e.value) == NAME_TOO_SHORT_MESSAGE


@pytest.mark.parametrize("name", BLANK)
def test_blank_or_missing_is_still_name_is_required(name):
    with pytest.raises(PatientNameError) as e:
        validate_patient_name(name)
    assert str(e.value) == NAME_REQUIRED_MESSAGE


def test_registration_floor_equals_the_sage_matcher_floor_for_ascii_names():
    """The point of the rule: no record may be registrable by a name that Sage
    refuses to match. For ASCII names the two counts must agree exactly; for
    other scripts registration is looser on purpose (the matchers are
    ASCII-only, and that is their limitation, not the patient's)."""
    import services.sage_service as sage
    for name in ACCEPTED + TOO_SHORT:
        if not name.isascii():
            continue
        assert (name_letter_count(name) >= MIN_NAME_LETTERS) == (sage._name_letters(name) >= 2), name


# ── door 1: the Vigil create-patient route ────────────────────────────────────

def test_vigil_create_patient_refuses_a_one_letter_name_and_writes_nothing():
    before = _count_named("S")
    r = client.post("/api/vigil/patients", headers=_auth(),
                    json={"name": "S", "ward": "General"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == NAME_TOO_SHORT_MESSAGE
    assert _count_named("S") == before

    r = client.post("/api/vigil/patients", headers=_auth(),
                    json={"name": "   ", "ward": "General"})
    assert r.status_code == 400
    assert r.json()["detail"] == NAME_REQUIRED_MESSAGE


def test_vigil_create_patient_still_accepts_a_real_name():
    name = f"Regrule Vigil {uuid.uuid4().hex[:6]}"
    r = client.post("/api/vigil/patients", headers=_auth(), json={"name": name, "ward": "General"})
    assert r.status_code == 200, r.text
    _SEEDED.append(r.json()["id"])
    assert _count_named(name) == 1


# ── door 2: the admission service and its route ───────────────────────────────

def _admission_payload(name):
    return {"name": name, "age": 40, "sex": "F", "admission_source": "ED",
            "admission_type": "emergency", "department": "General Medicine",
            "ward": "General", "bed": "G-1", "attending_doctor_id": "D001",
            "chief_complaint": "fever"}


def test_admit_route_refuses_a_one_letter_name_with_400_not_500():
    """Before the rule the service would have written the row; and had it
    raised, the route mapped every exception to a 500 "Admission failed"."""
    before_users = _count_users_named("X")
    r = client.post("/api/admissions/admit", headers=_auth(), json=_admission_payload("X"))
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == NAME_TOO_SHORT_MESSAGE
    assert _count_named("X") == 0
    assert _count_users_named("X") == before_users, "a Bridge login was created for a refused name"


def _count_users_named(name):
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM users WHERE name = ?", (name,)).fetchone()[0]


def test_admit_route_still_admits_a_real_name():
    name = f"Regrule Admit {uuid.uuid4().hex[:6]}"
    r = client.post("/api/admissions/admit", headers=_auth(), json=_admission_payload(name))
    assert r.status_code == 200, r.text
    _SEEDED.append(r.json()["patient_id"])
    assert _count_named(name) == 1


def test_admit_service_refuses_before_writing_anything():
    from services.admission_service import admit_patient
    with pytest.raises(PatientNameError):
        admit_patient(_admission_payload("S."), {"id": "D001", "name": "Dr. Test", "role": "doctor"})
    assert _count_named("S.") == 0


# ── door 3: the HIS roster importer ───────────────────────────────────────────

ROSTER_HEADER = "MRN,Name,Age,Sex,Ward,Bed,Diagnosis"
ROSTER_MAPPING = {"mrn": "MRN", "name": "Name", "age": "Age", "sex": "Sex",
                  "ward": "Ward", "bed": "Bed", "diagnosis": "Diagnosis"}


def _csv(header, *rows):
    return ("\n".join([header, *rows]) + "\n").encode()


def test_roster_import_rejects_the_one_letter_row_and_keeps_the_rest():
    from services import roster_import
    bad, good = f"RN{uuid.uuid4().hex[:8].upper()}", f"RN{uuid.uuid4().hex[:8].upper()}"
    data = _csv(ROSTER_HEADER,
                f"{bad},S,62,F,Ward A,A-3,Pneumonia",
                f"{good},Asha Regrule,44,F,Ward A,A-1,Sepsis")
    plan = roster_import.plan(data, ROSTER_MAPPING)
    rows = {r["mrn"]: r for r in plan["rows"]}
    assert rows[bad]["action"] == "skip" and rows[bad]["reason"] == "name_too_short"
    issue = next(i for i in rows[bad]["issues"] if i["code"] == "name_too_short")
    assert issue["severity"] == "error"
    assert rows[good]["action"] == "create"

    result = roster_import.apply_import(data, ROSTER_MAPPING, actor_user_id="D001")
    _SEEDED.extend(result["created_patient_ids"])
    assert len(result["created_patient_ids"]) == 1
    assert _by_mrn(bad) is None
    assert _by_mrn(good)["name"] == "Asha Regrule"


def test_roster_import_still_imports_initials_with_a_warning():
    """Two letters is the floor, so the pinned behaviour for 'S. R.' holds."""
    from services import roster_import
    m = f"RN{uuid.uuid4().hex[:8].upper()}"
    plan = roster_import.plan(_csv(ROSTER_HEADER, f"{m},S. R.,62,F,Ward A,A-3,Pneumonia"), ROSTER_MAPPING)
    row = next(r for r in plan["rows"] if r["mrn"] == m)
    assert row["action"] == "create"
    assert "initials_only_name" in {i["code"] for i in row["issues"]}


# ── door 4: the history importer ──────────────────────────────────────────────

HISTORY_HEADER = "PatientID,MRN,Name,DOB,Sex,Phone"
HISTORY_MAPPING = {"external_id": "PatientID", "mrn": "MRN", "name": "Name",
                   "date_of_birth": "DOB", "sex": "Sex", "phone": "Phone"}


def _codes(result):
    out = set(result.get("issue_summary", {}))
    for row in result.get("rows", []):
        out |= {i["code"] for i in row.get("issues", [])}
    for fail in result.get("failures", []):
        out |= {i["code"] for i in fail.get("issues", [])}
    return out


def test_history_import_rejects_the_one_letter_row_and_keeps_the_rest():
    from services import history_import_service as hi
    tag = uuid.uuid4().hex[:6].upper()
    bad, good = f"HN{tag}A", f"HN{tag}B"
    data = _csv(HISTORY_HEADER,
                f"EXT-{tag}-A,{bad},X,1971-04-02,M,9876500000",
                f"EXT-{tag}-B,{good},Ramesh Regrule,1971-04-02,M,9876500001")
    result = hi.apply_import(data, "patients", HISTORY_MAPPING,
                             source_system=f"Regrule HIS {tag}", actor_user_id="system")
    assert "name_too_short" in _codes(result)
    assert _by_mrn(bad) is None
    created = _by_mrn(good)
    assert created is not None and created["name"] == "Ramesh Regrule"
    # Rule 1 of that importer: historical patients are not in the building.
    assert created["status"] == "discharged"
