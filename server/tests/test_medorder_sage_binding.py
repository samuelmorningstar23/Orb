"""Exact formulary binding for the Sage lane (medorders-20, W9-C;
services/medorders/ai_lanes.bind_sage_product and
services/sage_handoff_service._screen_helix).

Pinned here:
  * lifecycle on: the product is the reconciler's match at 0.8, bound
    exactly (med_orders.formulary_id, binding 'reconciled' with its score on
    the order's 'created' and 'supply_requested' events and the audit row);
  * the names screened are the product dispensed: the bound product's name,
    its generic name when a brand does not carry it, and the card's words when
    they read differently, so an allergy to a brand's ingredient blocks it;
  * a substring-boosted wrong match is refused and flagged: 'Vextrabin' is
    part of two products' names and binds neither (the old name match bound
    whichever SQLite returned first), 'Zelqu' is part of one and binds nothing;
  * below the threshold the order stays 'charted-only' in the same words;
  * a product retired between the screen and the write, a screen made without
    a binding, and a card whose drug changed after the screen bind nothing;
  * record mode (store on, lifecycle off) and store off keep the name match.
"""
from __future__ import annotations

import inspect
import json
import uuid

import pytest

import config
from database import db_session
from services import medication_safety_gate
from services import sage_handoff_service as handoff
from services.clinical_records_service import add_allergy
from services.medorders import ai_lanes, chart_lane, core
from services.rxnorm_reconciler import reconcile_medication

DEPT = "W9C Binding Ward"
# Coined names only: tests/test_patient_specific_screen.py reads every word of
# the catalogue as a drug token.
ZEL_ID, ZEL_NAME = "HXW9CZEL", "Zelquorin 250mg Tablet"
VEX_A, VEX_A_NAME = "HXW9CVXA", "Vextrabin Lorazine 100 units/ml Injection"
VEX_B, VEX_B_NAME = "HXW9CVXB", "Vextrabin Morlaspar 100 units/ml Injection"
KAL_ID, KAL_NAME = "HXW9CKAL", "Kalvorex 500 Capsule"
UNLISTED = "Brivolane"
_PRODUCTS = ((ZEL_ID, ZEL_NAME, "zelquorin"),
             (VEX_A, VEX_A_NAME, "vextrabin lorazine"),
             (VEX_B, VEX_B_NAME, "vextrabin morlaspar"),
             (KAL_ID, KAL_NAME, "amoxicillin"))
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _seed_module():
    from services.helix_db import init_helix_db
    init_helix_db()
    with db_session() as db:
        for pid, name, generic in _PRODUCTS:
            db.execute("INSERT OR IGNORE INTO helix_medication_master "
                       "(id, name, generic_name, form, strength) VALUES (?, ?, ?, 'tablet', '1')",
                       (pid, name, generic))
    yield
    marks = ", ".join("?" for _ in _PATIENTS) or "''"
    with db_session() as db:
        if _PATIENTS:
            db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
            db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
            db.execute(f"UPDATE medications SET med_order_id = NULL WHERE patient_id IN ({marks})",
                       _PATIENTS)
            db.execute(f"DELETE FROM helix_patient_med_orders WHERE patient_id IN ({marks})",
                       _PATIENTS)
            db.execute(f"UPDATE admissions SET status = 'discharged' "
                       f"WHERE patient_id IN ({marks})", _PATIENTS)
        db.executemany("DELETE FROM helix_medication_master WHERE id = ?",
                       [(p[0],) for p in _PRODUCTS])


@pytest.fixture()
def lifecycle(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)


@pytest.fixture()
def record_mode(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)


@pytest.fixture(scope="module")
def doctor():
    uid = f"W9CBDOC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, 'doctor', 'x', ?)", (uid, f"Doctor {uid}", DEPT))
    return uid


