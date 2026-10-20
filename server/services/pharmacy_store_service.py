"""Orb, the pharmacy store's lot-side rules (grn-22 first, then grn-15's ward
returns; grn-10 and grn-12 add theirs here later).

Design: docs/his_design/grn.json, sections D and E. The vendor-return rules
below are reads on the caller's connection, so a writing service can ask them
inside its own immediate transaction. Nothing here writes stock directly: every
movement goes through pharmacy_stock_service (the single writer), including the
ward returns at the end of this file.

WHICH LOTS MAY GO BACK TO A VENDOR (grn-22: "from quarantined or near-expiry
lots"). A lot is returnable when it holds units, sits in a pharmacy store that
holds stock (the dispensing pharmacy, a sub-store, the NDPS vault or returns
quarantine) and is either
  * HELD off the shelf: quarantined, recalled, found not of standard quality,
    or a ward return waiting for inspection (GRN-RS3 says such a return
    leaves by a write-off or a vendor return, never back to the shelf); or
  * available but EXPIRED, or within the near-expiry window
    (ORB_PHARMACY_NEAR_EXPIRY_DAYS, 90), judged on the hospital's date
    (pharmacy_stock_service.site_today).
An available lot with more shelf life than that stays on the shelf, and a lot
with no expiry recorded is not offered (it is neither near expiry nor held).
Written-off and already returned lots hold nothing and are closed.
"""

from __future__ import annotations

import logging as _logging
import uuid as _uuid
from collections.abc import Mapping
from datetime import date
from typing import Any, Literal, overload

import config

from services import pharmacy_stock_service as stock

#: Lot statuses that are off the shelf and may leave by a vendor return.
RETURNABLE_HELD_STATUSES: tuple[str, ...] = ("quarantined", "recalled", "nsq", "pending_inspection")

#: Pharmacy store kinds that hold stock a vendor return may take from. Ward
#: stores and crash carts are filled by transfers and empty by consumption or a
#: ward return, never straight to a vendor.
RETURN_STORE_KINDS: tuple[str, ...] = (
    stock.DISPENSING_STORE_KIND,
    "sub_store",
    stock.NDPS_VAULT_KIND,
    "returns_quarantine",
)

RETURN_WHY_LABELS = {
    "held": "Held off the shelf",
    "expired": "Expired",
    "near_expiry": "Near expiry",
}

REFUSAL_LABELS = {
    "empty": "Holds no units",
    "closed": "Already written off or returned",
    "in_date": "Not near expiry",
    "no_expiry": "No expiry recorded",
    "not_in_store": "Not in a pharmacy store",
}


def near_expiry_days() -> int:
    return int(getattr(config, "PHARMACY_NEAR_EXPIRY_DAYS", 90) or 90)


def days_left(expiry: Any, today: str) -> int | None:
    text = str(expiry or "")[:10]
    if not text:
        return None
    try:
        return (date.fromisoformat(text) - date.fromisoformat(today)).days
    except ValueError:
        return None


def return_eligibility(
    lot: Mapping, *, today: str | None = None, window_days: int | None = None
) -> dict:
    """{returnable, why, why_label, days_left} for one lot row. `why` is
    held, expired or near_expiry when returnable, else the refusal code
    (empty, closed, in_date, no_expiry, not_in_store)."""
    day = today or stock.site_today()
    window = near_expiry_days() if window_days is None else window_days
    left = days_left(lot["expiration_date"], day)
    status = lot["status"]
    if lot["store_id"] is None:
        why = "not_in_store"
    elif status in ("written_off", "returned_to_vendor"):
        why = "closed"
    elif int(lot["quantity"] or 0) <= 0:
        why = "empty"
    elif status in RETURNABLE_HELD_STATUSES:
        why = "held"
    elif status == "available":
        if left is None:
            why = "no_expiry"
        elif left < 0:
            why = "expired"
        elif left <= window:
            why = "near_expiry"
        else:
            why = "in_date"
    else:  # an unknown status is never returned on a guess
        why = "closed"
    returnable = why in RETURN_WHY_LABELS
    return {
        "returnable": returnable,
        "why": why,
        "why_label": RETURN_WHY_LABELS.get(why) or REFUSAL_LABELS.get(why, why),
        "days_left": left,
    }


