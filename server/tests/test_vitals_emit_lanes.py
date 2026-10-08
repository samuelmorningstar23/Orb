"""Every vitals writer must tell the open chart, and the nurse lane its case room.

WHY THIS FILE EXISTS (2026-10-03). The Vigil chart updates its command bar,
badge and NEWS2 breakdown from the `vitals:update` socket event. Two lanes
emitted it (the simulator, bot_service; the monitor feed, ingestion_service).
Two writers of vitals rows emitted nothing:

  * the nurse observation route (POST /api/nurse/patients/{id}/observations),
    which on a ward with no monitors is the ONLY vitals path there is;
  * Scribe auto-extraction (nervous_system_triggers._persist_extraction).

So a doctor with the chart open kept looking at the older set and its score
until something reloaded the page, which on a manual-only ward can be hours.
The nurse lane also stored its own Relay alert and never pushed it
(`message:new`), so the case room did not see a bedside NEWS2 19 until someone
reopened the room.

THE RULES PINNED HERE
  * The payload is the wire shape the other lanes send, and its `risk` is
    score_vitals_row on the row as stored, with the patient's on_scale_2. The
    chart ignores a tick without `risk` (command-bar contract, 2026-10-03).
  * A row that is not the patient's newest (back-charted) is stored and NOT
    pushed: "newest" is get_patient_risk's ORDER BY timestamp DESC, id DESC, so
    a live push always equals what a chart reload shows. A same-time
    correction wins the id tiebreak and IS pushed. A replay pushes nothing.
  * Pushing the chart pages nobody: one critical nurse set is exactly one Relay
    alert, one `vitals:update` and one `message:new`.

WHAT IS ASSERTED is what leaves the lane (the payload handed to the socket
layer, the Relay row as persisted), never a value the lane computed and kept.
"""
from __future__ import annotations

import ast
import asyncio
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ws.realtime as realtime
from auth import create_token
from database import db_session
from routes.nurse_routes import router as nurse_router
from services import nervous_system_triggers as nst
from services.event_bus import StateEvent
from services.vigil_service import score_vitals_row

SERVER = Path(__file__).resolve().parent.parent
DEPT = "Vitals Emit Test Ward"

#: The `vitals:update` body: bot_service's keys plus the `source` that
#: ingestion_service added. test_every_lane_sends_the_same_wire_shape holds the
#: two existing lanes to it, so this is not a third, private copy.
WIRE_KEYS = {
    "patient_id", "timestamp", "heart_rate", "systolic_bp", "diastolic_bp",
    "spo2", "temperature", "respiratory_rate", "consciousness", "on_oxygen",
    "risk", "source",
}

NORMAL = {"heart_rate": 78, "systolic_bp": 124, "diastolic_bp": 76,
          "respiratory_rate": 16, "spo2": 97, "temperature": 37.0}
# NEWS2 >= 7 on any reading of the bands: RR 28 (3), SpO2 89 (3), SBP 88 (3),
# HR 132 (3), Temp 39.4 (2).
CRITICAL = {"heart_rate": 132, "systolic_bp": 88, "diastolic_bp": 50,
            "respiratory_rate": 28, "spo2": 89, "temperature": 39.4}


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def patient():
    """An admitted patient on an isolated ward, with a case room. Discharged on
    teardown (TEST-ISOLATION LAW, see test_nurse_mode._discharge)."""
    pid = f"PATVE{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status) "
            "VALUES (?, ?, ?, 'VE-1', 'VE', 64, 'F', 'Pneumonia', 'admitted')",
            (pid, f"Emit Test {pid}", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('VEADMITTER', 'Emit Test Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, status, admitted_by) "
            "VALUES (?, ?, 'active', 'VEADMITTER')",
            (pid, DEPT),
        )
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
            (_room(pid), f"Case room {pid}", pid),
        )
    yield pid
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))


@pytest.fixture()
def nurse_user():
    uid = f"NUR{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, 'nurse', 'x', ?)",
            (uid, "Emit Test Nurse", DEPT),
        )
    return {"Authorization": f"Bearer {create_token(uid, 'nurse')}"}


