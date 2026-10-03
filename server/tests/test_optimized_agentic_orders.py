"""Tests for Orb Agentic Order Execution Flow

Validates:
  1. RxNorm formulary matching (Double Metaphone + Levenshtein).
  2. PIN-gated confirmation & safety triggers.
  3. High-risk double-doctor sign-off.
  4. Pre-flight inventory checks & alternatives.
  5. Cryptographic delegation hash chain creation in audit logs.
"""
import os
import sys
import pytest
import json
import uuid
import hashlib
from unittest import mock

# Explicitly import routes.ingestion_routes so it is loaded
import routes.ingestion_routes

with mock.patch("config.INGESTION_MODE", "hybrid"), \
     mock.patch("routes.ingestion_routes.INGESTION_MODE", "hybrid"), \
     mock.patch("main.INGESTION_MODE", "hybrid"):
    from fastapi.testclient import TestClient
    from main import app
    from database import db_session, get_db

client = TestClient(app)

def _create_test_user(user_id: str, name: str, role: str, pin: str | None):
    """Helper to insert a test user. PINs are seeded as LEGACY unsalted SHA-256
    hashes on purpose — the compatibility path pin_security.verify_pin must keep
    accepting (and upgrading) them. pin=None leaves pin_hash NULL (unprovisioned)."""
    from auth import hash_password
    pwd = hash_password("testpass123")
    pin_hash = hashlib.sha256(pin.encode()).hexdigest() if pin else None
    with db_session() as db:
        db.execute(
            """INSERT OR REPLACE INTO users (id, name, role, password_hash, pin_hash)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, name, role, pwd, pin_hash),
        )
    return user_id


def _null_hash_audit_count() -> int:
    """Rows outside the tamper-evident hash chain (legacy/raw inserts)."""
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) AS c FROM audit_logs WHERE row_hash IS NULL"
        ).fetchone()["c"]

def _login(user_id: str) -> str:
    """Helper to login and get JWT token."""
    resp = client.post("/api/auth/login", json={"employee_id": user_id, "password": "testpass123"})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.json()["token"]

def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

@pytest.fixture(autouse=True)
def setup_agentic_order_data():
    """Seeds isolation-ready dataset for agentic order testing."""
    from services.helix_db import init_helix_db
    init_helix_db()
    
    def _clean(db):
        db.execute("PRAGMA foreign_keys = OFF")
        tables = [
            "sage_handoff_events", "sage_handoffs", "sage_action_events", "sage_actions",
            "sage_room_memory", "sage_observations", "sage_conversations", "messages",
            "channels", "discharge_access_tokens", "discharge_summaries", "lens_analyses",
            "surgery_team_assignments", "surgery_prep_requests", "surgeries", "admissions",
            "audit_logs", "helix_stock_movements", "helix_transitions", "helix_patient_med_orders",
            "helix_inventory_lots", "helix_medication_master", "event_log", "sage_decision_cache",
            "risk_scores", "scribe_notes", "vitals", "medications", "forecast_predictions",
            "patient_profiles", "clinical_documents", "observations", "ingestion_events",
            "hospital_forecasts", "admission_history", "patients", "users"
        ]
        for table in tables:
            try:
                db.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        db.execute("PRAGMA foreign_keys = ON")

    with db_session() as db:
        _clean(db)

    # The PIN attempt limiter is in-process state — reset it so a lockout in one
    # test cannot leak into the next.
    from services import pin_security
    pin_security.PIN_ATTEMPTS.clear()

    # Create test doctors AFTER cleaning the database!
    _create_test_user("D-PRIMARY-DOC", "Dr. Primary Confirm", "doctor", "1234")
    _create_test_user("D-COSIGN-DOC", "Dr. Cosign Co", "doctor", "5678")
        
    with db_session() as db:
        # Insert test patients
        db.execute(
            "INSERT OR IGNORE INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
            ("PAT-ORDER-1", "Order Patient One", "MRN-ORDER-1")
        )
        
        # Insert test channels/rooms
        db.execute(
            "INSERT OR IGNORE INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
            ("case-PAT-ORDER-1", "Order 1 Case Room", "PAT-ORDER-1")
        )
        
        # Insert medications to master catalog
        # 1. Epinephrine (High-risk, in stock)
        db.execute(
            """INSERT INTO helix_medication_master (id, name, generic_name, form, strength) 
               VALUES (?, ?, ?, ?, ?)""",
            ("MED-EPI", "Epinephrine 1mg/mL Injection", "Epinephrine", "Vial", "1mg/mL")
        )
        # 2. Amoxicillin (Standard, in stock)
        db.execute(
            """INSERT INTO helix_medication_master (id, name, generic_name, form, strength) 
               VALUES (?, ?, ?, ?, ?)""",
            ("MED-AMOX", "Amoxicillin 500mg Oral Capsule", "Amoxicillin", "Capsule", "500mg")
        )
        # 3. A cephalosporin, in stock, labelled with its TRUE molecule. It used
        # to be labelled amoxicillin so the stockout lookup would offer it.
        db.execute(
            """INSERT INTO helix_medication_master (id, name, generic_name, form, strength) 
               VALUES (?, ?, ?, ?, ?)""",
            ("MED-CEFADIN", "Cefadroxil 500mg Oral Capsule", "Cefadroxil", "Capsule", "500mg")
        )
        # 3b. The same molecule, strength and form as the 250mg capsule below,
        # under another brand: the only kind of row a stockout may suggest.
        db.execute(
            """INSERT INTO helix_medication_master (id, name, generic_name, form, strength) 
               VALUES (?, ?, ?, ?, ?)""",
            ("MED-MOX-250", "Mox 250mg Capsule", "Amoxicillin", "Capsule", "250mg")
        )
        # 4. Out-of-stock medication (e.g. Epinephrine alternative/standard out-of-stock Amoxicillin)
        db.execute(
            """INSERT INTO helix_medication_master (id, name, generic_name, form, strength) 
               VALUES (?, ?, ?, ?, ?)""",
            ("MED-OUT-OF-STOCK", "Amoxicillin 250mg Oral Capsule", "Amoxicillin", "Capsule", "250mg")
        )

        # Seed Stock
        db.execute(
            """INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) 
               VALUES (?, ?, ?, ?, ?)""",
            ("LOT-EPI", "MED-EPI", "LOTEPI1", 50, "2029-12-31")
        )
        db.execute(
            """INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) 
               VALUES (?, ?, ?, ?, ?)""",
            ("LOT-AMOX", "MED-AMOX", "LOTAMOX1", 20, "2029-12-31")
        )
        db.execute(
            """INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) 
               VALUES (?, ?, ?, ?, ?)""",
            ("LOT-CEFADIN", "MED-CEFADIN", "LOTCEF1", 10, "2029-12-31")
        )
        db.execute(
            """INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) 
               VALUES (?, ?, ?, ?, ?)""",
            ("LOT-MOX-250", "MED-MOX-250", "LOTMOX1", 10, "2029-12-31")
        )
        # Out-of-stock has 0 quantity
        db.execute(
            """INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) 
               VALUES (?, ?, ?, ?, ?)""",
            ("LOT-OOS", "MED-OUT-OF-STOCK", "LOTOOS1", 0, "2029-12-31")
        )

    yield
    
    with db_session() as db:
        _clean(db)


# ════════════════════════════════════════════
#  Test Cases
# ════════════════════════════════════════════

def test_formulary_reconciliation():
    """Verify raw/misspelled drug names resolve correctly to Helix catalog entries."""
    from services.rxnorm_reconciler import reconcile_medication
    
    # Exact match
    med, score = reconcile_medication("Amoxicillin 500mg Oral Capsule")
    assert med is not None
    assert med["id"] == "MED-AMOX"
    assert score >= 0.95

    # Phonetic/ASR misspelling match ("amoxcilin")
    med, score = reconcile_medication("amoxcilin")
    assert med is not None
    assert med["id"] == "MED-AMOX"
    assert score >= 0.8


def test_pre_flight_checks():
    """Verify that pre-flight endpoint returns corrected details, high-risk flags, stock checks and substitutes."""
    token = _login("D-PRIMARY-DOC")
    action_id = str(uuid.uuid4())
    
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, state)
               VALUES (?, 'case-PAT-ORDER-1', 'PAT-ORDER-1', 'prescribe', 'amoxcilin', 'Suspected UTI', 'suggested')""",
            (action_id,)
        )
        
    resp = client.get(
        f"/api/relay/channels/case-PAT-ORDER-1/sage_actions/{action_id}/pre_flight",
        headers=_auth_header(token)
    )
    
    assert resp.status_code == 200
    data = resp.json()
    assert data["resolved_medication"]["id"] == "MED-AMOX"
    assert data["stock_status"]["in_stock"] is True
    assert data["stock_status"]["total_quantity"] == 20
    assert data["is_high_risk"] is False


