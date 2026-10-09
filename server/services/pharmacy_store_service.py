"""Orb, the pharmacy store's lot-side rules (grn-22 first; grn-10, grn-12 and
grn-15 add theirs here later).

Design: docs/his_design/grn.json, sections D and E. Reads only, on the
caller's connection, so a writing service can ask them inside its own
immediate transaction. Nothing here writes stock: every movement goes through
pharmacy_stock_service (the single writer).

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

from datetime import date
from typing import Any, Mapping, Optional

import config
from services import pharmacy_stock_service as stock

#: Lot statuses that are off the shelf and may leave by a vendor return.
RETURNABLE_HELD_STATUSES: tuple[str, ...] = (
    "quarantined", "recalled", "nsq", "pending_inspection")

#: Pharmacy store kinds that hold stock a vendor return may take from. Ward
#: stores and crash carts are filled by transfers and empty by consumption or a
#: ward return, never straight to a vendor.
RETURN_STORE_KINDS: tuple[str, ...] = (
    stock.DISPENSING_STORE_KIND, "sub_store", stock.NDPS_VAULT_KIND, "returns_quarantine")

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


def days_left(expiry: Any, today: str) -> Optional[int]:
    text = str(expiry or "")[:10]
    if not text:
        return None
    try:
        return (date.fromisoformat(text) - date.fromisoformat(today)).days
    except ValueError:
        return None


def return_eligibility(lot: Mapping, *, today: Optional[str] = None,
                       window_days: Optional[int] = None) -> dict:
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
    return {"returnable": returnable, "why": why,
            "why_label": RETURN_WHY_LABELS.get(why) or REFUSAL_LABELS.get(why, why),
            "days_left": left}


def returnable_lots(conn, store_id: int, *, today: Optional[str] = None,
                    window_days: Optional[int] = None) -> list[dict]:
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
            (store_id,)).fetchall():
        lot = dict(row)
        verdict = return_eligibility(lot, today=day, window_days=window_days)
        if not verdict["returnable"]:
            continue
        out.append({
            "lot_id": lot["id"], "drug_id": lot["medication_id"],
            "drug_name": lot["drug_name"] or lot["medication_id"],
            "strength": lot["strength"], "form": lot["form"],
            "dispense_unit": lot["dispense_unit"],
            "ndps": lot["ndps_category"] in stock.NDPS_CONTROLLED,
            "batch_no": lot["lot_number"],
            "expiry_date": str(lot["expiration_date"])[:10] if lot["expiration_date"] else None,
            "status": lot["status"], "quantity_units": int(lot["quantity"] or 0),
            "unit_cost_paise": lot["unit_cost_paise"],
            "vendor_id": lot["vendor_id"], "vendor_code": lot["vendor_code"],
            "vendor_name": lot["vendor_name"],
            "why": verdict["why"], "why_label": verdict["why_label"],
            "days_left": verdict["days_left"],
        })
    return out
