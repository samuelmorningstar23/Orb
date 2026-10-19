"""The chart's medication order list carries names, a supply line and the
last dose (medorders-10, W9-D): GET /api/medorders/patient/{patient_id}.

Pinned:
  * each order names its prescriber and the person who entered it, by name;
  * 'supply' is None for an order no supply row was ever requested for (the
    chart says 'supply not linked'), and otherwise the LATEST supply row's
    state with who dispensed and who received it on the ward;
  * 'last_dose' is the last bedside record on the order's ledger, and names
    the administration record it came from (ref_type, ref_id), so the chart
    can mark a dose given across a charted allergy;
  * one patient's ledger never leaks into another's list;
  * the list still writes nothing (no event, no audit row besides the read).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services.medorders import core

client = TestClient(app)
DEPT = "W9D Summary Ward"
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL WHERE patient_id IN ({marks})",
                   _PATIENTS)


@pytest.fixture(autouse=True)
def lifecycle_on(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)


def _user(role, name):
    uid = f"MOLS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)", (uid, name, role,
                                               DEPT if role in ("doctor", "nurse") else None))
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


def _patient(admitter):
    pid = f"PMOLS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, "Summary Patient", f"MRN-{pid}"))
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                   "admitted_by) VALUES (?, ?, ?, ?, 'B-1', 'active', ?)",
                   (f"ADM-{pid}", pid, DEPT, DEPT, admitter))
    _PATIENTS.append(pid)
    return pid


def _signed_order(doctor_hdr, pid, drug):
    resp = client.post("/api/medorders/orders", headers=doctor_hdr, json={
        "patient_id": pid, "drug_name": drug, "status": "prescribed", "dose": "500 mg",
        "route": "Oral", "frequency": "TDS"})
    assert resp.status_code == 200, resp.text
    return resp.json()["order"]


def _event(order_id, kind, actor, role, *, ref=None, reason=None):
    with db_session(immediate=True) as db:
        order = dict(db.execute("SELECT * FROM med_orders WHERE id = ?", (order_id,)).fetchone())
        core.record_event_in_txn(db, order=order, kind=kind, from_status=order["status"],
                                 to_status=order["status"], actor_id=actor, actor_role=role,
                                 reason=reason, ref_type="helix_order" if ref else None,
                                 ref_id=ref)


def _listed(hdr, pid):
    resp = client.get(f"/api/medorders/patient/{pid}", headers=hdr)
    assert resp.status_code == 200, resp.text
    return {o["id"]: o for o in resp.json()["orders"]}


def test_names_supply_line_and_last_dose():
    doctor, doctor_hdr = _user("doctor", "Dr Asha Menon")
    nurse, _ = _user("nurse", "Nurse Ravi Iyer")
    pharmacist, _ = _user("pharmacist", "Ph Leela Das")
    pid = _patient(doctor)
    bare = _signed_order(doctor_hdr, pid, "Paracetamol")
    supplied = _signed_order(doctor_hdr, pid, "Amoxicillin")

    _event(supplied["id"], "supply_requested", doctor, "doctor", ref="HXA")
    _event(supplied["id"], "dispensed", pharmacist, "pharmacist", ref="HXA")
    _event(supplied["id"], "received", nurse, "nurse", ref="HXA")
    _event(supplied["id"], "administered", nurse, "nurse")
    _event(supplied["id"], "not_given", nurse, "nurse", reason="Refused")

    with db_session() as db:
        events_before = db.execute("SELECT COUNT(*) FROM med_order_events WHERE patient_id = ?",
                                   (pid,)).fetchone()[0]
    orders = _listed(doctor_hdr, pid)

    one = orders[bare["id"]]
    assert one["prescriber_name"] == "Dr Asha Menon" and one["entered_by_name"] == "Dr Asha Menon"
    assert one["supply"] is None and one["last_dose"] is None

    two = orders[supplied["id"]]
    supply = two["supply"]
    assert supply["helix_order_id"] == "HXA" and supply["state"] == "received"
    assert supply["requested_at"] is not None
    assert supply["dispensed"]["by_name"] == "Ph Leela Das"
    assert supply["received"]["by_name"] == "Nurse Ravi Iyer"
    assert two["last_dose"]["kind"] == "not_given" and two["last_dose"]["reason"] == "Refused"
    assert two["last_dose"]["by_name"] == "Nurse Ravi Iyer"

    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM med_order_events WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == events_before, "a read writes no event"


def test_a_new_supply_row_replaces_the_last_one_on_the_line():
    doctor, doctor_hdr = _user("doctor", "Dr Kiran Rao")
    pharmacist, _ = _user("pharmacist", "Ph Sam Paul")
    pid = _patient(doctor)
    order = _signed_order(doctor_hdr, pid, "Ceftriaxone")
    _event(order["id"], "supply_requested", doctor, "doctor", ref="HX1")
    _event(order["id"], "dispensed", pharmacist, "pharmacist", ref="HX1")
    _event(order["id"], "supply_requested", doctor, "doctor", ref="HX2")
    supply = _listed(doctor_hdr, pid)[order["id"]]["supply"]
    assert supply["helix_order_id"] == "HX2" and supply["state"] == "supply_requested"
    assert supply["dispensed"] is None and supply["received"] is None


def test_one_patients_ledger_stays_on_their_own_list():
    doctor, doctor_hdr = _user("doctor", "Dr Neha Shah")
    nurse, _ = _user("nurse", "Nurse Joy Mathew")
    first, second = _patient(doctor), _patient(doctor)
    order = _signed_order(doctor_hdr, first, "Ondansetron")
    _event(order["id"], "administered", nurse, "nurse")
    other = _signed_order(doctor_hdr, second, "Ondansetron")
    assert _listed(doctor_hdr, second)[other["id"]]["last_dose"] is None
    assert _listed(doctor_hdr, first)[order["id"]]["last_dose"]["kind"] == "administered"


def test_the_last_dose_names_the_administration_record_it_came_from():
    """The chart marks a dose given across a charted allergy on the order list
    (src/api/client.ts givenAcrossAllergy, 'on every surface that shows an
    administration'). The marker lives on the administration record, so the
    ledger's last dose must name that record: ref_type and ref_id are the ids
    the eMAR wrote, and they find the record, override reason and all, in the
    chart's own administrations read. End to end, through the real eMAR."""
    from services import emar_service
    from services.clinical_records_service import add_allergy
    from services.medorders import adoption

    doctor, doctor_hdr = _user("doctor", "Dr Override Pillai")
    pid = _patient(doctor)
    with db_session() as db:
        med_id = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source) VALUES (?, 'Ceftriaxone', '2 g', 'IV', 'OD', 'active', ?, "
            "'clinician')", (pid, doctor)).lastrowid
    adoption.adopt_legacy(trigger="script", patient_ids=[pid])
    add_allergy(pid, "Ceftriaxone", reaction="angio-oedema, hypotension (2023)",
                severity="severe")
    reason = ("Allergy de-labelled by immunology after negative skin test and graded "
              "oral challenge; giving under 1:1 observation.")
    rec = emar_service.record_administration(med_id, doctor, status="given",
                                             allergy_override_reason=reason)

    (order,) = [o for o in _listed(doctor_hdr, pid).values()
                if o["legacy_medication_id"] == med_id]
    last = order["last_dose"]
    assert last["kind"] == "administered"
    assert last["ref_type"] == "medication_administration"
    assert last["ref_id"] == str(rec["id"])

    resp = client.get(f"/api/emar/patients/{pid}/administrations", headers=doctor_hdr)
    assert resp.status_code == 200, resp.text
    (joined,) = [a for a in resp.json()["administrations"] if str(a["id"]) == last["ref_id"]]
    assert joined["status"] == "given"
    assert joined["allergy_check"]["override"]["reason"] == reason


