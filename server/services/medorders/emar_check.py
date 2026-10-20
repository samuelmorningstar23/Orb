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

THE PHARMACIST CHECK BEFORE THE FIRST DOSE (medorders-11, W8-I; ruling MO-R1,
a safety ruling held for Samuel). Only while ORB_MED_ORDER_VERIFY_BEFORE_DOSE
is on (it needs the lifecycle), and only for a LINKED ward row: a 'given' dose
also needs the ward row's verified_by, the one verification fact every verify
path writes (invariant I1). Unlinked rows are untouched; an AI-charted row
keeps the older check above this one (a pharmacist, whatever the switch).
Built to the strictest option named, (c): no dose of any order before the
check. ORB_MED_ORDER_FIRST_DOSE_BEFORE_CHECK='stat_urgent' gives option (a),
the architect's recommendation: the FIRST given dose of an order whose
priority is stat or urgent may go before the check, is marked on its bedside
event ('before_pharmacist_check'), on its audit row and on the MAR ('Given
before pharmacist check'), and every later dose waits for the check. That is
the retrospective window: the pharmacist checks before the next dose. Counted
from the MAR under the dose's write lock (the second check, after the INSERT,
leaves out the row just written), so two nurses cannot both give the 'first'
dose. 'not_given' is never refused. Unlike a hold (keyed on the order), this
rule follows the switch: switching it off returns the ward to today's rule.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.medorders import core

__all__ = [
    "ENTRY_POINT",
    "OrderStateRefused",
    "check_dose_in_txn",
    "record_bedside_event_in_txn",
    "NOT_VERIFIED_CODE",
    "FIRST_DOSE_RULES",
    "FIRST_DOSE_PRIORITIES",
    "verify_before_dose_rule",
    "pharmacist_check",
    "given_doses_in_txn",
]

ENTRY_POINT = "emar.administer"

#: The refusal code for a dose of an order no pharmacist has checked (MO-R1).
NOT_VERIFIED_CODE = "order_not_verified"
#: ORB_MED_ORDER_FIRST_DOSE_BEFORE_CHECK: 'none' (option (c), the default) or
#: 'stat_urgent' (option (a)).
FIRST_DOSE_RULES = ("none", "stat_urgent")
#: The order priorities whose first dose option (a) lets go before the check.
#: The order's structured priority only: a frequency typed as 'stat' on a
#: routine order does not count (the strict reading).
FIRST_DOSE_PRIORITIES = ("stat", "urgent")

_NOT_VERIFIED_WORDS = (
    "A pharmacist has not checked this order yet, so a dose cannot be given. "
    "You can still record it as not given."
)
_NEXT_DOSE_WORDS = (
    "The first dose of this {priority} order was given before the pharmacist "
    "check. The next dose waits until a pharmacist has checked the order. "
    "You can still record it as not given."
)


class OrderStateRefused(ValueError):
    """The linked order refuses this dose. A ValueError, so the eMAR route
    answers it as it answers every other flat refusal: 422 with the words,
    which are written for the nurse at the bedside."""

    def __init__(self, verdict: Mapping):
        super().__init__(
            verdict.get("message")
            or "The state of this order could not be read, so a dose cannot be given."
        )
        self.verdict = dict(verdict)


def _link(row: Any) -> str | None:
    if row is None:
        return None
    try:
        keys = row.keys()
    except AttributeError:
        return None
    return row["med_order_id"] if "med_order_id" in keys else None


def _field(row: Any, name: str) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(name)
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def verify_before_dose_rule() -> str | None:
    """None while ORB_MED_ORDER_VERIFY_BEFORE_DOSE is off (or its lifecycle
    is); otherwise which first doses may go before the check: 'none' or
    'stat_urgent'. Read at call time. An unknown setting reads as 'none'."""
    import config

    if not config.feature_enabled("med_order_verify_before_dose"):
        return None
    rule = getattr(config, "MED_ORDER_FIRST_DOSE_BEFORE_CHECK", "none")
    return rule if rule in FIRST_DOSE_RULES else "none"


