"""Orb, pharmacy purchase orders (grn-7).

Design: docs/his_design/grn.json, sections D, F and G, increment grn-7. Reached
only through routes/pharmacy_store/purchasing.py, behind ORB_PHARMACY_STORE
(default off). It needs nothing from ORB_PROCUREMENT: it calls procurement's
connection-taking cores directly, with domain 'pharmacy', and never a public
procurement wrapper or route.

ONE TRANSACTION PER ACTION. Create, submit, approve, issue and cancel each run
in one db_session(immediate=True): the checks below, the procurement core and
the 'security' audit row (written with db=conn, fail-closed) commit together or
not at all. A failed audit write rolls the order back. The PHPO number is drawn
AFTER the issue commits, exactly as procurement does (allocation takes its own
immediate session, and a gap in a numbered series is worse than an unnumbered
document that can be stamped again). The audit row carries the order's id.

WHAT A PHARMACY ORDER LINE NEEDS (checked here, before the core is called,
again at submit, approve and issue, so a drug retired or a licence that lapsed
in between stops the order):
  * the drug is active, with identity status 'active' (a draft drug waiting
    for its second pharmacist is not orderable, ruling GRN-RS1);
  * it is CLASSIFIED: both its schedule and its NDPS category are recorded.
    Unclassified is never read as 'none' (GRN-R18). Classification never
    gates a dispense of stock already on the shelf; only buying more;
  * its pack size is configured, and the pack size is snapshotted on the
    line, so a later change to the master never rewrites what was ordered;
  * its GST rate is configured (an unset rate is not 0 per cent);
  * a narcotic or psychotropic is ordered into the NDPS vault, and the vault
    takes nothing else (GRN-R14: narcotics are received into the vault; a
    non-controlled drug there could never be dispensed).

WHAT THE VENDOR NEEDS (GRN-R10): a drug licence on file, with its number and a
valid-to date on or after the hospital's date. A licence ending within
ORB_PHARMACY_LICENCE_WARN_DAYS is allowed with a warning on the response. An
order with a narcotic or psychotropic line also needs the vendor's NDPS
licence number on file.

SEPARATION OF DUTIES. The person who approves an order is never the person who
raised it (403 same_person), whatever tiers they hold. Approve and issue need
pharmacy_store.approve. Cancelling a draft or an order awaiting approval needs
the write tier; once approved or issued (a commitment to the vendor) it needs
the approve tier.

NUMBERS come from the pharmacy's own series, PHPO (GRN-R11). Migration 54
seeds it unconfigured; the order carries number_series_configured so a screen
can say the number is a placeholder until the site sets its series up.

GOODS RECEIPTS (grn-8) are the second half of this module: a DRAFT is what
arrived, checked line by line; POST puts it on the shelf; a draft may be
cancelled with a reason. Post is ONE immediate transaction: procurement's
pharmacy post core (receipt posted, order quantities and status derived),
pharmacy_stock_service.receive_grn_lines (lots opened or merged and their
receipt movements, into the receipt's store, status 'available') and the
'security' audit row commit together or not at all. No inventory_items or
stock_movements row is ever written for a drug. The PHGRN number is drawn
after the commit. What a receipt line needs, at draft and again at post:
  * the order is still with the vendor with packs to come (issued or
    partially received), and the line's order line is neither cancelled nor
    closed short. A draft whose order was cancelled or closed, or whose order
    line was closed, after it was made is refused at post (409) and puts
    nothing on the shelf; the draft can then only be cancelled;
  * batch, MRP per pack and expiry are mandatory; the expiry is TYPED TWICE
    and the two must match (ruling GRN-RS6 option (b), held for Samuel);
  * expiry and minimum shelf life are judged on the HOSPITAL's date
    (pharmacy_stock_service.site_today), never the server's UTC date: an
    expired batch is refused outright; one with fewer than
    ORB_PHARMACY_MIN_SHELF_LIFE_DAYS (90) left posts only when a person with
    the approve tier, who did not receive it, accepts it with a reason
    (GRN-R16);
  * a batch already on file with another expiry, or in the store at another
    MRP, is refused;
  * the drug passes the purchase-order checks again (classified, confirmed,
    pack size, NDPS into the vault and nothing else there), and the vendor's
    drug licence is still valid (GRN-R10);
  * nothing is over-received: every receipt line, and every draft posted
    later against the same order line, stays within what is outstanding;
  * free units come in at no cost and are spread into the lot's unit cost,
    which is the line's landed total (after discount, with GST, which a
    hospital cannot claim back) over paid plus free units (GRN-R12, lot cost);
  * a rate per pack above the MRP, and an invoice total that differs from
    the receipt by more than a rupee, are warnings, never silent.

RETURNS TO VENDOR (grn-22) are the third half, at the bottom of this module: a
DRAFT names lots of one store going back to one vendor; POST takes the units
off the shelf by vendor_return movements; the vendor's credit note is then
recorded against it. See the section's own notes.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any, Mapping, Optional, Sequence

from database import db_session
from services import pharmacy_stock_reports as reports
from services import pharmacy_stock_service as stock
from services import procurement_service as procurement
from services.money import QUANTITY_SCALE

logger = logging.getLogger("orb.pharmacy_purchasing")

DOMAIN = procurement.PHARMACY_DOMAIN
NUMBER_SERIES = "PHPO"

#: Pharmacy store kinds a purchase order may deliver to. Returns quarantine
#: holds returns, and ward stores are filled by transfers, never by a vendor.
ORDERABLE_STORE_KINDS: tuple[str, ...] = ("dispensing", "sub_store", "ndps_vault")

#: Statuses a holder of the write tier alone may cancel from: nothing has been
#: promised to the vendor yet.
WRITE_TIER_CANCELLABLE = frozenset({"draft", "pending_approval"})

MAX_LINES = 200
MAX_PACKS = 1_000_000


class PurchasingError(ValueError):
    """A purchasing request the caller must correct, or one the rules refuse.
    `code` maps to HTTP in the route."""

    code = "purchasing_error"

    def __init__(self, message: str, *, code: Optional[str] = None, detail: Optional[dict] = None):
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail or {}


# ═════════════════════════════════════════════════════════════════════════════
#  Audit
# ═════════════════════════════════════════════════════════════════════════════

def _audit(conn, actor: Mapping, action: str, *, summary: str, po_id: int,
           metadata: Optional[dict] = None, audit_ctx: Optional[Mapping] = None) -> None:
    """One 'security' row in the caller's transaction (fail-closed). A
    purchase order is hospital business, not a patient record: no patient id."""
    from services import audit_service

    ctx = audit_ctx or {}
    audit_service.log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type=action,
        outcome="success",
        db=conn,
        summary_message=summary,
        actor_user_id=str(actor.get("id")) if actor.get("id") is not None else None,
        actor_role=actor.get("role"),
        target_type="pharmacy_purchase_order",
        target_id=str(po_id),
        request_path=ctx.get("request_path"),
        http_method=ctx.get("http_method"),
        ip_address=ctx.get("ip_address"),
        metadata=metadata or {},
    )


def _actor_id(actor: Mapping) -> str:
    uid = actor.get("id") if actor else None
    if not uid:
        raise PurchasingError("the request has no signed-in person behind it", code="no_actor")
    return str(uid)


def _procurement_error(exc: procurement.ProcurementError) -> PurchasingError:
    return PurchasingError(str(exc), code=exc.code)


# ═════════════════════════════════════════════════════════════════════════════
#  Drugs, vendors and stores: what may be ordered, from whom, into where
# ═════════════════════════════════════════════════════════════════════════════

def _drug_name(row: Mapping) -> str:
    return row["name"] or row["id"]


def drug_problems(row: Mapping) -> list[dict]:
    """Why this drug cannot be put on a purchase order, as {code, message}
    items; [] when it can."""
    problems: list[dict] = []
    name = _drug_name(row)
    if not row["is_active"]:
        problems.append({"code": "drug_inactive",
                         "message": f"{name} is marked inactive in the drug master"})
    if (row["identity_status"] or "active") != "active":
        problems.append({"code": "drug_not_confirmed",
                         "message": f"{name} is a {row['identity_status']} drug, not yet "
                                    "confirmed for use"})
    if row["drug_schedule"] is None or row["ndps_category"] is None:
        problems.append({"code": "drug_unclassified",
                         "message": f"{name} is not classified. Record its schedule and NDPS "
                                    "class before ordering it."})
    if not row["pack_size_units"]:
        problems.append({"code": "pack_size_not_configured",
                         "message": f"{name} has no pack size. Set how many units come in a "
                                    "pack before ordering it."})
    if row["gst_rate_bp"] is None:
        problems.append({"code": "gst_not_configured",
                         "message": f"{name} has no GST rate. Set it before ordering; an "
                                    "unset rate is not 0 per cent."})
    return problems


def _is_ndps(row: Mapping) -> bool:
    return row["ndps_category"] in stock.NDPS_CONTROLLED


def _find_drug(conn, drug_id: str):
    """The drug with this id. An exact match first; else a case-insensitive
    one, refused when it is ambiguous."""
    text = (drug_id or "").strip()
    if not text:
        return None
    row = conn.execute("SELECT * FROM helix_medication_master WHERE id = ?", (text,)).fetchone()
    if row is not None:
        return row
    rows = conn.execute("SELECT * FROM helix_medication_master WHERE upper(id) = upper(?)",
                        (text,)).fetchall()
    return rows[0] if len(rows) == 1 else None


def vendor_licence(vendor: Mapping, *, today: Optional[str] = None) -> dict:
    """{status, valid_to, numbers, ndps_number, warning} for a vendor's drug
    licence. status is 'none', 'expired', 'expiring' or 'valid'."""
    from services.pharmacy_master_service import licence_status

    numbers = (vendor["drug_licence_numbers"] or "").strip() or None
    valid_to = vendor["drug_licence_valid_to"]
    status = licence_status(valid_to, today=today) if numbers else "none"
    warning = None
    if status == "expiring":
        warning = {"code": "vendor_licence_expiring",
                   "message": f"{vendor['name']}'s drug licence ends on "
                              f"{str(valid_to)[:10]}. Ask for the renewed licence."}
    return {"status": status, "valid_to": str(valid_to)[:10] if valid_to else None,
            "numbers": numbers, "ndps_number": (vendor["ndps_licence_number"] or "").strip() or None,
            "warning": warning}


def _check_vendor(conn, vendor_code: str, *, needs_ndps: bool, today: str):
    """The vendor row, or PurchasingError. Returns (row, warnings)."""
    code = (vendor_code or "").strip().upper()
    vendor = conn.execute("SELECT * FROM vendors WHERE code = ?", (code,)).fetchone() if code \
        else None
    if vendor is None:
        raise PurchasingError(f"no vendor with code {vendor_code!r}", code="vendor_not_found")
    if vendor["is_blacklisted"]:
        raise PurchasingError(f"{vendor['name']} is blacklisted. Nothing was changed.",
                              code="vendor_blacklisted")
    if not vendor["is_active"]:
        raise PurchasingError(f"{vendor['name']} is inactive. Nothing was changed.",
                              code="vendor_inactive")
    licence = vendor_licence(vendor, today=today)
    if licence["status"] == "none":
        raise PurchasingError(
            f"{vendor['name']} has no drug licence on file. Record the licence number and "
            "its valid-to date in Masters before ordering drugs from them.",
            code="vendor_licence_missing")
    if licence["status"] == "expired":
        raise PurchasingError(
            f"{vendor['name']}'s drug licence ended on {licence['valid_to']}. Record the "
            "renewed licence before ordering drugs from them.", code="vendor_licence_expired")
    if needs_ndps and not licence["ndps_number"]:
        raise PurchasingError(
            f"{vendor['name']} has no NDPS licence on file, so narcotic and psychotropic "
            "drugs cannot be ordered from them.", code="vendor_ndps_licence_missing")
    return vendor, ([licence["warning"]] if licence["warning"] else [])


def _check_store(conn, store_code: Optional[str]):
    code = (store_code or "").strip().upper()
    if not code:
        raise PurchasingError("choose the pharmacy store the order is delivered to",
                              code="store_required")
    store = conn.execute("SELECT * FROM stores WHERE code = ? AND pharmacy_kind IS NOT NULL",
                         (code,)).fetchone()
    if store is None:
        raise PurchasingError(f"no pharmacy store {store_code!r}", code="store_not_found")
    if not store["is_active"]:
        raise PurchasingError(f"{store['name']} is closed", code="store_inactive")
    if store["pharmacy_kind"] not in ORDERABLE_STORE_KINDS:
        raise PurchasingError(
            f"{store['name']} does not take deliveries from vendors. Order into the "
            "dispensing store, a sub-store or the NDPS vault.", code="store_not_orderable")
    return store


def _check_drug_for_store(drug: Mapping, store: Mapping) -> None:
    vault = store["pharmacy_kind"] == stock.NDPS_VAULT_KIND
    if _is_ndps(drug) and not vault:
        raise PurchasingError(
            f"{_drug_name(drug)} is a narcotic or psychotropic drug. It is received into the "
            "NDPS vault, so order it into the vault.", code="ndps_needs_vault")
    if vault and not _is_ndps(drug):
        raise PurchasingError(
            f"{_drug_name(drug)} is not a narcotic or psychotropic drug. It could not be "
            "dispensed from the NDPS vault, so order it into another store.",
            code="vault_is_for_ndps")


def _orderable_drug(conn, drug_id: str):
    drug = _find_drug(conn, drug_id)
    if drug is None:
        raise PurchasingError(f"no drug {drug_id!r} in the drug master", code="drug_not_found")
    problems = drug_problems(drug)
    if problems:
        raise PurchasingError(problems[0]["message"], code=problems[0]["code"],
                              detail={"drug_id": drug["id"], "problems": problems})
    return drug


# ═════════════════════════════════════════════════════════════════════════════
#  Lines
# ═════════════════════════════════════════════════════════════════════════════

def _whole(value: Any, name: str, *, minimum: int, maximum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PurchasingError(f"{name} must be a whole number", code="bad_value")
    if value < minimum or (maximum is not None and value > maximum):
        span = f"from {minimum} to {maximum}" if maximum is not None else f"{minimum} or more"
        raise PurchasingError(f"{name} must be {span}", code="bad_value")
    return value


def _clean_lines(raw_lines: Sequence[Any]) -> list[dict]:
    if not isinstance(raw_lines, (list, tuple)) or not raw_lines:
        raise PurchasingError("a purchase order needs at least one line", code="empty_document")
    if len(raw_lines) > MAX_LINES:
        raise PurchasingError(f"a purchase order has at most {MAX_LINES} lines", code="bad_value")
    out: list[dict] = []
    seen: set[str] = set()
    for n, raw in enumerate(raw_lines, start=1):
        if not isinstance(raw, Mapping):
            raise PurchasingError("each line is an object", code="bad_line")
        drug_id = str(raw.get("helix_medication_id") or "").strip()
        if not drug_id:
            raise PurchasingError(f"line {n} names no drug", code="bad_line")
        key = drug_id.upper()
        if key in seen:
            raise PurchasingError(f"{drug_id} is on the order twice; put it on one line",
                                  code="duplicate_line")
        seen.add(key)
        remarks = raw.get("remarks")
        if remarks is not None and not isinstance(remarks, str):
            raise PurchasingError("line remarks must be text", code="bad_value")
        out.append({
            "drug_id": drug_id,
            "packs": _whole(raw.get("packs"), f"line {n} packs", minimum=1, maximum=MAX_PACKS),
            "unit_rate_paise": _whole(raw.get("unit_rate_paise"), f"line {n} rate per pack",
                                      minimum=0),
            "discount_bp": _whole(raw.get("discount_bp", 0) or 0, f"line {n} discount",
                                  minimum=0, maximum=10000),
            "remarks": (remarks or "").strip() or None,
        })
    return out


def _resolved_lines(conn, lines: list[dict], store: Mapping) -> tuple[list[dict], dict, bool]:
    """(core line dicts, {UPPER(id): drug row}, any NDPS line)."""
    drugs: dict[str, Any] = {}
    core_lines: list[dict] = []
    for line in lines:
        drug = _orderable_drug(conn, line["drug_id"])
        _check_drug_for_store(drug, store)
        drugs[str(drug["id"]).upper()] = drug
        core_lines.append({
            "item_code": drug["id"],
            "quantity_milli": line["packs"] * QUANTITY_SCALE,
            "unit_rate_paise": line["unit_rate_paise"],
            "discount_bp": line["discount_bp"],
            "uom": drug["purchase_uom"] or "pack",
            "description": _drug_name(drug),
            "remarks": line["remarks"],
        })
    return core_lines, drugs, any(_is_ndps(d) for d in drugs.values())


def _resolver(drugs: Mapping[str, Any]):
    """The drug catalogue answer procurement's core asks for. It only reads
    the rows this service already checked."""
    def resolve(db, item_code: str):
        drug = drugs.get((item_code or "").upper())
        if drug is None:
            return None
        return {"code": drug["id"], "name": _drug_name(drug), "helix_medication_id": drug["id"],
                "uom": drug["purchase_uom"] or "pack", "hsn_sac": drug["hsn_code"],
                "gst_treatment": "taxable", "gst_rate_bp": drug["gst_rate_bp"],
                "units_per_pack": drug["pack_size_units"]}
    return resolve


# ═════════════════════════════════════════════════════════════════════════════
#  Reading orders
# ═════════════════════════════════════════════════════════════════════════════

def _series_configured(conn) -> bool:
    row = conn.execute("SELECT is_configured FROM document_number_series WHERE series_code = ?",
                       (NUMBER_SERIES,)).fetchone()
    return bool(row and row["is_configured"])


def _user_names(conn, ids: set[str]) -> dict[str, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {r["id"]: r["name"] for r in conn.execute(
        f"SELECT id, name FROM users WHERE id IN ({marks})", tuple(ids)).fetchall()}


def _decorate(conn, doc: dict, *, today: Optional[str] = None) -> dict:
    vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (doc.get("vendor_id"),)).fetchone()
    names = _user_names(conn, {doc.get("created_by"), doc.get("approved_by"),
                               doc.get("cancelled_by")})
    doc["created_by_name"] = names.get(doc.get("created_by"))
    doc["approved_by_name"] = names.get(doc.get("approved_by"))
    doc["cancelled_by_name"] = names.get(doc.get("cancelled_by"))
    doc["vendor_licence"] = vendor_licence(vendor, today=today) if vendor else None
    doc["number_series_configured"] = _series_configured(conn)
    for line in doc.get("lines", []):
        packs = int(line["quantity_milli"] or 0) // QUANTITY_SCALE
        per_pack = line.get("units_per_pack")
        line["quantity_packs"] = packs
        line["quantity_units"] = packs * int(per_pack) if per_pack else None
        line["received_packs"] = int(line.get("received_quantity_milli") or 0) // QUANTITY_SCALE
    return doc


def get_purchase_order(po_id: int) -> Optional[dict]:
    with db_session() as conn:
        doc = procurement._get_purchase_order_core(conn, po_id, domain=DOMAIN)
        return _decorate(conn, doc) if doc else None


def list_purchase_orders(*, status: Optional[str] = None, limit: int = 100) -> list[dict]:
    if status is not None and status not in procurement.PO_TRANSITIONS:
        raise PurchasingError(f"unknown purchase order status {status!r}", code="bad_value")
    return procurement.list_purchase_orders(status=status, limit=max(1, min(int(limit), 500)),
                                            domain=DOMAIN)


def _load(conn, po_id: int, *, moving_to: Optional[str] = None) -> dict:
    """The pharmacy order, else not_found. With `moving_to`, an order whose
    status cannot make that move is refused first (409), before any other
    check, so a cancelled order is never answered with a licence problem."""
    doc = procurement._get_purchase_order_core(conn, po_id, domain=DOMAIN)
    if not doc:
        raise PurchasingError(f"no purchase order {po_id}", code="not_found")
    if moving_to is not None:
        try:
            procurement.assert_transition("purchase_order", doc["status"], moving_to)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
    return doc


def _recheck(conn, doc: dict, *, today: str) -> list[dict]:
    """The order's vendor, store and every line, checked again now. Returns
    the warnings to show."""
    store = conn.execute("SELECT * FROM stores WHERE id = ?", (doc.get("store_id"),)).fetchone()
    if store is None or store["pharmacy_kind"] is None:
        raise PurchasingError("this order has no pharmacy store to deliver to",
                              code="store_required")
    store = _check_store(conn, store["code"])
    any_ndps = False
    for line in doc.get("lines", []):
        if line.get("status") == "cancelled":
            continue
        drug = _orderable_drug(conn, line.get("helix_medication_id") or "")
        _check_drug_for_store(drug, store)
        any_ndps = any_ndps or _is_ndps(drug)
    _vendor, warnings = _check_vendor(conn, doc.get("vendor_code") or "", needs_ndps=any_ndps,
                                      today=today)
    return warnings


# ═════════════════════════════════════════════════════════════════════════════
#  Writes
# ═════════════════════════════════════════════════════════════════════════════

def create_purchase_order(*, vendor_code: str, store_code: str, lines: Sequence[Any],
                          actor: Mapping, expected_date: Optional[str] = None,
                          is_interstate: bool = False, place_of_supply: Optional[str] = None,
                          other_charges_paise: int = 0, terms: Optional[str] = None,
                          remarks: Optional[str] = None, source: str = "manual",
                          audit_ctx: Optional[Mapping] = None) -> dict:
    """Raise a pharmacy purchase order in draft (the write tier). Returns
    {"purchase_order", "warnings"}."""
    actor_id = _actor_id(actor)
    clean = _clean_lines(lines)
    if not isinstance(is_interstate, bool):
        raise PurchasingError("interstate is yes or no", code="bad_value")
    other = _whole(other_charges_paise or 0, "other charges", minimum=0)
    today = stock.site_today()
    with db_session(immediate=True) as conn:
        store = _check_store(conn, store_code)
        core_lines, drugs, any_ndps = _resolved_lines(conn, clean, store)
        _vendor, warnings = _check_vendor(conn, vendor_code, needs_ndps=any_ndps, today=today)
        try:
            po_id = procurement._create_purchase_order_core(
                conn, vendor_code=vendor_code.strip().upper(), lines=core_lines,
                store_code=store["code"], expected_date=expected_date or None,
                place_of_supply=place_of_supply or None, is_interstate=is_interstate,
                other_charges_paise=other, terms=terms or None, remarks=remarks or None,
                created_by=actor_id, today=today, domain=DOMAIN, resolve_line=_resolver(drugs))
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        doc = _load(conn, po_id)
        _audit(conn, actor, "pharmacy_po_created",
               summary=f"Pharmacy purchase order drafted for {doc.get('vendor_name')} "
                       f"({len(doc['lines'])} lines)",
               po_id=po_id,
               metadata={"purchase_order_id": str(po_id), "vendor_code": doc.get("vendor_code"),
                         "store_code": doc.get("store_code"),
                         "total_paise": doc.get("total_paise"),
                         "lines": len(doc["lines"]), "source": source,
                         "drug_ids": [str(line["helix_medication_id"]) for line in doc["lines"]],
                         "warnings": [w["code"] for w in warnings]},
               audit_ctx=audit_ctx)
        doc = _decorate(conn, doc, today=today)
    return {"purchase_order": doc, "warnings": warnings}


def submit_purchase_order(po_id: int, *, actor: Mapping,
                          audit_ctx: Optional[Mapping] = None) -> dict:
    """Send a draft for approval (the write tier)."""
    actor_id = _actor_id(actor)
    today = stock.site_today()
    with db_session(immediate=True) as conn:
        doc = _load(conn, po_id, moving_to="pending_approval")
        warnings = _recheck(conn, doc, today=today)
        try:
            procurement._submit_purchase_order_core(conn, po_id, actor=actor_id, domain=DOMAIN)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        _audit(conn, actor, "pharmacy_po_submitted",
               summary=f"Pharmacy purchase order {po_id} sent for approval", po_id=po_id,
               metadata={"purchase_order_id": str(po_id), "total_paise": doc.get("total_paise")},
               audit_ctx=audit_ctx)
        doc = _decorate(conn, _load(conn, po_id), today=today)
    return {"purchase_order": doc, "warnings": warnings}


def approve_purchase_order(po_id: int, *, actor: Mapping,
                           audit_ctx: Optional[Mapping] = None) -> dict:
    """Approve an order awaiting approval (the approve tier). Never by the
    person who raised it."""
    actor_id = _actor_id(actor)
    today = stock.site_today()
    with db_session(immediate=True) as conn:
        doc = _load(conn, po_id, moving_to="approved")
        if str(doc.get("created_by") or "") == actor_id:
            raise PurchasingError(
                "you raised this order, so someone else must approve it. Nothing was changed.",
                code="same_person")
        warnings = _recheck(conn, doc, today=today)
        try:
            procurement._approve_purchase_order_core(conn, po_id, actor=actor_id, domain=DOMAIN)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        _audit(conn, actor, "pharmacy_po_approved",
               summary=f"Pharmacy purchase order {po_id} approved", po_id=po_id,
               metadata={"purchase_order_id": str(po_id), "total_paise": doc.get("total_paise"),
                         "created_by": str(doc.get("created_by") or "")},
               audit_ctx=audit_ctx)
        doc = _decorate(conn, _load(conn, po_id), today=today)
    return {"purchase_order": doc, "warnings": warnings}


def issue_purchase_order(po_id: int, *, actor: Mapping,
                         audit_ctx: Optional[Mapping] = None) -> dict:
    """Send an approved order to the vendor (the approve tier). The PHPO
    number is drawn after the issue commits."""
    actor_id = _actor_id(actor)
    today = stock.site_today()
    with db_session(immediate=True) as conn:
        doc = _load(conn, po_id, moving_to="issued")
        warnings = _recheck(conn, doc, today=today)
        try:
            procurement._issue_purchase_order_core(conn, po_id, actor=actor_id, domain=DOMAIN)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        _audit(conn, actor, "pharmacy_po_issued",
               summary=f"Pharmacy purchase order {po_id} issued to {doc.get('vendor_name')}",
               po_id=po_id,
               metadata={"purchase_order_id": str(po_id), "total_paise": doc.get("total_paise"),
                         "vendor_code": doc.get("vendor_code")},
               audit_ctx=audit_ctx)
    number = procurement._stamp_document_number("pharmacy_purchase_order", po_id)
    if number is None:
        logger.error("pharmacy purchase order %s was issued but not numbered; stamp it again",
                     po_id)
    with db_session() as conn:
        doc = _decorate(conn, _load(conn, po_id), today=today)
    return {"purchase_order": doc, "warnings": warnings}


def cancel_purchase_order(po_id: int, *, reason: Optional[str], actor: Mapping,
                          may_approve: bool, audit_ctx: Optional[Mapping] = None) -> dict:
    """Cancel an order. A draft or an order awaiting approval needs the write
    tier (or approve); an approved or issued one needs the approve tier,
    because the vendor may already hold it. A reason is always required."""
    actor_id = _actor_id(actor)
    text = (reason or "").strip()
    if not text:
        raise PurchasingError("a cancellation needs a reason", code="missing_field")
    today = stock.site_today()
    with db_session(immediate=True) as conn:
        doc = _load(conn, po_id)
        if doc["status"] not in WRITE_TIER_CANCELLABLE and not may_approve:
            raise PurchasingError(
                "this order has been approved, so cancelling it needs the approver tier. "
                "Nothing was changed.", code="approve_needed")
        try:
            procurement._cancel_purchase_order_core(conn, po_id, reason=text, actor=actor_id,
                                                    domain=DOMAIN)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        _audit(conn, actor, "pharmacy_po_cancelled",
               summary=f"Pharmacy purchase order {doc.get('po_number') or po_id} cancelled",
               po_id=po_id,
               metadata={"purchase_order_id": str(po_id), "from_status": doc["status"],
                         "reason": text},
               audit_ctx=audit_ctx)
        doc = _decorate(conn, _load(conn, po_id), today=today)
    return {"purchase_order": doc, "warnings": []}


# ═════════════════════════════════════════════════════════════════════════════
#  Reorder suggestions to a draft order
# ═════════════════════════════════════════════════════════════════════════════

def _last_rates(conn) -> dict[str, dict]:
    """{drug_id: {rate, vendor_code, order_date}} from the most recent
    pharmacy order line for each drug that was not cancelled."""
    out: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT pl.helix_medication_id AS drug_id, pl.unit_rate_paise, v.code AS vendor_code, "
            "p.order_date FROM purchase_order_lines pl "
            "JOIN purchase_orders p ON p.id = pl.purchase_order_id "
            "LEFT JOIN vendors v ON v.id = p.vendor_id "
            "WHERE p.domain = 'pharmacy' AND p.status <> 'cancelled' "
            "AND pl.helix_medication_id IS NOT NULL "
            "ORDER BY p.order_date DESC, p.id DESC, pl.line_no").fetchall():
        out.setdefault(r["drug_id"], {"last_rate_paise": r["unit_rate_paise"],
                                      "last_vendor_code": r["vendor_code"],
                                      "last_ordered_on": r["order_date"]})
    return out


def reorder_suggestions(*, store_id: Optional[int] = None) -> list[dict]:
    """The drugs that need ordering now, each with whether it can be ordered
    (and why not), and the last rate paid. No rate is ever invented: a drug
    never bought before has none."""
    with db_session() as conn:
        rows = reports.reorder_report(conn, store_id=store_id, needs_only=True)["rows"]
        rates = _last_rates(conn)
        out = []
        for row in rows:
            drug = _find_drug(conn, row["drug_id"])
            problems = drug_problems(drug) if drug is not None else [
                {"code": "drug_not_found", "message": "not in the drug master"}]
            if row["store_id"] is None:
                problems.append({"code": "store_required", "message": row["note"]
                                 or "Set up the store this drug is ordered into"})
            out.append({**row, **rates.get(row["drug_id"], {"last_rate_paise": None,
                                                           "last_vendor_code": None,
                                                           "last_ordered_on": None}),
                        "orderable": not problems, "problems": problems})
    return out


def create_from_suggestions(*, vendor_code: str, store_code: str, items: Sequence[Any],
                            actor: Mapping, is_interstate: bool = False,
                            expected_date: Optional[str] = None, remarks: Optional[str] = None,
                            audit_ctx: Optional[Mapping] = None) -> dict:
    """Turn reorder suggestions into a draft order. Each item names a drug and
    the rate per pack the buyer typed; its quantity is the server's current
    suggestion for that drug in that store unless the item gives `packs`. A
    drug that no longer needs ordering there, with no packs given, is refused."""
    if not isinstance(items, (list, tuple)) or not items:
        raise PurchasingError("choose at least one suggestion", code="empty_document")
    with db_session() as conn:
        store = _check_store(conn, store_code)
        rows = reports.reorder_report(conn, store_id=int(store["id"]), needs_only=True)["rows"]
    suggested = {str(r["drug_id"]).upper(): r for r in rows}
    lines = []
    for n, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise PurchasingError("each suggestion is an object", code="bad_line")
        drug_id = str(item.get("helix_medication_id") or "").strip()
        packs = item.get("packs")
        if packs is None:
            row = suggested.get(drug_id.upper())
            if row is None or not row.get("suggested_packs"):
                raise PurchasingError(
                    f"{drug_id or f'line {n}'} has no reorder suggestion for this store now. "
                    "Enter the number of packs.", code="no_suggestion")
            packs = int(row["suggested_packs"])
        lines.append({"helix_medication_id": drug_id, "packs": packs,
                      "unit_rate_paise": item.get("unit_rate_paise"),
                      "discount_bp": item.get("discount_bp", 0) or 0,
                      "remarks": item.get("remarks")})
    return create_purchase_order(vendor_code=vendor_code, store_code=store_code, lines=lines,
                                 actor=actor, expected_date=expected_date,
                                 is_interstate=is_interstate, remarks=remarks,
                                 source="reorder_suggestions", audit_ctx=audit_ctx)


# ═════════════════════════════════════════════════════════════════════════════
#  What the order form offers
# ═════════════════════════════════════════════════════════════════════════════

def options() -> dict:
    """Vendors with their licence state, the stores an order may go to, and
    every active drug with whether it can be ordered and why not."""
    today = stock.site_today()
    with db_session() as conn:
        vendors = []
        for v in conn.execute("SELECT * FROM vendors WHERE is_active = 1 AND is_blacklisted = 0 "
                              "ORDER BY name LIMIT 1000").fetchall():
            licence = vendor_licence(v, today=today)
            vendors.append({"code": v["code"], "name": v["name"], "state_code": v["state_code"],
                            "licence_status": licence["status"],
                            "drug_licence_valid_to": licence["valid_to"],
                            "has_ndps_licence": bool(licence["ndps_number"]),
                            "orderable": licence["status"] in ("valid", "expiring")})
        stores = [{"code": s["code"], "name": s["name"], "pharmacy_kind": s["pharmacy_kind"],
                   "kind_label": reports.STORE_KIND_LABELS.get(s["pharmacy_kind"],
                                                               s["pharmacy_kind"])}
                  for s in conn.execute(
                      "SELECT code, name, pharmacy_kind FROM stores WHERE is_active = 1 AND "
                      f"pharmacy_kind IN ({','.join('?' for _ in ORDERABLE_STORE_KINDS)}) "
                      "ORDER BY name", ORDERABLE_STORE_KINDS).fetchall()]
        drugs = []
        for d in conn.execute(
                "SELECT * FROM helix_medication_master WHERE is_active = 1 ORDER BY name "
                "LIMIT 2000").fetchall():
            problems = drug_problems(d)
            drugs.append({"id": d["id"], "name": d["name"], "generic_name": d["generic_name"],
                          "form": d["form"], "strength": d["strength"],
                          "pack_size_units": d["pack_size_units"],
                          "purchase_uom": d["purchase_uom"], "gst_rate_bp": d["gst_rate_bp"],
                          "ndps": _is_ndps(d), "orderable": not problems,
                          "problems": problems})
        configured = _series_configured(conn)
    import config

    return {"vendors": vendors, "stores": stores, "drugs": drugs,
            "number_series_configured": configured,
            "licence_warn_days": int(config.PHARMACY_LICENCE_WARN_DAYS)}


# ═════════════════════════════════════════════════════════════════════════════
#  Goods receipts (grn-8)
# ═════════════════════════════════════════════════════════════════════════════

GRN_NUMBER_SERIES = "PHGRN"
GRN_STATUSES = ("draft", "posted", "cancelled", "returned")

#: An invoice total within this many paise of the receipt's own total is a
#: rounding difference, not a mismatch (invoices round to the rupee).
INVOICE_TOLERANCE_PAISE = 100

_TEXT_LIMIT = 60


def _min_shelf_life_days() -> int:
    import config

    return max(0, int(getattr(config, "PHARMACY_MIN_SHELF_LIFE_DAYS", 90) or 0))


def _receipt_text(value: Any, name: str, *, required: bool, limit: int = _TEXT_LIMIT) -> Optional[str]:
    if value is not None and not isinstance(value, str):
        raise PurchasingError(f"{name} must be text", code="bad_value")
    text = " ".join((value or "").split())
    if not text:
        if required:
            raise PurchasingError(f"enter the {name}", code="missing_field", detail={"field": name})
        return None
    if len(text) > limit:
        raise PurchasingError(f"{name} is longer than {limit} characters", code="bad_value")
    return text


def _receipt_date(value: Any, name: str, *, required: bool) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise PurchasingError(f"enter the {name}", code="missing_field", detail={"field": name})
        return None
    if not isinstance(value, str):
        raise PurchasingError(f"{name} is a date (YYYY-MM-DD)", code="bad_date")
    text = value.strip()
    try:
        if len(text) != 10:
            raise ValueError
        date.fromisoformat(text)
    except ValueError:
        raise PurchasingError(f"{name} is a date (YYYY-MM-DD), not {value!r}",
                              code="bad_date") from None
    return text


def _clean_receipt_lines(raw_lines: Sequence[Any]) -> list[dict]:
    """Each receipt line as typed: whole packs received, free and rejected,
    the batch, the expiry typed twice, the MRP per pack and the optional
    manufacturing date. Shape only; the rules come later."""
    if not isinstance(raw_lines, (list, tuple)) or not raw_lines:
        raise PurchasingError("a goods receipt needs at least one line", code="empty_document")
    if len(raw_lines) > MAX_LINES:
        raise PurchasingError(f"a goods receipt has at most {MAX_LINES} lines", code="bad_value")
    out: list[dict] = []
    for n, raw in enumerate(raw_lines, start=1):
        if not isinstance(raw, Mapping):
            raise PurchasingError("each line is an object", code="bad_line")
        po_line_no = _whole(raw.get("po_line_no"), f"line {n} order line", minimum=1)
        packs = _whole(raw.get("packs", 0) if raw.get("packs") is not None else 0,
                       f"line {n} packs received", minimum=0, maximum=MAX_PACKS)
        free = _whole(raw.get("free_packs", 0) if raw.get("free_packs") is not None else 0,
                      f"line {n} free packs", minimum=0, maximum=MAX_PACKS)
        rejected = _whole(raw.get("rejected_packs", 0)
                          if raw.get("rejected_packs") is not None else 0,
                          f"line {n} packs rejected", minimum=0, maximum=MAX_PACKS)
        if packs + free + rejected == 0:
            raise PurchasingError(f"line {n} receives nothing; leave it off the receipt",
                                  code="empty_line")
        if free and not packs:
            raise PurchasingError(f"line {n}: free packs come with paid packs on the same line",
                                  code="bad_line")
        reason = _receipt_text(raw.get("rejection_reason"), f"line {n} rejection reason",
                               required=bool(rejected), limit=500)
        line = {"n": n, "po_line_no": po_line_no, "packs": packs, "free_packs": free,
                "rejected_packs": rejected, "rejection_reason": reason,
                "batch_no": None, "expiry_date": None, "mrp_pack_paise": None, "mfg_date": None,
                "unit_rate_paise": None,
                "remarks": _receipt_text(raw.get("remarks"), f"line {n} remarks",
                                         required=False, limit=500)}
        if packs:
            line["batch_no"] = _receipt_text(raw.get("batch_no"), f"line {n} batch number",
                                             required=True)
            expiry = _receipt_date(raw.get("expiry_date"), f"line {n} expiry date",
                                   required=True)
            again = _receipt_date(raw.get("expiry_date_confirm"),
                                  f"line {n} expiry date, typed again", required=True)
            if expiry != again:
                raise PurchasingError(
                    f"line {n}: the two expiry dates do not match ({expiry} and {again}). Check "
                    "the pack and type the expiry again. Nothing was saved.",
                    code="expiry_mismatch", detail={"line": n})
            line["expiry_date"] = expiry
            mrp = raw.get("mrp_pack_paise")
            if mrp is None:
                raise PurchasingError(f"line {n}: enter the MRP printed on the pack",
                                      code="missing_field", detail={"field": "mrp_pack_paise"})
            line["mrp_pack_paise"] = _whole(mrp, f"line {n} MRP per pack", minimum=1)
            line["mfg_date"] = _receipt_date(raw.get("mfg_date"), f"line {n} manufacturing date",
                                             required=False)
            if raw.get("unit_rate_paise") is not None:
                line["unit_rate_paise"] = _whole(raw.get("unit_rate_paise"),
                                                 f"line {n} rate per pack", minimum=0)
        out.append(line)
    return out


def _days_left(expiry: Optional[str], today: str) -> Optional[int]:
    if not expiry:
        return None
    return (date.fromisoformat(str(expiry)[:10]) - date.fromisoformat(today)).days


def _round_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator // 2) // denominator


def _receipt_line_view(line: dict, *, today: str, min_days: int) -> dict:
    """A receipt line with its packs, units, landed unit cost and expiry
    verdicts, for the screen and for the post."""
    out = dict(line)
    per_pack = line.get("units_per_pack")
    packs = int(line.get("accepted_quantity_milli") or 0) // QUANTITY_SCALE
    free = int(line.get("free_quantity_milli") or 0) // QUANTITY_SCALE
    rejected = int(line.get("rejected_quantity_milli") or 0) // QUANTITY_SCALE
    units = (packs + free) * int(per_pack) if per_pack and packs else 0
    out.update({
        "packs": packs, "free_packs": free, "rejected_packs": rejected,
        "units": units, "free_units": free * int(per_pack) if per_pack else 0,
        "unit_cost_paise": _round_div(int(line.get("total_paise") or 0), units) if units else None,
    })
    left = _days_left(line.get("expiry_date"), today) if packs else None
    out["days_to_expiry"] = left
    out["expired"] = left is not None and left < 0
    out["short_dated"] = left is not None and 0 <= left < min_days
    mrp = line.get("mrp_pack_paise")
    out["rate_above_mrp"] = bool(packs and mrp is not None
                                 and int(line.get("unit_rate_paise") or 0) > int(mrp))
    return out


def _receipt_warnings(doc: dict) -> list[dict]:
    warnings: list[dict] = []
    for line in doc.get("lines", []):
        name = line.get("description") or line.get("item_code")
        if line.get("short_dated"):
            warnings.append({
                "code": "short_shelf_life", "line_no": line["line_no"],
                "message": f"{name} batch {line.get('batch_no')} has {line['days_to_expiry']} "
                           f"day{'' if line['days_to_expiry'] == 1 else 's'} left, under the "
                           f"{doc['min_shelf_life_days']} the hospital asks for. Posting it "
                           "needs a person with approval rights, with a reason."})
        if line.get("rate_above_mrp"):
            warnings.append({
                "code": "rate_above_mrp", "line_no": line["line_no"],
                "message": f"{name}: the rate per pack is above the MRP printed on the pack."})
    invoice = doc.get("vendor_invoice_total_paise")
    if invoice is not None and abs(int(invoice) - int(doc.get("total_paise") or 0)) \
            > INVOICE_TOLERANCE_PAISE:
        warnings.append({
            "code": "invoice_total_mismatch",
            "difference_paise": int(invoice) - int(doc.get("total_paise") or 0),
            "message": "The vendor's invoice total differs from this receipt's total. Check "
                       "the rates, discounts and quantities against the invoice."})
    return warnings


def _receipt_series_configured(conn) -> bool:
    row = conn.execute("SELECT is_configured FROM document_number_series WHERE series_code = ?",
                       (GRN_NUMBER_SERIES,)).fetchone()
    return bool(row and row["is_configured"])


def _decorate_receipt(conn, doc: dict, *, today: Optional[str] = None) -> dict:
    day = today or stock.site_today()
    min_days = _min_shelf_life_days()
    doc["min_shelf_life_days"] = min_days
    doc["lines"] = [_receipt_line_view(line, today=day, min_days=min_days)
                    for line in doc.get("lines", [])]
    names = _user_names(conn, {doc.get("received_by"), doc.get("posted_by"),
                               doc.get("cancelled_by")})
    doc["received_by_name"] = names.get(doc.get("received_by"))
    doc["posted_by_name"] = names.get(doc.get("posted_by"))
    doc["cancelled_by_name"] = names.get(doc.get("cancelled_by"))
    doc["needs_approval"] = doc.get("status") == "draft" and any(
        line["short_dated"] for line in doc["lines"])
    doc["has_expired_line"] = any(line["expired"] for line in doc["lines"])
    doc["number_series_configured"] = _receipt_series_configured(conn)
    doc["warnings"] = _receipt_warnings(doc)
    return doc


def _load_receipt(conn, grn_id: int) -> dict:
    doc = procurement._get_goods_receipt_core(conn, grn_id, domain=DOMAIN)
    if not doc:
        raise PurchasingError(f"no goods receipt {grn_id}", code="not_found")
    return doc


def _audit_receipt(conn, actor: Mapping, action: str, *, summary: str, grn_id: int,
                   metadata: dict, audit_ctx: Optional[Mapping] = None) -> None:
    """One 'security' row in the receipt's transaction (fail-closed). A goods
    receipt is hospital business: no patient id, ids as strings."""
    from services import audit_service

    ctx = audit_ctx or {}
    audit_service.log_audit_event(
        correlation_id=str(uuid.uuid4()), event_category="security", action_type=action,
        outcome="success", db=conn, summary_message=summary,
        actor_user_id=str(actor.get("id")) if actor.get("id") is not None else None,
        actor_role=actor.get("role"), target_type="pharmacy_goods_receipt",
        target_id=str(grn_id), request_path=ctx.get("request_path"),
        http_method=ctx.get("http_method"), ip_address=ctx.get("ip_address"),
        metadata=metadata)


def get_goods_receipt(grn_id: int) -> Optional[dict]:
    with db_session() as conn:
        doc = procurement._get_goods_receipt_core(conn, grn_id, domain=DOMAIN)
        return _decorate_receipt(conn, doc) if doc else None


def list_goods_receipts(*, status: Optional[str] = None, purchase_order_id: Optional[int] = None,
                        limit: int = 100) -> list[dict]:
    if status is not None and status not in GRN_STATUSES:
        raise PurchasingError(f"unknown goods receipt status {status!r}", code="bad_value")
    return procurement.list_goods_receipts(status=status, purchase_order_id=purchase_order_id,
                                           limit=max(1, min(int(limit), 500)), domain=DOMAIN)


def receivable_orders() -> dict:
    """Pharmacy orders sent to a vendor that still have packs to come, each
    line with what is outstanding, for the receipt form. No patient data."""
    today = stock.site_today()
    with db_session() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM purchase_orders WHERE domain = ? AND status IN "
            f"({','.join('?' for _ in procurement.RECEIVABLE_PO_STATUSES)}) "
            "ORDER BY order_date, id LIMIT 200",
            (DOMAIN, *sorted(procurement.RECEIVABLE_PO_STATUSES))).fetchall()]
        orders = []
        for po_id in ids:
            doc = procurement._get_purchase_order_core(conn, po_id, domain=DOMAIN)
            lines = []
            for line in doc.get("lines", []):
                if line.get("status") in ("cancelled", "short_closed"):
                    continue
                outstanding = int(line.get("outstanding_quantity_milli") or 0) // QUANTITY_SCALE
                if outstanding <= 0:
                    continue
                drug = _find_drug(conn, line.get("helix_medication_id") or "")
                lines.append({
                    "line_no": line["line_no"], "helix_medication_id": line.get("helix_medication_id"),
                    "description": line.get("description") or line.get("item_code"),
                    "uom": line.get("uom"), "units_per_pack": line.get("units_per_pack"),
                    "ordered_packs": int(line["quantity_milli"]) // QUANTITY_SCALE,
                    "received_packs": int(line.get("received_quantity_milli") or 0)
                    // QUANTITY_SCALE,
                    "outstanding_packs": outstanding,
                    "unit_rate_paise": line.get("unit_rate_paise"),
                    "gst_rate_bp": line.get("gst_rate_bp"),
                    "ndps": bool(drug is not None and _is_ndps(drug))})
            if not lines:
                continue
            vendor = conn.execute("SELECT * FROM vendors WHERE id = ?",
                                  (doc.get("vendor_id"),)).fetchone()
            orders.append({
                "id": doc["id"], "po_number": doc.get("po_number"), "status": doc["status"],
                "vendor_code": doc.get("vendor_code"), "vendor_name": doc.get("vendor_name"),
                "store_code": doc.get("store_code"), "store_name": doc.get("store_name"),
                "order_date": doc.get("order_date"),
                "vendor_licence": vendor_licence(vendor, today=today) if vendor else None,
                "lines": lines})
        configured = _receipt_series_configured(conn)
    return {"orders": orders, "min_shelf_life_days": _min_shelf_life_days(),
            "number_series_configured": configured, "today": today}


def _order_rate(po_line: Mapping) -> Optional[int]:
    """The rate per pack a receipt line takes when it names none: the order's
    NET rate when the order line carries a discount (procurement's receipt
    core prices a line from its rate alone, so the discount would otherwise
    be lost from the landed cost), else None for the order's own rate."""
    if int(po_line.get("discount_paise") or 0) <= 0 or not int(po_line.get("quantity_milli") or 0):
        return None
    return _round_div(int(po_line.get("taxable_value_paise") or 0) * QUANTITY_SCALE,
                      int(po_line["quantity_milli"]))


