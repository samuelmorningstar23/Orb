"""Emergency surgical packages must not claim anything nobody assessed (F017).

Before 2026-10-02 both package writers stored a fixed "ASA IV, septic shock"
risk text, a success estimate (72.5 / 78.5, or a default 80 when a package left
it out), a hard-coded laparotomy title and a roster of invented clinicians on
every real emergency case, with no demo gate. The streamed risk assessment's
offline fallback printed "ASA Class III", and closing a case under DEMO_MODE
filed an invented operative account ("EBL 150mL, no complications").

Every test here runs with DEMO_MODE explicitly OFF (the appliance setting) except
the completion test, which turns it ON to prove the flag no longer fabricates.
"""
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services.who_checklist import TEMPLATE_VERSION, WHO_CHECKLIST

client = TestClient(app)

FORBIDDEN = (r"\basa\b", r"septic", r"sepsis status", r"class iv", r"class iii")


def _mk_user(role: str, prefix: str) -> str:
    uid = f"{prefix}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, 'x')",
            (uid, f"{role.title()} {prefix}", role),
        )
    return uid


@pytest.fixture()
def doctor():
    return _mk_user("doctor", "HONDOC")


@pytest.fixture()
def patient():
    pid = f"PATHON{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis) "
            "VALUES (?, ?, ?, 'Ward A', 'A-9', 'Acute abdomen')",
            (pid, "Honesty Patient", f"MRN-{uuid.uuid4().hex[:8]}"),
        )
    yield pid
    with db_session() as db:
        db.execute(
            "DELETE FROM surgery_team_assignments WHERE surgery_id IN "
            "(SELECT id FROM surgeries WHERE patient_id = ?)", (pid,),
        )
        db.execute("DELETE FROM surgery_prep_requests WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM surgeries WHERE patient_id = ?", (pid,))