class _Recorder:
    """Everything handed to the socket layer, with the real sockets stubbed.

    `emit_patient_vitals` is replaced (it is the department-scoped fan-out every
    lane uses; tests/test_ws_realtime.py covers the rooms it targets), and so is
    `sio.emit`, which carries `message:new` to a case room.
    """

    def __init__(self):
        self.patient_events: list[tuple[str, dict]] = []
        self.room_events: list[tuple[str, dict, str | None]] = []
        self.alert_lane_calls: list[str] = []

    def vitals(self, pid: str) -> list[dict]:
        return [p for e, p in self.patient_events
                if e == "vitals:update" and p.get("patient_id") == pid]

    def room(self, event: str, room: str) -> list[dict]:
        return [d for e, d, r in self.room_events if e == event and r == room]


@pytest.fixture()
def rec(monkeypatch):
    r = _Recorder()

    async def _patient_emit(event, payload):
        r.patient_events.append((event, payload))

    async def _sio_emit(event, data=None, room=None, **kw):
        r.room_events.append((event, data, room))

    monkeypatch.setattr(realtime, "emit_patient_vitals", _patient_emit)
    monkeypatch.setattr(realtime.sio, "emit", _sio_emit)

    # The prewarm/Sage draft that a critical nurse alert publishes would reach
    # for a model; it is not what these tests are about.
    import services.bot_service as bot_service
    monkeypatch.setattr(bot_service, "publish_vigil_critical", lambda *a, **k: True)

    # The other two alert lanes. Pushing the chart must never route through
    # them: the nurse lane already paged, and a second page is alert fatigue.
    async def _bot_lane(*a, **k):
        r.alert_lane_calls.append("bot_service.evaluate_and_dispatch_alerts")

    async def _ingest_lane(*a, **k):
        r.alert_lane_calls.append("ingestion_service._check_ingested_alert")

    import services.ingestion_service as ingestion_service
    monkeypatch.setattr(bot_service, "evaluate_and_dispatch_alerts", _bot_lane)
    monkeypatch.setattr(ingestion_service, "_check_ingested_alert", _ingest_lane)
    return r


@pytest.fixture()
def client():
    """`with`, so the portal (and its event loop) outlives each request: a push
    scheduled from the route's worker thread runs on that loop after the
    response, exactly as it does under uvicorn."""
    app = FastAPI()
    app.include_router(nurse_router)
    with TestClient(app) as c:
        yield c


# ── helpers ───────────────────────────────────────────────────────────────────
def _room(pid: str) -> str:
    return f"verm-{pid.lower()}"


async def _drain():
    """Let every push scheduled from a worker thread finish on this loop."""
    await asyncio.sleep(0)
    pending = [t for t in list(getattr(realtime, "_THREAD_EMITS", ())) if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=5)


def _chart(client, pid, headers, obs: dict, taken_at: str | None = None) -> dict:
    # A bedside nurse chooses ACVPU and air or oxygen on every set (nothing is
    # preselected and the route refuses a set without them).
    body = {"consciousness": "alert", "on_oxygen": False, **obs}
    if taken_at is not None:
        body["taken_at"] = taken_at
    resp = client.post(f"/api/nurse/patients/{pid}/observations", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    client.portal.call(_drain)
    return resp.json()


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def _stored(vitals_id: int):
    with db_session() as db:
        return db.execute("SELECT * FROM vitals WHERE id = ?", (vitals_id,)).fetchone()


def _vitals_count(pid: str) -> int:
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM vitals WHERE patient_id = ?", (pid,)).fetchone()[0]


def _alerts(pid: str) -> list:
    with db_session() as db:
        return db.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND message_type = 'alert' ORDER BY id",
            (_room(pid),),
        ).fetchall()


def _assert_wire(payload: dict) -> None:
    """The chart drops a tick without a scored `risk`, so this is the contract."""
    assert set(payload) == WIRE_KEYS, sorted(set(payload) ^ WIRE_KEYS)
    risk = payload["risk"]
    assert isinstance(risk, dict), payload
    assert isinstance(risk.get("breakdown"), list) and risk["breakdown"], risk
    assert isinstance(risk.get("raw_news2"), int), risk
    assert isinstance(payload["consciousness"], str)
    assert isinstance(payload["on_oxygen"], bool)