def returnable_lots(
    conn, store_id: int, *, today: str | None = None, window_days: int | None = None
) -> list[dict]:
    """The lots of one store a vendor return may take from, earliest expiry
    first, each with its drug, vendor and the reason it may go back."""
    day = today or stock.site_today()
    out: list[dict] = []
    for row in conn.execute(
        "SELECT l.*, m.name AS drug_name, m.strength, m.form, m.ndps_category, "
        "m.dispense_unit, v.code AS vendor_code, v.name AS vendor_name "
        "FROM helix_inventory_lots l "
        "LEFT JOIN helix_medication_master m ON m.id = l.medication_id "
        "LEFT JOIN vendors v ON v.id = l.vendor_id "
        "WHERE l.store_id = ? AND COALESCE(l.quantity, 0) > 0 "
        "ORDER BY date(l.expiration_date) IS NULL, date(l.expiration_date), l.rowid",
        (store_id,),
    ).fetchall():
        lot = dict(row)
        verdict = return_eligibility(lot, today=day, window_days=window_days)
        if not verdict["returnable"]:
            continue
        out.append(
            {
                "lot_id": lot["id"],
                "drug_id": lot["medication_id"],
                "drug_name": lot["drug_name"] or lot["medication_id"],
                "strength": lot["strength"],
                "form": lot["form"],
                "dispense_unit": lot["dispense_unit"],
                "ndps": lot["ndps_category"] in stock.NDPS_CONTROLLED,
                "batch_no": lot["lot_number"],
                "expiry_date": str(lot["expiration_date"])[:10] if lot["expiration_date"] else None,
                "status": lot["status"],
                "quantity_units": int(lot["quantity"] or 0),
                "unit_cost_paise": lot["unit_cost_paise"],
                "vendor_id": lot["vendor_id"],
                "vendor_code": lot["vendor_code"],
                "vendor_name": lot["vendor_name"],
                "why": verdict["why"],
                "why_label": verdict["why_label"],
                "days_left": verdict["days_left"],
            }
        )
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Ward returns of dispensed medicines (grn-15, W9-D)
# ═════════════════════════════════════════════════════════════════════════════
#
# A medicine dispensed for one patient comes back unused: the course was
# stopped, the patient went home, the dose was changed. Two lanes record it:
#
#   pharmacy  a pharmacy_store.write holder at the counter, hospital-wide (the
#             pharmacist has no department, F109);
#   ward      a ward_return.record holder (the nurse, ruling GRN-R8 (a)) from
#             the Deliveries tab, inside her own department only. Another
#             ward's order is a 404 that writes an authorization
#             patient_scope_denied row first, the same answer as an order that
#             does not exist.
#
# WHAT A RETURN IS ON THE BOOKS. A return names one DELIVERED Helix order and
# the batch its dispense movement took (the ORIGINAL lot). It can never exceed
# the units that order's dispense took from that batch minus the units already
# returned against it. The units land in a returns_quarantine store as a lot of
# the same batch and expiry with status 'pending_inspection', by an inward
# 'ward_return' movement, so they are never dispensable: the status is not
# 'available' and the store is not the dispensing shelf
# (pharmacy_stock_service.shelf_lot_sql). The return row, its movement and its
# clinical_access audit row commit together or not at all.
#
#   pharmacy_ward_returns.lot_id    the ORIGINAL lot (the batch dispensed)
#   pharmacy_ward_returns.store_id  the returns quarantine store
#   the quarantine lot              the 'ward_return' movement whose reference
#                                   is pharmacy_ward_return:<id>
#
# INSPECTION (pharmacy_store.write). Two outcomes:
#   write off  a 'write_off' movement takes the units out of the quarantine lot;
#              the lot itself stays 'pending_inspection' (at zero), because a
#              closed lot of the same batch in the same store would refuse the
#              next return of that batch (one lot per batch per store). The
#              person who took the return in never writes it off (403
#              same_person, RETURN_WRITE_OFF_NEEDS_SECOND_PERSON): grn.json
#              section F's separation rule, the write-off approver differs
#              from the requester. A strict default listed for Samuel. A
#              return the nurse took in on the ward is any pharmacist's to
#              write off;
#   restock    ruling GRN-RS3, held for Samuel. Built to option (b) (only a
#              sealed, in-date, non cold-chain, non-NDPS return, after a
#              pharmacist other than the one who took it in has inspected it,
#              back into the ORIGINAL lot, which must still be available on the
#              dispensing shelf) and switched OFF behind
#              pharmacy_stock_service.RETURN_RESTOCK_ALLOWED, so the strictest
#              option, (c) never, is what every site gets until he rules. The
#              units move by a paired transfer_out (quarantine lot) and
#              transfer_in (original lot).
#
# CONTROLLED DRUGS (narcotic or psychotropic). Recording a return, and writing
# one off, need pharmacy_store.ndps and a second pharmacy_store.ndps holder
# witnessing with their signing PIN, whether the NDPS register is on or off
# (the transfers' NDPS_WITNESS_ALWAYS rule). A nurse therefore never records a
# controlled return from the ward: it comes back to the counter. With the
# register on, both movements are entered in it (ward_return, write_off).
#
# CREDIT. credit_candidate() is what grn-16 (pharmacy charges at batch MRP)
# reads to credit the patient: units times the batch MRP per unit, or None
# where the batch has no MRP or pack size on file. Nothing here bills.


_logger = _logging.getLogger("orb.pharmacy_store")

WARD_RETURN_REFERENCE = "pharmacy_ward_return"
WARD_RETURN_NUMBER_KIND = "pharmacy_ward_return"
WARD_RETURN_NUMBER_SERIES = "PHRET"
WARD_RETURN_STORE_KIND = "returns_quarantine"
WARD_RETURN_STATUSES: tuple[str, ...] = ("pending_inspection", "restocked", "written_off")
WARD_RETURN_STATUS_LABELS = {
    "pending_inspection": "Waiting for inspection",
    "restocked": "Back on the shelf",
    "written_off": "Written off",
}
INSPECTION_DECISIONS: tuple[str, ...] = ("write_off", "restock")
#: Strict default for Samuel to confirm (grn.json section F, grn-10): the
#: person who took a return in cannot also write it off, so units never leave
#: the books on one person's word. False lets the receiver write it off alone.
RETURN_WRITE_OFF_NEEDS_SECOND_PERSON = True
_WRITE_OFF_SAME_PERSON = (
    "The person who took this return in cannot also write it off. "
    "Ask another pharmacist to inspect it. Nothing was changed."
)
LANE_PHARMACY = "pharmacy"
LANE_WARD = "ward"
#: Only an order the ward has signed for can come back from the ward.
RETURNABLE_ORDER_STATUS = "delivered"
#: How far back the returnable-orders list looks (the list only; a return of
#: an older delivery is still recorded if someone names the order).
RETURNABLE_WINDOW_DAYS = 30
MAX_TEXT = 1000
MAX_LIST = 200

#: Refusal code -> HTTP status, for routes/pharmacy_store/returns.py.
WARD_RETURN_STATUS_CODES = {
    "not_found": 404,
    "ndps_permission_needed": 403,
    "same_person": 403,
    "witness_same_person": 403,
    "witness_not_eligible": 403,
    "witness_no_pin": 403,
    "witness_pin_wrong": 403,
    "witness_locked": 429,
    "order_not_delivered": 409,
    "nothing_dispensed": 409,
    "over_return": 409,
    "no_returns_store": 409,
    "already_inspected": 409,
    "idempotency_conflict": 409,
    "lot_status_conflict": 409,
    "units_not_in_quarantine": 409,
    "restock_not_allowed": 409,
    "return_changed": 409,
    "insufficient_stock": 409,
}


