"""Scoring switched off everywhere (the Orb Ward Record profile, 6 October 2026).

With ORB_SCORING_ENABLED off no screen and no API computes or returns a NEWS2
score, band, response line or a schedule worked out from one. The guard in
vigil_service makes every scoring entry point refuse with ScoringSwitchedOff,
and each lane below answers its no-score shape instead. A control beside the
main cases proves the same rows are still scored with the switch on.
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import vigil_service
from services.clinical_time import format_utc

client = TestClient(app)

DEPT = "Scoring Off Medicine"
_CRITICAL = dict(heart_rate=135, systolic_bp=85, diastolic_bp=50, spo2=88,
                 temperature=39.5, respiratory_rate=30)
MARKER = "SCORING OFF STORED BAND"


def _scoring(monkeypatch, on: bool):
    monkeypatch.setattr(config, "SCORING_ENABLED", on, raising=False)


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _user(role):
    uid = f"SOF{role[:3].upper()}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)", (uid, f"Scoring Off {role}", role, DEPT))
    return uid


def _ago(minutes):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _patient(ward, *, admitted_minutes_ago=600, vitals_minutes_ago=(30,), stored_band=True):
    """An admitted patient on `ward` with critical vitals and a stored band."""
    pid = f"SOFP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('SOFADM', 'Scoring Off Admitter', 'doctor', 'x')")
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status,
                                     admission_date)
               VALUES (?, ?, ?, 'B-1', ?, 60, 'F', 'Pneumonia', 'admitted', ?)""",
            (pid, f"Scoring Off {pid[-4:]}", f"MRN-{pid}", ward,
             format_utc(_ago(admitted_minutes_ago))))
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, admitted_by, created_at) "
            "VALUES (?, ?, ?, ?, 'B-1', 'active', 'SOFADM', ?)",
            (f"ADM-{pid}", pid, DEPT, ward, format_utc(_ago(admitted_minutes_ago))))
        for minutes in vitals_minutes_ago:
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                       spo2, temperature, respiratory_rate, consciousness, on_oxygen, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'alert', 0, 'nurse_manual')""",
                (pid, format_utc(_ago(minutes)), _CRITICAL["heart_rate"],
                 _CRITICAL["systolic_bp"], _CRITICAL["diastolic_bp"], _CRITICAL["spo2"],
                 _CRITICAL["temperature"], _CRITICAL["respiratory_rate"]))
        if stored_band:
            db.execute("INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
                       "VALUES (?, ?, 0.9, 'critical', ?)",
                       (pid, format_utc(_ago(30)), json.dumps([MARKER])))
    return pid


def _interval(ward, minutes=240, grace=30):
    with db_session() as db:
        db.execute("INSERT INTO ward_observation_settings (ward, interval_minutes, grace_minutes, "
                   "set_by, set_at) VALUES (?, ?, ?, 'SOFADM', ?)",
                   (ward, minutes, grace, format_utc(_ago(60 * 24))))


@pytest.fixture(autouse=True)
def _clean_calls():
    vigil_service.SWITCHED_OFF_CALLS.clear()
    yield
    vigil_service.SWITCHED_OFF_CALLS.clear()


# ── The guard ────────────────────────────────────────────────────────────────

def test_every_scoring_entry_point_refuses_with_scoring_off(monkeypatch):
    _scoring(monkeypatch, False)
    row = {"heart_rate": 135, "systolic_bp": 85, "respiratory_rate": 30,
           "temperature": 39.5, "spo2": 88}
    with pytest.raises(vigil_service.ScoringSwitchedOff):
        vigil_service.compute_news2(135, 85, 30, 39.5, 88)
    with pytest.raises(vigil_service.ScoringSwitchedOff):
        vigil_service.score_vitals_row(row, on_scale_2=False, overlay=False)
    with pytest.raises(vigil_service.ScoringSwitchedOff):
        vigil_service.risk_for_row(None, patient_id="X", on_scale_2=False)
    assert len(vigil_service.SWITCHED_OFF_CALLS) == 3


def test_the_ward_record_profile_switches_scoring_off_on_its_own(monkeypatch):
    _scoring(monkeypatch, True)
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    assert vigil_service.scoring_enabled() is False
    with pytest.raises(vigil_service.ScoringSwitchedOff):
        vigil_service.compute_news2(135, 85, 30, 39.5, 88)


def test_the_entry_points_score_as_before_with_scoring_on(monkeypatch):
    _scoring(monkeypatch, True)
    assert vigil_service.compute_news2(135, 85, 30, 39.5, 88)["raw_news2"] >= 7
    assert vigil_service.SWITCHED_OFF_CALLS == []


# ── The Vigil chart ──────────────────────────────────────────────────────────

def test_the_chart_sends_no_risk_and_says_why(monkeypatch):
    _scoring(monkeypatch, False)
    pid = _patient("SOF-CHART")
    doctor = _user("doctor")
    r = client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor, "doctor"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["risk"] is None
    assert body["risk_off_reason"] == vigil_service.SCORING_OFF_REASON
    assert body["obs_schedule"] is None and body["risk_history"] == []
    assert body["vitals"], "the readings themselves are still sent"
    assert MARKER not in r.text and vigil_service.SWITCHED_OFF_CALLS == []


def test_the_chart_is_scored_with_scoring_on(monkeypatch):
    _scoring(monkeypatch, True)
    pid = _patient("SOF-CHART")
    doctor = _user("doctor")
    body = client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor, "doctor")).json()
    assert body["risk"]["level"] == "critical" and body["risk_off_reason"] is None
    assert body["obs_schedule"]["band"]


# ── The nurse shift board ────────────────────────────────────────────────────

def _board_card(nurse, pid):
    r = client.get("/api/nurse/board", headers=_auth(nurse, "nurse"))
    assert r.status_code == 200, r.text
    body = r.json()
    return body, next(c for c in body["patients"] if c["patient_id"] == pid)


def test_the_board_shows_the_due_time_from_the_ward_interval(monkeypatch):
    _scoring(monkeypatch, False)
    ward = f"SOF-W{uuid.uuid4().hex[:4]}"
    _interval(ward, minutes=240)
    # Arrived 10 hours ago: rounds due at 4 h and 8 h. A set 130 minutes ago
    # (10 minutes before the 8 h round, inside its on time window) does the
    # 8 h round, by the rounds file's rule, so the next is due in 2 hours.
    pid = _patient(ward, admitted_minutes_ago=600, vitals_minutes_ago=(130,))
    body, card = _board_card(_user("nurse"), pid)
    assert card["news2"] is None and card["obs_band"] is None and card["obs_response"] is None
    assert card["obs_interval_minutes"] == 240 and card["obs_due_source"] == "ward_interval"
    assert 110 <= card["obs_minutes_until_due"] <= 121
    assert card["obs_overdue"] is False and card["obs_due_note"] is None
    assert body["totals"]["critical"] is None and body["totals"]["elevated"] is None
    assert body["scoring_enabled"] is False


def test_a_round_past_its_grace_window_is_late_on_the_board(monkeypatch):
    _scoring(monkeypatch, False)
    ward = f"SOF-W{uuid.uuid4().hex[:4]}"
    _interval(ward, minutes=240, grace=30)
    # Arrived 10 hours ago, the 4 h round done, nothing since: the 8 h round
    # was due 2 hours ago and is past its 30 minute grace.
    pid = _patient(ward, admitted_minutes_ago=600, vitals_minutes_ago=(360,))
    _body, card = _board_card(_user("nurse"), pid)
    assert card["obs_overdue"] is True
    assert -121 <= card["obs_minutes_until_due"] <= -119


def test_a_ward_with_no_interval_shows_no_due_time(monkeypatch):
    _scoring(monkeypatch, False)
    pid = _patient(f"SOF-NOSET{uuid.uuid4().hex[:4]}")
    _body, card = _board_card(_user("nurse"), pid)
    assert card["obs_due_at"] is None and card["obs_minutes_until_due"] is None
    assert card["obs_due_note"] == "Set the ward's observation interval"
    assert card["obs_overdue"] is False and card["obs_interval_minutes"] is None


def test_the_board_scores_with_scoring_on(monkeypatch):
    _scoring(monkeypatch, True)
    pid = _patient(f"SOF-ON{uuid.uuid4().hex[:4]}")
    body, card = _board_card(_user("nurse"), pid)
    assert card["news2"]["level"] == "critical" and card["obs_due_source"] == "news2"
    assert body["totals"]["critical"] >= 1


# ── I-PASS ───────────────────────────────────────────────────────────────────

def test_ipass_with_scoring_off_drafts_no_severity_and_no_news2_contingency(monkeypatch):
    _scoring(monkeypatch, False)
    from services.ipass_service import build_ipass
    pid = _patient("SOF-IPASS")
    doc = build_ipass(pid, include_assessment=False)
    sev = doc["sections"]["illness_severity"]
    assert sev["severity"] is None and sev["news2"] is None and sev["text"] is None
    assert doc["sections"]["situation_awareness"]["contingencies"] == []
    assert "ILLNESS SEVERITY" not in doc["plain_text"]
    assert "NEWS2" not in doc["plain_text"] and "Contingency planning" not in doc["plain_text"]
    assert "Illness severity" not in doc["source_summary"]
    assert vigil_service.SWITCHED_OFF_CALLS == []


def test_ipass_with_scoring_on_still_drafts_the_severity(monkeypatch):
    _scoring(monkeypatch, True)
    from services.ipass_service import build_ipass
    doc = build_ipass(_patient("SOF-IPASS"), include_assessment=False)
    assert doc["sections"]["illness_severity"]["severity"]
    assert "ILLNESS SEVERITY" in doc["plain_text"]


def test_under_the_profile_every_ipass_route_is_switched_off(monkeypatch):
    _scoring(monkeypatch, False)
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    pid = _patient("SOF-IPASS")
    doctor = _user("doctor")
    for path in (f"/api/ipass/{pid}/draft", f"/api/ipass/{pid}/recent",
                 f"/api/ipass/{pid}/reasoned-assessment"):
        r = client.get(path, headers=_auth(doctor, "doctor"))
        assert r.status_code == 404, (path, r.text)
        assert r.json()["detail"] == "The handover is switched off on this computer"
    r = client.post(f"/api/ipass/{pid}/save", json={}, headers=_auth(doctor, "doctor"))
    assert r.status_code == 404


# ── The downtime pack ────────────────────────────────────────────────────────

def test_the_downtime_snapshot_and_pack_carry_no_news2(monkeypatch):
    from services import downtime_service
    _scoring(monkeypatch, True)
    ward = f"SOF-DT{uuid.uuid4().hex[:4]}"
    pid = _patient(ward)
    downtime_service.capture_snapshot(pid)          # an old snapshot, scored
    _scoring(monkeypatch, False)
    old = downtime_service.get_latest_snapshot(pid)
    assert old["snapshot"]["news2"] is None and old["snapshot"]["recent_risk_scores"] == []
    assert "NEWS2" not in old["summary"]
    env = downtime_service.capture_snapshot(pid)
    snap = env.get("snapshot", env)
    assert snap["news2"] is None and snap["recent_risk_scores"] == []
    assert snap["latest_vitals"] is not None
    html = downtime_service._render_ward_pack(
        ward, [{"id": pid, "name": "x", "mrn": "m", "bed": "B-1",
                "envelope": downtime_service.get_latest_snapshot(pid)}],
        format_utc(_ago(0)), names={})
    assert "NEWS2" not in html and MARKER not in html
    assert vigil_service.SWITCHED_OFF_CALLS == []


# ── The simulator, the feeds and the Scribe push ─────────────────────────────

def test_the_simulator_stores_a_reading_and_scores_nothing(monkeypatch):
    from services import bot_service
    _scoring(monkeypatch, False)
    pid = _patient("SOF-SIM", stored_band=False)
    with db_session() as db:
        latest = dict(db.execute("SELECT * FROM vitals WHERE patient_id = ?", (pid,)).fetchone())
        row, emit = bot_service._next_reading(db, pid, latest, format_utc(_ago(0)), on_scale_2=False)
    assert emit["risk"] is None and row[0] == pid
    sent = []
    monkeypatch.setattr(bot_service, "_persist_alerts", lambda *a, **k: sent.append(a))
    asyncio.run(bot_service.evaluate_and_dispatch_alerts(None, [emit]))
    assert sent == [] and vigil_service.SWITCHED_OFF_CALLS == []


def test_the_ingestion_feed_stores_the_reading_and_scores_nothing(monkeypatch):
    from services import ingestion_service
    _scoring(monkeypatch, False)
    pid = _patient("SOF-FEED", vitals_minutes_ago=(), stored_band=False)
    payload = json.dumps({
        "patient_id": pid, "message_id": f"sof-{uuid.uuid4().hex}",
        "observations": [
            {"code": "8867-4", "name": "Heart Rate", "value": 138, "unit": "bpm"},
            {"code": "2708-6", "name": "Oxygen Saturation", "value": 84, "unit": "%"},
            {"code": "9279-1", "name": "Respiratory Rate", "value": 30, "unit": "/min"},
        ],
    })
    res = asyncio.run(ingestion_service.ingest_payload(payload, source_name="sof-feed"))
    assert not res.errors and res.inserted_vitals == 1
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM risk_scores WHERE patient_id = ?", (pid,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM clinical_escalations WHERE patient_id = ?", (pid,)).fetchone()[0] == 0
    assert vigil_service.SWITCHED_OFF_CALLS == []


def test_the_scribe_extraction_push_carries_no_risk(monkeypatch):
    from services.nervous_system_triggers import _live_vitals_payload
    _scoring(monkeypatch, False)
    pid = _patient("SOF-SCRIBE", stored_band=False)
    with db_session() as db:
        vid = db.execute("SELECT id FROM vitals WHERE patient_id = ?", (pid,)).fetchone()["id"]
        payload = _live_vitals_payload(db, pid, vid)
    assert payload["risk"] is None and payload["heart_rate"] == 135


# ── Sage's insights feed and ward brief ──────────────────────────────────────

def test_the_insights_feed_raises_and_shows_no_deterioration_card(monkeypatch):
    from services import sage_proactive_service as sp
    _scoring(monkeypatch, True)
    pid = _patient("SOF-INS", stored_band=False)
    sp.evaluate_patient(pid)               # scoring on: a critical card
    with db_session() as db:
        on = db.execute("SELECT COUNT(*) FROM sage_insights WHERE patient_id = ? AND "
                        "insight_type = 'deterioration' AND state = 'active'", (pid,)).fetchone()[0]
    assert on == 1
    _scoring(monkeypatch, False)
    sp.evaluate_patient(pid)
    shown = [i for i in sp.get_active_insights("SOFADM", "admin") if i["patient_id"] == pid]
    assert not any(i["insight_type"] == "deterioration" for i in shown)
    assert vigil_service.SWITCHED_OFF_CALLS == []


def test_the_ward_brief_has_no_risk_mix_and_no_watchlist(monkeypatch):
    from services.sage_narrator_service import ward_narrator
    _scoring(monkeypatch, False)
    _patient("SOF-BRIEF")
    snap = ward_narrator._build_ward_snapshot()
    assert snap["risk_distribution"] is None and snap["watchlist"] == []
    assert snap["scoring_enabled"] is False
    assert asyncio.run(ward_narrator._narrate(snap)) is None


# ── Stored bands on other screens ────────────────────────────────────────────

def test_the_chart_timeline_and_story_leave_out_the_stored_band(monkeypatch):
    pid = _patient("SOF-TL")
    doctor = _user("doctor")
    _scoring(monkeypatch, True)
    on = client.get(f"/api/chart/patients/{pid}/timeline", headers=_auth(doctor, "doctor"))
    assert on.status_code == 200 and MARKER in on.text
    _scoring(monkeypatch, False)
    for path in (f"/api/chart/patients/{pid}/timeline", f"/api/vigil/patients/{pid}/story"):
        r = client.get(path, headers=_auth(doctor, "doctor"))
        assert r.status_code == 200, r.text
        assert MARKER not in r.text, path


def test_patient_search_carries_no_stored_band(monkeypatch):
    _scoring(monkeypatch, False)
    pid = _patient("SOF-SEARCH")
    doctor = _user("doctor")
    r = client.get(f"/api/search/patients?q={pid}", headers=_auth(doctor, "doctor"))
    assert r.status_code == 200, r.text
    hits = [p for p in r.json() if p["id"] == pid]
    assert hits and hits[0]["risk_level"] is None
    assert "critical" not in r.text


def test_the_alert_assessment_routes_are_switched_off_with_scoring_off(monkeypatch):
    _scoring(monkeypatch, False)
    doctor = _user("doctor")
    r = client.get("/api/relay/sage_assessments/nothing", headers=_auth(doctor, "doctor"))
    assert r.status_code == 404 and r.json()["detail"] == "Switched off on this computer"


# ── Review, 7 October 2026: a chain opened before the switch ─────────────────
# advance_due climbed open chains on the clock with scoring off, so a chain
# opened while scoring was on kept paging the next tier, with its stored NEWS2
# in the Relay post, after the computer was switched to the Ward Record.

def test_an_open_chain_does_not_climb_with_scoring_off(monkeypatch):
    from services import escalation_service as esc
    _scoring(monkeypatch, True)
    pid = _patient(f"SOF Chain {uuid.uuid4().hex[:6]}", stored_band=False)
    t0 = datetime.now(timezone.utc)
    news2 = vigil_service.compute_news2(
        _CRITICAL["heart_rate"], _CRITICAL["systolic_bp"], _CRITICAL["respiratory_rate"],
        _CRITICAL["temperature"], _CRITICAL["spo2"], "alert", False)
    chain = esc.evaluate_observation(pid, news2, now=t0, notify=False)
    assert chain is not None
    tier = esc.get_escalation(chain["id"])["current_tier"]

    _scoring(monkeypatch, False)
    assert esc.advance_due(now=t0 + timedelta(days=1)) == []
    assert esc.get_escalation(chain["id"])["current_tier"] == tier

    # Control: the same chain climbs once scoring is back on.
    _scoring(monkeypatch, True)
    esc.advance_due(now=t0 + timedelta(days=1))
    assert esc.get_escalation(chain["id"])["current_tier"] > tier


def test_a_stored_chain_is_listed_without_its_score_with_scoring_off(monkeypatch):
    from services import escalation_service as esc
    _scoring(monkeypatch, True)
    pid = _patient(f"SOF Chain {uuid.uuid4().hex[:6]}", stored_band=False)
    news2 = vigil_service.compute_news2(
        _CRITICAL["heart_rate"], _CRITICAL["systolic_bp"], _CRITICAL["respiratory_rate"],
        _CRITICAL["temperature"], _CRITICAL["spo2"], "alert", False)
    chain = esc.evaluate_observation(pid, news2, notify=False)
    on = esc.get_escalation(chain["id"])
    assert on["peak_news2"] and on["drivers"]
    assert any(e["news2"] for e in on["events"])

    _scoring(monkeypatch, False)
    for got in (esc.get_escalation(chain["id"]), esc.patient_escalations(pid)[0]):
        assert got["id"] == chain["id"] and got["status"] == "open"
        assert got["news2"] is None and got["peak_news2"] is None
        assert got["level"] is None and got["drivers"] == [] and got["drivers_json"] is None
    events = esc.get_escalation(chain["id"])["events"]
    assert events and all(e["news2"] is None for e in events)
    assert all("NEWS2" not in (e["detail"] or "") for e in events)
    assert all("drivers" not in (e["metadata"] or {}) for e in events)


# ── Review, 7 October 2026: a bundle Orb opened on a score ──────────────────
# The chart's Sepsis Six card printed "Opened by Orb at NEWS2 15" and the
# stored criterion for a bundle auto-started before scoring was switched off.

def test_an_auto_started_bundle_shows_no_score_with_scoring_off(monkeypatch):
    pid = _patient(f"SOF Bundle {uuid.uuid4().hex[:6]}", stored_band=False)
    with db_session() as db:
        db.execute("INSERT INTO care_bundles (patient_id, bundle_type, triggered_at, "
                   "triggered_by, trigger_reason, status, source) VALUES (?, 'sepsis6', ?, "
                   "'vigil_auto', 'NEWS2 15 (>=7)', 'active', 'vigil_auto')",
                   (pid, datetime.now(timezone.utc).isoformat()))
    admin = _auth(_user("admin"), "admin")
    _scoring(monkeypatch, True)
    r = client.get(f"/api/bundles/patient/{pid}", headers=admin)
    assert r.status_code == 200, r.text
    assert "NEWS2 15" in r.text and r.json()["bundles"][0]["trigger_news2"] == 15
    _scoring(monkeypatch, False)
    r = client.get(f"/api/bundles/patient/{pid}", headers=admin)
    assert r.status_code == 200, r.text
    bundle = r.json()["bundles"][0]
    assert "NEWS2" not in r.text
    assert bundle["trigger_news2"] is None and bundle["trigger_reason"] is None


# ── Review, 7 October 2026: the handover's deterioration warning ────────────
# With scoring off the I-PASS draft still printed "ACTIVE DETERIORATION: watch
# closely." whenever the six hour vitals trend read as deteriorating.

def _deteriorating_patient():
    pid = _patient(f"SOF IPASS Trend {uuid.uuid4().hex[:6]}", stored_band=False)
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                   spo2, temperature, respiratory_rate, consciousness, on_oxygen, source)
               VALUES (?, ?, 72, 125, 80, 98, 36.8, 16, 'alert', 0, 'nurse_manual')""",
            (pid, format_utc(_ago(9 * 60))))
    return pid