def _receipt_store(conn, po: dict, store_code: Optional[str]):
    code = store_code
    if not code:
        row = conn.execute("SELECT code, pharmacy_kind FROM stores WHERE id = ?",
                           (po.get("store_id"),)).fetchone()
        if row is None or row["pharmacy_kind"] is None:
            raise PurchasingError("this order has no pharmacy store to receive into",
                                  code="store_required")
        code = row["code"]
    return _check_store(conn, code)


#: How an order that can take no more goods is named on screen.
_ORDER_STATE_WORDS = {
    "draft": "still a draft", "pending_approval": "waiting for approval",
    "approved": "approved but not yet sent to the vendor", "received": "fully received",
    "closed": "closed", "cancelled": "cancelled",
}
#: Order line statuses that take no more goods, and how they are named.
_CLOSED_LINE_WORDS = {"cancelled": "cancelled", "short_closed": "closed short"}


def _check_order_receivable(po: Mapping, po_line_nos: Sequence[int], *, posting: bool) -> None:
    """The order must still be with the vendor with packs to come, and every
    order line the receipt names must still be open, at draft AND at post. An
    order cancelled or closed, or a line closed, after a draft was made puts
    nothing on the shelf: procurement's post core leaves cancelled lines out
    when it derives the order's status, so without this a stale draft would
    post stock against a cancelled order. Raises; writes nothing."""
    if po["status"] not in procurement.RECEIVABLE_PO_STATUSES:
        if not posting:
            raise PurchasingError(
                "goods are received only against an order sent to the vendor that still has "
                "packs to come", code="po_not_receivable")
        words = _ORDER_STATE_WORDS.get(str(po["status"]), "no longer open")
        raise PurchasingError(
            f"order {po.get('po_number') or po['id']} is {words}, so nothing more can be "
            "received against it. Cancel this receipt. Nothing was changed.",
            code="po_not_receivable", detail={"order_status": po["status"]})
    lines = {int(line["line_no"]): line for line in po.get("lines", [])}
    for po_line_no in po_line_nos:
        line = lines.get(int(po_line_no)) if po_line_no is not None else None
        if line is None:
            continue
        words = _CLOSED_LINE_WORDS.get(str(line.get("status")))
        if words:
            name = line.get("description") or line.get("item_code") or "this drug"
            raise PurchasingError(
                f"order line {po_line_no} ({name}) is {words}, so nothing can be received "
                f"against it.{' Cancel this receipt.' if posting else ''} Nothing was changed.",
                code="line_closed", detail={"po_line_no": int(po_line_no)})


