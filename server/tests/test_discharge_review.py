"""The drafted discharge summary, its review gate, and the patient portal.

FOUND 2026-10-01. Every discharge summary was the raw 27B output, stored as
final and shown on the Bridge portal and the /discharge/<token> link. The model
was asked for "Discharge: [Today's date]" and "ATTENDING PHYSICIAN: [Doctor
name]" without being given either, so it invented the date and echoed the slot,
and the discharging doctor never saw the text at all.

The invariants pinned here:
  1. The prompt CARRIES the discharge date, the attending doctor, the charted
     allergies and the comorbidities, and an unfilled template slot never
     survives into the stored text.
  2. A summary is stored as a draft. No patient-facing reader returns its text
     until a prescriber on the patient's ward marks it reviewed, and that review
     is audited.
  3. The discharge response hands the doctor the draft and an API path for the
     QR, never a location on the appliance's disk.
  4. The Bridge care team never names a nurse nobody assigned, and the status
     line is scored from the same reading it prints, as charted.
"""
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app

client = TestClient(app)

DEPT = "Discharge Review Medicine"
OTHER_DEPT = "Discharge Review Surgery"


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_user(role, department=None, name=None):
    uid = f"DRV{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) VALUES (?, ?, ?, 'x', ?)",
            (uid, name or f"Discharge Review {role}", role, department),
        )
    return uid


