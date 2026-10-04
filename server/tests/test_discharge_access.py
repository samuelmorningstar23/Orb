"""Tests for patient-facing discharge access."""
import pytest
import datetime
import hashlib
from fastapi.testclient import TestClient
from unittest.mock import patch

from main import app
from database import db_session
from config import DISCHARGE_ACCESS_MAX_ATTEMPTS

client = TestClient(app)

@pytest.fixture
def auth_headers():
    response = client.post("/api/auth/login", json={"employee_id": "DOC001", "password": "password"})
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}

@pytest.fixture
def test_patient():
    from auth import hash_password
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
            ("RBAC-PAT", "Patient RBAC Test", "patient", hash_password("testpass123"))
        )
        db.execute(
            "INSERT OR IGNORE INTO patients (id, user_id, name, mrn, date_of_birth) VALUES (?, ?, ?, ?, ?)",
            ("PAT001", "RBAC-PAT", "Dr. Legacy Patient", "MRN-LEGACY-1", "1990-01-01")
        )
        db.commit()
        patient = db.execute("SELECT * FROM patients WHERE id = 'PAT001'").fetchone()
    return patient

@pytest.fixture
def setup_discharge(auth_headers, test_patient):
    # Ensure patient is admitted
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'admitted' WHERE id = 'PAT001'")
        
        import uuid
        token_id = f"test_secure_token_{uuid.uuid4().hex}"
        # Store the REAL sha256(token_id) hash that discharge_service computes
        # (discharge_service.py: token_hash = sha256(token_id).hexdigest()), not a
        # bogus literal. This exercises the security-critical hashing column and
        # keeps the test correct whether the service looks up by token_id (current)
        # or by token_hash (the intended hardened lookup).
        token_hash = hashlib.sha256(token_id.encode()).hexdigest()
        expires_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)).isoformat()
        
        # Save discharge summary
        db.execute(
            """INSERT INTO discharge_summaries 
               (patient_id, author_id, summary, timeline_json, qr_code_path)
               VALUES (?, ?, ?, ?, ?)""",
            ("PAT001", "DOC001", "Test Summary", "[]", "/fake/path.png")
        )

        # Save access token
        db.execute(
            """INSERT INTO discharge_access_tokens
               (token_id, token_hash, patient_id, expires_at, created_by_user_id)
               VALUES (?, ?, ?, ?, ?)""",
            (token_id, token_hash, "PAT001", expires_at, "DOC001")
        )

        # Update patient status
        db.execute(
            "UPDATE patients SET status = 'discharged' WHERE id = 'PAT001'"
        )
        
    return token_id

def test_verify_discharge_success(setup_discharge, test_patient):
    token_id = setup_discharge
    dob = test_patient["date_of_birth"]
    
    response = client.post("/api/discharge-access/verify", json={"token": token_id, "dob": dob})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "access_grant" in data
    
    # Test getting summary with grant
    summary_res = client.get(f"/api/discharge-access/{token_id}/summary", headers={"Authorization": f"Bearer {data['access_grant']}"})
    assert summary_res.status_code == 200
    assert "summary" in summary_res.json()

def test_verify_discharge_invalid_dob(setup_discharge):
    token_id = setup_discharge
    
    response = client.post("/api/discharge-access/verify", json={"token": token_id, "dob": "1900-01-01"})
    assert response.status_code == 401
    assert response.json()["detail"]["reason"] == "invalid_dob"

def test_verify_discharge_lockout(setup_discharge):
    token_id = setup_discharge
    
    for _ in range(DISCHARGE_ACCESS_MAX_ATTEMPTS):
        response = client.post("/api/discharge-access/verify", json={"token": token_id, "dob": "1900-01-01"})
        assert response.status_code == 401
        
    # Max attempts reached, next attempt should show locked
    response = client.post("/api/discharge-access/verify", json={"token": token_id, "dob": "1900-01-01"})
    assert response.status_code == 401
    assert response.json()["detail"]["reason"] == "locked"

def test_verify_discharge_expired(setup_discharge, test_patient):
    token_id = setup_discharge
    dob = test_patient["date_of_birth"]
    
    # Manually expire the token in DB
    with db_session() as db:
        old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).isoformat()
        db.execute("UPDATE discharge_access_tokens SET expires_at = ? WHERE token_id = ?", (old_time, token_id))
        
    response = client.post("/api/discharge-access/verify", json={"token": token_id, "dob": dob})
    assert response.status_code == 401
    assert response.json()["detail"]["reason"] == "expired"

