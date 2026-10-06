"""The Orb Ward Record's clinical switches (6 October 2026).

ORB_SCORING_ENABLED=false: an observation set is stored and nothing else. No
risk_scores row, no NEWS2 or schedule in the response, no escalation, no Relay
alert, no sepsis bundle, the score preview is 404 and the ward board sorts by
ward then bed. ORB_SEPSIS_AUTOSTART=false: no lane opens a bundle by itself.
Both on: what happens today.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from routes.nurse_routes import router as nurse_router

DEPT = "Ward Record Switch Medicine"

# NEWS2 12: RR 26 (3), SpO2 90 (3), SBP 95 (2), HR 125 (2), temp 39.2 (2).
_NEWS2_12 = {"heart_rate": 125, "systolic_bp": 95, "diastolic_bp": 60,
             "respiratory_rate": 26, "temperature": 39.2, "spo2": 90,
             "consciousness": "alert", "on_oxygen": False}


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nurse_router)
    return TestClient(app)


@pytest.fixture()
def spies(monkeypatch):
    """Count every post-commit hook the write path fires."""
    from services import escalation_service, nurse_shift_service, sepsis_bundle_service
    calls = {"relay": 0, "escalation": 0, "bundle": 0, "push": 0}

    def _relay(*a, **k):
        calls["relay"] += 1
        return False

    def _esc(*a, **k):
        calls["escalation"] += 1

    real_start = sepsis_bundle_service.start_bundle

    def _start(*a, **k):
        calls["bundle"] += 1
        return real_start(*a, **k)

    def _push(*a, **k):
        calls["push"] += 1
        return False

    monkeypatch.setattr(nurse_shift_service, "dispatch_observation_alert", _relay)
    monkeypatch.setattr(escalation_service, "evaluate_observation", _esc)
    monkeypatch.setattr(sepsis_bundle_service, "start_bundle", _start)
    monkeypatch.setattr(nurse_shift_service, "_push_vitals_update", _push)
    return calls


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(role):
    uid = f"WRS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Ward Record Switch {role}", role, DEPT),
        )
    return uid


def _mk_patient(ward="WS-1", bed="B-1", dept=DEPT):
    pid = f"WRSP{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WRSADM', 'Ward Record Switch Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, ?, ?, 64, 'M', 'Ward record switch fixture',
                       'admitted', ?)""",
            (pid, f"WRS {bed}", f"MRN-{uuid.uuid4().hex[:8].upper()}", bed, ward, now),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, ?, 'active', 'WRSADM')",
            (pid, dept, ward, bed),
        )
    return pid


def _counts(pid):
    with db_session() as db:
        def n(sql):
            return db.execute(sql, (pid,)).fetchone()[0]
        return {
            "vitals": n("SELECT COUNT(*) FROM vitals WHERE patient_id = ?"),
            "risk_scores": n("SELECT COUNT(*) FROM risk_scores WHERE patient_id = ?"),
            "bundles": n("SELECT COUNT(*) FROM care_bundles WHERE patient_id = ?"),
            "escalations": n("SELECT COUNT(*) FROM clinical_escalations WHERE patient_id = ?"),
        }


# ── Scoring off ──────────────────────────────────────────────────────────────