def _assert_scored_as_stored(payload: dict, row, *, on_scale_2: bool = False) -> None:
    """The pushed risk is what a reload would score from the stored row."""
    assert payload["timestamp"] == row["timestamp"]
    for k in ("heart_rate", "systolic_bp", "diastolic_bp", "spo2",
              "temperature", "respiratory_rate"):
        assert payload[k] == row[k], k
    assert payload["risk"] == score_vitals_row(row, on_scale_2=on_scale_2, overlay=False)


# ═════════════════════════════════════════════════════════════════════════════
#  The nurse lane
# ═════════════════════════════════════════════════════════════════════════════
def test_a_nurse_set_reaches_the_open_chart(patient, nurse_user, client, rec):
    body = _chart(client, patient, nurse_user,
                  {"heart_rate": 112, "systolic_bp": 98, "respiratory_rate": 22,
                   "spo2": 94, "temperature": 38.4})

    pushed = rec.vitals(patient)
    assert len(pushed) == 1, rec.patient_events
    payload = pushed[0]
    _assert_wire(payload)
    assert payload["source"] == "nurse_manual"
    assert payload["timestamp"] == body["recorded_at"]
    _assert_scored_as_stored(payload, _stored(body["vitals_id"]))
    assert payload["risk"]["raw_news2"] == body["news2"]["raw_news2"]


def test_a_partial_set_is_pushed_with_its_gaps_honest(patient, nurse_user, client, rec):
    """No BP on an agitated patient: the payload carries None for what was not
    taken, and the risk says which parameters are missing."""
    body = _chart(client, patient, nurse_user, {"heart_rate": 118, "respiratory_rate": 24})

    (payload,) = rec.vitals(patient)
    _assert_wire(payload)
    assert payload["systolic_bp"] is None and payload["spo2"] is None
    _assert_scored_as_stored(payload, _stored(body["vitals_id"]))
    assert payload["risk"].get("missing_parameters"), payload["risk"]


def test_a_scale_2_patient_is_pushed_on_scale_2(patient, nurse_user, client, rec):
    """COPD on air at SpO2 88 is on target: 0 points on Scale 2, 3 on Scale 1.
    The chart REPLACES its risk with the pushed one, so a Scale 1 push would
    paint a false +3 over a correct chart."""
    with db_session() as db:
        db.execute("UPDATE patients SET on_scale_2 = 1 WHERE id = ?", (patient,))

    body = _chart(client, patient, nurse_user, {**NORMAL, "spo2": 88})

    (payload,) = rec.vitals(patient)
    _assert_wire(payload)
    spo2 = [b for b in payload["risk"]["breakdown"] if str(b["parameter"]).startswith("SpO2")]
    assert len(spo2) == 1 and spo2[0]["parameter"] == "SpO2 (Scale 2)", spo2
    assert spo2[0]["points"] == 0, spo2
    _assert_scored_as_stored(payload, _stored(body["vitals_id"]), on_scale_2=True)


def test_a_back_charted_set_is_stored_but_not_pushed(patient, nurse_user, client, rec):
    """A set entered late for 2 hours ago is history, not news. Pushing it would
    show the chart an older reading as current."""
    _chart(client, patient, nurse_user, NORMAL, taken_at=_ago(minutes=10))
    assert len(rec.vitals(patient)) == 1

    late = _chart(client, patient, nurse_user, CRITICAL, taken_at=_ago(hours=2))

    assert _vitals_count(patient) == 2, "the back-charted set must still be charted"
    assert late["replayed"] is False
    assert len(rec.vitals(patient)) == 1, "a back-charted set must not reach the chart"


