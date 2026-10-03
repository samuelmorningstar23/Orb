"""Orders / CPOE-lite service tests.

Covers order creation with terminology validation, the guarded state machine
(legal chain, illegal rejection, idempotency), the result flow, and the
terminology validate/seed helpers.
"""
import uuid

import pytest

from database import db_session
from services import orders_service, terminology_service


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module", autouse=True)
def _seed():
    """Ensure the curated terminology starter set is present (idempotent)."""
    terminology_service.seed_terminology()


@pytest.fixture()
def patient():
    pid = f"PATORD{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, "Orders Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    return pid


# ── terminology ───────────────────────────────────────────────────────────────
def test_seed_is_idempotent_and_counts():
    first = terminology_service.seed_terminology()
    second = terminology_service.seed_terminology()
    assert second["counts"]["LOINC"] == first["counts"]["LOINC"]
    assert second["counts"]["LOINC"] >= 30
    assert second["counts"]["SNOMED"] >= 20
    assert second["counts"]["ICD10"] >= 20


def test_validate_and_display():
    assert terminology_service.validate_code("LOINC", "2524-7") is True
    # case-insensitive code_system
    assert terminology_service.validate_code("loinc", "2524-7") is True
    assert terminology_service.get_display("LOINC", "2524-7") == \
        "Lactate [Moles/volume] in Serum or Plasma"
    assert terminology_service.validate_code("LOINC", "0000-0") is False
    assert terminology_service.get_display("LOINC", "0000-0") is None


# ── create ────────────────────────────────────────────────────────────────────
def test_create_with_valid_code_succeeds(patient):
    order = orders_service.create_order(
        patient_id=patient,
        order_type="lab",
        description="Serum lactate",
        code="2524-7",
        code_system="LOINC",
        ordered_by="D001",
    )
    assert order["status"] == "draft"
    assert order["code"] == "2524-7"
    assert order["code_system"] == "LOINC"
    events = orders_service.get_order_events(order["id"])
    assert len(events) == 1
    assert events[0]["new_status"] == "draft"


def test_create_with_unknown_code_rejected(patient):
    with pytest.raises(ValueError, match="Unknown"):
        orders_service.create_order(
            patient_id=patient,
            order_type="lab",
            description="Bogus test",
            code="9999-9",
            code_system="LOINC",
            ordered_by="D001",
        )


def test_create_nursing_without_code_allowed(patient):
    order = orders_service.create_order(
        patient_id=patient,
        order_type="nursing",
        description="Ambulate patient TID",
        ordered_by="N001",
    )
    assert order["status"] == "draft"
    assert order["code"] is None


def test_create_coded_type_requires_code(patient):
    with pytest.raises(ValueError, match="requires a coded concept"):
        orders_service.create_order(
            patient_id=patient,
            order_type="lab",
            description="No code lab",
            ordered_by="D001",
        )


# ── state machine ─────────────────────────────────────────────────────────────
def _draft_lab(patient):
    return orders_service.create_order(
        patient_id=patient, order_type="lab", description="CBC panel",
        code="58410-2", code_system="LOINC", ordered_by="D001",
    )


def test_legal_transition_chain(patient):
    order = orders_service.create_order(
        patient_id=patient, order_type="procedure", description="IM injection",
        code="76601001", code_system="SNOMED", ordered_by="D001",
    )
    oid = order["id"]
    assert orders_service.transition_order(oid, "active", "D001")["status"] == "active"
    assert orders_service.transition_order(oid, "in_progress", "D001")["status"] == "in_progress"
    assert orders_service.transition_order(oid, "completed", "D001")["status"] == "completed"
    events = orders_service.get_order_events(oid)
    # created + 3 transitions
    assert [e["new_status"] for e in events] == ["draft", "active", "in_progress", "completed"]


def test_illegal_transition_rejected(patient):
    order = _draft_lab(patient)
    with pytest.raises(ValueError, match="Illegal transition"):
        orders_service.transition_order(order["id"], "completed", "D001")


def test_transition_from_terminal_rejected(patient):
    order = _draft_lab(patient)
    orders_service.transition_order(order["id"], "cancelled", "D001")
    with pytest.raises(ValueError, match="Illegal transition"):
        orders_service.transition_order(order["id"], "active", "D001")


def test_idempotent_transition_is_noop(patient):
    order = _draft_lab(patient)
    orders_service.transition_order(order["id"], "active", "D001")
    events_before = len(orders_service.get_order_events(order["id"]))
    # transition to current status → no-op success, no new event
    result = orders_service.transition_order(order["id"], "active", "D001")
    assert result["status"] == "active"
    assert len(orders_service.get_order_events(order["id"])) == events_before


def test_resulted_only_for_lab_imaging(patient):
    order = orders_service.create_order(
        patient_id=patient, order_type="procedure", description="IM injection",
        code="76601001", code_system="SNOMED", ordered_by="D001",
    )
    orders_service.transition_order(order["id"], "active", "D001")
    with pytest.raises(ValueError, match="resulted"):
        orders_service.transition_order(order["id"], "resulted", "D001")


# ── result flow ───────────────────────────────────────────────────────────────
def test_add_result_flow(patient):
    order = _draft_lab(patient)
    orders_service.transition_order(order["id"], "active", "D001")
    orders_service.transition_order(order["id"], "in_progress", "D001")
    resulted = orders_service.add_result(order["id"], "WBC 11.2, Hgb 13.4", "D001")
    assert resulted["status"] == "resulted"
    assert resulted["result_text"] == "WBC 11.2, Hgb 13.4"
    assert resulted["result_at"] is not None
    events = orders_service.get_order_events(order["id"])
    assert events[-1]["new_status"] == "resulted"


def test_add_result_rejected_for_nursing(patient):
    order = orders_service.create_order(
        patient_id=patient, order_type="nursing", description="Turn q2h",
        ordered_by="N001",
    )
    with pytest.raises(ValueError, match="Cannot result"):
        orders_service.add_result(order["id"], "done", "N001")


# ── read ──────────────────────────────────────────────────────────────────────
def test_list_and_get(patient):
    a = _draft_lab(patient)
    b = orders_service.create_order(
        patient_id=patient, order_type="nursing", description="Vitals q4h",
        ordered_by="N001",
    )
    orders_service.transition_order(b["id"], "active", "N001")

    all_orders = orders_service.list_orders(patient)
    assert {a["id"], b["id"]}.issubset({o["id"] for o in all_orders})

    active_only = orders_service.list_orders(patient, status="active")
    assert b["id"] in {o["id"] for o in active_only}
    assert a["id"] not in {o["id"] for o in active_only}

    assert orders_service.get_order(a["id"])["id"] == a["id"]
    assert orders_service.get_order("nonexistent") is None
