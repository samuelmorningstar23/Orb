"""Orb: pharmacy supply for a medication order, the Helix hook and the dose's
custody link (medorders-5b, W7-E; design sections 5 and 6).

ONE SUPPLY ROW IS ONE UNIT. A helix_patient_med_orders row is one dispense
request, and Helix dispenses one unit per row (helix_service._atomic_dispense).
So a medication order has many supply rows over its life and at most one OPEN
at a time (not yet rejected and not yet received). The link is the supply row's
med_order_id (migration 61), written in the transaction that inserts the row
and never changed afterwards. Nothing writes it but request_supply_in_txn here,
so a database that never used the order store has no linked supply row and
nothing in this module changes anything for it.

REQUEST FROM PHARMACY (ruling MO-R12 (a): the prescriber picks the supply
source on each order; until the drug master defaults it, choosing pharmacy is
this explicit request, and Orb makes no supply row on its own for a chart or a
CPOE order). request_supply_in_txn is idempotent per order: an open supply row
is returned as it stands (replayed), otherwise one row is written at 'pending'
through the unchanged helix_service.insert_order, with the order's exact
formulary product (never a name lookup), dose, route and frequency, and the
order records 'pharmacy' as its supply source. Refused for an order that is not
live, is on hold, names no formulary product, is supplied another way, or still
carries a blank or placeholder dose, route or frequency. The prescriber can also
say outright that an order is given from ward stock or the patient's own supply
(set_supply_source_in_txn), after which pharmacy is not asked; moving away from
pharmacy is refused while a pharmacy supply is still open.

THE HELIX HOOK (helix_step_hook, run by helix_service inside its own
transaction, after its writes and before its commit). For a LINKED supply row
only, keyed on the link and never on the switches (invariant I2):
  * a step that commits a drug (a suggested substitute, verified, scheduled,
    dispensed) is refused when the order has ended or was never signed;
  * verifying is refused while the supply's or the order's dose or route is
    blank or a placeholder ('REVIEW REQUIRED': defect F-A, closed here for
    linked rows), or when the supply no longer says what the order says;
  * a step that commits or decides is refused when the order changed since the
    pharmacist's screen read it (stale_version) or an allergy or a drug was
    charted since the safety screen ran (screen_stale);
  * with the lifecycle on, a Helix verification or rejection of an order still
    waiting for its pharmacist IS that check: the order moves through
    core.transition_in_txn (core.legacy_decision_on_held_in_txn while held),
    which needs medication.verify (ruling MO-R4 (a)). A supply carrying a
    pharmacy substitute never stands in for the order's own check (held
    ruling R002). An order already checked makes the Helix verification a
    supply step;
  * otherwise the step writes its supply event (supply_reviewing,
    supply_verified, supply_rejected, dispensed or received; 'scheduled' books
    a time and moves no drug, so it writes none) with a clinical_access audit
    row.
Every refusal is a core.MedOrderError, which rolls back the Helix step with its
stock movement and its case-room message. helix_routes runs the same plan
before its safety screen (plan_helix_step), so a refused step audits no screen.

THE DOSE'S CUSTODY LINK (custody_for_dose_in_txn; design section 5, scoped
down). A dose given on a linked ward row names the supply row it came from only
when exactly one of the order's supply rows is delivered, signed for by the
ward, and not named by an earlier dose of the same order. Otherwise the
'administered' event records 'supply not linked': with two units on the ward,
or none, nothing can say which one this dose was. Lot-to-dose custody for
multi-unit supply and ward stock belongs to the pharmacy store epic.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from database import db_session

from services.clinical_time import utc_iso_now
from services.medorders import core

logger = logging.getLogger("orb.medorders.supply")

__all__ = [
    "ENTRY_REQUEST",
    "ENTRY_SUPPLY_SOURCE",
    "LANE_SUPPLY",
    "LANE_HELIX",
    "REF_HELIX_ORDER",
    "SUPPLY_PHARMACY",
    "CLOSED_SUPPLY_STATUSES",
    "SUPPLY_EVENT_FOR_STEP",
    "COMMITTING_STEPS",
    "DECISION_STEPS",
    "GUARDED_STEPS",
    "SUPPLY_NOT_LINKED",
    "SupplyRequest",
    "open_supply_in_txn",
    "request_supply_in_txn",
    "request_supply",
    "set_supply_source_in_txn",
    "set_supply_source",
    "linked_supply",
    "plan_helix_step",
    "on_helix_step_in_txn",
    "helix_step_hook",
    "custody_for_dose_in_txn",
]

ENTRY_REQUEST = "medorder.request_supply"
ENTRY_SUPPLY_SOURCE = "medorder.supply_source"
LANE_SUPPLY = "pharmacy_supply"
LANE_HELIX = "helix"
REF_HELIX_ORDER = "helix_order"
SUPPLY_PHARMACY = "pharmacy"

#: Supply states that are finished: a rejected row never leaves the shelf, a
#: delivered one is on the ward. Every other state is an open request.
CLOSED_SUPPLY_STATUSES = ("rejected", "delivered")

#: The event a Helix step writes on the order's ledger when it does not move
#: the order itself. 'scheduled' books a dispense time and moves no drug, so
#: it is not here and writes nothing.
SUPPLY_EVENT_FOR_STEP = {
    "reviewing": "supply_reviewing",
    "substitute_suggested": "supply_reviewing",
    "verified": "supply_verified",
    "rejected": "supply_rejected",
    "dispensed": "dispensed",
    "delivered": "received",
}

#: Helix steps that commit a drug to the patient, or book it: refused on a
#: linked supply row whose order has ended or was never signed.
COMMITTING_STEPS = ("substitute_suggested", "verified", "scheduled", "dispensed")

#: The Helix steps that, with the lifecycle on, are the pharmacist's decision
#: on the order itself.
DECISION_STEPS = ("verified", "rejected")

#: Steps refused when the order changed after the pharmacist's screen read it.
GUARDED_STEPS = COMMITTING_STEPS + ("rejected",)

#: The Helix steps the drug-safety gate screens (helix_service.SCREENED_TARGETS),
#: which also check that nothing was charted since that screen ran.
_SCREENED_STEPS = ("substitute_suggested", "verified", "dispensed")

#: What an 'administered' event says when it cannot name one supply row.
SUPPLY_NOT_LINKED = "supply not linked"

_NOT_LIVE_WORDS = {
    "draft": "has not been signed by a prescriber",
    "stopped": "was stopped",
    "completed": "is complete",
    "cancelled": "was cancelled",
    "rejected": "was rejected",
}
_STEP_WORDS = {
    "substitute_suggested": "suggest a substitute for",
    "verified": "verify",
    "scheduled": "schedule",
    "dispensed": "dispense",
}
_SUPPLY_SOURCE_WORDS = {"ward_stock": "ward stock", "patient_own": "the patient's own supply"}

BROKEN_LINK_MESSAGE = (
    "This supply's medication order is missing, so pharmacy cannot act on it "
    "here. Ask the administrator to run the medication order parity report."
)
HELD_MESSAGE = "This medicine is on hold. Resume it before asking pharmacy to supply it."
NOT_BOUND_MESSAGE = (
    "This medicine is not linked to a pharmacy product, so pharmacy cannot be "
    "asked to supply it from here."
)
PLACEHOLDER_REQUEST_MESSAGE = (
    "Enter the dose, the route and how often it is given before "
    "asking pharmacy to supply this medicine."
)
PLACEHOLDER_VERIFY_MESSAGE = (
    "The dose or route on this supply still needs review. Reject this "
    "supply, correct the order, then request it again."
)
OUT_OF_DATE_MESSAGE = (
    "The order's dose, route or frequency changed after this supply was "
    "requested. Reject this supply and request it again."
)
STALE_SCREEN_MESSAGE = (
    "An allergy or a medicine was charted for this patient while this order "
    "was being checked. Check the order again."
)
STALE_ORDER_MESSAGE = (
    "This medicine's order changed while you were working on it. Reload it and try again."
)
INVALID_SOURCE_MESSAGE = "Choose pharmacy, ward stock or the patient's own supply."
OPEN_SUPPLY_MESSAGE = (
    "Pharmacy is already preparing a supply of this medicine. Ask pharmacy to "
    "reject that supply before choosing another source."
)


class SupplyRequest(NamedTuple):
    """What request_supply_in_txn returns: the order as it now stands, its
    open supply row, the 'supply_requested' event (None on a replay) and
    whether an open supply row already existed."""

    order: dict
    supply: dict
    event_id: int | None
    replayed: bool


# ── Small helpers ─────────────────────────────────────────────────────────────


def _require_txn(conn) -> None:
    if not getattr(conn, "in_transaction", False):
        raise core.StoreUsageError(
            "supply writers run inside the caller's write transaction: open "
            "db_session(immediate=True) first, then call this with that connection"
        )


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def _is_placeholder(value: Any) -> bool:
    """Blank, or the charting placeholder ('REVIEW REQUIRED', route 'Review'):
    the Verify Queue's own rule, reused so the two surfaces cannot disagree."""
    from services.pharmacy_verification_service import _is_placeholder as rule

    return rule(value)