class WardReturnError(ValueError):
    """A ward return the request must correct, or one the rules refuse.
    `code` maps to HTTP through WARD_RETURN_STATUS_CODES (anything else is
    422); `detail` carries what a screen needs beyond the sentence, never
    patient data."""

    code = "ward_return_error"

    def __init__(self, message: str, *, code: str | None = None, detail: dict | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail or {}


# ── Small helpers ─────────────────────────────────────────────────────────────


def _actor_id(actor: Mapping) -> str:
    uid = actor.get("id") if actor else None
    if not uid:
        raise WardReturnError("the request has no signed-in person behind it", code="no_actor")
    return str(uid)


def _holds(actor: Mapping, permission: str) -> bool:
    from permissions import has_permission

    return has_permission(str((actor or {}).get("role") or ""), permission)


def ward_return_lane(actor: Mapping) -> str:
    """'pharmacy' for a pharmacy_store.write holder, else 'ward'."""
    from permissions import PHARMACY_STORE_WRITE

    return LANE_PHARMACY if _holds(actor, PHARMACY_STORE_WRITE) else LANE_WARD


def _scope(actor: Mapping) -> tuple[str | None, Any]:
    """(department, facilities) for the caller, read BEFORE any transaction
    opens (the facility scope may read on a cache miss)."""
    from permissions import department_scope, facility_scope

    return department_scope(dict(actor or {})), facility_scope(dict(actor or {}))


@overload
def _text(value: Any, name: str, *, required: Literal[True]) -> str: ...


@overload
def _text(value: Any, name: str, *, required: Literal[False] = False) -> str | None: ...


@overload
def _text(value: Any, name: str, *, required: bool = False) -> str | None: ...


def _text(value: Any, name: str, *, required: bool = False) -> str | None:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = " ".join(value.split())
    else:
        raise WardReturnError(f"{name} must be text", code="bad_value")
    if not text:
        if required:
            raise WardReturnError(f"give {name}", code="missing_field")
        return None
    if len(text) > MAX_TEXT:
        raise WardReturnError(f"{name} is longer than {MAX_TEXT} characters", code="bad_value")
    return text


def _units(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WardReturnError(
            "give the number of units coming back as a whole number", code="bad_quantity"
        )
    if value < 1:
        raise WardReturnError("a return brings back at least one unit", code="bad_quantity")
    return value


def _seal(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    raise WardReturnError(
        "say whether the seal is intact (yes or no), or leave it unanswered", code="bad_value"
    )


def _units_words(units: int) -> str:
    return f"{units} unit" if units == 1 else f"{units} units"


def _audit(
    conn,
    action: str,
    *,
    actor: Mapping,
    summary: str,
    return_id: Any,
    patient_id: str | None,
    metadata: dict,
    audit_ctx: Mapping | None,
    category: str = "clinical_access",
    outcome: str = "success",
) -> None:
    from services.audit_service import log_audit_event

    ctx = dict(audit_ctx or {})
    log_audit_event(
        correlation_id=str(_uuid.uuid4()),
        event_category=category,
        action_type=action,
        outcome=outcome,
        db=conn,
        summary_message=summary,
        actor_user_id=(actor or {}).get("id"),
        actor_role=(actor or {}).get("role"),
        target_type=WARD_RETURN_REFERENCE,
        target_id=str(return_id) if return_id is not None else None,
        patient_id=patient_id,
        request_path=ctx.get("request_path"),
        http_method=ctx.get("http_method"),
        ip_address=ctx.get("ip_address"),
        metadata=metadata,
    )


def _scope_denied(
    actor: Mapping, *, patient_id: str | None, helix_order_id: str, audit_ctx: Mapping | None
) -> None:
    """Another ward's order: recorded (fail-soft, as require_patient_access
    records it), then answered exactly like an order that does not exist."""
    from services.audit_service import log_audit_event

    ctx = dict(audit_ctx or {})
    try:
        log_audit_event(
            correlation_id=str(_uuid.uuid4()),
            event_category="authorization",
            action_type="patient_scope_denied",
            outcome="denied",
            summary_message="Ward return refused: the order is outside the caller's department",
            actor_user_id=(actor or {}).get("id"),
            actor_role=(actor or {}).get("role"),
            target_type="helix_order",
            target_id=str(helix_order_id),
            patient_id=patient_id,
            request_path=ctx.get("request_path"),
            http_method=ctx.get("http_method"),
            ip_address=ctx.get("ip_address"),
            metadata={
                "department": (actor or {}).get("department"),
                "lane": "pharmacy_ward_return",
            },
        )
    except Exception:  # noqa: BLE001  (an authorization row is fail-soft; the 404 stands)
        pass


def _order_not_found(helix_order_id: str) -> WardReturnError:
    return WardReturnError(
        "That order is not on file for your ward. Check the order and try again.",
        code="not_found",
        detail={"helix_order_id": str(helix_order_id)},
    )


def _patient_department(conn, patient_id: str) -> str | None:
    from permissions import PATIENT_DEPARTMENT_SQL

    row = conn.execute(
        f"SELECT {PATIENT_DEPARTMENT_SQL} AS dept FROM patients p WHERE p.id = ?", (patient_id,)
    ).fetchone()
    return row["dept"] if row is not None else None


def _dispensed_lots(conn, helix_order_id: str) -> list[dict]:
    """Every batch this order's dispense movements took, with the units each
    took, the drug actually dispensed (the substitute when one was set) and
    the batch's details. One lot per order today (a dispense is never split),
    but the reading allows more."""
    rows = conn.execute(
        "SELECT s.lot_id, s.medication_id, -SUM(s.quantity_change) AS units "
        "FROM helix_stock_movements s WHERE s.order_id = ? AND s.movement_type = 'dispense' "
        "GROUP BY s.lot_id, s.medication_id ORDER BY MIN(s.seq), s.lot_id",
        (helix_order_id,),
    ).fetchall()
    out = []
    for r in rows:
        lot = conn.execute(
            "SELECT l.*, m.name AS drug_name, m.strength, m.form, m.ndps_category, "
            "m.storage_condition, m.dispense_unit FROM helix_inventory_lots l "
            "LEFT JOIN helix_medication_master m ON m.id = l.medication_id WHERE l.id = ?",
            (r["lot_id"],),
        ).fetchone()
        if lot is None:
            continue
        out.append(
            {
                "lot": dict(lot),
                "medication_id": r["medication_id"],
                "dispensed_units": int(r["units"] or 0),
            }
        )
    return out


def _returned_units(conn, helix_order_id: str, lot_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity_units), 0) FROM pharmacy_ward_returns "
        "WHERE helix_order_id = ? AND lot_id = ?",
        (helix_order_id, lot_id),
    ).fetchone()
    return int(row[0] or 0)


def _returns_stores(conn) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, code, name FROM stores WHERE pharmacy_kind = ? AND is_active = 1 "
            "ORDER BY name, id",
            (WARD_RETURN_STORE_KIND,),
        ).fetchall()
    ]


def _pick_store(conn, store_code: str | None) -> dict:
    stores = _returns_stores(conn)
    if store_code:
        for s in stores:
            if s["code"] == store_code:
                return s
        raise WardReturnError(
            "that is not an active returns quarantine store",
            code="store_not_found",
            detail={"store_code": store_code},
        )
    if not stores:
        raise WardReturnError(
            "There is no returns quarantine store on this site, so a return has nowhere to "
            "wait for inspection. The pharmacy sets one up in Masters, Stores.",
            code="no_returns_store",
        )
    if len(stores) > 1:
        raise WardReturnError(
            "choose which returns quarantine store this goes to",
            code="store_required",
            detail={"stores": [{"code": s["code"], "name": s["name"]} for s in stores]},
        )
    return stores[0]


def _is_controlled(lot: Mapping) -> bool:
    return lot.get("ndps_category") in stock.NDPS_CONTROLLED


def _quarantine_lot_id(conn, return_id: int) -> str | None:
    row = conn.execute(
        "SELECT lot_id FROM helix_stock_movements WHERE reference_type = ? AND reference_id = ? "
        "AND movement_type = 'ward_return' ORDER BY seq LIMIT 1",
        (WARD_RETURN_REFERENCE, str(return_id)),
    ).fetchone()
    return row["lot_id"] if row is not None else None