def test_pre_flight_out_of_stock_substitute():
    """An out-of-stock pre-flight flags the stockout and suggests only an exact
    equivalent: the same molecule, strength and form under another brand."""
    with db_session() as db:
        db.execute("UPDATE helix_inventory_lots SET quantity = 0 WHERE id = 'LOT-AMOX'")

    token = _login("D-PRIMARY-DOC")
    action_id = str(uuid.uuid4())
    
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, state)
               VALUES (?, 'case-PAT-ORDER-1', 'PAT-ORDER-1', 'prescribe', 'Amoxicillin 250mg Oral Capsule', 'Suspected infection', 'suggested')""",
            (action_id,)
        )
        
    resp = client.get(
        f"/api/relay/channels/case-PAT-ORDER-1/sage_actions/{action_id}/pre_flight",
        headers=_auth_header(token)
    )
    
    assert resp.status_code == 200
    data = resp.json()
    assert data["resolved_medication"]["id"] == "MED-OUT-OF-STOCK"
    assert data["stock_status"]["in_stock"] is False
    assert data["alternative_suggestion"]["id"] == "MED-MOX-250"
    assert data["alternative_suggestion"]["equivalence"] == "Same molecule, strength and form"


def test_pre_flight_never_suggests_a_different_molecule():
    """Amoxicillin 500mg out of stock with only a cephalosporin and a different
    strength on the shelf: nothing is suggested. Choosing another molecule is
    the prescriber's decision, not the formulary's."""
    with db_session() as db:
        db.execute("UPDATE helix_inventory_lots SET quantity = 0 WHERE id = 'LOT-AMOX'")
    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("Amoxicillin 500mg Oral Capsule")
    resp = client.get(
        f"/api/relay/channels/case-PAT-ORDER-1/sage_actions/{action_id}/pre_flight",
        headers=_auth_header(token)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["resolved_medication"]["id"] == "MED-AMOX"
    assert data["stock_status"]["in_stock"] is False
    assert data["alternative_suggestion"] is None


def test_stockout_confirm_suggests_and_never_changes_the_drug():
    """Confirming an out-of-stock drug used to switch the order silently to the
    alternative, carrying the original dose. It now orders nothing and returns
    the suggestion for the prescriber."""
    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("Amoxicillin 250mg Oral Capsule")
    resp = _confirm(token, action_id, {
        "clinician_pin": "1234",
        "dose_confirmation": {"dose": "250mg", "frequency": "TID"},
    })
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "out_of_stock"
    assert detail["alternative_suggestion"]["id"] == "MED-MOX-250"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) AS c FROM helix_patient_med_orders").fetchone()["c"] == 0
        act = db.execute("SELECT state FROM sage_actions WHERE id = ?", (action_id,)).fetchone()
        assert act["state"] == "suggested"


def test_stockout_confirm_with_a_reason_orders_the_drug_as_written():
    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("Amoxicillin 250mg Oral Capsule")
    resp = _confirm(token, action_id, {
        "clinician_pin": "1234",
        "dose_confirmation": {"dose": "250mg", "frequency": "TID"},
        "override_reason": "Pharmacy sourcing from the satellite store",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    with db_session() as db:
        order = db.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?",
                           (data["order_id"],)).fetchone()
        assert order["medication_id"] == "MED-OUT-OF-STOCK", "the drug confirmed is the drug ordered"
        audit = db.execute("SELECT metadata_json FROM audit_logs WHERE correlation_id = ?",
                           (data["correlation_id"],)).fetchone()
    meta = json.loads(audit["metadata_json"])
    assert meta["out_of_stock_override"] is True
    assert meta["override_reason"] == "Pharmacy sourcing from the satellite store"


def _seed_action(target: str, rationale: str = "Infection treatment") -> str:
    action_id = str(uuid.uuid4())
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, state)
               VALUES (?, 'case-PAT-ORDER-1', 'PAT-ORDER-1', 'prescribe', ?, ?, 'suggested')""",
            (action_id, target, rationale)
        )
    return action_id