def _has_supply_link(conn) -> bool:
    """Whether this database has linked supply rows at all: the Helix table
    and its med_order_id column (migration 61, or HELIX_SCHEMA on a fresh
    database). A database without them has nothing to link."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(helix_patient_med_orders)").fetchall()}
    return "med_order_id" in cols


def _substituted(supply: Mapping) -> bool:
    sub = supply.get("substitute_medication_id")
    return bool(sub) and sub != supply.get("medication_id")


def _payload(supply: Mapping, from_status: str, to_status: str, transition_id: str | None) -> dict:
    out = {
        "helix_order_id": supply.get("id"),
        "helix_from": from_status,
        "helix_status": to_status,
        "helix_transition_id": transition_id,
    }
    if _substituted(supply):
        out["substitute_medication_id"] = supply.get("substitute_medication_id")
    return out


# ── Request from pharmacy ─────────────────────────────────────────────────────


def open_supply_in_txn(conn, order_id: str) -> dict | None:
    """The order's open supply row (not rejected, not delivered), or None."""
    marks = ", ".join("?" for _ in CLOSED_SUPPLY_STATUSES)
    row = conn.execute(
        f"SELECT * FROM helix_patient_med_orders WHERE med_order_id = ? "
        f"AND status NOT IN ({marks}) ORDER BY created_at, id LIMIT 1",
        (order_id, *CLOSED_SUPPLY_STATUSES),
    ).fetchone()
    return dict(row) if row is not None else None