# ── What a return is worth to the patient (grn-16 reads this) ────────────────


def credit_candidate(row: Mapping, lot: Mapping | None) -> dict:
    """The patient's credit for a return at the batch MRP: units times the MRP
    per unit, half up to the paisa. amount_paise is None where the batch has no
    MRP or pack size recorded (billing then decides; nothing is guessed)."""
    units = int(row.get("quantity_units") or 0)
    mrp = lot.get("mrp_pack_paise") if lot else None
    per_pack = lot.get("units_per_pack") if lot else None
    amount = None
    unit_mrp = None
    if isinstance(mrp, int) and isinstance(per_pack, int) and per_pack > 0 and mrp >= 0:
        amount = (2 * mrp * units + per_pack) // (2 * per_pack)
        unit_mrp = (2 * mrp + per_pack) // (2 * per_pack)
    return {
        "units": units,
        "unit_mrp_paise": unit_mrp,
        "amount_paise": amount,
        "basis": "batch_mrp" if amount is not None else "not_recorded",
        "helix_order_id": row.get("helix_order_id"),
        "patient_id": row.get("patient_id"),
    }


# ── The restock rule (GRN-RS3 (b), switched off) ─────────────────────────────

#: A batch's status in the words a restock refusal uses.
_LOT_WORDS = {
    "quarantined": "quarantined",
    "recalled": "recalled",
    "nsq": "marked not of standard quality",
    "pending_inspection": "waiting for inspection",
    "written_off": "written off",
    "returned_to_vendor": "returned to the vendor",
}


def restock_check(conn, row: Mapping, *, today: str | None = None) -> dict:
    """Whether this return could go back on the shelf, and why not, in words.
    {allowed, code, reasons}. The seal and who inspects are checked at the
    inspection itself (the inspector says whether the seal is intact)."""
    day = today or stock.site_today()
    reasons: list[str] = []
    code = None
    if not stock.RETURN_RESTOCK_ALLOWED:
        return {
            "allowed": False,
            "code": "restock_not_allowed",
            "reasons": [
                "This site does not put returned medicines back on the shelf. "
                "Write it off, or send it back to the vendor."
            ],
        }
    lot = conn.execute(
        "SELECT l.*, m.ndps_category, m.storage_condition FROM helix_inventory_lots l "
        "LEFT JOIN helix_medication_master m ON m.id = l.medication_id WHERE l.id = ?",
        (row.get("lot_id"),),
    ).fetchone()
    if lot is None:
        return {
            "allowed": False,
            "code": "restock_refused",
            "reasons": ["The batch it was dispensed from is no longer on file."],
        }
    lot = dict(lot)
    if _is_controlled(lot):
        reasons.append("A narcotic or psychotropic medicine is never put back on the shelf.")
    if lot.get("storage_condition") in stock.COLD_CHAIN_STORAGE:
        reasons.append(
            "A refrigerated or frozen medicine is never put back on the shelf once "
            "it has left the pharmacy."
        )
    expiry = str(lot.get("expiration_date") or "")[:10]
    if not expiry or expiry < day:
        reasons.append("The batch has expired or has no expiry recorded.")
    if lot.get("status") != "available":
        words = _LOT_WORDS.get((lot.get("status") or ""), "not available")
        reasons.append(f"The batch it came from is {words}.")
    else:
        on_shelf = conn.execute(
            f"SELECT 1 FROM helix_inventory_lots l WHERE l.id = ? AND {stock.shelf_lot_sql('l')}",
            (lot["id"],),
        ).fetchone()
        if on_shelf is None or lot.get("store_id") is None:
            reasons.append("The batch it came from is no longer on the dispensing shelf.")
    if reasons:
        code = "restock_refused"
    return {"allowed": not reasons, "code": code, "reasons": reasons}


# ── Reads ─────────────────────────────────────────────────────────────────────

_RETURN_SELECT = (
    "SELECT r.*, p.name AS patient_name, p.mrn AS patient_mrn, p.bed AS patient_bed, "
    "p.ward AS patient_ward, m.name AS drug_name, m.strength, m.form, m.ndps_category, "
    "m.storage_condition, m.dispense_unit, l.lot_number AS batch_no, "
    "l.expiration_date AS expiry_date, l.status AS original_lot_status, "
    "l.mrp_pack_paise, l.units_per_pack, s.code AS store_code, s.name AS store_name, "
    "ur.name AS received_by_name, ub.name AS returned_by_name, ui.name AS inspected_by_name "
    "FROM pharmacy_ward_returns r "
    "LEFT JOIN patients p ON p.id = r.patient_id "
    "LEFT JOIN helix_medication_master m ON m.id = r.helix_medication_id "
    "LEFT JOIN helix_inventory_lots l ON l.id = r.lot_id "
    "LEFT JOIN stores s ON s.id = r.store_id "
    "LEFT JOIN users ur ON ur.id = r.received_by "
    "LEFT JOIN users ub ON ub.id = r.returned_by "
    "LEFT JOIN users ui ON ui.id = r.inspected_by "
)


def _utc(value: Any) -> Any:
    if not value:
        return value
    from services.clinical_time import to_utc_iso

    try:
        return to_utc_iso(value)
    except (TypeError, ValueError):
        return value


def _decorate(conn, row: Mapping, *, today: str | None = None) -> dict:
    out = dict(row)
    rid = out["id"]
    q_lot = _quarantine_lot_id(conn, rid)
    q_units = None
    if q_lot:
        found = conn.execute(
            "SELECT quantity FROM helix_inventory_lots WHERE id = ?", (q_lot,)
        ).fetchone()
        q_units = int(found["quantity"] or 0) if found is not None else None
    lot_view = {
        "mrp_pack_paise": out.pop("mrp_pack_paise", None),
        "units_per_pack": out.pop("units_per_pack", None),
    }
    out["status_label"] = WARD_RETURN_STATUS_LABELS.get(
        (out.get("status") or ""), out.get("status")
    )
    out["seal_intact"] = None if out.get("seal_intact") is None else bool(out["seal_intact"])
    out["controlled"] = out.get("ndps_category") in stock.NDPS_CONTROLLED
    out["cold_chain"] = out.get("storage_condition") in stock.COLD_CHAIN_STORAGE
    out["original_lot_id"] = out.get("lot_id")
    out["quarantine_lot_id"] = q_lot
    out["quarantine_units"] = q_units
    out["expiry_date"] = str(out["expiry_date"])[:10] if out.get("expiry_date") else None
    for col in ("occurred_at", "created_at", "inspected_at"):
        out[col] = _utc(out.get(col))
    out["credit"] = credit_candidate(out, lot_view)
    if out.get("status") == "pending_inspection":
        out["restock"] = restock_check(conn, out, today=today)
    else:
        out["restock"] = None
    return out


