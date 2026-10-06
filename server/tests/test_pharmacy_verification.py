"""Pharmacist Verification Queue — the closed loop for AI/agentic meds.

Covers the service layer (list pending → verify with a corrected dose → gone from
pending; reject → discontinued) and the drug-safety alert surfacing, plus the
HTTP routes wired with pharmacist (helix.read / helix.write) auth.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import db_session
from services.clinical_records_service import add_allergy
from services import pharmacy_verification_service as pharmacy
from routes.pharmacy_routes import router as pharmacy_router
from auth import create_token


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def patient():
    pid = f"PATRX{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, "Rx Verify Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    return pid


@pytest.fixture()
def pharmacist():
    uid = f"PH{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'pharmacist', 'x')",
            (uid, "Pharm Test"),
        )
    return uid


def _chart_ai_med(patient_id, drug="Amoxicillin", dosage="REVIEW REQUIRED",
                  route="Review", frequency="REVIEW REQUIRED", source="llm_extracted"):
    """Chart an unverified AI/agentic med (verified_by NULL), return its id."""
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, prescribed_by, source)
               VALUES (?, ?, ?, ?, ?, 'active', 'D001', ?)""",
            (patient_id, drug, dosage, route, frequency, source),
        )
        return cur.lastrowid


# A complete pharmacist correction of the AI placeholders.
_FULL_ORDER = {"dosage": "500mg", "route": "Oral", "frequency": "TDS"}


# ── service: pending listing ──────────────────────────────────────────────────
def test_ai_med_appears_in_pending(patient):
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    pending = pharmacy.list_pending_verification()
    ids = [p["id"] for p in pending]
    assert med_id in ids
    row = next(p for p in pending if p["id"] == med_id)
    assert row["dose"] == "REVIEW REQUIRED"
    assert row["source"] == "llm_extracted"
    assert row["patient"] == "Rx Verify Test"


