"""Order-time screening against the medication-order store (medorders-9, W9-C;
services/medorders/screening.py; rulings MO-R3 and MO-R6, held for Samuel).

Pinned here:
  * lifecycle off: the interaction screen reads 'active' ward rows exactly as
    before (a PRN drug is not screened), a PRN ward row is not givable, and no
    duplicate rule runs;
  * lifecycle on, the interaction screen (MO-R3 (a)): warfarin alerts against
    a PRN order and against a signed CPOE anticoagulant, and against a held
    order; a draft and an ended order are not screened against; an unlinked
    live ward row and a ward row whose link names no order are; an outpatient
    line is left to the outpatient lane; the screened order never meets
    itself (exclude_med_id and exclude_order_id, both directions);
  * lifecycle on, PRN givable (MO-R3 (a)): a PRN dose passes the full bedside
    gate set, and every gate still stops it when it should (held order,
    unverified AI drug, pharmacist check before the first dose, severe
    allergy, dose block, discharged patient), and the repeat window sees an
    earlier PRN dose;
  * lifecycle on, the same-ingredient duplicate (MO-R6): under the default
    rule 'block' a second live order for the same ingredient is refused (409
    duplicate_order, MedicationSafetyBlocked on a non-interactive lane) and no
    reason clears it; under 'acknowledge' it is a 428 that a reflex reason
    does not clear and a clinical reason does, audited; the rule runs only at
    order-time entry points; two insulins are two drugs, two strengths of
    paracetamol are one, a brand meets its generic through the catalogue;
    a longer catalogue name counts only as the same product, so a
    combination tablet never makes metformin and glimepiride one, nor a
    brand's cold remedy its paracetamol tablet and cetirizine (both
    directions, through the gate too);
    medicines sharing only a class word, a salt or an anion are not one
    (Vitamin K and Vitamin D3, Sodium Bicarbonate and Calcium Carbonate,
    Vitamin C and Vitamin B12, Calcium Gluconate and Zinc Gluconate,
    Magnesium Hydroxide and Aluminium Hydroxide), through the gate too;
  * the outpatient lane: inpatient rows count only while the patient is
    admitted (a finished stay's orders never block a clinic line); a line on
    the visit being prescribed on keeps the rule, a line on another visit (or
    one whose visit cannot be read) clears with a written reason, so a refill
    of a continuing medicine goes through with a reason, end to end through
    outpatient.write_prescription, which names its visit to the gate;
  * every entry point the server passes to the gate is classified;
  * discharge reconciliation reads the store's statuses and reasons.
"""
from __future__ import annotations

import ast
import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

import config
from database import db_session
from services import drug_safety_service, emar_service
from services.allergy_interlock import AllergyInterlockBlocked
from services.clinical_records_service import add_allergy
from services.discharge_service import _render_med_rec_text, build_medication_reconciliation
from services.drug_safety_service import DoseGuardBlocked
from services.medication_safety_gate import MedicationSafetyBlocked, enforce_medication_safety
from services.medorders import chart_lane, core, emar_check, outpatient, screening

DEPT = "W9C Screen Ward"
#: A coined brand whose catalogue generic is paracetamol (no English word in
#: it: tests/test_patient_specific_screen.py reads catalogue names).
BRAND_ID, BRAND_NAME = "HXW9CDLVX", "Dolvexa 650 Tablet"
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _module_rows():
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO helix_medication_master "
                   "(id, name, generic_name, form, strength) VALUES (?, ?, 'paracetamol', "
                   "'tablet', '650mg')", (BRAND_ID, BRAND_NAME))
    yield
    with db_session() as db:
        db.execute("DELETE FROM helix_medication_master WHERE id = ?", (BRAND_ID,))
        if not _PATIENTS:
            return
        marks = ", ".join("?" for _ in _PATIENTS)
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL, held_at = NULL, "
                   f"hold_reason = NULL WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                   _PATIENTS)
        db.execute(f"DELETE FROM opd_visits WHERE patient_id IN ({marks})", _PATIENTS)


@pytest.fixture()
def lifecycle(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)


@pytest.fixture()
def acknowledge(lifecycle, monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_DUPLICATE_RULE", "acknowledge")


def _user(role):
    uid = f"W9C{role[:3].upper()}{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)", (uid, f"{role.title()} {uid}", role, DEPT))
    return uid


@pytest.fixture(scope="module")
def doctor():
    return _user("doctor")


@pytest.fixture(scope="module")
def nurse():
    return _user("nurse")


def _patient(admitted_by):
    pid = f"PW9C{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, ?, ?, 'admitted', 'W9C', '3')",
                   (pid, f"Screen Scope {uuid.uuid4().hex[:5]}", f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                   "admitted_by) VALUES (?, ?, ?, 'W9C', '3', 'active', ?)",
                   (f"ADM-{pid}", pid, DEPT, admitted_by))
    _PATIENTS.append(pid)
    return pid


def _outpatient():
    """A registered patient who is not admitted (the outpatient lane's)."""
    pid = f"PW9CO{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'registered')",
                   (pid, f"Clinic Scope {uuid.uuid4().hex[:5]}", f"MRN-{uuid.uuid4().hex[:8]}"))
    _PATIENTS.append(pid)
    return pid


def _opd_visit(pid, *, status="in_consultation", days_ago=0):
    from datetime import timedelta
    from services.hospital_time import hospital_today
    vid = f"OPDV-W9C-{uuid.uuid4().hex[:8]}"
    day = (hospital_today() - timedelta(days=days_ago)).isoformat()
    with db_session() as db:
        db.execute("INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
                   "status, registered_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                   (vid, pid, DEPT, day, status, f"{day}T03:00:00Z"))
    return vid


def _opd_line(pid, doctor, drug, visit_id=None, *, dose="500 mg", freq="BD", end_at=None):
    """A live outpatient line written on `visit_id` (None: no visit on its
    'created' event), the way the outpatient lane writes one."""
    with db_session(immediate=True) as conn:
        write = core.create_in_txn(
            conn, patient_id=pid, drug_name=drug, source="opd_prescription",
            status="prescribed", entered_by=doctor, actor_id=doctor,
            entry_point=outpatient.ENTRY_POINT, encounter_type="op", dose=dose, route="Oral",
            frequency=freq, prescriber_id=doctor, end_at=end_at,
            event_payload=({"setting": "outpatient", "opd_visit_id": visit_id}
                           if visit_id else None))
    return write.order["id"]


def _charted(pid, doctor, drug, *, status="active", dose="5 mg", route="Oral", freq="OD"):
    """A live chart order (ward row and order, linked), written without the
    gate so a test can set up a chart the gate would question."""
    with db_session(immediate=True) as conn:
        out = chart_lane.write_chart_order_in_txn(
            conn, patient_id=pid, drug_name=drug, dosage=dose, route=route, frequency=freq,
            status=status, actor_id=doctor, actor_role="doctor")
    return out["id"], out["med_order_id"]


def _ward_row(pid, drug, *, status="active", dose="5 mg", route="Oral", freq="OD",
              source="clinician", verified_by=None):
    """A ward row with no order behind it (a legacy writer)."""
    with db_session() as db:
        return db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "source, verified_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, drug, dose, route, freq, status, source, verified_by)).lastrowid


def _draft(pid, doctor, drug, *, dose="40 mg", route="SC", freq="OD"):
    return chart_lane.create_order(patient_id=pid, drug_name=drug, status="draft",
                                   actor_id=doctor, actor_role="doctor", dose=dose,
                                   route=route, frequency=freq)["order"]


def _hold(order_id, reason="Nil by mouth before theatre"):
    with db_session(immediate=True) as conn:
        core.transition_in_txn(conn, order_id, "held", expected_version=None,
                               actor_id="W9CHOLDER", actor_role="doctor",
                               entry_point="test.hold", reason=reason)


def _end(order_id, status="stopped", reason="Course no longer needed"):
    with db_session(immediate=True) as conn:
        core.transition_in_txn(conn, order_id, status, expected_version=None,
                               actor_id="W9CENDER", actor_role="doctor",
                               entry_point="test.end", reason=reason)


def _alerts_with(pid, drug="Warfarin", **kw):
    alerts, _ = drug_safety_service._screen_interactions(pid, drug, **kw)
    return [a["with"] for a in alerts]


def _audits(action, pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? ORDER BY rowid",
            (action, pid)).fetchall()]


