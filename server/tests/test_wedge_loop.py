"""Orb — THE WEDGE: Vigil detects → Sage reasons → Relay puts it in front of someone.

Orb does not sell agents, it sells this loop, and the end-to-end verification on
2026-08-13 found it was the WEAKEST of the eight workflows. Three defects, none
of them visible from inside any single module:

DEFECT A — THE INSTRUMENT WAS BLIND TO THE LANE THE PILOT RUNS ON.
``nurse_shift_service.dispatch_observation_alert`` called ``_persist_relay_alert``
with no ``episode_id``, so a nurse-charted deterioration wrote NEITHER
``alert.fired`` NOR ``alert.persisted``. Verified on the appliance: zero shadow
rows for the two real alerts raised during the dry run. The simulator lane
threads an episode correctly, so ``instrument.healthy`` read true and the
scorecard was a document about the simulator. Week-13 IS shadow go-live and
manual entry is the redundancy layer the pilot runs on, which makes this the
pilot-evidence invariant rather than an observability nicety.

DEFECT B — THE NERVOUS SYSTEM ONLY WOKE FOR SIMULATED PATIENTS.
``vigil.alert.critical`` had exactly one publisher, in ``bot_service``. A nurse
charting peri-arrest vitals raised a Relay message and left the prewarm asleep —
no care bundle opened, no RAG context assembled, Sage cold when the registrar
opens the chart.

DEFECT C — SAGE CONTRIBUTED NOTHING MEASURABLE TO AN ALERT.
The only Sage output in the whole loop is a one-sentence 4B garnish appended on
the simulator lane, which no shadow row recorded. It is STILL a garnish — see
``test_the_alert_draft_is_not_an_approvable_assessment``, which pins that
deliberately. What changed is that the loop is measurable, so the distance
between what it does and what the wedge story implies is now a number.

WHAT IS ASSERTED HERE is always a row in a table — never a return value, never a
log line. ``shadow_service`` swallows its own exceptions by design, so a test
that trusts a 200 can happily measure an empty instrument.

WHY THE TEST CLIENT IS ALWAYS CONTEXT-MANAGED. Starlette tears the blocking
portal down when a non-context TestClient finishes a request, so a task the
request scheduled on the loop is cancelled before it ever runs — the event-bus
assertions would fail against perfectly correct code. `with TestClient(...)`
keeps one portal alive across the block, which is also the production shape: a
long-lived loop the request hands work back to.
"""
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrations  # noqa: E402
from auth import create_token  # noqa: E402
from database import db_session  # noqa: E402
from routes.nurse_routes import router as nurse_router  # noqa: E402
from services import shadow_service  # noqa: E402
from services.vigil_service import compute_news2  # noqa: E402


DEPARTMENT = "Wedge Loop Medicine"

# NEWS2 19 — the exact clinical picture from the dry run: RR 32 (3), SpO2 86 (3),
# on O2 (2), Temp 39.4 (2), SBP 78 (3), HR 138 (3), ACVPU voice (3).
PERI_ARREST = {
    "heart_rate": 138, "systolic_bp": 78, "diastolic_bp": 44,
    "respiratory_rate": 32, "temperature": 39.4, "spo2": 86,
    "consciousness": "voice", "on_oxygen": True,
}
# NEWS2 9 — critical, and therefore already at tier 2, the highest tier the
# escalation policy defines an entry score for. The open chain a worse score has
# to get past.
CRITICAL_9 = {
    "heart_rate": 120, "systolic_bp": 95, "diastolic_bp": 62,
    "respiratory_rate": 24, "temperature": 38.5, "spo2": 92,
    "consciousness": "alert", "on_oxygen": False,
}
# NEWS2 0 — nothing may be dispatched and nothing may be instrumented.
BENIGN = {
    "heart_rate": 74, "systolic_bp": 122, "diastolic_bp": 78,
    "respiratory_rate": 16, "temperature": 36.8, "spo2": 98,
    "consciousness": "alert", "on_oxygen": False,
}


def _news2(vitals: dict) -> dict:
    return compute_news2(vitals["heart_rate"], vitals["systolic_bp"],
                         vitals["respiratory_rate"], vitals["temperature"],
                         vitals["spo2"], vitals.get("consciousness", "alert"),
                         vitals.get("on_oxygen", False))


# ── the app under test ───────────────────────────────────────────────────────
def _app() -> FastAPI:
    """ONLY the nurse router.

    Not `main.app`: its lifespan starts the simulator, the shadow sweeper, the
    escalation sweep and the model warmups, and every assertion below depends on
    the observation write being the only thing that ran.
    """
    app = FastAPI()
    app.include_router(nurse_router)
    return app


def _auth(uid, role="nurse"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_nurse() -> str:
    uid = f"WEDGE{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, 'nurse', 'x', ?)",
            (uid, "Wedge Loop Nurse", DEPARTMENT),
        )
    return uid


#: Every patient this module seeds, so teardown can put them back.
_CREATED: list[str] = []


def _mk_patient(*, case_room=True) -> str:
    """An admitted patient in DEPARTMENT, optionally with a Relay case room.

    The department lives on the ADMISSION — `patients.ward` is a bed label and is
    not what scoping reads (tests/test_nurse_write_scope.py holds that boundary).
    """
    pid = f"WEDGEP{uuid.uuid4().hex[:8].upper()}"
    _CREATED.append(pid)
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WEDGEADM', 'Wedge Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, 'W-1', 'WL-1', 64, 'M', 'Wedge loop fixture',
                       'admitted', ?)""",
            (pid, f"Wedge {pid}", f"MRN-{uuid.uuid4().hex[:8].upper()}", now),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'WL-1', 'W-1', 'active', 'WEDGEADM')",
            (pid, DEPARTMENT),
        )
        if case_room:
            db.execute(
                "INSERT OR REPLACE INTO channels (id, name, type, patient_id) "
                "VALUES (?, ?, 'case_room', ?)",
                (f"wedge-room-{pid}", f"Wedge Case Room {pid}", pid),
            )
    return pid


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW: this module seeds `status='admitted'` patients into a
    session DB shared by every other file.

    Left admitted they are not a cosmetic leak. `bot_service._generate_vitals`
    appends a reading to EVERY admitted patient on a simulation tick, so a
    leftover grows a vitals trend it was never meant to have and starts counting
    as `worsening` in `compute_ward_state` — an aggregate this file never
    touches. These fixtures charted deliberate deterioration, so they landed
    there as `critical`: on 2026-08-15 this module was the last one still
    feeding patients into that list.

    Discharge rather than DELETE: the vitals, shadow and channel rows written
    during the test legitimately reference these ids. Teardown runs AFTER the
    test, so its own assertions still see an admitted patient.
    """
    yield
    if not _CREATED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?",
            [(p,) for p in _CREATED],
        )
    _CREATED.clear()