def _patient(doctor):
    pid = f"PW9CB{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, ?, ?, 'admitted', 'W9C', '9')",
                   (pid, f"Binding {uuid.uuid4().hex[:5]}", f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
                   "admitted_by) VALUES (?, ?, ?, 'W9C', '9', 'active', ?)",
                   (f"ADM-{pid}", pid, DEPT, doctor))
        db.execute("INSERT INTO channels (id, name, type, patient_id) "
                   "VALUES (?, ?, 'case_room', ?)", (f"case-{pid}", f"Case {pid}", pid))
    _PATIENTS.append(pid)
    return pid


def _action(pid, target):
    action = {
        "id": str(uuid.uuid4()), "room_id": f"case-{pid}", "patient_id": pid,
        "action_type": "prescribe", "target": target,
        "rationale": "Start it by mouth twice a day.", "confidence": 0.9,
        "state": "confirmed", "version": 1,
    }
    with db_session() as db:
        db.execute("INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, "
                   "rationale, confidence, state, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (action["id"], action["room_id"], pid, action["action_type"], target,
                    action["rationale"], action["confidence"], action["state"],
                    action["version"]))
    return action


def _q(sql, *params):
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _orders(pid):
    return _q("SELECT * FROM med_orders WHERE patient_id = ? ORDER BY created_at, id", pid)


def _supply(pid):
    return _q("SELECT * FROM helix_patient_med_orders WHERE patient_id = ? ORDER BY created_at",
              pid)


def _events(order_id):
    return _q("SELECT * FROM med_order_events WHERE med_order_id = ? ORDER BY created_at, id",
              order_id)


def _payload(event):
    return json.loads(event["payload_json"] or "null")


@pytest.fixture()
def screened_names(monkeypatch):
    """Every drug name the Sage lane put through the gate, in order."""
    names: list[str] = []
    real = medication_safety_gate.enforce_medication_safety

    def _spy(patient_id, drug_name, **kwargs):
        if kwargs.get("entry_point") == ai_lanes.ENTRY_SAGE:
            names.append(drug_name)
        return real(patient_id, drug_name, **kwargs)

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety", _spy)
    return names


# ── The reconciler's match, bound exactly ─────────────────────────────────────

def test_the_reconciled_product_is_bound_exactly(doctor, lifecycle):
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid, "Zelquorin"), doctor)
    assert result["status"] == "accepted" and result["error_summary"] is None
    [order] = _orders(pid)
    [supply] = _supply(pid)
    assert result["downstream_ref_id"] == supply["id"]
    assert supply["medication_id"] == ZEL_ID and supply["med_order_id"] == order["id"]
    # An exact binding: the order names its product.
    assert order["formulary_id"] == ZEL_ID and order["supply_source"] == "pharmacy"
    # 'Zelquorin' is the product's generic name exactly: the reconciler's 1.0.
    created, requested = _events(order["id"])
    assert _payload(created)["formulary_binding"] == {
        "binding": "reconciled", "score": 1.0, "candidate_product_id": ZEL_ID}
    assert _payload(requested) == {"helix_order_id": supply["id"], "product_id": ZEL_ID,
                                   "binding": "reconciled", "score": 1.0,
                                   "supply_source": "pharmacy"}
    [audit] = _q("SELECT metadata_json FROM audit_logs WHERE action_type = "
                 "'med_order_supply_requested' AND target_id = ?", order["id"])
    meta = json.loads(audit["metadata_json"])
    assert meta["binding"] == "reconciled" and meta["score"] == 1.0


def test_the_screened_name_is_the_dispensed_product(doctor, lifecycle, screened_names):
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid, "Zelquorin"), doctor)
    [supply] = _supply(pid)
    [product] = _q("SELECT name FROM helix_medication_master WHERE id = ?",
                   supply["medication_id"])
    assert screened_names[0] == product["name"] == ZEL_NAME
    # The card's own words too, since they read differently.
    assert screened_names == [ZEL_NAME, "Zelquorin"]
    card = json.loads(_q("SELECT safety_json FROM sage_handoffs WHERE id = ?",
                         result["id"])[0]["safety_json"])
    assert card["screened_as"] == [ZEL_NAME, "Zelquorin"]