# ── Lifecycle off: exactly as before ──────────────────────────────────────────

def test_lifecycle_off_screens_active_rows_only_and_runs_no_duplicate_rule(doctor):
    pid = _patient(doctor)
    _ward_row(pid, "Enoxaparin", status="prn", dose="40 mg", route="SC")
    _ward_row(pid, "Paracetamol", dose="1 g")
    assert _alerts_with(pid) == []
    assert screening.store_screen_on() is False
    assert screening.duplicate_rule() is None
    safety = enforce_medication_safety(pid, "Paracetamol 500 mg", actor_id=doctor,
                                       entry_point="vigil.med_add", actor_role="doctor")
    assert "duplicate_check" not in safety


def test_lifecycle_off_a_prn_row_is_not_givable(doctor, nurse):
    pid = _patient(doctor)
    med_id = _ward_row(pid, "Paracetamol", status="prn", dose="1 g", freq="PRN")
    with pytest.raises(ValueError, match="not an active order"):
        emar_service.record_administration(med_id, nurse, status="given")


# ── Lifecycle on: what the interaction screen compares with ──────────────────

def test_warfarin_alerts_against_a_prn_order(doctor, lifecycle):
    pid = _patient(doctor)
    _charted(pid, doctor, "Ibuprofen", status="prn", dose="400 mg", freq="PRN")
    alerts, _ = drug_safety_service._screen_interactions(pid, "Warfarin")
    assert [a["with"] for a in alerts] == ["Ibuprofen"]
    assert alerts[0]["severity"] == "severe" and alerts[0]["with_status"] == "as needed"
    assert "(as needed)" in alerts[0]["message"]


def test_warfarin_alerts_against_a_signed_cpoe_anticoagulant_not_a_draft(doctor, lifecycle):
    pid = _patient(doctor)
    order = _draft(pid, doctor, "Enoxaparin")
    assert _alerts_with(pid) == [], "a draft has reached nothing and is not screened"
    chart_lane.sign_order(order["id"], actor_id=doctor, actor_role="doctor",
                          expected_version=order["version"])
    assert _alerts_with(pid) == ["Enoxaparin"]


def test_a_held_order_is_screened_and_an_ended_one_is_not(doctor, lifecycle):
    pid = _patient(doctor)
    _, held = _charted(pid, doctor, "Enoxaparin", dose="40 mg", route="SC")
    _hold(held)
    alerts, _ = drug_safety_service._screen_interactions(pid, "Warfarin")
    assert [(a["with"], a.get("with_status")) for a in alerts] == [("Enoxaparin", "on hold")]
    _end(held)
    assert _alerts_with(pid) == []


def test_unlinked_and_broken_link_rows_are_screened(doctor, lifecycle):
    pid = _patient(doctor)
    _ward_row(pid, "Heparin", status="prn", dose="5000 units", route="SC")
    broken = _ward_row(pid, "Ibuprofen", dose="400 mg")
    with db_session() as db:
        db.execute("UPDATE medications SET med_order_id = ? WHERE id = ?",
                   (f"missing-{uuid.uuid4()}", broken))
    assert sorted(_alerts_with(pid)) == ["Heparin", "Ibuprofen"]


def test_an_outpatient_line_is_left_to_the_outpatient_lane(doctor, lifecycle):
    pid = _patient(doctor)
    with db_session(immediate=True) as conn:
        core.create_in_txn(conn, patient_id=pid, drug_name="Ibuprofen", source="opd_prescription",
                           status="prescribed", entered_by=doctor, actor_id=doctor,
                           entry_point=outpatient.ENTRY_POINT, encounter_type="op",
                           dose="400 mg", route="Oral", frequency="TDS", prescriber_id=doctor)
    assert _alerts_with(pid) == []
    with db_session() as db:
        assert screening.comparison_rows(db, pid) == []
        assert [r["drug_name"] for r in screening.outpatient_rows(db, pid)] == ["Ibuprofen"]


def test_the_screened_order_never_meets_itself(doctor, lifecycle):
    pid = _patient(doctor)
    med_id, order_id = _charted(pid, doctor, "Enoxaparin", dose="40 mg", route="SC")
    assert _alerts_with(pid, "Enoxaparin") == ["Enoxaparin"]
    assert _alerts_with(pid, "Enoxaparin", exclude_med_id=med_id) == []
    assert _alerts_with(pid, "Enoxaparin", exclude_order_id=order_id) == []
    # Through the gate: the exclusion is on the audit row.
    enforce_medication_safety(pid, "Enoxaparin", actor_id=doctor, entry_point="test.resume",
                              actor_role="doctor", exclude_order_id=order_id)
    [audit] = [a for a in _audits("drug_safety_screen", pid)
               if json.loads(a["metadata_json"]).get("entry_point") == "test.resume"]
    meta = json.loads(audit["metadata_json"])
    assert meta["excluded_med_order_id"] == order_id and meta["interaction_alerts"] == []


def test_the_comparison_set_is_what_the_screen_fingerprint_stamps(doctor, lifecycle):
    """A drug that changes the screen changes the stamp, so a step that
    screened before its write transaction sees the change (screen_stale)."""
    pid = _patient(doctor)
    with db_session() as db:
        before = core.screen_fingerprint_in_txn(db, pid)
    _charted(pid, doctor, "Ibuprofen", status="prn", dose="400 mg", freq="PRN")
    with db_session() as db:
        assert core.screen_fingerprint_in_txn(db, pid) != before
        before = core.screen_fingerprint_in_txn(db, pid)
    _ward_row(pid, "Heparin", dose="5000 units", route="SC")
    with db_session() as db:
        assert core.screen_fingerprint_in_txn(db, pid) != before