def request_supply_in_txn(
    conn,
    order_id: str,
    *,
    actor_id: str,
    actor_role: str | None,
    entry_point: str = ENTRY_REQUEST,
    lane: str = LANE_SUPPLY,
    request_meta: Mapping | None = None,
    now: str | None = None,
) -> SupplyRequest:
    """Ask pharmacy to supply one unit of a live order, in the caller's
    immediate transaction. Idempotent: an open supply row is returned as it
    stands and nothing is written. Otherwise the supply row (through the
    unchanged helix_service.insert_order, 'pending'), its link, the order's
    supply source, the 'supply_requested' event and its clinical_access audit
    row, together."""
    _require_txn(conn)
    order = core.get_order_in_txn(conn, order_id)
    if order is None:
        raise core.MedOrderNotFound("This medication order no longer exists")
    status = order["status"]
    if status == "held":
        raise core.MedOrderConflict(HELD_MESSAGE, code="order_held", order=order)
    if status not in ("prescribed", "verified"):
        words = _NOT_LIVE_WORDS.get(status, "cannot be supplied")
        raise core.TransitionNotAllowed(
            f"This medicine's order {words}, so pharmacy cannot be asked to supply it.",
            code="order_not_live",
            order=order,
        )
    source = order.get("supply_source")
    if source not in (None, SUPPLY_PHARMACY):
        raise core.MedOrderConflict(
            f"This medicine is given from {_SUPPLY_SOURCE_WORDS.get(source, source)}, "
            "not supplied by pharmacy.",
            code="supply_source_conflict",
            order=order,
            supply_source=source,
        )
    existing = open_supply_in_txn(conn, order["id"])
    if existing is not None:
        return SupplyRequest(order, existing, None, True)
    if not order.get("formulary_id"):
        raise core.MedOrderConflict(NOT_BOUND_MESSAGE, code="not_formulary_bound", order=order)
    if any(_is_placeholder(order.get(f)) for f in ("dose", "route", "frequency")):
        raise core.NeedsInput(
            PLACEHOLDER_REQUEST_MESSAGE, code="dose_route_and_frequency_required", order=order
        )

    from services import helix_service

    try:
        supply = helix_service.insert_order(
            conn,
            order["patient_id"],
            order["formulary_id"],
            order["dose"],
            order["route"],
            order["frequency"],
            order.get("prescriber_id") or order["entered_by"],
        )
    except ValueError as e:
        raise core.MedOrderConflict(
            str(e), code="formulary_product_unavailable", order=order
        ) from e
    conn.execute(
        "UPDATE helix_patient_med_orders SET med_order_id = ? "
        "WHERE id = ? AND med_order_id IS NULL",
        (order["id"], supply["id"]),
    )
    now = now or utc_iso_now()
    if source is None:
        # MO-R12 (a): asking pharmacy is the prescriber's choice of supply
        # source for this order. Not a change to what the order prescribes,
        # so its version (the screens' concurrency stamp) is untouched.
        conn.execute(
            "UPDATE med_orders SET supply_source = ?, updated_at = ? "
            "WHERE id = ? AND supply_source IS NULL",
            (SUPPLY_PHARMACY, now, order["id"]),
        )
    after = core.get_order_in_txn(conn, order["id"])
    assert after is not None, "Medication order disappeared inside its transaction"
    payload = {
        "helix_order_id": supply["id"],
        "formulary_id": order["formulary_id"],
        "supply_source": SUPPLY_PHARMACY,
    }
    event_id = core.record_event_in_txn(
        conn,
        order=after,
        kind="supply_requested",
        from_status=status,
        to_status=status,
        actor_id=actor_id,
        actor_role=actor_role,
        entry_point=entry_point,
        ref_type=REF_HELIX_ORDER,
        ref_id=supply["id"],
        payload=payload,
        created_at=now,
    )
    core.audit_in_txn(
        conn,
        order=after,
        action="med_order_supply_requested",
        actor_id=actor_id,
        actor_role=actor_role,
        entry_point=entry_point,
        from_status=status,
        to_status=status,
        lane=lane,
        event_id=event_id,
        extra={"helix_order_id": str(supply["id"]), "formulary_id": str(order["formulary_id"])},
        request_meta=request_meta,
    )
    row = conn.execute(
        "SELECT * FROM helix_patient_med_orders WHERE id = ?", (supply["id"],)
    ).fetchone()
    return SupplyRequest(after, dict(row), event_id, False)


