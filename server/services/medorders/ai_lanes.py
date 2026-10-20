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

THE NAME MATCH BINDS THE SUPPLY ROW ONLY (record mode). med_orders.formulary_id
is an exact product id, never a name match (design section 1). So in record
mode a Sage order carries no formulary_id: the matched product is recorded on
its first supply row and in that row's 'supply_requested' event (binding
'name_match'), exactly as the unlinked Helix row carried it before. A further
'Request from pharmacy' on such an order therefore answers not_formulary_bound
until a pharmacist binds a product at verification (medorders-5a).

EXACT BINDING (medorders-20, W9-C; ORB_MED_ORDER_LIFECYCLE on). The LIKE match
takes whichever product SQLite returns first among every product whose name
contains the target, and the gate screened the target's free text, so the
screened name and the dispensed product could differ ('Insulin' screened,
Insulin Glargine dispensed). With the lifecycle on, bind_sage_product runs
OUTSIDE the write transaction, before the screen:
  * the product is the reconciler's match (rxnorm_reconciler.reconcile_medication,
    confirmed drugs only), at its threshold of 0.8 (RECONCILE_THRESHOLD);
  * a match that clears the threshold only through the reconciler's
    substring boost (the target is part of the product's name or generic,
    which lifts the score to 0.85) binds only when the target names every
    ingredient word of the product (screening.ingredient_word_set, short
    words such as the K of Vitamin K included): 'Quorvex' binds Quorvex 250mg
    Tablet, 'Insulin' does not bind Insulin Glargine, 'Vitamin' does not bind
    Vitamin K and 'Amox' does not bind Amoxicillin. Refused, it is flagged on
    the order's 'created' event and the card says no product was chosen;
  * no match at the threshold is 'charted-only', as before.
sage_handoff_service then screens the bound product's NAME (as the Relay
confirm path does), its generic name when the name does not carry it (a brand),
and the target too when it reads differently, so the names screened are the
product dispensed and the name on the chart. Inside the write the product is
read again (still confirmed, or 'charted-only' with a note); the order carries
formulary_id (exact binding), and the supply row and its 'supply_requested'
event say binding 'reconciled' with the score. A screen that carries no
binding (not made by the Sage prescreen) binds nothing.

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
from collections.abc import Mapping
from typing import Any, NamedTuple

from services.clinical_time import utc_iso_now
from services.medorders import core, queries

logger = logging.getLogger("orb.medorders.ai_lanes")