def _check_receipt_lines(conn, lines: list[dict], *, po: dict, store: Mapping, today: str
                         ) -> tuple[list[dict], bool]:
    """Every line against its order line, the drug master, the store and the
    stock already on file. Returns (lines with their order line and drug, any
    NDPS line). Raises on the first refusal; nothing is written."""
    po_lines = {int(line["line_no"]): line for line in po.get("lines", [])}
    asked: dict[int, int] = {}
    checked: list[dict] = []
    any_ndps = False
    #: (drug, batch) -> (expiry, MRP) across this receipt's own lines: one
    #: batch on two lines must agree with itself as much as with the shelf.
    batches: dict[tuple[str, str], tuple[Any, Any]] = {}
    for line in lines:
        po_line = po_lines.get(line["po_line_no"])
        if po_line is None:
            raise PurchasingError(f"the order has no line {line['po_line_no']}", code="bad_line")
        drug = _orderable_drug(conn, po_line.get("helix_medication_id") or "")
        _check_drug_for_store(drug, store)
        any_ndps = any_ndps or _is_ndps(drug)
        if not po_line.get("units_per_pack"):
            raise PurchasingError(f"{_drug_name(drug)}: the order line has no pack size, so the "
                                  "units cannot be counted", code="pack_size_not_configured")
        asked[line["po_line_no"]] = asked.get(line["po_line_no"], 0) + \
            (line["packs"] + line["rejected_packs"]) * QUANTITY_SCALE
        if line["packs"]:
            left = _days_left(line["expiry_date"], today)
            if left < 0:
                raise PurchasingError(
                    f"{_drug_name(drug)} batch {line['batch_no']} expired on "
                    f"{line['expiry_date']}. Expired stock is never received.",
                    code="expired_on_receipt", detail={"line": line["n"]})
            if line["mfg_date"] and line["mfg_date"] > today:
                raise PurchasingError(f"line {line['n']}: the manufacturing date is in the "
                                      "future", code="bad_date")
            conflict = stock.batch_conflict(conn, medication_id=drug["id"],
                                            lot_number=line["batch_no"],
                                            expiration_date=line["expiry_date"],
                                            store_id=store["id"],
                                            mrp_pack_paise=line["mrp_pack_paise"])
            if conflict:
                raise PurchasingError(conflict["message"], code=conflict["code"],
                                      detail={"line": line["n"], "lot_id": conflict["lot_id"]})
            key = (str(drug["id"]), "".join(str(line["batch_no"]).upper().split()))
            seen = batches.setdefault(key, (line["expiry_date"], line["mrp_pack_paise"]))
            if seen[0] != line["expiry_date"]:
                raise PurchasingError(
                    f"batch {line['batch_no']} of {_drug_name(drug)} is on this receipt twice "
                    "with different expiry dates. One batch has one expiry date.",
                    code="batch_expiry_mismatch", detail={"line": line["n"]})
            if seen[1] != line["mrp_pack_paise"]:
                raise PurchasingError(
                    f"batch {line['batch_no']} of {_drug_name(drug)} is on this receipt twice "
                    "with different MRPs. One batch carries one printed MRP.",
                    code="batch_mrp_mismatch", detail={"line": line["n"]})
        checked.append({**line, "po_line": po_line, "drug": drug})
    for po_line_no, milli in asked.items():
        outstanding = int(po_lines[po_line_no].get("outstanding_quantity_milli") or 0)
        if milli > outstanding:
            raise PurchasingError(
                f"order line {po_line_no}: receiving {milli // QUANTITY_SCALE} packs against "
                f"{outstanding // QUANTITY_SCALE} still to come would over-receive the order. "
                "Raise a new order for the excess.", code="over_receipt")
    return checked, any_ndps


