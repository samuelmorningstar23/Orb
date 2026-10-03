import pytest
import json
from unittest import mock

# Explicitly import routes.ingestion_routes so it is loaded before mock.patch checks attributes
import routes.ingestion_routes

# Mock config before importing main or anything that imports config
with mock.patch("config.INGESTION_MODE", "hybrid"), \
     mock.patch("routes.ingestion_routes.INGESTION_MODE", "hybrid"), \
     mock.patch("main.INGESTION_MODE", "hybrid"):
    from fastapi.testclient import TestClient
    from main import app
    from database import db_session, get_db

client = TestClient(app)

VALID_JSON_PAYLOAD = {
    "patient_id": "P-1001",
    "timestamp": "2026-04-20T10:00:00Z",
    "observations": [
        {"name": "Heart Rate", "value": 85, "unit": "bpm"},
        {"name": "Systolic BP", "value": 120, "unit": "mmHg"},
        {"name": "Diastolic BP", "value": 80, "unit": "mmHg"},
        {"name": "SpO2", "value": 98, "unit": "%"},
        {"name": "Temperature", "value": 37.2, "unit": "C"},
        {"name": "Respiratory Rate", "value": 16, "unit": "bpm"}
    ]
}

VALID_HL7_PAYLOAD = "MSH|^~\\&|SENDING_APP|SENDING_FAC|ORB|ORB_FAC|20260420100000||ORU^R01|MSG12345|P|2.5\rPID|1||P-1002^^^MRN||DOE^JOHN||19800101|M\rOBR|1|||VITAL_PANEL^Vital Signs\rOBX|1|NM|8867-4^Heart Rate||95|bpm|||||F\rOBX|2|NM|8480-6^Systolic BP||110|mmHg|||||F"

CRITICAL_JSON_PAYLOAD = {
    "patient_id": "P-1001",
    "timestamp": "2026-04-20T10:05:00Z",
    "observations": [
        {"name": "Heart Rate", "value": 150, "unit": "bpm"},
        {"name": "Systolic BP", "value": 70, "unit": "mmHg"},
        {"name": "SpO2", "value": 85, "unit": "%"}
    ]
}


def clear_audit_logs():
    with db_session() as db:
        # audit_logs is append-only at the DB layer (triggers block DELETE). For a test
        # reset, drop the guards, wipe, then restore them so the protection still holds.
        db.execute("DROP TRIGGER IF EXISTS trg_audit_logs_no_update")
        db.execute("DROP TRIGGER IF EXISTS trg_audit_logs_no_delete")
        db.execute("DELETE FROM audit_logs")
        db.execute("DELETE FROM vitals")
        db.execute("DELETE FROM observations")
        db.execute("DELETE FROM ingestion_events")
        db.execute("CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_update "
                   "BEFORE UPDATE ON audit_logs BEGIN "
                   "SELECT RAISE(ABORT, 'audit_logs is append-only: UPDATE is forbidden'); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_delete "
                   "BEFORE DELETE ON audit_logs BEGIN "
                   "SELECT RAISE(ABORT, 'audit_logs is append-only: DELETE is forbidden'); END")


@pytest.fixture(autouse=True)
def setup_teardown():
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES ('U-1001', 'Test Patient 1', 'patient', 'x')")
        db.execute("INSERT OR IGNORE INTO patients (id, user_id, name, mrn) VALUES ('P-1001', 'U-1001', 'Test Patient 1', 'MRN-TEST-1')")
        db.execute("INSERT OR IGNORE INTO channels (id, type, name, patient_id) VALUES ('case-P-1001', 'case_room', 'P-1001 Case Room', 'P-1001')")
        
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES ('U-1002', 'Test Patient 2', 'patient', 'x')")
        db.execute("INSERT OR IGNORE INTO patients (id, user_id, name, mrn) VALUES ('P-1002', 'U-1002', 'Test Patient 2', 'MRN-TEST-2')")
    clear_audit_logs()
    yield
    clear_audit_logs()
    # TEST-ISOLATION LAW: patients.status DEFAULTS to 'admitted', so these two
    # seeds used to sit admitted in the shared session DB for the rest of the
    # run. The FHIR self-export in services/downtime_service.py iterates
    # `patients WHERE status='admitted'` and sleeps _PATIENT_SLEEP_S (250 ms)
    # BETWEEN PATIENTS, so each leftover adds ~0.275 s to every export call in
    # tests/test_downtime_fhir_export.py. Discharge rather than DELETE — the
    # channels row and the ingestion/audit rows this module asserts on
    # legitimately reference these ids, and the INSERT OR IGNORE above re-uses
    # them on the next test.
    with db_session() as db:
        db.execute(
            "UPDATE patients SET status = 'discharged' WHERE id IN ('P-1001', 'P-1002')"
        )