@pytest.fixture()
def demo_off(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", False)


@pytest.fixture()
def quiet_socket(monkeypatch):
    from ws import realtime

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(realtime.sio, "emit", _noop)


def _mk_surgery(patient_id, surgeon_id, status="emergency_pending"):
    sid = f"SURG{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis,
                                      scheduled_date, scheduled_time, status)
               VALUES (?, ?, ?, 'To be confirmed', 'Acute abdomen', '2026-10-02', '09:00', ?)""",
            (sid, patient_id, surgeon_id, status),
        )
    return sid


def _surgery(sid):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM surgeries WHERE id = ?", (sid,)).fetchone())


def _assert_honest(row):
    assert row["status"] == "emergency_ready"
    assert row["success_probability"] is None, "no one estimated a success rate"
    text = " ".join(str(row[k] or "") for k in (
        "risk_assessment", "possible_complications", "surgical_checklist",
        "instruments_required", "staff_required")).lower()
    for pattern in FORBIDDEN:
        assert not re.search(pattern, text), f"package claims {pattern!r} with no assessment behind it"
    staff = json.loads(row["staff_required"])
    assert staff and all(s["name"] == "Assigned on arrival" for s in staff)
    assert json.loads(row["notes"])["ai_package_status"] == "template"


def test_relay_prep_package_is_honest(doctor, patient, demo_off):
    from services import surgery_escalation_service as esc
    sid = _mk_surgery(patient, doctor)
    asyncio.run(esc.generate_surgical_prep_package_async(sid, prep_id=-1))
    _assert_honest(_surgery(sid))


def test_transfer_package_is_honest(doctor, patient, demo_off, quiet_socket):
    from services.sage_handoff_service import generate_transfer_surgery_package_async
    sid = _mk_surgery(patient, doctor)
    asyncio.run(generate_transfer_surgery_package_async(sid))
    _assert_honest(_surgery(sid))


def test_a_failed_transfer_package_still_opens_the_case(doctor, patient, demo_off,
                                                       quiet_socket, monkeypatch):
    """A failed write used to leave the case behind the 'generating' spinner."""
    import services.sage_handoff_service as handoff
    sid = _mk_surgery(patient, doctor)

    def _boom(*a, **kw):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(handoff, "_persist_package", _boom)
    asyncio.run(handoff.generate_transfer_surgery_package_async(sid))
    row = _surgery(sid)
    assert row["status"] == "emergency_ready", "a failure must still open the case"
    assert row["risk_assessment"].startswith("Assessment unavailable")
    assert row["success_probability"] is None
    assert json.loads(row["notes"])["ai_package_status"] == "failed"


def test_a_package_without_an_estimate_stores_null_not_80(doctor, patient):
    """_finalize_surgery used to default a missing estimate to 80."""
    from services import surgery_escalation_service as esc
    sid = _mk_surgery(patient, doctor)
    esc._finalize_surgery(sid, {"risk_assessment": "x"}, "failed", "boom")
    assert _surgery(sid)["success_probability"] is None


def test_a_failed_package_says_assessment_unavailable(doctor, patient, demo_off):
    from services import surgery_escalation_service as esc
    sid = _mk_surgery(patient, doctor)

    def _boom(*a, **kw):
        raise RuntimeError("db hiccup")
    real = esc._mark_generating
    esc._mark_generating = _boom
    try:
        asyncio.run(esc.generate_surgical_prep_package_async(sid, prep_id=-1))
    finally:
        esc._mark_generating = real
    row = _surgery(sid)
    assert row["status"] == "emergency_ready", "a failure must still open the case"
    assert row["risk_assessment"].startswith("Assessment unavailable")
    assert row["success_probability"] is None
    assert json.loads(row["notes"])["ai_package_status"] == "failed"


@pytest.mark.parametrize("target, expected", [
    ("General Surgery", "To be confirmed"),
    ("general surgery", "To be confirmed"),
    ("", "To be confirmed"),
    ("Theatre", "To be confirmed"),
    ("Exploratory Laparotomy", "Exploratory Laparotomy"),
])
def test_transfer_procedure_comes_from_the_card(target, expected):
    from services.sage_handoff_service import _procedure_from_target
    assert _procedure_from_target(target, {"General Surgery", "Internal Medicine"}) == expected


def test_risk_assessment_fallback_makes_no_classification(monkeypatch):
    """The streamed assessment's offline fallback printed 'ASA Class III'."""
    import services.model_router as model_router
    from services.surgery_service import generate_risk_assessment

    async def _no_model():
        raise RuntimeError("no LLM in tests")
    monkeypatch.setattr(model_router, "select_chat_model", _no_model)

    detail = {
        "patient_name": "X", "age": 50, "sex": "M", "mrn": "M1",
        "patient_diagnosis": "Sepsis", "procedure_name": "Laparotomy",
        "diagnosis": "Perforation", "co_morbidities": ["DM", "HTN"],
        "risk_level": "critical", "risk_score": 9, "estimated_duration_min": 120,
        "possible_complications": ["bleeding"], "success_probability": 72.5,
    }

    async def drain():
        return [c async for c in generate_risk_assessment("S-X", detail=detail)]

    body = " ".join(asyncio.run(drain())).lower()
    assert "assessment unavailable" in body
    for pattern in (r"\basa\b", r"class iii", r"success rate", r"72\.5"):
        assert not re.search(pattern, body)


def test_closing_a_case_never_files_an_invented_operative_account(doctor, patient, monkeypatch):
    """DEMO_MODE used to file 'EBL 150mL, no complications' on any closed case."""
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.delenv("ORB_SURGERY_REQUIRE_CHECKLIST", raising=False)
    sid = _mk_surgery(patient, doctor, status="in_progress")
    with db_session() as db:
        db.execute(
            "INSERT INTO surgery_team_assignments (surgery_id, user_id, role_name, status) "
            "VALUES (?, ?, 'Surgeon', 'confirmed')", (sid, doctor),
        )
        now = datetime.now(timezone.utc).isoformat()
        state = {item["id"]: {"checked": True, "by": doctor, "at": now}
                 for phase in WHO_CHECKLIST["phases"] for item in phase["items"]}
        db.execute("UPDATE surgeries SET who_checklist = ? WHERE id = ?",
                   (json.dumps({"template": TEMPLATE_VERSION, "state": state}), sid))

    headers = {"Authorization": f"Bearer {create_token(doctor, 'doctor')}"}
    resp = client.post(f"/api/surgery/{sid}/complete", headers=headers)
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        note = db.execute("SELECT * FROM scribe_notes WHERE id = ?",
                          (resp.json()["note_id"],)).fetchone()
    body = (note["structured_note"] + note["raw_transcript"]).lower()
    assert "operative note pending" in body
    for invented in ("blood loss", "no complications", "hemostasis", "pacu"):
        assert invented not in body
    assert note["ai_generated"] == 0