def _load_return(conn, return_id: int) -> dict | None:
    row = conn.execute(_RETURN_SELECT + "WHERE r.id = ?", (return_id,)).fetchone()
    return dict(row) if row is not None else None


def _visible(conn, row: Mapping, dept: str | None, facilities: Any) -> bool:
    from permissions import patient_in_scope

    return patient_in_scope(conn, row["patient_id"], dept, facilities=facilities)


def list_ward_returns(*, actor: Mapping, status: str | None = None, limit: int = 100) -> list[dict]:
    """Returns the caller may see, newest first: a ward account its own
    department's patients, the pharmacy desk every ward. `status` is one of
    WARD_RETURN_STATUSES or None for all."""
    from database import db_session
    from permissions import scope_patient_ids_sql

    if status is not None and status not in WARD_RETURN_STATUSES:
        raise WardReturnError("unknown return status", code="bad_value")
    limit = max(1, min(int(limit), MAX_LIST))
    dept, facilities = _scope(actor)
    scope_sql, scope_params = scope_patient_ids_sql(dept, facilities=facilities)
    sql = _RETURN_SELECT + f"WHERE r.patient_id IN ({scope_sql})"
    params: list[Any] = list(scope_params)
    if status:
        sql += " AND r.status = ?"
        params.append(status)
    sql += " ORDER BY r.id DESC LIMIT ?"
    params.append(limit)
    today = stock.site_today()
    with db_session() as conn:
        return [
            _decorate(conn, dict(r), today=today)
            for r in conn.execute(sql, tuple(params)).fetchall()
        ]


def get_ward_return(return_id: int, *, actor: Mapping, audit_ctx: Mapping | None = None) -> dict:
    """One return the caller may see. Another ward's return is a 404 that
    writes an authorization patient_scope_denied row first, the same answer
    as a return that does not exist."""
    from database import db_session

    dept, facilities = _scope(actor)
    denied_patient = None
    with db_session() as conn:
        row = _load_return(conn, return_id)
        if row is not None and _visible(conn, row, dept, facilities):
            return _decorate(conn, row)
        if row is not None:
            denied_patient = row["patient_id"]
    if denied_patient is not None:
        assert row is not None
        _scope_denied(
            actor,
            patient_id=denied_patient,
            helix_order_id=str(row["helix_order_id"]),
            audit_ctx=audit_ctx,
        )
    raise WardReturnError("no such return", code="not_found")


def returnable_orders(
    *, actor: Mapping, helix_order_id: str | None = None, limit: int = 100
) -> list[dict]:
    """Delivered orders in the caller's scope that still hold units a return
    could name, newest delivery first, within RETURNABLE_WINDOW_DAYS (or the
    one order named). Each lists its dispensed batches with what can still
    come back."""
    from database import db_session
    from permissions import scope_patient_ids_sql

    limit = max(1, min(int(limit), MAX_LIST))
    dept, facilities = _scope(actor)
    scope_sql, scope_params = scope_patient_ids_sql(dept, facilities=facilities)
    sql = (
        "SELECT o.id, o.patient_id, o.dosage, o.route, o.frequency, o.received_by, "
        "o.received_at, o.created_at, o.med_order_id, p.name AS patient_name, p.bed, p.ward, "
        "p.mrn, u.name AS received_by_name FROM helix_patient_med_orders o "
        "JOIN patients p ON p.id = o.patient_id "
        "LEFT JOIN users u ON u.id = o.received_by "
        f"WHERE o.status = ? AND o.patient_id IN ({scope_sql}) "
        "AND EXISTS (SELECT 1 FROM helix_stock_movements s WHERE s.order_id = o.id "
        "AND s.movement_type = 'dispense') "
    )
    params: list[Any] = [RETURNABLE_ORDER_STATUS, *scope_params]
    if helix_order_id:
        sql += "AND o.id = ? "
        params.append(helix_order_id)
    else:
        # julianday reads both spellings the column holds (CURRENT_TIMESTAMP's
        # 'YYYY-MM-DD HH:MM:SS' and ISO with an offset).
        sql += "AND julianday(COALESCE(o.received_at, o.created_at)) >= julianday('now', ?) "
        params.append(f"-{RETURNABLE_WINDOW_DAYS} days")
    sql += "ORDER BY COALESCE(o.received_at, o.created_at) DESC, o.id LIMIT ?"
    params.append(limit)
    out = []
    with db_session() as conn:
        for r in conn.execute(sql, tuple(params)).fetchall():
            batches = []
            for d in _dispensed_lots(conn, r["id"]):
                lot = d["lot"]
                returned = _returned_units(conn, r["id"], lot["id"])
                left = max(0, d["dispensed_units"] - returned)
                batches.append(
                    {
                        "lot_id": lot["id"],
                        "drug_id": d["medication_id"],
                        "drug_name": lot.get("drug_name") or d["medication_id"],
                        "strength": lot.get("strength"),
                        "form": lot.get("form"),
                        "dispense_unit": lot.get("dispense_unit"),
                        "batch_no": lot.get("lot_number"),
                        "expiry_date": str(lot["expiration_date"])[:10]
                        if lot.get("expiration_date")
                        else None,
                        "controlled": _is_controlled(lot),
                        "dispensed_units": d["dispensed_units"],
                        "returned_units": returned,
                        "returnable_units": left,
                    }
                )
            if not any(b["returnable_units"] > 0 for b in batches):
                continue
            item = dict(r)
            item["received_at"] = _utc(item.get("received_at"))
            item["created_at"] = _utc(item.get("created_at"))
            item["batches"] = batches
            out.append(item)
    return out


def options(*, actor: Mapping) -> dict:
    """What the return screens need: the returns quarantine stores, whether
    restock is on for this site, whether the person who took a return in is
    barred from writing it off (RETURN_WRITE_OFF_NEEDS_SECOND_PERSON), whether
    this caller may take a controlled return, and who may witness one. No
    patient data."""
    from database import db_session
    from permissions import PHARMACY_STORE_NDPS

    from services import ndps_service

    me = (actor or {}).get("id")
    with db_session() as conn:
        stores = [{"code": s["code"], "name": s["name"]} for s in _returns_stores(conn)]
        may_ndps = _holds(actor, PHARMACY_STORE_NDPS)
        witnesses = (
            ndps_service.witnesses(conn, exclude=str(me) if me else None) if may_ndps else []
        )
    return {
        "stores": stores,
        "restock_allowed": bool(stock.RETURN_RESTOCK_ALLOWED),
        "write_off_needs_second_person": bool(RETURN_WRITE_OFF_NEEDS_SECOND_PERSON),
        "lane": ward_return_lane(actor),
        "may_record_controlled": may_ndps,
        "witnesses": witnesses,
        "decisions": list(INSPECTION_DECISIONS),
        "status_labels": WARD_RETURN_STATUS_LABELS,
        "number_series": WARD_RETURN_NUMBER_SERIES,
    }


