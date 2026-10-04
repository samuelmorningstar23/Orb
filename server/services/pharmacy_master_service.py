"""Orb, pharmacy masters: the drug master's commercial and regulatory fields,
drug classification, pharmacy stores, par levels and vendors (grn-5).

Design: docs/his_design/grn.json, sections D and F, increment grn-5. Reached
only through routes/pharmacy_store/masters.py, which sits behind
ORB_PHARMACY_STORE (default off), so nothing here runs on a site that has not
turned the pharmacy store on.

WHAT THIS MAY CHANGE ON A DRUG, AND WHAT IT MAY NOT
  * Commercial and stock fields (pack size, purchase and dispense units, HSN
    code, GST rate, manufacturer, storage condition, reorder defaults, the
    active flag): the write tier, at once, audited.
  * Clinical identity (name, generic name, form, strength): NEVER here. Those
    drive allergy and interaction screening, and their governance is ruling
    GRN-RS1 (grn-6). A request to change them is refused with 422.
  * Classification (schedule, NDPS category, the essential narcotic mark): a
    change that ADDS control applies at once at the write tier. A change that
    REMOVES control is a pharmacy_master_change_requests row that a different
    person holding the approve tier must approve (GRN-R20 (b)), and it is
    refused while the drug has stock in an NDPS vault. A first classification
    of an unclassified drug applies at once: unclassified is never read as
    'none', so it carries no control to remove.

STORES. Pharmacy stores are created and edited at the approve tier through
procurement's store core (one store master, partitioned by desk). Creating the
dispensing store, or bringing it back into use, adopts every legacy lot in the
same transaction (pharmacy_stock_service.adopt_legacy_lots). A store that
still holds stock is never deactivated: its lots would silently stop being
dispensable. Ward and crash-cart stores are refused while
ORB_PHARMACY_WARD_STOCK is off (ruling GRN-RS5), and a department is always
canonicalised by admission_service.resolve_admission_department.

EVERY WRITE runs in one db_session(immediate=True) and writes its 'security'
audit row with db=conn in that transaction (fail-closed): the change and its
record commit together or not at all. Audit target ids are strings.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Mapping, Optional

from database import db_session
from db_backend import DBIntegrityError
from services import pharmacy_stock_service as stock
from services import procurement_service as procurement

# ═════════════════════════════════════════════════════════════════════════════
#  Errors and labels
# ═════════════════════════════════════════════════════════════════════════════


class MasterError(ValueError):
    """A masters write the caller must correct. `code` maps to HTTP in the route."""

    code = "master_error"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code:
            self.code = code


NOT_CLASSIFIED = "Not classified"
NOT_CONFIGURED = "Not configured"

SCHEDULE_LABELS = {"none": "No schedule", "G": "Schedule G", "H": "Schedule H",
                   "H1": "Schedule H1", "X": "Schedule X"}
NDPS_LABELS = {"none": "Not controlled", "narcotic": "Narcotic",
               "psychotropic": "Psychotropic"}
STORAGE_LABELS = {"room_temperature": "Room temperature", "cool": "Cool place",
                  "refrigerated": "Refrigerated, 2 to 8 °C", "frozen": "Frozen"}
STORE_KIND_LABELS = {"dispensing": "Dispensing pharmacy", "sub_store": "Sub-store",
                     "ndps_vault": "NDPS vault", "returns_quarantine": "Returns quarantine",
                     "ward_imprest": "Ward stock", "crash_cart": "Crash cart"}

#: A pharmacy store's row in the shared store master needs a store_type from
#: procurement's list; the pharmacy kind is what matters, so it is derived.
_STORE_TYPE_FOR_KIND = {"dispensing": "central", "sub_store": "sub_store",
                        "ndps_vault": "sub_store", "returns_quarantine": "sub_store",
                        "ward_imprest": "ward", "crash_cart": "ward"}

IDENTITY_FIELDS = ("name", "generic_name", "form", "strength")
CLASSIFICATION_FIELDS = ("drug_schedule", "ndps_category", "is_essential_narcotic")
GOVERNED_FIELDS = ("identity_status", "id")

#: Order of control: a move down this list REMOVES control.
_SCHEDULE_RANK = {"none": 0, "G": 1, "H": 2, "H1": 3, "X": 4}

_HSN = re.compile(r"^\d{4,8}$")
_MAX_GST_BP = 4000  # 40%, the highest GST slab


def _text_field(limit: int):
    def check(value, name):
        if value is None:
            return None
        if not isinstance(value, str):
            raise MasterError(f"{name} must be text", code="bad_value")
        text = value.strip()
        if len(text) > limit:
            raise MasterError(f"{name} is longer than {limit} characters", code="bad_value")
        return text or None
    return check


def _int_field(minimum: int, maximum: Optional[int] = None, *, nullable: bool = True):
    def check(value, name):
        if value is None:
            if nullable:
                return None
            raise MasterError(f"{name} cannot be blank", code="bad_value")
        if isinstance(value, bool) or not isinstance(value, int):
            raise MasterError(f"{name} must be a whole number", code="bad_value")
        if value < minimum or (maximum is not None and value > maximum):
            span = f"from {minimum} to {maximum}" if maximum is not None else f"{minimum} or more"
            raise MasterError(f"{name} must be {span}", code="bad_value")
        return value
    return check


def _hsn(value, name):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or not _HSN.match(value.strip()):
        raise MasterError("an HSN code is 4 to 8 digits", code="bad_value")
    return value.strip()


def _gst(value, name):
    checked = _int_field(0, _MAX_GST_BP)(value, name)
    return checked


def _storage(value, name):
    if value is None:
        return None
    if value not in stock.STORAGE_CONDITIONS:
        raise MasterError(f"storage condition must be one of {list(stock.STORAGE_CONDITIONS)}",
                          code="bad_value")
    return value


def _active(value, name):
    if not isinstance(value, bool):
        raise MasterError("active is yes or no", code="bad_value")
    return 1 if value else 0


#: The fields the write tier may edit, each with its validator.
EDITABLE_DRUG_FIELDS: dict[str, Any] = {
    "pack_size_units": _int_field(1, 100000),
    "purchase_uom": _text_field(40),
    "dispense_unit": _text_field(40),
    "hsn_code": _hsn,
    "gst_rate_bp": _gst,
    "manufacturer": _text_field(120),
    "storage_condition": _storage,
    "reorder_level_units": _int_field(0),
    "reorder_qty_units": _int_field(0),
    "lead_time_days": _int_field(0, 365),
    "is_active": _active,
}


def _decorate_drug(row: Mapping) -> dict:
    drug = dict(row)
    drug["schedule_label"] = SCHEDULE_LABELS.get(drug.get("drug_schedule"), NOT_CLASSIFIED) \
        if drug.get("drug_schedule") is not None else NOT_CLASSIFIED
    drug["ndps_label"] = NDPS_LABELS.get(drug.get("ndps_category"), NOT_CLASSIFIED) \
        if drug.get("ndps_category") is not None else NOT_CLASSIFIED
    drug["storage_label"] = STORAGE_LABELS.get(drug.get("storage_condition"), NOT_CONFIGURED) \
        if drug.get("storage_condition") is not None else NOT_CONFIGURED
    bp = drug.get("gst_rate_bp")
    drug["gst_label"] = NOT_CONFIGURED if bp is None else f"{bp / 100:g}%"
    return drug


def options() -> dict:
    """The choices a Masters screen offers, with their labels."""
    return {
        "drug_schedules": [{"value": v, "label": SCHEDULE_LABELS[v]} for v in stock.DRUG_SCHEDULES],
        "ndps_categories": [{"value": v, "label": NDPS_LABELS[v]} for v in stock.NDPS_CATEGORIES],
        "storage_conditions": [{"value": v, "label": STORAGE_LABELS[v]}
                               for v in stock.STORAGE_CONDITIONS],
        "store_kinds": [{"value": v, "label": STORE_KIND_LABELS[v]}
                        for v in procurement.PHARMACY_STORE_KINDS],
        "not_classified": NOT_CLASSIFIED,
        "not_configured": NOT_CONFIGURED,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Audit
# ═════════════════════════════════════════════════════════════════════════════

def _audit(conn, actor: Mapping, action: str, *, summary: str, target_type: str,
           target_id: Any, metadata: Optional[dict] = None,
           audit_ctx: Optional[Mapping] = None) -> None:
    """One 'security' row in the caller's transaction (fail-closed). A drug
    master is hospital configuration, not a patient record: no patient id."""
    import uuid

    from services.audit_service import log_audit_event

    ctx = audit_ctx or {}
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type=action,
        outcome="success",
        db=conn,
        summary_message=summary,
        actor_user_id=str(actor.get("id")) if actor.get("id") is not None else None,
        actor_role=actor.get("role"),
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        request_path=ctx.get("request_path"),
        http_method=ctx.get("http_method"),
        ip_address=ctx.get("ip_address"),
        metadata=metadata or {},
    )


def _actor_id(actor: Mapping) -> str:
    uid = actor.get("id")
    if not uid:
        raise MasterError("the change has no signed-in person behind it", code="no_actor")
    return str(uid)


# ═════════════════════════════════════════════════════════════════════════════
#  Drugs
# ═════════════════════════════════════════════════════════════════════════════

def _drug_row(conn, drug_id: str):
    return conn.execute("SELECT * FROM helix_medication_master WHERE id = ?",
                        (drug_id,)).fetchone()


def list_drugs(*, search: Optional[str] = None, include_inactive: bool = True,
               limit: int = 500) -> list[dict]:
    sql = ("SELECT m.*, (SELECT COUNT(*) FROM pharmacy_master_change_requests r "
           "WHERE r.helix_medication_id = m.id AND r.decision = 'pending') AS pending_requests "
           "FROM helix_medication_master m WHERE 1 = 1")
    params: list[Any] = []
    if not include_inactive:
        sql += " AND m.is_active = 1"
    if search and search.strip():
        sql += " AND (m.name LIKE ? OR m.generic_name LIKE ? OR m.id LIKE ?)"
        params.extend([f"%{search.strip()}%"] * 3)
    sql += " ORDER BY m.name LIMIT ?"
    params.append(max(1, min(int(limit), 2000)))
    with db_session() as conn:
        return [_decorate_drug(r) for r in conn.execute(sql, params).fetchall()]


def get_drug(drug_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT m.*, (SELECT COUNT(*) FROM pharmacy_master_change_requests r "
            "WHERE r.helix_medication_id = m.id AND r.decision = 'pending') AS pending_requests "
            "FROM helix_medication_master m WHERE m.id = ?", (drug_id,)).fetchone()
    return _decorate_drug(row) if row else None


def _refuse_locked_fields(changes: Mapping, *, allowed: tuple[str, ...]) -> None:
    identity = [f for f in IDENTITY_FIELDS if f in changes]
    if identity:
        raise MasterError(
            "a drug's name, generic name, form and strength cannot be changed here. They "
            "decide how allergies and interactions are screened, and changing them is "
            "governed separately.", code="identity_locked")
    governed = [f for f in GOVERNED_FIELDS if f in changes]
    if governed:
        raise MasterError("a drug's identity status is governed separately",
                          code="identity_locked")
    unknown = sorted(set(changes) - set(allowed))
    if unknown:
        raise MasterError(f"these fields cannot be changed here: {', '.join(unknown)}",
                          code="unknown_field")


def update_drug_fields(drug_id: str, changes: Mapping, *, actor: Mapping,
                       audit_ctx: Optional[Mapping] = None) -> dict:
    """Edit a drug's commercial and stock fields (the write tier). Only the
    fields named in `changes` are touched; a null clears an optional field."""
    if not isinstance(changes, Mapping) or not changes:
        raise MasterError("name at least one field to change", code="bad_value")
    if any(f in changes for f in CLASSIFICATION_FIELDS):
        raise MasterError("a drug's schedule and NDPS class are changed through its "
                          "classification, which keeps a second-person check on any "
                          "change that removes control", code="unknown_field")
    _refuse_locked_fields(changes, allowed=tuple(EDITABLE_DRUG_FIELDS))
    clean = {field: EDITABLE_DRUG_FIELDS[field](value, field.replace("_", " "))
             for field, value in changes.items()}
    actor_id = _actor_id(actor)
    with db_session(immediate=True) as conn:
        row = _drug_row(conn, drug_id)
        if row is None:
            raise MasterError(f"no drug {drug_id}", code="not_found")
        diff = {f: {"from": row[f], "to": v} for f, v in clean.items() if row[f] != v}
        if diff:
            sets = ", ".join(f"{f} = ?" for f in diff)
            conn.execute(
                f"UPDATE helix_medication_master SET {sets}, updated_by = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*[clean[f] for f in diff], actor_id, drug_id))
            _audit(conn, actor, "pharmacy_drug_master_updated",
                   summary=f"Drug master fields updated: {', '.join(sorted(diff))}",
                   target_type="helix_medication", target_id=drug_id,
                   metadata={"changes": diff}, audit_ctx=audit_ctx)
    return get_drug(drug_id)


def _vault_units(conn, drug_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(l.quantity), 0) FROM helix_inventory_lots l "
        "JOIN stores s ON s.id = l.store_id "
        "WHERE l.medication_id = ? AND s.pharmacy_kind = 'ndps_vault' AND l.quantity > 0",
        (drug_id,)).fetchone()
    return int(row[0] or 0)


def _direction(field: str, current: Any, wanted: Any) -> str:
    """'same', 'initial' (first classification), 'upgrade' (adds control) or
    'downgrade' (removes control)."""
    if field == "is_essential_narcotic":
        # The essential narcotic mark eases licensing (NDPS Amendment Act
        # 2014), so SETTING it removes control and clearing it adds control.
        current, wanted = int(bool(current)), int(bool(wanted))
        if current == wanted:
            return "same"
        return "downgrade" if wanted == 1 else "upgrade"
    if current == wanted:
        return "same"
    if current is None:
        return "initial"
    if field == "drug_schedule":
        return "upgrade" if _SCHEDULE_RANK[wanted] > _SCHEDULE_RANK[current] else "downgrade"
    # ndps_category: narcotic and psychotropic are different controls, not a
    # ladder, so leaving either for anything else is treated as removing one.
    if current == "none" and wanted in stock.NDPS_CONTROLLED:
        return "upgrade"
    return "downgrade"


def _check_classification_value(field: str, value: Any) -> Any:
    if field == "drug_schedule":
        if value not in stock.DRUG_SCHEDULES:
            raise MasterError(f"schedule must be one of {list(stock.DRUG_SCHEDULES)}",
                              code="bad_value")
        return value
    if field == "ndps_category":
        if value not in stock.NDPS_CATEGORIES:
            raise MasterError(f"NDPS category must be one of {list(stock.NDPS_CATEGORIES)}",
                              code="bad_value")
        return value
    if not isinstance(value, bool):
        raise MasterError("the essential narcotic mark is yes or no", code="bad_value")
    return 1 if value else 0


def classify_drug(drug_id: str, changes: Mapping, *, actor: Mapping,
                  reason: Optional[str] = None,
                  audit_ctx: Optional[Mapping] = None) -> dict:
    """Set a drug's schedule, NDPS category or essential narcotic mark.

    Each field is judged on its own. An initial classification or a change
    that adds control is applied now. A change that removes control becomes a
    pending request for a different approve-tier person, needs a reason, and
    is refused while the drug has stock in an NDPS vault. Everything is
    checked before anything is written: one refused field writes nothing.
    Returns {"drug", "applied": [fields], "requested": [request rows]}."""
    if not isinstance(changes, Mapping) or not changes:
        raise MasterError("name at least one classification to change", code="bad_value")
    _refuse_locked_fields(changes, allowed=CLASSIFICATION_FIELDS)
    wanted = {f: _check_classification_value(f, v) for f, v in changes.items()}
    why = (reason or "").strip() or None
    actor_id = _actor_id(actor)
    with db_session(immediate=True) as conn:
        row = _drug_row(conn, drug_id)
        if row is None:
            raise MasterError(f"no drug {drug_id}", code="not_found")
        plan = {f: _direction(f, row[f], v) for f, v in wanted.items()}
        downgrades = [f for f, d in plan.items() if d == "downgrade"]
        applies = {f: wanted[f] for f, d in plan.items() if d in ("initial", "upgrade")}

        # The state the drug would be in once the immediate changes apply, and
        # once every requested change were approved too: the essential
        # narcotic mark belongs to a narcotic in both.
        after = {f: row[f] for f in CLASSIFICATION_FIELDS} | applies
        final = {f: row[f] for f in CLASSIFICATION_FIELDS} | wanted
        if any(int(state["is_essential_narcotic"] or 0) == 1
               and state["ndps_category"] != "narcotic" for state in (after, final)):
            raise MasterError("only a drug classified as a narcotic can be marked an essential "
                              "narcotic", code="bad_value")
        if downgrades:
            if not why:
                raise MasterError("give a reason for a change that removes control. Another "
                                  "person must approve it.", code="missing_reason")
            held = _vault_units(conn, drug_id)
            if held:
                raise MasterError(
                    f"this drug has {held} unit{'' if held == 1 else 's'} in an NDPS vault. Its "
                    "control cannot be reduced while vault stock exists. Nothing was changed.",
                    code="vault_stock")

        if applies:
            sets = ", ".join(f"{f} = ?" for f in applies)
            conn.execute(
                f"UPDATE helix_medication_master SET {sets}, updated_by = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*applies.values(), actor_id, drug_id))
            _audit(conn, actor, "pharmacy_classification_changed",
                   summary=f"Drug classification set: {', '.join(sorted(applies))}",
                   target_type="helix_medication", target_id=drug_id,
                   metadata={"changes": {f: {"from": row[f], "to": v, "kind": plan[f]}
                                         for f, v in applies.items()}},
                   audit_ctx=audit_ctx)

        requested = []
        for field in downgrades:
            try:
                request_id = conn.execute(
                    """INSERT INTO pharmacy_master_change_requests (helix_medication_id,
                           change_kind, field, from_value, to_value, reason, requested_by)
                       VALUES (?, 'classification_downgrade', ?, ?, ?, ?, ?)""",
                    (drug_id, field, _as_text(row[field]), _as_text(wanted[field]), why,
                     actor_id)).lastrowid
            except DBIntegrityError as exc:
                raise MasterError(f"a change to this drug's {field.replace('_', ' ')} is already "
                                  "waiting for approval. Nothing was changed.",
                                  code="request_pending") from exc
            requested.append(dict(conn.execute(
                "SELECT * FROM pharmacy_master_change_requests WHERE id = ?",
                (request_id,)).fetchone()))
            _audit(conn, actor, "pharmacy_classification_change_requested",
                   summary=f"Classification change requested for approval: {field}",
                   target_type="pharmacy_master_change_request", target_id=request_id,
                   metadata={"drug_id": str(drug_id), "field": field,
                             "from": _as_text(row[field]), "to": _as_text(wanted[field])},
                   audit_ctx=audit_ctx)
    return {"drug": get_drug(drug_id), "applied": sorted(applies), "requested": requested}


def _as_text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _from_text(field: str, value: Optional[str]) -> Any:
    if field == "is_essential_narcotic":
        return int(value or 0)
    return value


def list_change_requests(*, decision: Optional[str] = "pending",
                         drug_id: Optional[str] = None) -> list[dict]:
    sql = ("SELECT r.*, m.name AS drug_name, ru.name AS requested_by_name, "
           "du.name AS decided_by_name FROM pharmacy_master_change_requests r "
           "LEFT JOIN helix_medication_master m ON m.id = r.helix_medication_id "
           "LEFT JOIN users ru ON ru.id = r.requested_by "
           "LEFT JOIN users du ON du.id = r.decided_by WHERE 1 = 1")
    params: list[Any] = []
    if decision:
        if decision not in ("pending", "approved", "refused", "withdrawn"):
            raise MasterError("unknown decision filter", code="bad_value")
        sql += " AND r.decision = ?"
        params.append(decision)
    if drug_id:
        sql += " AND r.helix_medication_id = ?"
        params.append(drug_id)
    sql += " ORDER BY r.id DESC LIMIT 500"
    with db_session() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def decide_change_request(request_id: int, *, approve: bool, actor: Mapping,
                          reason: Optional[str] = None,
                          audit_ctx: Optional[Mapping] = None) -> dict:
    """Approve or refuse a pending downgrade. The decider is never the
    requester (403). An approval re-checks that the drug still holds the value
    the request was raised against (else 409, still pending) and that no NDPS
    vault stock has arrived since (409), then applies the change."""
    actor_id = _actor_id(actor)
    why = (reason or "").strip() or None
    if not approve and not why:
        raise MasterError("give a reason for refusing", code="missing_reason")
    with db_session(immediate=True) as conn:
        req = conn.execute("SELECT * FROM pharmacy_master_change_requests WHERE id = ?",
                           (request_id,)).fetchone()
        if req is None:
            raise MasterError(f"no change request {request_id}", code="not_found")
        if req["decision"] != "pending":
            raise MasterError(f"this request was already {req['decision']}", code="decided")
        if req["requested_by"] == actor_id:
            raise MasterError("the person who asked for this change cannot also decide it. "
                              "Another approver must.", code="same_person")
        field = req["field"]
        if approve:
            drug = _drug_row(conn, req["helix_medication_id"])
            if drug is None:
                raise MasterError("the drug this request was for no longer exists",
                                  code="not_found")
            if _as_text(drug[field]) != req["from_value"]:
                raise MasterError("the drug's classification has changed since this request "
                                  "was raised. Refuse it and raise a new one.",
                                  code="request_stale")
            held = _vault_units(conn, drug["id"])
            if held:
                raise MasterError(
                    f"this drug now has {held} unit{'' if held == 1 else 's'} in an NDPS vault. "
                    "Its control cannot be reduced while vault stock exists.",
                    code="vault_stock")
            new_value = _from_text(field, req["to_value"])
            after = {f: drug[f] for f in CLASSIFICATION_FIELDS} | {field: new_value}
            if int(after["is_essential_narcotic"] or 0) == 1 and \
                    after["ndps_category"] != "narcotic":
                raise MasterError("clear the essential narcotic mark before this drug stops "
                                  "being classified as a narcotic", code="bad_value")
            conn.execute(
                f"UPDATE helix_medication_master SET {field} = ?, updated_by = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_value, actor_id, drug["id"]))
        conn.execute(
            "UPDATE pharmacy_master_change_requests SET decision = ?, decided_by = ?, "
            "decided_at = CURRENT_TIMESTAMP, decision_reason = ? WHERE id = ? "
            "AND decision = 'pending'",
            ("approved" if approve else "refused", actor_id, why, request_id))
        _audit(conn, actor, "pharmacy_classification_change_decided",
               summary=(f"Classification change {'approved' if approve else 'refused'}: "
                        f"{field}"),
               target_type="pharmacy_master_change_request", target_id=request_id,
               metadata={"drug_id": str(req["helix_medication_id"]), "field": field,
                         "from": req["from_value"], "to": req["to_value"],
                         "decision": "approved" if approve else "refused",
                         "requested_by": str(req["requested_by"])},
               audit_ctx=audit_ctx)
        out = dict(conn.execute("SELECT * FROM pharmacy_master_change_requests WHERE id = ?",
                                (request_id,)).fetchone())
    return out


def withdraw_change_request(request_id: int, *, actor: Mapping,
                            audit_ctx: Optional[Mapping] = None) -> dict:
    """The requester takes back a pending request. Nobody else may."""
    actor_id = _actor_id(actor)
    with db_session(immediate=True) as conn:
        req = conn.execute("SELECT * FROM pharmacy_master_change_requests WHERE id = ?",
                           (request_id,)).fetchone()
        if req is None:
            raise MasterError(f"no change request {request_id}", code="not_found")
        if req["decision"] != "pending":
            raise MasterError(f"this request was already {req['decision']}", code="decided")
        if req["requested_by"] != actor_id:
            raise MasterError("only the person who raised a request can withdraw it",
                              code="not_requester")
        conn.execute(
            "UPDATE pharmacy_master_change_requests SET decision = 'withdrawn', decided_by = ?, "
            "decided_at = CURRENT_TIMESTAMP WHERE id = ? AND decision = 'pending'",
            (actor_id, request_id))
        _audit(conn, actor, "pharmacy_classification_change_decided",
               summary=f"Classification change withdrawn: {req['field']}",
               target_type="pharmacy_master_change_request", target_id=request_id,
               metadata={"drug_id": str(req["helix_medication_id"]), "field": req["field"],
                         "decision": "withdrawn"}, audit_ctx=audit_ctx)
        out = dict(conn.execute("SELECT * FROM pharmacy_master_change_requests WHERE id = ?",
                                (request_id,)).fetchone())
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Stores
# ═════════════════════════════════════════════════════════════════════════════

def _department(value: Optional[str], *, required: bool) -> Optional[str]:
    """The canonical department, or None. Resolved BEFORE the caller's
    immediate session opens, because the resolver reads the staff list in a
    session of its own."""
    from services.admission_service import AdmissionInputError, resolve_admission_department

    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        if required:
            raise MasterError("a ward store needs the department it serves",
                              code="department_required")
        return None
    try:
        return resolve_admission_department(text)
    except AdmissionInputError as exc:
        raise MasterError(f"{text!r} is not a department on this site's list",
                          code="department_unknown") from exc


def _store_stock_units(conn, store_id: int) -> int:
    row = conn.execute("SELECT COALESCE(SUM(quantity), 0) FROM helix_inventory_lots "
                       "WHERE store_id = ? AND quantity > 0", (store_id,)).fetchone()
    return int(row[0] or 0)


def _decorate_store(row: Mapping, units: Optional[int] = None) -> dict:
    out = dict(row)
    out["kind_label"] = STORE_KIND_LABELS.get(out.get("pharmacy_kind"), out.get("pharmacy_kind"))
    if units is not None:
        out["units_on_hand"] = units
    return out


def list_stores(*, include_inactive: bool = True) -> list[dict]:
    sql = ("SELECT s.*, COALESCE((SELECT SUM(l.quantity) FROM helix_inventory_lots l "
           "WHERE l.store_id = s.id AND l.quantity > 0), 0) AS units_on_hand "
           "FROM stores s WHERE s.pharmacy_kind IS NOT NULL")
    if not include_inactive:
        sql += " AND s.is_active = 1"
    sql += " ORDER BY s.is_active DESC, s.name"
    with db_session() as conn:
        return [_decorate_store(r) for r in conn.execute(sql).fetchall()]


def legacy_lot_count() -> int:
    """Lots not yet in any store: what creating the dispensing store adopts.
    An emptied duplicate already merged into its batch is history, not stock
    waiting for a store, so it is not counted."""
    with db_session() as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM helix_inventory_lots l "
            f"WHERE {stock.awaiting_adoption_sql('l')}").fetchone()[0])


def _check_incharge(conn, user_id: Optional[str]) -> Optional[str]:
    if not user_id:
        return None
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise MasterError("the person in charge must be a user on this site",
                          code="bad_value")
    return user_id


def _procurement_error(exc: procurement.ProcurementError) -> MasterError:
    return MasterError(str(exc), code=exc.code)


def _audit_adoption(conn, actor, store_id, adoption, audit_ctx):
    _audit(conn, actor, "pharmacy_legacy_lots_adopted",
           summary=(f"Dispensing store adopted {len(adoption['adopted'])} legacy lots and merged "
                    f"{len(adoption['merged'])} duplicates"),
           target_type="pharmacy_store", target_id=store_id,
           metadata={"adopted": len(adoption["adopted"]),
                     "merged": adoption["merged"][:50],
                     "conflicts": len(adoption["conflicts"]),
                     "conflict_lots": [lot for c in adoption["conflicts"][:20]
                                       for lot in c["lot_ids"]]},
           audit_ctx=audit_ctx)


def create_store(*, code: str, name: str, pharmacy_kind: str, actor: Mapping,
                 department: Optional[str] = None, ward: Optional[str] = None,
                 incharge_user_id: Optional[str] = None,
                 audit_ctx: Optional[Mapping] = None) -> dict:
    """Create a pharmacy store (the approve tier). Creating the dispensing
    store adopts every legacy lot in the same transaction. Returns
    {"store", "adoption": dict or None}."""
    kind = (pharmacy_kind or "").strip().lower()
    if kind not in procurement.PHARMACY_STORE_KINDS:
        raise MasterError(f"a pharmacy store is one of {list(procurement.PHARMACY_STORE_KINDS)}",
                          code="bad_store_kind")
    ward_kind = kind in procurement.WARD_PHARMACY_STORE_KINDS
    import config
    if ward_kind and not config.feature_enabled("pharmacy_ward_stock"):
        raise MasterError("ward and crash-cart drug stores are switched off on this site. "
                          "Nothing was changed.", code="ward_stock_off")
    canonical = _department(department, required=ward_kind)
    actor_id = _actor_id(actor)
    with db_session(immediate=True) as conn:
        _check_incharge(conn, incharge_user_id)
        try:
            row = procurement._upsert_store_core(
                conn, code=code, name=name, store_type=_STORE_TYPE_FOR_KIND[kind],
                department=canonical, ward=(ward or "").strip() or None,
                incharge_user_id=incharge_user_id or None, is_active=True, create_only=True,
                domain=procurement.PHARMACY_DOMAIN, pharmacy_kind=kind)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        _audit(conn, actor, "pharmacy_store_upserted",
               summary=f"Pharmacy store created: {row['name']} ({STORE_KIND_LABELS[kind]})",
               target_type="pharmacy_store", target_id=row["id"],
               metadata={"code": row["code"], "pharmacy_kind": kind, "created": True,
                         "department": canonical}, audit_ctx=audit_ctx)
        adoption = None
        if kind == stock.DISPENSING_STORE_KIND:
            adoption = stock.adopt_legacy_lots(conn, row["id"], actor_id=actor_id)
            _audit_adoption(conn, actor, row["id"], adoption, audit_ctx)
        store = _decorate_store(row, _store_stock_units(conn, row["id"]))
    return {"store": store, "adoption": adoption}


def update_store(code: str, *, actor: Mapping, name: Optional[str] = None,
                 department: Optional[str] = None, ward: Optional[str] = None,
                 incharge_user_id: Optional[str] = None, is_active: Optional[bool] = None,
                 audit_ctx: Optional[Mapping] = None) -> dict:
    """Edit a pharmacy store (the approve tier). Its kind never changes. A
    store holding stock is never deactivated (409). Bringing the dispensing
    store back into use adopts any legacy lots that appeared meanwhile."""
    if is_active is not None and not isinstance(is_active, bool):
        raise MasterError("active is yes or no", code="bad_value")
    code_key = (code or "").strip().upper()
    with db_session() as conn:
        current = conn.execute("SELECT * FROM stores WHERE code = ? AND pharmacy_kind IS NOT NULL",
                               (code_key,)).fetchone()
    if current is None:
        raise MasterError(f"no pharmacy store {code}", code="not_found")
    kind = current["pharmacy_kind"]
    canonical = _department(department, required=False) if department else None
    actor_id = _actor_id(actor)
    with db_session(immediate=True) as conn:
        current = conn.execute("SELECT * FROM stores WHERE code = ? AND pharmacy_kind IS NOT NULL",
                               (code_key,)).fetchone()
        if current is None:
            raise MasterError(f"no pharmacy store {code}", code="not_found")
        _check_incharge(conn, incharge_user_id)
        active = bool(current["is_active"]) if is_active is None else is_active
        if current["is_active"] and not active:
            held = _store_stock_units(conn, current["id"])
            if held:
                raise MasterError(
                    f"this store still holds {held} unit{'' if held == 1 else 's'} of stock. Move "
                    "or write it off before the store is closed: its lots would stop being "
                    "dispensable. Nothing was changed.", code="store_holds_stock")
        try:
            row = procurement._upsert_store_core(
                conn, code=code_key, name=(name or "").strip() or current["name"],
                store_type=current["store_type"], department=canonical,
                ward=(ward or "").strip() or None, incharge_user_id=incharge_user_id or None,
                is_active=active, create_only=False, domain=procurement.PHARMACY_DOMAIN,
                pharmacy_kind=kind)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        changed = {f: {"from": current[f], "to": row[f]}
                   for f in ("name", "department", "ward", "incharge_user_id", "is_active")
                   if current[f] != row[f]}
        _audit(conn, actor, "pharmacy_store_upserted",
               summary=f"Pharmacy store updated: {row['name']}",
               target_type="pharmacy_store", target_id=row["id"],
               metadata={"code": row["code"], "pharmacy_kind": kind, "created": False,
                         "changes": changed}, audit_ctx=audit_ctx)
        adoption = None
        if kind == stock.DISPENSING_STORE_KIND and active and not current["is_active"]:
            adoption = stock.adopt_legacy_lots(conn, row["id"], actor_id=actor_id)
            _audit_adoption(conn, actor, row["id"], adoption, audit_ctx)
        store = _decorate_store(row, _store_stock_units(conn, row["id"]))
    return {"store": store, "adoption": adoption}


# ═════════════════════════════════════════════════════════════════════════════
#  Par levels
# ═════════════════════════════════════════════════════════════════════════════

def list_par_levels(*, store_id: Optional[int] = None) -> list[dict]:
    sql = ("SELECT p.*, s.code AS store_code, s.name AS store_name, m.name AS drug_name "
           "FROM pharmacy_par_levels p JOIN stores s ON s.id = p.store_id "
           "LEFT JOIN helix_medication_master m ON m.id = p.helix_medication_id WHERE 1 = 1")
    params: list[Any] = []
    if store_id is not None:
        sql += " AND p.store_id = ?"
        params.append(store_id)
    sql += " ORDER BY s.name, m.name"
    with db_session() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def upsert_par_level(*, store_id: int, helix_medication_id: str, par_units: int,
                     actor: Mapping, reorder_level_units: Optional[int] = None,
                     max_units: Optional[int] = None, is_active: bool = True,
                     audit_ctx: Optional[Mapping] = None) -> dict:
    """Set one drug's stock levels in one pharmacy store (the write tier)."""
    par = _int_field(0, nullable=False)(par_units, "par level")
    reorder = _int_field(0)(reorder_level_units, "reorder level")
    ceiling = _int_field(0)(max_units, "maximum")
    if ceiling is not None and ceiling < par:
        raise MasterError("the maximum cannot be below the par level", code="bad_value")
    if reorder is not None and ceiling is not None and reorder > ceiling:
        raise MasterError("the reorder level cannot be above the maximum", code="bad_value")
    if not isinstance(is_active, bool):
        raise MasterError("active is yes or no", code="bad_value")
    actor_id = _actor_id(actor)
    with db_session(immediate=True) as conn:
        store_row = conn.execute("SELECT id, name, pharmacy_kind FROM stores WHERE id = ?",
                                 (store_id,)).fetchone()
        if store_row is None or store_row["pharmacy_kind"] is None:
            raise MasterError(f"no pharmacy store {store_id}", code="not_found")
        if _drug_row(conn, helix_medication_id) is None:
            raise MasterError(f"no drug {helix_medication_id}", code="not_found")
        before = conn.execute("SELECT * FROM pharmacy_par_levels WHERE store_id = ? "
                              "AND helix_medication_id = ?",
                              (store_id, helix_medication_id)).fetchone()
        conn.execute(
            """INSERT INTO pharmacy_par_levels (store_id, helix_medication_id, par_units,
                   reorder_level_units, max_units, is_active, updated_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(store_id, helix_medication_id) DO UPDATE SET
                   par_units = excluded.par_units,
                   reorder_level_units = excluded.reorder_level_units,
                   max_units = excluded.max_units,
                   is_active = excluded.is_active,
                   updated_by = excluded.updated_by,
                   updated_at = CURRENT_TIMESTAMP""",
            (store_id, helix_medication_id, par, reorder, ceiling, 1 if is_active else 0,
             actor_id))
        row = dict(conn.execute("SELECT * FROM pharmacy_par_levels WHERE store_id = ? "
                                "AND helix_medication_id = ?",
                                (store_id, helix_medication_id)).fetchone())
        _audit(conn, actor, "pharmacy_par_level_upserted",
               summary=f"Par level set for {helix_medication_id} in {store_row['name']}",
               target_type="pharmacy_par_level", target_id=row["id"],
               metadata={"store_id": str(store_id), "drug_id": str(helix_medication_id),
                         "par_units": par, "reorder_level_units": reorder,
                         "max_units": ceiling, "is_active": bool(is_active),
                         "created": before is None}, audit_ctx=audit_ctx)
    return row