def _confirm(token: str, action_id: str, payload: dict):
    return client.post(
        f"/api/relay/channels/case-PAT-ORDER-1/sage_actions/{action_id}/confirm_agentic",
        headers=_auth_header(token),
        json=payload,
    )


def test_confirm_standard_medication_success():
    """Verify standard medication confirmation executes successfully with valid PIN and generates correlation hash logs."""
    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("amoxcilin")
    null_hash_rows_before = _null_hash_audit_count()

    resp = _confirm(token, action_id, {
        "clinician_pin": "1234",
        "dose_confirmation": {"dose": "500mg", "frequency": "TID"},
    })

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["order_id"] is not None
    assert data["correlation_id"] is not None
    assert data["delegation_hash"] is not None

    # Check database status transitions and audit logs
    with db_session() as db:
        # Action is confirmed
        act = db.execute("SELECT state FROM sage_actions WHERE id = ?", (action_id,)).fetchone()
        assert act["state"] == "confirmed"

        # SAGE action event logged
        event = db.execute("SELECT * FROM sage_action_events WHERE action_id = ?", (action_id,)).fetchone()
        assert event is not None
        assert "Confirmed agentic order" in event["reason"]

        # Order successfully placed in Helix — with the clinician-confirmed dose,
        # never 'TBD'
        order = db.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?", (data["order_id"],)).fetchone()
        assert order is not None
        assert order["medication_id"] == "MED-AMOX"
        assert order["dosage"] == "500mg"
        assert order["frequency"] == "TID"

        # Immutable healthcare audit logs written with cryptographically matching correlation_id
        audit = db.execute("SELECT * FROM audit_logs WHERE correlation_id = ?", (data["correlation_id"],)).fetchone()
        assert audit is not None
        assert audit["event_category"] == "clinical_safety"
        assert audit["action_type"] == "agentic_med_confirm"
        assert data["delegation_hash"] in audit["summary_message"]
        # The confirmation row is ON the tamper-evident hash chain (the old raw
        # INSERT left row_hash NULL, invisible to verify_audit_chain).
        assert audit["row_hash"] is not None

    # No new rows outside the hash chain, and the whole chain still verifies.
    assert _null_hash_audit_count() == null_hash_rows_before
    from database import verify_audit_chain
    chain = verify_audit_chain()
    assert chain["ok"] is True, f"audit chain broken: {chain}"