@pytest.fixture(autouse=True)
def _fresh_instrument(monkeypatch):
    """A clean shadow table per test, and an empty episode registry.

    The registry is PROCESS-LOCAL and survives a table drop, so without the reset
    a later test could resolve an episode id minted by an earlier one and every
    `episode.opened` assertion would be measuring the wrong test.
    """
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "true")
    shadow_service.reset_registry()
    _rebuild_shadow_table()
    yield
    shadow_service.reset_registry()
    _rebuild_shadow_table()


def _rebuild_shadow_table() -> None:
    with db_session() as conn:
        conn.execute("DROP VIEW IF EXISTS shadow_events_current")
        conn.execute("DROP TABLE IF EXISTS shadow_events")
        migrations._m4_shadow_events(conn)
        migrations._m18_shadow_none_confirmed_once(conn)


# ── readers ──────────────────────────────────────────────────────────────────
def _rows(patient_id=None, kind=None) -> list[dict]:
    sql = "SELECT * FROM shadow_events_current WHERE 1=1"
    args: list = []
    if patient_id:
        sql += " AND patient_id = ?"
        args.append(patient_id)
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    with db_session() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id", tuple(args)).fetchall()]


def _payload(row) -> dict:
    try:
        return json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def _relay_alert_text(patient_id: str) -> str:
    """The Relay message body as it was PERSISTED — the text a nurse opens.

    Read from `messages` rather than from a return value: the alert is composed
    in `evaluate_and_dispatch_alerts` and written by `_persist_alerts` on a
    `run_db` worker, and the whole class of defect here is a lane whose OUTPUT
    says something different from what the composing code believes it says.
    """
    with db_session() as db:
        row = db.execute(
            "SELECT m.content FROM messages m "
            "JOIN channels c ON c.id = m.channel_id "
            "WHERE c.patient_id = ? AND m.message_type = 'alert' "
            "ORDER BY m.id DESC LIMIT 1",
            (patient_id,),
        ).fetchone()
    assert row is not None, f"no Relay alert was persisted for {patient_id}"
    return row["content"]


def _messages(patient_id) -> list[dict]:
    with db_session() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT m.* FROM messages m JOIN channels c ON c.id = m.channel_id "
            "WHERE c.patient_id = ? ORDER BY m.id", (patient_id,)).fetchall()]


def _vitals(patient_id) -> list[dict]:
    with db_session() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM vitals WHERE patient_id = ? ORDER BY id", (patient_id,)).fetchall()]


def _bus_events(patient_id) -> list[dict]:
    with db_session() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM event_log WHERE topic = 'vigil.alert.critical' "
            "AND patient_id = ? ORDER BY rowid", (patient_id,)).fetchall()]