def create_goods_receipt(*, purchase_order_id: int, lines: Sequence[Any], actor: Mapping,
                         vendor_invoice_number: Any = None, vendor_invoice_date: Any = None,
                         vendor_invoice_total_paise: Any = None, store_code: Optional[str] = None,
                         remarks: Optional[str] = None, idempotency_key: Optional[str] = None,
                         audit_ctx: Optional[Mapping] = None) -> dict:
    """Record what arrived as a DRAFT receipt (the write tier). Nothing moves
    into stock until it is posted. Returns {"goods_receipt", "warnings",
    "replayed"}; a replay of the same Idempotency-Key returns the receipt it
    made, and writes nothing."""
    actor_id = _actor_id(actor)
    po_id = _whole(purchase_order_id, "purchase order", minimum=1)
    clean = _clean_receipt_lines(lines)
    invoice_no = _receipt_text(vendor_invoice_number, "vendor invoice number", required=True)
    invoice_date = _receipt_date(vendor_invoice_date, "vendor invoice date", required=True)
    invoice_total = (None if vendor_invoice_total_paise is None
                     else _whole(vendor_invoice_total_paise, "vendor invoice total", minimum=0))
    note = _receipt_text(remarks, "remarks", required=False, limit=2000)
    key = _receipt_text(idempotency_key, "request key", required=False, limit=200)
    today = stock.site_today()
    if invoice_date > today:
        raise PurchasingError("the vendor invoice date is in the future", code="bad_date")
    with db_session(immediate=True) as conn:
        if key:
            try:
                existing = procurement._goods_receipt_id_for_key(conn, key, domain=DOMAIN)
            except procurement.ProcurementError as exc:
                raise _procurement_error(exc) from exc
            if existing is not None:
                doc = _load_receipt(conn, existing)
                if int(doc.get("purchase_order_id") or 0) != po_id:
                    raise PurchasingError("this request key was already used for another "
                                          "receipt. Nothing was saved.",
                                          code="idempotency_conflict")
                doc = _decorate_receipt(conn, doc, today=today)
                return {"goods_receipt": doc, "warnings": doc["warnings"], "replayed": True}
        po = _load(conn, po_id)
        _check_order_receivable(po, [c["po_line_no"] for c in clean], posting=False)
        store = _receipt_store(conn, po, store_code)
        checked, any_ndps = _check_receipt_lines(conn, clean, po=po, store=store, today=today)
        _vendor, licence_warnings = _check_vendor(conn, po.get("vendor_code") or "",
                                                  needs_ndps=any_ndps, today=today)
        try:
            grn_id = procurement._create_goods_receipt_core(
                conn, purchase_order_id=po_id, store_code=store["code"],
                lines=[{"po_line_no": c["po_line_no"],
                        "accepted_quantity_milli": c["packs"] * QUANTITY_SCALE,
                        "rejected_quantity_milli": c["rejected_packs"] * QUANTITY_SCALE,
                        "rejection_reason": c["rejection_reason"],
                        "batch_no": (stock.batch_spelling(conn, c["drug"]["id"], c["batch_no"])
                                     if c["batch_no"] else None),
                        "expiry_date": c["expiry_date"],
                        "unit_rate_paise": (c["unit_rate_paise"]
                                            if c["unit_rate_paise"] is not None
                                            else _order_rate(c["po_line"])),
                        "free_quantity_milli": c["free_packs"] * QUANTITY_SCALE,
                        "mrp_pack_paise": c["mrp_pack_paise"], "mfg_date": c["mfg_date"],
                        "remarks": c["remarks"]} for c in checked],
                vendor_invoice_number=invoice_no, vendor_invoice_date=invoice_date,
                vendor_invoice_total_paise=invoice_total, qc_status="passed",
                received_by=actor_id, remarks=note, idempotency_key=key, today=today,
                domain=DOMAIN)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        doc = _decorate_receipt(conn, _load_receipt(conn, grn_id), today=today)
        warnings = licence_warnings + doc["warnings"]
        _audit_receipt(conn, actor, "pharmacy_grn_created",
                       summary=f"Pharmacy goods receipt drafted against order "
                               f"{po.get('po_number') or po_id} ({len(doc['lines'])} lines)",
                       grn_id=grn_id,
                       metadata={"goods_receipt_id": str(grn_id),
                                 "purchase_order_id": str(po_id), "store_code": store["code"],
                                 "vendor_invoice_number": invoice_no,
                                 "lines": len(doc["lines"]),
                                 "drug_ids": [str(c["drug"]["id"]) for c in checked],
                                 "total_paise": doc.get("total_paise"),
                                 "warnings": [w["code"] for w in warnings]},
                       audit_ctx=audit_ctx)
    return {"goods_receipt": doc, "warnings": warnings, "replayed": False}