__all__ = [
    "ENTRY_SAGE",
    "LANE_SAGE",
    "SOURCE_SAGE",
    "ENTRY_SCRIBE",
    "LANE_SCRIBE",
    "SOURCE_SCRIBE",
    "WARD_SOURCE",
    "BINDING_NAME_MATCH",
    "BINDING_RECONCILED",
    "BINDING_NOT_BOUND",
    "RECONCILE_THRESHOLD",
    "NOT_BOUND_NO_MATCH",
    "NOT_BOUND_PARTIAL_NAME",
    "NOT_BOUND_NOT_SCREENED",
    "NOT_BOUND_UNAVAILABLE",
    "NOT_BOUND_CATALOGUE_UNREADABLE",
    "SUPPLY_PHARMACY",
    "SageOrder",
    "SageBinding",
    "store_on",
    "exact_binding_on",
    "bind_sage_product",
    "unbound",
    "write_sage_order_in_txn",
    "write_scribe_order_in_txn",
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
#: medorders-20: the reconciler's match, checked (the module note).
BINDING_RECONCILED = "reconciled"
#: medorders-20: no product bound; the order says why on its 'created' event.
BINDING_NOT_BOUND = "not_bound"

#: The reconciler's own threshold: reconcile_medication returns a match only
#: at or above it (pinned equal by tests/test_medorder_sage_binding.py).
RECONCILE_THRESHOLD = 0.8

#: Why the exact binding bound nothing.
NOT_BOUND_NO_MATCH = "no_match"
NOT_BOUND_PARTIAL_NAME = "partial_name"
NOT_BOUND_NOT_SCREENED = "not_screened"
NOT_BOUND_UNAVAILABLE = "product_unavailable"
NOT_BOUND_CATALOGUE_UNREADABLE = "catalogue_unreadable"

SUPPLY_PHARMACY = "pharmacy"


class SageOrder(NamedTuple):
    """What write_sage_order_in_txn wrote: the chart row's id, the order as it
    now stands, its supply row (None when the catalogue had no match) and,
    then, why no supply row was written, in the card's words."""

    medication_id: int
    order: dict
    supply: dict | None
    not_sent: str | None


class SageBinding(NamedTuple):
    """The product a Sage order is bound to (medorders-20), decided before the
    screen. `product` {id, name, generic_name} when bound, else None with
    `code` (NOT_BOUND_*) and `note`, the card's words for why nothing was sent
    to pharmacy. `candidate` is the product the reconciler named, bound or
    not; `target` the card's drug name the binding was made for."""

    target: str
    product: dict | None
    score: float
    code: str | None
    note: str | None
    candidate: dict | None

    @property
    def bound(self) -> bool:
        return self.product is not None

    def record(self) -> dict:
        """What the order's ledger keeps about the binding."""
        out: dict[str, Any] = {
            "binding": BINDING_RECONCILED if self.bound else BINDING_NOT_BOUND,
            "score": round(float(self.score or 0.0), 3),
        }
        if self.candidate:
            out["candidate_product_id"] = self.candidate.get("id")
        if not self.bound:
            out["reason"] = self.code
        return out


def store_on() -> bool:
    """The record-mode switch (ORB_MED_ORDER_STORE), read at call time. Both
    AI lanes go through the store whenever it is on, lifecycle or not."""
    return core.store_on()


def exact_binding_on() -> bool:
    """medorders-20: the Sage lane binds the reconciler's match exactly and
    screens the bound product's name. With the lifecycle on (authoritative
    mode); in record mode the lane keeps the name match it always made."""
    return core.lifecycle_on()


def unbound(target: str, code: str, note: str) -> SageBinding:
    """A binding that bound nothing, with the card's words for why."""
    return SageBinding(target, None, 0.0, code, note, None)


def _unboosted_score(name: str, med: Mapping) -> float:
    """The reconciler's hybrid score for `med` WITHOUT its substring boost:
    the same formula as rxnorm_reconciler.reconcile_medication (pinned equal
    on names with no substring match by tests/test_medorder_sage_binding.py)."""
    from services.rxnorm_reconciler import (
        calculate_metaphone_similarity,
        calculate_similarity,
    )

    cand_name = med.get("name") or ""
    cand_generic = med.get("generic_name") or ""
    lex = max(calculate_similarity(name, cand_name), calculate_similarity(name, cand_generic))
    meta = max(
        calculate_metaphone_similarity(name, cand_name),
        calculate_metaphone_similarity(name, cand_generic),
    )
    return (lex * 0.7) + (meta * 0.3)


def _names_every_ingredient(target: str, med: Mapping) -> bool:
    """Does the target name the whole product: every ingredient word of its
    generic name, or every such word of its own name (a brand)? 'Quorvex'
    names quorvex, and a brand names its own product; 'Insulin' does not name
    insulin glargine, 'Vitamin' does not name Vitamin K, and 'Amox' does not
    name amoxicillin."""
    from services.medorders.screening import ingredient_word_set

    said = ingredient_word_set(target)
    for name in (med.get("generic_name"), med.get("name")):
        wanted = ingredient_word_set(name or "")
        if wanted and wanted <= said:
            return True
    return False


def bind_sage_product(target: str) -> SageBinding:
    """The product a Sage card's drug is bound to (the module note). BLOCKING
    (the reconciler reads the catalogue in a session of its own): call it
    outside any write transaction. Raises only if the catalogue cannot be read."""
    from services.rxnorm_reconciler import reconcile_medication

    name = (target or "").strip()
    match, score = reconcile_medication(name)
    if not match:
        return SageBinding(
            name,
            None,
            float(score or 0.0),
            NOT_BOUND_NO_MATCH,
            f"Medication {name} not found in Helix master catalog",
            None,
        )
    candidate = {
        "id": match["id"],
        "name": match["name"],
        "generic_name": match.get("generic_name"),
    }
    if _unboosted_score(name, match) < RECONCILE_THRESHOLD and not _names_every_ingredient(
        name, match
    ):
        # The match stands only on the substring boost, and the card names
        # only part of the product: which product was meant is a guess.
        return SageBinding(
            name,
            None,
            float(score),
            NOT_BOUND_PARTIAL_NAME,
            f"{name} matches only part of the name {match['name']}, so no product was chosen",
            candidate,
        )
    return SageBinding(name, candidate, float(score), None, None, candidate)


def _role_of(conn, user_id: str | None) -> str | None:
    if not user_id:
        return None
    row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["role"] if row else None


def write_sage_order_in_txn(
    conn,
    *,
    patient_id: str,
    drug_name: str,
    dose: str,
    route: str,
    frequency: str,
    actor_id: str,
    safety: Mapping | None = None,
    sage_action_id: str | None = None,
    sage_handoff_id: str | None = None,
    request_meta: Mapping | None = None,
    now: str | None = None,
    binding: SageBinding | None = None,
) -> SageOrder:
    """The Sage handoff's chart row, medication order and supply row, on the
    caller's transaction (the handoff's own savepoint). Never commits.

    `binding` is the exact binding (medorders-20, lifecycle mode) the screen
    was run for, from bind_sage_product; None keeps the record-mode name
    match.

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

    # 2. The product. Record mode: the same name match create_order makes (a
    #    read). Exact binding: the product the screen was run for, read again
    #    here so a product retired since is never ordered.
    not_bound_note = None
    created_payload = None
    if binding is None:
        product = helix_service.resolve_by_name_in_txn(conn, drug_name)
        binding_mode = BINDING_NAME_MATCH
    else:
        binding_mode = BINDING_RECONCILED
        product = None
        if binding.target != (drug_name or "").strip():
            binding = unbound(
                drug_name,
                NOT_BOUND_NOT_SCREENED,
                f"the product for {drug_name} was not checked against this "
                "patient's allergies and medicines",
            )
        if binding.bound:
            assert binding.product is not None
            row = conn.execute(
                "SELECT id, name, generic_name FROM helix_medication_master "
                "WHERE id = ? AND identity_status = ?",
                (binding.product["id"], helix_service.ORDERABLE_IDENTITY),
            ).fetchone()
            if row is not None:
                product = dict(row)
            else:
                binding = SageBinding(
                    binding.target,
                    None,
                    binding.score,
                    NOT_BOUND_UNAVAILABLE,
                    f"{binding.product['name']} can no longer be ordered",
                    binding.candidate,
                )
        if product is None:
            not_bound_note = binding.note
        created_payload = {"formulary_binding": binding.record()}

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
        # An exact binding only (medorders-20): a name match is never one.
        formulary_id=product["id"] if (product and binding is not None) else None,
        legacy_medication_id=medication_id,
        sage_action_id=sage_action_id,
        sage_handoff_id=sage_handoff_id,
        safety=safety,
        event_payload=created_payload,
        lane=LANE_SAGE,
        request_meta=request_meta,
        now=now,
    )
    core.link_projection_in_txn(conn, write.order["id"], medication_id)
    order = core.get_order_in_txn(conn, write.order["id"]) or write.order

    if product is None:
        # 'charted-only', in the words the card has always used, or the
        # exact binding's own reason (medorders-20).
        not_sent = not_bound_note or f"Medication {drug_name} not found in Helix master catalog"
        return SageOrder(medication_id, order, None, not_sent)

    # 4. The supply row, through the unchanged insert_order ('pending'), and
    #    its link to the order, written once and never changed.
    supply = helix_service.insert_order(
        conn, patient_id, product["id"], dose, route, frequency, actor_id
    )
    conn.execute(
        "UPDATE helix_patient_med_orders SET med_order_id = ? "
        "WHERE id = ? AND med_order_id IS NULL",
        (order["id"], supply["id"]),
    )
    payload: dict[str, Any] = {
        "helix_order_id": supply["id"],
        "product_id": product["id"],
        "binding": binding_mode,
        "supply_source": SUPPLY_PHARMACY,
    }
    extra: dict[str, Any] = {
        "helix_order_id": str(supply["id"]),
        "product_id": str(product["id"]),
        "binding": binding_mode,
    }
    if binding is not None:
        payload["score"] = extra["score"] = round(float(binding.score or 0.0), 3)
    event_id = core.record_event_in_txn(
        conn,
        order=order,
        kind="supply_requested",
        from_status=order["status"],
        to_status=order["status"],
        actor_id=actor_id,
        actor_role=role,
        entry_point=ENTRY_SAGE,
        ref_type="helix_order",
        ref_id=supply["id"],
        payload=payload,
        created_at=now,
    )
    core.audit_in_txn(
        conn,
        order=order,
        action="med_order_supply_requested",
        actor_id=actor_id,
        actor_role=role,
        entry_point=ENTRY_SAGE,
        from_status=order["status"],
        to_status=order["status"],
        lane=LANE_SAGE,
        event_id=event_id,
        extra=extra,
        request_meta=request_meta,
    )
    row = conn.execute(
        "SELECT * FROM helix_patient_med_orders WHERE id = ?", (supply["id"],)
    ).fetchone()
    return SageOrder(medication_id, order, dict(row), None)


def write_scribe_order_in_txn(
    conn,
    *,
    medication_id: int,
    patient_id: str,
    author_id: str | None,
    note_id: Any,
    drug_name: str,
    dose: str | None,
    route: str | None,
    frequency: str | None,
    notes: str | None = None,
    source_sentence: str | None = None,
    request_meta: Mapping | None = None,
) -> dict:
    """One medication order for a drug a signed note starts, linked to the
    chart row `medication_id` the caller has just written in this same
    transaction. Never commits; anything it raises rolls the caller's whole
    extraction back (the chart row included), so a chart row is never left
    without its order. Returns the order."""
    if author_id is None:
        raise core.MedOrderInvalid("A signed note must name its author", code="actor_required")
    role = _role_of(conn, author_id)
    payload: dict[str, Any] = {"source_note_id": str(note_id) if note_id is not None else None}
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