# ═════════════════════════════════════════════════════════════════════════════
#  Vendors and drug licences
# ═════════════════════════════════════════════════════════════════════════════

VENDOR_FIELDS = ("code", "name", "vendor_type", "gstin", "pan", "state_code", "address",
                 "contact_name", "contact_phone", "contact_email", "payment_terms_days",
                 "is_active", "notes", "drug_licence_numbers", "drug_licence_valid_to",
                 "ndps_licence_number")
LICENCE_FIELDS = ("drug_licence_numbers", "drug_licence_valid_to", "ndps_licence_number")


def licence_status(valid_to: Optional[str], *, today: Optional[str] = None) -> str:
    """'none', 'expired', 'expiring' (within PHARMACY_LICENCE_WARN_DAYS) or
    'valid', judged on the hospital's date."""
    if not valid_to:
        return "none"
    import config

    now = date.fromisoformat(today or stock.site_today())
    try:
        until = date.fromisoformat(str(valid_to)[:10])
    except ValueError:
        return "none"
    if until < now:
        return "expired"
    if until <= now + timedelta(days=int(config.PHARMACY_LICENCE_WARN_DAYS)):
        return "expiring"
    return "valid"


def _decorate_vendor(row: Mapping) -> dict:
    vendor = {k: row[k] for k in row.keys() if k not in ("bank_name", "account_last4", "ifsc")}
    vendor["licence_status"] = licence_status(vendor.get("drug_licence_valid_to"))
    return vendor