def post_goods_receipt(grn_id: int, *, actor: Mapping, may_write: bool, may_approve: bool,
                       short_expiry_reason: Optional[str] = None,
                       audit_ctx: Optional[Mapping] = None) -> dict:
    """Put a draft receipt on the shelf, in ONE immediate transaction (see
    the module docstring). A receipt already posted is answered as it is,
    with replayed True, and nothing is written again.

    Who may post: the write tier. A receipt holding a batch with less than
    the minimum shelf life left needs the approve tier instead, from someone
    other than the person who received it, with a reason (GRN-R16); the
    reason and the lines it covered are on the audit row. An expired batch
    is never posted."""
    actor_id = _actor_id(actor)
    today = stock.site_today()
    reason = _receipt_text(short_expiry_reason, "reason for accepting short expiry",
                           required=False, limit=1000)
    with db_session(immediate=True) as conn:
        doc = _load_receipt(conn, grn_id)
        if doc["status"] == "posted":
            doc = _decorate_receipt(conn, doc, today=today)
            return {"goods_receipt": doc, "warnings": [], "replayed": True}
        if doc["status"] != "draft":
            raise PurchasingError(f"this receipt is {doc['status']} and cannot be posted",
                                  code="illegal_transition")
        doc = _decorate_receipt(conn, doc, today=today)
        # The order and its lines as they are NOW, before any other check: an
        # order cancelled or closed, or a line closed, since this draft was
        # made takes nothing onto the shelf, whoever is posting.
        po = _load(conn, int(doc["purchase_order_id"]))
        relines = [{"n": line["line_no"], "po_line_no": None, "packs": line["packs"],
                    "rejected_packs": line["rejected_packs"], "batch_no": line.get("batch_no"),
                    "expiry_date": line.get("expiry_date"), "mfg_date": line.get("mfg_date"),
                    "mrp_pack_paise": line.get("mrp_pack_paise")}
                   for line in doc["lines"]]
        po_line_by_id = {int(p["id"]): int(p["line_no"]) for p in po.get("lines", [])}
        for reline, line in zip(relines, doc["lines"]):
            reline["po_line_no"] = po_line_by_id.get(int(line.get("purchase_order_line_id") or 0))
            if reline["po_line_no"] is None:
                raise PurchasingError(f"receipt line {line['line_no']} has lost its order line",
                                      code="bad_line")
        _check_order_receivable(po, [r["po_line_no"] for r in relines], posting=True)
        if doc["has_expired_line"]:
            raise PurchasingError("a batch on this receipt has expired since it was drafted. "
                                  "Cancel the receipt and record the delivery again without it.",
                                  code="expired_on_receipt")
        short = [line["line_no"] for line in doc["lines"] if line["short_dated"]]
        if short:
            if not may_approve:
                raise PurchasingError(
                    "a batch on this receipt has less shelf life left than the hospital asks "
                    "for. A person with approval rights must accept it, with a reason. Nothing "
                    "was changed.", code="approve_needed", detail={"short_dated_lines": short})
            if str(doc.get("received_by") or "") == actor_id:
                raise PurchasingError("you received this delivery, so someone else must accept "
                                      "its short expiry. Nothing was changed.",
                                      code="same_person")
            if not reason:
                raise PurchasingError("give the reason for accepting stock with short expiry",
                                      code="missing_reason")
        elif not may_write:
            raise PurchasingError("posting a receipt needs the pharmacy write tier",
                                  code="write_needed")
        store = _check_store(conn, doc.get("store_code") or "")
        _checked, any_ndps = _check_receipt_lines(conn, relines, po=po, store=store, today=today)
        _vendor, licence_warnings = _check_vendor(conn, po.get("vendor_code") or "",
                                                  needs_ndps=any_ndps, today=today)
        try:
            procurement._post_goods_receipt_core(conn, grn_id, actor=actor_id, domain=DOMAIN)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        receipt_lines = [{
            "line_no": line["line_no"], "line_id": line["id"],
            "medication_id": line["helix_medication_id"], "batch_no": line["batch_no"],
            "expiry_date": line["expiry_date"], "units": line["units"],
            "units_per_pack": line.get("units_per_pack"),
            "mrp_pack_paise": line.get("mrp_pack_paise"),
            "unit_cost_paise": line["unit_cost_paise"], "mfg_date": line.get("mfg_date"),
        } for line in doc["lines"] if line["units"] > 0]
        try:
            received = stock.receive_grn_lines(
                conn, grn_id=grn_id, store_id=int(store["id"]), vendor_id=doc.get("vendor_id"),
                lines=receipt_lines, actor_id=actor_id, today=today)
        except stock.StockError as exc:
            raise PurchasingError(str(exc), code=exc.code) from exc
        for item in received:
            conn.execute("UPDATE goods_receipt_lines SET helix_lot_ref = ? "
                         "WHERE goods_receipt_id = ? AND line_no = ?",
                         (item["lot_id"], grn_id, item["line_no"]))
        po_status = conn.execute("SELECT status FROM purchase_orders WHERE id = ?",
                                 (po["id"],)).fetchone()["status"]
        _audit_receipt(conn, actor, "pharmacy_grn_posted",
                       summary=f"Pharmacy goods receipt {grn_id} posted into {store['name']} "
                               f"({sum(i['units'] for i in received)} units)",
                       grn_id=grn_id,
                       metadata={"goods_receipt_id": str(grn_id),
                                 "purchase_order_id": str(po["id"]),
                                 "purchase_order_status": po_status,
                                 "store_id": str(store["id"]),
                                 "lots": [{"line_no": i["line_no"], "lot_id": str(i["lot_id"]),
                                           "movement_id": (str(i["movement_id"])
                                                           if i["movement_id"] else None),
                                           "units": i["units"],
                                           "unit_cost_paise": i["unit_cost_paise"],
                                           "merged": i["merged"]} for i in received],
                                 "short_expiry_accepted": ({"lines": short, "reason": reason,
                                                            "received_by":
                                                                str(doc.get("received_by") or "")}
                                                           if short else None),
                                 "total_paise": doc.get("total_paise")},
                       audit_ctx=audit_ctx)
    number = procurement._stamp_document_number("pharmacy_goods_receipt", grn_id)
    if number is None:
        logger.error("pharmacy goods receipt %s was posted but not numbered; stamp it again",
                     grn_id)
    with db_session() as conn:
        doc = _decorate_receipt(conn, _load_receipt(conn, grn_id), today=today)
    return {"goods_receipt": doc, "warnings": licence_warnings, "replayed": False}