def test_verify_discharge_unknown_token_rejected(setup_discharge, test_patient):
    """An unknown/forged token (no matching DB row) must be rejected, even with a
    valid DOB. Guards the hashing/lookup path the previous bogus 'test_hash_123'
    value left unexercised: a token whose hash isn't stored cannot grant access."""
    dob = test_patient["date_of_birth"]
    forged_token = "forged_token_that_was_never_issued"

    response = client.post(
        "/api/discharge-access/verify",
        json={"token": forged_token, "dob": dob},
    )
    assert response.status_code == 401


def test_legacy_endpoint_removed():
    response = client.get("/api/vigil/patients/PAT001/discharge-summary/view")
    assert response.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
#  THE SECOND FACTOR  (pilot dry run, 2026-08-14)
#
#  Every discharge printed a summary URL and a QR code, and not one of them
#  could be opened: unlocking requires a stored date of birth, the admission
#  path never captured one, and 0 of 31 patients had one. A code that can only
#  ever answer "Incorrect Date of Birth" is worse than no code — the patient
#  leaves believing they have their record.
#
#  The invariants:
#   1. NO UNOPENABLE CODE. With no usable second factor on file, the discharge
#      still completes in full but NO link, token or QR is produced, and the
#      reason is stated to the discharge desk.
#   2. WITH a second factor, the portal opens with it.
#   3. A WRONG one is refused and reveals nothing about the patient — including
#      whether that patient exists or what state their record is in.
#   4. ONE definition at both ends: a date the portal cannot send is not a
#      second factor, so it must not cause a link to be issued either.
# ═══════════════════════════════════════════════════════════════════════════

def _seed_patient(patient_id: str, dob, *, mrn: str) -> None:
    with db_session() as db:
        db.execute("DELETE FROM discharge_access_tokens WHERE patient_id = ?", (patient_id,))
        db.execute("DELETE FROM discharge_summaries WHERE patient_id = ?", (patient_id,))
        db.execute(
            """INSERT OR REPLACE INTO patients
                   (id, name, mrn, age, sex, ward, bed, diagnosis, date_of_birth, status)
               VALUES (?, ?, ?, 61, 'F', 'General Medicine', 'C-2', 'Test diagnosis', ?, 'admitted')""",
            (patient_id, f"Second Factor {patient_id}", mrn, dob),
        )


def _discharge(patient_id: str) -> dict:
    """Run the real discharge write path with a canned summary (the LLM half is
    not what is under test here)."""
    from services.discharge_service import discharge_patient
    return discharge_patient(patient_id, "DOC001", "Test hospital course.", [])


def test_discharge_without_a_second_factor_prints_no_code_and_says_why():
    _seed_patient("PAT-SF-NODOB", None, mrn="MRN-SF-1")
    result = _discharge("PAT-SF-NODOB")

    assert result["status"] == "success", "The discharge itself must not be blocked"
    assert result["secure_link_issued"] is False
    assert not result["summary_url"], "A link was printed that can never be opened"
    # qr_code_url (the API path) replaced qr_path (a location on the
    # appliance's disk) on 2026-10-03; the client only ever read qr_code_url.
    assert "qr_path" not in result, "A filesystem path left the server"
    assert not result["qr_code_url"], "A QR code was printed that can never be opened"
    assert "date of birth" in (result["secure_link_reason"] or "").lower(), (
        "The discharge desk is given no reason it can act on"
    )

    with db_session() as db:
        tokens = db.execute(
            "SELECT COUNT(*) c FROM discharge_access_tokens WHERE patient_id = ?",
            ("PAT-SF-NODOB",),
        ).fetchone()["c"]
        row = db.execute(
            "SELECT qr_code_path FROM discharge_summaries WHERE patient_id = ?",
            ("PAT-SF-NODOB",),
        ).fetchone()
        status = db.execute(
            "SELECT status FROM patients WHERE id = ?", ("PAT-SF-NODOB",)
        ).fetchone()["status"]
    assert tokens == 0, "A token row exists for a link nobody can ever open"
    assert row is not None, "The discharge summary must still be written"
    assert not row["qr_code_path"]
    assert status == "discharged", "The discharge must complete regardless of paperwork"