def _await_bus_event(patient_id, timeout=5.0) -> list[dict]:
    """Poll for the published event.

    The publish is deliberately fire-and-forget: the clinical write is already
    committed and the nervous system is an accelerator, so the request must not
    block on the bus. A poll is therefore the honest way to observe it — a
    synchronous assertion would only pass if the publish blocked the nurse.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _bus_events(patient_id)
        if rows:
            return rows
        time.sleep(0.02)
    return _bus_events(patient_id)


def _chart(client, nurse, patient, vitals, expect=200):
    r = client.post(f"/api/nurse/patients/{patient}/observations",
                    json=dict(vitals), headers=_auth(nurse))
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else None


# ═════════════════════════════════════════════════════════════════════════════
#  DEFECT A — the manual lane is on the instrument
# ═════════════════════════════════════════════════════════════════════════════
def test_a_nurse_charted_deterioration_writes_fired_and_persisted_on_one_episode():
    """THE PILOT-EVIDENCE INVARIANT.

    Both halves of the alert, sharing ONE episode_id. The shared id is the whole
    point: `messages` has no patient_id, no level and no FK to the vital that
    fired it, and correlation_id overlap between audit_logs and event_log is
    literally zero rows — so there is no table in which both halves of an episode
    appear and no join that could reconstruct this afterwards. Not threaded at
    write time means detection lead time is not computable for the lane the pilot
    runs on, and is not recoverable later.
    """
    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, PERI_ARREST)

    fired = _rows(patient, "alert.fired")
    persisted = _rows(patient, "alert.persisted")
    assert len(fired) == 1, f"no alert.fired for a NEWS2 19 nurse observation: {fired}"
    assert len(persisted) == 1, (
        "the alert was written into Relay but never recorded as persisted — the "
        f"scorecard cannot see that anybody was reachable: {persisted}")
    assert fired[0]["episode_id"] == persisted[0]["episode_id"], (
        "the two halves of one alert landed on DIFFERENT episodes; nothing "
        "downstream can pair them")

    opened = _rows(patient, "episode.opened")
    assert len(opened) == 1
    assert opened[0]["episode_id"] == fired[0]["episode_id"]

    payload = _payload(fired[0])
    assert payload["raw_news2"] == _news2(PERI_ARREST)["raw_news2"] == 19
    assert payload["level"] == "critical"
    assert payload["repeat"] is False, "the first alert of an episode is not a repeat"


def test_the_manual_lane_is_recorded_as_manual():
    """The lane string IS the finding.

    A manual alert filed under lane='ingestion' leaves the scorecard exactly as
    blind to bedside entry as no row at all — worse, it would look like the
    monitor feed was working. `_persist_relay_alert` hard-codes lane='ingestion'
    on the rows it writes, which is why this path emits its own instead of
    handing an episode into it.
    """
    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, PERI_ARREST)

    lanes = {r["lane"] for r in _rows(patient)}
    assert lanes == {"manual"}, f"a manual-lane alert was recorded under {lanes}"


def test_the_alert_fired_row_points_at_the_vitals_row_that_caused_it():
    """`subject_id` is the only link from the instrument back to the measurement.

    Without it an adjudicator reviewing a flagged episode cannot see which
    observation fired it, and `false_alert_rate` becomes a judgement about a
    number with no chart behind it.
    """
    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        result = _chart(client, nurse, patient, PERI_ARREST)

    fired = _rows(patient, "alert.fired")[0]
    assert fired["subject_type"] == "vitals"
    assert str(fired["subject_id"]) == str(result["vitals_id"])
    assert _payload(fired)["vital_id"] == result["vitals_id"]


def test_the_persisted_row_is_the_join_relay_engagement_needs():
    """`alert.persisted` is the ONLY route from a message back to its episode.

    `messages` carries no episode_id, so `relay_service.shadow_alert_episodes`
    joins on exactly (kind='alert.persisted', subject_type='messages',
    subject_id=message_id) and INHERITS the lane from the row it finds. Without a
    manual-lane row, opening a nurse-raised alert produces no `alert.viewed`, the
    sweeper expires 100% of them, and engagement for the pilot's own lane reads a
    structural zero — a number produced by missing code rather than by clinician
    behaviour.

    Asserted on the row's shape rather than by driving the Relay route: the join
    is a literal column tuple, and a subject_type of 'message' or a subject_id
    holding the vitals id would silently miss it.
    """
    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, PERI_ARREST)

    persisted = _rows(patient, "alert.persisted")[0]
    message_ids = [m["id"] for m in _messages(patient)]
    assert persisted["subject_type"] == "messages"
    assert int(persisted["subject_id"]) in message_ids
    assert _payload(persisted)["message_id"] == int(persisted["subject_id"])
    assert persisted["lane"] == "manual", (
        "the view row would inherit the wrong lane and land in another lane's "
        "engagement figure")


def test_detection_time_is_stamped_before_the_relay_write():
    """`occurred_at` is the instant NEWS2 crossed, not the instant the row landed.

    Since encryption-at-rest every page write pays HMAC-SHA512 + AES. Stamping
    after the write would build SQLCipher's own latency into the pilot's headline
    lead-time number — the one figure this whole instrument exists to produce.
    """
    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, PERI_ARREST)

    fired = _rows(patient, "alert.fired")[0]
    persisted = _rows(patient, "alert.persisted")[0]
    assert fired["occurred_at"] <= persisted["occurred_at"]
    assert fired["occurred_at"] <= fired["recorded_at"], (
        "detection was stamped AFTER it was recorded — occurred_at is coming "
        "from the insert rather than from the NEWS2 result")


def test_a_second_deterioration_joins_the_open_episode():
    """A re-alert JOINS; it does not mint.

    Minting per alert would turn one deteriorating patient into a string of
    one-row episodes, and the false-alert denominator would become a measurement
    of how often the ward takes observations. Same rule the simulator lane
    documents in bot_service.
    """
    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, CRITICAL_9)
        _chart(client, nurse, patient, PERI_ARREST)

    fired = _rows(patient, "alert.fired")
    assert len(fired) == 2
    assert len({r["episode_id"] for r in fired}) == 1, "two episodes for one deterioration"
    assert len(_rows(patient, "episode.opened")) == 1
    assert _payload(fired[0])["repeat"] is False
    assert _payload(fired[1])["repeat"] is True


def test_a_stable_observation_instruments_nothing():
    """An instrument that fires on everything measures nothing.

    `alert.suppressed` exists for a deterioration that was muted; a NEWS2 0 is
    not a suppressed alert and must not open an episode, or every per-episode
    denominator in the scorecard starts counting healthy patients.
    """
    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, BENIGN)

    assert _vitals(patient), "the observation itself must still be charted"
    assert _rows(patient) == [], "a stable observation opened a shadow episode"


def test_a_deterioration_that_reached_nobody_is_recorded_as_dropped():
    """THE NEGATIVE. NEWS2 fired and there was no case room to post into.

    Without this row, "the nurse charted a deterioration and nobody was
    reachable" is indistinguishable from "the nurse charted nothing", and a
    scorecard built only on the alerts that worked is marketing.
    """
    nurse, patient = _mk_nurse(), _mk_patient(case_room=False)
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, PERI_ARREST)

    dropped = _rows(patient, "alert.dropped")
    assert len(dropped) == 1, f"a deterioration reached nobody, silently: {dropped}"
    assert _payload(dropped[0])["reason"] == "no_case_room"
    assert dropped[0]["episode_id"] == _rows(patient, "alert.fired")[0]["episode_id"]
    assert _rows(patient, "alert.persisted") == []


# ═════════════════════════════════════════════════════════════════════════════
#  The instrument FAILS OPEN — audit is the fail-closed one
# ═════════════════════════════════════════════════════════════════════════════
def test_a_shadow_failure_never_breaks_the_observation_write(monkeypatch):
    """The rule that separates this log from `audit_logs`.

    Shadow is the observational record and fails OPEN; audit is the compliance
    record and stays fail-closed. A broken instrument must cost the pilot its
    evidence, never the ward its charting — so the reading, its NEWS2 and the
    Relay alert all have to survive an instrument that raises on every call.
    """
    def _explode(*a, **kw):
        raise RuntimeError("shadow instrument is down")

    monkeypatch.setattr(shadow_service, "open_episode", _explode)
    monkeypatch.setattr(shadow_service, "emit", _explode)
    monkeypatch.setattr(shadow_service, "emit_many", _explode)

    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        result = _chart(client, nurse, patient, PERI_ARREST)

    assert result["news2"]["raw_news2"] == 19
    assert len(_vitals(patient)) == 1, "the charted reading was lost"
    assert any("VIGIL ALERT" in (m["content"] or "") for m in _messages(patient)), (
        "the Relay alert was lost because the shadow log was down")


# ═════════════════════════════════════════════════════════════════════════════
#  DEFECT B — one event, every lane
# ═════════════════════════════════════════════════════════════════════════════
def test_a_manual_critical_alert_publishes_vigil_alert_critical():
    """The prewarm has to fire for the lane the pilot runs on.

    `nervous_system_triggers.on_critical_vigil_alert` opens the deterioration
    care bundle and pre-assembles the RAG context Sage needs. Until 2026-08-14
    the only publisher was the simulator loop, so it fired for demo patients and
    for nobody a nurse charts.

    Asserted on the `event_log` WAL row rather than on a subscriber callback:
    publish() persists BEFORE it dispatches, so the row is the durable fact and a
    test that hooks a subscriber would pass on a bus that wrote nothing.
    """
    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, PERI_ARREST)
        events = _await_bus_event(patient)

    assert len(events) == 1, (
        "a nurse-charted NEWS2 19 did not wake the nervous system: " + str(events))
    evt = events[0]
    assert evt["source_node"] == "vigil"
    assert evt["event_type"] == "patient_deteriorated"
    payload = json.loads(evt["payload"])
    assert payload["level"] == "critical"
    assert payload["raw_news2"] == 19
    assert payload["lane"] == "manual"
    assert payload["message_id"] is not None, (
        "the event cannot be joined back to the Relay message it belongs to")


def test_both_lanes_publish_the_same_event_shape():
    """One topic, one shape, two lanes — asserted by comparison, not by reading.

    The failure this prevents is the tempting one: a second `StateEvent(...)`
    literal in nurse_shift_service that drifts from bot_service's within a week
    and leaves subscribers reading keys that exist on only one lane. Both lanes
    go through `build_vigil_critical_event`, so an edit to one is an edit to
    both.
    """
    from services import bot_service

    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, PERI_ARREST)
        events = _await_bus_event(patient)
    assert events, "no manual-lane event to compare against"
    manual = json.loads(events[0]["payload"])

    sim = bot_service.build_vigil_critical_event(
        patient, level="critical", score=0.9, drivers=["x"],
        message_id=1, raw_news2=19, lane="simulator")

    assert sim.topic == "vigil.alert.critical"
    assert sim.source_node == "vigil"
    assert sim.event_type == "patient_deteriorated"
    assert set(sim.payload) == set(manual), (
        f"the lanes publish different payload shapes: "
        f"simulator={sorted(sim.payload)} manual={sorted(manual)}")


def test_the_publisher_degrades_instead_of_raising_when_no_loop_is_reachable():
    """A script, a `run_db` worker, a unit test — no portal back to any loop.

    The publish is an accelerator. It must degrade to a log line, because the
    alternative is a nurse's charted observation raising out of a post-commit
    hook over a preloader that could not be woken.
    """
    from services import bot_service

    assert bot_service.publish_vigil_critical(
        "NOBODY", level="critical", score=0.9, drivers=[], raw_news2=19,
        lane="manual") is False


def test_the_event_carries_the_score_so_the_bundle_policy_is_not_bypassed():
    """`raw_news2` in the payload is load-bearing, not decoration.

    `on_critical_vigil_alert` falls back to an UNCONDITIONAL `start_bundle()`
    when the score is absent — so without this key a NEWS2 5 'elevated' alert
    opens a Sepsis Six bundle, bypassing `sepsis_bundle_service`'s own stated
    NEWS2>=7 policy. With the score present the subscriber takes
    `maybe_autostart`, which IS that policy.
    """
    from services import bot_service

    evt = bot_service.build_vigil_critical_event(
        "P", level="elevated", score=0.4, drivers=[], raw_news2=5, lane="manual")
    assert evt.payload["raw_news2"] == 5


# ═════════════════════════════════════════════════════════════════════════════
#  DEFECT C — Sage's contribution to an alert is on the record
# ═════════════════════════════════════════════════════════════════════════════
async def test_a_sage_assessment_for_an_alert_lands_on_the_alerts_episode(monkeypatch):
    """`sage.drafted` threaded onto the SAME episode as the alert.

    Not merely "a Sage row exists": on a different episode it is an orphan and
    the middle of the wedge stays unmeasurable. The claim under test is that
    detection, reasoning and delivery are ONE chain.
    """
    from services import bot_service

    patient = _mk_patient()
    _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation("Escalate to the registrar; start fluids."))

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])

    drafted = _rows(patient, "sage.drafted")
    fired = _rows(patient, "alert.fired")
    assert len(fired) == 1
    assert len(drafted) == 1, f"Sage drafted on an alert and nothing recorded it: {drafted}"
    assert drafted[0]["episode_id"] == fired[0]["episode_id"], (
        "the Sage draft is an orphan — it is not on the alert's episode")
    assert drafted[0]["lane"] == "simulator"


async def test_the_static_fallback_is_not_counted_as_a_sage_draft(monkeypatch):
    """Sage down must READ as Sage down.

    `generate_ai_recommendation` returns a fixed sentence when the circuit
    breaker is open, the ambient tier has been evicted, or the LLM queue times
    out. Counting that as a draft would make the instrument report Sage
    participation on a night no model ran — the single most misleading row this
    scorecard could carry.
    """
    from services import bot_service, vigil_service

    patient = _mk_patient()
    _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation(vigil_service._FALLBACK_RECOMMENDATION))

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])

    assert _rows(patient, "alert.fired"), "the alert itself must still fire"
    assert _rows(patient, "sage.drafted") == [], (
        "the static degraded-mode sentence was recorded as a Sage draft")


# ═════════════════════════════════════════════════════════════════════════════
#  D6 — LANE PARITY ON THE DEGRADED-MODE SENTENCE
# ═════════════════════════════════════════════════════════════════════════════
# WHAT WAS ON SCREEN (2026-08-14): every Relay alert ended with
#
#     ✦ MedGemma AI: Ensure continuous monitoring and prepare for possible
#     intervention
#
# — which is `vigil_service._FALLBACK_RECOMMENDATION`, the STATIC degraded-mode
# sentence emitted when the circuit breaker is open, the ambient tier has been
# evicted, or the LLM queue timed out. Attributed to a model that never ran, with
# none of the disclosure the alert surface makes about the identical sentence
# ("Standing guidance … This is Orb's fixed degraded-mode line — the same
# sentence for every alert, not an assessment of this patient").
#
# The shadow lane already knew the difference — `test_the_static_fallback_is_not
# _counted_as_a_sage_draft` above has passed since the day it was written — which
# is what made this a LANE PARITY defect rather than a missing feature: the same
# content was honest in one lane, dishonest in another, and honest in the lane
# nobody but us reads. The law in this repo is that a phrase any lane strips,
# every lane must strip, and the same disclosure any lane makes, every lane must
# make.

async def test_a_degraded_relay_alert_never_attributes_the_static_line_to_a_model(monkeypatch):
    """THE D6 REGRESSION, read off the persisted Relay message a nurse opens."""
    from services import bot_service, vigil_service

    patient = _mk_patient()
    _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation(vigil_service._FALLBACK_RECOMMENDATION))

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])

    msg = _relay_alert_text(patient)
    assert vigil_service._FALLBACK_RECOMMENDATION in msg, (
        "the standing guidance itself is still worth sending — this test is "
        "about the attribution, not about removing the sentence")
    assert "MedGemma" not in msg, (
        f"the static degraded-mode sentence is credited to a model that never "
        f"ran:\n{msg}")
    assert "✦" not in msg, (
        "the ✦ glyph is Orb's model-output marker everywhere else in the UI; on "
        "a fixed sentence it makes the same claim the words would")
    assert bot_service.RELAY_DEGRADED_MODE_DISCLAIMER in msg, (
        f"Relay omits the degraded-mode disclosure the alert surface makes about "
        f"this exact sentence:\n{msg}")


async def test_a_model_written_sentence_is_still_attributed_to_the_model(monkeypatch):
    """The positive control, and the thing that stops the cheap fix.

    Stripping the attribution unconditionally would pass the test above and lose
    the only honest signal in the message: that a model DID write this line and
    the reader should weigh it accordingly.
    """
    from services import bot_service

    patient = _mk_patient()
    _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation("Escalate to the registrar; start fluids."))

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])

    msg = _relay_alert_text(patient)
    assert "**✦ MedGemma AI**: Escalate to the registrar; start fluids." in msg, msg
    assert bot_service.RELAY_DEGRADED_MODE_DISCLAIMER not in msg, (
        "a real drafted sentence is being disclaimed as the fixed degraded line — "
        "the disclaimer means nothing if it is on every alert")


@pytest.mark.parametrize("sentence,drafted", [
    ("Escalate to the registrar; start fluids.", True),
    (None, False),      # None -> the module's own static fallback, resolved below
])
async def test_the_relay_message_and_the_shadow_row_agree_about_who_wrote_it(
        monkeypatch, sentence, drafted):
    """ONE decision, TWO consumers — asserted by comparison, not by reading.

    What the nurse is told and what the scorecard counts are now the same
    boolean. They were two independent expressions of it, which is how they came
    to disagree: the shadow lane excluded the fallback, the message lane did not.
    """
    from services import bot_service, vigil_service

    if sentence is None:
        sentence = vigil_service._FALLBACK_RECOMMENDATION

    patient = _mk_patient()
    _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation(sentence))

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])

    msg = _relay_alert_text(patient)
    says_model_wrote_it = "MedGemma" in msg
    recorded_a_draft = bool(_rows(patient, "sage.drafted"))
    assert says_model_wrote_it == recorded_a_draft == drafted, (
        f"message says model-written={says_model_wrote_it}, shadow recorded a "
        f"draft={recorded_a_draft}, expected {drafted}\n{msg}")


async def test_the_relay_reason_line_uses_severe_for_the_same_patients_as_the_drivers(monkeypatch):
    """D5's SECOND copy — the one that lives in the Relay lane.

    `evaluate_and_dispatch_alerts` composes its own phrases for the same
    physiology, against its own ad-hoc cutoffs, and they had drifted the same way
    the driver strip's had: "Severe tachycardia" from HR 121, "SpO2 critically
    low" from 89 %. A nurse reads this line and the driver strip within a minute
    of each other; two sets of words for one patient is how a demo unravels.

    The dry-run peri-arrest set is the useful probe precisely because it is
    MIXED: RR 32 and SpO2 86 genuinely are severe, HR 138 is not. Since
    2026-10-02 HR 138 is counted in "(+4 more)" rather than printed, so its
    word is checked on the full line the helper would print with no limit.
    """
    from services.vigil_service import news2_reason_line
    from services import bot_service

    patient = _mk_patient()
    _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation("Escalate to the registrar."))

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])

    msg = _relay_alert_text(patient)
    full = news2_reason_line(_news2(PERI_ARREST), limit=7)
    assert "Moderate tachycardia (HR 138)" in full, (
        f"HR {PERI_ARREST['heart_rate']} is tachycardia, not severe "
        f"tachycardia:\n{full}")
    # SpO2 86 IS below the 88 % severity threshold, so the strong wording here is
    # earned, asserted so the fix cannot be "delete every strong word".
    assert f"Severe hypoxia (SpO2 {PERI_ARREST['spo2']}%)" in msg, msg
    assert "dropping" not in msg, (
        "one vitals tick cannot support a claim about a trend")
    # Seven parameters scored. Since 2026-10-02 the line names the worst three
    # (points, then Severe before Moderate, then RCP chart order) and counts
    # the rest rather than hiding them.
    assert ("Severe tachypnea (RR 32); Severe hypoxia (SpO2 86%); "
            "Severe hypotension (SBP 78) (+4 more)") in msg, msg


# ═════════════════════════════════════════════════════════════════════════════
#  ALL THREE LANES PRINT THE NUMBER NEWS2 SCORED (ruled 2026-10-02)
# ═════════════════════════════════════════════════════════════════════════════
# NEWS2 now scores each vital AS CHARTED (half-up to the chart's precision; see
# services/vital_charting.py and tests/test_news2_charted_value.py). Each alert
# lane printed its own copy of the vitals with f"{v:.0f}" / f"{v:.1f}", which
# round half to even and round 38.05 DOWN, and chose its words off the raw
# float, so one Relay message could carry "RR 20" in the vitals line and
# "Tachypnea (RR 21)" in the drivers. The two non-simulator lanes also printed
# a stand-in for a vital that was never measured. The expected lines below are
# literals, not built with the helper under test.

async def test_the_simulator_alert_prints_and_words_the_charted_values(monkeypatch):
    """Raw HR 139.6 is charted, scored and labelled HR 140, which is severe.
    The reason line used to choose its word off 139.6 and read "Moderate
    tachycardia: HR 140 bpm"; SpO2 87.5 read "critically low at 88%" beside a
    driver calling 88 % moderate."""
    from services import bot_service

    vitals = {"heart_rate": 139.6, "systolic_bp": 79.5, "diastolic_bp": 44,
              "spo2": 87.5, "temperature": 38.05, "respiratory_rate": 20.5,
              "consciousness": "voice", "on_oxygen": True}
    patient = _mk_patient()
    _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation("Escalate to the registrar."))
    payload = {"patient_id": patient, "risk": _news2(vitals),
               **{k: vitals[k] for k in ("heart_rate", "systolic_bp", "diastolic_bp",
                                         "spo2", "temperature", "respiratory_rate")}}

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [payload])

    msg = _relay_alert_text(patient)
    assert "HR 140 · BP 80/44 · SpO2 88% · Temp 38.1°C · RR 21" in msg, msg
    assert "Severe tachycardia (HR 140)" in msg, msg
    assert "Moderate tachycardia" not in msg, msg
    assert "Moderate hypoxia (SpO2 88%)" in msg, msg
    assert "Severe hypoxia" not in msg, (
        f"SpO2 charted 88% is not below the 88% severity line:\n{msg}")


def test_the_nurse_alert_prints_charted_values_and_no_stand_in_for_a_missing_one():
    """No SpO2 was taken. The alert used to say "Observed at the bedside: ...
    SpO2 100%", beside a NEWS2 that had counted SpO2 as not measured."""
    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, {
            "heart_rate": 130.5, "systolic_bp": 120, "diastolic_bp": 80,
            "respiratory_rate": 20.5, "temperature": 38.05,
            "consciousness": "alert", "on_oxygen": False,
        })

    msg = _relay_alert_text(patient)
    assert ("Observed at the bedside: HR 131 · BP 120/80 · SpO2 -- · "
            "Temp 38.1°C · RR 21 · ACVPU alert") in msg, msg
    assert "SpO2 100%" not in msg, msg


