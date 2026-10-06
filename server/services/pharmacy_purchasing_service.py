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
"""
from __future__ import annotations

import logging
import uuid
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