def test_clinician_med_not_in_pending(patient):
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, source)
               VALUES (?, 'Aspirin', '75mg', 'Oral', 'OD', 'active', 'clinician')""",
            (patient,),
        )
        clin_id = cur.lastrowid
    assert clin_id not in [p["id"] for p in pharmacy.list_pending_verification()]


# ── service: verify ───────────────────────────────────────────────────────────
def test_verify_activates_and_corrects_dose(patient, pharmacist):
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    result = pharmacy.verify_medication(med_id, pharmacist, dosage="500mg", route="Oral",
                                        frequency="QDS")

    assert result["status"] == "verified"
    med = result["medication"]
    assert med["dose"] == "500mg"
    assert med["frequency"] == "QDS"
    assert med["status"] == "active"
    assert med["verified_by"] == pharmacist

    with db_session() as db:
        row = db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()
    assert row["dosage"] == "500mg"
    assert row["verified_by"] == pharmacist
    assert row["verified_at"] is not None
    assert row["status"] == "active"

    # no longer pending
    assert med_id not in [p["id"] for p in pharmacy.list_pending_verification()]


def test_verify_without_corrections_keeps_existing(patient, pharmacist):
    med_id = _chart_ai_med(patient, drug="Paracetamol", dosage="1g", route="Oral",
                           frequency="QDS")
    result = pharmacy.verify_medication(med_id, pharmacist)
    assert result["medication"]["dose"] == "1g"
    assert result["medication"]["route"] == "Oral"


def test_double_verify_rejected(patient, pharmacist):
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    pharmacy.verify_medication(med_id, pharmacist, **_FULL_ORDER)
    with pytest.raises(ValueError):
        pharmacy.verify_medication(med_id, pharmacist, **{**_FULL_ORDER, "dosage": "1g"})


# ── service: reject ───────────────────────────────────────────────────────────
def test_reject_discontinues(patient, pharmacist):
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    result = pharmacy.reject_medication(med_id, pharmacist, reason="Wrong patient")

    assert result["status"] == "rejected"
    assert result["medication"]["status"] == "discontinued"

    with db_session() as db:
        row = db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()
    assert row["status"] == "discontinued"
    assert row["verified_by"] == pharmacist
    assert row["verified_at"] is not None
    assert "Wrong patient" in (row["notes"] or "")

    assert med_id not in [p["id"] for p in pharmacy.list_pending_verification()]


def test_reject_requires_reason(patient, pharmacist):
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    with pytest.raises(ValueError):
        pharmacy.reject_medication(med_id, pharmacist, reason="   ")


# ── drug-safety alert surfacing ───────────────────────────────────────────────
def test_pending_surfaces_allergy_alert(patient):
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_ai_med(patient, drug="Amoxicillin 500mg")
    row = next(p for p in pharmacy.list_pending_verification() if p["id"] == med_id)
    assert row["safety_alerts"], "expected an allergy alert on the pending row"
    assert any(a["type"] == "allergy" for a in row["safety_alerts"])
    assert row["max_severity"] == "severe"
    assert row["interaction_screened"] is True
    assert row["screen_failed"] is False


def test_pending_surfaces_unscreened_drug(patient):
    """A drug outside the interaction KB must NOT look like a clean screen."""
    med_id = _chart_ai_med(patient, drug="Obscuremab")
    row = next(p for p in pharmacy.list_pending_verification() if p["id"] == med_id)
    assert row["interaction_screened"] is False
    assert row["coverage_note"], "expected a coverage note for an unscreened drug"
    assert row["screen_failed"] is False


def test_pending_screen_failure_fails_closed(patient, monkeypatch):
    """A crashed safety screen must surface as 'unknown/failed', never 'no alerts'."""
    def boom(patient_id, drug_name, **_):
        raise RuntimeError("interaction DB down")
    monkeypatch.setattr(pharmacy, "check_medication_safety", boom)

    med_id = _chart_ai_med(patient, drug="Paracetamol")
    row = next(p for p in pharmacy.list_pending_verification() if p["id"] == med_id)
    assert row["screen_failed"] is True
    assert row["max_severity"] == "unknown"
    assert row["interaction_screened"] is False
    assert "unavailable" in row["coverage_note"].lower()
    assert row["safety_alerts"] == []


# ── severe-ack gate on verify (fail closed) ───────────────────────────────────
def test_verify_severe_requires_acknowledgement(patient, pharmacist):
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_ai_med(patient, drug="Amoxicillin 500mg")
    with pytest.raises(ValueError, match=r"^severe_ack_required:") as exc:
        pharmacy.verify_medication(med_id, pharmacist, **_FULL_ORDER)
    # the screen rides on the exception so the route can return it in the 428
    assert exc.value.safety["max_severity"] == "severe"

    # the med was NOT activated
    with db_session() as db:
        row = db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()
    assert row["verified_by"] is None
    assert med_id in [p["id"] for p in pharmacy.list_pending_verification()]


def test_verify_screen_failure_requires_acknowledgement(patient, pharmacist, monkeypatch):
    def boom(patient_id, drug_name, **_):
        raise RuntimeError("interaction DB down")
    monkeypatch.setattr(pharmacy, "check_medication_safety", boom)

    med_id = _chart_ai_med(patient, drug="Paracetamol")
    with pytest.raises(ValueError, match=r"^severe_ack_required:") as exc:
        pharmacy.verify_medication(med_id, pharmacist, **_FULL_ORDER)
    assert exc.value.safety["screen_failed"] is True

    with db_session() as db:
        row = db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()
    assert row["verified_by"] is None


def test_a_pharmacist_cannot_override_a_severe_allergy(patient, pharmacist):
    """Prescriber only, as at the bedside and at order time (2026-10-06): the
    reason is refused, the med stays unverified, and no override is recorded."""
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_ai_med(patient, drug="Amoxicillin 500mg")
    with pytest.raises(ValueError, match=r"^severe_ack_required:") as exc:
        pharmacy.verify_medication(med_id, pharmacist, **_FULL_ORDER,
                                   ack_reason="Desensitisation protocol in place")
    assert exc.value.safety["ack_refused"].startswith(
        "Only a prescriber can override an allergy block.")
    assert exc.value.safety["override_requires_permission"] == "medication.prescribe"
    with db_session() as db:
        row = db.execute("SELECT verified_by FROM medications WHERE id = ?", (med_id,)).fetchone()
        overrides = db.execute(
            "SELECT count(*) FROM audit_logs WHERE action_type = 'pharmacy_verify_override' "
            "AND target_id = ?", (str(med_id),)).fetchone()[0]
        refused = db.execute(
            "SELECT count(*) FROM audit_logs WHERE action_type = 'pharmacy_verify_allergy_block' "
            "AND target_id = ?", (str(med_id),)).fetchone()[0]
    assert row["verified_by"] is None
    assert overrides == 0 and refused == 1


def test_verify_severe_with_reason_verifies_and_audits(patient):
    """A verifier who holds medication.prescribe may still clear it, audited."""
    prescriber = f"PRX{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, 'doctor', 'x')",
                   (prescriber, "Dr Verify Test"))
    pharmacist = prescriber
    add_allergy(patient, "amoxicillin", reaction="anaphylaxis", severity="severe")
    med_id = _chart_ai_med(patient, drug="Amoxicillin 500mg")
    result = pharmacy.verify_medication(med_id, pharmacist, **_FULL_ORDER,
                                        ack_reason="Desensitisation protocol in place")

    assert result["status"] == "verified"
    assert result["medication"]["verified_by"] == pharmacist
    assert any(a["type"] == "allergy" for a in result["safety_alerts"])
    assert result["safety"]["max_severity"] == "severe"
    assert result["safety"]["override"] == {"by": pharmacist,
                                            "reason": "Desensitisation protocol in place"}

    # the override is on the audit chain
    with db_session() as db:
        audit = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'pharmacy_verify_override' "
            "AND target_id = ? ORDER BY id DESC LIMIT 1",
            (str(med_id),),
        ).fetchone()
    assert audit is not None
    assert audit["event_category"] == "clinical_safety"
    assert audit["outcome"] == "partial_success"
    assert audit["actor_user_id"] == pharmacist
    assert "Desensitisation protocol in place" in (audit["summary_message"] or "")
    # By name too, as the order lanes record an override.
    import json
    who = json.loads(audit["metadata_json"])["override_by"]
    assert who == {"user_id": prescriber, "display_name": "Dr Verify Test", "role": "doctor"}


def test_verify_sub_severe_needs_no_acknowledgement(patient, pharmacist):
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    result = pharmacy.verify_medication(med_id, pharmacist, **{**_FULL_ORDER, "dosage": "1g"})
    assert result["status"] == "verified"
    assert "override" not in result["safety"]


# ── HTTP routes (pharmacist auth) ─────────────────────────────────────────────
@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(pharmacy_router)
    return TestClient(app)


def _auth(uid, role="pharmacist"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def test_route_pending_verify_reject_flow(patient, pharmacist, client):
    add_allergy(patient, "amoxicillin", severity="severe")
    verify_id = _chart_ai_med(patient, drug="Amoxicillin 500mg")
    reject_id = _chart_ai_med(patient, drug="Ibuprofen")

    hdr = _auth(pharmacist)

    # pending list
    resp = client.get("/api/pharmacy/pending", headers=hdr)
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()["pending"]]
    assert verify_id in ids and reject_id in ids

    # verify with a corrected dose: the severe allergy now blocks with 428 until
    # the pharmacist acknowledges with a reason
    resp = client.post(f"/api/pharmacy/{verify_id}/verify",
                       json={"dosage": "500mg", "route": "Oral", "frequency": "TDS"},
                       headers=hdr)
    assert resp.status_code == 428, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "severe_ack_required"
    assert detail["safety"]["max_severity"] == "severe"
    assert any(a["type"] == "allergy" for a in detail["safety"]["alerts"])

    # A pharmacist's reason does not clear a severe allergy (prescriber only,
    # 2026-10-06): still 428, with the refusal, and the way on is a rejection.
    resp = client.post(f"/api/pharmacy/{verify_id}/verify",
                       json={"dosage": "500mg", "route": "Oral", "frequency": "TDS",
                             "ack_reason": "Discussed with consultant"}, headers=hdr)
    assert resp.status_code == 428, resp.text
    assert resp.json()["detail"]["safety"]["ack_refused"].startswith("Only a prescriber")
    resp = client.post(f"/api/pharmacy/{verify_id}/reject",
                       json={"reason": "Allergy: back to the prescriber"}, headers=hdr)
    assert resp.status_code == 200, resp.text

    # reject the other
    resp = client.post(f"/api/pharmacy/{reject_id}/reject",
                       json={"reason": "Duplicate order"}, headers=hdr)
    assert resp.status_code == 200, resp.text
    assert resp.json()["medication"]["status"] == "discontinued"

    # both gone from pending
    resp = client.get("/api/pharmacy/pending", headers=hdr)
    remaining = [p["id"] for p in resp.json()["pending"]]
    assert verify_id not in remaining and reject_id not in remaining


def test_route_verify_forbidden_for_patient(patient, client):
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('PAT-RX-1', 'Pat', 'patient', 'x')")
    resp = client.post(f"/api/pharmacy/{med_id}/verify", json={"dosage": "1g"},
                       headers=_auth("PAT-RX-1", "patient"))
    assert resp.status_code == 403


# ── placeholders never become the verified order (F012) ───────────────────────
def _row(med_id):
    with db_session() as db:
        return db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()


@pytest.mark.parametrize("corrections", [
    {},                                                         # nothing entered
    {"dosage": "500mg", "frequency": "TDS"},                    # route left as 'Review'
    {"dosage": "", "route": "Oral", "frequency": "TDS"},        # blank dose
    {"dosage": "   ", "route": "Oral", "frequency": "TDS"},     # whitespace dose
    {"dosage": "500mg", "route": "Oral", "frequency": "review required"},
])
def test_verify_refuses_blank_or_placeholder_fields(patient, pharmacist, corrections):
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    with pytest.raises(pharmacy.IncompleteOrderError,
                       match="Enter a dose, route and frequency before verifying"):
        pharmacy.verify_medication(med_id, pharmacist, **corrections)
    row = _row(med_id)
    assert row["verified_by"] is None
    assert row["dosage"] == "REVIEW REQUIRED"
    assert med_id in [p["id"] for p in pharmacy.list_pending_verification()]


def test_route_verify_blank_dose_is_422_and_unverified(patient, pharmacist, client):
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    resp = client.post(f"/api/pharmacy/{med_id}/verify",
                       json={"dosage": "", "route": "Oral", "frequency": "TDS"},
                       headers=_auth(pharmacist))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Enter a dose, route and frequency before verifying"
    assert _row(med_id)["verified_by"] is None


# ── the row under review, stopped drugs, discharged patients (F063) ───────────
def test_single_unverified_anticoagulant_is_not_screened_against_itself(patient, pharmacist):
    """An AI med is charted active, so it used to interact with its own row."""
    med_id = _chart_ai_med(patient, drug="Enoxaparin")
    row = next(p for p in pharmacy.list_pending_verification() if p["id"] == med_id)
    assert row["safety_alerts"] == []
    assert row["max_severity"] == "none"
    # and verification needs no severe acknowledgement
    result = pharmacy.verify_medication(med_id, pharmacist, dosage="40mg", route="SC",
                                        frequency="OD")
    assert result["status"] == "verified"
    assert result["safety"]["max_severity"] == "none"


def test_second_anticoagulant_still_flags_the_duplicate(patient):
    with db_session() as db:
        db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, source)
               VALUES (?, 'Heparin', '5000 units', 'SC', 'BD', 'active', 'clinician')""",
            (patient,),
        )
    med_id = _chart_ai_med(patient, drug="Enoxaparin")
    row = next(p for p in pharmacy.list_pending_verification() if p["id"] == med_id)
    assert row["max_severity"] == "severe"
    assert any(a["type"] == "interaction" for a in row["safety_alerts"])


