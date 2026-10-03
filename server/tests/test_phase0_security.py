"""Phase 0 security immediates — regression tests.

Covers three fixes:
  1. Patient accounts are created with a unique one-time password, never the old
     shared "patient123". First fixed in routes/vigil_routes.py create_patient;
     that route is retired (410), so this pins the admission door that replaced
     it (POST /api/admissions/admit, services/admission_service.admit_patient).
  2. The ingestion API key is compared in constant time (routes/ingestion_routes.py).
  3. The Scribe sign endpoint refuses to sign a note that failed hallucination
     verification unless the signer explicitly acknowledges (routes/scribe_routes.py).
"""
import json
from unittest import mock

import routes.ingestion_routes  # ensure loaded before patching

with mock.patch("config.INGESTION_MODE", "hybrid"), \
     mock.patch("routes.ingestion_routes.INGESTION_MODE", "hybrid"), \
     mock.patch("main.INGESTION_MODE", "hybrid"):
    from fastapi.testclient import TestClient
    from main import app
    from database import db_session

client = TestClient(app)


def _login(user_id: str, password: str) -> str:
    resp = client.post("/api/auth/login", json={"employee_id": user_id, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _doctor_token() -> str:
    from auth import hash_password
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            ("P0-DOC", "Dr P0", "doctor", hash_password("docpass123")),
        )
    return _login("P0-DOC", "docpass123")


# ── Fix 1: no shared default patient password ────────────────────────────────
def test_created_patient_gets_unique_onetime_password():
    token = _doctor_token()
    resp = client.post(
        "/api/admissions/admit",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Phase0 Patient", "age": 40, "sex": "F", "admission_source": "ED",
              "admission_type": "emergency", "department": "General Medicine",
              "ward": "General", "bed": "P0-1", "attending_doctor_id": "D001",
              "chief_complaint": "fever"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    try:
        temp = body.get("temporary_password")
        assert temp and temp != "patient123", "must return a unique one-time password, not the shared default"

        uid = body["patient_login_id"]
        # The old shared password must NOT work.
        bad = client.post("/api/auth/login", json={"employee_id": uid, "password": "patient123"})
        assert bad.status_code != 200
        # The returned one-time password must work.
        good = client.post("/api/auth/login", json={"employee_id": uid, "password": temp})
        assert good.status_code == 200, good.text
    finally:
        # Discharged again on both tables, so the admission leaves no one on a board.
        with db_session() as db:
            db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (body["patient_id"],))
            db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?",
                       (body["patient_id"],))


# ── Fix 2: ingestion key verification rejects wrong keys ─────────────────────
def test_ingestion_rejects_wrong_api_key():
    resp = client.post(
        "/api/ingest/json",
        headers={"X-API-Key": "definitely-not-the-key"},
        json={"observations": []},
    )
    assert resp.status_code == 401


def test_ingestion_rejects_missing_api_key():
    resp = client.post("/api/ingest/json", json={"observations": []})
    assert resp.status_code == 401


# ── Fix 3: Scribe sign-gate on failed verification ───────────────────────────
def _make_note(status="draft", verification=None) -> int:
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, structured_note, note_format, status, verification_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (None, "P0-DOC", "t", "S: note", "soap", status,
             json.dumps(verification) if verification else None),
        )
        return cur.lastrowid


def test_sign_blocked_when_verification_failed():
    _doctor_token()  # ensure P0-DOC exists
    token = _login("P0-DOC", "docpass123")
    note_id = _make_note(verification={"verified": False, "verification_status": "unverifiable"})
    resp = client.post(
        f"/api/scribe/notes/{note_id}/sign",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp.status_code == 428, resp.text
    assert resp.json()["detail"]["requires"] == "acknowledge_unverified"


def test_sign_allowed_with_acknowledgement():
    token = _login("P0-DOC", "docpass123")
    note_id = _make_note(verification={"verified": False, "verification_status": "unverifiable"})
    resp = client.post(
        f"/api/scribe/notes/{note_id}/sign",
        headers={"Authorization": f"Bearer {token}"},
        json={"acknowledge_unverified": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "signed"


def test_sign_allowed_when_verified():
    token = _login("P0-DOC", "docpass123")
    note_id = _make_note(verification={"verified": True, "verification_status": "verified", "warnings": [], "omissions": []})
    resp = client.post(
        f"/api/scribe/notes/{note_id}/sign",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp.status_code == 200, resp.text
