"""Orb: the two AI lanes through the medication-order store (medorders-8, W8-E;
design section 4, "Sage handoff" and "Scribe extraction").

Two lanes put a medicine on the ward chart that a model read rather than a
person typed:

  * THE SAGE HANDOFF. A clinician confirms a Sage card in Relay and
    services/sage_handoff_service._write_helix charts the drug as an
    unverified 'llm_extracted' row (dose 'REVIEW REQUIRED') and places a Helix
    supply order for it.
  * SCRIBE EXTRACTION. A signed note is read by the model and
    services/nervous_system_triggers._persist_extraction charts each drug the
    note starts the same way, with no supply order.

With ORB_MED_ORDER_STORE on (record mode, and lifecycle mode on top of it)
each such medicine is also a medication order, written by this module on the
CALLER's connection, inside the caller's transaction, never committing:

  Sage handoff      the chart row, the order (prescribed, needs_verification
                    1, prescriber = the clinician who confirmed the card, as
                    the chart row has always said) and its supply row,
                    together. The supply row is bound to the product
                    helix_service.resolve_by_name_in_txn finds, the SAME name
                    match create_order has always made, so the product is
                    unchanged; 'charted-only' (no supply row) when the
                    catalogue has no match, as before. Anything else that
                    fails after the chart row leaves NEITHER row: the caller's
                    savepoint (or session) rolls the whole lane back.
  Scribe extraction one order per started drug, linked to the chart row the
                    caller has just written, prescriber = the note's author,
                    needs_verification 1, no supply row, the [SAFETY] note
                    kept. Held ruling W111 is untouched: the drug stays
                    'prescribed, needs a pharmacist', never a draft.

THE NAME MATCH BINDS THE SUPPLY ROW ONLY. med_orders.formulary_id is an exact
product id, never a name match (design section 1). So a Sage order carries no
formulary_id: the matched product is recorded on its first supply row and in
that row's 'supply_requested' event (binding 'name_match'), exactly as the
unlinked Helix row carried it before. A further 'Request from pharmacy' on such
an order therefore answers not_formulary_bound until a pharmacist binds a
product at verification (medorders-5a) or the Sage lane binds exactly
(medorders-20).

WHAT A PHARMACIST'S CHECK DOES, keyed on the links and not on the switches
(invariant I2, built in W5-E and W7-E): the Verify Queue's verify or reject of
the chart row moves the order in the same transaction
(chart_lane.mirror_legacy_decision_in_txn); a Helix step on the supply row
writes its supply event, and with the lifecycle on a Helix verification or
rejection of an order still waiting for its pharmacist IS the order's check
(supply.on_helix_step_in_txn). So in lifecycle mode one pharmacist decision
covers both rows; in record mode the two stay separate, each writing its own
event. A Sage supply row still carries the placeholder dose, so Helix refuses
to verify it (placeholder_dose, defect F-A closed for linked rows in W7-E): the
dose is corrected at the Verify Queue.

With the store off nothing here is called and both lanes write exactly what
they always wrote. Not touched: relay_routes.py (the synchronous confirm),
held rulings W111 and R010.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, NamedTuple, Optional

from services.clinical_time import utc_iso_now
from services.medorders import core, queries

logger = logging.getLogger("orb.medorders.ai_lanes")

__all__ = [
    "ENTRY_SAGE", "LANE_SAGE", "SOURCE_SAGE", "ENTRY_SCRIBE", "LANE_SCRIBE", "SOURCE_SCRIBE",
    "WARD_SOURCE", "BINDING_NAME_MATCH", "SUPPLY_PHARMACY", "SageOrder",
    "store_on", "write_sage_order_in_txn", "write_scribe_order_in_txn",
]

#: The Sage handoff lane: its gate entry point (sage_handoff_service._screen_helix
#: screens with the same name), its lane and its order source.
ENTRY_SAGE = "sage.agentic_handoff"
LANE_SAGE = "sage_handoff"
SOURCE_SAGE = "sage_handoff"

#: The Scribe extraction lane (nervous_system_triggers._screen_medications
#: screens with the same entry point).
ENTRY_SCRIBE = "scribe.auto_extraction"
LANE_SCRIBE = "scribe_extracted"
SOURCE_SCRIBE = "scribe_extracted"

#: The chart row's provenance on both lanes: the Verify Queue lists it.
WARD_SOURCE = "llm_extracted"

#: How the Sage lane's supply row found its product: a name match
#: (helix_service.resolve_by_name_in_txn), not an exact binding.
BINDING_NAME_MATCH = "name_match"

SUPPLY_PHARMACY = "pharmacy"


class SageOrder(NamedTuple):
    """What write_sage_order_in_txn wrote: the chart row's id, the order as it
    now stands, its supply row (None when the catalogue had no match) and,
    then, why no supply row was written, in the card's words."""
    medication_id: int
    order: dict
    supply: Optional[dict]
    not_sent: Optional[str]


def store_on() -> bool:
    """The record-mode switch (ORB_MED_ORDER_STORE), read at call time. Both
    AI lanes go through the store whenever it is on, lifecycle or not."""
    return core.store_on()


def _role_of(conn, user_id: Optional[str]) -> Optional[str]:
    if not user_id:
        return None
    row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["role"] if row else None