def _mk_patient(*, attending=None, department=DEPT, patient_user=None, dob="1964-03-11"):
    pid = f"DRVP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, user_id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date, attending_doctor_id, date_of_birth)
               VALUES (?, ?, ?, ?, 'R-7', 'DR-1', 58, 'M', 'Community acquired pneumonia',
                       'admitted', '2026-09-28 06:30:00', ?, ?)""",
            (pid, patient_user, f"Review Patient {pid[-4:]}", f"MRN-{pid[-6:]}", attending, dob),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, 'DR-1', 'R-7', 'active', ?)",
            (f"ADM-{pid}", pid, department, attending),
        )
    return pid


@pytest.fixture()
def world():
    doctor = _mk_user("doctor", DEPT, name="Dr Meera Raman")
    p_user = _mk_user("patient")
    pid = _mk_patient(attending=doctor, patient_user=p_user)
    with db_session() as db:
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, reaction, severity, status) "
            "VALUES (?, 'Penicillin', 'Anaphylaxis', 'severe', 'active')",
            (pid,),
        )
        db.execute(
            "INSERT OR REPLACE INTO patient_profiles (patient_id, comorbidities) VALUES (?, ?)",
            (pid, json.dumps(["Type 2 diabetes", "Hypertension"])),
        )
    return {
        "pid": pid,
        "doctor": doctor,
        "patient_user": p_user,
        "nurse": _mk_user("nurse", DEPT),
        "other_doctor": _mk_user("doctor", OTHER_DEPT),
    }


# ═════════════════════════════════════════════════════════════════════════════
# 1. The prompt carries what the model used to invent
# ═════════════════════════════════════════════════════════════════════════════

class _FakeResponse:
    def __init__(self, content):
        self._content = content

    def json(self):
        return {"message": {"content": self._content}}


def _fake_client(captured, reply):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            captured["payload"] = json
            return _FakeResponse(reply)

    return _Client


@pytest.mark.asyncio
async def test_the_prompt_carries_the_date_attending_allergies_and_comorbidities(world, monkeypatch):
    import services.discharge_service as ds
    captured = {}
    monkeypatch.setattr(ds.httpx, "AsyncClient", _fake_client(captured, "DISCHARGE SUMMARY"))

    await ds.generate_discharge_summary(world["pid"])

    prompt = captured["payload"]["messages"][-1]["content"]
    assert f"DISCHARGE DATE: {ds._site_today()}" in prompt, "the model was not told the date"
    assert "Dr Meera Raman" in prompt, "the model was not told the attending doctor"
    assert "Penicillin" in prompt and "Anaphylaxis" in prompt, "the allergy was not in the prompt"
    assert "Type 2 diabetes" in prompt and "Hypertension" in prompt
    assert "[Doctor name]" not in prompt and "[Today's date]" not in prompt, (
        "the prompt still hands the model a slot to fill"
    )


@pytest.mark.asyncio
async def test_an_unfilled_slot_never_reaches_the_stored_text(world, monkeypatch):
    import services.discharge_service as ds
    reply = ("ATTENDING DOCTOR:\n[Doctor name]\nDischarge: [Today's date]\n"
             "Ceftriaxone 1 g IV [UNVERIFIED: pharmacist review required]")
    monkeypatch.setattr(ds.httpx, "AsyncClient", _fake_client({}, reply))

    text = await ds.generate_discharge_summary(world["pid"])

    assert "[Doctor name]" not in text and "[Today's date]" not in text
    assert "Not documented" in text
    # Orb's own bracketed markers carry a colon and must survive.
    assert "[UNVERIFIED: pharmacist review required]" in text


def test_the_offline_summary_states_only_recorded_facts(world):
    from services.discharge_service import offline_discharge_summary
    text = offline_discharge_summary(world["pid"])
    assert "Drafting assistant unavailable" in text
    assert "Stable for discharge" not in text, "a condition was asserted that nobody recorded"
    assert "LLM" not in text
    assert "Penicillin" in text and "Dr Meera Raman" in text
    # A worded date, never a raw ISO timestamp.
    assert "T" not in text.split("Discharge:", 1)[1].split("\n", 1)[0]


# ═════════════════════════════════════════════════════════════════════════════
# 2. The discharge response, and the review gate
# ═════════════════════════════════════════════════════════════════════════════

def _discharge(world, monkeypatch, summary="Admitted with pneumonia. Treated with antibiotics."):
    import services.discharge_service as ds

    async def _draft(_pid):
        return summary

    monkeypatch.setattr(ds, "generate_discharge_summary", _draft)
    resp = client.post(
        f"/api/vigil/patients/{world['pid']}/discharge",
        headers=_auth(world["doctor"], "doctor"),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_the_discharge_response_gives_the_doctor_the_draft_and_no_disk_path(world, monkeypatch):
    body = _discharge(world, monkeypatch)
    assert body["review_status"] == "draft"
    assert body["summary"].startswith("Admitted with pneumonia")
    assert body["drafted_by"] == "assistant"
    assert "qr_path" not in body
    assert body["qr_code_url"] == f"/api/vigil/patients/{world['pid']}/qr"
    for value in body.values():
        if isinstance(value, str):
            assert "/uploads/" not in value and not value.startswith("/Users/"), (
                f"a filesystem path left the server: {value}"
            )


def test_the_portal_withholds_the_draft_until_a_prescriber_reviews_it(world, monkeypatch):
    body = _discharge(world, monkeypatch)
    patient = _auth(world["patient_user"], "patient")

    before = client.get("/api/bridge/discharge", headers=patient)
    assert before.status_code == 200, before.text
    assert before.json()["discharge_summary"] is None, "an unreviewed draft reached the patient"
    assert before.json()["status"] == "draft"
    assert "being finalised" in before.json()["message"]
    reports = client.get("/api/bridge/reports", headers=patient).json()
    assert reports["discharge_summary"]["summary"] is None

    # A nurse may read a discharge but may not release one.
    nurse = client.post(
        f"/api/vigil/patients/{world['pid']}/discharge-summary/review",
        json={"summary_id": body["summary_id"]}, headers=_auth(world["nurse"], "nurse"),
    )
    assert nurse.status_code == 403, nurse.text
    # A doctor on another ward cannot reach it at all.
    stranger = client.post(
        f"/api/vigil/patients/{world['pid']}/discharge-summary/review",
        json={"summary_id": body["summary_id"]}, headers=_auth(world["other_doctor"], "doctor"),
    )
    assert stranger.status_code == 404, stranger.text

    ok = client.post(
        f"/api/vigil/patients/{world['pid']}/discharge-summary/review",
        json={"summary_id": body["summary_id"]}, headers=_auth(world["doctor"], "doctor"),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["review_status"] == "reviewed"
    assert ok.json()["reviewed_by_name"] == "Dr Meera Raman"

    after = client.get("/api/bridge/discharge", headers=patient).json()
    assert after["discharge_summary"].startswith("Admitted with pneumonia")
    assert after["status"] == "reviewed"

    with db_session() as db:
        audit = db.execute(
            "SELECT actor_user_id FROM audit_logs WHERE action_type = 'discharge_summary_reviewed' "
            "AND patient_id = ? ORDER BY id DESC LIMIT 1",
            (world["pid"],),
        ).fetchone()
    assert audit is not None and audit["actor_user_id"] == world["doctor"], (
        "the release to the patient was not audited against the reviewer"
    )


def test_a_review_is_idempotent_and_names_the_first_reviewer(world, monkeypatch):
    body = _discharge(world, monkeypatch)
    url = f"/api/vigil/patients/{world['pid']}/discharge-summary/review"
    first = client.post(url, json={"summary_id": body["summary_id"]},
                        headers=_auth(world["doctor"], "doctor")).json()
    second_doc = _mk_user("doctor", DEPT, name="Dr Second Reader")
    second = client.post(url, json={"summary_id": body["summary_id"]},
                         headers=_auth(second_doc, "doctor")).json()
    assert second["reviewed_at"] == first["reviewed_at"]
    assert second["reviewed_by_name"] == "Dr Meera Raman"


def test_the_clinician_view_labels_the_draft_and_has_no_qr_when_none_was_issued(monkeypatch):
    doctor = _mk_user("doctor", DEPT)
    pid = _mk_patient(attending=doctor, dob=None)
    world = {"pid": pid, "doctor": doctor}
    body = _discharge(world, monkeypatch)
    assert body["secure_link_issued"] is False
    assert body["qr_code_url"] is None
    assert body["secure_link_reason"], "the desk is not told why no link was printed"

    view = client.get(f"/api/vigil/patients/{pid}/discharge-summary",
                      headers=_auth(doctor, "doctor"))
    assert view.status_code == 200, view.text
    assert view.json()["review_status"] == "draft"
    assert view.json()["qr_code_url"] is None


def test_the_discharge_link_prints_only_continued_verified_medicines(world, monkeypatch):
    with db_session() as db:
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source) "
            "VALUES (?, 'Amoxicillin', '500 mg', 'Oral', 'TDS', 'discontinued', 'clinician')",
            (world["pid"],),
        )
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source) "
            "VALUES (?, 'Metformin', '500 mg', 'Oral', 'BD', 'active', 'clinician')",
            (world["pid"],),
        )
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source) "
            "VALUES (?, 'Warfarin', '5 mg', 'Oral', 'OD', 'active', 'llm_extracted')",
            (world["pid"],),
        )
    body = _discharge(world, monkeypatch)
    raw_token = body["summary_url"].rsplit("/", 1)[-1]
    grant = client.post("/api/discharge-access/verify",
                        json={"token": raw_token, "dob": "1964-03-11"}).json()["access_grant"]
    data = client.get(f"/api/discharge-access/{raw_token}/summary",
                      headers={"Authorization": f"Bearer {grant}"}).json()
    names = [m["drug_name"] for m in data["medications"]]
    assert names == ["Metformin"], names
    assert data["unverified_medication_count"] == 1
    assert data["patient"]["mrn"] == f"MRN-{world['pid'][-6:]}"
    assert data["patient"]["admission_id"] == f"ADM-{world['pid']}", "the encounter id is missing"
    assert [a["substance"] for a in data["allergies"]] == ["Penicillin"]
    assert data["comorbidities"] == ["Type 2 diabetes", "Hypertension"]


# ═════════════════════════════════════════════════════════════════════════════
# 3. The Bridge care team and vitals
# ═════════════════════════════════════════════════════════════════════════════

def test_the_care_team_names_no_nurse_nobody_assigned(world):
    _mk_user("nurse", DEPT, name="Nurse Who Was Never Assigned")
    resp = client.get("/api/bridge/care-team", headers=_auth(world["patient_user"], "patient"))
    assert resp.status_code == 200, resp.text
    team = resp.json()["care_team"]
    names = [m["name"] for m in team]
    assert "Nurse Who Was Never Assigned" not in names
    nursing = [m for m in team if m.get("is_team")]
    assert nursing and nursing[0]["name"] == "Nursing team"
    assert nursing[0]["ward"] == "DR-1"
    assert "Dr Meera Raman" in names


def test_the_portal_status_is_scored_from_the_reading_it_prints(world):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with db_session() as db:
        # An older risk row that says 'stable', and a newer reading that is not.
        db.execute(
            "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
            "VALUES (?, ?, 0.0, 'stable', '[]')",
            (world["pid"], now),
        )
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                                   spo2, temperature, respiratory_rate)
               VALUES (?, ?, 128.4, 100.2, 61.6, 85.0, 38.05, 24.6)""",
            (world["pid"], now),
        )
    patient = _auth(world["patient_user"], "patient")

    # (The /vitals-trends card is ruled verdict-free and owned by another
    # change; this covers the status line only.)
    status = client.get("/api/bridge/status", headers=patient).json()
    assert status["status"] == "critical", status
    assert "normal precaution" not in status["status_message"]
    assert status["vitals"]["oxygen"] == "85%"
    assert status["vitals"]["temperature"] == "38.1°C"