def test_a_brand_is_screened_by_its_generic_and_an_allergy_blocks_it(doctor, lifecycle,
                                                                     screened_names):
    pid = _patient(doctor)
    add_allergy(pid, "amoxicillin", reaction="anaphylaxis", severity="severe")
    result = handoff.create_and_dispatch_handoff(_action(pid, "Kalvorex"), doctor)
    assert screened_names[:2] == [KAL_NAME, "amoxicillin"]
    assert result["status"] == "blocked"
    assert "Not charted" in result["error_summary"]
    assert _orders(pid) == [] and _supply(pid) == []
    assert _q("SELECT 1 FROM medications WHERE patient_id = ?", pid) == []


def test_a_brand_with_no_allergy_binds_its_own_product(doctor, lifecycle):
    """'Kalvorex' is only part of its product's name, so the reconciler's
    0.85 is the substring boost; the brand names its product whole (every
    ingredient word of the name), so it binds."""
    match, score = reconcile_medication("Kalvorex")
    assert match["id"] == KAL_ID and score == pytest.approx(0.85)
    assert ai_lanes._unboosted_score("Kalvorex", match) < ai_lanes.RECONCILE_THRESHOLD
    pid = _patient(doctor)
    handoff.create_and_dispatch_handoff(_action(pid, "Kalvorex"), doctor)
    [order] = _orders(pid)
    assert order["formulary_id"] == KAL_ID and order["drug_name"] == "Kalvorex"
    assert _payload(_events(order["id"])[1])["score"] == 0.85


# ── A substring-boosted wrong match is refused and flagged ───────────────────

def test_a_name_shared_by_two_products_binds_neither(doctor, lifecycle):
    """'Vextrabin' is part of two products' names. The reconciler lifts either
    to 0.85 on the substring alone; the card names neither product whole."""
    match, score = reconcile_medication("Vextrabin")
    assert match["id"] in (VEX_A, VEX_B) and score == pytest.approx(0.85)
    assert ai_lanes._unboosted_score("Vextrabin", match) < ai_lanes.RECONCILE_THRESHOLD

    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid, "Vextrabin"), doctor)
    assert result["status"] == "accepted" and result["downstream_ref_id"] == "charted-only"
    assert "matches only part of the name" in result["error_summary"]
    assert "no product was chosen" in result["error_summary"]
    [order] = _orders(pid)
    assert _supply(pid) == []
    assert order["formulary_id"] is None and order["supply_source"] is None
    [created] = _events(order["id"])
    flagged = _payload(created)["formulary_binding"]
    assert flagged["binding"] == "not_bound" and flagged["reason"] == "partial_name"
    assert flagged["candidate_product_id"] in (VEX_A, VEX_B)


def test_record_mode_keeps_the_name_match_it_always_made(doctor, record_mode):
    """Shown, not changed: in record mode the old LIKE match still binds the
    first product SQLite returns, and binds only the supply row."""
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid, "Vextrabin"), doctor)
    [order] = _orders(pid)
    [supply] = _supply(pid)
    assert supply["medication_id"] in (VEX_A, VEX_B)
    assert order["formulary_id"] is None and result["downstream_ref_id"] == supply["id"]
    assert _payload(_events(order["id"])[1])["binding"] == "name_match"
    card = json.loads(_q("SELECT safety_json FROM sage_handoffs WHERE id = ?",
                         result["id"])[0]["safety_json"])
    assert "screened_as" not in card


def test_part_of_one_products_name_binds_nothing(doctor, lifecycle):
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid, "Zelqu"), doctor)
    assert result["downstream_ref_id"] == "charted-only"
    [order] = _orders(pid)
    assert order["formulary_id"] is None
    assert _payload(_events(order["id"])[0])["formulary_binding"]["reason"] == "partial_name"


