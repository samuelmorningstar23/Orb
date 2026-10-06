"""Surgical Suite day list and WHO checklist (2026-10-06).

  * GET /api/surgery/schedule banded each case from risk_scores, and printed a
    patient with no risk row as "stable". The band is now the NEWS2 of the
    patient's latest vitals (the Vigil board's rule) with the number, and
    nothing recorded is None ("not scored").
  * The WHO checklist printed "Attested by DOC004". Each attested item now
    carries `by_name`, resolved from users at read time.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services.clinical_time import format_utc
from services.hospital_time import hospital_today

client = TestClient(app)

SURG_DEPT = "SchedNews2 General Surgery"


def _world():
    surgeon = f"SN2S{uuid.uuid4().hex[:6].upper()}"
    sick = f"SN2P{uuid.uuid4().hex[:6].upper()}"
    blank = f"SN2P{uuid.uuid4().hex[:6].upper()}"
    ts = format_utc(datetime.now(timezone.utc) - timedelta(minutes=10))
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) VALUES (?, ?, 'doctor', 'x', ?)",
            (surgeon, "Dr Meera Sharma", SURG_DEPT),
        )
        for pid in (sick, blank):
            db.execute(
                """INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status, age, sex)
                   VALUES (?, ?, ?, 'S-1', 'Surg', 'Test', 'admitted', 50, 'F')""",
                (pid, f"Sched {pid[-6:]}", f"MRN-{pid}"),
            )
        # A risk row saying stable: the old source. It must be ignored.
        db.execute(
            "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) VALUES (?, ?, 0.1, 'stable', '[]')",
            (sick, ts),
        )
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                   spo2, temperature, respiratory_rate, consciousness, on_oxygen)
               VALUES (?, ?, 135, 85, 50, 88, 39.5, 30, 'alert', 0)""",
            (sick, ts),
        )
        ids = []
        for pid, time in ((sick, "09:00"), (blank, "11:00")):
            sid = f"SN2X{uuid.uuid4().hex[:8].upper()}"
            db.execute(
                """INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis,
                                          scheduled_date, scheduled_time, status)
                   VALUES (?, ?, ?, 'Laparotomy', 'Perforation', ?, ?, 'scheduled')""",
                (sid, pid, surgeon, hospital_today().isoformat(), time),
            )
            ids.append(sid)
    return surgeon, sick, blank, ids


def _cleanup(surgeon, pids, sids):
    with db_session() as db:
        for sid in sids:
            db.execute("DELETE FROM surgeries WHERE id = ?", (sid,))
        for pid in pids:
            for tbl in ("vitals", "risk_scores"):
                db.execute(f"DELETE FROM {tbl} WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))


def test_the_day_list_bands_from_the_latest_vitals_with_the_number():
    surgeon, sick, blank, sids = _world()
    try:
        headers = {"Authorization": f"Bearer {create_token(surgeon, 'doctor')}"}
        resp = client.get("/api/surgery/schedule?days=1", headers=headers)
        assert resp.status_code == 200, resp.text
        cases = {s["patient_id"]: s for d in resp.json() for s in d["surgeries"]
                 if s["id"] in sids}
        assert cases[sick]["risk_level"] == "critical"
        assert cases[sick]["news2"] >= 7
        assert cases[sick]["surgeon_name"] == "Dr Meera Sharma"
        # Nothing recorded: no band, no number, never "stable".
        assert cases[blank]["risk_level"] is None
        assert cases[blank]["news2"] is None
    finally:
        _cleanup(surgeon, [sick, blank], sids)


def test_the_who_checklist_names_the_attester():
    surgeon, sick, blank, sids = _world()
    try:
        headers = {"Authorization": f"Bearer {create_token(surgeon, 'doctor')}"}
        r = client.post(f"/api/surgery/{sids[0]}/checklist",
                        json={"item_id": "si_identity", "checked": True}, headers=headers)
        assert r.status_code == 200, r.text
        item = r.json()["state"]["si_identity"]
        assert item["by"] == surgeon
        assert item["by_name"] == "Dr Meera Sharma"
        got = client.get(f"/api/surgery/{sids[0]}/checklist", headers=headers).json()
        assert got["state"]["si_identity"]["by_name"] == "Dr Meera Sharma"
    finally:
        _cleanup(surgeon, [sick, blank], sids)


def test_a_set_entered_in_error_is_not_the_band():
    """The schedule read the newest vitals row whatever its state. A set marked
    entered in error (voided_at) is skipped by every other vitals reader, the
    Vigil board included, so it must not band a case here either."""
    surgeon, sick, blank, sids = _world()
    later = format_utc(datetime.now(timezone.utc) - timedelta(minutes=2))
    try:
        with db_session() as db:
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                       spo2, temperature, respiratory_rate, consciousness, on_oxygen,
                       voided_at, voided_by, void_reason)
                   VALUES (?, ?, 72, 124, 70, 98, 36.8, 16, 'alert', 0, ?, ?, 'Wrong patient')""",
                (sick, later, later, surgeon),
            )
        headers = {"Authorization": f"Bearer {create_token(surgeon, 'doctor')}"}
        resp = client.get("/api/surgery/schedule?days=1", headers=headers)
        assert resp.status_code == 200, resp.text
        cases = {s["patient_id"]: s for d in resp.json() for s in d["surgeries"]
                 if s["id"] in sids}
        assert cases[sick]["risk_level"] == "critical"
    finally:
        _cleanup(surgeon, [sick, blank], sids)
