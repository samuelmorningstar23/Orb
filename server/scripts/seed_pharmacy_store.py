"""Orb DEV SEED for the pharmacy store module (grn-18, W8-F).

WHAT IT WRITES, and only on a development box with the pharmacy store switched
on (ORB_DEPLOYMENT_PROFILE=dev and ORB_PHARMACY_STORE on):
  * the dispensing pharmacy, which adopts the lots seed_helix just opened (a
    reseed keeps the store, a master, and adopts the fresh lots into it);
  * one licensed vendor;
  * the drug-master entries a purchase needs, for the two drugs it buys, and
    only where the pharmacist has not entered them (schedule, NDPS class, pack
    size, GST): an entry already on file is never overwritten;
  * one purchase order, approved, issued and received on one posted goods
    receipt, one of whose batches is short-dated, so the near-expiry list has
    a real example;
  * with ward stock also switched on (ORB_PHARMACY_WARD_STOCK, off until
    ruling GRN-RS5 is confirmed): one ward stock store for the seeded nurse's
    department and its stock list (par levels). No stock is sent to it.
It seeds NO narcotic or psychotropic stock (GRN-R14).

WHERE IT RUNS. scripts/reset_db_and_seed.reset_and_seed calls it right after
seed_helix (which deletes every lot and opens the seed lots) and before
seed_pharmacy_queue (whose dispenses then come off the adopted shelf). With the
module off it is not called and prints nothing, so a reseed without the module
writes exactly what it wrote before.

HOW. Every row goes through the pharmacy services (pharmacy_master_service,
pharmacy_purchasing_service, pharmacy_stock_service), so the seed obeys every
rule a pharmacist meets: separation of duties (the pharmacist raises and
receives, the administrator approves, issues and accepts the short expiry),
the drug licence, the expiry typed twice, and the audit row each write makes.
Seeded masters carry the SEED- prefix and the "Sample data" marker that
scripts/seed_modules.py uses, and the purchase order says its rates and GST
are illustrative.

IDEMPOTENT. A second run adds no second vendor, store or order. A reseed wipes
the order and receipt (reset_db_and_seed.EXTRA_TABLES_TO_CLEAR) and the lots
(seed_helix), so it writes them again against the fresh lots.

Usage (from server/, on a development box only):
    ORB_DEPLOYMENT_PROFILE=dev ORB_PHARMACY_STORE=true .venv/bin/python scripts/seed_pharmacy_store.py
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db_session  # noqa: E402
# One marker and one code prefix for every seeded module row, so a person can
# find all seed data with one search.
from scripts.seed_modules import CODE_PREFIX, SEED_MARKER  # noqa: E402

#: Who the seeded writes are made by: the seeded pharmacist raises and
#: receives, the seeded administrator approves and issues (two people, as the
#: services require).
PHARMACIST_ID = "PHA001"
ADMIN_ID = "ADM001"
NURSE_ID = "NUR001"

DISPENSING_CODE = f"{CODE_PREFIX}PHDISP"
VENDOR_CODE = f"{CODE_PREFIX}PHVEN1"
WARD_STORE_CODE = f"{CODE_PREFIX}PHWARD1"

#: The two drugs the seeded order buys (both in scripts/seed_helix.DRUGS), with
#: the master entries a purchase needs. Schedule H for both; no NDPS class.
#: GST is illustrative (5 per cent) and set only where the master holds none.
SEEDED_DRUGS: dict[str, dict] = {
    "MED-PANTO": {"classification": {"drug_schedule": "H", "ndps_category": "none"},
                  "fields": {"pack_size_units": 10, "gst_rate_bp": 500,
                             "purchase_uom": "strip", "dispense_unit": "tablet"}},
    "MED-ATENOLOL": {"classification": {"drug_schedule": "H", "ndps_category": "none"},
                     "fields": {"pack_size_units": 14, "gst_rate_bp": 500,
                                "purchase_uom": "strip", "dispense_unit": "tablet"}},
}

#: The order: (drug, packs, rate per pack in paise, MRP per pack in paise,
#: days to expiry, batch). The second batch expires inside the near-expiry
#: window and under the minimum shelf life, so posting it needs the approve
#: tier and a reason (GRN-R16), which the administrator gives.
ORDER_LINES: tuple[tuple[str, int, int, int, int, str], ...] = (
    ("MED-PANTO", 10, 4500, 7200, 540, "SEEDPAN01"),
    ("MED-ATENOLOL", 5, 2800, 4200, 60, "SEEDATN01"),
)

#: Par levels for the ward stock store (only with ward stock switched on).
WARD_PAR_UNITS: dict[str, int] = {"MED-PANTO": 20, "MED-ATENOLOL": 14}


def _log(verbose: bool, message: str) -> None:
    if verbose:
        print(message)


def should_seed() -> bool:
    """A development box with the pharmacy store switched on. Read at call
    time, never bound at import."""
    import config

    return (getattr(config, "DEPLOYMENT_PROFILE", "prod") == "dev"
            and config.module_state("pharmacy-store") != "off")


def _actor(user_id: str) -> dict | None:
    with db_session() as db:
        row = db.execute("SELECT id, role FROM users WHERE id = ?", (user_id,)).fetchone()
    return {"id": row["id"], "role": row["role"]} if row else None


def _today() -> date:
    from services import pharmacy_stock_service as stock

    return date.fromisoformat(stock.site_today())


# ═════════════════════════════════════════════════════════════════════════════
#  The dispensing pharmacy
# ═════════════════════════════════════════════════════════════════════════════

def _dispensing_store(admin: dict, verbose: bool) -> dict:
    """The active dispensing store, adopting every legacy lot into it. Made
    once (masters.create_store adopts in the same step); on a reseed the store
    is kept and the fresh seed lots are adopted here, with the same audit row
    the masters write."""
    from services import audit_service
    from services import pharmacy_master_service as masters
    from services import pharmacy_stock_service as stock

    with db_session() as db:
        row = db.execute("SELECT * FROM stores WHERE pharmacy_kind = 'dispensing' "
                         "AND is_active = 1").fetchone()
    if row is None:
        made = masters.create_store(code=DISPENSING_CODE, name="Main pharmacy (seed)",
                                    pharmacy_kind="dispensing", actor=admin)
        adopted = made["adoption"] or {}
        _log(verbose, f"  + dispensing pharmacy {made['store']['code']}, "
                      f"{len(adopted.get('adopted', []))} lots adopted")
        return made["store"]
    with db_session(immediate=True) as db:
        adoption = stock.adopt_legacy_lots(db, int(row["id"]), actor_id=admin["id"])
        if adoption["adopted"] or adoption["merged"]:
            audit_service.log_audit_event(
                correlation_id=str(uuid.uuid4()), event_category="security",
                action_type="pharmacy_legacy_lots_adopted", outcome="success", db=db,
                summary_message=(f"Dispensing store adopted {len(adoption['adopted'])} legacy "
                                 f"lots and merged {len(adoption['merged'])} duplicates"),
                actor_user_id=admin["id"], actor_role=admin["role"],
                target_type="pharmacy_store", target_id=str(row["id"]),
                metadata={"adopted": len(adoption["adopted"]),
                          "merged": adoption["merged"][:50],
                          "conflicts": len(adoption["conflicts"]), "source": "seed"})
    _log(verbose, f"  ~ dispensing pharmacy {row['code']} kept, "
                  f"{len(adoption['adopted'])} lots adopted")
    return dict(row)


# ═════════════════════════════════════════════════════════════════════════════
#  Drug master entries and the vendor
# ═════════════════════════════════════════════════════════════════════════════

def _drug_entries(pharmacist: dict, verbose: bool) -> list[str]:
    """Fill only what the master does not hold; never overwrite an entry."""
    from services import pharmacy_master_service as masters

    ready: list[str] = []
    for drug_id, wanted in SEEDED_DRUGS.items():
        drug = masters.get_drug(drug_id)
        if drug is None:
            _log(verbose, f"  x {drug_id} is not in the drug master; not ordered")
            continue
        classify = {k: v for k, v in wanted["classification"].items() if drug.get(k) is None}
        if classify:
            masters.classify_drug(drug_id, classify, actor=pharmacist,
                                  reason=f"{SEED_MARKER}: classified for the seeded order")
        fields = {k: v for k, v in wanted["fields"].items() if drug.get(k) is None}
        if fields:
            masters.update_drug_fields(drug_id, fields, actor=pharmacist)
        ready.append(drug_id)
    return ready


def _vendor(pharmacist: dict, today: date, verbose: bool) -> None:
    from services import pharmacy_master_service as masters
    from services import pharmacy_purchasing_service as purchasing

    valid_to = (today + timedelta(days=730)).isoformat()
    with db_session() as db:
        row = db.execute("SELECT * FROM vendors WHERE code = ?", (VENDOR_CODE,)).fetchone()
    if row is None:
        masters.create_vendor({
            "code": VENDOR_CODE, "name": "Sample medicines distributor (seed)",
            "vendor_type": "distributor", "state_code": "29",
            "payment_terms_days": 30, "contact_name": "Order desk (seed)",
            "contact_phone": "080-4000-0004", "notes": SEED_MARKER,
            "drug_licence_numbers": "SAMPLE-20B-0001, SAMPLE-21B-0001",
            "drug_licence_valid_to": valid_to,
        }, actor=pharmacist)
        _log(verbose, f"  + vendor {VENDOR_CODE} with a drug licence")
        return
    if purchasing.vendor_licence(row, today=today.isoformat())["status"] in ("none", "expired"):
        masters.set_drug_licence(VENDOR_CODE, actor=pharmacist,
                                 drug_licence_numbers="SAMPLE-20B-0001, SAMPLE-21B-0001",
                                 drug_licence_valid_to=valid_to)
        _log(verbose, f"  ~ vendor {VENDOR_CODE} licence renewed for the sample")
    else:
        _log(verbose, f"  ~ vendor {VENDOR_CODE} kept")


# ═════════════════════════════════════════════════════════════════════════════
#  One order, received on one posted goods receipt
# ═════════════════════════════════════════════════════════════════════════════

def _seeded_order_exists() -> int | None:
    with db_session() as db:
        row = db.execute("SELECT id FROM purchase_orders WHERE domain = 'pharmacy' "
                         "AND remarks LIKE ? ORDER BY id LIMIT 1",
                         (f"{SEED_MARKER}%",)).fetchone()
    return int(row["id"]) if row else None


def _order_and_receipt(pharmacist: dict, admin: dict, store: dict, drugs: list[str],
                       today: date, verbose: bool) -> dict:
    from services import pharmacy_purchasing_service as purchasing

    existing = _seeded_order_exists()
    if existing is not None:
        _log(verbose, f"  ~ pharmacy purchase order #{existing} exists")
        return {"purchase_order_id": existing, "goods_receipt_id": None}
    lines = [line for line in ORDER_LINES if line[0] in drugs]
    if not lines:
        return {"purchase_order_id": None, "goods_receipt_id": None}
    po = purchasing.create_purchase_order(
        vendor_code=VENDOR_CODE, store_code=store["code"],
        lines=[{"helix_medication_id": drug, "packs": packs, "unit_rate_paise": rate}
               for drug, packs, rate, _mrp, _days, _batch in lines],
        actor=pharmacist,
        remarks=f"{SEED_MARKER}: illustrative rates and GST, not a negotiated price",
    )["purchase_order"]
    po_id = int(po["id"])
    purchasing.submit_purchase_order(po_id, actor=pharmacist)
    purchasing.approve_purchase_order(po_id, actor=admin)
    purchasing.issue_purchase_order(po_id, actor=admin)
    by_drug = {str(line["helix_medication_id"]): line for line in
               (purchasing.get_purchase_order(po_id) or {}).get("lines", [])}
    receipt_lines = []
    for drug, packs, _rate, mrp, days, batch in lines:
        expiry = (today + timedelta(days=days)).isoformat()
        receipt_lines.append({"po_line_no": by_drug[drug]["line_no"], "packs": packs,
                              "batch_no": batch, "expiry_date": expiry,
                              "expiry_date_confirm": expiry, "mrp_pack_paise": mrp})
    grn = purchasing.create_goods_receipt(
        purchase_order_id=po_id, lines=receipt_lines, actor=pharmacist,
        vendor_invoice_number="SEED/PH/INV/0001",
        vendor_invoice_date=(today - timedelta(days=1)).isoformat(),
        remarks=f"{SEED_MARKER}: one batch is short-dated on purpose",
        idempotency_key=f"seed-pharmacy-grn-{po_id}",
    )["goods_receipt"]
    grn_id = int(grn["id"])
    short = any(days < 90 for *_x, days, _b in lines)
    purchasing.post_goods_receipt(
        grn_id, actor=admin if short else pharmacist, may_write=not short, may_approve=short,
        short_expiry_reason=(f"{SEED_MARKER}: short-dated batch accepted so the near-expiry "
                             "list has an example") if short else None)
    _log(verbose, f"  + pharmacy purchase order #{po_id} issued and received on goods "
                  f"receipt #{grn_id}")
    return {"purchase_order_id": po_id, "goods_receipt_id": grn_id}


# ═════════════════════════════════════════════════════════════════════════════
#  Ward stock (only with ORB_PHARMACY_WARD_STOCK on)
# ═════════════════════════════════════════════════════════════════════════════

def _ward_store(admin: dict, pharmacist: dict, drugs: list[str], verbose: bool) -> dict | None:
    import config
    from services import pharmacy_master_service as masters

    if not config.feature_enabled("pharmacy_ward_stock"):
        return None
    with db_session() as db:
        nurse = db.execute("SELECT department FROM users WHERE id = ?", (NURSE_ID,)).fetchone()
        row = db.execute("SELECT * FROM stores WHERE code = ?", (WARD_STORE_CODE,)).fetchone()
    department = (nurse["department"] if nurse else None) or None
    if row is None:
        if not department:
            _log(verbose, "  x no seeded nurse department; no ward stock store")
            return None
        row = masters.create_store(code=WARD_STORE_CODE, name=f"{department} ward stock (seed)",
                                   pharmacy_kind="ward_imprest", department=department,
                                   actor=admin)["store"]
        _log(verbose, f"  + ward stock store {WARD_STORE_CODE} for {department}")
    for drug in drugs:
        par = WARD_PAR_UNITS.get(drug)
        if par:
            masters.upsert_par_level(store_id=int(row["id"]), helix_medication_id=drug,
                                     par_units=par, actor=pharmacist)
    return dict(row)


# ═════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════════════════════════════

def seed_pharmacy_store(*, verbose: bool = True) -> dict:
    """Seed the pharmacy store. Returns what it did; {'skipped': why} when it
    must not run (not a dev box, or the module is off). Idempotent."""
    if not should_seed():
        return {"skipped": "not a development box with the pharmacy store switched on"}
    pharmacist = _actor(PHARMACIST_ID)
    admin = _actor(ADMIN_ID)
    if pharmacist is None or admin is None:
        _log(verbose, "  x the seeded pharmacist and administrator are missing; nothing seeded")
        return {"skipped": "seeded pharmacist or administrator missing"}
    today = _today()
    store = _dispensing_store(admin, verbose)
    drugs = _drug_entries(pharmacist, verbose)
    _vendor(pharmacist, today, verbose)
    order = _order_and_receipt(pharmacist, admin, store, drugs, today, verbose)
    ward = _ward_store(admin, pharmacist, drugs, verbose)
    return {"dispensing_store": store["code"], "drugs": drugs, **order,
            "ward_store": ward["code"] if ward else None}


if __name__ == "__main__":
    print(seed_pharmacy_store())