def test_a_same_time_correction_is_pushed_and_a_replay_is_not(patient, nurse_user, client, rec):
    at = _ago(minutes=5)
    _chart(client, patient, nurse_user, NORMAL, taken_at=at)

    replay = _chart(client, patient, nurse_user, NORMAL, taken_at=at)
    assert replay["replayed"] is True
    assert len(rec.vitals(patient)) == 1, "a replay charts nothing and must push nothing"

    fixed = _chart(client, patient, nurse_user, {**NORMAL, "heart_rate": 131}, taken_at=at)
    assert fixed.get("supersedes_vitals_id")
    pushed = rec.vitals(patient)
    assert len(pushed) == 2, "a correction at the same bedside time is the newest set"
    assert pushed[-1]["heart_rate"] == 131
    _assert_scored_as_stored(pushed[-1], _stored(fixed["vitals_id"]))


def test_one_critical_set_is_one_alert_one_chart_push_one_room_push(
        patient, nurse_user, client, rec):
    """Relay lane parity. The nurse lane already pages (dispatch_observation_alert);
    pushing the chart must add no second alert, and the alert it writes must
    reach the open case room live, as the simulator and ingestion lanes' do."""
    body = _chart(client, patient, nurse_user, CRITICAL)
    assert body["news2"]["level"] == "critical", body["news2"]

    alerts = _alerts(patient)
    assert len(alerts) == 1, [a["content"] for a in alerts]
    assert len(rec.vitals(patient)) == 1

    pushed = rec.room("message:new", _room(patient))
    assert len(pushed) == 1, rec.room_events
    assert pushed[0]["id"] == alerts[0]["id"]
    assert pushed[0]["message_type"] == "alert"
    # What a reload shows, AUTO chip included (`is_bot`): the live body used to
    # omit it, so the alert read as typed by a person until the room reloaded.
    from services.relay_service import get_channel_messages
    (reloaded,) = [m for m in get_channel_messages(_room(patient)) if m["id"] == alerts[0]["id"]]
    assert pushed[0] == reloaded
    assert pushed[0]["is_bot"] is True
    assert rec.alert_lane_calls == []


def test_a_stable_set_pushes_the_chart_and_pages_nobody(patient, nurse_user, client, rec):
    body = _chart(client, patient, nurse_user, NORMAL)
    assert body["news2"]["level"] == "stable"

    assert len(rec.vitals(patient)) == 1
    assert _alerts(patient) == []
    assert rec.room("message:new", _room(patient)) == []
    assert rec.alert_lane_calls == []


# ═════════════════════════════════════════════════════════════════════════════
#  The Scribe lane
# ═════════════════════════════════════════════════════════════════════════════
def _seed_note(pid: str) -> int:
    with db_session() as db:
        return db.execute(
            "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, structured_note, "
            "status, note_format) VALUES (?, 'D001', 'Transcript', 'O: see stub', 'signed', 'soap')",
            (pid,),
        ).lastrowid


async def _sign(monkeypatch, pid: str, vitals: dict) -> None:
    note_id = _seed_note(pid)

    async def _fake_extract(note_id, patient_id, structured_text):
        return {"vitals": vitals}

    monkeypatch.setattr(nst, "_extract_updates", _fake_extract)
    await nst.on_scribe_note_signed(StateEvent(
        topic="scribe.note.signed", source_node="scribe", event_type="note_signed",
        patient_id=pid, payload={"note_id": note_id, "signer_id": "D001"},
    ))


def _scribe_row(pid: str):
    with db_session() as db:
        return db.execute(
            "SELECT * FROM vitals WHERE patient_id = ? AND source = 'llm_extracted' "
            "ORDER BY id DESC LIMIT 1", (pid,),
        ).fetchone()


async def test_scribe_extracted_vitals_reach_the_open_chart(patient, rec, monkeypatch):
    await _sign(monkeypatch, patient, {"heart_rate": 124, "systolic_bp": 92,
                                        "respiratory_rate": 25, "spo2": 92,
                                        "temperature": 38.6})

    row = _scribe_row(patient)
    assert row is not None
    pushed = rec.vitals(patient)
    assert len(pushed) == 1, rec.patient_events
    _assert_wire(pushed[0])
    assert pushed[0]["source"] == "llm_extracted"
    # Scored on the row AS STORED, which is exactly what a chart reload scores.
    _assert_scored_as_stored(pushed[0], row)
    # The wire is never null for ACVPU (ws/realtime.vitals_update_payload);
    # whether it was assessed travels in risk.missing_parameters. Since
    # 2026-10-03 a Scribe row with no assessment in the last 12 h stores NULL
    # rather than the column default 'alert'.
    assert pushed[0]["consciousness"] == (row["consciousness"] or "alert")
    if row["consciousness"] is None:
        assert "Consciousness (ACVPU)" in pushed[0]["risk"]["missing_parameters"]