def write_sage_order_in_txn(conn, *, patient_id: str, drug_name: str, dose: str,
                            route: str, frequency: str, actor_id: str,
                            safety: Optional[Mapping] = None,
                            sage_action_id: Optional[str] = None,
                            sage_handoff_id: Optional[str] = None,
                            request_meta: Optional[Mapping] = None,
                            now: Optional[str] = None) -> SageOrder:
    """The Sage handoff's chart row, medication order and supply row, on the
    caller's transaction (the handoff's own savepoint). Never commits.

    Raises whatever a write raises (a core.MedOrderError, a database error,
    an unwritable audit row): the caller's savepoint then rolls back the chart
    row too, so a fault never leaves one row without the other."""
    from services import helix_service

    # 1. The chart row, exactly as the lane has always written it: unverified,
    #    model-sourced, the placeholder dose flagged for the pharmacist.
    cur = conn.execute(
        "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
        "prescribed_by, source) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        (patient_id, drug_name, dose, route, frequency, actor_id, WARD_SOURCE),
    )
    medication_id = int(cur.lastrowid)

    # 2. The product, by the same name match create_order makes (a read).
    product = helix_service.resolve_by_name_in_txn(conn, drug_name)

    # 3. The order, linked both ways to the chart row.
    now = now or utc_iso_now()
    role = _role_of(conn, actor_id)
    write = core.create_in_txn(
        conn,
        patient_id=patient_id,
        drug_name=drug_name,
        source=SOURCE_SAGE,
        status="prescribed",
        entered_by=actor_id,
        entered_by_role=role,
        actor_id=actor_id,
        actor_role=role,
        entry_point=ENTRY_SAGE,
        admission_id=queries.active_admission_id(conn, patient_id),
        needs_verification=True,
        dose=dose,
        route=route,
        frequency=frequency,
        # The clinician who confirmed the card, as the chart row says
        # (prescribed_by); who is the prescriber of record is held ruling R010.
        prescriber_id=actor_id,
        supply_source=SUPPLY_PHARMACY if product else None,
        legacy_medication_id=medication_id,
        sage_action_id=sage_action_id,
        sage_handoff_id=sage_handoff_id,
        safety=safety,
        lane=LANE_SAGE,
        request_meta=request_meta,
        now=now,
    )
    core.link_projection_in_txn(conn, write.order["id"], medication_id)
    order = core.get_order_in_txn(conn, write.order["id"]) or write.order

    if product is None:
        # 'charted-only', in the words the card has always used.
        not_sent = f"Medication {drug_name} not found in Helix master catalog"
        return SageOrder(medication_id, order, None, not_sent)

    # 4. The supply row, through the unchanged insert_order ('pending'), and
    #    its link to the order, written once and never changed.
    supply = helix_service.insert_order(conn, patient_id, product["id"], dose, route,
                                        frequency, actor_id)
    conn.execute("UPDATE helix_patient_med_orders SET med_order_id = ? "
                 "WHERE id = ? AND med_order_id IS NULL", (order["id"], supply["id"]))
    payload = {"helix_order_id": supply["id"], "product_id": product["id"],
               "binding": BINDING_NAME_MATCH, "supply_source": SUPPLY_PHARMACY}
    event_id = core.record_event_in_txn(
        conn, order=order, kind="supply_requested", from_status=order["status"],
        to_status=order["status"], actor_id=actor_id, actor_role=role, entry_point=ENTRY_SAGE,
        ref_type="helix_order", ref_id=supply["id"], payload=payload, created_at=now)
    core.audit_in_txn(
        conn, order=order, action="med_order_supply_requested", actor_id=actor_id,
        actor_role=role, entry_point=ENTRY_SAGE, from_status=order["status"],
        to_status=order["status"], lane=LANE_SAGE, event_id=event_id,
        extra={"helix_order_id": str(supply["id"]), "product_id": str(product["id"]),
               "binding": BINDING_NAME_MATCH},
        request_meta=request_meta)
    row = conn.execute("SELECT * FROM helix_patient_med_orders WHERE id = ?",
                       (supply["id"],)).fetchone()
    return SageOrder(medication_id, order, dict(row), None)


def write_scribe_order_in_txn(conn, *, medication_id: int, patient_id: str,
                              author_id: Optional[str], note_id: Any, drug_name: str,
                              dose: Optional[str], route: Optional[str],
                              frequency: Optional[str], notes: Optional[str] = None,
                              source_sentence: Optional[str] = None,
                              request_meta: Optional[Mapping] = None) -> dict:
    """One medication order for a drug a signed note starts, linked to the
    chart row `medication_id` the caller has just written in this same
    transaction. Never commits; anything it raises rolls the caller's whole
    extraction back (the chart row included), so a chart row is never left
    without its order. Returns the order."""
    role = _role_of(conn, author_id)
    payload = {"source_note_id": str(note_id) if note_id is not None else None}
    if source_sentence:
        payload["source_sentence"] = source_sentence
    write = core.create_in_txn(
        conn,
        patient_id=patient_id,
        drug_name=drug_name,
        source=SOURCE_SCRIBE,
        status="prescribed",
        entered_by=author_id,
        entered_by_role=role,
        actor_id=author_id,
        actor_role=role,
        entry_point=ENTRY_SCRIBE,
        admission_id=queries.active_admission_id(conn, patient_id),
        needs_verification=True,
        dose=dose,
        route=route,
        frequency=frequency,
        notes=notes,
        # The signed note's author, as the chart row says (prescribed_by).
        prescriber_id=author_id,
        legacy_medication_id=medication_id,
        scribe_note_id=str(note_id) if note_id is not None else None,
        event_payload=payload,
        lane=LANE_SCRIBE,
        request_meta=request_meta,
    )
    core.link_projection_in_txn(conn, write.order["id"], medication_id)
    return core.get_order_in_txn(conn, write.order["id"]) or write.order