def request_supply(
    order_id: str, *, actor_id: str, actor_role: str | None, request_meta: Mapping | None = None
) -> dict:
    """POST /api/medorders/orders/{id}/request-supply. BLOCKING: one immediate
    transaction. Returns {order, supply, event_id, replayed}."""
    with db_session(immediate=True) as conn:
        result = request_supply_in_txn(
            conn, order_id, actor_id=actor_id, actor_role=actor_role, request_meta=request_meta
        )
    return {
        "order": result.order,
        "supply": result.supply,
        "event_id": result.event_id,
        "replayed": result.replayed,
    }


# ── The prescriber's choice of supply source (MO-R12 (a)) ─────────────────────


def set_supply_source_in_txn(
    conn,
    order_id: str,
    supply_source: str,
    *,
    actor_id: str,
    actor_role: str | None,
    expected_version: int | None = None,
    entry_point: str = ENTRY_SUPPLY_SOURCE,
    lane: str = LANE_SUPPLY,
    request_meta: Mapping | None = None,
    now: str | None = None,
) -> core.StoreWrite:
    """Record where this order's medicine comes from (pharmacy, ward stock or
    the patient's own supply), in the caller's immediate transaction. Ruling
    MO-R12 (a): the prescriber picks it on each order. It changes only whether
    the pharmacy queue sees the order, never who may give a dose.

    The same guarded UPDATE as every store write (a new version), an 'amended'
    event with the before and after values and its clinical_access audit row.
    Choosing the source the order already has writes nothing (replayed).
    Refused for an order that has ended, and for a move away from pharmacy
    while a pharmacy supply is still open (pharmacy rejects it first, so it
    never prepares a medicine the ward will not use)."""
    _require_txn(conn)
    if supply_source not in core.SUPPLY_SOURCES:
        raise core.MedOrderInvalid(INVALID_SOURCE_MESSAGE, code="invalid_supply_source")
    order = core.get_order_in_txn(conn, order_id)
    if order is None:
        raise core.MedOrderNotFound("This medication order no longer exists")
    if expected_version is not None and order["version"] != expected_version:
        raise core.StaleOrderVersion(STALE_ORDER_MESSAGE, order=order)
    status = order["status"]
    if status in core.TERMINAL_STATUSES:
        words = _NOT_LIVE_WORDS.get(status, "has ended")
        raise core.TransitionNotAllowed(
            f"This medicine's order {words}, so where it comes from cannot be changed.",
            code="order_not_live",
            order=order,
        )
    before = order.get("supply_source")
    if before == supply_source:
        return core.StoreWrite(order, None, True)
    if supply_source != SUPPLY_PHARMACY and open_supply_in_txn(conn, order["id"]) is not None:
        raise core.MedOrderConflict(OPEN_SUPPLY_MESSAGE, code="supply_open", order=order)

    now = now or utc_iso_now()
    cur = conn.execute(
        "UPDATE med_orders SET supply_source = ?, version = ?, updated_at = ? "
        "WHERE id = ? AND version = ? AND status = ?",
        (supply_source, order["version"] + 1, now, order["id"], order["version"], status),
    )
    if cur.rowcount != 1:
        raise core.StaleOrderVersion(
            STALE_ORDER_MESSAGE, order=core.get_order_in_txn(conn, order["id"]) or order
        )
    after = core.get_order_in_txn(conn, order["id"])
    assert after is not None, "Medication order disappeared inside its transaction"
    changes = {"supply_source": {"before": before, "after": supply_source}}
    event_id = core.record_event_in_txn(
        conn,
        order=after,
        kind="amended",
        from_status=status,
        to_status=status,
        actor_id=actor_id,
        actor_role=actor_role,
        entry_point=entry_point,
        payload={"changes": changes},
        created_at=now,
    )
    core.audit_in_txn(
        conn,
        order=after,
        action="med_order_amended",
        actor_id=actor_id,
        actor_role=actor_role,
        entry_point=entry_point,
        from_status=status,
        to_status=status,
        lane=lane,
        event_id=event_id,
        extra={
            "amended_fields": ["supply_source"],
            "supply_source_before": before,
            "supply_source_after": supply_source,
        },
        request_meta=request_meta,
    )
    return core.StoreWrite(after, event_id, False)