def test_a_delivery_names_its_order_only_when_it_has_one():
    """DeliveriesSheet names the order (medorders-10): GET
    /api/helix/orders/awaiting-receipt adds linked_order to a supply row linked
    to a medication order, and leaves an unlinked row exactly as before."""
    from services import helix_service
    from services.helix_db import init_helix_db

    init_helix_db()
    doctor, doctor_hdr = _user("doctor", "Dr Delivery Nair")
    nurse, nurse_hdr = _user("nurse", "Nurse Delivery Bose")
    pid = _patient(doctor)
    order = _signed_order(doctor_hdr, pid, "Metronidazole")
    med = f"MEDMOLS{uuid.uuid4().hex[:6].upper()}"
    linked, plain = f"HXL{uuid.uuid4().hex[:8]}", f"HXP{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute("INSERT INTO helix_medication_master (id, name, generic_name, form, strength) "
                   "VALUES (?, 'Metronidazole 400 mg', 'metronidazole', 'tablet', '400 mg')",
                   (med,))
        for hid, link in ((linked, order["id"]), (plain, None)):
            db.execute("INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, "
                       "dosage, route, frequency, status, med_order_id) "
                       "VALUES (?, ?, ?, '400 mg', 'PO', 'TDS', 'dispensed', ?)",
                       (hid, pid, med, link))
    try:
        rows = {r["id"]: r for r in helix_service.orders_awaiting_receipt(DEPT)}
        assert rows[linked]["linked_order"]["id"] == order["id"]
        assert rows[linked]["linked_order"]["status"] == "prescribed"
        assert "linked_order" not in rows[plain] and "med_order_id" not in rows[plain]
        resp = client.get("/api/helix/orders/awaiting-receipt", headers=nurse_hdr)
        assert resp.status_code == 200, resp.text
        assert {o["id"] for o in resp.json()["orders"]} >= {linked, plain}
    finally:
        with db_session() as db:
            db.execute("DELETE FROM helix_patient_med_orders WHERE id IN (?, ?)", (linked, plain))
            db.execute("DELETE FROM helix_medication_master WHERE id = ?", (med,))
