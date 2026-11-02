"""Offline journals and explicitly attributed doctor payout statements.

No account, rate, tax deduction or commercial share is invented. Journal
postings name their source documents; originals and reversals remain separate
opposing events. Export stores the exact mapped rows and digest in the encrypted
DB, so later account edits cannot alter a downloaded journal. Doctor earnings
require a named charge attribution and a matching effective rule. Approval is
by a second person and claims each source line once under an immediate lock.
Nothing here sends money, contacts an accounting package or gates care.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from typing import Any

from database import db_session
from services import facility_service
from services.hospital_time import to_hospital
from services.money import apply_basis_points
from services.reporting_finance_common import (
    WorkbenchError, audit, csv_text, digest, facility, identity, integer,
    module_on, packed, patients_sql, require_patient, scope, stamp, text, window,
)

MAX_SOURCES = 2000
MAX_LINES = 10000
ACCOUNT_ROLES = {
    "receivable": "Patient and payer receivables", "revenue": "Service revenue before discounts and tax",
    "discount": "Invoice discounts", "cgst": "CGST", "sgst": "SGST", "igst": "IGST",
    "rounding": "Invoice round off", "patient_advances": "Patient advances held",
    "refund_clearing": "Refunds without an invoice or deposit allocation",
    **{f"funds:{method}": f"Money account: {method}" for method in
       ("cash", "card", "upi", "netbanking", "cheque", "dd", "neft", "rtgs", "wallet", "insurance", "scheme")},
}
JOURNAL_COLUMNS = ["entry_key", "line_no", "occurred_at", "source_type", "source_id",
                   "source_number", "patient_id", "supplier_facility_id", "supplier_legal_name",
                   "supplier_gstin", "supplier_snapshot_at", "account_role", "account_code",
                   "account_name", "debit_paise", "credit_paise", "source_notice"]


def _actor(db, user: dict, *extra: str) -> dict:
    module_on("FINANCE_TOOLS_ENABLED", "finance tools")
    return identity(db, user, "finance_tools.read", "billing.read", "payments.read", *extra)


def _rows(db, sql: str, args: tuple | list, *, maximum: int = MAX_SOURCES) -> list[dict]:
    rows = db.execute(sql + " LIMIT ?", (*args, maximum + 1)).fetchall()
    if len(rows) > maximum:
        raise WorkbenchError("source_limit", "This period contains too many source records. Choose a shorter period.", 409)
    return [dict(row) for row in rows]


def _supplier(db, row: dict, fid: int) -> bool:
    recorded = row.get("supplier_facility_id")
    if recorded is not None:
        return int(recorded) == fid
    if facility_service.facility_count(db) == 1 and fid == 1:
        return True  # Migration 72's explicitly labelled legacy MAIN documents.
    raise WorkbenchError("unknown_supplier", "A source document has no supplier snapshot; resolve its provenance before exporting.", 409)


def _visible_sources(db, actor: dict, table: str, alias: str, date_sql: str,
                     fid: int, start: str, end: str) -> list[dict]:
    patient_sql, params = patients_sql(db, actor)
    # table/alias/date_sql are private authored constants, never request text.
    return _rows(db,
        f"SELECT {alias}.* FROM {table} {alias} WHERE {alias}.patient_id IN ({patient_sql}) "
        f"AND ({alias}.supplier_facility_id=? OR {alias}.supplier_facility_id IS NULL) "
        f"AND ({date_sql}) ORDER BY {alias}.id",
        (*params, fid, start, end))


def _doctor(db, actor: dict, doctor_id: Any, fid: int) -> dict:
    did = text(doctor_id, "doctor_id", maximum=100)
    row = db.execute("SELECT * FROM users WHERE id=? AND role IN ('doctor','clinician')", (did,)).fetchone()
    if row is None or (dict(row).get("is_active") is not None and not row["is_active"]):
        raise WorkbenchError("invalid_doctor", "Choose an active doctor account.")
    doctor = dict(row)
    dept, _ = scope(db, actor)
    own = facility_service.facility_scope_for(doctor, db=db)
    if (dept is not None and (doctor.get("department") or "").strip() != dept) or (own is not None and fid not in own):
        raise WorkbenchError("not_found", "Doctor not found in this scope.", 404)
    return doctor


def metadata(*, user: dict, facility_id: int = 1) -> dict:
    with db_session() as db:
        actor = _actor(db, user)
        fid = facility(db, actor, facility_id)
        dept, _ = scope(db, actor)
        doctor_scope, doctor_params = "", ()
        if dept is not None:
            doctor_scope, doctor_params = " AND TRIM(department)=?", (dept,)
        doctors = []
        # Scope before LIMIT: unrelated or inactive accounts must not crowd
        # the caller's active doctors out of this bounded picker.
        for row in db.execute(
            "SELECT id,name FROM users WHERE role IN ('doctor','clinician') AND COALESCE(is_active,1)=1"
            + doctor_scope + " ORDER BY name,id LIMIT 1000", doctor_params).fetchall():
            try:
                _doctor(db, actor, row["id"], fid)
            except WorkbenchError:
                continue
            doctors.append(dict(row))
        mappings = [dict(row) for row in db.execute("SELECT * FROM finance_account_mappings WHERE facility_id=? ORDER BY account_role", (fid,)).fetchall()]
        rule_scope, rule_params = "", (fid,)
        if dept is not None:
            rule_scope = " AND TRIM(u.department)=?"
            rule_params = (*rule_params, dept)
        # identity() keeps these local workflows behind the single-facility
        # fence. Apply the same active-doctor/department rule as _doctor before
        # LIMIT so a foreign department cannot disclose rates or crowd out the
        # caller's own rules.
        rules = [dict(row) for row in db.execute(
            "SELECT r.* FROM finance_payout_rules r JOIN users u ON u.id=r.doctor_id "
            "WHERE r.facility_id=? AND u.role IN ('doctor','clinician') AND COALESCE(u.is_active,1)=1"
            + rule_scope + " ORDER BY r.id DESC LIMIT 500", rule_params).fetchall()]
    return {"facility_id": fid, "account_roles": [{"id": key, "label": value} for key, value in ACCOUNT_ROLES.items()],
            "mappings": mappings, "doctors": doctors, "rules": rules,
            "limits": {"days": 366, "sources": MAX_SOURCES, "lines": MAX_LINES},
            "notes": ["No account mappings, doctor attributions or share rules are supplied by Orb.",
                      "Fixed rules mean a fixed amount per billed invoice line, not per unit.",
                      "Matching rules use service first, then category, then all services; equal-specificity overlap is refused.",
                      "A credit-only negative payout must be combined with later unclaimed earnings before a statement can be created.",
                      "Fully collected rules become eligible when the invoice is completely covered by live allocations, less recorded refunds.",
                      "Deductions are entered explicitly with a reason. Orb does not calculate TDS.",
                      "Recording a payout is an attestation; Orb never transmits funds."]}


def set_mapping(*, user: dict, facility_id: Any, account_role: Any, account_code: Any, account_name: Any) -> dict:
    role = text(account_role, "account_role", maximum=50)
    if role not in ACCOUNT_ROLES:
        raise WorkbenchError("invalid_account_role", "Choose a named journal account role.")
    code, name = text(account_code, "account_code", maximum=60), text(account_name, "account_name", maximum=100)
    with db_session(immediate=True) as db:
        actor = _actor(db, user, "finance_tools.write")
        fid = facility(db, actor, facility_id)
        db.execute("INSERT INTO finance_account_mappings(facility_id,account_role,account_code,account_name,updated_by,updated_at) "
                   "VALUES (?,?,?,?,?,?) ON CONFLICT(facility_id,account_role) DO UPDATE SET account_code=excluded.account_code,account_name=excluded.account_name,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                   (fid, role, code, name, actor["id"], stamp()))
        audit(db, actor, "finance_account_mapping_saved", target=f"{fid}:{role}", metadata={"facility_id": fid, "account_role": role})
    return {"facility_id": fid, "account_role": role, "account_code": code, "account_name": name}


def _post(rows: list[dict], *, key: str, source: dict, source_type: str, when: str,
          legs: list[tuple[str, int]], mappings: dict, issues: list[dict]) -> None:
    if sum(amount for _, amount in legs) != 0:
        raise WorkbenchError("unbalanced_source", "A source document does not balance. No journal was produced.", 409)
    line_number = 0
    for role, amount in legs:
        if not amount:
            continue
        mapping = mappings.get(role)
        if mapping is None:
            issues.append({"code": "account_not_mapped", "account_role": role, "entry_key": key})
        line_number += 1
        rows.append({"entry_key": key, "line_no": line_number,
                     "occurred_at": when, "source_type": source_type, "source_id": source["id"],
                     "source_number": source.get("invoice_number") or source.get("receipt_number"),
                     "patient_id": source["patient_id"], "supplier_facility_id": source.get("supplier_facility_id"),
                     "supplier_legal_name": source.get("supplier_legal_name"), "supplier_gstin": source.get("supplier_gstin"),
                     "supplier_snapshot_at": source.get("supplier_snapshot_at"),
                     "source_notice": None if source.get("supplier_snapshot_at") else "Legacy document: supplier identity was not snapshotted.",
                     "account_role": role, "account_code": mapping["account_code"] if mapping else None,
                     "account_name": mapping["account_name"] if mapping else None,
                     "debit_paise": max(amount, 0), "credit_paise": max(-amount, 0)})
    if len(rows) > MAX_LINES:
        raise WorkbenchError("source_limit", "The journal has too many lines. Choose a shorter period.", 409)


def _invoice_legs(db, source: dict) -> list[tuple[str, int]]:
    lines = _rows(db, "SELECT gross_paise,discount_paise,taxable_value_paise,tax_paise,total_paise FROM invoice_lines WHERE invoice_id=? ORDER BY id", (source["id"],), maximum=MAX_LINES)
    if not lines:
        raise WorkbenchError("missing_invoice_lines", "A numbered invoice has no source lines.", 409)
    for header, line_field in (("subtotal_paise", "gross_paise"), ("discount_paise", "discount_paise"),
                               ("taxable_value_paise", "taxable_value_paise"), ("tax_paise", "tax_paise"), ("total_paise", "total_paise")):
        if sum(integer(row[line_field], line_field, minimum=-9_000_000_000_000_000) for row in lines) != source[header]:
            raise WorkbenchError("source_totals_mismatch", "An invoice header differs from its recorded lines.", 409)
    values = {name: integer(source[name], name, minimum=-9_000_000_000_000_000) for name in
              ("payable_paise", "subtotal_paise", "discount_paise", "cgst_paise", "sgst_paise", "igst_paise", "rounding_adjustment_paise")}
    return [("receivable", values["payable_paise"]), ("revenue", -values["subtotal_paise"]),
            ("discount", values["discount_paise"]), ("cgst", -values["cgst_paise"]),
            ("sgst", -values["sgst_paise"]), ("igst", -values["igst_paise"]),
            ("rounding", -values["rounding_adjustment_paise"])]


def _payment_legs(db, source: dict, *, reversing: bool = False) -> list[tuple[str, int]]:
    amount = integer(source["amount_paise"], "receipt amount", minimum=1)
    if source.get("reversal_of_payment_id") is not None:
        if reversing:
            raise WorkbenchError("ambiguous_reversal", "A reversal points at another reversal.", 409)
        prior = db.execute("SELECT * FROM payments WHERE id=?", (source["reversal_of_payment_id"],)).fetchone()
        if prior is None:
            raise WorkbenchError("ambiguous_reversal", "A reversal has no original receipt.", 409)
        prior = dict(prior)
        if (prior["patient_id"] != source["patient_id"] or prior["amount_paise"] != amount or
            prior["direction"] != "in" or source["direction"] != "out" or prior["method"] != source["method"] or
            prior.get("supplier_facility_id") != source.get("supplier_facility_id")):
            raise WorkbenchError("ambiguous_reversal", "A reversal does not match its original receipt.", 409)
        return [(role, -value) for role, value in _payment_legs(db, prior, reversing=True)]
    if source["status"] not in {"received", "cleared", "reversed"}:
        raise WorkbenchError("unsupported_receipt_status", "A receipt needs explicit settlement or reversal evidence before journal export.", 409)
    if source["method"] == "adjustment":
        applied = db.execute("SELECT COALESCE(SUM(-amount_paise),0) FROM patient_deposits WHERE payment_id=? AND movement='apply'", (source["id"],)).fetchone()[0]
        if source["direction"] != "in" or applied != amount:
            raise WorkbenchError("ambiguous_adjustment", "An adjustment receipt has no matching deposit application.", 409)
        return [("patient_advances", amount), ("receivable", -amount)]
    funds = f"funds:{source['method']}"
    if funds not in ACCOUNT_ROLES:
        raise WorkbenchError("unsupported_payment_method", "A receipt uses an unsupported money account.", 409)
    if source["direction"] == "out":
        deposit = db.execute("SELECT COALESCE(SUM(-amount_paise),0) FROM patient_deposits WHERE payment_id=? AND movement='refund'", (source["id"],)).fetchone()[0]
        role = "receivable" if source.get("invoice_id") else "patient_advances" if deposit == amount else "refund_clearing"
        return [(role, amount), (funds, -amount)]
    allocated = db.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payment_allocations WHERE payment_id=?", (source["id"],)).fetchone()[0]
    allocated = integer(allocated, "receipt allocations")
    if allocated > amount:
        raise WorkbenchError("source_totals_mismatch", "A receipt is allocated for more than its amount.", 409)
    return [(funds, amount), ("receivable", -allocated), ("patient_advances", -(amount - allocated))]


def _journal(db, actor: dict, *, facility_id: Any, from_day: Any, to_day: Any) -> dict:
    fid = facility(db, actor, facility_id)
    first, last, start, end = window(from_day, to_day)
    mappings = {row["account_role"]: dict(row) for row in db.execute("SELECT * FROM finance_account_mappings WHERE facility_id=?", (fid,)).fetchall()}
    invoices = _visible_sources(db, actor, "invoices", "i",
        "(julianday(i.issued_at)>=julianday(?) AND julianday(i.issued_at)<julianday(?))", fid, start, end)
    cancelled = _visible_sources(db, actor, "invoices", "i",
        "(julianday(i.cancelled_at)>=julianday(?) AND julianday(i.cancelled_at)<julianday(?))", fid, start, end)
    payments = _visible_sources(db, actor, "payments", "r",
        "julianday(r.received_at)>=julianday(?) AND julianday(r.received_at)<julianday(?)", fid, start, end)
    rows, issues = [], []
    for source in invoices:
        if not source.get("invoice_number") or not _supplier(db, source, fid):
            continue
        _post(rows, key=f"invoice:{source['id']}", source=source, source_type="invoice", when=source["issued_at"], legs=_invoice_legs(db, source), mappings=mappings, issues=issues)
    for source in cancelled:
        if not source.get("invoice_number") or not source.get("issued_at") or not _supplier(db, source, fid):
            continue
        if source["status"] != "cancelled":
            raise WorkbenchError("ambiguous_cancellation", "A cancellation timestamp has no cancelled lifecycle.", 409)
        _post(rows, key=f"invoice-cancel:{source['id']}", source=source, source_type="invoice_cancellation", when=source["cancelled_at"], legs=[(role, -value) for role, value in _invoice_legs(db, source)], mappings=mappings, issues=issues)
    for source in payments:
        if not source.get("receipt_number"):
            raise WorkbenchError("unnumbered_receipt", "A receipt has no document number.", 409)
        if not _supplier(db, source, fid):
            continue
        _post(rows, key=f"payment:{source['id']}", source=source, source_type="receipt", when=source["received_at"], legs=_payment_legs(db, source), mappings=mappings, issues=issues)
    rows.sort(key=lambda row: (row["occurred_at"], row["entry_key"], row["line_no"]))
    debit, credit = sum(row["debit_paise"] for row in rows), sum(row["credit_paise"] for row in rows)
    integer(debit, "journal debit total")
    integer(credit, "journal credit total")
    if debit != credit:
        raise WorkbenchError("unbalanced_journal", "The journal does not balance.", 409)
    return {"facility_id": fid, "from_day": first, "to_day": last, "rows": rows, "issues": issues,
            "debit_paise": debit, "credit_paise": credit, "balanced": True,
            "mappings": mappings, "can_export": bool(rows) and not issues}


def journal_preview(*, user: dict, facility_id: Any, from_day: Any, to_day: Any) -> dict:
    with db_session(immediate=True) as db:
        actor = _actor(db, user)
        result = _journal(db, actor, facility_id=facility_id, from_day=from_day, to_day=to_day)
        audit(db, actor, "finance_journal_previewed", read=True, metadata={"facility_id": result["facility_id"], "rows": len(result["rows"])})
        return result


def _export_visible(db, actor: dict, row: dict) -> None:
    facility(db, actor, row["facility_id"])
    for pid in {item["patient_id"] for item in json.loads(row["rows_json"])}:
        require_patient(db, actor, pid)


def create_journal_export(*, user: dict, facility_id: Any, from_day: Any, to_day: Any, idempotency_key: Any) -> dict:
    key = text(idempotency_key, "idempotency_key", maximum=100)
    request_hash = digest({"facility_id": facility_id, "from_day": from_day, "to_day": to_day})
    with db_session(immediate=True) as db:
        actor = _actor(db, user, "finance_tools.export")
        facility(db, actor, facility_id)
        old = db.execute("SELECT * FROM finance_journal_exports WHERE created_by=? AND idempotency_key=?", (actor["id"], key)).fetchone()
        if old is not None:
            old = dict(old)
            _export_visible(db, actor, old)
            if old["request_hash"] != request_hash:
                raise WorkbenchError("idempotency_conflict", "This retry key belongs to another export request.", 409)
            audit(db, actor, "finance_journal_export_replayed", target=old["id"], read=True)
            return {"id": old["id"], "content_sha256": old["content_sha256"], "replayed": True}
        result = _journal(db, actor, facility_id=facility_id, from_day=from_day, to_day=to_day)
        if not result["can_export"]:
            raise WorkbenchError("journal_not_ready", "Map every used account and include at least one source before exporting.", 409)
        content = csv_text(JOURNAL_COLUMNS, result["rows"])
        sha, export_id = hashlib.sha256(content.encode()).hexdigest(), str(uuid.uuid4())
        db.execute("INSERT INTO finance_journal_exports(id,facility_id,from_day,to_day,rows_json,mappings_json,debit_paise,credit_paise,content_sha256,created_by,created_at,idempotency_key,request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (export_id, result["facility_id"], result["from_day"], result["to_day"], packed(result["rows"]), packed(result["mappings"]), result["debit_paise"], result["credit_paise"], sha, actor["id"], stamp(), key, request_hash))
        for pid in {row["patient_id"] for row in result["rows"]}:
            db.execute("INSERT INTO finance_journal_export_patients(export_id,patient_id,created_at) VALUES (?,?,?)", (export_id, pid, stamp()))
        audit(db, actor, "finance_journal_export_created", target=export_id, metadata={"content_sha256": sha, "rows": len(result["rows"]), "facility_id": result["facility_id"]})
        return {"id": export_id, "content_sha256": sha, "replayed": False}


def list_journal_exports(*, user: dict, facility_id: Any) -> list[dict]:
    """Scope frozen patient ownership before pagination, including absent child indexes."""
    with db_session(immediate=True) as db:
        actor = _actor(db, user)
        fid = facility(db, actor, facility_id)
        patient_sql, params = patients_sql(db, actor)
        rows = db.execute(
            "SELECT e.id,e.facility_id,e.from_day,e.to_day,e.debit_paise,e.credit_paise,"
            "e.content_sha256,e.created_by,e.created_at FROM finance_journal_exports e "
            "WHERE e.facility_id=? AND NOT EXISTS (SELECT 1 FROM json_each(e.rows_json) item "
            f"WHERE json_extract(item.value,'$.patient_id') NOT IN ({patient_sql}) "
            "OR json_extract(item.value,'$.patient_id') IS NULL) ORDER BY e.created_at DESC,e.id LIMIT 100",
            (fid, *params)).fetchall()
        audit(db, actor, "finance_journal_exports_viewed", read=True, metadata={"facility_id": fid, "count": len(rows)})
        return [dict(row) for row in rows]


def journal_csv(*, user: dict, export_id: str) -> str:
    with db_session(immediate=True) as db:
        actor = _actor(db, user, "finance_tools.export")
        row = db.execute("SELECT * FROM finance_journal_exports WHERE id=?", (export_id,)).fetchone()
        if row is None:
            raise WorkbenchError("not_found", "Journal export not found.", 404)
        row = dict(row)
        _export_visible(db, actor, row)
        content = csv_text(JOURNAL_COLUMNS, json.loads(row["rows_json"]))
        if hashlib.sha256(content.encode()).hexdigest() != row["content_sha256"]:
            raise WorkbenchError("snapshot_integrity", "The stored export failed its integrity check.", 409)
        audit(db, actor, "finance_journal_downloaded", target=export_id, read=True, metadata={"content_sha256": row["content_sha256"]})
        return content


def _charge(db, actor: dict, charge_id: Any, fid: int) -> dict:
    cid = integer(charge_id, "charge_id", minimum=1)
    row = db.execute("SELECT c.*,p.name AS patient_name FROM encounter_charges c JOIN patients p ON p.id=c.patient_id WHERE c.id=?", (cid,)).fetchone()
    if row is None:
        raise WorkbenchError("not_found", "Charge not found.", 404)
    charge = dict(row)
    require_patient(db, actor, charge["patient_id"])
    if charge.get("invoice_id"):
        source = db.execute("SELECT * FROM invoices WHERE id=? AND patient_id=?", (charge["invoice_id"], charge["patient_id"])).fetchone()
        if source is None or not _supplier(db, dict(source), fid):
            raise WorkbenchError("not_found", "Charge not found in this facility.", 404)
    elif charge.get("admission_id"):
        source = db.execute("SELECT facility_id FROM admissions WHERE id=? AND patient_id=?", (charge["admission_id"], charge["patient_id"])).fetchone()
        if source is None or source[0] != fid:
            raise WorkbenchError("not_found", "Charge not found in this facility.", 404)
    else:
        source = db.execute("SELECT registered_facility_id FROM patients WHERE id=?", (charge["patient_id"],)).fetchone()
        if source is None or source[0] != fid:
            raise WorkbenchError("not_found", "Charge not found in this facility.", 404)
    return charge


def charges(*, user: dict, facility_id: Any, from_day: Any, to_day: Any) -> dict:
    first, last, start, end = window(from_day, to_day)
    with db_session(immediate=True) as db:
        actor = _actor(db, user)
        fid = facility(db, actor, facility_id)
        patient_sql, params = patients_sql(db, actor)
        # Scope is applied before LIMIT; source facility is proven in _charge.
        rows = _rows(db, f"SELECT c.id FROM encounter_charges c WHERE c.patient_id IN ({patient_sql}) AND julianday(c.charged_at)>=julianday(?) AND julianday(c.charged_at)<julianday(?) ORDER BY c.id", (*params, start, end))
        visible = []
        for row in rows:
            try:
                charge = _charge(db, actor, row["id"], fid)
            except WorkbenchError as exc:
                if exc.code == "not_found":
                    continue
                raise
            attribution = db.execute("SELECT a.*,u.name AS doctor_name FROM finance_charge_attributions a LEFT JOIN users u ON u.id=a.doctor_id WHERE a.charge_id=?", (charge["id"],)).fetchone()
            visible.append({key: charge.get(key) for key in ("id", "patient_id", "patient_name", "service_code", "description", "status", "service_date", "gross_paise", "discount_paise", "total_paise", "invoice_id")}
                           | {"attribution": dict(attribution) if attribution else None})
        audit(db, actor, "finance_charge_attributions_viewed", read=True, metadata={"facility_id": fid, "count": len(visible)})
        return {"charges": visible, "from_day": first, "to_day": last, "facility_id": fid}


def attribute_charge(*, user: dict, facility_id: Any, charge_id: Any, doctor_id: Any,
                     reason: Any, version: Any = None) -> dict:
    why = text(reason, "reason", maximum=500)
    with db_session(immediate=True) as db:
        actor = _actor(db, user, "finance_tools.write")
        fid = facility(db, actor, facility_id)
        charge = _charge(db, actor, charge_id, fid)
        doctor = _doctor(db, actor, doctor_id, fid)
        old = db.execute("SELECT * FROM finance_charge_attributions WHERE charge_id=?", (charge["id"],)).fetchone()
        claimed = db.execute("SELECT 1 FROM finance_payout_claims pc JOIN invoice_lines il ON il.id=pc.source_line_id WHERE il.encounter_charge_id=? LIMIT 1", (charge["id"],)).fetchone()
        if claimed:
            raise WorkbenchError("attribution_locked", "An approved payout already uses this attribution. It cannot be reassigned.", 409)
        now = stamp()
        if old:
            if integer(version, "version", minimum=1) != old["version"]:
                raise WorkbenchError("stale_attribution", "This attribution changed. Reload before saving.", 409)
            db.execute("UPDATE finance_charge_attributions SET doctor_id=?,reason=?,version=version+1,updated_by=?,updated_at=? WHERE charge_id=?", (doctor["id"], why, actor["id"], now, charge["id"]))
        else:
            if version not in (None, 0):
                raise WorkbenchError("stale_attribution", "This charge has no attribution yet.", 409)
            db.execute("INSERT INTO finance_charge_attributions(charge_id,doctor_id,reason,created_by,created_at,updated_by,updated_at) VALUES (?,?,?,?,?,?,?)", (charge["id"], doctor["id"], why, actor["id"], now, actor["id"], now))
        audit(db, actor, "finance_charge_attributed", target=str(charge["id"]), metadata={"patient_id": charge["patient_id"], "doctor_id": doctor["id"], "previous_doctor_id": old["doctor_id"] if old else None})
        return dict(db.execute("SELECT * FROM finance_charge_attributions WHERE charge_id=?", (charge["id"],)).fetchone())


def create_rule(*, user: dict, facility_id: Any, doctor_id: Any, selector_type: Any,
                selector_value: Any, mode: Any, basis: Any, recognition: Any,
                effective_from: Any, effective_to: Any = None, percent_bp: Any = None,
                fixed_paise: Any = None) -> dict:
    if any(not isinstance(value, str) for value in (selector_type, mode, basis, recognition)) or selector_type not in {"service", "category", "all"} or mode not in {"percentage", "fixed"} or basis not in {"gross_ex_tax", "net_ex_tax"} or recognition not in {"billed", "fully_collected"}:
        raise WorkbenchError("invalid_rule", "Choose an explicit selector, amount method, basis and recognition rule.")
    value = text(selector_value, "selector_value", maximum=100, blank=selector_type == "all")
    if selector_type == "all" and value:
        raise WorkbenchError("invalid_rule", "An all-services rule has an empty selector value.")
    try:
        first = date.fromisoformat(text(effective_from, "effective_from", maximum=10)).isoformat()
        last = date.fromisoformat(text(effective_to, "effective_to", maximum=10)).isoformat() if effective_to is not None else None
    except ValueError as exc:
        raise WorkbenchError("invalid_rule", "Rule dates must use YYYY-MM-DD.") from exc
    if last is not None and first > last:
        raise WorkbenchError("invalid_rule", "The rule end must not precede its start.")
    if mode == "percentage":
        rate = integer(percent_bp, "percent_bp", maximum=10000)
        if fixed_paise is not None:
            raise WorkbenchError("invalid_rule", "A percentage rule cannot also have a fixed amount.")
        fixed = None
    else:
        fixed = integer(fixed_paise, "fixed_paise")
        if percent_bp is not None:
            raise WorkbenchError("invalid_rule", "A fixed rule cannot also have a percentage.")
        rate = None
    with db_session(immediate=True) as db:
        actor = _actor(db, user, "finance_tools.write")
        fid = facility(db, actor, facility_id)
        doctor = _doctor(db, actor, doctor_id, fid)
        if selector_type != "all":
            column = "code" if selector_type == "service" else "category"
            if db.execute(f"SELECT 1 FROM service_items WHERE {column}=? LIMIT 1", (value,)).fetchone() is None:
                raise WorkbenchError("invalid_rule", "The selector does not exist in the service master.")
        if db.execute("SELECT 1 FROM finance_payout_rules WHERE facility_id=? AND doctor_id=? AND selector_type=? AND selector_value=? AND is_active=1 AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?) LIMIT 1",
                      (fid, doctor["id"], selector_type, value, last or "9999-12-31", first)).fetchone():
            raise WorkbenchError("overlapping_rule", "An active rule already covers this doctor, selector and period.", 409)
        cur = db.execute("INSERT INTO finance_payout_rules(facility_id,doctor_id,selector_type,selector_value,mode,basis,recognition,percent_bp,fixed_paise,effective_from,effective_to,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (fid, doctor["id"], selector_type, value, mode, basis, recognition, rate, fixed, first, last, actor["id"], stamp()))
        rid = cur.lastrowid
        audit(db, actor, "finance_payout_rule_created", target=str(rid), metadata={"facility_id": fid, "doctor_id": doctor["id"]})
        return dict(db.execute("SELECT * FROM finance_payout_rules WHERE id=?", (rid,)).fetchone())


def retire_rule(*, user: dict, rule_id: Any) -> dict:
    rid = integer(rule_id, "rule_id", minimum=1)
    with db_session(immediate=True) as db:
        actor = _actor(db, user, "finance_tools.write")
        row = db.execute("SELECT * FROM finance_payout_rules WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise WorkbenchError("not_found", "Rule not found.", 404)
        facility(db, actor, row["facility_id"])
        _doctor(db, actor, row["doctor_id"], row["facility_id"])
        db.execute("UPDATE finance_payout_rules SET is_active=0 WHERE id=?", (rid,))
        audit(db, actor, "finance_payout_rule_retired", target=str(rid))
    return {"id": rid, "is_active": 0}


def _matching_rule(db, fid: int, doctor_id: str, service_code: str, day: str) -> dict | None:
    item = db.execute("SELECT category FROM service_items WHERE code=?", (service_code,)).fetchone()
    category = item[0] if item else None
    rules = [dict(row) for row in db.execute("SELECT * FROM finance_payout_rules WHERE facility_id=? AND doctor_id=? AND is_active=1 AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?) AND (selector_type='all' OR (selector_type='service' AND selector_value=?) OR (selector_type='category' AND selector_value=?))",
             (fid, doctor_id, day, day, service_code, category)).fetchall()]
    if not rules:
        return None
    specificity = {"service": 2, "category": 1, "all": 0}
    best = max(specificity[rule["selector_type"]] for rule in rules)
    selected = [rule for rule in rules if specificity[rule["selector_type"]] == best]
    if len(selected) != 1:
        raise WorkbenchError("ambiguous_rule", "More than one equally specific payout rule matches a source line.", 409)
    return selected[0]


def _fully_collected(db, source: dict) -> bool:
    # Receipt-level refunds do not record which invoice/advance part they
    # returned. An allocated receipt is no longer proof of full collection
    # after such a refund; do not invent a proportional allocation.
    ambiguous = db.execute("SELECT 1 FROM refunds r JOIN payment_allocations a ON a.payment_id=r.payment_id "
                           "WHERE a.invoice_id=? AND a.reversed_at IS NULL AND r.invoice_id IS NULL "
                           "AND r.status='paid' AND r.amount_paise>0 LIMIT 1", (source["invoice_id"],)).fetchone()
    if ambiguous:
        raise WorkbenchError("unallocated_receipt_refund", "A receipt-level refund has no invoice or advance allocation. Collection-based earnings require an unambiguous collection record.", 409)
    paid = db.execute("SELECT COALESCE(SUM(a.amount_paise),0) FROM payment_allocations a JOIN payments p ON p.id=a.payment_id WHERE a.invoice_id=? AND a.reversed_at IS NULL AND p.status IN ('received','cleared') AND p.direction='in'", (source["invoice_id"],)).fetchone()[0]
    refunds = db.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE invoice_id=? AND direction='out' AND purpose='refund' AND reversal_of_payment_id IS NULL AND status IN ('received','cleared')", (source["invoice_id"],)).fetchone()[0]
    return paid - refunds >= source["payable_paise"]


def _payout(db, actor: dict, *, facility_id: Any, from_day: Any, to_day: Any,
            doctor_id: Any = None) -> dict:
    fid = facility(db, actor, facility_id)
    first, last, start, end = window(from_day, to_day)
    if doctor_id is not None:
        doctor_id = _doctor(db, actor, doctor_id, fid)["id"]
    patient_sql, params = patients_sql(db, actor)
    sources = _rows(db,
        "SELECT il.*,i.patient_id,i.invoice_number,i.invoice_type,i.issued_at,i.status AS invoice_status,"
        "i.supplier_facility_id,i.supplier_legal_name,i.supplier_gstin,i.supplier_snapshot_at,"
        "i.payable_paise,i.supersedes_invoice_id FROM invoice_lines il JOIN invoices i ON i.id=il.invoice_id "
        f"WHERE i.patient_id IN ({patient_sql}) AND (i.supplier_facility_id=? OR i.supplier_facility_id IS NULL) "
        "AND i.status IN ('issued','paid','partially_paid') AND i.invoice_type IN ('interim','final','credit_note') "
        "AND julianday(i.issued_at)>=julianday(?) AND julianday(i.issued_at)<julianday(?) ORDER BY il.id",
        (*params, fid, start, end), maximum=MAX_LINES)
    lines, issues, excluded = [], [], []
    for source in sources:
        if not _supplier(db, source, fid):
            continue
        if not source.get("invoice_number"):
            issues.append({"code": "unnumbered_invoice", "source_line_id": source["id"]})
            continue
        if db.execute("SELECT 1 FROM finance_payout_claims WHERE source_line_id=?", (source["id"],)).fetchone():
            excluded.append({"code": "already_claimed", "source_line_id": source["id"]})
            continue
        if source["invoice_type"] == "credit_note":
            originals = db.execute("SELECT pl.* FROM finance_payout_lines pl JOIN finance_payout_claims pc ON pc.source_line_id=pl.source_line_id AND pc.doctor_id=pl.doctor_id AND pc.statement_id=pl.statement_id JOIN invoice_lines il ON il.id=pl.source_line_id WHERE il.invoice_id=? AND il.line_no=? AND il.encounter_charge_id IS ?", (source["supersedes_invoice_id"], source["line_no"], source["encounter_charge_id"])).fetchall()
            if not originals:
                excluded.append({"code": "credit_without_claimed_earning", "source_line_id": source["id"]})
                continue
            if len(originals) != 1:
                raise WorkbenchError("ambiguous_credit", "A credit line does not identify exactly one approved earning.", 409)
            prior = dict(originals[0])
            if doctor_id is not None and prior["doctor_id"] != doctor_id:
                continue
            previous = json.loads(prior["snapshot_json"])
            original = previous["invoice_line"]
            if any(source[key] != -original[key] for key in ("gross_paise", "discount_paise", "taxable_value_paise", "tax_paise", "total_paise")):
                raise WorkbenchError("ambiguous_credit", "The credit differs from the approved source line. A manual adjustment is required.", 409)
            lines.append({"source_line_id": source["id"], "patient_id": source["patient_id"], "doctor_id": prior["doctor_id"],
                          "attribution_version": prior["attribution_version"], "rule_id": prior["rule_id"], "source_kind": "credit",
                          "basis_paise": -prior["basis_paise"], "amount_paise": -prior["amount_paise"],
                          "snapshot": {"invoice_line": source, "rule": previous["rule"], "original_source_line_id": prior["source_line_id"]}})
            continue
        if db.execute("SELECT 1 FROM invoices WHERE supersedes_invoice_id=? AND invoice_type='credit_note' AND status IN ('issued','paid','partially_paid') LIMIT 1", (source["invoice_id"],)).fetchone():
            excluded.append({"code": "source_credited", "source_line_id": source["id"]})
            continue
        attribution = db.execute("SELECT * FROM finance_charge_attributions WHERE charge_id=?", (source["encounter_charge_id"],)).fetchone()
        if attribution is None:
            issues.append({"code": "doctor_not_attributed", "source_line_id": source["id"], "charge_id": source["encounter_charge_id"]})
            continue
        attribution = dict(attribution)
        if doctor_id is not None and attribution["doctor_id"] != doctor_id:
            continue
        when = to_hospital(source["issued_at"])
        if when is None:
            raise WorkbenchError("invalid_source_date", "A source invoice has an unreadable issue date.", 409)
        rule = _matching_rule(db, fid, attribution["doctor_id"], source["service_code"], when.date().isoformat())
        if rule is None:
            issues.append({"code": "payout_rule_not_configured", "source_line_id": source["id"], "doctor_id": attribution["doctor_id"]})
            continue
        if rule["recognition"] == "fully_collected":
            try:
                collected = _fully_collected(db, source)
            except WorkbenchError as exc:
                if exc.code != "unallocated_receipt_refund":
                    raise
                excluded.append({"code": exc.code, "source_line_id": source["id"]})
                continue
            if not collected:
                excluded.append({"code": "not_fully_collected", "source_line_id": source["id"]})
                continue
        gross = integer(source["gross_paise"], "line gross")
        discount = integer(source["discount_paise"], "line discount", maximum=gross)
        basis = gross if rule["basis"] == "gross_ex_tax" else gross - discount
        amount = apply_basis_points(basis, rule["percent_bp"]) if rule["mode"] == "percentage" else rule["fixed_paise"]
        lines.append({"source_line_id": source["id"], "patient_id": source["patient_id"], "doctor_id": attribution["doctor_id"],
                      "attribution_version": attribution["version"], "rule_id": rule["id"], "source_kind": "earning",
                      "basis_paise": basis, "amount_paise": amount,
                      "snapshot": {"invoice_line": source, "rule": rule, "attribution": attribution}})
    total = sum(line["amount_paise"] for line in lines)
    integer(total, "statement total", minimum=-9_000_000_000_000_000)
    if total < 0:
        issues.append({"code": "negative_payout_requires_offset"})
    return {"facility_id": fid, "from_day": first, "to_day": last, "doctor_id": doctor_id,
            "lines": lines, "issues": issues, "excluded": excluded, "total_paise": total,
            "preview_hash": digest(lines), "can_create": bool(lines) and not issues}


def payout_preview(*, user: dict, facility_id: Any, from_day: Any, to_day: Any, doctor_id: Any = None) -> dict:
    with db_session(immediate=True) as db:
        actor = _actor(db, user)
        out = _payout(db, actor, facility_id=facility_id, from_day=from_day, to_day=to_day, doctor_id=doctor_id)
        audit(db, actor, "finance_payout_previewed", read=True, metadata={"facility_id": out["facility_id"], "lines": len(out["lines"])})
        return out


def _statement(db, actor: dict, statement_id: str) -> dict:
    row = db.execute("SELECT * FROM finance_payout_statements WHERE id=?", (statement_id,)).fetchone()
    if row is None:
        raise WorkbenchError("not_found", "Payout statement not found.", 404)
    out = dict(row)
    facility(db, actor, out["facility_id"])
    lines = [dict(item) for item in db.execute("SELECT * FROM finance_payout_lines WHERE statement_id=? ORDER BY source_line_id,doctor_id", (statement_id,)).fetchall()]
    for pid in {line["patient_id"] for line in lines}:
        require_patient(db, actor, pid)
    out["lines"] = [{**{key: value for key, value in line.items() if key != "snapshot_json"}, "snapshot": json.loads(line["snapshot_json"])} for line in lines]
    return out


def create_statement(*, user: dict, facility_id: Any, from_day: Any, to_day: Any,
                     idempotency_key: Any, doctor_id: Any = None, deduction_paise: Any = 0,
                     deduction_reason: Any = None) -> dict:
    key = text(idempotency_key, "idempotency_key", maximum=100)
    deduction = integer(deduction_paise, "deduction_paise")
    reason = text(deduction_reason, "deduction_reason", maximum=500) if deduction else None
    request_hash = digest({"facility_id": facility_id, "from_day": from_day, "to_day": to_day,
                           "doctor_id": doctor_id, "deduction_paise": deduction, "deduction_reason": reason})
    with db_session(immediate=True) as db:
        actor = _actor(db, user, "finance_tools.write")
        facility(db, actor, facility_id)
        existing = db.execute("SELECT id,request_hash FROM finance_payout_statements WHERE created_by=? AND idempotency_key=?", (actor["id"], key)).fetchone()
        if existing:
            out = _statement(db, actor, existing["id"])
            if existing["request_hash"] != request_hash:
                raise WorkbenchError("idempotency_conflict", "This retry key belongs to another statement request.", 409)
            audit(db, actor, "finance_payout_statement_replayed", target=out["id"], read=True)
            return {**out, "replayed": True}
        result = _payout(db, actor, facility_id=facility_id, from_day=from_day, to_day=to_day, doctor_id=doctor_id)
        if not result["can_create"]:
            raise WorkbenchError("payout_not_ready", "Attribute and configure every eligible source line before creating a statement.", 409)
        if deduction and len({line["doctor_id"] for line in result["lines"]}) != 1:
            raise WorkbenchError("ambiguous_deduction", "Choose one doctor's earnings before applying a manual deduction. No allocation across doctors is inferred.", 409)
        if deduction > max(0, result["total_paise"]):
            raise WorkbenchError("invalid_deduction", "Deductions cannot exceed the positive statement total.")
        sid, now = str(uuid.uuid4()), stamp()
        db.execute("INSERT INTO finance_payout_statements(id,facility_id,from_day,to_day,doctor_id,status,total_paise,deduction_paise,net_paise,deduction_reason,preview_hash,created_by,created_at,idempotency_key,request_hash) VALUES (?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?)",
                   (sid, result["facility_id"], result["from_day"], result["to_day"], result["doctor_id"], result["total_paise"], deduction, result["total_paise"] - deduction, reason, result["preview_hash"], actor["id"], now, key, request_hash))
        for line in result["lines"]:
            db.execute("INSERT INTO finance_payout_lines(statement_id,source_line_id,patient_id,doctor_id,attribution_version,rule_id,source_kind,basis_paise,amount_paise,snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (sid, line["source_line_id"], line["patient_id"], line["doctor_id"], line["attribution_version"], line["rule_id"], line["source_kind"], line["basis_paise"], line["amount_paise"], packed(line["snapshot"]), now))
        audit(db, actor, "finance_payout_statement_created", target=sid, metadata={"total_paise": result["total_paise"], "lines": len(result["lines"]), "facility_id": result["facility_id"]})
        return {**_statement(db, actor, sid), "replayed": False}


def list_statements(*, user: dict, facility_id: Any) -> list[dict]:
    with db_session(immediate=True) as db:
        actor = _actor(db, user)
        fid = facility(db, actor, facility_id)
        patient_sql, params = patients_sql(db, actor)
        rows = db.execute("SELECT s.* FROM finance_payout_statements s WHERE s.facility_id=? AND NOT EXISTS "
                          f"(SELECT 1 FROM finance_payout_lines l WHERE l.statement_id=s.id AND l.patient_id NOT IN ({patient_sql})) ORDER BY s.created_at DESC LIMIT 100", (fid, *params)).fetchall()
        audit(db, actor, "finance_payout_statements_viewed", read=True, metadata={"facility_id": fid, "count": len(rows)})
        return [dict(row) for row in rows]


def get_statement(*, user: dict, statement_id: str) -> dict:
    with db_session(immediate=True) as db:
        actor = _actor(db, user)
        result = _statement(db, actor, statement_id)
        audit(db, actor, "finance_payout_statement_viewed", target=statement_id, read=True)
        return result


def approve_statement(*, user: dict, statement_id: str) -> dict:
    with db_session(immediate=True) as db:
        actor = _actor(db, user, "finance_tools.approve")
        current = _statement(db, actor, statement_id)
        if current["created_by"] == actor["id"]:
            raise WorkbenchError("second_person_required", "The statement creator cannot approve it.", 409)
        if current["status"] in {"approved", "paid"}:
            audit(db, actor, "finance_payout_approval_replayed", target=statement_id, read=True)
            return current
        if current["status"] != "draft":
            raise WorkbenchError("invalid_transition", "Only a draft statement can be approved.", 409)
        fresh = _payout(db, actor, facility_id=current["facility_id"], from_day=current["from_day"], to_day=current["to_day"], doctor_id=current["doctor_id"])
        if not fresh["can_create"] or fresh["preview_hash"] != current["preview_hash"]:
            raise WorkbenchError("statement_changed", "Source data, attribution or rules changed. Cancel the draft and create a fresh statement.", 409)
        now = stamp()
        for line in fresh["lines"]:
            db.execute("INSERT INTO finance_payout_claims(source_line_id,doctor_id,statement_id,claimed_at) VALUES (?,?,?,?)", (line["source_line_id"], line["doctor_id"], statement_id, now))
        db.execute("UPDATE finance_payout_statements SET status='approved',approved_by=?,approved_at=? WHERE id=?", (actor["id"], now, statement_id))
        audit(db, actor, "finance_payout_statement_approved", target=statement_id, metadata={"preview_hash": fresh["preview_hash"], "total_paise": current["total_paise"]})
        return _statement(db, actor, statement_id)


def mark_statement_paid(*, user: dict, statement_id: str, payment_reference: Any) -> dict:
    reference = text(payment_reference, "payment_reference", maximum=120)
    with db_session(immediate=True) as db:
        actor = _actor(db, user, "finance_tools.write", "finance_tools.approve")
        current = _statement(db, actor, statement_id)
        if current["status"] == "paid":
            if current["payment_reference"] != reference:
                raise WorkbenchError("payment_reference_conflict", "This statement already has another payment attestation.", 409)
            audit(db, actor, "finance_payout_payment_replayed", target=statement_id, read=True)
            return current
        if current["status"] != "approved" or current["net_paise"] < 0:
            raise WorkbenchError("invalid_transition", "Only an approved non-negative statement can be marked paid. Carry a credit adjustment into a later statement.", 409)
        db.execute("UPDATE finance_payout_statements SET status='paid',paid_by=?,paid_at=?,payment_reference=? WHERE id=?", (actor["id"], stamp(), reference, statement_id))
        audit(db, actor, "finance_payout_payment_attested", target=statement_id, metadata={"payment_reference": reference, "net_paise": current["net_paise"]})
        return _statement(db, actor, statement_id)


def cancel_statement(*, user: dict, statement_id: str) -> dict:
    with db_session(immediate=True) as db:
        actor = _actor(db, user, "finance_tools.write")
        current = _statement(db, actor, statement_id)
        if current["status"] != "draft":
            raise WorkbenchError("invalid_transition", "Only an unapproved draft can be cancelled.", 409)
        db.execute("UPDATE finance_payout_statements SET status='cancelled',cancelled_by=?,cancelled_at=? WHERE id=?", (actor["id"], stamp(), statement_id))
        audit(db, actor, "finance_payout_draft_cancelled", target=statement_id)
        return _statement(db, actor, statement_id)


def statement_csv(*, user: dict, statement_id: str) -> str:
    with db_session(immediate=True) as db:
        actor = _actor(db, user, "finance_tools.export")
        current = _statement(db, actor, statement_id)
        rows = [{"statement_id": statement_id, "status": current["status"],
                 "source_line_id": line["source_line_id"], "doctor_id": line["doctor_id"],
                 "source_kind": line["source_kind"], "basis_paise": line["basis_paise"],
                 "amount_paise": line["amount_paise"], "rule_id": line["rule_id"],
                 "invoice_number": line["snapshot"]["invoice_line"]["invoice_number"],
                 "attribution_version": line["attribution_version"],
                 "statement_total_paise": current["total_paise"], "statement_deduction_paise": current["deduction_paise"],
                 "statement_net_paise": current["net_paise"], "deduction_reason": current["deduction_reason"]} for line in current["lines"]]
        audit(db, actor, "finance_payout_statement_exported", target=statement_id, read=True)
        return csv_text(["statement_id", "status", "source_line_id", "invoice_number", "doctor_id", "source_kind", "basis_paise", "amount_paise", "rule_id", "attribution_version", "statement_total_paise", "statement_deduction_paise", "statement_net_paise", "deduction_reason"], rows)