def list_vendors(*, search: Optional[str] = None, include_inactive: bool = True) -> list[dict]:
    sql = "SELECT * FROM vendors WHERE 1 = 1"
    params: list[Any] = []
    if not include_inactive:
        sql += " AND is_active = 1"
    if search and search.strip():
        sql += " AND (name LIKE ? OR code LIKE ?)"
        params.extend([f"%{search.strip()}%"] * 2)
    sql += " ORDER BY name LIMIT 1000"
    with db_session() as conn:
        return [_decorate_vendor(r) for r in conn.execute(sql, params).fetchall()]


def _vendor_kwargs(fields: Mapping) -> dict:
    unknown = sorted(set(fields) - set(VENDOR_FIELDS))
    if unknown:
        raise MasterError(f"these vendor fields are not kept here: {', '.join(unknown)}",
                          code="unknown_field")
    kwargs = {k: v for k, v in fields.items() if k in VENDOR_FIELDS}
    terms = kwargs.get("payment_terms_days")
    if terms is not None:
        kwargs["payment_terms_days"] = _int_field(0, 365)(terms, "payment terms")
    if "is_active" in kwargs and not isinstance(kwargs["is_active"], bool):
        raise MasterError("active is yes or no", code="bad_value")
    for key, value in kwargs.items():
        if key not in ("payment_terms_days", "is_active") and value is not None \
                and not isinstance(value, str):
            raise MasterError(f"{key.replace('_', ' ')} must be text", code="bad_value")
    return kwargs