def cancel_goods_receipt(grn_id: int, *, reason: Optional[str], actor: Mapping,
                         audit_ctx: Optional[Mapping] = None) -> dict:
    """Cancel a DRAFT receipt with a reason (the write tier). A posted
    receipt is never cancelled: its stock is on the shelf."""
    actor_id = _actor_id(actor)
    text = _receipt_text(reason, "reason", required=True, limit=1000)
    with db_session(immediate=True) as conn:
        doc = _load_receipt(conn, grn_id)
        try:
            procurement._cancel_goods_receipt_core(conn, grn_id, reason=text, actor=actor_id,
                                                   domain=DOMAIN)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        _audit_receipt(conn, actor, "pharmacy_grn_cancelled",
                       summary=f"Pharmacy goods receipt {grn_id} cancelled before posting",
                       grn_id=grn_id,
                       metadata={"goods_receipt_id": str(grn_id),
                                 "purchase_order_id": str(doc.get("purchase_order_id")),
                                 "reason": text},
                       audit_ctx=audit_ctx)
        doc = _decorate_receipt(conn, _load_receipt(conn, grn_id))
    return {"goods_receipt": doc, "warnings": [], "replayed": False}


# ═════════════════════════════════════════════════════════════════════════════
#  Returns to vendor (grn-22)
# ═════════════════════════════════════════════════════════════════════════════
#
# A return document (PHVR) takes stock that should not stay in the hospital
# back to the vendor who supplied it: lots that are held off the shelf
# (quarantined, recalled, not of standard quality, a ward return waiting for
# inspection) or that are expired or near expiry (pharmacy_store_service
# decides which, return_eligibility). The life of one:
#   draft      lots of ONE store going to ONE vendor, units per lot, a reason.
#              Nothing moves; the units are still on the lot.
#   posted     one vendor_return movement per line, through the single stock
#              writer, in ONE immediate transaction with the 'security' audit
#              row. A held lot that is emptied is closed as returned to the
#              vendor. The PHVR number is drawn after the commit.
#   credited   the vendor's credit note number recorded against it.
#   cancelled  a draft given up, with a reason. A posted return is never
#              cancelled: its units have left.
# Strict defaults (each listed for Samuel):
#   * posting needs the approve tier, from somebody other than the person who
#     drafted it (separation of duties, as for a purchase order);
#   * a lot received from another vendor is refused; a lot with no vendor on
#     file (stock older than the pharmacy store) goes with a warning;
#   * a narcotic or psychotropic line (GRN-R14) needs the NDPS permission to
#     draft, the vendor's NDPS licence on file, and at posting a witness: a
#     third person holding pharmacy_store.ndps, neither the drafter nor the
#     poster, confirming with their own signing PIN. The register (grn-13)
#     enters the return when it is on;
#   * the units of each line are checked again at posting: never more than
#     the lot holds then, and the lot must still be returnable.

VR_NUMBER_SERIES = "PHVR"
VR_NUMBER_KIND = "pharmacy_vendor_return"
#: reference_type on the vendor_return movements (the ledger labels it).
VR_REFERENCE = "pharmacy_vendor_return"
VR_STATUSES: tuple[str, ...] = ("draft", "posted", "credited", "cancelled")
VR_STATUS_LABELS = {
    "draft": "Draft",
    "posted": "Sent back",
    "credited": "Credit note received",
    "cancelled": "Cancelled",
}
VR_MAX_LINES = 200
VR_MAX_UNITS = 1_000_000
VR_TEXT_LIMIT = 1000


def _vr_audit(conn, actor: Mapping, action: str, *, summary: str, return_id: int,
              metadata: dict, audit_ctx: Optional[Mapping] = None) -> None:
    """One 'security' row in the return's transaction (fail-closed). A return
    to vendor is hospital business: no patient id, ids as strings."""
    from services import audit_service

    ctx = audit_ctx or {}
    audit_service.log_audit_event(
        correlation_id=str(uuid.uuid4()), event_category="security", action_type=action,
        outcome="success", db=conn, summary_message=summary,
        actor_user_id=str(actor.get("id")) if actor.get("id") is not None else None,
        actor_role=actor.get("role"), target_type="pharmacy_vendor_return",
        target_id=str(return_id), request_path=ctx.get("request_path"),
        http_method=ctx.get("http_method"), ip_address=ctx.get("ip_address"),
        metadata=metadata)


def _vr_text(value: Any, name: str, *, required: bool) -> Optional[str]:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise PurchasingError(f"{name} must be text", code="bad_value")
    if len(text) > VR_TEXT_LIMIT:
        raise PurchasingError(f"{name} is at most {VR_TEXT_LIMIT} characters", code="bad_value")
    if required and not text:
        raise PurchasingError(f"give {name}", code="missing_reason" if "reason" in name
                              else "missing_field")
    return text or None


def _vr_clean_lines(raw: Any) -> list[dict]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise PurchasingError("name at least one lot to return", code="empty_document")
    if len(raw) > VR_MAX_LINES:
        raise PurchasingError(f"a return covers at most {VR_MAX_LINES} lots", code="bad_value")
    out: list[dict] = []
    seen: set[str] = set()
    for n, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise PurchasingError("each line is an object", code="bad_line")
        lot_id = item.get("lot_id")
        if not isinstance(lot_id, str) or not lot_id.strip():
            raise PurchasingError(f"line {n} names no lot", code="bad_line")
        lot_id = lot_id.strip()
        if lot_id in seen:
            raise PurchasingError("a lot is named once per return", code="duplicate_line")
        seen.add(lot_id)
        out.append({"lot_id": lot_id,
                    "quantity_units": _whole(item.get("quantity_units"),
                                             f"units on line {n}", minimum=1,
                                             maximum=VR_MAX_UNITS),
                    "reason": _vr_text(item.get("reason"), f"the reason for line {n}",
                                       required=False)})
    return out


def _vr_store(conn, store_code: Any):
    from services import pharmacy_store_service as store_rules

    code = (store_code or "").strip().upper() if isinstance(store_code, str) else ""
    if not code:
        raise PurchasingError("choose the store the stock goes back from", code="store_required")
    store = conn.execute("SELECT * FROM stores WHERE code = ? AND pharmacy_kind IS NOT NULL",
                         (code,)).fetchone()
    if store is None:
        raise PurchasingError(f"no pharmacy store {store_code!r}", code="store_not_found")
    if not store["is_active"]:
        raise PurchasingError(f"{store['name']} is closed", code="store_inactive")
    if store["pharmacy_kind"] not in store_rules.RETURN_STORE_KINDS:
        raise PurchasingError(f"{store['name']} does not hold stock that goes back to a vendor",
                              code="store_not_returnable")
    return store


def _vr_vendor(conn, vendor_code: Any, *, needs_ndps: bool, today: str):
    """The vendor, with warnings. Stock goes back to a blacklisted or inactive
    vendor too (it is theirs to take back); a narcotic or psychotropic goes
    only to a vendor with an NDPS licence on file."""
    code = (vendor_code or "").strip().upper() if isinstance(vendor_code, str) else ""
    vendor = conn.execute("SELECT * FROM vendors WHERE code = ?", (code,)).fetchone() if code \
        else None
    if vendor is None:
        raise PurchasingError(f"no vendor with code {vendor_code!r}", code="vendor_not_found")
    licence = vendor_licence(vendor, today=today)
    warnings: list[dict] = []
    if licence["status"] in ("none", "expired"):
        warnings.append({"code": "vendor_licence_" + ("missing" if licence["status"] == "none"
                                                       else "expired"),
                         "message": f"{vendor['name']} has no valid drug licence on file. "
                                    "Check they may take the stock back."})
    elif licence["warning"]:
        warnings.append(licence["warning"])
    if needs_ndps and not licence["ndps_number"]:
        raise PurchasingError(
            f"{vendor['name']} has no NDPS licence on file, so narcotic and psychotropic "
            "drugs cannot go back to them.", code="vendor_ndps_licence_missing")
    return vendor, warnings


