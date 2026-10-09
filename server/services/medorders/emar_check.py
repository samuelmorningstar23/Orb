"""Orb: the eMAR's order-state check for a ward row linked to a medication
order (medorders-3, W5-E; design section 6).

emar_service.record_administration keeps every check it had (an active ward
row, a pharmacist for an AI-charted drug, the inpatient guard, the allergy
interlock, the dose guard). For a ward row LINKED to an order it asks one more
question: does the ORDER allow this record? core.order_allows_dose answers it:
a held, unsigned or ended order refuses 'given'; 'not_given' is always allowed,
so withholding a dose, including 'not given (held)', is always easy to record.

KEYED ON THE LINK, NOT THE SWITCH (ruling MO-R9 (a)): a held order stays
ungivable after ORB_MED_ORDER_STORE or ORB_MED_ORDER_LIFECYCLE is switched off.
A database that never ran the store has no links, so nothing here changes for
it. An unlinked ward row is never touched.

WHERE THE CHECK SITS, and why (emar_service has the long form):
  * BELOW the idempotent replay check and the discharged-patient guard: a
    replay of a dose given before a hold returns the original record, never a
    4xx the offline client would treat as a failed sync;
  * BEFORE the allergy interlock, so a held drug is refused cleanly before the
    nurse is asked about an allergy;
  * AGAIN AFTER the medication_administrations INSERT, in the same
    transaction: the INSERT holds the write lock, so no hold can commit after
    this second read, and a hold that committed between the first read and the
    INSERT is seen here and rolls the dose back. (A db_session(immediate=True)
    around the whole call would close the window too, at the price of
    serialising every dose on the ward.)

The bedside event ('administered' or 'not_given') is appended in the dose's
own transaction, so it commits with the MAR row and its audit row or not at
all. The dose's clinical_access audit row carries the order's id.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from services.medorders import core

__all__ = ["ENTRY_POINT", "OrderStateRefused", "check_dose_in_txn", "record_bedside_event_in_txn"]

ENTRY_POINT = "emar.administer"


class OrderStateRefused(ValueError):
    """The linked order refuses this dose. A ValueError, so the eMAR route
    answers it as it answers every other flat refusal: 422 with the words,
    which are written for the nurse at the bedside."""

    def __init__(self, verdict: Mapping):
        super().__init__(verdict.get("message") or
                         "The state of this order could not be read, so a dose cannot be given.")
        self.verdict = dict(verdict)


def _link(row: Any) -> Optional[str]:
    if row is None:
        return None
    try:
        keys = row.keys()
    except AttributeError:
        return None
    return row["med_order_id"] if "med_order_id" in keys else None


def check_dose_in_txn(conn, medication_id: int, administration_status: str) -> Optional[dict]:
    """None for an unlinked ward row. For a linked one, {med_order_id, order}
    (order None when the link names an order that does not exist), after
    raising OrderStateRefused when the order refuses this record.

    Reads the link afresh on every call, so the second call after the INSERT
    also sees a link adoption made in between. Writes nothing."""
    row = conn.execute("SELECT * FROM medications WHERE id = ?", (medication_id,)).fetchone()
    link = _link(row)
    if not link:
        return None
    order = core.get_order_in_txn(conn, link)
    verdict = core.order_allows_dose(order, administration_status)
    if not verdict["allowed"]:
        raise OrderStateRefused({**verdict, "med_order_id": link})
    return {"med_order_id": link, "order": order}


def record_bedside_event_in_txn(conn, linked: Optional[Mapping], *, medication_id: int,
                                administration_id: int, administration_status: str,
                                actor_id: Optional[str],
                                not_given_reason: Optional[str] = None,
                                confirmation_method: Optional[str] = None,
                                custody: Optional[Mapping] = None) -> Optional[int]:
    """Append the 'administered' or 'not_given' event for a linked ward row,
    in the dose's transaction. Returns the event id, or None for an unlinked
    row or a broken link (nothing to append to).

    `custody` is the dose's supply link (services/medorders/supply.py,
    custody_for_dose_in_txn), carried on the event's payload as 'supply'
    when given (medorders-5b)."""
    if not linked or not linked.get("order"):
        return None
    order = linked["order"]
    role = conn.execute("SELECT role FROM users WHERE id = ?", (actor_id,)).fetchone() \
        if actor_id else None
    payload = {"medication_id": medication_id, "confirmation_method": confirmation_method}
    if custody is not None:
        payload["supply"] = dict(custody)
    return core.record_event_in_txn(
        conn,
        order=order,
        kind="administered" if administration_status == "given" else "not_given",
        from_status=order["status"],
        to_status=order["status"],
        actor_id=actor_id,
        actor_role=role["role"] if role else None,
        entry_point=ENTRY_POINT,
        reason=not_given_reason,
        ref_type="medication_administration",
        ref_id=administration_id,
        payload=payload,
    )