# ── Lifecycle on: a PRN drug is givable through the full bedside gate set ────

def test_a_prn_dose_passes_the_full_bedside_gate_set(doctor, nurse, lifecycle):
    pid = _patient(doctor)
    med_id, order_id = _charted(pid, doctor, "Paracetamol", status="prn", dose="1 g",
                                freq="PRN")
    given = emar_service.record_administration(med_id, nurse, status="given")
    assert given["status"] == "given"
    # The order's ledger has the dose, as for any linked drug.
    with db_session() as db:
        kinds = [e["kind"] for e in core.events_in_txn(db, order_id)]
    assert kinds[-1] == "administered"
    # A second PRN dose at once: the repeat window sees the first one.
    again = emar_service.record_administration(med_id, nurse, status="given")
    assert (again["dose_check"] or {}).get("duplicate"), again
    # Withholding is always easy.
    assert emar_service.record_administration(
        med_id, nurse, status="not_given", not_given_reason="Pain settled")["status"] == "not_given"


def test_a_held_prn_order_refuses_given(doctor, nurse, lifecycle):
    pid = _patient(doctor)
    med_id, order_id = _charted(pid, doctor, "Ondansetron", status="prn", dose="4 mg",
                                route="IV", freq="PRN")
    _hold(order_id)
    with pytest.raises(emar_check.OrderStateRefused):
        emar_service.record_administration(med_id, nurse, status="given")


def test_an_unverified_ai_prn_row_needs_the_pharmacist(doctor, nurse, lifecycle):
    pid = _patient(doctor)
    med_id = _ward_row(pid, "Paracetamol", status="prn", dose="1 g", freq="PRN",
                       source="llm_extracted")
    with pytest.raises(ValueError, match="pharmacist verification required"):
        emar_service.record_administration(med_id, nurse, status="given")