def test_scoring_off_stores_the_set_and_nothing_else(client, spies, monkeypatch):
    monkeypatch.setattr(config, "SCORING_ENABLED", False)
    pid, nurse = _mk_patient(), _mk_user("nurse")
    r = client.post(f"/api/nurse/patients/{pid}/observations", json=_NEWS2_12,
                    headers=_auth(nurse, "nurse"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["news2"] is None and body["schedule"] is None
    assert _counts(pid) == {"vitals": 1, "risk_scores": 0, "bundles": 0, "escalations": 0}
    assert spies == {"relay": 0, "escalation": 0, "bundle": 0, "push": 0}
    with db_session() as db:
        audit = db.execute(
            "SELECT metadata_json, summary_message FROM audit_logs WHERE action_type = "
            "'observation_recorded' AND target_id = ?", (str(body["vitals_id"]),)).fetchone()
    import json
    meta = json.loads(audit["metadata_json"])
    assert meta["news2"] is None and meta["level"] is None
    assert meta["scoring_enabled"] is False
    assert "NEWS2" not in audit["summary_message"]


def test_scoring_off_the_preview_is_404(client, monkeypatch):
    monkeypatch.setattr(config, "SCORING_ENABLED", False)
    pid, nurse = _mk_patient(), _mk_user("nurse")
    r = client.post(f"/api/nurse/patients/{pid}/news2-preview", json=_NEWS2_12,
                    headers=_auth(nurse, "nurse"))
    assert r.status_code == 404
    assert r.json()["detail"] == "Scoring is switched off on this computer"


def test_scoring_off_the_board_is_ward_then_bed_with_no_score(client, monkeypatch):
    from services import vigil_service
    dept = f"Ward Record Board {uuid.uuid4().hex[:6]}"
    later = _mk_patient(ward="WB-2", bed="B-1", dept=dept)
    bed10 = _mk_patient(ward="WB-1", bed="B-10", dept=dept)
    bed2 = _mk_patient(ward="WB-1", bed="B-2", dept=dept)
    nurse = _mk_user("nurse")
    with db_session() as db:
        db.execute("UPDATE users SET department = ? WHERE id = ?", (dept, nurse))
    # bed10 is the sickest; with scoring on it would sort first.
    client.post(f"/api/nurse/patients/{bed10}/observations", json=_NEWS2_12,
                headers=_auth(nurse, "nurse"))
    monkeypatch.setattr(config, "SCORING_ENABLED", False)
    scored = vigil_service.scored_admitted_patients(dept)
    assert [p["id"] for p, _v, _n in scored] == [bed2, bed10, later]
    assert all(n is None for _p, _v, n in scored)


def test_health_publishes_the_clinical_switches(monkeypatch):
    import main
    monkeypatch.setattr(config, "SCORING_ENABLED", False)
    monkeypatch.setattr(config, "LENS_ENABLED", False)
    monkeypatch.setattr(config, "SEPSIS_AUTOSTART", True)
    monkeypatch.setattr(config, "MODELS_ENABLED", True)
    monkeypatch.setattr(config, "WARD_RECORD", False)
    assert main.clinical_switches() == {
        "scoring_enabled": False, "lens_enabled": False, "sepsis_autostart": True,
        "models_enabled": True, "ward_record": False}


# ── Autostart off ────────────────────────────────────────────────────────────

def test_autostart_off_a_news2_12_opens_no_bundle_but_still_alerts(client, spies, monkeypatch):
    monkeypatch.setattr(config, "SEPSIS_AUTOSTART", False)
    pid, nurse = _mk_patient(), _mk_user("nurse")
    r = client.post(f"/api/nurse/patients/{pid}/observations", json=_NEWS2_12,
                    headers=_auth(nurse, "nurse"))
    assert r.status_code == 200
    assert r.json()["news2"]["raw_news2"] == 12
    assert _counts(pid)["bundles"] == 0
    assert spies["bundle"] == 0
    assert spies["relay"] == 1 and spies["escalation"] == 1


def test_autostart_off_maybe_autostart_returns_none():
    from services import sepsis_bundle_service
    pid = _mk_patient()
    import pytest as _pt
    with _pt.MonkeyPatch.context() as mp:
        mp.setattr(config, "SEPSIS_AUTOSTART", False)
        assert sepsis_bundle_service.maybe_autostart(pid, {"raw_news2": 12}) is None
    assert _counts(pid)["bundles"] == 0


async def test_autostart_off_the_critical_event_fallback_opens_no_bundle(monkeypatch):
    from services import nervous_system_triggers
    from services.event_bus import StateEvent

    async def _no_warm(*a, **k):
        return None

    monkeypatch.setattr(nervous_system_triggers, "warm_patient_context", _no_warm)
    monkeypatch.setattr(config, "SEPSIS_AUTOSTART", False)
    pid = _mk_patient()
    # No raw_news2: the branch that used to call start_bundle unconditionally.
    evt = StateEvent("vigil.alert.critical", "vigil", "patient_deteriorated", pid,
                     {"level": "critical"})
    await nervous_system_triggers.on_critical_vigil_alert(evt)
    assert _counts(pid)["bundles"] == 0


def test_autostart_off_a_clinician_can_still_start_a_bundle(monkeypatch):
    from services import sepsis_bundle_service
    monkeypatch.setattr(config, "SEPSIS_AUTOSTART", False)
    pid = _mk_patient()
    bundle = sepsis_bundle_service.start_bundle(
        pid, triggered_by="WRSADM", trigger_reason="clinician judgement", source="manual")
    assert bundle and _counts(pid)["bundles"] == 1


# ── Both on: unchanged ───────────────────────────────────────────────────────

def test_both_on_a_news2_12_scores_alerts_escalates_and_opens_a_bundle(client, spies):
    assert config.SCORING_ENABLED and config.SEPSIS_AUTOSTART
    pid, nurse = _mk_patient(), _mk_user("nurse")
    r = client.post(f"/api/nurse/patients/{pid}/observations", json=_NEWS2_12,
                    headers=_auth(nurse, "nurse"))
    assert r.status_code == 200
    assert r.json()["news2"]["raw_news2"] == 12
    assert r.json()["schedule"] is not None
    c = _counts(pid)
    assert c["risk_scores"] == 1 and c["bundles"] == 1
    assert spies["relay"] == 1 and spies["escalation"] == 1 and spies["bundle"] == 1