# ── Below the threshold: charted-only, as before ──────────────────────────────

def test_below_the_threshold_stays_charted_only(doctor, lifecycle, screened_names):
    match, score = reconcile_medication(UNLISTED)
    assert match is None and score < ai_lanes.RECONCILE_THRESHOLD
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid, UNLISTED), doctor)
    assert result["status"] == "accepted" and result["downstream_ref_id"] == "charted-only"
    assert result["error_summary"] == (
        f"Charted for review, but not sent to pharmacy: Medication {UNLISTED} not found in "
        "Helix master catalog. Place the order in Helix.")
    assert screened_names == [UNLISTED]
    [order] = _orders(pid)
    assert order["formulary_id"] is None and _supply(pid) == []
    assert _payload(_events(order["id"])[0])["formulary_binding"]["reason"] == "no_match"


# ── Nothing bound unless the screen ran for it ────────────────────────────────

def test_a_product_retired_after_the_screen_is_not_ordered(doctor, lifecycle):
    pid = _patient(doctor)
    action = _action(pid, "Zelquorin")
    screened = handoff.prescreen_handoff(action, doctor)
    assert screened.binding.bound and screened.binding.product["id"] == ZEL_ID
    with db_session() as db:
        db.execute("UPDATE helix_medication_master SET identity_status = 'retired' WHERE id = ?",
                   (ZEL_ID,))
    try:
        with db_session() as db:
            view, _ = handoff.dispatch_in_transaction(db, action, doctor, screened)
    finally:
        with db_session() as db:
            db.execute("UPDATE helix_medication_master SET identity_status = 'active' "
                       "WHERE id = ?", (ZEL_ID,))
    assert view["downstream_ref_id"] == "charted-only"
    assert "can no longer be ordered" in view["error_summary"]
    [order] = _orders(pid)
    assert order["formulary_id"] is None and _supply(pid) == []
    assert _payload(_events(order["id"])[0])["formulary_binding"]["reason"] == \
        ai_lanes.NOT_BOUND_UNAVAILABLE


def test_a_screen_made_without_a_binding_binds_nothing(doctor, lifecycle):
    pid = _patient(doctor)
    plain = {"drug": "Zelquorin", "allergy_alerts": [], "interaction_alerts": [],
             "blocking": False}
    with db_session() as db:
        ref, _safety, note = handoff._write_helix(db, {"patient_id": pid, "target": "Zelquorin"},
                                                  doctor, plain)
    assert ref == "charted-only" and "was not checked" in note
    [order] = _orders(pid)
    assert order["formulary_id"] is None
    assert _payload(_events(order["id"])[0])["formulary_binding"]["reason"] == \
        ai_lanes.NOT_BOUND_NOT_SCREENED


def test_a_card_whose_drug_changed_after_the_screen_binds_nothing(doctor, lifecycle):
    pid = _patient(doctor)
    screened = handoff.prescreen_handoff(_action(pid, "Zelquorin"), doctor)
    changed = _action(pid, "Kalvorex")
    with db_session() as db:
        view, _ = handoff.dispatch_in_transaction(db, changed, doctor, screened)
    assert view["downstream_ref_id"] == "charted-only"
    [order] = _orders(pid)
    assert order["drug_name"] == "Kalvorex" and order["formulary_id"] is None


def test_an_unreadable_catalogue_binds_nothing_and_still_screens(doctor, lifecycle,
                                                                 monkeypatch, screened_names):
    def _boom(name):
        raise RuntimeError("catalogue unreadable")

    monkeypatch.setattr(ai_lanes, "bind_sage_product", _boom)
    pid = _patient(doctor)
    result = handoff.create_and_dispatch_handoff(_action(pid, "Zelquorin"), doctor)
    assert screened_names == ["Zelquorin"]
    assert result["downstream_ref_id"] == "charted-only"
    assert "formulary could not be read" in result["error_summary"]


