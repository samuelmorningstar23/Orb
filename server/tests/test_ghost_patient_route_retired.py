"""The ghost POST /api/vigil/patients is retired (opd-1 part g; adt-5's legacy item).

WHAT IT DID. It wrote a patients row with status 'admitted' and NO admissions
row: no department, so the patient sat on nobody's board, while status-only
readers (the vitals simulator, the sepsis sweep) treated them as an inpatient.
It also minted a Bridge login, a case room and an admission message. Its only
callers were a dead client wrapper, server/scripts/soak_test.py and the name
rule test, all moved to the admission door, POST /api/admissions/admit.

NOW. It answers 410 Gone with words naming the admission door, reads no body
and writes nothing. Not behind a flag: the route was never part of any
workflow, so retiring it changes no screen. The permission check in front of
it is unchanged, so an unauthenticated caller still gets 401 and a nurse
(no patient.write) still gets 403.
"""
import importlib.util
import inspect
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
import routes.vigil_routes as vigil_routes

client = TestClient(app)


def _auth(user_id="D001", role="doctor"):
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _counts(name: str) -> dict:
    with db_session() as db:
        return {
            "patients": db.execute("SELECT COUNT(*) FROM patients").fetchone()[0],
            "named": db.execute("SELECT COUNT(*) FROM patients WHERE name = ?", (name,)).fetchone()[0],
            "logins": db.execute("SELECT COUNT(*) FROM users WHERE name = ?", (name,)).fetchone()[0],
            "case_rooms": db.execute(
                "SELECT COUNT(*) FROM channels WHERE type = 'case_room' AND name = ?",
                (f"Case: {name}",)).fetchone()[0],
            "admissions": db.execute("SELECT COUNT(*) FROM admissions").fetchone()[0],
        }


def test_a_doctor_gets_410_naming_the_admission_door_and_nothing_is_written():
    name = f"Ghost Route {uuid.uuid4().hex[:6]}"
    before = _counts(name)
    r = client.post("/api/vigil/patients", headers=_auth(), json={
        "name": name, "ward": "General", "bed": "G-9", "age": 50, "sex": "M",
        "diagnosis": "Pneumonia",
    })
    assert r.status_code == 410, r.text
    detail = r.json()["detail"]
    assert "/api/admissions/admit" in detail
    assert detail == vigil_routes.GHOST_PATIENT_ROUTE_RETIRED
    assert _counts(name) == before, "the retired route still wrote a row"


def test_the_retired_route_reads_no_body():
    """No body, or a body that is not an object, is still 410, never a 422 or
    a 400 that would suggest a better payload might work."""
    assert client.post("/api/vigil/patients", headers=_auth()).status_code == 410
    r = client.post("/api/vigil/patients", headers=_auth(), json=["not", "an", "object"])
    assert r.status_code == 410


def test_who_is_refused_is_unchanged():
    assert client.post("/api/vigil/patients", json={"name": "Nobody Here"}).status_code == 401
    r = client.post("/api/vigil/patients", headers=_auth("N001", "nurse"),
                    json={"name": "Nobody Here"})
    assert r.status_code == 403, r.text


def test_the_handler_holds_no_write_at_all():
    """Belt and braces: the retired handler's source has no SQL write and no
    database session, so a future edit cannot quietly bring the ghost back."""
    src = inspect.getsource(vigil_routes.create_patient)
    for forbidden in ("INSERT", "UPDATE", "db_session", "get_db", "hash_password"):
        assert forbidden not in src, forbidden


def test_the_ward_board_route_is_untouched():
    r = client.get("/api/vigil/patients", headers=_auth())
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def _soak_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "soak_test.py"
    spec = importlib.util.spec_from_file_location("orb_soak_test_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_soak_harness_admits_through_the_admission_door():
    """soak_test.py used the ghost route to top up its census. Its synthetic
    admission, built from the admission form's own choices, is accepted by
    POST /api/admissions/admit and lands on the admitting doctor's board."""
    soak = _soak_module()
    assert "/api/vigil/patients\", json=" not in Path(soak.__file__).read_text()
    options = client.get("/api/admissions/options", headers=_auth())
    assert options.status_code == 200, options.text
    department, attending = soak.soak_admission_choice(options.json())
    assert department and attending

    idx = 900 + int(uuid.uuid4().hex[:2], 16)
    r = client.post("/api/admissions/admit", headers=_auth(),
                    json=soak.soak_admission_payload(idx, department, attending))
    assert r.status_code == 200, r.text
    pid = r.json()["patient_id"]
    try:
        board = client.get("/api/vigil/patients", headers=_auth(), params={"limit": 500})
        assert board.status_code == 200
        assert pid in {p["id"] for p in board.json()}
        with db_session() as db:
            adm = db.execute("SELECT department FROM admissions WHERE patient_id = ? "
                             "AND status = 'active'", (pid,)).fetchone()
        assert adm is not None and adm["department"] == department
    finally:
        with db_session() as db:
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
            db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))


def test_the_soak_choice_is_empty_when_the_form_offers_nothing():
    soak = _soak_module()
    assert soak.soak_admission_choice({}) == (None, None)
    assert soak.soak_admission_choice({"departments": ["Medicine"], "doctors": [
        {"id": "DX1", "department": "Surgery"}, {"id": "DX2", "department": "Medicine"},
    ]}) == ("Medicine", "DX2")