def test_discharge_with_a_second_factor_issues_a_link_that_opens():
    _seed_patient("PAT-SF-DOB", "1964-03-11", mrn="MRN-SF-2")
    result = _discharge("PAT-SF-DOB")

    assert result["secure_link_issued"] is True
    assert result["summary_url"], "No link was issued for a patient who has a second factor"
    raw_token = result["summary_url"].rsplit("/", 1)[-1]

    verify = client.post(
        "/api/discharge-access/verify", json={"token": raw_token, "dob": "1964-03-11"}
    )
    assert verify.status_code == 200, f"The printed link does not open: {verify.text}"
    grant = verify.json()["access_grant"]
    summary = client.get(
        f"/api/discharge-access/{raw_token}/summary",
        headers={"Authorization": f"Bearer {grant}"},
    )
    assert summary.status_code == 200, summary.text
    # The link opens, but the drafted text is withheld until a clinician has
    # reviewed it (2026-10-03): the patient is told it is being finalised.
    body = summary.json()
    assert body["summary"] is None, "An unreviewed draft reached the patient"
    assert body["summary_status"] == "draft"
    assert "being finalised" in body["summary_message"]
    assert body["patient"]["mrn"] == "MRN-SF-2", "The MRN printed is not the hospital number"

    from services.discharge_service import mark_discharge_summary_reviewed
    mark_discharge_summary_reviewed("PAT-SF-DOB", result["summary_id"], "DOC001", "doctor")
    reviewed = client.get(
        f"/api/discharge-access/{raw_token}/summary",
        headers={"Authorization": f"Bearer {grant}"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["summary"] == "Test hospital course."
    assert reviewed.json()["summary_status"] == "reviewed"


def test_a_stored_date_the_portal_cannot_send_is_not_a_second_factor():
    """The portal submits YYYY-MM-DD from a date picker. A record holding
    "11/03/1964" cannot be matched by anything it can send, so it must not cause
    a link to be issued — issue-time and verify-time share one definition."""
    _seed_patient("PAT-SF-BADDOB", "11/03/1964", mrn="MRN-SF-3")
    result = _discharge("PAT-SF-BADDOB")
    assert result["secure_link_issued"] is False
    assert not result["summary_url"]


def test_a_stored_timestamp_dob_still_opens_with_the_date_the_picker_sends():
    """Imported records carry "1964-03-11 00:00:00". The date part is the factor;
    a patient typing the date they were born must get in."""
    _seed_patient("PAT-SF-TSDOB", "1964-03-11 00:00:00", mrn="MRN-SF-4")
    result = _discharge("PAT-SF-TSDOB")
    assert result["secure_link_issued"] is True
    raw_token = result["summary_url"].rsplit("/", 1)[-1]
    verify = client.post(
        "/api/discharge-access/verify", json={"token": raw_token, "dob": "1964-03-11"}
    )
    assert verify.status_code == 200, verify.text


def test_a_wrong_second_factor_reveals_nothing_about_the_patient():
    _seed_patient("PAT-SF-SECRET", "1964-03-11", mrn="MRN-SF-5")
    result = _discharge("PAT-SF-SECRET")
    raw_token = result["summary_url"].rsplit("/", 1)[-1]

    wrong = client.post(
        "/api/discharge-access/verify", json={"token": raw_token, "dob": "1970-01-01"}
    )
    assert wrong.status_code == 401
    detail = wrong.json()["detail"]
    assert detail["reason"] == "invalid_dob"
    assert "attempts_remaining" not in detail
    body = wrong.text
    for leak in ("PAT-SF-SECRET", "MRN-SF-5", "Second Factor", "1964"):
        assert leak not in body, f"The refusal leaks {leak!r}"

    # A token whose patient has NO usable second factor must fail identically:
    # the response may not disclose that this record is the incomplete one.
    _seed_patient("PAT-SF-LEGACY", None, mrn="MRN-SF-6")
    import hashlib as _hashlib
    import uuid as _uuid
    legacy_raw = f"legacy-token-{_uuid.uuid4().hex}"
    legacy_hash = _hashlib.sha256(legacy_raw.encode()).hexdigest()
    with db_session() as db:
        db.execute(
            """INSERT INTO discharge_access_tokens
                   (token_id, token_hash, patient_id, expires_at, created_by_user_id)
               VALUES (?, ?, ?, ?, ?)""",
            (
                legacy_hash, legacy_hash, "PAT-SF-LEGACY",
                (datetime.datetime.now(datetime.timezone.utc)
                 + datetime.timedelta(days=7)).isoformat(),
                "DOC001",
            ),
        )
    legacy = client.post(
        "/api/discharge-access/verify", json={"token": legacy_raw, "dob": "1964-03-11"}
    )
    assert legacy.status_code == wrong.status_code
    assert legacy.json()["detail"]["reason"] == detail["reason"], (
        "The response tells a token holder that this patient's record has no date "
        "of birth — a different answer for a different patient state is a leak"
    )


def test_admission_records_a_date_of_birth_but_is_never_blocked_by_one():
    """The admission path can now carry a date of birth — optional, and a bad one
    never stops an admission (an admission must not wait on paperwork)."""
    from services.admission_service import admit_patient

    staff = {"id": "DOC001", "name": "Test Doctor"}
    good = admit_patient(
        {"name": "DOB Good", "age": 44, "sex": "F", "ward": "General Medicine",
         "bed": "D-1", "date_of_birth": "1982-06-04"},
        staff,
    )
    assert good["date_of_birth_recorded"] is True
    from services.discharge_access_service import second_factor_status
    assert second_factor_status(good["patient_id"])["available"] is True

    for bad in (None, "", "not-a-date", "2099-01-01"):
        result = admit_patient(
            {"name": "DOB Bad", "age": 44, "sex": "F", "ward": "General Medicine",
             "bed": "D-2", "date_of_birth": bad},
            staff,
        )
        assert result["success"] is True, f"An admission was blocked by date_of_birth={bad!r}"
        assert result["date_of_birth_recorded"] is False
        assert second_factor_status(result["patient_id"])["available"] is False


def test_a_grant_signed_with_the_staff_secret_is_rejected(setup_discharge, test_patient):
    """Patient grants have a signing key of their own. A grant minted with the
    staff JWT_SECRET, with every claim correct, must not open the summary: a
    leak of the staff key cannot reach patient sessions."""
    import jwt
    import config

    assert config.DISCHARGE_JWT_SECRET != config.JWT_SECRET
    token_id = setup_discharge
    payload = {
        "sub": "discharge_access",
        "aud": "discharge",
        "token_id": hashlib.sha256(token_id.encode()).hexdigest(),
        "patient_id": "PAT001",
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
    }
    forged = jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)
    res = client.get(f"/api/discharge-access/{token_id}/summary",
                     headers={"Authorization": f"Bearer {forged}"})
    assert res.status_code in (401, 403), res.text


# ═══════════════════════════════════════════════════════════════════════════
#  THE PER-IP BUDGET  (2026-10-04)
#
#  /verify also counts every call per IP across ALL tokens, so one address
#  cannot walk through many links one guess at a time. Every TestClient call
#  comes from 'testclient', and that budget used to last the whole session:
#  this file spent 14 of 15 and test_discharge_review.py the last one.
#  conftest._isolate_discharge_verify_throttle now resets it around each
#  test. These two prove the reset did not switch the throttle off, and that
#  it really does stop at the test boundary. They run in file order.
# ═══════════════════════════════════════════════════════════════════════════

def test_the_per_ip_budget_throttles_verify_within_one_test():
    """A fresh token on every call, so the per-token lockout never engages and
    only the cross-token budget can say no."""
    from services import discharge_access_service as das

    for i in range(das._VERIFY_IP_MAX_ATTEMPTS):
        res = client.post("/api/discharge-access/verify",
                          json={"token": f"never-issued-{i}", "dob": "1900-01-01"})
        assert res.json()["detail"]["reason"] == "invalid_token", (
            f"call {i + 1} of {das._VERIFY_IP_MAX_ATTEMPTS}: {res.text}"
        )

    res = client.post("/api/discharge-access/verify",
                      json={"token": "never-issued-one-too-many", "dob": "1900-01-01"})
    assert res.status_code == 401
    assert res.json()["detail"]["reason"] == "rate_limited"


def test_the_next_test_starts_with_a_full_verify_budget():
    """Runs straight after the test above has used up the whole budget."""
    from services import discharge_access_service as das

    with das._VERIFY_IP_LOCK:
        spent = len(das._VERIFY_IP_ATTEMPTS.get("testclient", []))
    assert spent == 0, f"{spent} verify call(s) from an earlier test still count against this one"

    res = client.post("/api/discharge-access/verify",
                      json={"token": "never-issued-next-test", "dob": "1900-01-01"})
    assert res.json()["detail"]["reason"] == "invalid_token", res.text