def _vr_check_lines(conn, lines: list[dict], *, store: Mapping, vendor: Mapping,
                    today: str) -> tuple[list[dict], bool, list[dict]]:
    """Each line against its lot now: in this store, returnable, from this
    vendor (or none recorded), never more units than the lot holds. Returns
    (lines with their lots, any narcotic or psychotropic, warnings)."""
    from services import pharmacy_store_service as store_rules

    checked: list[dict] = []
    any_ndps = False
    warnings: list[dict] = []
    for n, line in enumerate(lines, start=1):
        lot = conn.execute(
            "SELECT l.*, m.name AS drug_name, m.ndps_category FROM helix_inventory_lots l "
            "LEFT JOIN helix_medication_master m ON m.id = l.medication_id WHERE l.id = ?",
            (line["lot_id"],)).fetchone()
        if lot is None or lot["store_id"] != store["id"]:
            raise PurchasingError(f"line {n}: lot {line['lot_id']} is not in {store['name']}",
                                  code="lot_not_in_store", detail={"line_no": n})
        verdict = store_rules.return_eligibility(dict(lot), today=today)
        name = lot["drug_name"] or lot["medication_id"]
        if not verdict["returnable"]:
            raise PurchasingError(
                f"line {n}: {name}, batch {lot['lot_number']}, cannot go back to the vendor "
                f"({verdict['why_label'].lower()}). Only stock held off the shelf, expired or "
                "near expiry is returned.", code="lot_not_returnable",
                detail={"line_no": n, "why": verdict["why"]})
        held = int(lot["quantity"] or 0)
        if line["quantity_units"] > held:
            raise PurchasingError(
                f"line {n}: {name}, batch {lot['lot_number']}, holds {held} units, so "
                f"{line['quantity_units']} cannot go back. Nothing was changed.",
                code="exceeds_lot", detail={"line_no": n, "lot_units": held})
        if lot["vendor_id"] is not None and int(lot["vendor_id"]) != int(vendor["id"]):
            raise PurchasingError(
                f"line {n}: {name}, batch {lot['lot_number']}, came from another vendor. "
                "Return it to the vendor who supplied it.", code="lot_from_another_vendor",
                detail={"line_no": n})
        if lot["vendor_id"] is None:
            warnings.append({"code": "vendor_not_recorded",
                             "message": f"Line {n}: no vendor is on file for batch "
                                        f"{lot['lot_number']} of {name}. Check it came from "
                                        f"{vendor['name']}."})
        ndps = lot["ndps_category"] in stock.NDPS_CONTROLLED
        any_ndps = any_ndps or ndps
        checked.append(line | {"line_no": n, "lot": dict(lot), "ndps": ndps,
                               "why": verdict["why"], "why_label": verdict["why_label"]})
    return checked, any_ndps, warnings


def _vr_series_configured(conn) -> bool:
    row = conn.execute("SELECT is_configured FROM document_number_series WHERE series_code = ?",
                       (VR_NUMBER_SERIES,)).fetchone()
    return bool(row and row["is_configured"])


def _vr_raw(conn, return_id: Any):
    try:
        rid = int(return_id)
    except (TypeError, ValueError):
        return None
    return conn.execute("SELECT * FROM pharmacy_vendor_returns WHERE id = ?", (rid,)).fetchone()


def _vr_load(conn, return_id: Any):
    row = _vr_raw(conn, return_id)
    if row is None:
        raise PurchasingError(f"no return to vendor {return_id}", code="not_found")
    return row


def _vr_decorate(conn, row: Mapping, *, with_lines: bool = True) -> dict:
    doc = dict(row)
    store = conn.execute("SELECT code, name, pharmacy_kind FROM stores WHERE id = ?",
                         (doc["store_id"],)).fetchone()
    vendor = conn.execute("SELECT code, name FROM vendors WHERE id = ?",
                          (doc["vendor_id"],)).fetchone()
    names = _user_names(conn, {doc.get("created_by"), doc.get("posted_by"),
                               doc.get("cancelled_by")})
    lines = [dict(r) for r in conn.execute(
        "SELECT rl.*, m.name AS drug_name, m.strength, m.form, m.ndps_category, "
        "l.status AS lot_status, l.quantity AS lot_units FROM pharmacy_vendor_return_lines rl "
        "LEFT JOIN helix_medication_master m ON m.id = rl.helix_medication_id "
        "LEFT JOIN helix_inventory_lots l ON l.id = rl.lot_id "
        "WHERE rl.pharmacy_vendor_return_id = ? ORDER BY rl.line_no", (doc["id"],)).fetchall()]
    for line in lines:
        line["ndps"] = line.pop("ndps_category", None) in stock.NDPS_CONTROLLED
        line["drug_name"] = line["drug_name"] or line["helix_medication_id"]
    doc.update({
        "status_label": VR_STATUS_LABELS.get(doc["status"], doc["status"]),
        "store_code": store["code"] if store else None,
        "store_name": (store["name"] or store["code"]) if store else None,
        "store_kind": store["pharmacy_kind"] if store else None,
        "vendor_code": vendor["code"] if vendor else None,
        "vendor_name": vendor["name"] if vendor else None,
        "created_by_name": names.get(doc.get("created_by")),
        "posted_by_name": names.get(doc.get("posted_by")),
        "cancelled_by_name": names.get(doc.get("cancelled_by")),
        "number_series_configured": _vr_series_configured(conn),
        "line_count": len(lines),
        "total_units": sum(int(line["quantity_units"]) for line in lines),
        "controlled": any(line["ndps"] for line in lines),
    })
    doc["witness_required"] = doc["controlled"]
    if with_lines:
        doc["lines"] = lines
    return doc


def list_vendor_returns(*, status: Optional[str] = None, limit: int = 100) -> list[dict]:
    if status is not None and status not in VR_STATUSES:
        raise PurchasingError(f"unknown return status {status!r}", code="bad_value")
    sql = "SELECT * FROM pharmacy_vendor_returns"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with db_session() as conn:
        return [_vr_decorate(conn, r, with_lines=False)
                for r in conn.execute(sql, params).fetchall()]


def get_vendor_return(return_id: Any) -> Optional[dict]:
    with db_session() as conn:
        row = _vr_raw(conn, return_id)
        return _vr_decorate(conn, row) if row is not None else None


def vendor_return_options(actor: Mapping) -> dict:
    """The stores stock goes back from, the vendors, who may witness a
    controlled return (never the caller) and the site's near-expiry window."""
    from services import ndps_service
    from services import pharmacy_store_service as store_rules

    me = str(actor.get("id") or "")
    with db_session() as conn:
        marks = ",".join("?" for _ in store_rules.RETURN_STORE_KINDS)
        stores = [{"id": s["id"], "code": s["code"], "name": s["name"] or s["code"],
                   "pharmacy_kind": s["pharmacy_kind"]} for s in conn.execute(
            f"SELECT id, code, name, pharmacy_kind FROM stores WHERE is_active = 1 "
            f"AND pharmacy_kind IN ({marks}) ORDER BY name, code",
            store_rules.RETURN_STORE_KINDS).fetchall()]
        today = stock.site_today()
        vendors = []
        for v in conn.execute("SELECT * FROM vendors ORDER BY name, code LIMIT 1000").fetchall():
            licence = vendor_licence(v, today=today)
            vendors.append({"code": v["code"], "name": v["name"],
                            "is_active": bool(v["is_active"]),
                            "licence_status": licence["status"],
                            "has_ndps_licence": bool(licence["ndps_number"])})
        people = ndps_service.witnesses(conn, exclude=me)
        configured = _vr_series_configured(conn)
    return {"stores": stores, "vendors": vendors, "witnesses": people,
            "may_ndps": ndps_service.may_ndps(actor.get("role")),
            "near_expiry_days": store_rules.near_expiry_days(),
            "number_series_configured": configured, "max_lines": VR_MAX_LINES}


def returnable_stock(store_code: Any) -> dict:
    """The lots of one store that may go back to a vendor now."""
    from services import pharmacy_store_service as store_rules

    today = stock.site_today()
    with db_session() as conn:
        store = _vr_store(conn, store_code)
        lots = store_rules.returnable_lots(conn, int(store["id"]), today=today)
    return {"store": {"id": store["id"], "code": store["code"],
                      "name": store["name"] or store["code"],
                      "pharmacy_kind": store["pharmacy_kind"]},
            "today": today, "near_expiry_days": store_rules.near_expiry_days(), "lots": lots}


def _vr_require_ndps(actor: Mapping, controlled: bool) -> None:
    from services import ndps_service

    if controlled and not ndps_service.may_ndps(actor.get("role")):
        raise PurchasingError(
            "this return carries narcotic or psychotropic stock, which needs the NDPS "
            "permission. Nothing was changed.", code="ndps_permission_needed")


def create_vendor_return(*, vendor_code: Any, store_code: Any, lines: Any, actor: Mapping,
                         reason: Any, remarks: Any = None, idempotency_key: Any = None,
                         audit_ctx: Optional[Mapping] = None) -> dict:
    """Draft a return (the write tier, plus the NDPS permission for a narcotic
    or psychotropic line). Nothing moves until it is posted. A request key
    already used for the same return answers with it, replayed True; the same
    key for a different return is refused. Returns
    {"vendor_return", "warnings", "replayed"}."""
    actor_id = _actor_id(actor)
    clean = _vr_clean_lines(lines)
    why = _vr_text(reason, "the reason the stock goes back", required=True)
    note = _vr_text(remarks, "remarks", required=False)
    key = _vr_text(idempotency_key, "request key", required=False)
    today = stock.site_today()
    with db_session(immediate=True) as conn:
        store = _vr_store(conn, store_code)
        if key:
            prior = conn.execute("SELECT * FROM pharmacy_vendor_returns WHERE idempotency_key = ?",
                                 (key,)).fetchone()
            if prior is not None:
                asked = {r["lot_id"]: int(r["quantity_units"]) for r in conn.execute(
                    "SELECT lot_id, quantity_units FROM pharmacy_vendor_return_lines "
                    "WHERE pharmacy_vendor_return_id = ?", (prior["id"],)).fetchall()}
                vendor_row = conn.execute("SELECT code FROM vendors WHERE id = ?",
                                          (prior["vendor_id"],)).fetchone()
                same = (prior["store_id"] == store["id"]
                        and str(prior["created_by"]) == actor_id
                        and vendor_row is not None
                        and vendor_row["code"] == str(vendor_code or "").strip().upper()
                        and asked == {r["lot_id"]: r["quantity_units"] for r in clean})
                if not same:
                    raise PurchasingError("this request key was already used for a different "
                                          "return. Nothing was changed.",
                                          code="idempotency_conflict")
                return {"vendor_return": _vr_decorate(conn, prior), "warnings": [],
                        "replayed": True}
        any_ndps_guess = any(
            (conn.execute("SELECT m.ndps_category FROM helix_inventory_lots l JOIN "
                          "helix_medication_master m ON m.id = l.medication_id WHERE l.id = ?",
                          (r["lot_id"],)).fetchone() or {"ndps_category": None})["ndps_category"]
            in stock.NDPS_CONTROLLED for r in clean)
        vendor, warnings = _vr_vendor(conn, vendor_code, needs_ndps=any_ndps_guess, today=today)
        checked, any_ndps, line_warnings = _vr_check_lines(conn, clean, store=store,
                                                           vendor=vendor, today=today)
        _vr_require_ndps(actor, any_ndps)
        warnings += line_warnings
        receipts = set()
        for line in checked:
            ref = line["lot"].get("receipt_line_ref")
            got = conn.execute("SELECT goods_receipt_id FROM goods_receipt_lines WHERE id = ?",
                               (ref,)).fetchone() if ref is not None else None
            receipts.add(got["goods_receipt_id"] if got else None)
        source_receipt = next(iter(receipts)) if len(receipts) == 1 else None
        total = 0
        rid = conn.execute(
            "INSERT INTO pharmacy_vendor_returns (vendor_id, store_id, source_receipt_ref, "
            "status, reason, total_paise, created_by, idempotency_key, remarks) "
            "VALUES (?, ?, ?, 'draft', ?, 0, ?, ?, ?)",
            (vendor["id"], store["id"], source_receipt, why, actor_id, key, note)).lastrowid
        for line in checked:
            lot = line["lot"]
            cost = lot.get("unit_cost_paise")
            value = int(cost) * line["quantity_units"] if cost is not None else None
            total += value or 0
            conn.execute(
                "INSERT INTO pharmacy_vendor_return_lines (pharmacy_vendor_return_id, line_no, "
                "lot_id, helix_medication_id, batch_no, expiry_date, quantity_units, "
                "unit_cost_paise, value_paise, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rid, line["line_no"], lot["id"], lot["medication_id"], lot["lot_number"],
                 str(lot["expiration_date"])[:10] if lot["expiration_date"] else None,
                 line["quantity_units"], cost, value, line["reason"] or line["why_label"]))
        conn.execute("UPDATE pharmacy_vendor_returns SET total_paise = ? WHERE id = ?",
                     (total, rid))
        _vr_audit(conn, actor, "pharmacy_vendor_return_created",
                  summary=f"Return to vendor drafted: {len(checked)} lots to {vendor['name']}",
                  return_id=rid,
                  metadata={"vendor_return_id": str(rid), "vendor_id": str(vendor["id"]),
                            "store_id": str(store["id"]), "controlled": any_ndps,
                            "reason": why, "total_paise": total,
                            "lines": [{"line_no": c["line_no"], "lot_id": c["lot_id"],
                                       "drug_id": c["lot"]["medication_id"],
                                       "units": c["quantity_units"], "why": c["why"]}
                                      for c in checked]},
                  audit_ctx=audit_ctx)
        doc = _vr_decorate(conn, _vr_load(conn, rid))
    return {"vendor_return": doc, "warnings": warnings, "replayed": False}