async def test_the_monitor_feed_alert_prints_charted_values_and_no_stand_in(monkeypatch):
    """A feed with no HR and no SpO2. HR used to default to 0, which printed
    "HR 0" in the vitals line AND fired "Bradycardia: HR 0 bpm" as a reason.
    SBP 90.4 is charted 90, which scores 3; raw banding scored it 2."""
    from services import ingestion_service

    patient = _mk_patient()
    _silence_bus(monkeypatch)
    payload = {"systolic_bp": 90.4, "diastolic_bp": 50, "respiratory_rate": 20.5,
               "temperature": 38.05, "source": "ingested_json"}
    risk = compute_news2(None, payload["systolic_bp"], payload["respiratory_rate"],
                         payload["temperature"], None)
    assert risk["single_param_3"] is True, risk

    await ingestion_service._check_ingested_alert(patient, payload, risk)

    msg = _relay_alert_text(patient)
    assert "Current vitals: HR -- · BP 90/50 · SpO2 -- · Temp 38.1°C · RR 21" in msg, msg
    assert "HR 0" not in msg and "Bradycardia" not in msg, msg
    assert "Moderate hypotension (SBP 90)" in msg, msg


# ═════════════════════════════════════════════════════════════════════════════
#  THE REASON LINE IS THE NEWS2 BREAKDOWN, IN ALL THREE LANES (ruled 2026-10-02)
# ═════════════════════════════════════════════════════════════════════════════
# Each lane composed its own reason phrases against its own cutoffs. Measured
# through the real lanes before the ruling, cases 1 to 6 came out wrong in all
# 18 lane and case pairs (case 1 dropped RR 22 and case 6 the ACVPU change
# everywhere), the simulator included, which D5 had only partly aligned. The
# line is now the NEWS2 parameters that scored, worst first (points, then
# Severe before Moderate, then RCP chart order), three named and the rest
# counted. Cases 7 to 10 were added on 2026-10-03 for the severity rule and
# the tie order of oxygen and Scale 2. A
# parameter that scored 0 is never a reason. The expected lines are literals,
# not built with `news2_reason_line`.
_REASON_CASES = {
    # All 2 points. Two lanes called HR 125 "Severe tachycardia".
    "1_three_2_pointers": (
        {"heart_rate": 125, "systolic_bp": 120, "diastolic_bp": 80, "spo2": 93,
         "temperature": 37.0, "respiratory_rate": 22},
        "Tachypnea (RR 22); Hypoxia (SpO2 93%); Tachycardia (HR 125)"),
    # The 3-point hypotension is what escalated this patient. Every lane
    # printed only "Tachycardia: HR 105 bpm".
    "2_hypotension_beside_mild_tachycardia": (
        {"heart_rate": 105, "systolic_bp": 88, "diastolic_bp": 50, "spo2": 97,
         "temperature": 37.0, "respiratory_rate": 16},
        "Moderate hypotension (SBP 88); Mild tachycardia (HR 105)"),
    # New confusion. Every lane printed only "Tachycardia: HR 105 bpm", and
    # NEWS2's own `drivers` left it out too until they were sorted worst first
    # the same day (tests/test_news2_driver_order.py).
    "3_new_confusion_behind_three_1_pointers": (
        {"heart_rate": 105, "systolic_bp": 120, "diastolic_bp": 80, "spo2": 95,
         "temperature": 37.0, "respiratory_rate": 10, "consciousness": "confusion"},
        "Altered consciousness (ACVPU); Low RR (10); Mild hypoxia (SpO2 95%) (+1 more)"),
    # COPD on Scale 2: SpO2 90 on air is ON TARGET and scores 0. Every lane
    # called it low or "dropping".
    "4_scale_2_saturation_on_target": (
        {"heart_rate": 80, "systolic_bp": 120, "diastolic_bp": 80, "spo2": 90,
         "temperature": 37.0, "respiratory_rate": 26, "scale_2": True},
        "Moderate tachypnea (RR 26)"),
    # SBP 170 scores 0. Every lane printed "Hypertension" in the third slot of
    # this critical alert and dropped the 3-point RR 26 off the line.
    "5_hypertension_scored_nothing": (
        {"heart_rate": 115, "systolic_bp": 170, "diastolic_bp": 95, "spo2": 93,
         "temperature": 37.0, "respiratory_rate": 26},
        "Moderate tachypnea (RR 26); Hypoxia (SpO2 93%); Tachycardia (HR 115)"),
    # Three 3-pointers no lane had a phrase for. The ACVPU change sorts with
    # Severe, so it is named before the mild (32-35 C) hypothermia.
    "6_low_side_three_way_tie": (
        {"heart_rate": 80, "systolic_bp": 120, "diastolic_bp": 80, "spo2": 97,
         "temperature": 34.8, "respiratory_rate": 7, "consciousness": "confusion"},
        "Severe bradypnea (RR 7); Altered consciousness (ACVPU); Hypothermia (34.8°C)"),
    # Four 3-pointers, one Severe. Chart order alone named the three Moderate
    # ones and counted Severe tachycardia, which the old line had shown.
    "7_septic_shock_severe_before_moderate": (
        {"heart_rate": 142, "systolic_bp": 82, "diastolic_bp": 48, "spo2": 90,
         "temperature": 39.5, "respiratory_rate": 26},
        "Severe tachycardia (HR 142); Moderate tachypnea (RR 26); Moderate hypoxia (SpO2 90%) (+2 more)"),
    # Inside the Severe group the RCP chart decides: consciousness BEFORE
    # temperature, though the engine checks them the other way round.
    "8_hypothermia_severe_group_in_chart_order": (
        {"heart_rate": 38, "systolic_bp": 82, "diastolic_bp": 50, "spo2": 90,
         "temperature": 31.6, "respiratory_rate": 8, "consciousness": "voice"},
        "Severe bradypnea (RR 8); Severe bradycardia (HR 38); Altered consciousness (ACVPU) (+3 more)"),
    # Three 2-pointers: oxygen sits between SpO2 and blood pressure on the chart.
    "9_oxygen_in_chart_order": (
        {"heart_rate": 125, "systolic_bp": 95, "diastolic_bp": 60, "spo2": 97,
         "temperature": 37.0, "respiratory_rate": 16, "on_oxygen": True},
        "On supplemental oxygen; Hypotension (SBP 95); Tachycardia (HR 125)"),
    # A scoring Scale 2 saturation keeps the SpO2 row's place on the chart.
    "10_scale_2_spo2_in_chart_order": (
        {"heart_rate": 125, "systolic_bp": 120, "diastolic_bp": 80, "spo2": 85,
         "temperature": 37.0, "respiratory_rate": 22, "scale_2": True},
        "Tachypnea (RR 22); Hypoxia (SpO2 85%, Scale 2); Tachycardia (HR 125)"),
}