def test_stopped_med_is_not_pending_and_cannot_be_verified(patient, pharmacist, client):
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    with db_session() as db:
        db.execute("UPDATE medications SET status = 'discontinued' WHERE id = ?", (med_id,))
    assert med_id not in [p["id"] for p in pharmacy.list_pending_verification()]

    resp = client.post(f"/api/pharmacy/{med_id}/verify", json=_FULL_ORDER,
                       headers=_auth(pharmacist))
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "This medication was stopped on the chart"
    row = _row(med_id)
    assert row["status"] == "discontinued"
    assert row["verified_by"] is None


def test_med_stopped_during_the_screen_is_not_revived(patient, pharmacist, monkeypatch):
    """Verification no longer writes status: a stop that lands mid-screen holds."""
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    real = pharmacy._safety_alerts

    def stop_then_screen(patient_id, drug_name, mid=None):
        with db_session() as db:
            db.execute("UPDATE medications SET status = 'discontinued' WHERE id = ?", (med_id,))
        return real(patient_id, drug_name, mid)
    monkeypatch.setattr(pharmacy, "_safety_alerts", stop_then_screen)

    with pytest.raises(ValueError, match="stopped on the chart"):
        pharmacy.verify_medication(med_id, pharmacist, **_FULL_ORDER)
    row = _row(med_id)
    assert row["status"] == "discontinued"
    assert row["verified_by"] is None


def test_discharged_patient_meds_leave_the_queue(patient):
    med_id = _chart_ai_med(patient, drug="Paracetamol")
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (patient,))
    assert med_id not in [p["id"] for p in pharmacy.list_pending_verification()]


def test_route_interaction_coverage_report(pharmacist, client):
    resp = client.get("/api/pharmacy/interaction-coverage", headers=_auth(pharmacist))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["covered"] + body["not_covered"] == body["total"]
    assert isinstance(body["items"], list)