# ── The duplicate rule reaches the Sage card (MO-R6, lifecycle) ──────────────

def test_a_drug_already_ordered_is_blocked_with_the_reason_on_the_card(doctor, lifecycle):
    pid = _patient(doctor)
    with db_session(immediate=True) as conn:
        chart_lane.write_chart_order_in_txn(
            conn, patient_id=pid, drug_name="Zelquorin 250mg", dosage="250 mg", route="Oral",
            frequency="BD", status="active", actor_id=doctor, actor_role="doctor")
    result = handoff.create_and_dispatch_handoff(_action(pid, "Zelquorin"), doctor)
    assert result["status"] == "blocked"
    assert "already ordered" in result["error_summary"]
    assert len(_orders(pid)) == 1 and _supply(pid) == []


# ── The pieces ────────────────────────────────────────────────────────────────

def test_the_unboosted_score_is_the_reconcilers_formula():
    """On a name with no substring hit the reconciler's score IS its hybrid
    score, so the two must agree exactly."""
    match, score = reconcile_medication("Zelquorine")
    assert match is not None and match["id"] == ZEL_ID
    assert "zelquorine" not in (match["name"].lower() + " " + match["generic_name"].lower())
    assert ai_lanes._unboosted_score("Zelquorine", match) == pytest.approx(score)
    binding = ai_lanes.bind_sage_product("Zelquorine")
    assert binding.bound and binding.product["id"] == ZEL_ID


def test_the_threshold_is_the_reconcilers():
    source = inspect.getsource(reconcile_medication)
    assert f"best_score >= {ai_lanes.RECONCILE_THRESHOLD}" in source
    assert "0.85" in source


def test_what_names_a_product_whole():
    zel = {"name": ZEL_NAME, "generic_name": "zelquorin"}
    kal = {"name": KAL_NAME, "generic_name": "amoxicillin"}
    insulin = {"name": "Insulin Glargine 100 units/ml", "generic_name": "insulin glargine"}
    assert ai_lanes._names_every_ingredient("Zelquorin", zel)
    assert ai_lanes._names_every_ingredient("Kalvorex", kal)
    assert not ai_lanes._names_every_ingredient("Zelqu", zel)
    assert not ai_lanes._names_every_ingredient("Insulin", insulin)
    assert ai_lanes._names_every_ingredient("Insulin glargine", insulin)
    # A short word is an ingredient word too: 'Vitamin' names no one vitamin.
    vit_k = {"name": "Vitamin K 10mg Injection", "generic_name": "Vitamin K"}
    assert not ai_lanes._names_every_ingredient("Vitamin", vit_k)
    assert ai_lanes._names_every_ingredient("Vitamin K", vit_k)
    # A salt beside a drug is not an ingredient word: 'Losartan' names
    # Losartan Potassium, and 'Potassium' does not.
    losartan = {"name": "Losartan Potassium 50mg Tablet", "generic_name": "Losartan Potassium"}
    assert ai_lanes._names_every_ingredient("Losartan", losartan)
    assert not ai_lanes._names_every_ingredient("Potassium", losartan)


def test_record_mode_and_store_off_do_not_bind_exactly(monkeypatch):
    assert ai_lanes.exact_binding_on() is False
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    assert ai_lanes.exact_binding_on() is False
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    assert ai_lanes.exact_binding_on() is True


def test_store_off_the_lane_screens_the_card_once(doctor, screened_names):
    pid = _patient(doctor)
    handoff.create_and_dispatch_handoff(_action(pid, "Zelquorin"), doctor)
    assert screened_names == ["Zelquorin"]
    assert _orders(pid) == []
    [supply] = _supply(pid)
    assert supply["medication_id"] == ZEL_ID and supply["med_order_id"] is None