async def _reason_line_from(lane: str, vitals: dict) -> str:
    """The reason line of the alert `lane` actually PERSISTED for `vitals`."""
    from services import bot_service, ingestion_service

    vitals = dict(vitals)
    scale_2 = vitals.pop("scale_2", False)
    consciousness = vitals.pop("consciousness", "alert")
    on_oxygen = vitals.pop("on_oxygen", False)
    patient = _mk_patient()
    if lane == "nurse":
        # The real route, which scores the observation itself, on the
        # patient's own SpO2 scale.
        if scale_2:
            with db_session() as db:
                db.execute("UPDATE patients SET on_scale_2 = 1 WHERE id = ?", (patient,))
        with TestClient(_app()) as client:
            _chart(client, _mk_nurse(), patient,
                   {**vitals, "consciousness": consciousness, "on_oxygen": on_oxygen})
    else:
        risk = compute_news2(vitals["heart_rate"], vitals["systolic_bp"],
                             vitals["respiratory_rate"], vitals["temperature"],
                             vitals["spo2"], consciousness=consciousness,
                             on_oxygen=on_oxygen, on_scale_2=scale_2)
        if lane == "simulator":
            await bot_service.evaluate_and_dispatch_alerts(
                _NullSio(), [{"patient_id": patient, "risk": risk, **vitals}])
        else:
            await ingestion_service._check_ingested_alert(
                patient, {**vitals, "source": "ingested_json"}, risk)
    # "⚠️ VIGIL ALERT [NURSE OBS]: <reason>", "⚠️ VIGIL ALERT: <reason>", ...
    first_line = _relay_alert_text(patient).split("\n", 1)[0]
    return first_line.split(": ", 1)[1]