def test_confirm_high_risk_medication_requires_and_accepts_safety_ack():
    """High-alert medications require an explicit safety acknowledgement (single
    doctor sign-off, no co-signer) — 428 without it, dispatched with it."""
    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("Epinephrine 1mg/mL Injection", "Bradycardic code event")

    dose_conf = {"dose": "1mg", "frequency": "STAT", "route": "IV"}

    # Without safety_ack the high-alert med is refused with the 428 ack contract.
    resp = _confirm(token, action_id, {"clinician_pin": "1234", "dose_confirmation": dose_conf})
    assert resp.status_code == 428
    assert resp.json()["detail"]["error"] == "drug_safety_acknowledgement_required"

    # With an acknowledged reason it dispatches on a single doctor's PIN.
    resp = _confirm(token, action_id, {
        "clinician_pin": "1234",
        "dose_confirmation": dose_conf,
        "safety_ack": {"acknowledged": True, "reason": "Code event — benefit outweighs risk"},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["order_id"] is not None


def test_tbd_dose_without_dose_confirmation_is_422():
    """An agentic order carries no machine-resolved dose — dispatch without an
    explicit dose_confirmation must be refused, never written as 'TBD'."""
    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("amoxcilin")

    resp = _confirm(token, action_id, {"clinician_pin": "1234"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "dose_confirmation_required"

    # Explicit 'TBD' strings are refused just the same.
    resp = _confirm(token, action_id, {
        "clinician_pin": "1234",
        "dose_confirmation": {"dose": "TBD", "frequency": "TBD"},
    })
    assert resp.status_code == 422

    with db_session() as db:
        act = db.execute("SELECT state FROM sage_actions WHERE id = ?", (action_id,)).fetchone()
        assert act["state"] == "suggested"  # nothing dispatched
        assert db.execute("SELECT COUNT(*) AS c FROM helix_patient_med_orders").fetchone()["c"] == 0


def test_wrong_pin_five_times_locks_out():
    """5 PIN failures inside the window lock the user out — even the correct
    PIN is then refused with 429 for the lockout duration."""
    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("amoxcilin")
    payload = {"clinician_pin": "9999", "dose_confirmation": {"dose": "500mg", "frequency": "TID"}}

    for _ in range(5):
        resp = _confirm(token, action_id, payload)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid clinician PIN"

    # 6th attempt — with the CORRECT pin — is locked out.
    resp = _confirm(token, action_id, {**payload, "clinician_pin": "1234"})
    assert resp.status_code == 429
    assert "locked" in resp.json()["detail"].lower()


def test_null_pin_hash_fails_closed_with_distinct_error():
    """A user with no provisioned PIN (pin_hash NULL — the default now that the
    universal-PIN seed is gone) gets a distinct fail-closed error, not a
    guessable 'Invalid PIN'."""
    _create_test_user("D-NOPIN-DOC", "Dr. No Pin", "doctor", None)
    token = _login("D-NOPIN-DOC")
    action_id = _seed_action("amoxcilin")

    resp = _confirm(token, action_id, {
        "clinician_pin": "1234",
        "dose_confirmation": {"dose": "500mg", "frequency": "TID"},
    })
    assert resp.status_code == 403
    assert "no signing pin is provisioned" in resp.json()["detail"].lower()


def test_pin_provisioning_endpoint_enables_confirmation():
    """Admin provisions a bcrypt PIN for an unprovisioned doctor via
    POST /api/admin/users/{id}/pin; self-service requires the password; a
    non-admin cannot set another user's PIN."""
    _create_test_user("D-NEWPIN-DOC", "Dr. New Pin", "doctor", None)
    _create_test_user("A-PIN-ADMIN", "Admin Pin", "admin", None)
    admin_token = _login("A-PIN-ADMIN")
    doc_token = _login("D-NEWPIN-DOC")

    # Non-admin cannot provision someone else's PIN.
    resp = client.post("/api/admin/users/D-PRIMARY-DOC/pin",
                       headers=_auth_header(doc_token), json={"pin": "4321"})
    assert resp.status_code == 403

    # Self-service without / with wrong password is refused.
    resp = client.post("/api/admin/users/D-NEWPIN-DOC/pin",
                       headers=_auth_header(doc_token), json={"pin": "4321"})
    assert resp.status_code == 400
    resp = client.post("/api/admin/users/D-NEWPIN-DOC/pin",
                       headers=_auth_header(doc_token),
                       json={"pin": "4321", "password": "wrongpass"})
    assert resp.status_code == 400

    # Non-digit PINs are refused.
    resp = client.post("/api/admin/users/D-NEWPIN-DOC/pin",
                       headers=_auth_header(admin_token), json={"pin": "abcd"})
    assert resp.status_code == 400

    # Admin provisions the PIN (stored as bcrypt, never sha256).
    resp = client.post("/api/admin/users/D-NEWPIN-DOC/pin",
                       headers=_auth_header(admin_token), json={"pin": "4321"})
    assert resp.status_code == 200
    with db_session() as db:
        ph = db.execute("SELECT pin_hash FROM users WHERE id = 'D-NEWPIN-DOC'").fetchone()["pin_hash"]
    assert ph.startswith("$2")

    # The provisioned PIN now signs an agentic confirmation.
    action_id = _seed_action("amoxcilin")
    resp = _confirm(doc_token, action_id, {
        "clinician_pin": "4321",
        "dose_confirmation": {"dose": "500mg", "frequency": "TID"},
    })
    assert resp.status_code == 200

    # Self-service with the correct password also works.
    resp = client.post("/api/admin/users/D-NEWPIN-DOC/pin",
                       headers=_auth_header(doc_token),
                       json={"pin": "8765", "password": "testpass123"})
    assert resp.status_code == 200


def test_legacy_sha256_pin_upgraded_to_bcrypt_on_success():
    """The seeded legacy SHA-256 pin_hash still verifies and is transparently
    upgraded to bcrypt after a successful confirmation."""
    token = _login("D-PRIMARY-DOC")
    action_id = _seed_action("amoxcilin")

    with db_session() as db:
        before = db.execute("SELECT pin_hash FROM users WHERE id = 'D-PRIMARY-DOC'").fetchone()["pin_hash"]
    assert not before.startswith("$2")

    resp = _confirm(token, action_id, {
        "clinician_pin": "1234",
        "dose_confirmation": {"dose": "500mg", "frequency": "TID"},
    })
    assert resp.status_code == 200

    with db_session() as db:
        after = db.execute("SELECT pin_hash FROM users WHERE id = 'D-PRIMARY-DOC'").fetchone()["pin_hash"]
    assert after.startswith("$2")

    # The persisted delegation hash must NOT be derivable from the legacy
    # unsalted digest (sha256 of a 4-8 digit PIN is offline-brute-forceable):
    # it must chain the POST-upgrade bcrypt hash, whose random salt an attacker
    # reading audit rows cannot reconstruct.
    with db_session() as db:
        act = db.execute("SELECT * FROM sage_actions WHERE id = ?", (action_id,)).fetchone()
    src = str(dict(act).get("source_msg_id", "0"))
    llm_sig = hashlib.sha256(f"{act['target']}:{act['rationale']}".encode()).hexdigest()

    def _delegation(pin_hash: str) -> str:
        vh = hashlib.sha256(f"D-PRIMARY-DOC:{pin_hash}".encode()).hexdigest()
        return hashlib.sha256(f"{src}:{llm_sig}:{vh}".encode()).hexdigest()

    delegation = resp.json()["delegation_hash"]
    assert delegation != _delegation(before)   # legacy digest NOT embedded
    assert delegation == _delegation(after)    # salted bcrypt hash embedded
