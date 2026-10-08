"""Two guards on a set the ward clerk copies from the paper sheet
(Orb Ward Record, 9 October 2026).

(a) A sheet time before this stay began is refused with "The time on the
    sheet is before this patient was admitted". Up to 60 minutes before the
    admission time is accepted, for a transfer sheet whose last round was taken
    where the patient came from.

(b) With scoring on, a copied set whose sheet time is more than
    ORB_TRANSCRIBED_LIVE_HOOK_MAX_AGE_MINUTES (default 120) before it was keyed
    fires no live hook: no Relay alert, no escalation chain (from the write
    path or the background sweep), no sepsis bundle, and no vitals:update
    unless it is the chart's newest set. It is charted and scored for the
    record, and the response says "Charted. Older than 2 hours, so no alert
    was raised."
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from routes.nurse_routes import router as nurse_router
from services import nurse_shift_service

DEPT = "Transcribed Guard Medicine"

# NEWS2 12: RR 26 (3), SpO2 90 (3), SBP 95 (2), HR 125 (2), temp 39.2 (2).
_NEWS2_12 = {"heart_rate": 125, "systolic_bp": 95, "diastolic_bp": 60,
             "respiratory_rate": 26, "temperature": 39.2, "spo2": 90,
             "consciousness": "alert", "on_oxygen": False}
_CALM = {"heart_rate": 78, "systolic_bp": 124, "diastolic_bp": 76,
         "respiratory_rate": 16, "temperature": 37.0, "spo2": 97,
         "consciousness": "alert", "on_oxygen": False}


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nurse_router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def scoring_on(monkeypatch):
    monkeypatch.setattr(config, "SCORING_ENABLED", True)
    monkeypatch.setattr(config, "SEPSIS_AUTOSTART", True)
    monkeypatch.setattr(config, "TRANSCRIBED_LIVE_HOOK_MAX_AGE_MINUTES", 120)


@pytest.fixture()
def spies(monkeypatch):
    """Count every post-commit hook the write path fires."""
    from services import escalation_service, sepsis_bundle_service
    calls = {"relay": 0, "escalation": 0, "bundle": 0, "push": 0}

    def _relay(*a, **k):
        calls["relay"] += 1
        return False

    def _esc(*a, **k):
        calls["escalation"] += 1

    def _bundle(*a, **k):
        calls["bundle"] += 1

    def _push(*a, **k):
        calls["push"] += 1
        return False

    monkeypatch.setattr(nurse_shift_service, "dispatch_observation_alert", _relay)
    monkeypatch.setattr(escalation_service, "evaluate_observation", _esc)
    monkeypatch.setattr(sepsis_bundle_service, "maybe_autostart", _bundle)
    monkeypatch.setattr(nurse_shift_service, "_push_vitals_update", _push)
    return calls


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(role):
    uid = f"WTG{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Transcribed guard {role}", role, DEPT),
        )
    return uid


def _mk_patient(*, admitted: datetime, admitted_at_column: bool = False):
    """A patient whose admission row was written at `admitted`, or, with
    `admitted_at_column`, written now with admitted_at (the beds module's
    back-timed arrival) set to `admitted`."""
    pid = f"WTGP{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc)
    written = now if admitted_at_column else admitted
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WTGADM', 'Transcribed guard admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, 'Guard Fixture', ?, 'B-3', 'TG-1', 66, 'F', 'Fixture',
                       'admitted', ?)""",
            (pid, f"MRN-{pid}", written.isoformat()),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
            "admitted_by, created_at, admitted_at) "
            "VALUES (?, ?, ?, 'TG-1', 'B-3', 'active', 'WTGADM', ?, ?)",
            (f"ADM-{pid}", pid, DEPT, written.isoformat(),
             admitted.isoformat() if admitted_at_column else None),
        )
    return pid


def _post(client, pid, uid, role, body, at: datetime):
    return client.post(f"/api/nurse/patients/{pid}/observations",
                       json={**body, "taken_at": at.isoformat()}, headers=_auth(uid, role))


def _count(sql, pid):
    with db_session() as db:
        return db.execute(sql, (pid,)).fetchone()[0]


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).replace(microsecond=0)


# ── (a) a sheet time before the stay began ────────────────────────────────────
def test_a_sheet_time_more_than_an_hour_before_admission_is_refused(client, spies):
    admitted = _ago(hours=10)
    pid = _mk_patient(admitted=admitted)
    clerk = _mk_user("ward_clerk")
    r = _post(client, pid, clerk, "ward_clerk", _CALM, admitted - timedelta(minutes=61))
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "The time on the sheet is before this patient was admitted"
    assert _count("SELECT COUNT(*) FROM vitals WHERE patient_id = ?", pid) == 0


def test_a_transfer_sheet_up_to_an_hour_before_admission_is_accepted(client, spies):
    admitted = _ago(hours=10)
    pid = _mk_patient(admitted=admitted)
    clerk = _mk_user("ward_clerk")
    r = _post(client, pid, clerk, "ward_clerk", _CALM, admitted - timedelta(minutes=59))
    assert r.status_code == 200, r.text
    r = _post(client, pid, clerk, "ward_clerk", _CALM, admitted + timedelta(minutes=30))
    assert r.status_code == 200, r.text