def create_vendor(fields: Mapping, *, actor: Mapping,
                  audit_ctx: Optional[Mapping] = None) -> dict:
    """Add a vendor through the shared vendor core (the write tier). A code
    already in use is refused (409), never overwritten."""
    kwargs = _vendor_kwargs(fields)
    if not (kwargs.get("code") or "").strip() or not (kwargs.get("name") or "").strip():
        raise MasterError("a vendor needs a code and a name", code="missing_field")
    actor_id = _actor_id(actor)
    with db_session(immediate=True) as conn:
        try:
            row = procurement._upsert_vendor_core(conn, **kwargs, created_by=actor_id,
                                                  create_only=True)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        _audit(conn, actor, "pharmacy_vendor_upserted",
               summary=f"Vendor added from pharmacy masters: {row['name']}",
               target_type="vendor", target_id=row["id"],
               metadata={"code": row["code"], "created": True,
                         "drug_licence_valid_to": row.get("drug_licence_valid_to")},
               audit_ctx=audit_ctx)
    return _decorate_vendor(row)


def update_vendor(code: str, fields: Mapping, *, actor: Mapping,
                  audit_ctx: Optional[Mapping] = None) -> dict:
    """Edit an existing vendor (the write tier). A blank optional field keeps
    what is on file, as the shared core always does."""
    kwargs = _vendor_kwargs(fields)
    code_key = (code or "").strip().upper()
    kwargs.pop("code", None)
    with db_session(immediate=True) as conn:
        current = conn.execute("SELECT * FROM vendors WHERE code = ?", (code_key,)).fetchone()
        if current is None:
            raise MasterError(f"no vendor {code}", code="not_found")
        kwargs.setdefault("name", current["name"])
        kwargs.setdefault("vendor_type", current["vendor_type"])
        kwargs.setdefault("is_active", bool(current["is_active"]))
        try:
            row = procurement._upsert_vendor_core(conn, code=code_key, **kwargs)
        except procurement.ProcurementError as exc:
            raise _procurement_error(exc) from exc
        changed = sorted(k for k in row if k in current.keys() and k != "updated_at"
                         and row[k] != current[k])
        _audit(conn, actor, "pharmacy_vendor_upserted",
               summary=f"Vendor updated from pharmacy masters: {row['name']}",
               target_type="vendor", target_id=row["id"],
               metadata={"code": row["code"], "created": False, "fields": changed},
               audit_ctx=audit_ctx)
    return _decorate_vendor(row)


def set_drug_licence(code: str, *, actor: Mapping, drug_licence_numbers: Optional[str] = None,
                     drug_licence_valid_to: Optional[str] = None,
                     ndps_licence_number: Optional[str] = None,
                     audit_ctx: Optional[Mapping] = None) -> dict:
    """Record a vendor's drug licence (the write tier)."""
    if not any(v not in (None, "") for v in (drug_licence_numbers, drug_licence_valid_to,
                                              ndps_licence_number)):
        raise MasterError("enter the licence number, its valid-to date or an NDPS licence",
                          code="bad_value")
    return update_vendor(code, {"drug_licence_numbers": drug_licence_numbers,
                                "drug_licence_valid_to": drug_licence_valid_to,
                                "ndps_licence_number": ndps_licence_number},
                         actor=actor, audit_ctx=audit_ctx)