def test_the_pharmacist_check_before_the_first_dose_covers_prn(doctor, nurse, lifecycle,
                                                                monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_VERIFY_BEFORE_DOSE_ENABLED", True)
    pid = _patient(doctor)
    med_id, _ = _charted(pid, doctor, "Paracetamol", status="prn", dose="1 g", freq="PRN")
    with pytest.raises(emar_check.OrderStateRefused) as refused:
        emar_service.record_administration(med_id, nurse, status="given")
    assert refused.value.verdict["code"] == emar_check.NOT_VERIFIED_CODE


def test_a_severe_allergy_stops_a_prn_dose(doctor, nurse, lifecycle):
    pid = _patient(doctor)
    med_id, _ = _charted(pid, doctor, "Morphine", status="prn", dose="2 mg", route="IV",
                         freq="PRN")
    add_allergy(pid, "morphine", reaction="anaphylaxis", severity="severe")
    with pytest.raises(AllergyInterlockBlocked):
        emar_service.record_administration(med_id, nurse, status="given")


def test_the_dose_guard_stops_a_prn_dose(doctor, nurse, lifecycle):
    pid = _patient(doctor)
    med_id, _ = _charted(pid, doctor, "Midazolam", status="prn", dose="5mg", route="IV",
                         freq="PRN")
    with pytest.raises(DoseGuardBlocked):
        emar_service.record_administration(med_id, nurse, status="given", dose_given="50mg")


def test_a_discharged_patient_gets_no_prn_dose(doctor, nurse, lifecycle):
    pid = _patient(doctor)
    med_id, _ = _charted(pid, doctor, "Paracetamol", status="prn", dose="1 g", freq="PRN")
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
    with pytest.raises(ValueError, match="not admitted"):
        emar_service.record_administration(med_id, nurse, status="given")


# ── Lifecycle on: the same-ingredient duplicate (MO-R6) ──────────────────────

def test_the_default_rule_blocks_a_second_order_and_nothing_clears_it(doctor, lifecycle):
    assert config.MED_ORDER_DUPLICATE_RULE == "block"
    pid = _patient(doctor)
    _charted(pid, doctor, "Paracetamol", dose="1 g", freq="QDS")
    ack = {"acknowledged": True, "reason": "IV while nil by mouth, oral doses not given"}
    for given_ack in (None, ack):
        with pytest.raises(HTTPException) as refused:
            enforce_medication_safety(pid, "Paracetamol 500 mg", actor_id=doctor,
                                      entry_point="vigil.med_add", actor_role="doctor",
                                      safety_ack=given_ack)
        assert refused.value.status_code == 409
        detail = refused.value.detail
        assert detail["error"] == screening.DUPLICATE_ERROR
        check = detail["safety"]["duplicate_check"]
        assert check["rule"] == "block" and check["decision"] == "block"
        assert [f["drug_name"] for f in check["findings"]] == ["Paracetamol"]
        assert "not allowed" in detail["message"] and "Paracetamol 1 g" in detail["message"]
    blocks = _audits("duplicate_order_block", pid)
    assert len(blocks) == 2 and blocks[0]["outcome"] == "denied"
    assert json.loads(blocks[0]["metadata_json"])["duplicate_check"]["findings"]
    assert _audits("duplicate_order_override", pid) == []


def test_a_non_interactive_lane_is_blocked_with_the_reason(doctor, lifecycle):
    pid = _patient(doctor)
    _charted(pid, doctor, "Paracetamol", dose="1 g", freq="QDS")
    with pytest.raises(MedicationSafetyBlocked) as blocked:
        enforce_medication_safety(pid, "Paracetamol", actor_id=doctor,
                                  entry_point="sage.agentic_handoff", interactive=False)
    assert "already ordered" in blocked.value.reason
    assert blocked.value.safety["duplicate_check"]["findings"]


def test_under_acknowledge_a_reason_clears_it_and_a_reflex_does_not(doctor, acknowledge):
    pid = _patient(doctor)
    _charted(pid, doctor, "Paracetamol", dose="1 g", freq="QDS")
    with pytest.raises(HTTPException) as asked:
        enforce_medication_safety(pid, "Paracetamol 500 mg", actor_id=doctor,
                                  entry_point="medorder.sign.cpoe", actor_role="doctor")
    assert asked.value.status_code == 428
    assert asked.value.detail["error"] == "drug_safety_acknowledgement_required"
    assert "Give a reason" in asked.value.detail["message"]
    with pytest.raises(HTTPException) as reflex:
        enforce_medication_safety(pid, "Paracetamol 500 mg", actor_id=doctor,
                                  entry_point="medorder.sign.cpoe", actor_role="doctor",
                                  safety_ack={"acknowledged": True, "reason": "ok"})
    assert reflex.value.status_code == 428
    assert reflex.value.detail["safety"]["ack_refused"]
    reason = "Top-up dose before theatre, regular dose held"
    safety = enforce_medication_safety(
        pid, "Paracetamol 500 mg", actor_id=doctor, entry_point="medorder.sign.cpoe",
        actor_role="doctor", safety_ack={"acknowledged": True, "reason": reason})
    assert safety["duplicate_check"]["decision"] == "acknowledged"
    assert safety["duplicate_check"]["override"]["reason"] == reason
    [override] = _audits("duplicate_order_override", pid)
    meta = json.loads(override["metadata_json"])
    assert meta["override_reason"] == reason and meta["duplicate_check"]["findings"]
    assert override["outcome"] == "partial_success"


def test_under_acknowledge_a_non_interactive_lane_is_still_blocked(doctor, acknowledge):
    pid = _patient(doctor)
    _charted(pid, doctor, "Paracetamol", dose="1 g", freq="QDS")
    with pytest.raises(MedicationSafetyBlocked):
        enforce_medication_safety(pid, "Paracetamol", actor_id=doctor,
                                  entry_point="scribe.auto_extraction", interactive=False)


def test_the_rule_runs_only_at_order_time_entry_points(doctor, lifecycle):
    pid = _patient(doctor)
    _charted(pid, doctor, "Paracetamol", dose="1 g", freq="QDS")
    for entry in ("helix.verified", "orders.cpoe", "order_set.apply", "test.resume"):
        safety = enforce_medication_safety(pid, "Paracetamol", actor_id=doctor,
                                           entry_point=entry, actor_role="doctor")
        assert "duplicate_check" not in safety, entry


def test_the_order_itself_is_never_its_own_duplicate(doctor, lifecycle):
    pid = _patient(doctor)
    med_id, order_id = _charted(pid, doctor, "Paracetamol", dose="1 g", freq="QDS")
    enforce_medication_safety(pid, "Paracetamol", actor_id=doctor, entry_point="vigil.med_add",
                              actor_role="doctor", exclude_med_id=med_id)
    enforce_medication_safety(pid, "Paracetamol", actor_id=doctor, entry_point="vigil.med_add",
                              actor_role="doctor", exclude_order_id=order_id)


def test_a_replay_is_never_its_own_duplicate(doctor, lifecycle):
    """The offline client resends a chart add or a create with the same
    Idempotency-Key; the replay is screened with its first write left out and
    answers the first write, never a 409 the client would treat as lost."""
    pid = _patient(doctor)
    key = f"w9c-chart-{uuid.uuid4()}"
    first = chart_lane.create_chart_order(pid, "Paracetamol", "1 g", "Oral", "QDS", "active",
                                          actor_id=doctor, actor_role="doctor",
                                          idempotency_key=key)
    again = chart_lane.create_chart_order(pid, "Paracetamol", "1 g", "Oral", "QDS", "active",
                                          actor_id=doctor, actor_role="doctor",
                                          idempotency_key=key)
    assert again["replayed"] is True and again["id"] == first["id"]
    assert "duplicate_check" not in again["safety"]
    # A new request for the same medicine is still refused.
    with pytest.raises(HTTPException) as refused:
        chart_lane.create_chart_order(pid, "Paracetamol", "1 g", "Oral", "QDS", "active",
                                      actor_id=doctor, actor_role="doctor",
                                      idempotency_key=f"w9c-chart-{uuid.uuid4()}")
    assert refused.value.status_code == 409

    pid2 = _patient(doctor)
    key2 = f"w9c-create-{uuid.uuid4()}"
    made = chart_lane.create_order(patient_id=pid2, drug_name="Ibuprofen", status="prescribed",
                                   actor_id=doctor, actor_role="doctor", dose="400 mg",
                                   route="Oral", frequency="TDS", idempotency_key=key2)
    replay = chart_lane.create_order(patient_id=pid2, drug_name="Ibuprofen", status="prescribed",
                                     actor_id=doctor, actor_role="doctor", dose="400 mg",
                                     route="Oral", frequency="TDS", idempotency_key=key2)
    assert replay["replayed"] is True and replay["order"]["id"] == made["order"]["id"]


def test_what_counts_as_the_same_ingredient(doctor, lifecycle):
    pid = _patient(doctor)
    _charted(pid, doctor, "Insulin Glargine", dose="10 units", route="SC")
    _charted(pid, doctor, "Paracetamol 1 g", dose="1 g", freq="QDS")
    with db_session() as db:
        def found(drug):
            return [f["drug_name"] for f in screening.find_duplicates(db, pid, drug)]
        # Basal and bolus insulin are two drugs.
        assert found("Insulin Aspart") == []
        # Two strengths of one drug are one drug; so is a combination with it.
        assert found("Paracetamol 500 mg") == ["Paracetamol 1 g"]
        assert found("Paracetamol + Codeine") == ["Paracetamol 1 g"]
        # A brand meets its generic through the hospital's catalogue.
        assert found("Dolvexa 650") == ["Paracetamol 1 g"]
        assert found("Ibuprofen 400 mg") == []


#: Different medicines that share a class word, a salt or an anion. The old
#: form dropped short words and cations, leaving 'vitamin' or 'carbonate',
#: and the substring rule then joined them: a hard 409 under 'block'.
_NOT_THE_SAME = (
    ("Vitamin K 10 mg", "Vitamin D3"),
    ("Sodium Bicarbonate 8.4% w/v", "Calcium Carbonate"),
    ("Vitamin C", "Vitamin B12"),
    ("Vitamin B1", "Vitamin B12"),
    ("Calcium Gluconate", "Zinc Gluconate"),
    ("Magnesium Hydroxide", "Aluminium Hydroxide"),
    ("Calcium Lactate", "Sodium Lactate"),
    ("Sodium Chloride 0.9%", "Potassium Chloride"),
    ("Calcium 500 mg", "Atorvastatin Calcium 10 mg"),
    ("Potassium", "Losartan Potassium 50 mg"),
    ("Insulin", "Insulin Glargine"),
    ("Vitamin", "Vitamin D3"),
    ("Insulin Aspart", "Insulin Glargine"),
)
#: One ingredient, however it is written.
_THE_SAME = (
    ("Paracetamol 500 mg", "Paracetamol 1 g"),
    ("Paracetamol500mg", "Paracetamol 650"),
    ("Losartan Potassium 50 mg", "Losartan 25 mg"),
    ("Metoprolol Succinate", "Metoprolol Tartrate"),
    ("Diclofenac Sodium", "Diclofenac Potassium"),
    ("Sodium Valproate", "Valproate Sodium"),
    ("Metformin 500 mg", "Metformin Hydrochloride SR 1 g"),
    ("Lithium Carbonate", "Lithium Citrate"),
    ("Calcium Carbonate 500 mg", "Calcium Carbonate 1250 mg"),
    ("Vitamin K 10 mg", "Vitamin K"),
    ("Calcium + Vitamin D3", "Vitamin D3"),
    ("Prednisolone", "Methylprednisolone"),
)


def test_different_medicines_sharing_a_word_are_not_one_ingredient():
    for a, b in _NOT_THE_SAME:
        assert not screening.same_ingredient(a, b), (a, b)
        assert not screening.same_ingredient(b, a), (b, a)
    for a, b in _THE_SAME:
        assert screening.same_ingredient(a, b), (a, b)
        assert screening.same_ingredient(b, a), (b, a)
    # The seed catalogue's bare generics ('Insulin', 'Potassium') never join
    # two insulins, or a potassium supplement to a potassium salt of a drug.
    seed = [("insulin aspart 100 units/ml injection", "insulin"),
            ("potassium chloride 20meq injection", "potassium")]
    assert not screening.same_ingredient("Insulin Aspart", "Insulin Glargine", seed)
    assert not screening.same_ingredient("Potassium Chloride", "Losartan Potassium", seed)
    # A bare class word resolves through the catalogue by exact name only.
    vitamins = [("vitamin d3 60000 iu", "cholecalciferol"), ("vitamin k 10mg", "phytomenadione")]
    assert not screening.same_ingredient("Vitamin", "Vitamin K 10 mg", vitamins)
    assert screening.same_ingredient("Vitamin K 10mg", "Phytomenadione", vitamins)
    assert screening.ingredient_word_set("Vitamin K 10mg Injection") == {"vitamin", "k"}


def test_a_vitamin_k_reversal_and_a_bicarbonate_are_not_refused(doctor, lifecycle):
    """The reviewer's two probes through the gate under the default 'block'."""
    assert config.MED_ORDER_DUPLICATE_RULE == "block"
    ack = {"acknowledged": True, "reason": "Warfarin reversal before the procedure"}
    pid = _patient(doctor)
    _charted(pid, doctor, "Vitamin D3", dose="60000 IU", freq="Weekly")
    safety = enforce_medication_safety(pid, "Vitamin K 10 mg", actor_id=doctor,
                                       entry_point="vigil.med_add", actor_role="doctor",
                                       safety_ack=ack)
    assert "duplicate_check" not in safety
    pid2 = _patient(doctor)
    _charted(pid2, doctor, "Calcium Carbonate", dose="500 mg", freq="TDS")
    safety = enforce_medication_safety(pid2, "Sodium Bicarbonate 8.4%", actor_id=doctor,
                                       entry_point="vigil.med_add", actor_role="doctor",
                                       safety_ack=ack)
    assert "duplicate_check" not in safety
    # The same medicine is still one: a second Vitamin D3 is refused.
    with pytest.raises(HTTPException) as refused:
        enforce_medication_safety(pid, "Vitamin D3 1000 IU", actor_id=doctor,
                                  entry_point="vigil.med_add", actor_role="doctor",
                                  safety_ack=ack)
    assert refused.value.status_code == 409


#: A fixed-dose combination, and another product under the coined brand
#: whose 650 tablet is paracetamol (BRAND_NAME), as a formulary lists them.
_COMBINATION_ROWS = (
    ("HXW9CMETGLM", "Metformin + Glimepiride 500mg/1mg Tablet", "Metformin + Glimepiride"),
    ("HXW9CDLVCLD", "Dolvexa Cold Tablet", "Paracetamol + Phenylephrine + Cetirizine"),
)


@pytest.fixture()
def combination_rows():
    with db_session() as db:
        for row_id, name, generic in _COMBINATION_ROWS:
            db.execute("INSERT OR IGNORE INTO helix_medication_master "
                       "(id, name, generic_name, form, strength) VALUES (?, ?, ?, 'tablet', '')",
                       (row_id, name, generic))
    yield
    with db_session() as db:
        db.execute("DELETE FROM helix_medication_master WHERE id IN (?, ?)",
                   tuple(row_id for row_id, _, _ in _COMBINATION_ROWS))


def test_a_longer_catalogue_name_counts_only_as_the_same_product():
    """Round 2 review: 'Metformin' took the generic of the combination tablet
    whose name starts with it, so a live glimepiride read as a second
    metformin; 'Dolvexa' took every ingredient of the brand's cold remedy. A
    catalogue name longer than the charted one counts only when it adds a
    strength, a unit, a form or an anion."""
    catalogue = [(name, generic) for _, name, generic in _COMBINATION_ROWS] + [
        (BRAND_NAME, "paracetamol"),
        ("Zentolar Succinate 50mg Tablet", "Metoprolol"),
        ("Lantorin 100 units/ml Injection", "Insulin Glargine"),
        ("Combivexa Plus Tablet", "Ibuprofen + Paracetamol + Caffeine"),
        ("Calvexa D3 Calcium Tablet", "Cholecalciferol + Calcium Carbonate"),
        ("Augmentin 625 DUO Tablet", "Amoxicillin/Clavulanate"),
    ]
    different = (
        ("Metformin", "Glimepiride 1 mg"),
        ("Metformin", "Glimepiride"),
        ("Dolvexa", "Cetirizine"),
        ("Dolvexa", "Phenylephrine"),
        ("Combivexa", "Caffeine"),
        ("Calvexa D3", "Calcium Carbonate"),
    )
    for a, b in different:
        assert not screening.same_ingredient(a, b, catalogue), (a, b)
        assert not screening.same_ingredient(b, a, catalogue), (b, a)
    same = (
        # A combination still meets each of its parts.
        ("Metformin + Glimepiride", "Glimepiride"),
        ("Metformin + Glimepiride", "Metformin 500 mg"),
        # A brand still meets its generic through a row that adds a strength,
        # a unit, a form or an anion.
        ("Dolvexa", "Paracetamol"),
        ("Zentolar", "Metoprolol"),
        ("Lantorin", "Insulin Glargine"),
        ("Augmentin 625 Duo", "Amoxicillin"),
    )
    for a, b in same:
        assert screening.same_ingredient(a, b, catalogue), (a, b)
        assert screening.same_ingredient(b, a, catalogue), (b, a)
    # The price: a brand charted bare meets nothing through a row that adds a
    # variant word, because on some brands that word adds a second drug.
    assert not screening.same_ingredient("Augmentin", "Amoxicillin", catalogue)


def test_a_combination_tablet_never_refuses_a_different_medicine(doctor, lifecycle,
                                                                 combination_rows):
    """The reviewer's probe through the gate under the default 'block', both
    directions, with the combination and the brand's cold remedy on the
    hospital's catalogue."""
    assert config.MED_ORDER_DUPLICATE_RULE == "block"
    ack = {"acknowledged": True, "reason": "Second agent for poor glycaemic control"}
    pid = _patient(doctor)
    _charted(pid, doctor, "Glimepiride", dose="1 mg")
    safety = enforce_medication_safety(pid, "Metformin", actor_id=doctor,
                                       entry_point="vigil.med_add", actor_role="doctor",
                                       dose="500 mg", route="Oral", safety_ack=ack)
    assert "duplicate_check" not in safety
    pid2 = _patient(doctor)
    _charted(pid2, doctor, "Metformin", dose="500 mg", freq="BD")
    safety = enforce_medication_safety(pid2, "Glimepiride", actor_id=doctor,
                                       entry_point="vigil.med_add", actor_role="doctor",
                                       dose="1 mg", route="Oral", safety_ack=ack)
    assert "duplicate_check" not in safety
    pid3 = _patient(doctor)
    _charted(pid3, doctor, "Cetirizine", dose="10 mg")
    safety = enforce_medication_safety(pid3, "Dolvexa", actor_id=doctor,
                                       entry_point="vigil.med_add", actor_role="doctor",
                                       dose="650 mg", route="Oral", safety_ack=ack)
    assert "duplicate_check" not in safety
    # The combination still meets the glimepiride it holds.
    with pytest.raises(HTTPException) as refused:
        enforce_medication_safety(pid, "Metformin + Glimepiride", actor_id=doctor,
                                  entry_point="vigil.med_add", actor_role="doctor",
                                  safety_ack=ack)
    assert refused.value.status_code == 409
    assert refused.value.detail["error"] == screening.DUPLICATE_ERROR
    # The brand still meets its generic through its 650 tablet.
    pid4 = _patient(doctor)
    _charted(pid4, doctor, "Paracetamol", dose="1 g", freq="QDS")
    with pytest.raises(HTTPException) as refused:
        enforce_medication_safety(pid4, "Dolvexa", actor_id=doctor,
                                  entry_point="vigil.med_add", actor_role="doctor",
                                  safety_ack=ack)
    assert refused.value.status_code == 409
    assert refused.value.detail["error"] == screening.DUPLICATE_ERROR


def test_the_outpatient_lane_also_meets_its_unended_lines(doctor, lifecycle):
    pid = _outpatient()
    visit = _opd_visit(pid)
    _opd_line(pid, doctor, "Paracetamol", visit, freq="TDS")
    _opd_line(pid, doctor, "Ibuprofen", visit, dose="400 mg", freq="TDS",
              end_at="2020-01-01T00:00:00+00:00")
    with db_session() as db:
        assert screening.find_duplicates(db, pid, "Paracetamol 650 mg") == []
        [finding] = screening.find_duplicates(db, pid, "Paracetamol 650 mg",
                                              include_outpatient=True, opd_visit_id=visit)
        assert finding["setting"] == "outpatient" and finding["clears_with_reason"] is False
        assert "on this visit" in finding["description"]
        # A course that has ended is not live.
        assert screening.find_duplicates(db, pid, "Ibuprofen", include_outpatient=True,
                                         opd_visit_id=visit) == []
    # A second line on the same visit keeps the block: the prescriber can
    # cancel the first one on this visit.
    with pytest.raises(HTTPException) as refused:
        enforce_medication_safety(pid, "Paracetamol 650 mg", actor_id=doctor,
                                  entry_point=outpatient.ENTRY_POINT, actor_role="doctor",
                                  opd_visit_id=visit,
                                  safety_ack={"acknowledged": True,
                                              "reason": "Higher dose wanted for this visit"})
    assert refused.value.status_code == 409


def test_a_refill_on_a_later_visit_clears_with_a_reason(doctor, lifecycle):
    """A chronic outpatient line with no duration never ends, and only its own
    visit can cancel it: on a later visit it is not a hard block."""
    assert config.MED_ORDER_DUPLICATE_RULE == "block"
    pid = _outpatient()
    first = _opd_visit(pid, status="completed", days_ago=30)
    _opd_line(pid, doctor, "Metformin", first)
    later = _opd_visit(pid)
    with pytest.raises(HTTPException) as asked:
        enforce_medication_safety(pid, "Metformin 500 mg", actor_id=doctor,
                                  entry_point=outpatient.ENTRY_POINT, actor_role="doctor",
                                  opd_visit_id=later)
    assert asked.value.status_code == 428
    check = asked.value.detail["safety"]["duplicate_check"]
    assert check["rule"] == "acknowledge" and check["decision"] == "acknowledge"
    assert check["findings"][0]["clears_with_reason"] is True
    assert "from another visit" in check["findings"][0]["description"]
    assert "refill" in asked.value.detail["message"]
    with pytest.raises(HTTPException) as reflex:
        enforce_medication_safety(pid, "Metformin 500 mg", actor_id=doctor,
                                  entry_point=outpatient.ENTRY_POINT, actor_role="doctor",
                                  opd_visit_id=later,
                                  safety_ack={"acknowledged": True, "reason": "ok"})
    assert reflex.value.status_code == 428 and reflex.value.detail["safety"]["ack_refused"]
    reason = "Monthly refill, sugars controlled on this dose"
    safety = enforce_medication_safety(
        pid, "Metformin 500 mg", actor_id=doctor, entry_point=outpatient.ENTRY_POINT,
        actor_role="doctor", opd_visit_id=later,
        safety_ack={"acknowledged": True, "reason": reason})
    assert safety["duplicate_check"]["decision"] == "acknowledged"
    [override] = _audits("duplicate_order_override", pid)
    assert json.loads(override["metadata_json"])["override_reason"] == reason
    # One line the prescriber could cancel on this visit keeps the block.
    _opd_line(pid, doctor, "Metformin", later)
    with pytest.raises(HTTPException) as refused:
        enforce_medication_safety(pid, "Metformin 500 mg", actor_id=doctor,
                                  entry_point=outpatient.ENTRY_POINT, actor_role="doctor",
                                  opd_visit_id=later,
                                  safety_ack={"acknowledged": True, "reason": reason})
    assert refused.value.status_code == 409


def test_a_finished_stays_orders_never_block_a_clinic_prescription(doctor, lifecycle):
    """Nothing closes an inpatient order at discharge while MO-R7 is held, so
    the clinic leaves inpatient rows out unless the patient is admitted."""
    pid = _patient(doctor)
    _charted(pid, doctor, "Amlodipine", dose="5 mg")
    with db_session() as db:
        # Still admitted: the inpatient row counts (the lane refuses an
        # admitted patient anyway; the screen stays strict).
        assert [f["drug_name"] for f in screening.find_duplicates(
            db, pid, "Amlodipine 5 mg", include_outpatient=True)] == ["Amlodipine"]
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
    visit = _opd_visit(pid)
    safety = enforce_medication_safety(pid, "Amlodipine 5 mg", actor_id=doctor,
                                       entry_point=outpatient.ENTRY_POINT, actor_role="doctor",
                                       opd_visit_id=visit)
    assert "duplicate_check" not in safety
    # The inpatient lanes still see the live order (readmission is theirs).
    with db_session() as db:
        assert [f["drug_name"] for f in screening.find_duplicates(
            db, pid, "Amlodipine 5 mg")] == ["Amlodipine"]


def test_a_caller_naming_no_visit_is_read_strictly(doctor, lifecycle):
    pid = _outpatient()
    _opd_line(pid, doctor, "Atorvastatin", _opd_visit(pid, status="completed", days_ago=10))
    _opd_line(pid, doctor, "Amlodipine", None)
    _opd_line(pid, doctor, "Pantoprazole", _opd_visit(pid))
    with db_session() as db:
        def clears(drug):
            return [f["clears_with_reason"] for f in screening.find_duplicates(
                db, pid, drug, include_outpatient=True)]
        # A finished visit's line, and a line whose visit cannot be read,
        # clear with a reason; a line on today's open visit keeps the rule.
        assert clears("Atorvastatin 10 mg") == [True]
        assert clears("Amlodipine 5 mg") == [True]
        assert clears("Pantoprazole 40 mg") == [False]
    with pytest.raises(HTTPException) as refused:
        enforce_medication_safety(pid, "Pantoprazole 40 mg", actor_id=doctor,
                                  entry_point=outpatient.ENTRY_POINT, actor_role="doctor")
    assert refused.value.status_code == 409
    with pytest.raises(HTTPException) as asked:
        enforce_medication_safety(pid, "Atorvastatin 10 mg", actor_id=doctor,
                                  entry_point=outpatient.ENTRY_POINT, actor_role="doctor")
    assert asked.value.status_code == 428


def test_rule_for_findings():
    other = {"clears_with_reason": True}
    mine = {"clears_with_reason": False}
    assert screening.rule_for_findings([other], "block") == "acknowledge"
    assert screening.rule_for_findings([other, mine], "block") == "block"
    assert screening.rule_for_findings([mine], "acknowledge") == "acknowledge"
    assert screening.rule_for_findings([], "block") == "block"
    assert screening.rule_for_findings([other], None) is None


def test_the_lane_names_its_visit_and_a_refill_goes_through(doctor, lifecycle, monkeypatch):
    """End to end through outpatient.write_prescription: the lane passes its
    visit to the gate, so a refill on a later visit asks for a reason and a
    reason writes it."""
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(outpatient, "_require_allergy_review",
                        lambda conn, pid, vid: {"status": "none_known", "basis": "test",
                                                "can_sign": True})
    seen: list = []
    real_gate = outpatient.medication_safety_gate.enforce_medication_safety

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("opd_visit_id"))
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(outpatient.medication_safety_gate, "enforce_medication_safety", _spy)
    pid = _outpatient()
    first = _opd_visit(pid, days_ago=1)
    line = {"drug_name": "Metformin", "dose": "500 mg", "route": "Oral", "frequency": "BD"}
    outpatient.write_prescription(first, line, actor_id=doctor, actor_role="doctor")
    with db_session() as db:
        db.execute("UPDATE opd_visits SET status = 'completed' WHERE id = ?", (first,))
    later = _opd_visit(pid)
    with pytest.raises(HTTPException) as asked:
        outpatient.write_prescription(later, line, actor_id=doctor, actor_role="doctor")
    assert asked.value.status_code == 428
    assert asked.value.detail["safety"]["duplicate_check"]["decision"] == "acknowledge"
    done = outpatient.write_prescription(
        later, line, actor_id=doctor, actor_role="doctor",
        safety_ack={"acknowledged": True, "reason": "Monthly refill of a continuing medicine"})
    assert done["prescription"]["opd_visit_id"] == later
    assert seen == [first, later, later]


