"""With scoring switched off (ORB_SCORING_ENABLED, the Orb Ward Record) the
appointments list, the Surgical Suite day list and the Command Center compute
no NEWS2 (integration of the Ward Record build, 2026-10-06).

The Vigil board already sent no score with the switch off. These three screens
were rewritten the same day to score each patient's latest vitals themselves,
so with the switch off they still printed bands and a critical count. Each now
asks vigil_service.scoring_enabled() and, when it is off, carries no band and
no number. A control test proves the same rows are still scored with it on.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services.clinical_time import format_utc
from services.hospital_time import hospital_today

client = TestClient(app)

TAG = "SOFF"
DOCTOR = "SOFF-DOC"
_CRITICAL = (135, 85, 88, 39.5, 30)   # NEWS2 14


def _ts(minutes_ago: int = 5) -> str:
    return format_utc(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))


@pytest.fixture
def world():
    """Two admitted patients with a critical set each, on two wards whose
    ward then bed order differs from their id order, each booked for an
    appointment tomorrow and a surgery today."""
    a = f"{TAG}-B{uuid.uuid4().hex[:6].upper()}"
    b = f"{TAG}-A{uuid.uuid4().hex[:6].upper()}"
    sids, appt_ids = [], []
    tomorrow = (hospital_today() + timedelta(days=1)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, 'Scoring Off Doctor', 'doctor', 'x')",
            (DOCTOR,),
        )
        for pid, ward, bed in ((a, "SOFF-W1", "B-2"), (b, "SOFF-W2", "B-1")):
            db.execute(
                """INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, status, age, sex)
                   VALUES (?, ?, ?, ?, ?, 'Test', 'admitted', 50, 'F')""",
                (pid, f"Scoring Off {pid[-6:]}", f"MRN-{pid}", ward, bed),
            )
            hr, sbp, spo2, temp, rr = _CRITICAL
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                       spo2, temperature, respiratory_rate, consciousness, on_oxygen)
                   VALUES (?, ?, ?, ?, 70, ?, ?, ?, 'alert', 0)""",
                (pid, _ts(), hr, sbp, spo2, temp, rr),
            )
            sid = f"{TAG}X{uuid.uuid4().hex[:8].upper()}"
            db.execute(
                """INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis,
                                          scheduled_date, scheduled_time, status)
                   VALUES (?, ?, ?, 'Laparotomy', 'Perforation', ?, '09:00', 'scheduled')""",
                (sid, pid, DOCTOR, hospital_today().isoformat()),
            )
            sids.append(sid)
            aid = str(uuid.uuid4())
            db.execute(
                """INSERT INTO appointments (id, patient_id, booked_by_user_id, scheduled_date,
                       scheduled_time, appointment_type, reason, status, origin)
                   VALUES (?, ?, ?, ?, '10:00', 'Follow-up', 'Review', 'booked', 'staff')""",
                (aid, pid, DOCTOR, tomorrow),
            )
            appt_ids.append(aid)
    yield {"pids": (a, b), "sids": sids}
    with db_session() as db:
        for sid in sids:
            db.execute("DELETE FROM surgeries WHERE id = ?", (sid,))
        for aid in appt_ids:
            db.execute("DELETE FROM appointments WHERE id = ?", (aid,))
        for pid in (a, b):
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _scoring(monkeypatch, on: bool):
    monkeypatch.setattr(config, "SCORING_ENABLED", on, raising=False)


def _appointments(pids):
    from services.forecast_service import get_appointments
    return [r for r in get_appointments(days_ahead=3) if r["patient_id"] in pids]


def _surgeries(sids):
    headers = {"Authorization": f"Bearer {create_token(DOCTOR, 'doctor')}"}
    resp = client.get("/api/surgery/schedule?days=1", headers=headers)
    assert resp.status_code == 200, resp.text
    return [s for d in resp.json() for s in d["surgeries"] if s["id"] in sids]


def test_scoring_off_the_appointments_carry_no_band(monkeypatch, world):
    _scoring(monkeypatch, False)
    rows = _appointments(world["pids"])
    assert len(rows) == 2
    for r in rows:
        assert r["acuity"] is None and r["news2"] is None and r["news2_complete"] is None


def test_scoring_on_the_same_appointments_are_banded(monkeypatch, world):
    _scoring(monkeypatch, True)
    rows = _appointments(world["pids"])
    assert {r["acuity"] for r in rows} == {"critical"}


def test_scoring_off_the_surgical_day_list_carries_no_band(monkeypatch, world):
    _scoring(monkeypatch, False)
    cases = _surgeries(world["sids"])
    assert len(cases) == 2
    for c in cases:
        assert c["risk_level"] is None and c["news2"] is None and c["risk_score"] is None


def test_scoring_on_the_same_cases_are_banded(monkeypatch, world):
    _scoring(monkeypatch, True)
    assert {c["risk_level"] for c in _surgeries(world["sids"])} == {"critical"}


def test_scoring_off_the_command_center_bands_nobody(monkeypatch, world):
    from services import command_center_service as cc
    _scoring(monkeypatch, False)
    patients = cc._admitted_with_acuity()
    mine = [p for p in patients if p["id"] in world["pids"]]
    assert all(p["level"] is None and p["news2"] is None for p in mine)
    # Ward, then bed: SOFF-W1 before SOFF-W2, whatever the ids say.
    a, b = world["pids"]
    assert [p["id"] for p in mine] == [a, b]
    snap = cc.house_snapshot()
    assert snap["scoring_enabled"] is False
    assert snap["deteriorations"] == []
    assert snap["acuity"]["critical"] == 0 and snap["acuity"]["elevated"] == 0
    assert snap["acuity"]["not_scored"] == snap["census"]["total_admitted"]


def test_scoring_on_the_command_center_lists_them(monkeypatch, world):
    from services import command_center_service as cc
    _scoring(monkeypatch, True)
    snap = cc.house_snapshot()
    assert snap["scoring_enabled"] is True
    det = [d["patient_id"] for d in snap["deteriorations"]]
    assert set(world["pids"]) <= set(det)