def post_vendor_return(return_id: Any, *, actor: Mapping, witness_id: Any = None,
                       witness_pin: Any = None, audit_ctx: Optional[Mapping] = None) -> dict:
    """Send a draft back (the approve tier, never the person who drafted it):
    one vendor_return movement per line, all or nothing, with the 'security'
    audit row in the same immediate transaction. A held lot left empty is
    closed as returned to the vendor. A controlled return needs a witness
    confirmed by PIN, neither the drafter nor the poster. A return already
    posted answers replayed True and posts nothing. The PHVR number is drawn
    after the commit."""
    from services import ndps_service

    actor_id = _actor_id(actor)
    with db_session() as conn:
        row = _vr_load(conn, return_id)
        if row["status"] in ("posted", "credited"):
            return {"vendor_return": _vr_decorate(conn, row), "warnings": [], "replayed": True}
        if row["status"] != "draft":
            raise PurchasingError(f"a {VR_STATUS_LABELS.get(row['status'], row['status']).lower()} "
                                  "return is not sent", code="illegal_transition")
        if str(row["created_by"] or "") == actor_id:
            raise PurchasingError("you drafted this return, so somebody else with approval "
                                  "rights sends it. Nothing was changed.", code="same_person")
        controlled = _vr_decorate(conn, row)["controlled"]
    witness = None
    if controlled:
        if isinstance(witness_id, str) and witness_id.strip() == str(row["created_by"] or ""):
            raise PurchasingError("the witness must be somebody other than the person who "
                                  "drafted this return", code="witness_same_person")
        try:
            witness = ndps_service.verify_witness(
                actor=actor, witness_id=witness_id, witness_pin=witness_pin,
                purpose="a return of narcotic or psychotropic stock to the vendor",
                target_type="pharmacy_vendor_return", target_id=row["id"],
                audit_ctx=audit_ctx)
        except ndps_service.NdpsError as exc:
            raise PurchasingError(str(exc), code=exc.code) from exc
    today = stock.site_today()
    with db_session(immediate=True) as conn:
        row = _vr_load(conn, return_id)
        rid = int(row["id"])
        if row["status"] in ("posted", "credited"):
            return {"vendor_return": _vr_decorate(conn, row), "warnings": [], "replayed": True}
        if row["status"] != "draft":
            raise PurchasingError("this return changed while you were working on it. Nothing "
                                  "was changed.", code="illegal_transition")
        store = conn.execute("SELECT * FROM stores WHERE id = ?", (row["store_id"],)).fetchone()
        if store is None:
            raise PurchasingError("the store on this return no longer exists",
                                  code="store_not_found")
        store = _vr_store(conn, store["code"])
        lines = [dict(r) for r in conn.execute(
            "SELECT * FROM pharmacy_vendor_return_lines WHERE pharmacy_vendor_return_id = ? "
            "ORDER BY line_no", (rid,)).fetchall()]
        if not lines:
            raise PurchasingError("this return has no lines", code="empty_document")
        vendor_code = conn.execute("SELECT code FROM vendors WHERE id = ?",
                                   (row["vendor_id"],)).fetchone()
        controlled_now = any(
            (conn.execute("SELECT ndps_category FROM helix_medication_master WHERE id = ?",
                          (line["helix_medication_id"],)).fetchone() or
             {"ndps_category": None})["ndps_category"] in stock.NDPS_CONTROLLED
            for line in lines)
        vendor, warnings = _vr_vendor(conn, vendor_code["code"] if vendor_code else None,
                                      needs_ndps=controlled_now, today=today)
        checked, any_ndps, line_warnings = _vr_check_lines(
            conn, [{"lot_id": line["lot_id"], "quantity_units": int(line["quantity_units"]),
                    "reason": line["reason"]} for line in lines],
            store=store, vendor=vendor, today=today)
        if any_ndps and witness is None:
            raise PurchasingError("this return now carries narcotic or psychotropic stock, so "
                                  "a witness is needed. Nothing was changed.",
                                  code="witness_required")
        warnings += line_warnings
        moved: list[dict] = []
        document = f"{VR_REFERENCE}:{rid}"
        with ndps_service.entry_context(witness_id=witness, counterparty_type="vendor",
                                        counterparty_ref=str(vendor["id"]),
                                        document_ref=document):
            for line, check in zip(lines, checked):
                lot = check["lot"]
                try:
                    movement = stock.post_movement(
                        conn, lot_id=lot["id"], movement_type="vendor_return",
                        quantity_change=-int(line["quantity_units"]), actor_id=actor_id,
                        reference_type=VR_REFERENCE, reference_id=str(rid),
                        idempotency_key=f"{VR_REFERENCE}:{rid}:line:{line['line_no']}",
                        remarks=f"Returned to {vendor['name']}",
                        unit_cost_paise=lot.get("unit_cost_paise"))
                except stock.InsufficientStock as exc:
                    raise PurchasingError(f"line {line['line_no']}: {exc}",
                                          code="exceeds_lot",
                                          detail={"line_no": line["line_no"]}) from exc
                except stock.StockError as exc:
                    raise PurchasingError(f"line {line['line_no']}: {exc}", code=exc.code,
                                          detail={"line_no": line["line_no"]}) from exc
                closed = False
                left = int(conn.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?",
                                        (lot["id"],)).fetchone()[0] or 0)
                if left == 0 and lot["status"] in ("quarantined", "recalled", "nsq",
                                                   "pending_inspection"):
                    try:
                        stock.set_lot_status(
                            conn, lot["id"], "returned_to_vendor", actor_id=actor_id,
                            reason=f"Returned to {vendor['name']} on return {rid}",
                            reference_type=VR_REFERENCE, reference_id=str(rid), today=today)
                    except stock.StockError as exc:
                        raise PurchasingError(f"line {line['line_no']}: {exc}", code=exc.code,
                                              detail={"line_no": line["line_no"]}) from exc
                    closed = True
                moved.append({"line_no": line["line_no"], "lot_id": lot["id"],
                              "drug_id": lot["medication_id"],
                              "units": int(line["quantity_units"]),
                              "movement_id": movement["id"], "lot_closed": closed})
        cur = conn.execute(
            "UPDATE pharmacy_vendor_returns SET status = 'posted', posted_by = ?, "
            "posted_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'draft'", (actor_id, rid))
        if cur.rowcount != 1:
            raise PurchasingError("this return changed while you were working on it. Nothing "
                                  "was changed.", code="illegal_transition")
        _vr_audit(conn, actor, "pharmacy_vendor_return_posted",
                  summary=(f"Return to vendor {rid} sent: "
                           f"{sum(m['units'] for m in moved)} units to {vendor['name']}"),
                  return_id=rid,
                  metadata={"vendor_return_id": str(rid), "vendor_id": str(vendor["id"]),
                            "store_id": str(store["id"]), "controlled": any_ndps,
                            "witness_id": witness, "created_by": str(row["created_by"]),
                            "lines": moved, "total_paise": row["total_paise"]},
                  audit_ctx=audit_ctx)
    if procurement._stamp_document_number(VR_NUMBER_KIND, rid) is None:
        logger.error("return to vendor %s was posted but not numbered; stamp it again", rid)
    with db_session() as conn:
        doc = _vr_decorate(conn, _vr_load(conn, rid))
    return {"vendor_return": doc, "warnings": warnings, "replayed": False}


def record_vendor_credit(return_id: Any, *, credit_note_ref: Any, actor: Mapping,
                         audit_ctx: Optional[Mapping] = None) -> dict:
    """Record the vendor's credit note number against a posted return."""
    _actor_id(actor)
    ref = _vr_text(credit_note_ref, "the vendor's credit note number", required=True)
    with db_session(immediate=True) as conn:
        row = _vr_load(conn, return_id)
        rid = int(row["id"])
        if row["status"] == "credited":
            if (row["vendor_credit_note_ref"] or "") == ref:
                return {"vendor_return": _vr_decorate(conn, row), "warnings": [],
                        "replayed": True}
            raise PurchasingError(f"credit note {row['vendor_credit_note_ref']} is already "
                                  "recorded for this return", code="already_credited")
        if row["status"] != "posted":
            raise PurchasingError("a credit note is recorded only for a return that was sent",
                                  code="illegal_transition")
        conn.execute("UPDATE pharmacy_vendor_returns SET status = 'credited', "
                     "vendor_credit_note_ref = ?, credited_at = CURRENT_TIMESTAMP "
                     "WHERE id = ? AND status = 'posted'", (ref, rid))
        _vr_audit(conn, actor, "pharmacy_vendor_return_credited",
                  summary=f"Vendor credit note {ref} recorded for return {rid}",
                  return_id=rid,
                  metadata={"vendor_return_id": str(rid), "credit_note_ref": ref},
                  audit_ctx=audit_ctx)
        doc = _vr_decorate(conn, _vr_load(conn, rid))
    return {"vendor_return": doc, "warnings": [], "replayed": False}


def cancel_vendor_return(return_id: Any, *, reason: Any, actor: Mapping,
                         audit_ctx: Optional[Mapping] = None) -> dict:
    """Give up a draft, with a reason (the write tier). Nothing has moved."""
    actor_id = _actor_id(actor)
    why = _vr_text(reason, "the reason for cancelling", required=True)
    with db_session(immediate=True) as conn:
        row = _vr_load(conn, return_id)
        rid = int(row["id"])
        if row["status"] != "draft":
            raise PurchasingError("only a draft return is cancelled. A return that was sent "
                                  "has left the hospital.", code="illegal_transition")
        _vr_require_ndps(actor, _vr_decorate(conn, row, with_lines=False)["controlled"])
        cur = conn.execute(
            "UPDATE pharmacy_vendor_returns SET status = 'cancelled', cancelled_by = ?, "
            "cancelled_at = CURRENT_TIMESTAMP, cancel_reason = ? WHERE id = ? "
            "AND status = 'draft'", (actor_id, why, rid))
        if cur.rowcount != 1:
            raise PurchasingError("this return changed while you were working on it. Nothing "
                                  "was changed.", code="illegal_transition")
        _vr_audit(conn, actor, "pharmacy_vendor_return_cancelled",
                  summary=f"Return to vendor {rid} cancelled before it was sent",
                  return_id=rid, metadata={"vendor_return_id": str(rid), "reason": why},
                  audit_ctx=audit_ctx)
        doc = _vr_decorate(conn, _vr_load(conn, rid))
    return {"vendor_return": doc, "warnings": [], "replayed": False}
