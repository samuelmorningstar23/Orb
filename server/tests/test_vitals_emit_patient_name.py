"""A `vitals:update` names the patient, so a deterioration alert can too.

src/services/criticalAlerts.ts announces "<patient> has entered CRITICAL
status" from `vital.patient_name`, but no lane put a name on the wire, so the
notification always read the raw patient id. The name is added once, where
every lane's update leaves the server (realtime.emit_patient_vitals), from a
short TTL cache like the department's. The rooms are unchanged: they mirror the
REST board's scope, so everyone who receives the update can already see this
patient's name on the board.
"""
import asyncio
import uuid

import pytest

from database import db_session
from ws import realtime


@pytest.fixture()
def admitted():
    pid = f"PATVN{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, 'Asha Rao', ?, 'admitted')",
                   (pid, f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'system', 'Vitals Name Ward', 'active')",
                   (f"ADM-{uuid.uuid4().hex[:8]}", pid))
    realtime._patient_dept_cache.pop(pid, None)
    realtime._patient_name_cache.pop(pid, None)
    yield pid
    with db_session() as db:
        db.execute("DELETE FROM admissions WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _capture(monkeypatch):
    sent = []

    async def fake_emit(event, payload, room=None):
        sent.append((event, payload, room))

    monkeypatch.setattr(realtime.sio, "emit", fake_emit)
    return sent


def test_a_vitals_update_carries_the_patient_name(admitted, monkeypatch):
    sent = _capture(monkeypatch)

    asyncio.run(realtime.emit_patient_vitals("vitals:update", {"patient_id": admitted, "risk": {}}))

    assert sent, "nothing was emitted"
    assert all(payload.get("patient_name") == "Asha Rao" for _e, payload, _r in sent), sent
    assert {room for _e, _p, room in sent} == {"dept:__all__", "dept:Vitals Name Ward"}


def test_other_patient_events_are_left_as_they_are(admitted, monkeypatch):
    sent = _capture(monkeypatch)

    asyncio.run(realtime.emit_patient_vitals("patient_status_update", {"patient_id": admitted}))

    assert all("patient_name" not in payload for _e, payload, _r in sent), sent


def test_an_unknown_patient_gets_no_invented_name(monkeypatch):
    sent = _capture(monkeypatch)

    asyncio.run(realtime.emit_patient_vitals("vitals:update", {"patient_id": "NOBODY-HERE"}))

    assert all("patient_name" not in payload for _e, payload, _r in sent), sent