@pytest.mark.parametrize("lane", ["simulator", "nurse", "feed"])
@pytest.mark.parametrize("case", sorted(_REASON_CASES))
async def test_every_lane_words_the_reason_as_the_news2_breakdown(case, lane, monkeypatch):
    from services import bot_service

    _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation("Escalate to the registrar."))
    vitals, expected = _REASON_CASES[case]

    assert await _reason_line_from(lane, vitals) == expected


@pytest.mark.parametrize("lane", ["simulator", "nurse", "feed"])
async def test_a_failing_reason_line_still_sends_the_alert_with_its_band(lane, monkeypatch):
    """Wording only means wording only (ruled 2026-10-03, G2). Before the
    fallback, forcing the helper to raise lost the whole alert in every lane:
    a charted critical reading reached nobody."""
    from services import bot_service, ingestion_service, nurse_shift_service

    _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation("Escalate to the registrar."))

    def _boom(*_a, **_k):
        raise RuntimeError("forced reason-line failure")

    for module in (bot_service, ingestion_service, nurse_shift_service):
        monkeypatch.setattr(module, "news2_reason_line", _boom)
    vitals, _ = _REASON_CASES["7_septic_shock_severe_before_moderate"]

    assert await _reason_line_from(lane, vitals) == "NEWS2 critical"