def test_the_back_timed_arrival_is_the_admission_time_when_the_beds_module_set_one(client, spies):
    # Keyed now, but the patient arrived 8 hours ago: a sheet from 7 hours ago
    # is this stay's, and one from 10 hours ago is not.
    admitted = _ago(hours=8)
    pid = _mk_patient(admitted=admitted, admitted_at_column=True)
    clerk = _mk_user("ward_clerk")
    assert _post(client, pid, clerk, "ward_clerk", _CALM, _ago(hours=7)).status_code == 200
    late = _post(client, pid, clerk, "ward_clerk", _CALM, _ago(hours=10))
    assert late.status_code == 422
    assert late.json()["detail"] == "The time on the sheet is before this patient was admitted"


# ── (b) an old copied set raises nothing live ────────────────────────────────
def test_an_old_copied_set_is_charted_and_scored_but_raises_nothing(client, spies):
    pid = _mk_patient(admitted=_ago(days=2))
    clerk = _mk_user("ward_clerk")
    # A newer set is already on the chart, so the copied one is history.
    assert _post(client, pid, clerk, "ward_clerk", _CALM, _ago(minutes=20)).status_code == 200
    before = dict(spies)

    r = _post(client, pid, clerk, "ward_clerk", _NEWS2_12, _ago(hours=3))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"] == "Charted. Older than 2 hours, so no alert was raised."
    assert body["live_hooks"] is False
    # Charted and scored for the record.
    assert body["news2"]["raw_news2"] == 12 and body["news2"]["level"] == "critical"
    assert _count("SELECT COUNT(*) FROM risk_scores WHERE patient_id = ?", pid) == 2
    # Nothing live: no alert, no escalation, no bundle, and no push for a set
    # that is not the newest.
    assert spies["relay"] == before["relay"]
    assert spies["escalation"] == before["escalation"]
    assert spies["bundle"] == before["bundle"]
    assert spies["push"] == before["push"]


def test_an_old_copied_set_that_is_the_newest_still_reaches_an_open_chart(client, spies):
    pid = _mk_patient(admitted=_ago(days=2))
    clerk = _mk_user("ward_clerk")
    r = _post(client, pid, clerk, "ward_clerk", _NEWS2_12, _ago(hours=3))
    assert r.status_code == 200, r.text
    assert r.json()["live_hooks"] is False
    assert spies["push"] == 1
    assert (spies["relay"], spies["escalation"], spies["bundle"]) == (0, 0, 0)


def test_a_recent_copied_set_still_raises_the_alert(client, spies):
    pid = _mk_patient(admitted=_ago(days=2))
    clerk = _mk_user("ward_clerk")
    r = _post(client, pid, clerk, "ward_clerk", _NEWS2_12, _ago(minutes=90))
    assert r.status_code == 200, r.text
    assert "message" not in r.json()
    assert (spies["relay"], spies["escalation"], spies["bundle"], spies["push"]) == (1, 1, 1, 1)


def test_a_bedside_set_is_never_held_back_by_its_age(client, spies):
    pid = _mk_patient(admitted=_ago(days=2))
    nurse = _mk_user("nurse")
    r = _post(client, pid, nurse, "nurse", _NEWS2_12, _ago(hours=3))
    assert r.status_code == 200, r.text
    assert "message" not in r.json()
    assert spies["relay"] == 1 and spies["escalation"] == 1


def test_the_limit_is_read_from_config_and_worded_from_it(client, spies, monkeypatch):
    monkeypatch.setattr(config, "TRANSCRIBED_LIVE_HOOK_MAX_AGE_MINUTES", 30)
    pid = _mk_patient(admitted=_ago(days=2))
    clerk = _mk_user("ward_clerk")
    r = _post(client, pid, clerk, "ward_clerk", _NEWS2_12, _ago(minutes=45))
    assert r.json()["message"] == "Charted. Older than 30 minutes, so no alert was raised."
    assert spies["relay"] == 0


def test_the_escalation_sweep_raises_no_chain_for_an_old_copied_set(client, monkeypatch):
    """The background safety net must not raise what the write path held back,
    even when its own look back window reaches the sheet time."""
    from services import escalation_service

    monkeypatch.setenv("ORB_ESCALATION_SCAN_LOOKBACK_MINUTES", "600")
    monkeypatch.setattr(nurse_shift_service, "dispatch_observation_alert", lambda *a, **k: False)
    pid = _mk_patient(admitted=_ago(days=2))
    clerk = _mk_user("ward_clerk")
    r = _post(client, pid, clerk, "ward_clerk", _NEWS2_12, _ago(hours=3))
    assert r.status_code == 200 and r.json()["live_hooks"] is False

    raised = escalation_service.scan_new_observations(department=DEPT)
    assert pid not in {c.get("patient_id") for c in raised}
    assert _count("SELECT COUNT(*) FROM clinical_escalations WHERE patient_id = ?", pid) == 0

    # Control: with a limit longer than the set's age the sweep would raise it,
    # so the exclusion above is what held it back.
    monkeypatch.setattr(config, "TRANSCRIBED_LIVE_HOOK_MAX_AGE_MINUTES", 100000)
    escalation_service.scan_new_observations(department=DEPT)
    assert _count("SELECT COUNT(*) FROM clinical_escalations WHERE patient_id = ?", pid) == 1