def set_supply_source(
    order_id: str,
    supply_source: str,
    *,
    actor_id: str,
    actor_role: str | None,
    expected_version: int | None = None,
    request_meta: Mapping | None = None,
) -> dict:
    """POST /api/medorders/orders/{id}/supply-source. BLOCKING: one immediate
    transaction. Returns {order, event_id, replayed}."""
    with db_session(immediate=True) as conn:
        write = set_supply_source_in_txn(
            conn,
            order_id,
            supply_source,
            actor_id=actor_id,
            actor_role=actor_role,
            expected_version=expected_version,
            request_meta=request_meta,
        )
    return {"order": write.order, "event_id": write.event_id, "replayed": write.replayed}


# ── The Helix hook ────────────────────────────────────────────────────────────


def linked_supply(helix_order_id: str, *, idempotency_key: str | None = None) -> dict | None:
    """What the pharmacy route needs before it screens a step on a supply row:
    {supply, order, fingerprint}, or None for a row that is not linked, an
    unknown row, or a replayed idempotency key (a replay writes nothing, so it
    is answered as it was the first time). Read outside any write transaction,
    like the safety screen that follows it."""
    with db_session() as conn:
        if not _has_supply_link(conn):
            return None
        if (
            idempotency_key
            and conn.execute(
                "SELECT 1 FROM helix_transitions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        ):
            return None
        row = conn.execute(
            "SELECT * FROM helix_patient_med_orders WHERE id = ?", (helix_order_id,)
        ).fetchone()
        if row is None or not row["med_order_id"]:
            return None
        order = core.get_order_in_txn(conn, row["med_order_id"])
        fingerprint = core.screen_fingerprint_in_txn(conn, row["patient_id"])
    return {"supply": dict(row), "order": order, "fingerprint": fingerprint}


def _decides_order(supply: Mapping, order: Mapping) -> bool:
    """Is this Helix verification or rejection the order's own pharmacist
    check? Only while the order still waits for one, and never on a supply
    carrying a pharmacy substitute (held ruling R002)."""
    if _substituted(supply):
        return False
    return order["status"] == "prescribed" or (
        order["status"] == "held" and order["held_from"] == "prescribed"
    )


def _refuse_unverifiable_supply(supply: Mapping, order: Mapping) -> None:
    if any(
        _is_placeholder(v)
        for v in (supply.get("dosage"), supply.get("route"), order.get("dose"), order.get("route"))
    ):
        raise core.NeedsInput(PLACEHOLDER_VERIFY_MESSAGE, code="placeholder_dose", order=order)
    for supply_col, order_col in (
        ("dosage", "dose"),
        ("route", "route"),
        ("frequency", "frequency"),
    ):
        if _norm(supply.get(supply_col)) != _norm(order.get(order_col)):
            raise core.MedOrderConflict(OUT_OF_DATE_MESSAGE, code="supply_out_of_date", order=order)


def plan_helix_step(
    supply: Mapping,
    order: Mapping | None,
    to_status: str,
    *,
    lifecycle: bool,
    may_verify_order: bool,
) -> str:
    """What a Helix step on a LINKED supply row does to its order, or the
    refusal (a core.MedOrderError):

      'decide'  the lifecycle is on and this verification or rejection is the
                order's pharmacist check (needs medication.verify)
      'event'   a supply event on the order's ledger
      'none'    nothing ('scheduled')

    Pure: the route calls it before screening, the hook again inside the
    transaction."""
    if order is None:
        raise core.MedOrderConflict(BROKEN_LINK_MESSAGE, code="order_link_broken")
    status = order["status"]
    if to_status in COMMITTING_STEPS and (status == "draft" or status in core.TERMINAL_STATUSES):
        words = _NOT_LIVE_WORDS.get(status, "cannot be supplied")
        tail = "" if status == "draft" else " Reject this supply instead."
        raise core.TransitionNotAllowed(
            f"This medicine's order {words}, so pharmacy cannot "
            f"{_STEP_WORDS.get(to_status, 'supply')} it.{tail}",
            code="order_not_live",
            order=order,
        )
    if to_status == "verified":
        _refuse_unverifiable_supply(supply, order)
    if lifecycle and to_status in DECISION_STEPS and _decides_order(supply, order):
        if not may_verify_order:
            verb = "verify" if to_status == "verified" else "reject"
            raise core.NotPermitted(
                f"Only a pharmacist can {verb} this medication order.",
                code="pharmacist_required",
                order=order,
            )
        return "decide"
    return "event" if to_status in SUPPLY_EVENT_FOR_STEP else "none"


def on_helix_step_in_txn(
    conn,
    supply: Mapping,
    from_status: str,
    to_status: str,
    transition_id: str | None,
    *,
    actor_id: str | None,
    actor_role: str | None,
    may_verify_order: bool = False,
    expected_version: int | None = None,
    fingerprint: str | None = None,
    safety: Mapping | None = None,
    notes: str = "",
    request_meta: Mapping | None = None,
) -> core.StoreWrite | None:
    """The Helix hook's body, inside Helix's transaction. None for an
    unlinked supply row (nothing to do), and for 'scheduled'."""
    link = supply.get("med_order_id")
    if not link:
        return None
    order = core.get_order_in_txn(conn, link)
    if order is None and to_status == "delivered":
        # The drug is on the ward: its receipt is recorded whatever the store
        # says, and the parity report lists the broken link.
        logger.warning(
            "supply %s received for a missing medication order %s", supply.get("id"), link
        )
        return None
    lifecycle = core.lifecycle_on()
    plan = plan_helix_step(
        supply, order, to_status, lifecycle=lifecycle, may_verify_order=may_verify_order
    )
    assert order is not None, "A validated supply step requires its medication order"
    if to_status in GUARDED_STEPS:
        if expected_version is not None and order["version"] != expected_version:
            raise core.StaleOrderVersion(STALE_ORDER_MESSAGE, order=order)
        if (
            fingerprint is not None
            and to_status in _SCREENED_STEPS
            and core.screen_fingerprint_in_txn(conn, order["patient_id"]) != fingerprint
        ):
            raise core.StaleScreen(STALE_SCREEN_MESSAGE, order=order)
    if plan == "none":
        return None

    entry = f"helix.{to_status}"
    payload = _payload(supply, from_status, to_status, transition_id)
    extra = {
        "helix_order_id": str(supply.get("id")),
        "helix_transition_id": str(transition_id) if transition_id else None,
        "helix_status": to_status,
    }
    reason = (notes or "").strip() or None
    if plan == "decide":
        decision_reason = reason if to_status == "rejected" else None
        if order["status"] == "held":
            write = core.legacy_decision_on_held_in_txn(
                conn,
                order["id"],
                to_status,
                actor_id=actor_id,
                actor_role=actor_role,
                entry_point=entry,
                reason=decision_reason,
                lane=LANE_HELIX,
                audit_extra=extra,
                request_meta=request_meta,
            )
            if to_status == "verified":
                # The verifier reaches the ward row (invariant I1); the hold
                # marker stays exactly as it was.
                core.project_in_txn(conn, write.order)
            return write
        return core.transition_in_txn(
            conn,
            order["id"],
            to_status,
            expected_version=None,
            actor_id=actor_id,
            actor_role=actor_role,
            entry_point=entry,
            reason=decision_reason,
            lane=LANE_HELIX,
            payload=payload,
            safety=safety,
            fingerprint=fingerprint,
            ref_type=REF_HELIX_ORDER,
            ref_id=supply.get("id"),
            audit_extra=extra,
            request_meta=request_meta,
        )

    kind = SUPPLY_EVENT_FOR_STEP[to_status]
    event_reason = reason if to_status in ("rejected", "substitute_suggested") else None
    event_id = core.record_event_in_txn(
        conn,
        order=order,
        kind=kind,
        from_status=order["status"],
        to_status=order["status"],
        actor_id=actor_id,
        actor_role=actor_role,
        entry_point=entry,
        reason=event_reason,
        ref_type=REF_HELIX_ORDER,
        ref_id=supply.get("id"),
        payload=payload,
    )
    core.audit_in_txn(
        conn,
        order=order,
        action=f"med_order_{kind}",
        actor_id=actor_id,
        actor_role=actor_role,
        entry_point=entry,
        from_status=order["status"],
        to_status=order["status"],
        reason=event_reason,
        lane=LANE_HELIX,
        event_id=event_id,
        safety=safety if to_status in _SCREENED_STEPS else None,
        extra=extra,
        request_meta=request_meta,
    )
    return core.StoreWrite(order, event_id, False)


def helix_step_hook(
    *,
    actor_id: str | None,
    actor_role: str | None,
    may_verify_order: bool = False,
    expected_version: int | None = None,
    fingerprint: str | None = None,
    safety: Mapping | None = None,
    notes: str = "",
    request_meta: Mapping | None = None,
) -> Callable:
    """The on_applied callback helix_service.transition_order_status and
    receive_order call inside their transaction."""

    def _hook(conn, supply, from_status, to_status, transition_id):
        return on_helix_step_in_txn(
            conn,
            supply,
            from_status,
            to_status,
            transition_id,
            actor_id=actor_id,
            actor_role=actor_role,
            may_verify_order=may_verify_order,
            expected_version=expected_version,
            fingerprint=fingerprint,
            safety=safety,
            notes=notes,
            request_meta=request_meta,
        )

    return _hook


# ── The dose's custody link ───────────────────────────────────────────────────


def custody_for_dose_in_txn(conn, linked: Mapping | None) -> dict | None:
    """The supply row a given dose on a linked ward row came from, in the
    dose's transaction (after its INSERT, so under the write lock: two doses
    cannot both take the last unit). None for an unlinked row or a broken
    link. Otherwise {linked: True, helix_order_id} when exactly one supply
    row of the order is delivered, signed for and not named by an earlier
    dose; else {linked: False, helix_order_id: None, note: 'supply not
    linked', received_unused: n}."""
    if not linked or not linked.get("order"):
        return None
    order_id = linked.get("med_order_id") or linked["order"].get("id")
    received: list[str] = []
    if _has_supply_link(conn):
        received = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM helix_patient_med_orders WHERE med_order_id = ? "
                "AND status = 'delivered' AND received_by IS NOT NULL "
                "ORDER BY received_at, id",
                (order_id,),
            ).fetchall()
        ]
    used: set = set()
    if received:
        for row in conn.execute(
            "SELECT payload_json FROM med_order_events WHERE med_order_id = ? "
            "AND kind = 'administered'",
            (order_id,),
        ).fetchall():
            raw = row[0]
            try:
                named = ((json.loads(raw) if raw else {}).get("supply") or {}).get("helix_order_id")
            except (TypeError, ValueError, AttributeError):
                named = None
            if named:
                used.add(named)
    unused = [i for i in received if i not in used]
    if len(unused) == 1:
        return {"linked": True, "helix_order_id": unused[0]}
    return {
        "linked": False,
        "helix_order_id": None,
        "note": SUPPLY_NOT_LINKED,
        "received_unused": len(unused),
    }