def test_a_result_with_no_breakdown_falls_back_to_its_drivers_then_its_band():
    """Hand-built NEWS2 dicts (other test files, any future caller) carry no
    breakdown. The line must still say something true rather than nothing."""
    from services.vigil_service import news2_reason_line

    assert news2_reason_line({"level": "elevated",
                              "drivers": ["A", "B", "C", "D"]}) == "A; B; C"
    assert news2_reason_line({"level": "critical"}) == "NEWS2 critical"


def test_relay_and_the_alert_surface_use_ONE_wording_for_the_disclosure():
    """The two lanes quote the same sentence, or the ward gets two stories.

    Asserted against the component file because that is where the other lane's
    copy actually lives; `SageAlertAssessment.tsx` renders it beside
    `fallback_text` on the alert card. If the copy is reworded on either side
    this fails, which is the point — the failure is a prompt to change both.
    """
    import re as _re
    from services import bot_service

    tsx = (Path(__file__).resolve().parents[2]
           / "src" / "components" / "sage" / "SageAlertAssessment.tsx")
    assert tsx.exists(), f"the alert surface moved: {tsx}"
    # JSX wraps the copy across source lines; compare on collapsed whitespace.
    rendered = _re.sub(r"\s+", " ", tsx.read_text(encoding="utf-8"))
    wanted = _re.sub(r"\s+", " ", bot_service.RELAY_DEGRADED_MODE_DISCLAIMER)
    assert wanted in rendered, (
        f"Relay and the alert card no longer make the same disclosure.\n"
        f"Relay says: {wanted}")


async def test_the_alert_draft_carries_no_latency_into_the_time_saved_proxy(monkeypatch):
    """THE HONEST-NUMBER GUARD, and it is deliberately restrictive.

    `shadow_scorecard_service._time_saved` multiplies every TIMED `sage.drafted`
    by an assumed manual-lookup baseline — the figure its own METRIC_SPEC calls
    "the one most likely to be quoted back in a fundraising context and the one
    with the weakest foundation". A one-sentence garnish nobody asked for did not
    replace a clinician's lookup, so it must not enter that denominator.

    The latency IS worth measuring. Recording it requires `_time_saved` to filter
    on `query_class` first — a read-side change in a module this file does not
    own. Until then, absent beats inflated.
    """
    from services import bot_service

    patient = _mk_patient()
    _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation("Escalate to the registrar."))

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])

    payload = _payload(_rows(patient, "sage.drafted")[0])
    assert payload.get("ttft_ms") is None and payload.get("total_ms") is None, (
        "an alert garnish is being timed into the time-saved proxy")
    assert payload["query_class"] == "VIGIL_ALERT_RECOMMENDATION", (
        "without a distinguishing query_class the read side cannot exclude these")
    assert payload["citation_basis"] == "none", (
        "no retrieval ran; 'none' is the honest value — null reads as "
        "'not captured'")