def test_a_failed_duplicate_read_is_a_failed_screen(doctor, lifecycle, monkeypatch):
    pid = _patient(doctor)

    def _boom(*args, **kwargs):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(screening, "find_duplicates", _boom)
    with pytest.raises(HTTPException) as failed:
        enforce_medication_safety(pid, "Paracetamol", actor_id=doctor,
                                  entry_point="vigil.med_add", actor_role="doctor")
    assert failed.value.status_code == 503
    with pytest.raises(MedicationSafetyBlocked):
        enforce_medication_safety(pid, "Paracetamol", actor_id=doctor,
                                  entry_point="sage.agentic_handoff", interactive=False)


def test_the_duplicate_reason_floor():
    for junk in ("", "   ", "ok", "override", "aaaaaaaaaaaa", "short"):
        with pytest.raises(ValueError):
            screening.validate_duplicate_reason(junk)
    assert screening.validate_duplicate_reason(
        "  IV while nil by mouth,   oral not given ") == "IV while nil by mouth, oral not given"


def test_the_switches_and_the_setting():
    assert screening.DUPLICATE_RULES[0] == "block" == config.MED_ORDER_DUPLICATE_RULE
    assert screening.ENTRY_OPD == outpatient.ENTRY_POINT
    assert screening.MIN_DUPLICATE_REASON_CHARS == drug_safety_service.MIN_OVERRIDE_REASON_CHARS