async def test_a_scribe_row_that_is_not_the_newest_is_not_pushed(patient, rec, monkeypatch):
    """A nurse set stamped a few minutes ahead (the route allows +5 min of
    clock skew) is newer than the extraction, so the extraction is history."""
    ahead = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, spo2, "
            "respiratory_rate, temperature, source) VALUES (?, ?, 80, 120, 97, 16, 37.0, "
            "'nurse_manual')", (patient, ahead),
        )

    await _sign(monkeypatch, patient, {"heart_rate": 124, "spo2": 92})

    assert _scribe_row(patient) is not None, "the extraction must still be charted"
    assert rec.vitals(patient) == []


async def test_the_scribe_timeline_update_reaches_the_ward(patient, rec, monkeypatch):
    """It went to room=<patient id>, which no socket ever joins, so PatientStory
    never heard it. It now takes the department-scoped fan-out."""
    await _sign(monkeypatch, patient, {"heart_rate": 90})

    timeline = [p for e, p in rec.patient_events if e == "patient:timeline_updated"]
    assert timeline == [{"patient_id": patient, "source": "scribe_extraction"}]
    assert [e for e, _d, r in rec.room_events if r == patient] == []


# ═════════════════════════════════════════════════════════════════════════════
#  The wire shape and the thread bridge
# ═════════════════════════════════════════════════════════════════════════════
def _payload_literals(path: Path) -> list[set[str]]:
    """Key sets of every dict literal that is a vitals:update body."""
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if {"patient_id", "heart_rate", "risk"} <= keys:
                out.append(keys)
    return out


def test_every_lane_sends_the_same_wire_shape():
    bot = _payload_literals(SERVER / "services" / "bot_service.py")
    ingest = _payload_literals(SERVER / "services" / "ingestion_service.py")
    assert bot == [WIRE_KEYS - {"source"}], bot
    assert ingest == [WIRE_KEYS], ingest

    row = {"patient_id": "P1", "timestamp": "2026-10-03T09:00:00+00:00",
           "heart_rate": 80.0, "systolic_bp": None, "diastolic_bp": None,
           "spo2": 97.0, "temperature": None, "respiratory_rate": 16.0,
           "consciousness": None, "on_oxygen": None}
    risk = score_vitals_row(row, on_scale_2=False, overlay=False)
    payload = realtime.vitals_update_payload(row, risk, source="nurse_manual")
    _assert_wire(payload)
    # Ingestion's coercions: never null on the wire for these two. The honest
    # "not assessed" travels in risk.missing_parameters.
    assert payload["consciousness"] == "alert" and payload["on_oxygen"] is False


async def test_the_bridge_reaches_the_loop_from_a_worker_thread(rec):
    payload = {"patient_id": "PBRIDGE1", "risk": {}}
    ok = await anyio.to_thread.run_sync(
        realtime.schedule_patient_vitals, "vitals:update", payload)
    await _drain()
    assert ok is True
    assert rec.patient_events == [("vitals:update", payload)]


async def test_the_bridge_works_on_the_loop_itself(rec):
    payload = {"patient_id": "PBRIDGE2", "risk": {}}
    assert realtime.schedule_patient_vitals("vitals:update", payload) is True
    await _drain()
    assert rec.patient_events == [("vitals:update", payload)]


def test_the_bridge_never_raises_without_a_loop(rec):
    """A script, or a unit test calling the service directly: the row is
    already charted, so the push is skipped, quietly and without an error."""
    result: list = []
    t = threading.Thread(target=lambda: result.append(
        realtime.schedule_patient_vitals("vitals:update", {"patient_id": "PBRIDGE3"})))
    t.start()
    t.join(5)
    assert result == [False]
    assert rec.patient_events == []