def pharmacist_check(order: Any, ward_row: Any, *, given_doses: int, rule: str | None) -> dict:
    """Does the pharmacist check allow a 'given' dose of this LINKED order?
    Pure, so the shift board can show the bedside's answer before the walk to
    the bed. `given_doses` counts the ward row's earlier 'given' records.

    Returns {required, checked, allowed, before_check, code, message,
    priority}. `required` is False while the switch is off (rule None);
    `before_check` is True only for a first stat or urgent dose let through
    under rule 'stat_urgent'."""
    priority = _field(order, "priority")
    out = {
        "required": rule is not None,
        "checked": bool(_field(ward_row, "verified_by")),
        "allowed": True,
        "before_check": False,
        "code": None,
        "message": None,
        "priority": priority,
    }
    if rule is None or out["checked"]:
        return out
    if rule == "stat_urgent" and priority in FIRST_DOSE_PRIORITIES:
        if not given_doses:
            out["before_check"] = True
            return out
        out.update(
            allowed=False,
            code=NOT_VERIFIED_CODE,
            message=_NEXT_DOSE_WORDS.format(priority=priority),
        )
        return out
    out.update(allowed=False, code=NOT_VERIFIED_CODE, message=_NOT_VERIFIED_WORDS)
    return out


def given_doses_in_txn(
    conn, medication_id: int, *, exclude_administration_id: int | None = None
) -> int:
    """How many 'given' records the ward row has, leaving one out (the row
    the dose's own transaction has just written)."""
    sql = (
        "SELECT COUNT(*) FROM medication_administrations "
        "WHERE medication_id = ? AND status = 'given'"
    )
    args: list = [medication_id]
    if exclude_administration_id is not None:
        sql += " AND id != ?"
        args.append(exclude_administration_id)
    return int(conn.execute(sql, args).fetchone()[0])


def check_dose_in_txn(
    conn,
    medication_id: int,
    administration_status: str,
    *,
    exclude_administration_id: int | None = None,
) -> dict | None:
    """None for an unlinked ward row. For a linked one, {med_order_id, order}
    (order None when the link names an order that does not exist), after
    raising OrderStateRefused when the order refuses this record.

    With ORB_MED_ORDER_VERIFY_BEFORE_DOSE on, a 'given' dose of a linked
    order no pharmacist has checked is refused too (code order_not_verified),
    unless the first-dose rule lets it go before the check, in which case the
    answer carries before_pharmacist_check True with the rule and the order's
    priority. The second call, after the INSERT, passes the new row's id as
    `exclude_administration_id` so the dose does not count itself.

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
    linked: dict[str, Any] = {"med_order_id": link, "order": order}
    if administration_status == "given":
        rule = verify_before_dose_rule()
        if rule is not None:
            check = pharmacist_check(
                order,
                row,
                rule=rule,
                given_doses=given_doses_in_txn(
                    conn, medication_id, exclude_administration_id=exclude_administration_id
                ),
            )
            if not check["allowed"]:
                raise OrderStateRefused(
                    {
                        "allowed": False,
                        "code": check["code"],
                        "message": check["message"],
                        "priority": check["priority"],
                        "med_order_id": link,
                    }
                )
            if check["before_check"]:
                linked.update(
                    before_pharmacist_check=True, first_dose_rule=rule, priority=check["priority"]
                )
    return linked


def record_bedside_event_in_txn(
    conn,
    linked: Mapping | None,
    *,
    medication_id: int,
    administration_id: int,
    administration_status: str,
    actor_id: str | None,
    not_given_reason: str | None = None,
    confirmation_method: str | None = None,
    custody: Mapping | None = None,
) -> int | None:
    """Append the 'administered' or 'not_given' event for a linked ward row,
    in the dose's transaction. Returns the event id, or None for an unlinked
    row or a broken link (nothing to append to).

    `custody` is the dose's supply link (services/medorders/supply.py,
    custody_for_dose_in_txn), carried on the event's payload as 'supply'
    when given (medorders-5b)."""
    if not linked or not linked.get("order"):
        return None
    order = linked["order"]
    role = (
        conn.execute("SELECT role FROM users WHERE id = ?", (actor_id,)).fetchone()
        if actor_id
        else None
    )
    payload: dict[str, Any] = {
        "medication_id": medication_id,
        "confirmation_method": confirmation_method,
    }
    if custody is not None:
        payload["supply"] = dict(custody)
    if administration_status == "given" and linked.get("before_pharmacist_check"):
        # Ruling MO-R1 (a): a first stat or urgent dose given before the
        # pharmacist's check, said on the order's own ledger (medorders-11).
        payload["before_pharmacist_check"] = True
        payload["first_dose_rule"] = linked.get("first_dose_rule")
        payload["priority"] = linked.get("priority")
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
