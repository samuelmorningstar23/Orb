"""F052: the device / HL7 feed alert lane, at parity with the nurse lane.

The ingestion lane wrote its Relay alert and stopped there: no
`vigil.alert.critical` publish (so no prewarm and no Sage draft), no escalation
chain until somebody opened the board, no vitals id on anything, and no
cooldown, so a real monitor feed paged on every reading. The simulator's
cooldown was process-local, so a restart re-paged every elevated patient.
"""
import json
import time
import uuid

import pytest

import services.bot_service as bot_service
from database import db_session
from services import escalation_service, ingestion_service, shadow_service


def _mk_patient() -> str:
    pid = f"PATIAP{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, status) VALUES (?, ?, ?, 'F-1', 'FEED', 'admitted')",
            (pid, f"Feed Parity {pid}", f"MRN-{uuid.uuid4().hex[:8]}"),
        )
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
            (f"CH-{pid}", f"Case: {pid}", pid),
        )
    return pid


@pytest.fixture()
def patient(monkeypatch):
    published: list[dict] = []

    def _record(*args, **kwargs):
        published.append({"args": args, "kwargs": kwargs})
        return True

    # Recorded, not published: a live publish schedules a task on a loop the
    # test closes when it returns.
    monkeypatch.setattr(bot_service, "publish_vigil_critical", _record)
    pid = _mk_patient()
    bot_service._alert_cooldowns.pop(pid, None)
    yield pid, published
    bot_service._alert_cooldowns.pop(pid, None)
    with db_session() as db:
        db.execute(
            "DELETE FROM escalation_events WHERE escalation_id IN "
            "(SELECT id FROM clinical_escalations WHERE patient_id = ?)", (pid,))
        db.execute("DELETE FROM clinical_escalations WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _payload(pid: str, *, hr=138, sbp=78, rr=30, spo2=84, temp=39.6) -> str:
    def obs(code, name, value, unit):
        return {"code": code, "name": name, "value": value, "unit": unit}
    return json.dumps({
        "patient_id": pid,
        "message_id": f"iap-{uuid.uuid4().hex}",
        "observations": [
            obs("8867-4", "Heart Rate", hr, "bpm"),
            obs("8480-6", "Systolic Blood Pressure", sbp, "mmHg"),
            obs("8462-4", "Diastolic Blood Pressure", 46, "mmHg"),
            obs("2708-6", "Oxygen Saturation", spo2, "%"),
            obs("8310-5", "Body Temperature", temp, "Cel"),
            obs("9279-1", "Respiratory Rate", rr, "/min"),
        ],
    })


async def _ingest(pid: str, **vitals):
    res = await ingestion_service.ingest_payload(_payload(pid, **vitals), source_name="feed-parity-test")
    assert not res.errors, res.errors
    assert res.inserted_vitals == 1, res.to_dict()


def _alerts_in_room(pid: str) -> int:
    with db_session() as db:
        return db.execute(
            "SELECT count(*) FROM messages WHERE channel_id = ? AND message_type = 'alert'",
            (f"CH-{pid}",)).fetchone()[0]


def _latest_vitals_id(pid: str) -> int:
    with db_session() as db:
        return db.execute(
            "SELECT id FROM vitals WHERE patient_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
            (pid,)).fetchone()["id"]


# ── the post-commit joins the nurse lane has ─────────────────────────────────
async def test_a_feed_deterioration_raises_the_escalation_chain_itself(patient):
    pid, _ = patient
    assert escalation_service.patient_escalations(pid) == []
    await _ingest(pid)
    chains = escalation_service.patient_escalations(pid)
    assert len(chains) == 1, "a device-fed peri-arrest reading must open the chain from the write path"
    assert chains[0]["last_vitals_id"] == _latest_vitals_id(pid), (
        "the chain must be pinned to the reading that raised it")


async def test_a_feed_alert_leaves_the_audit_chain_intact(patient):
    """relay_alert_generated passed the message id as an int: hashed as an int,
    verified as stored TEXT, so every feed alert that reached a room broke the
    tamper-evident chain."""
    from database import verify_audit_chain
    pid, _ = patient
    await _ingest(pid)
    assert _alerts_in_room(pid) == 1
    assert verify_audit_chain()["ok"]


async def test_a_feed_alert_wakes_the_nervous_system_with_its_message(patient):
    pid, published = patient
    await _ingest(pid)
    assert len(published) == 1
    call = published[0]
    assert call["args"][0] == pid
    assert call["kwargs"]["lane"] == "ingestion"
    assert call["kwargs"]["level"] == "critical"
    assert call["kwargs"]["message_id"], "the publish must name the Relay message it follows"


async def test_the_shadow_alert_names_the_reading(patient):
    pid, _ = patient
    await _ingest(pid)
    with db_session() as db:
        row = db.execute(
            "SELECT subject_id, payload_json FROM shadow_events_current "
            "WHERE kind = 'alert.fired' AND patient_id = ? ORDER BY id DESC LIMIT 1",
            (pid,)).fetchone()
    if row is None:
        pytest.skip("shadow instrument switched off in this environment")
    assert json.loads(row["payload_json"])["vital_id"] == _latest_vitals_id(pid)


# ── the cooldown ─────────────────────────────────────────────────────────────
async def test_a_steady_feed_pages_once_not_on_every_reading(patient):
    pid, published = patient
    for _ in range(4):
        await _ingest(pid)
    assert _alerts_in_room(pid) == 1, "a monitor feed paged on every reading"
    assert len(published) == 1
    with db_session() as db:
        held = db.execute(
            "SELECT count(*) FROM audit_logs WHERE action_type = 'vigil_alert_suppressed' "
            "AND patient_id = ?", (pid,)).fetchone()[0]
    assert held == 3


async def test_a_worse_reading_still_pages_inside_the_window(patient):
    pid, _ = patient
    await _ingest(pid, hr=112, sbp=105, rr=22, spo2=94, temp=38.4)
    ts, level, news2 = bot_service._alert_cooldowns[pid]
    bot_service._alert_cooldowns[pid] = (ts - (bot_service.MIN_REALERT_SECONDS + 1), level, news2)
    await _ingest(pid)   # the peri-arrest set: worse
    assert _alerts_in_room(pid) == 2, "a deteriorating patient was muted by the cooldown"


async def test_the_escalation_chain_still_sees_a_held_reading(patient):
    """The cooldown holds the page, never the chain."""
    pid, _ = patient
    await _ingest(pid)
    await _ingest(pid)
    chain = escalation_service.patient_escalations(pid)[0]
    assert chain["last_vitals_id"] == _latest_vitals_id(pid)


def test_a_restart_does_not_re_page_a_patient_paged_a_minute_ago(patient):
    """Seeded from the shadow log's `alert.fired`, so the window survives."""
    pid, _ = patient
    fired = shadow_service.utc_now()
    episode_id, _opened = shadow_service.open_episode(pid, "ingestion", occurred_at=fired,
                                                      trigger="news2_threshold")
    if not episode_id:
        pytest.skip("shadow instrument switched off in this environment")
    shadow_service.emit("alert.fired", episode_id, "ingestion", occurred_at=fired,
                        patient_id=pid, payload={"level": "critical", "raw_news2": 17})

    # The restart: an empty dict.
    bot_service._alert_cooldowns.pop(pid, None)
    assert bot_service.reconcile_alert_cooldowns() >= 1
    ts, level, news2 = bot_service._alert_cooldowns[pid]
    assert level == "critical" and news2 == 17
    assert abs(time.time() - ts) < 60

    suppressed, _since = bot_service.alert_cooldown_check(pid, "critical", 17)
    assert suppressed, "the same critical patient re-paged straight after a restart"
    worse, _ = bot_service.alert_cooldown_check(pid, "critical", 19, now=ts + bot_service.MIN_REALERT_SECONDS + 1)
    assert not worse, "a deterioration must still page after a restart"


def test_reconcile_never_overwrites_a_newer_live_entry(patient):
    pid, _ = patient
    fired = shadow_service.utc_now()
    episode_id, _opened = shadow_service.open_episode(pid, "ingestion", occurred_at=fired,
                                                      trigger="news2_threshold")
    if not episode_id:
        pytest.skip("shadow instrument switched off in this environment")
    shadow_service.emit("alert.fired", episode_id, "ingestion", occurred_at=fired,
                        patient_id=pid, payload={"level": "elevated", "raw_news2": 6})
    live = (time.time() + 5, "critical", 18)
    bot_service._alert_cooldowns[pid] = live
    bot_service.reconcile_alert_cooldowns()
    assert bot_service._alert_cooldowns[pid] == live