def test_json_ingestion_lifecycle():
    headers = {"X-API-Key": "orb-ingest-dev-key"}
    response = client.post("/api/ingest/json", json=VALID_JSON_PAYLOAD, headers=headers)
    assert response.status_code == 200
    
    data = response.json()
    correlation_id = data["correlation_id"]
    
    with db_session() as db:
        logs = db.execute("SELECT * FROM audit_logs WHERE correlation_id = ? ORDER BY timestamp ASC", (correlation_id,)).fetchall()
        
    actions = [log["action_type"] for log in logs]
    print("ALL LOGS:", [dict(log) for log in logs])
    assert "ingestion_request_received" in actions
    assert "ingestion_parse_success" in actions
    assert "vital_inserted" in actions
    assert "news2_recomputed" in actions


def test_hl7_ingestion_lifecycle():
    headers = {"X-API-Key": "orb-ingest-dev-key", "Content-Type": "text/plain"}
    response = client.post("/api/ingest/hl7", data=VALID_HL7_PAYLOAD.encode("utf-8"), headers=headers)
    assert response.status_code == 200
    
    data = response.json()
    correlation_id = data["correlation_id"]
    
    with db_session() as db:
        logs = db.execute("SELECT action_type FROM audit_logs WHERE correlation_id = ? ORDER BY timestamp ASC", (correlation_id,)).fetchall()
        
    actions = [log["action_type"] for log in logs]
    assert "ingestion_request_received" in actions
    assert "ingestion_parse_success" in actions
    assert "vital_inserted" in actions
    assert "news2_recomputed" in actions


def test_malformed_payload_parse_failure():
    headers = {"X-API-Key": "orb-ingest-dev-key"}
    response = client.post("/api/ingest/json", json={"invalid": "payload"}, headers=headers)
    # The API returns 422 for unprocessable, we just check the log
    
    data = response.json()
    correlation_id = data["correlation_id"]
    
    with db_session() as db:
        logs = db.execute("SELECT action_type FROM audit_logs WHERE correlation_id = ? ORDER BY timestamp ASC", (correlation_id,)).fetchall()
        
    actions = [log["action_type"] for log in logs]
    assert "ingestion_request_received" in actions
    assert "ingestion_parse_failure" in actions


def test_duplicate_payload_detected():
    headers = {"X-API-Key": "orb-ingest-dev-key"}
    
    # Send once
    client.post("/api/ingest/json", json=VALID_JSON_PAYLOAD, headers=headers)
    
    # Send again
    response = client.post("/api/ingest/json", json=VALID_JSON_PAYLOAD, headers=headers)
    assert response.status_code == 200
    
    data = response.json()
    correlation_id = data["correlation_id"]
    
    with db_session() as db:
        logs = db.execute("SELECT action_type FROM audit_logs WHERE correlation_id = ? ORDER BY timestamp ASC", (correlation_id,)).fetchall()
        
    actions = [log["action_type"] for log in logs]
    assert "ingestion_request_received" in actions
    assert "ingestion_duplicate_detected" in actions


def test_alert_generation():
    headers = {"X-API-Key": "orb-ingest-dev-key"}
    response = client.post("/api/ingest/json", json=CRITICAL_JSON_PAYLOAD, headers=headers)
    assert response.status_code == 200
    
    data = response.json()
    correlation_id = data["correlation_id"]
    
    with db_session() as db:
        logs = db.execute("SELECT action_type FROM audit_logs WHERE correlation_id = ? ORDER BY timestamp ASC", (correlation_id,)).fetchall()
        
    actions = [log["action_type"] for log in logs]
    assert "vigil_alert_generated" in actions
    assert "relay_alert_generated" in actions