# ── Writes ────────────────────────────────────────────────────────────────────


def _witness(
    actor: Mapping,
    witness_id: Any,
    witness_pin: Any,
    *,
    purpose: str,
    target_id: Any,
    audit_ctx: Mapping | None,
) -> str:
    from services import ndps_service

    try:
        return ndps_service.verify_witness(
            actor=actor,
            witness_id=witness_id,
            witness_pin=witness_pin,
            purpose=purpose,
            target_type="helix_order",
            target_id=target_id,
            audit_ctx=audit_ctx,
        )
    except ndps_service.NdpsError as exc:
        raise WardReturnError(str(exc), code=exc.code) from exc


class _ScopeMiss(Exception):
    """Raised inside a write transaction for an order outside the caller's
    scope, so the authorization row is written AFTER the rollback (a row
    written on the transaction's connection would roll back with it, and one
    written on another connection would wait on this one's write lock)."""

    def __init__(self, patient_id: str | None):
        super().__init__("out of scope")
        self.patient_id = patient_id


def _peek_order_lot(
    helix_order_id: str, lot_id: str | None, dept: str | None, facilities: Any
) -> tuple[str, str | None, dict | None]:
    """Before the transaction: (verdict, patient id, batch). The verdict is
    'missing', 'denied' (outside the caller's scope: nothing about the order is
    said) or 'ok'; the batch is the one this return will name when it is
    unambiguous, so a controlled drug's witness can be confirmed before the
    write lock is taken. Everything is re-read and re-decided inside the
    transaction."""
    from database import db_session
    from permissions import patient_in_scope

    with db_session() as conn:
        order = conn.execute(
            "SELECT patient_id FROM helix_patient_med_orders WHERE id = ?", (helix_order_id,)
        ).fetchone()
        if order is None:
            return "missing", None, None
        if not patient_in_scope(conn, order["patient_id"], dept, facilities=facilities):
            return "denied", order["patient_id"], None
        lots = _dispensed_lots(conn, helix_order_id)
    if lot_id:
        lots = [d for d in lots if d["lot"]["id"] == lot_id]
    return "ok", order["patient_id"], (lots[0]["lot"] if len(lots) == 1 else None)


def record_ward_return(
    helix_order_id: Any,
    *,
    actor: Mapping,
    quantity_units: Any,
    seal_intact: Any = None,
    lot_id: Any = None,
    store_code: Any = None,
    note: Any = None,
    idempotency_key: Any = None,
    witness_id: Any = None,
    witness_pin: Any = None,
    audit_ctx: Mapping | None = None,
) -> dict:
    """Take back units of a delivered order into returns quarantine.
    Returns {ward_return, replayed}."""
    from database import db_session
    from permissions import PHARMACY_STORE_NDPS

    from services import procurement_service as procurement

    me = _actor_id(actor)
    order_id = _text(helix_order_id, "the order", required=True)
    units = _units(quantity_units)
    seal = _seal(seal_intact)
    batch = _text(lot_id, "the batch")
    store_wanted = _text(store_code, "the store")
    remark = _text(note, "a note")
    key = _text(idempotency_key, "the request key")
    lane = ward_return_lane(actor)
    dept, facilities = _scope(actor)

    verdict, peek_patient, peek = _peek_order_lot(order_id, batch, dept, facilities)
    if verdict == "denied":
        _scope_denied(actor, patient_id=peek_patient, helix_order_id=order_id, audit_ctx=audit_ctx)
    if verdict != "ok":
        raise _order_not_found(order_id)
    witness = None
    if peek is not None and _is_controlled(peek):
        if not _holds(actor, PHARMACY_STORE_NDPS):
            raise WardReturnError(
                "A narcotic or psychotropic medicine is taken back at the pharmacy counter, "
                "with a second pharmacist witnessing. Bring it to the pharmacy.",
                code="ndps_permission_needed",
            )
        witness = _witness(
            actor,
            witness_id,
            witness_pin,
            purpose="Taking back this return",
            target_id=order_id,
            audit_ctx=audit_ctx,
        )

    try:
        result, rid, replayed = _record_in_txn(
            actor=actor,
            me=me,
            order_id=order_id,
            units=units,
            seal=seal,
            batch=batch,
            store_wanted=store_wanted,
            remark=remark,
            key=key,
            lane=lane,
            dept=dept,
            facilities=facilities,
            witness=witness,
            audit_ctx=audit_ctx,
        )
    except _ScopeMiss as miss:
        _scope_denied(
            actor, patient_id=miss.patient_id, helix_order_id=order_id, audit_ctx=audit_ctx
        )
        raise _order_not_found(order_id) from None
    if replayed:
        return {"ward_return": result, "replayed": True}
    if procurement._stamp_document_number(WARD_RETURN_NUMBER_KIND, rid) is None:
        _logger.error("ward return %s was taken in but not numbered; stamp it again", rid)
    else:
        with db_session() as conn:
            found = conn.execute(
                "SELECT return_number FROM pharmacy_ward_returns WHERE id = ?", (rid,)
            ).fetchone()
            result["return_number"] = found["return_number"] if found is not None else None
    return {"ward_return": result, "replayed": False}