# ── Every entry point the server passes to the gate is classified ────────────

SERVER_DIR = Path(__file__).resolve().parent.parent
_SKIP = {".venv", "tests", "__pycache__", "node_modules", "datasets", "eval", "corpus",
         "models", "uploads", "docs", "downtime_exports"}


def _gate_entry_points() -> dict[str, set]:
    """file -> the entry_point argument text of every enforce_medication_safety
    call in it (a literal, a name or an f-string, as written)."""
    found: dict[str, set] = {}
    for path in SERVER_DIR.rglob("*.py"):
        rel = path.relative_to(SERVER_DIR)
        if _SKIP & set(rel.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "enforce_medication_safety" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in ("enforce_medication_safety", "run_db"):
                continue
            if name == "run_db" and not (node.args and getattr(node.args[0], "id", None)
                                         == "enforce_medication_safety"):
                continue
            for kw in node.keywords:
                if kw.arg == "entry_point":
                    found.setdefault(str(rel), set()).add(ast.unparse(kw.value))
    return found


#: How each call site's entry_point argument reads, and the entry points it
#: can carry. A new call site, or a new value at an old one, fails here until
#: it is classified in services/medorders/screening.py.
CALL_SITES = {
    "routes/vigil_routes.py": {"'vigil.med_add'": {"vigil.med_add"}},
    "services/medorders/chart_lane.py": {
        "ENTRY_CHART_ADD": {"vigil.med_add"},
        "entry_point": {"medorder.create.cpoe", "medorder.sign.cpoe"},
    },
    "routes/orders_routes.py": {"'orders.cpoe'": {"orders.cpoe"},
                                "cpoe.ENTRY_CPOE_CREATE": {"orders.cpoe"}},
    "services/order_sets_service.py": {"entry_point": {"order_set.preview", "order_set.apply"}},
    "routes/relay_routes.py": {"'relay.confirm_agentic'": {"relay.confirm_agentic"}},
    "services/sage_handoff_service.py": {"'sage.agentic_handoff'": {"sage.agentic_handoff"}},
    "services/nervous_system_triggers.py": {"'scribe.auto_extraction'":
                                            {"scribe.auto_extraction"}},
    "services/sage_assessment_service.py": {"'sage_assessment'": {"sage_assessment"}},
    "routes/helix_routes.py": {"'helix.approve'": {"helix.approve"},
                               "f'helix.{req.target_status}'": {"helix.<step>"}},
    "services/medorders/outpatient.py": {"ENTRY_POINT": {"opd_prescription"}},
    "scripts/reset_db_and_seed.py": {"'seed.pharmacy_queue'": {"seed.pharmacy_queue"}},
}


def test_every_gate_entry_point_is_classified():
    found = _gate_entry_points()
    assert set(found) == set(CALL_SITES), sorted(set(found) ^ set(CALL_SITES))
    for path, args in found.items():
        assert args == set(CALL_SITES[path]), (path, sorted(args))
    known = set(screening.DUPLICATE_SCREENED_ENTRY_POINTS) | set(screening.NOT_DUPLICATE_SCREENED)
    assert not set(screening.DUPLICATE_SCREENED_ENTRY_POINTS) & set(
        screening.NOT_DUPLICATE_SCREENED)
    for path, args in CALL_SITES.items():
        for entries in args.values():
            assert entries <= known, (path, entries - known)
    # The named constants say what the table says.
    assert chart_lane.ENTRY_CHART_ADD == "vigil.med_add"
    assert {chart_lane.ENTRY_CREATE, chart_lane.ENTRY_SIGN} == {"medorder.create.cpoe",
                                                                "medorder.sign.cpoe"}
    assert outpatient.ENTRY_POINT == "opd_prescription"
    from services.medorders import cpoe
    assert cpoe.ENTRY_CPOE_CREATE == "orders.cpoe" and cpoe.ENTRY_ORDER_SET_APPLY == "order_set.apply"


# ── Discharge reconciliation reads the store ─────────────────────────────────

def test_lifecycle_off_reconciliation_is_as_before(doctor):
    pid = _patient(doctor)
    _ward_row(pid, "Amlodipine", dose="5 mg")
    _ward_row(pid, "Ondansetron", status="discontinued", dose="4 mg")
    rec = build_medication_reconciliation(pid)
    assert set(rec) == {"continued", "stopped", "unverified", "counts"}
    assert rec["continued"][0] == {"name": "Amlodipine", "dosage": "5 mg", "route": "Oral",
                                   "frequency": "OD", "source": "clinician"}
    assert set(rec["stopped"][0]) == {"name", "dosage", "route", "frequency", "source"}


def test_reconciliation_reads_store_statuses_and_reasons(doctor, lifecycle, monkeypatch):
    pid = _patient(doctor)
    _, stopped = _charted(pid, doctor, "Amlodipine", dose="5 mg")
    _end(stopped, "stopped", "Blood pressure low on two readings")
    rejected_med = _ward_row(pid, "Ceftriaxone", dose="1 g", route="IV", source="llm_extracted")
    with db_session(immediate=True) as conn:
        order = core.create_in_txn(
            conn, patient_id=pid, drug_name="Ceftriaxone", source="scribe_extracted",
            status="prescribed", entered_by=doctor, actor_id=doctor, entry_point="test.scribe",
            needs_verification=True, dose="1 g", route="IV", frequency="OD",
            prescriber_id=doctor, legacy_medication_id=rejected_med).order
        core.link_projection_in_txn(conn, order["id"], rejected_med)
    _, prn = _charted(pid, doctor, "Paracetamol", status="prn", dose="1 g", freq="PRN")
    _, held = _charted(pid, doctor, "Furosemide", dose="40 mg")
    _hold(held, "Low potassium")
    _draft(pid, doctor, "Enoxaparin")
    # A draft left from an earlier stay is not this discharge's business.
    with db_session(immediate=True) as conn:
        conn.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                     "admitted_by, created_at) VALUES (?, ?, ?, 'W9C', '3', 'discharged', ?, "
                     "'2026-01-01 08:00:00')", (f"OLD-{pid}", pid, DEPT, doctor))
        core.create_in_txn(conn, patient_id=pid, drug_name="Tramadol", source="cpoe",
                           status="draft", entered_by=doctor, actor_id=doctor,
                           entry_point="test.old_stay", admission_id=f"OLD-{pid}",
                           dose="50 mg", route="Oral", frequency="TDS")
    broken = _ward_row(pid, "Lactulose", dose="10 ml", freq="BD")
    with db_session() as db:
        db.execute("UPDATE medications SET med_order_id = ? WHERE id = ?",
                   (f"missing-{uuid.uuid4()}", broken))

    rec = build_medication_reconciliation(pid)
    by_name = {e["name"]: e for e in rec["continued"] + rec["stopped"]}
    amlodipine = by_name["Amlodipine"]
    assert amlodipine in rec["stopped"]
    assert amlodipine["ended_as"] == "stopped"
    assert amlodipine["stop_reason"] == "Blood pressure low on two readings"
    assert by_name["Paracetamol"]["prn"] is True and by_name["Paracetamol"] in rec["continued"]
    assert by_name["Furosemide"]["on_hold"] is True
    assert by_name["Ceftriaxone"]["needs_verification"] is True
    assert by_name["Lactulose"]["order_record_missing"] is True
    assert [d["name"] for d in rec["unsigned"]] == ["Enoxaparin"]
    assert rec["counts"]["unsigned"] == 1 and "Enoxaparin" not in by_name
    text = _render_med_rec_text(rec)
    assert "[Stopped: Blood pressure low on two readings]" in text
    assert "NOT SIGNED" in text and "Enoxaparin" in text
    assert "ORDER RECORD MISSING" in text

    # Rejected by the pharmacist, with the reason the store recorded.
    with db_session(immediate=True) as conn:
        conn.execute("UPDATE medications SET status = 'discontinued', verified_by = ? "
                     "WHERE id = ?", ("W9CPHARM", rejected_med))
        chart_lane.mirror_legacy_decision_in_txn(
            conn, medication_id=rejected_med, decision="rejected", actor_id="W9CPHARM",
            actor_role="pharmacist", reason="Not indicated: no infection on review")
    rec = build_medication_reconciliation(pid)
    ceftriaxone = next(e for e in rec["stopped"] if e["name"] == "Ceftriaxone")
    assert ceftriaxone["ended_as"] == "rejected"
    assert ceftriaxone["stop_reason"] == "Not indicated: no infection on review"
    assert "[Rejected by the pharmacist: Not indicated: no infection on review]" in \
        _render_med_rec_text(rec)

    # With every order waiting for a pharmacist (MO-R1), an unchecked one says so.
    monkeypatch.setattr(config, "MED_ORDER_VERIFY_BEFORE_DOSE_ENABLED", True)
    rec = build_medication_reconciliation(pid)
    assert {e["name"] for e in rec["unverified"]} >= {"Paracetamol", "Furosemide"}


def test_the_store_decides_when_the_ward_row_disagrees(doctor, lifecycle):
    """A ward row left 'active' behind an ended order is read as the order
    says: the eMAR refuses it, so the reconciliation must not continue it."""
    pid = _patient(doctor)
    med_id, order_id = _charted(pid, doctor, "Digoxin", dose="125 mcg")
    _end(order_id, "stopped", "Heart rate below 50")
    with db_session() as db:
        db.execute("UPDATE medications SET status = 'active' WHERE id = ?", (med_id,))
    rec = build_medication_reconciliation(pid)
    assert [e["name"] for e in rec["stopped"]] == ["Digoxin"]
    assert rec["stopped"][0]["ended_as"] == "stopped"
    assert "Digoxin" not in {e["name"] for e in rec["continued"]}
