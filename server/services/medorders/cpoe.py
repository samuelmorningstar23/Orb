"""Orb: CPOE and order-set medicines through the medication-order store
(medorders-7, W7-F).

Before this, a medication order placed under Orders, or a medication item of an
order set, was a draft in the orders table that reached nothing: the nurse
board, the eMAR and the interaction screen all read the ward chart
(medications), and the screens said so (MED_NOT_SENT). With
ORB_MED_ORDER_LIFECYCLE on, each such order is ALSO a draft medication order,
linked by med_orders.cpoe_order_id and written in the same transaction as the
orders row. A prescriber's signature (the store's sign step,
services/medorders/chart_lane.sign_order) is the release: it screens the order
again with the signer's role, writes the ward row and links it, so the drug is
on the board, the eMAR and the interaction screen from that moment. With the
lifecycle off nothing here runs and nothing is linked.

THE LINK DECIDES, NOT THE SWITCH (ruling MO-R9 (a)). Once an orders row is
linked, the store owns its lifecycle for life:
  * orders_service.transition_order refuses every move on it (409
    managed_by_medication_order, naming the medication order);
  * the orders route hands a cancel to the store (cancel_linked below): an
    unsigned draft is cancelled with a reason by its creator or anyone in
    scope who may write orders; a signed one is a prescriber's step (ruling
    MO-R2 (a)), refused 403 to anyone else and 409 cancel_not_offered to a
    prescriber until that step is built;
  * the store writes the orders row's status itself (mirror_cpoe_status_in_txn,
    called by core.transition_in_txn on every step of a linked order), as a
    guarded UPDATE plus an order_events row inside the store's transaction:
    draft stays draft; prescribed, verified and held are active; stopped,
    cancelled and rejected are cancelled; completed is completed.

THE SHADOW INSTRUMENT. A linked medication order is counted as a clinician's
action (shadow clinical.action) when it is SIGNED, with the signer as the
actor, and not when the draft is written (after_signed below; the orders
route skips the create-time record for a linked order). docs/SHADOW_MODE.md
section 5.3 records the timing change.

RULINGS APPLIED HERE
  MO-R10 (a)  a draft nobody signs is never cancelled by the system; the
              Orders list marks one waiting over queries.DRAFT_WAIT_HOURS,
              as the to-sign queue does.
  MO-R11 (a)  a CPOE medicine may be free text with no code; the Orders list
              says plainly when the drug is not on the formulary (on_formulary
              below, an exact name match, never LIKE). Binding a product is
              the pharmacist's step at verification, not built here.
  MO-R12      not ruled into code: a signed CPOE order raises no pharmacy
              supply request (the screens say so).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from services import orders_service
from services.clinical_time import format_utc
from services.medorders import core, queries

logger = logging.getLogger("orb.medorders.cpoe")

__all__ = [
    "ENTRY_CPOE_CREATE",
    "ENTRY_ORDER_SET_APPLY",
    "LANE_CPOE",
    "LANE_ORDER_SET",
    "MANAGED_CODE",
    "LINKED_SOURCES",
    "lifecycle_on",
    "structured_medication",
    "description_names_drug",
    "create_linked_draft_in_txn",
    "linked_order_in_txn",
    "linked_order",
    "mirror_cpoe_status_in_txn",
    "cancel_linked",
    "annotate_orders",
    "after_signed",
]

#: The entry points and lanes this module writes under. ENTRY_CPOE_CREATE is
#: the same name the order route's safety gate audits under.
ENTRY_CPOE_CREATE = "orders.cpoe"
ENTRY_ORDER_SET_APPLY = "order_set.apply"
LANE_CPOE = "cpoe"
LANE_ORDER_SET = "order_set"

#: detail.error on the 409 for a manual move of a linked order.
MANAGED_CODE = "managed_by_medication_order"

#: The order sources a linked draft can carry (core.SOURCES holds both).
LINKED_SOURCES = ("cpoe", "order_set")

#: What the orders row's event trail says for each store step.
_MIRROR_WORDS = {
    "signed": "Signed by a prescriber",
    "verified": "Verified by a pharmacist",
    "rejected": "Rejected by the pharmacist",
    "held": "Put on hold",
    "resumed": "Resumed",
    "stopped": "Stopped",
    "completed": "Course completed",
    "cancelled": "Cancelled",
}

#: The fields of the linked order the Orders list shows.
_SUMMARY_FIELDS = (
    "id",
    "status",
    "version",
    "source",
    "drug_name",
    "dose",
    "route",
    "frequency",
    "is_prn",
    "prn_indication",
    "priority",
    "entered_by",
    "entered_by_role",
    "prescriber_id",
    "prescribed_at",
    "verified_by",
    "verified_at",
    "status_reason",
    "status_changed_at",
    "legacy_medication_id",
    "needs_verification",
    "created_at",
)


def lifecycle_on() -> bool:
    """ORB_MED_ORDER_LIFECYCLE, read at call time (it is off whenever the
    store is)."""
    return core.lifecycle_on()


def _clean(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = " ".join(str(value).split())
    return text or None


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


# ── The order's fields ────────────────────────────────────────────────────────


def structured_medication(detail: Mapping | None) -> dict:
    """The medicine's own fields from an order's detail.medication: drug_name,
    dose, route, frequency, is_prn, prn_indication. Blank text is None; is_prn
    is True only for a literal true (a string 'false' is not a PRN order)."""
    medication = (detail or {}).get("medication") if isinstance(detail, Mapping) else None
    if not isinstance(medication, Mapping):
        medication = {}
    return {
        "drug_name": _clean(medication.get("drug_name")),
        "dose": _clean(medication.get("dose")),
        "route": _clean(medication.get("route")),
        "frequency": _clean(medication.get("frequency")),
        "is_prn": medication.get("is_prn") is True,
        "prn_indication": _clean(medication.get("prn_indication")),
    }


def description_names_drug(description: str | None, drug_name: str | None) -> bool:
    """Does the order's description name the medicine it carries? The list
    shows the description and the ward receives the drug, so the two must not
    disagree (case and spacing ignored)."""
    drug = _norm(drug_name)
    return bool(drug) and drug in _norm(description)


# ── Writers (the caller's transaction) ───────────────────────────────────────


def linked_order_in_txn(conn, cpoe_order_id: str) -> dict | None:
    """The medication order linked to an orders row, or None."""
    row = conn.execute(
        "SELECT * FROM med_orders WHERE cpoe_order_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (cpoe_order_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def create_linked_draft_in_txn(
    conn,
    cpoe_order: Mapping,
    *,
    source: str,
    drug_name: str,
    actor_id: str,
    actor_role: str | None,
    entry_point: str,
    lane: str,
    dose: str | None = None,
    route: str | None = None,
    frequency: str | None = None,
    is_prn: bool = False,
    prn_indication: str | None = None,
    notes: str | None = None,
    order_set_application_id: int | None = None,
    request_meta: Mapping | None = None,
) -> core.StoreWrite:
    """The draft medication order beside a just-written orders row, in the
    SAME transaction (orders_service.create_order's in_transaction hook), so
    the two commit together or not at all. A draft has no ward row: it
    reaches nothing until a prescriber signs it. Its first event and its
    clinical_access audit row (fail-closed) are core.create_in_txn's."""
    if source not in LINKED_SOURCES:
        raise core.MedOrderInvalid(
            f"A linked draft comes from CPOE or an order set, not {source!r}", code="invalid_source"
        )
    if cpoe_order.get("order_type") != "medication":
        raise core.MedOrderInvalid(
            "Only a medication order is linked to a medication order", code="not_a_medication_order"
        )
    if cpoe_order.get("status") != "draft":
        raise core.MedOrderInvalid("A linked order starts as a draft", code="invalid_status")
    if linked_order_in_txn(conn, cpoe_order["id"]) is not None:
        raise core.MedOrderConflict(
            "This order already has a medication order", code="order_already_linked"
        )
    priority = cpoe_order.get("priority")
    return core.create_in_txn(
        conn,
        patient_id=cpoe_order["patient_id"],
        drug_name=drug_name,
        source=source,
        status="draft",
        entered_by=actor_id,
        entered_by_role=actor_role,
        actor_id=actor_id,
        actor_role=actor_role,
        entry_point=entry_point,
        admission_id=queries.active_admission_id(conn, cpoe_order["patient_id"]),
        dose=dose,
        route=route,
        frequency=frequency,
        is_prn=is_prn,
        prn_indication=prn_indication if is_prn else None,
        priority=priority if priority in core.PRIORITIES else None,
        notes=notes,
        cpoe_order_id=cpoe_order["id"],
        order_set_application_id=order_set_application_id,
        event_payload={
            "cpoe_order_id": cpoe_order["id"],
            "order_set_application_id": order_set_application_id,
        },
        lane=lane,
        request_meta=request_meta,
    )


def mirror_cpoe_status_in_txn(
    conn,
    order: Mapping,
    *,
    from_status: str,
    kind: str,
    actor_id: str | None,
    reason: str | None = None,
) -> dict | None:
    """After the store moved a linked order from `from_status` to its current
    status, move its orders row to match, in the same transaction
    (core.transition_in_txn calls this for every linked order). Returns
    {cpoe_order_id, from, to, outcome} for the audit row, or None for an order
    with no CPOE row.

    The UPDATE is guarded on the status the orders row should still have. If
    it says something else, nothing is written to it, and:
      * a step that puts the drug on the ward (sign, resume) is REFUSED
        (409 cpoe_order_changed): a drug never reaches the round while its
        order says otherwise;
      * a step that takes the drug off the ward or ends it is NOT refused (a
        stop must never be blocked by a stale view); the mismatch is logged
        and written to the store's audit row."""
    cpoe_order_id = order.get("cpoe_order_id")
    if not cpoe_order_id:
        return None
    expected = core.cpoe_status_for(from_status)
    target = core.cpoe_status_for(order["status"])
    summary = {"cpoe_order_id": cpoe_order_id, "from": expected, "to": target}
    if expected == target:
        return {**summary, "outcome": "unchanged"}
    words = _MIRROR_WORDS.get(kind, kind)
    detail = f"Medication order: {words}" + (f": {reason}" if reason else "")
    result = orders_service.mirror_medication_status_in_conn(
        conn,
        cpoe_order_id,
        expected_status=expected,
        to_status=target,
        actor_id=actor_id,
        detail=detail,
    )
    if result["outcome"] in ("mirrored", "already"):
        return {**summary, "outcome": result["outcome"]}
    if target == "active":
        raise core.MedOrderConflict(
            "This medicine's order under Orders changed while it was being signed. "
            "Refresh Orders and check it again.",
            code="cpoe_order_changed",
            order=order,
            cpoe_order_id=cpoe_order_id,
            cpoe_status=result["status"],
        )
    logger.warning(
        "CPOE order %s says %r, not %r, as medication order %s became %s",
        cpoe_order_id,
        result["status"],
        expected,
        order.get("id"),
        order.get("status"),
    )
    return {**summary, "outcome": result["outcome"], "cpoe_status": result["status"]}


# ── Blocking steps (call from a def handler) ──────────────────────────────────


def linked_order(cpoe_order_id: str) -> dict | None:
    """linked_order_in_txn on its own read session. BLOCKING."""
    from database import db_session

    with db_session() as conn:
        return linked_order_in_txn(conn, cpoe_order_id)


def cancel_linked(
    cpoe_order_id: str,
    *,
    actor_id: str,
    actor_role: str | None,
    reason: str | None,
    may_cancel_any_draft: bool,
    may_prescribe: bool,
    request_meta: Mapping | None = None,
) -> dict:
    """The orders route's Cancel on a linked order, handed to the store's own
    cancel (chart_lane.cancel_draft: a draft by its creator or an order.write
    holder, with a reason; a signed order is a prescriber's step not offered
    yet). The store's transition moves the orders row too. BLOCKING.

    A cancel of an order that is already cancelled writes nothing and answers
    as it stands (replayed True), as a cancel on Orders always has: a retried
    tap is not an error. Returns {order: the orders row after, med_order: the
    medication order, replayed}."""
    from services.medorders import chart_lane

    med = linked_order(cpoe_order_id)
    if med is None:
        raise core.MedOrderNotFound("This medicine's medication order no longer exists")
    if med["status"] == "cancelled":
        return {
            "order": orders_service.get_order(cpoe_order_id),
            "med_order": med,
            "replayed": True,
        }
    result = chart_lane.cancel_draft(
        med["id"],
        actor_id=actor_id,
        actor_role=actor_role,
        expected_version=None,
        reason=reason,
        may_cancel_any_draft=may_cancel_any_draft,
        may_prescribe=may_prescribe,
        request_meta=request_meta,
    )
    return {
        "order": orders_service.get_order(cpoe_order_id),
        "med_order": result["order"],
        "replayed": False,
    }


# ── Reads ─────────────────────────────────────────────────────────────────────


def _formulary_names(conn) -> set | None:
    """Every name and generic name on the formulary, normalised; None when
    the formulary cannot be read (the screen then says nothing either way)."""
    queries_to_try = (
        "SELECT name, generic_name FROM helix_medication_master "
        "WHERE COALESCE(is_active, 1) = 1 AND COALESCE(identity_status, 'active') = 'active'",
        "SELECT name, generic_name FROM helix_medication_master",
    )
    for sql in queries_to_try:
        try:
            rows = conn.execute(sql).fetchall()
        except Exception as exc:  # sqlite3 or sqlcipher3 OperationalError
            text = str(exc).lower()
            if "no such column" in text:
                continue
            if "no such table" in text:
                return None
            raise
        names = set()
        for r in rows:
            for value in (r[0], r[1]):
                if _norm(value):
                    names.add(_norm(value))
        return names
    return None


def _user_names(conn, ids: Iterable[str | None]) -> dict:
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    marks = ", ".join("?" for _ in wanted)
    return {
        r["id"]: r["name"]
        for r in conn.execute(
            f"SELECT id, name FROM users WHERE id IN ({marks})", wanted
        ).fetchall()
    }


def annotate_orders(conn, orders: list[dict], *, now: datetime | None = None) -> None:
    """Add what the Orders screen needs to every LINKED medication order in
    `orders`, in place: managed_by_medication_order True and
    medication_order {the order's state and fields, prescriber_name,
    entered_by_name, waiting_over_24h (a draft only, ruling MO-R10 (a)),
    on_formulary (ruling MO-R11 (a); None when the formulary cannot be
    read)}. An order with no link gets no new key, so every answer is as it
    was until the lifecycle links something. Read from the link whatever the
    switches say now. Writes nothing."""
    medication = [o for o in orders if o.get("order_type") == "medication"]
    if not medication:
        return
    linked: dict[str, dict] = {}
    ids = [o["id"] for o in medication]
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        marks = ", ".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                f"SELECT * FROM med_orders WHERE cpoe_order_id IN ({marks}) "
                "ORDER BY created_at, id",
                chunk,
            ).fetchall()
        except Exception as exc:
            if "no such table" in str(exc).lower():
                rows = []
            else:
                raise
        for r in rows:
            linked[r["cpoe_order_id"]] = dict(r)  # the newest wins, as linked_order_in_txn
    if not linked:
        return
    formulary = _formulary_names(conn)
    names = _user_names(
        conn,
        [m.get("prescriber_id") for m in linked.values()]
        + [m.get("entered_by") for m in linked.values()],
    )
    cutoff = format_utc((now or datetime.now(UTC)) - timedelta(hours=queries.DRAFT_WAIT_HOURS))
    for o in medication:
        med = linked.get(o["id"])
        if med is None:
            continue
        o["managed_by_medication_order"] = True
        summary = {k: med.get(k) for k in _SUMMARY_FIELDS}
        summary["is_prn"] = bool(summary.get("is_prn"))
        summary["needs_verification"] = bool(summary.get("needs_verification"))
        summary["prescriber_name"] = names.get(med.get("prescriber_id"))
        summary["entered_by_name"] = names.get(med.get("entered_by"))
        summary["waiting_over_24h"] = bool(
            med.get("status") == "draft" and med.get("created_at") and med["created_at"] < cutoff
        )
        summary["on_formulary"] = (
            None if formulary is None else _norm(med.get("drug_name")) in formulary
        )
        o["medication_order"] = summary


# ── After a signature commits ─────────────────────────────────────────────────


def after_signed(order: Mapping | None, *, actor_id: str | None, actor_role: str | None) -> None:
    """The shadow clinical.action for a signed CPOE or order-set medicine,
    once, with the signer as the actor (orders_service.record_clinical_action,
    the same emitter the orders route uses at create). Call it after the sign
    commits, and not for a replay. Never raises."""
    try:
        if not order or not order.get("cpoe_order_id") or order.get("source") not in LINKED_SOURCES:
            return
        orders_service.record_clinical_action(
            order["patient_id"],
            "medication",
            order["cpoe_order_id"],
            actor_id=actor_id,
            actor_role=actor_role,
        )
    except Exception:  # noqa: BLE001  (the signature has committed; never fail it)
        logger.warning(
            "shadow clinical.action for signed order %s not recorded", (order or {}).get("id")
        )