def _record_in_txn(
    *,
    actor: Mapping,
    me: str,
    order_id: str,
    units: int,
    seal: int | None,
    batch: str | None,
    store_wanted: str | None,
    remark: str | None,
    key: str | None,
    lane: str,
    dept: str | None,
    facilities: Any,
    witness: str | None,
    audit_ctx: Mapping | None,
) -> tuple[dict, int, bool]:
    """The return's one immediate transaction: (decorated return, id, replayed)."""
    from database import db_session
    from permissions import patient_in_scope

    from services import ndps_service

    with db_session(immediate=True) as conn:
        if key:
            prior = conn.execute(
                "SELECT * FROM pharmacy_ward_returns WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if prior is not None:
                if (
                    prior["helix_order_id"],
                    int(prior["quantity_units"]),
                    prior["received_by"],
                ) != (order_id, units, me) or (batch and prior["lot_id"] != batch):
                    raise WardReturnError(
                        "this request key was already used for a different "
                        "return. Nothing was changed.",
                        code="idempotency_conflict",
                    )
                row = _load_return(conn, prior["id"])
                assert row is not None
                return _decorate(conn, row), int(prior["id"]), True

        order = conn.execute(
            "SELECT id, patient_id, status FROM helix_patient_med_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            raise _order_not_found(order_id)
        if not patient_in_scope(conn, order["patient_id"], dept, facilities=facilities):
            raise _ScopeMiss(order["patient_id"])
        if order["status"] != RETURNABLE_ORDER_STATUS:
            raise WardReturnError(
                "Only a medicine the ward has signed for can be returned. This order has not "
                "been received on the ward.",
                code="order_not_delivered",
                detail={"order_status": order["status"]},
            )
        dispensed = _dispensed_lots(conn, order_id)
        if not dispensed:
            raise WardReturnError(
                "No dispense of this order is on the stock ledger, so there "
                "is nothing to take back against it.",
                code="nothing_dispensed",
            )
        if batch:
            dispensed = [d for d in dispensed if d["lot"]["id"] == batch]
            if not dispensed:
                raise WardReturnError(
                    "that batch was not dispensed for this order", code="lot_not_dispensed"
                )
        if len(dispensed) > 1:
            raise WardReturnError(
                "this order was dispensed from more than one batch: choose "
                "the batch that is coming back",
                code="lot_required",
                detail={"lots": [d["lot"]["id"] for d in dispensed]},
            )
        source = dispensed[0]
        lot = source["lot"]
        controlled = _is_controlled(lot)
        if controlled and witness is None:
            # Classified controlled between the peek and the write lock.
            raise WardReturnError(
                "this medicine is now classified narcotic or psychotropic, "
                "so a witness is needed. Nothing was changed.",
                code="witness_required",
            )
        already = _returned_units(conn, order_id, lot["id"])
        left = source["dispensed_units"] - already
        if units > left:
            raise WardReturnError(
                f"This order took {_units_words(source['dispensed_units'])} of batch "
                f"{lot.get('lot_number') or lot['id']} and {_units_words(already)} "
                f"{'has' if already == 1 else 'have'} already come back, so at most "
                f"{_units_words(max(0, left))} can be returned. Nothing was changed.",
                code="over_return",
                detail={
                    "dispensed_units": source["dispensed_units"],
                    "returned_units": already,
                    "returnable_units": max(0, left),
                },
            )
        store = _pick_store(conn, store_wanted)
        patient_id = order["patient_id"]
        department = _patient_department(conn, patient_id)
        from services.clinical_time import utc_iso_now

        occurred = utc_iso_now()
        rid = conn.execute(
            "INSERT INTO pharmacy_ward_returns (patient_id, helix_order_id, helix_medication_id, "
            "lot_id, store_id, department, quantity_units, seal_intact, status, returned_by, "
            "received_by, occurred_at, note, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_inspection', ?, ?, ?, ?, ?)",
            (
                patient_id,
                order_id,
                lot["medication_id"],
                lot["id"],
                store["id"],
                department,
                units,
                seal,
                me if lane == LANE_WARD else None,
                me,
                occurred,
                remark,
                key,
            ),
        ).lastrowid
        try:
            with ndps_service.entry_context(
                witness_id=witness,
                counterparty_type="patient",
                counterparty_ref=str(patient_id),
                document_ref=f"{WARD_RETURN_REFERENCE}:{rid}",
            ):
                opened = stock.open_or_merge_lot(
                    conn,
                    medication_id=lot["medication_id"],
                    lot_number=lot["lot_number"],
                    expiration_date=lot["expiration_date"],
                    units=units,
                    actor_id=me,
                    store_id=store["id"],
                    status="pending_inspection",
                    movement_type="ward_return",
                    units_per_pack=lot.get("units_per_pack"),
                    mrp_pack_paise=lot.get("mrp_pack_paise"),
                    unit_cost_paise=lot.get("unit_cost_paise"),
                    vendor_id=lot.get("vendor_id"),
                    reference_type=WARD_RETURN_REFERENCE,
                    reference_id=str(rid),
                    idempotency_key=f"{WARD_RETURN_REFERENCE}:{rid}:in",
                    remarks=f"Returned from the ward against order {order_id}",
                )
        except stock.StockError as exc:
            if exc.code == "lot_status_conflict":
                raise WardReturnError(
                    f"Batch {lot.get('lot_number')} of this medicine is already closed in "
                    f"{store['name']} (written off or sent back to the vendor), so it cannot "
                    "hold this return. Take it into another returns quarantine store.",
                    code="lot_status_conflict",
                ) from exc
            raise WardReturnError(str(exc), code=exc.code) from exc
        movement = opened["movement"] or {}
        _audit(
            conn,
            "pharmacy_ward_return_recorded",
            actor=actor,
            return_id=rid,
            patient_id=patient_id,
            audit_ctx=audit_ctx,
            summary=(
                f"Ward return {rid} taken in: {_units_words(units)} of "
                f"{lot.get('drug_name') or lot['medication_id']} against order "
                f"{order_id}"
            ),
            metadata={
                "ward_return_id": str(rid),
                "helix_order_id": str(order_id),
                "original_lot_id": lot["id"],
                "quarantine_lot_id": (opened["lot"] or {}).get("id"),
                "movement_id": movement.get("id"),
                "medication_id": lot["medication_id"],
                "quantity_units": units,
                "seal_intact": seal,
                "lane": lane,
                "store_id": str(store["id"]),
                "controlled": controlled,
                "witness_id": witness,
                "department": department,
            },
        )
        row = _load_return(conn, rid)
        assert row is not None
        return _decorate(conn, row), int(rid), False


def inspect_ward_return(
    return_id: Any,
    *,
    actor: Mapping,
    decision: Any,
    reason: Any,
    seal_intact: Any = None,
    witness_id: Any = None,
    witness_pin: Any = None,
    audit_ctx: Mapping | None = None,
) -> dict:
    """The pharmacist's decision on a return waiting for inspection: write it
    off, or (only while RETURN_RESTOCK_ALLOWED is on, ruling GRN-RS3) put it
    back into the batch it was dispensed from. Either decision is refused to
    the person who took the return in (same_person). Returns {ward_return}."""
    from database import db_session
    from permissions import PHARMACY_STORE_NDPS

    from services import ndps_service

    me = _actor_id(actor)
    if decision not in INSPECTION_DECISIONS:
        raise WardReturnError("choose write off or restock", code="bad_value")
    why = _text(reason, "the reason for this decision", required=True)
    seal = _seal(seal_intact)
    try:
        rid = int(return_id)
    except (TypeError, ValueError):
        raise WardReturnError("no such return", code="not_found") from None
    dept, facilities = _scope(actor)

    with db_session() as conn:
        peek = _load_return(conn, rid)
        if peek is not None and not _visible(conn, peek, dept, facilities):
            peek = None
    if peek is None:
        raise WardReturnError("no such return", code="not_found")
    controlled = peek.get("ndps_category") in stock.NDPS_CONTROLLED
    witness = None
    if decision == "restock" and not stock.RETURN_RESTOCK_ALLOWED:
        raise WardReturnError(
            "This site does not put returned medicines back on the shelf. "
            "Write it off, or send it back to the vendor. Nothing was changed.",
            code="restock_not_allowed",
        )
    # Refused before a witness is asked for their PIN; checked again under the
    # write lock below, which is the check that holds.
    if (
        decision == "write_off"
        and RETURN_WRITE_OFF_NEEDS_SECOND_PERSON
        and peek.get("status") == "pending_inspection"
        and str(peek.get("received_by")) == me
    ):
        raise WardReturnError(_WRITE_OFF_SAME_PERSON, code="same_person")
    if controlled and decision == "write_off":
        if not _holds(actor, PHARMACY_STORE_NDPS):
            raise WardReturnError(
                "writing off a narcotic or psychotropic return needs the NDPS permission",
                code="ndps_permission_needed",
            )
        witness = _witness(
            actor,
            witness_id,
            witness_pin,
            purpose="Writing off this return",
            target_id=peek["helix_order_id"],
            audit_ctx=audit_ctx,
        )

    today = stock.site_today()
    with db_session(immediate=True) as conn:
        row = _load_return(conn, rid)
        if row is None or not _visible(conn, row, dept, facilities):
            raise WardReturnError("no such return", code="not_found")
        if row["status"] != "pending_inspection":
            raise WardReturnError(
                f"This return was already inspected: "
                f"{WARD_RETURN_STATUS_LABELS.get(row['status'], row['status']).lower()}.",
                code="already_inspected",
                detail={"status": row["status"]},
            )
        units = int(row["quantity_units"])
        q_lot = _quarantine_lot_id(conn, rid)
        held = (
            conn.execute(
                "SELECT quantity FROM helix_inventory_lots WHERE id = ?", (q_lot,)
            ).fetchone()
            if q_lot
            else None
        )
        if held is None or int(held["quantity"] or 0) < units:
            raise WardReturnError(
                "The units of this return are no longer in returns quarantine (for example, "
                "they went back to the vendor). Check the returns store. Nothing was changed.",
                code="units_not_in_quarantine",
            )
        movements: list[dict] = []
        if decision == "write_off":
            if RETURN_WRITE_OFF_NEEDS_SECOND_PERSON and str(row["received_by"]) == me:
                raise WardReturnError(_WRITE_OFF_SAME_PERSON, code="same_person")
            try:
                with ndps_service.entry_context(
                    witness_id=witness,
                    entry_type="write_off",
                    document_ref=f"{WARD_RETURN_REFERENCE}:{rid}",
                ):
                    movements.append(
                        stock.post_movement(
                            conn,
                            lot_id=q_lot or "",
                            movement_type="write_off",
                            quantity_change=-units,
                            actor_id=me,
                            reference_type=WARD_RETURN_REFERENCE,
                            reference_id=str(rid),
                            idempotency_key=f"{WARD_RETURN_REFERENCE}:{rid}:write_off",
                            remarks=f"Ward return written off: {why}",
                        )
                    )
            except stock.StockError as exc:
                raise WardReturnError(str(exc), code=exc.code) from exc
            new_status = "written_off"
        else:
            if str(row["received_by"]) == me:
                raise WardReturnError(
                    "The person who took this return in cannot also put it back on the shelf. "
                    "Ask another pharmacist to inspect it.",
                    code="same_person",
                )
            if seal != 1:
                raise WardReturnError(
                    "A return goes back on the shelf only with its seal "
                    "intact. Confirm the seal, or write it off.",
                    code="restock_refused",
                )
            check = restock_check(conn, row, today=today)
            if not check["allowed"]:
                raise WardReturnError(
                    " ".join(check["reasons"]), code=check["code"] or "restock_refused"
                )
            try:
                movements.append(
                    stock.post_movement(
                        conn,
                        lot_id=q_lot or "",
                        movement_type="transfer_out",
                        quantity_change=-units,
                        actor_id=me,
                        reference_type=WARD_RETURN_REFERENCE,
                        reference_id=str(rid),
                        idempotency_key=f"{WARD_RETURN_REFERENCE}:{rid}:restock:out",
                        remarks="Ward return inspected and put back on the shelf",
                    )
                )
                movements.append(
                    stock.post_movement(
                        conn,
                        lot_id=row["lot_id"],
                        movement_type="transfer_in",
                        quantity_change=units,
                        actor_id=me,
                        reference_type=WARD_RETURN_REFERENCE,
                        reference_id=str(rid),
                        idempotency_key=f"{WARD_RETURN_REFERENCE}:{rid}:restock:in",
                        remarks="Ward return inspected and put back on the shelf",
                    )
                )
            except stock.StockError as exc:
                raise WardReturnError(str(exc), code=exc.code) from exc
            new_status = "restocked"
        cur = conn.execute(
            "UPDATE pharmacy_ward_returns SET status = ?, inspected_by = ?, "
            "inspected_at = CURRENT_TIMESTAMP, inspection_note = ?, "
            "seal_intact = COALESCE(?, seal_intact) "
            "WHERE id = ? AND status = 'pending_inspection'",
            (new_status, me, why, seal, rid),
        )
        if cur.rowcount != 1:
            raise WardReturnError(
                "this return changed while you were looking at it. Nothing was changed.",
                code="return_changed",
            )
        _audit(
            conn,
            "pharmacy_ward_return_inspected",
            actor=actor,
            return_id=rid,
            patient_id=row["patient_id"],
            audit_ctx=audit_ctx,
            summary=(
                f"Ward return {rid} inspected: {WARD_RETURN_STATUS_LABELS[new_status].lower()}"
            ),
            metadata={
                "ward_return_id": str(rid),
                "decision": decision,
                "status": new_status,
                "quantity_units": units,
                "helix_order_id": str(row["helix_order_id"]),
                "original_lot_id": row["lot_id"],
                "quarantine_lot_id": q_lot,
                "movement_ids": [m.get("id") for m in movements],
                "seal_intact": seal,
                "controlled": controlled,
                "witness_id": witness,
                "received_by": str(row["received_by"]),
                "restock_allowed": bool(stock.RETURN_RESTOCK_ALLOWED),
            },
        )
        returned = _load_return(conn, rid)
        assert returned is not None
        return {"ward_return": _decorate(conn, returned, today=today)}