def test_login_success():
    with db_session() as db:
        from auth import hash_password
        pwd = hash_password("password123")
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)", ("D-101", "Dr. Test", "doctor", pwd))
        
    response = client.post("/api/auth/login", json={"employee_id": "D-101", "password": "password123"})
    assert response.status_code == 200
    
    with db_session() as db:
        log = db.execute("SELECT * FROM audit_logs WHERE action_type = 'login_success' ORDER BY timestamp DESC LIMIT 1").fetchone()
        
    assert log is not None
    assert log["actor_user_id"] == "D-101"
    assert log["outcome"] == "success"


def test_login_failure():
    response = client.post("/api/auth/login", json={"employee_id": "D-101", "password": "wrongpassword"})
    assert response.status_code == 401
    
    with db_session() as db:
        log = db.execute("SELECT * FROM audit_logs WHERE action_type = 'login_failure' ORDER BY timestamp DESC LIMIT 1").fetchone()
        
    assert log is not None
    assert log["outcome"] == "failure"
    metadata = json.loads(log["metadata_json"])
    assert metadata["attempted_id"] == "D-101"


# ══════════════════════════════════════════════════════════════════════════════
#  ATTRIBUTION — the mutation trail must NAME the person who made the change
# ══════════════════════════════════════════════════════════════════════════════
# THE DEFECT THESE THREE HOLD SHUT (found 2026-08-14, no test covered it).
#
# services/audit_middleware.py records one `api_mutation` row for EVERY
# POST/PUT/PATCH/DELETE — the safety net that guarantees no change to clinical
# data goes unlogged. It resolved the actor by reading the `Authorization`
# header and nothing else. Deferred A then moved the SPA onto the HttpOnly
# `orb_session` cookie (auth.py:26) precisely so an XSS could not read the
# session out of localStorage — and the browser stopped sending an
# Authorization header.
#
# From that day every mutation made THROUGH THE APP landed on the tamper-evident
# chain with actor_user_id NULL. Nothing looked broken: the rows were written,
# the hash chain still verified, the middleware still awaited its write. The
# trail simply said nobody did any of it — while machine clients and this test
# suite, which do send bearer tokens, kept producing perfectly attributed rows.
# That is the worst shape a logging bug can take: correct-looking output from
# the population you test, silence from the population you ship.
#
# It matters more here than "an audit field is empty". auth.py:31-44 rests the
# PIN-on-device design on the claim that the ACTOR is the person, recorded per
# request — that is what makes a shared ward tablet acceptable at all. And
# audit_logs is append-only (see clear_audit_logs above), so the attribution
# cannot be backfilled: whatever was lost is lost.
#
# THE THREE CASES ARE ASSERTED TOGETHER ON PURPOSE. A test that only checked
# "the cookie mutation names an actor" could pass against a middleware that
# stamped a constant, and a test that only checked bearer would have passed
# throughout the entire outage.

_ATTR_DOC = "AUDIT-ATTR-DOC"
_ATTR_PWD = "attribution-pass-123"


def _mk_attribution_doctor() -> str:
    """A doctor with a password, NO department (hospital-wide, so ward scoping
    is not what this test is measuring)."""
    from auth import hash_password
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
            (_ATTR_DOC, "Dr Attribution", "doctor", hash_password(_ATTR_PWD)),
        )
        # A prior test's logout bumps token_version and would 401 us.
        db.execute("UPDATE users SET token_version = 0, is_active = 1 WHERE id = ?", (_ATTR_DOC,))
    return _ATTR_DOC


_ORDER_BODY = {
    "patient_id": "P-1001",
    "order_type": "nursing",
    "description": "Hourly observations",
}


def _mutation_rows(path="/api/orders"):
    """The api_mutation safety-net rows for `path`, newest first.

    Read straight from the table rather than through /api/audit/logs: the point
    of contention is what was WRITTEN, and a read API could plausibly fill in an
    actor it did not store.
    """
    with db_session() as db:
        return db.execute(
            "SELECT * FROM audit_logs WHERE event_category = 'api_mutation' "
            "AND request_path = ? ORDER BY id DESC",
            (path,),
        ).fetchall()