def test_ipass_with_scoring_off_raises_no_deterioration_warning(monkeypatch):
    from services.ipass_service import build_ipass
    pid = _deteriorating_patient()
    _scoring(monkeypatch, True)
    on = build_ipass(pid, include_assessment=False)
    assert on["sections"]["situation_awareness"]["active_deterioration"] is True
    assert "ACTIVE DETERIORATION" in on["plain_text"]
    _scoring(monkeypatch, False)
    off = build_ipass(pid, include_assessment=False)
    # The trend alone still reads deteriorating, so only the fix keeps the line out.
    assert off["sections"]["situation_awareness"]["trend"] == "deteriorating"
    assert off["sections"]["situation_awareness"]["active_deterioration"] is False
    assert "ACTIVE DETERIORATION" not in off["plain_text"]


# ── Review, 7 October 2026: the discharge summary ───────────────────────────
# The discharge timeline (stored with the summary) and the draft's inputs read
# every stored critical band, so a discharge on a scoring-off computer printed
# "Critical Alert (NEWS2 band: critical)" with the stored drivers.

def test_the_discharge_timeline_and_draft_carry_no_stored_band_with_scoring_off(monkeypatch):
    from services import discharge_service
    pid = _patient(f"SOF Discharge {uuid.uuid4().hex[:6]}")
    _scoring(monkeypatch, True)
    assert MARKER in json.dumps(discharge_service.build_patient_timeline(pid))
    assert discharge_service._draft_inputs(pid)["risks"]
    _scoring(monkeypatch, False)
    timeline = json.dumps(discharge_service.build_patient_timeline(pid))
    assert MARKER not in timeline and "NEWS2" not in timeline
    assert list(discharge_service._draft_inputs(pid)["risks"]) == []


# ── Review, 9 October 2026: Sage's temporal state ───────────────────────────
# With only scoring switched off (the models on), Sage's per patient state read
# the stored band and answered "current_risk_level": "critical", and a state
# persisted before the switch kept answering it.

def test_the_temporal_state_names_no_stored_band_with_scoring_off(monkeypatch):
    from services import world_model_service
    pid = _patient("SOF-WORLD")
    doctor = _user("doctor")
    _scoring(monkeypatch, True)
    persisted = world_model_service.get_patient_state(pid)
    assert persisted["current_risk_level"] == "critical"
    _scoring(monkeypatch, False)
    state = world_model_service.get_patient_state(pid)
    assert state["current_risk_level"] == "unknown" and state["risk_level_history"] == []
    ward = world_model_service.get_ward_state()
    assert ward["risk_distribution"]["critical"] == 0
    r = client.get(f"/api/sage/world/patient/{pid}", headers=_auth(doctor, "doctor"))
    assert r.status_code == 200, r.text
    assert "critical" not in r.text
