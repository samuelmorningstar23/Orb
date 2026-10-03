"""On a decision-cache hit the VITALS line and the NEWS2 come from ONE observation (F032).

The cache-hit path recomputed only the risk block, so the prompt carried the
preloader's VITALS line (up to 10 minutes old) beside a NEWS2 and per-parameter
breakdown scored on a newer observation: "HR:104" two lines above "Moderate
tachycardia (HR 131)", two readings presented as one fact in the same answer.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import services.clinical_knowledge as ck
from database import db_session
from services.sage_service import get_relevant_context

DEPT = "Cache Coherence Medicine"
QUERY = "When starting empiric antibiotics in sepsis, how often must lactate be remeasured?"
STALE_LINE = "HR:104 BP:98/60 SpO2:93% Temp:38.4°C RR:24"


@pytest.fixture(autouse=True)
def _no_live_retrieval(monkeypatch):
    monkeypatch.setattr(ck, "_cross_encoder", None)
    monkeypatch.setattr(ck, "_cross_encoder_checked", True)
    monkeypatch.setattr(ck, "search_guidelines", lambda *a, **k: [])


def _setup():
    uid = f"CCU{uuid.uuid4().hex[:8].upper()}"
    pid = f"CCP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Coherence Doctor', 'doctor', 'x', ?)", (uid, DEPT))
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status, admission_date) "
            "VALUES (?, ?, ?, 'CC-1', ?, 58, 'M', 'Pneumonia', 'admitted', ?)",
            (pid, f"Coherence Patient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8]}", DEPT,
             datetime.now(timezone.utc).isoformat()))
        db.execute("INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
                   "VALUES (?, ?, ?, 'CC-1', 'active', ?)", (pid, DEPT, DEPT, uid))
        # The observation charted AFTER the cache entry was compiled.
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp, "
            "spo2, temperature, respiratory_rate, consciousness, on_oxygen) "
            "VALUES (?, ?, 131, 98, 60, 93, 38.4, 26.4, 'alert', 0)",
            (pid, datetime.now(timezone.utc).isoformat()))
        now = datetime.now(timezone.utc)
        facts = {
            "patient_id": pid,
            "demographics": f"PATIENT: Coherence Patient, 58y/M, Bed CC-1, Ward {DEPT}",
            "vitals": [STALE_LINE], "vital_trends": "", "risk": None,
            "medications": [], "recent_labs": [], "recent_notes": [],
            "scribe_notes": "", "imaging": "",
            "completeness": {"has_vitals": True, "has_meds": False, "has_labs": False,
                             "has_imaging": False, "has_notes": False,
                             "has_risk_score": False, "missing": []},
        }
        db.execute(
            "INSERT OR REPLACE INTO sage_decision_cache "
            "(patient_id, mews_score, compiled_context, preloaded_at, expires_at) VALUES (?, 0, ?, ?, ?)",
            (pid, json.dumps({"channel_a_facts": facts, "channel_b_c_guidelines": "",
                              "sources": [], "confidence": "high"}),
             (now - timedelta(minutes=8)).isoformat(), (now + timedelta(minutes=2)).isoformat()))
    return {"id": uid, "role": "doctor", "department": DEPT, "name": "Coherence Doctor"}, pid


def test_a_cache_hit_quotes_the_vitals_the_news2_was_scored_on():
    user, pid = _setup()
    context, _scope, _src, _conf, _esc, pctx, _q = get_relevant_context(QUERY, user, patient_id=pid)

    assert pctx is not None, "the cached chart was not served"
    assert STALE_LINE not in context, "the cached VITALS line survived beside a fresh NEWS2"
    assert pctx["vitals"][0].startswith("HR:131 "), pctx["vitals"]
    # AS CHARTED: RR 26.4 is charted and scored as 26, on both lines.
    assert "RR:26" in pctx["vitals"][0]
    by_param = {b["parameter"]: b["value"] for b in pctx["risk"]["news2_breakdown"]}
    assert by_param["Heart rate"] == 131
    assert by_param["Respiratory rate"] == 26
    assert "VITALS (latest): HR:131" in context