def test_a_cookie_authenticated_mutation_names_its_actor():
    """THE REGRESSION TEST. A mutation made the way the SPA actually makes them
    — HttpOnly session cookie, CSRF echo, no Authorization header — must land on
    the chain with the clinician's id on it."""
    doc = _mk_attribution_doctor()
    login = client.post("/api/auth/login",
                        json={"employee_id": doc, "password": _ATTR_PWD})
    assert login.status_code == 200, login.text

    # The browser's request: cookies only. Assert the absence explicitly — if a
    # future client helper started attaching a bearer token this test would go
    # on passing while testing the wrong path entirely.
    from auth import SESSION_COOKIE, CSRF_COOKIE
    assert client.cookies.get(SESSION_COOKIE), "no session cookie: not the browser path"
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf, "no CSRF cookie: the double-submit echo below would be a 403"

    resp = client.post("/api/orders", json=_ORDER_BODY, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200, resp.text
    assert "authorization" not in {k.lower() for k in resp.request.headers.keys()}

    rows = _mutation_rows()
    assert rows, "no api_mutation row was written for the cookie mutation at all"
    assert rows[0]["actor_user_id"] == doc, (
        "a mutation made from the SPA landed on the tamper-evident chain with "
        f"actor_user_id={rows[0]['actor_user_id']!r}. The audit middleware is "
        "decoding only the Authorization header again, and the browser does not "
        "send one — so every order, note and payment made through the app is "
        "now unattributable, permanently (audit_logs is append-only)."
    )
    assert rows[0]["actor_role"] == "doctor"


def test_a_bearer_authenticated_mutation_still_names_its_actor():
    """The CONTROL, and the reason the bug survived: this path never broke.

    Machine clients and this suite send `Authorization: Bearer`. Keeping the
    assertion here means a future 'simplification' of the extraction cannot fix
    the cookie by breaking the header.
    """
    from auth import create_token
    doc = _mk_attribution_doctor()
    token = create_token(doc, "doctor")

    resp = client.post("/api/orders", json=_ORDER_BODY,
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text

    rows = _mutation_rows()
    assert rows and rows[0]["actor_user_id"] == doc


def test_an_unauthenticated_mutation_records_no_actor():
    """NON-VACUITY. `actor_user_id` is not simply always populated — an
    anonymous attempt is still audited, and is still honestly anonymous.

    Without this, a middleware that stamped any non-empty string would satisfy
    the two tests above.
    """
    resp = client.post("/api/orders", json=_ORDER_BODY)
    assert resp.status_code == 401, resp.text

    rows = _mutation_rows()
    assert rows, "an unauthenticated mutation attempt was not audited at all"
    assert rows[0]["actor_user_id"] is None
    assert rows[0]["outcome"] == "denied"


def test_audit_query_endpoint():
    # 1. Login as admin
    # Assuming D-101 is 'doctor' not 'admin', so we need an admin to test.
    # Let's see if we have an admin or we can just mock the dependency.
    # Wait, system initialization probably doesn't have an admin by default. We can create one.
    with db_session() as db:
        from auth import hash_password
        pwd = hash_password("adminpass")
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)", ("A-999", "Admin User", "admin", pwd))
        
    response = client.post("/api/auth/login", json={"employee_id": "A-999", "password": "adminpass"})
    token = response.json()["token"]
    
    # 2. Ingest something to get a correlation ID
    headers = {"X-API-Key": "orb-ingest-dev-key"}
    ingest_resp = client.post("/api/ingest/json", json=VALID_JSON_PAYLOAD, headers=headers)
    corr_id = ingest_resp.json()["correlation_id"]
    
    # 3. Query audit logs
    query_resp = client.get(f"/api/audit/logs?correlation_id={corr_id}", headers={"Authorization": f"Bearer {token}"})
    assert query_resp.status_code == 200
    
    data = query_resp.json()
    assert "logs" in data
    assert len(data["logs"]) > 0
    for log in data["logs"]:
        assert log["correlation_id"] == corr_id