async def test_the_alert_draft_is_not_an_approvable_assessment(monkeypatch):
    """WHAT THE LOOP ACTUALLY DOES, pinned so it cannot be mistaken for the pitch.

    The wedge story is "Sage drafts the action, a clinician approves or declines
    it". What ships is a one-sentence recommendation appended to the alert text.
    There is no `action.proposed`, so there is nothing to approve, nothing to
    decline, and `agreement` — the metric that would show whether clinicians
    trust Sage — stays pinned at insufficient_data however many alerts fire.

    This is a STATEMENT OF SCOPE, not a defect report. Delete it the day a real
    drafted assessment with an approve/decline affordance ships; until then it
    stops the gap being closed by accident in a commit message.
    """
    from services import bot_service

    patient = _mk_patient()
    _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation("Escalate to the registrar."))

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])

    assert _rows(patient, "sage.drafted"), "precondition: a draft was recorded"
    assert _rows(patient, "action.proposed") == [], (
        "an approve/decline affordance appeared — update this test and the wedge "
        "report, the middle of the loop is no longer a garnish")


async def test_the_simulator_lane_publishes_through_the_shared_publisher(monkeypatch):
    """The simulator's own call site still goes through the one publisher.

    Extracting the event constructor is only worth anything if BOTH lanes use it.
    Asserted on the call rather than on `event_log` so the assertion does not
    depend on a fire-and-forget task completing inside the test's loop.
    """
    from services import bot_service

    patient = _mk_patient()
    calls = _silence_bus(monkeypatch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation",
                        _fake_recommendation("Escalate to the registrar."))

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])

    assert len(calls) == 1, f"the simulator lane no longer publishes: {calls}"
    assert calls[0]["kwargs"]["lane"] == "simulator"
    assert calls[0]["kwargs"]["raw_news2"] == 19
    assert calls[0]["kwargs"]["level"] == "critical"


# ═════════════════════════════════════════════════════════════════════════════
#  Detection → escalation, through the write path
# ═════════════════════════════════════════════════════════════════════════════
def test_a_worse_bedside_observation_escalates_an_already_open_chain():
    """The NEWS2 19 that vanished, reproduced through the route a nurse uses.

    On 2026-08-13 the case room showed "NEWS2 19 (critical)" while the escalation
    board showed 10 for the same patient in the same minute. Tier 2 is the
    highest tier the policy defines an entry score for, so "does this deserve a
    HIGHER tier?" answered no for every score from 7 to 20 and the worse reading
    was swallowed whole.

    Driven through the HTTP route, not by calling escalation_service: half of
    this defect was a MISSING CALL, and a test that calls the service directly
    cannot see a missing call.
    """
    from services import escalation_service as esc

    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, CRITICAL_9)

        mine = [c for c in esc.list_escalations(department=DEPARTMENT)
                if c["patient_id"] == patient]
        assert len(mine) == 1, "charting a NEWS2 9 raised no escalation chain"
        chain_id, entry_tier = mine[0]["id"], mine[0]["current_tier"]
        assert mine[0]["peak_news2"] == 9

        _chart(client, nurse, patient, PERI_ARREST)

    after = esc.get_escalation(chain_id)
    assert after["peak_news2"] == 19, (
        f"a bedside NEWS2 19 never reached the chain — the board reads "
        f"{after['peak_news2']}")
    assert after["news2"] == 19, "the board is displaying a stale, milder score"
    assert after["current_tier"] >= entry_tier
    assert len([c for c in esc.list_escalations(department=DEPARTMENT)
                if c["patient_id"] == patient]) == 1, (
        "the deterioration spawned a SECOND chain instead of extending the open one")


def test_one_bedside_deterioration_produces_one_coherent_episode():
    """The loop, end to end, as a single readable chain.

    Every row a downstream reader needs to answer "Vigil saw it, Relay carried
    it, and here is when" shares one episode_id and arrives in causal order.

    ASSERTED AS A SUBSEQUENCE, not as the whole list. `sage.drafted` joins this
    same episode whenever a MODEL actually produced the alert sentence
    (bot_service gates it on `model_drafted`, so the static degraded-mode
    fallback is correctly excluded). Whether that happens depends on the state
    of the AI lane, which varies between a solo run and a full suite — so an
    exact-list assertion was really asserting "Sage was down", and it went red
    on 2026-08-15 for the entirely correct reason that Sage was up. What this
    test is about is the CHAIN: the three required rows, in causal order, on one
    episode and one lane. Extra members of the loop belong to it and must not
    break it; a MISSING or REORDERED required row still fails.
    """
    nurse, patient = _mk_nurse(), _mk_patient()
    with TestClient(_app()) as client:
        _chart(client, nurse, patient, PERI_ARREST)

    rows = _rows(patient)
    kinds = [r["kind"] for r in rows]
    required = ["episode.opened", "alert.fired", "alert.persisted"]
    positions = [kinds.index(k) for k in required if k in kinds]
    assert len(positions) == len(required), f"a required row is missing: {kinds}"
    assert positions == sorted(positions), f"the chain is out of causal order: {kinds}"
    assert set(kinds) <= set(required) | {"sage.drafted"}, f"unexpected row: {kinds}"
    assert len({r["episode_id"] for r in rows}) == 1
    assert len({r["lane"] for r in rows}) == 1


# ── helpers for the simulator-lane tests ─────────────────────────────────────
class _NullSio:
    """The socket layer, silenced. `evaluate_and_dispatch_alerts` awaits one emit
    per delivered alert; nothing in this file asserts on sockets."""

    async def emit(self, *a, **kw):
        return None


def _fake_recommendation(text: str):
    async def _gen(vitals_summary, risk_level, drivers):
        return text
    return _gen


def _silence_bus(monkeypatch) -> list[dict]:
    """Record `publish_vigil_critical` calls instead of publishing them.

    The real publish schedules a task on the running loop; pytest-asyncio closes
    that loop when the test returns, so a live publish leaves a destroyed-pending
    task and a half-written event_log row racing the fixture teardown. The
    manual-lane tests above assert the real thing through a portal that stays
    open; these tests are about the shadow rows.
    """
    from services import bot_service

    calls: list[dict] = []

    def _record(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return True

    monkeypatch.setattr(bot_service, "publish_vigil_critical", _record)
    return calls


def _sim_payload(patient_id: str) -> dict:
    """One vitals tick as the simulator hands it to the dispatcher: NEWS2 19,
    critical — the only band that asks Sage for anything."""
    return {
        "patient_id": patient_id,
        "heart_rate": PERI_ARREST["heart_rate"],
        "systolic_bp": PERI_ARREST["systolic_bp"],
        "diastolic_bp": PERI_ARREST["diastolic_bp"],
        "spo2": PERI_ARREST["spo2"],
        "temperature": PERI_ARREST["temperature"],
        "respiratory_rate": PERI_ARREST["respiratory_rate"],
        "risk": _news2(PERI_ARREST),
    }
