"""Offline report builder and cohorts over named, role-scoped datasets.

The request is a small query description, never SQL. Identifiers and joins come
only from this module's catalog, values are bound, and scope precedes grouping
and LIMIT. Saved definitions contain no results and always rerun under the
reader's current stored identity. Row previews disclose truncation; CSV refuses
truncated exports. No scoring, inference, ETL or external submission occurs.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from database import db_session
from permissions import PATIENT_DEPARTMENT_SQL, has_all_permissions
from services.reporting_finance_common import (
    WorkbenchError, audit, csv_text, digest, identity, integer, module_on,
    packed, patients_sql, scope, stamp, text, window,
)

# (SQL expression, value kind, can group). Every expression below is authored.
PATIENT_FIELDS = {
    "patient_id": ("p.id", "text", False),
    "patient_name": ("p.name", "text", False),
    "mrn": ("p.mrn", "text", False),
    "sex": ("p.sex", "text", True),
    "charted_age": ("p.age", "integer", False),
    "status": ("p.status", "text", True),
    "diagnosis": ("p.diagnosis", "text", False),
    "current_department": (PATIENT_DEPARTMENT_SQL, "text", True),
    "created_at": ("p.created_at", "text", False),
}

DATASETS = {
    "patients": {
        "label": "Patient cohort", "permissions": ("patient.read_all",),
        "from": "patients p", "patient": "p.id", "date": "p.created_at", "local_date": False,
        "fields": PATIENT_FIELDS, "default": ["patient_id", "patient_name", "sex", "charted_age", "status"],
        "measures": {"count": "COUNT(*)"},
    },
    "admissions": {
        "label": "Admissions", "permissions": ("patient.read_all",),
        "from": "admissions a JOIN patients p ON p.id = a.patient_id", "patient": "a.patient_id",
        "date": "COALESCE(a.admitted_at, a.created_at)", "local_date": False,
        "fields": {"admission_id": ("a.id", "text", False), "patient_id": ("a.patient_id", "text", False),
                   "patient_name": ("p.name", "text", False), "department": ("a.department", "text", True),
                   "ward": ("a.ward", "text", True), "status": ("a.status", "text", True),
                   "doctor_id": ("a.attending_doctor_id", "text", True),
                   "admitted_at": ("COALESCE(a.admitted_at, a.created_at)", "text", False),
                   "diagnosis": ("a.initial_diagnosis", "text", False)},
        "default": ["admission_id", "patient_name", "department", "status", "admitted_at"],
        "measures": {"count": "COUNT(*)"},
    },
    "opd_visits": {
        "label": "Outpatient visits", "permissions": ("registration.read",),
        "from": "opd_visits v JOIN patients p ON p.id = v.patient_id", "patient": "v.patient_id",
        "date": "v.visit_date", "local_date": True,
        "fields": {"visit_id": ("v.id", "text", False), "patient_id": ("v.patient_id", "text", False),
                   "patient_name": ("p.name", "text", False), "visit_date": ("v.visit_date", "text", True),
                   "department": ("v.department", "text", True), "doctor_id": ("v.doctor_id", "text", True),
                   "visit_type": ("v.visit_type", "text", True), "status": ("v.status", "text", True)},
        "default": ["visit_id", "patient_name", "visit_date", "department", "status"],
        "measures": {"count": "COUNT(*)"},
    },
    "invoices": {
        "label": "Invoices and credit notes", "permissions": ("billing.read",),
        "from": "invoices i JOIN patients p ON p.id = i.patient_id", "patient": "i.patient_id",
        "date": "COALESCE(i.issued_at, i.created_at)", "local_date": False, "supplier": "i.supplier_facility_id",
        "fields": {"invoice_id": ("i.id", "integer", False), "invoice_number": ("i.invoice_number", "text", False),
                   "patient_id": ("i.patient_id", "text", False), "patient_name": ("p.name", "text", False),
                   "invoice_type": ("i.invoice_type", "text", True), "status": ("i.status", "text", True),
                   "encounter_type": ("i.encounter_type", "text", True),
                   "supplier_facility_id": ("i.supplier_facility_id", "integer", True),
                   "current_patient_department": (PATIENT_DEPARTMENT_SQL, "text", True),
                   "issued_at": ("i.issued_at", "text", False),
                   "payable_paise": ("i.payable_paise", "integer", False),
                   "tax_paise": ("i.tax_paise", "integer", False),
                   "balance_paise": ("i.balance_paise", "integer", False)},
        "default": ["invoice_number", "invoice_type", "status", "payable_paise", "balance_paise"],
        "measures": {"count": "COUNT(*)", "payable_paise": "COALESCE(SUM(i.payable_paise),0)",
                     "tax_paise": "COALESCE(SUM(i.tax_paise),0)", "balance_paise": "COALESCE(SUM(i.balance_paise),0)"},
    },
    "payments": {
        "label": "Receipts and money out", "permissions": ("payments.read",),
        "from": "payments r JOIN patients p ON p.id = r.patient_id", "patient": "r.patient_id",
        "date": "r.received_at", "local_date": False, "supplier": "r.supplier_facility_id",
        "fields": {"payment_id": ("r.id", "integer", False), "receipt_number": ("r.receipt_number", "text", False),
                   "patient_id": ("r.patient_id", "text", False), "patient_name": ("p.name", "text", False),
                   "direction": ("r.direction", "text", True), "method": ("r.method", "text", True),
                   "purpose": ("r.purpose", "text", True), "status": ("r.status", "text", True),
                   "supplier_facility_id": ("r.supplier_facility_id", "integer", True),
                   "received_at": ("r.received_at", "text", False),
                   "amount_paise": ("r.amount_paise", "integer", False)},
        "default": ["receipt_number", "direction", "method", "purpose", "amount_paise"],
        "measures": {"count": "COUNT(*)", "amount_paise": "COALESCE(SUM(r.amount_paise),0)",
                     "net_paise": "COALESCE(SUM(CASE WHEN r.direction='in' THEN r.amount_paise ELSE -r.amount_paise END),0)"},
    },
}
TEMPLATES = [
    {"name": "Admitted patients by current department", "query": {"dataset": "patients", "mode": "summary", "group_by": ["current_department"], "measures": ["count"], "filters": [{"field": "status", "op": "eq", "value": "admitted"}]}},
    {"name": "Admissions by department", "query": {"dataset": "admissions", "mode": "summary", "group_by": ["department"], "measures": ["count"]}},
    {"name": "Outpatient visits by clinic and status", "query": {"dataset": "opd_visits", "mode": "summary", "group_by": ["department", "status"], "measures": ["count"]}},
    {"name": "Issued invoices and credit notes", "query": {"dataset": "invoices", "mode": "summary", "group_by": ["invoice_type"], "measures": ["count", "payable_paise", "tax_paise"], "filters": [{"field": "status", "op": "in", "value": ["issued", "paid", "partially_paid"]}]}},
    {"name": "Receipts by method and direction", "query": {"dataset": "payments", "mode": "summary", "group_by": ["method", "direction"], "measures": ["count", "amount_paise"]}},
]
QUERY_KEYS = frozenset({"dataset", "mode", "columns", "filters", "group_by", "measures", "limit", "from_day", "to_day"})


def _dataset(actor: dict, name: Any) -> dict:
    if not isinstance(name, str) or name not in DATASETS:
        raise WorkbenchError("invalid_dataset", "Choose a dataset from the report catalog.")
    dataset = DATASETS[name]
    if not has_all_permissions(actor["role"], *dataset["permissions"]):
        raise WorkbenchError("permission_denied", "This account cannot read that dataset.", 403)
    return dataset


def catalog(*, user: dict) -> dict:
    module_on("REPORTING_ENABLED", "reporting")
    with db_session() as db:
        actor = identity(db, user, "reporting.read")
        available = {name: data for name, data in DATASETS.items()
                     if has_all_permissions(actor["role"], *data["permissions"])}
    return {"datasets": [{"id": name, "label": data["label"],
                           "fields": [{"id": key, "kind": value[1], "groupable": value[2]} for key, value in data["fields"].items()],
                           "default_columns": data["default"], "measures": list(data["measures"]),
                           "date_required": name != "patients",
                           "cohort_problem_filter": name == "patients"} for name, data in available.items()],
            "templates": [template for template in TEMPLATES if template["query"]["dataset"] in available],
            "limits": {"filters": 10, "columns": 12, "groups": 2, "rows": 500, "days": 366},
            "notes": ["Charted age is the recorded age, not a calculated current age.",
                      "Current patient department is not historical revenue attribution.",
                      "Money fields are integer paise. Receipt totals include each visible row and direction; choose status filters deliberately."]}


def _list(value: Any, name: str, *, maximum: int, default: list | None = None) -> list:
    value = default if value is None else value
    if not isinstance(value, list) or len(value) > maximum:
        raise WorkbenchError("invalid_query", f"{name} must be a list of at most {maximum} items.")
    return value


def _condition(field: tuple, op: Any, value: Any) -> tuple[str, list]:
    expression, kind, _ = field
    if op == "is_null":
        if not isinstance(value, bool):
            raise WorkbenchError("invalid_filter", "is_null needs true or false.")
        return f"{expression} IS {'NULL' if value else 'NOT NULL'}", []
    def checked(v):
        return integer(v, "filter value", minimum=-9_000_000_000_000_000) if kind == "integer" else text(v, "filter value", maximum=200, blank=True)
    if op == "in":
        values = _list(value, "filter values", maximum=20)
        if not values:
            raise WorkbenchError("invalid_filter", "An in filter requires at least one value.")
        return f"{expression} IN ({','.join('?' for _ in values)})", [checked(v) for v in values]
    if op == "contains" and kind == "text":
        v = checked(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"{expression} LIKE ? ESCAPE '\\'", [f"%{v}%"]
    operators = {"eq": "=", "ne": "!=", "gte": ">=", "lte": "<="}
    if not isinstance(op, str) or op not in operators:
        raise WorkbenchError("invalid_filter", "Unsupported filter operator.")
    return f"{expression} {operators[op]} ?", [checked(value)]


def _query(db, actor: dict, query: Any, *, exporting: bool = False) -> dict:
    if not isinstance(query, dict) or set(query) - QUERY_KEYS:
        raise WorkbenchError("invalid_query", "Use only fields from the report query catalog.")
    dataset = _dataset(actor, query.get("dataset"))
    name, fields = query["dataset"], dataset["fields"]
    mode = query.get("mode", "rows")
    if not isinstance(mode, str) or mode not in {"rows", "summary"}:
        raise WorkbenchError("invalid_query", "Mode must be rows or summary.")
    limit = integer(query.get("limit", 200), "limit", minimum=1, maximum=500)
    subquery, params = patients_sql(db, actor, department=name != "opd_visits")
    clauses = [f"{dataset['patient']} IN ({subquery})"]
    args = list(params)
    dept, facilities = scope(db, actor)
    if name == "opd_visits" and dept is not None:
        clauses.append("v.department = ?")
        args.append(dept)
    if dataset.get("supplier") and facilities is not None:
        # Unknown legacy supplier identity cannot be assigned to a facility.
        if not facilities:
            clauses.append("0")
        else:
            clauses.append(f"{dataset['supplier']} IN ({','.join('?' for _ in facilities)})")
            args.extend(sorted(facilities))
    period = None
    if name != "patients" or query.get("from_day") is not None or query.get("to_day") is not None:
        first, last, start, end = window(query.get("from_day"), query.get("to_day"), default=True)
        period = {"from_day": first, "to_day": last}
        if dataset["local_date"]:
            clauses += [f"{dataset['date']} >= ?", f"{dataset['date']} <= ?"]
            args += [first, last]
        else:
            clauses += [f"julianday({dataset['date']}) >= julianday(?)", f"julianday({dataset['date']}) < julianday(?)"]
            args += [start, end]
    for item in _list(query.get("filters"), "filters", maximum=10, default=[]):
        if not isinstance(item, dict) or set(item) != {"field", "op", "value"}:
            raise WorkbenchError("invalid_filter", "Each filter needs field, op and value.")
        key = item["field"]
        if name == "patients" and key == "problem_icd10":
            if not isinstance(item["op"], str) or item["op"] not in {"eq", "in"}:
                raise WorkbenchError("invalid_filter", "Problem codes support eq or in.")
            condition, values = _condition(("pl.icd10_code", "text", False), item["op"], item["value"])
            clauses.append(f"EXISTS (SELECT 1 FROM problem_list pl WHERE pl.patient_id=p.id AND pl.status='active' AND {condition})")
        else:
            if not isinstance(key, str) or key not in fields:
                raise WorkbenchError("invalid_field", "Choose a field from this dataset.")
            condition, values = _condition(fields[key], item["op"], item["value"])
            clauses.append(condition)
        args.extend(values)
    group = []
    if mode == "summary":
        group = _list(query.get("group_by"), "group_by", maximum=2, default=[])
        measures = _list(query.get("measures"), "measures", maximum=4, default=["count"])
        if not measures or any(not isinstance(m, str) for m in measures) or len(set(measures)) != len(measures) or any(m not in dataset["measures"] for m in measures):
            raise WorkbenchError("invalid_measure", "Choose measures from this dataset.")
        if any(not isinstance(g, str) for g in group) or len(set(group)) != len(group) or any(g not in fields or not fields[g][2] for g in group):
            raise WorkbenchError("invalid_group", "Choose groupable fields from this dataset.")
        columns = group + measures
        select = [f'{fields[g][0]} AS "{g}"' for g in group] + [f'{dataset["measures"][m]} AS "{m}"' for m in measures]
    else:
        columns = _list(query.get("columns"), "columns", maximum=12, default=dataset["default"])
        if not columns or any(not isinstance(c, str) for c in columns) or len(set(columns)) != len(columns) or any(c not in fields for c in columns):
            raise WorkbenchError("invalid_field", "Choose distinct columns from this dataset.")
        select = [f'{fields[c][0]} AS "{c}"' for c in columns]
    sql = f"SELECT {', '.join(select)} FROM {dataset['from']} WHERE " + " AND ".join(clauses)
    if group:
        sql += " GROUP BY " + ", ".join(fields[g][0] for g in group)
    sql += " ORDER BY " + ", ".join(f'"{c}"' for c in (group or columns[:1]))
    sql += " LIMIT ?"
    rows = [dict(row) for row in db.execute(sql, (*args, limit + 1)).fetchall()]
    truncated = len(rows) > limit
    if truncated and (exporting or mode == "summary"):
        raise WorkbenchError("result_too_large", "The result exceeds the row limit. Narrow the filters before exporting or grouping.", 409)
    result = {"dataset": name, "mode": mode, "columns": columns, "rows": rows[:limit],
              "truncated": truncated, "limit": limit, "period": period,
              "scope": {"department": dept, "facilities": None if facilities is None else sorted(facilities)}}
    audit(db, actor, "report_exported" if exporting else "report_queried", read=True,
          metadata={"dataset": name, "mode": mode, "row_count": len(result["rows"]), "truncated": truncated,
                    "query_hash": digest(query)})
    return result


def run(*, user: dict, query: Any, exporting: bool = False) -> dict:
    module_on("REPORTING_ENABLED", "reporting")
    with db_session(immediate=True) as db:
        actor = identity(db, user, "reporting.read", *(('reporting.export',) if exporting else ()))
        return _query(db, actor, query, exporting=exporting)


def export_csv(*, user: dict, query: Any) -> str:
    result = run(user=user, query=query, exporting=True)
    return csv_text(result["columns"], result["rows"])


def definitions(*, user: dict) -> list[dict]:
    module_on("REPORTING_ENABLED", "reporting")
    with db_session() as db:
        actor = identity(db, user, "reporting.read")
        return [{**{key: row[key] for key in row.keys() if key != "query_json"}, "query": json.loads(row["query_json"])} for row in db.execute(
            "SELECT id,name,query_json,version,created_at,updated_at FROM report_definitions WHERE owner_id=? ORDER BY updated_at DESC LIMIT 100", (actor["id"],)).fetchall()]


def save_definition(*, user: dict, name: Any, query: Any, definition_id: str | None = None, version: Any = None) -> dict:
    module_on("REPORTING_ENABLED", "reporting")
    label = text(name, "name", maximum=100)
    if isinstance(query, dict):
        identifying = {"patient_id", "patient_name", "mrn", "diagnosis", "admission_id", "visit_id", "invoice_number", "receipt_number"}
        for item in _list(query.get("filters"), "filters", maximum=10, default=[]):
            if isinstance(item, dict) and isinstance(item.get("field"), str) and item["field"] in identifying:
                raise WorkbenchError("identifying_saved_filter", "Patient identifiers and free-text diagnoses may be queried but cannot be saved in a report definition.")
    with db_session(immediate=True) as db:
        actor = identity(db, user, "reporting.read", "reporting.write")
        # Run the same validator and scope; a stored definition cannot smuggle SQL.
        _query(db, actor, query)
        now = stamp()
        if definition_id:
            row = db.execute("SELECT * FROM report_definitions WHERE id=? AND owner_id=?", (definition_id, actor["id"])).fetchone()
            if row is None:
                raise WorkbenchError("not_found", "Report definition not found.", 404)
            if integer(version, "version", minimum=1) != row["version"]:
                raise WorkbenchError("stale_definition", "This definition changed. Reload before saving.", 409)
            db.execute("UPDATE report_definitions SET name=?,query_json=?,version=version+1,updated_at=? WHERE id=?", (label, packed(query), now, definition_id))
        else:
            if db.execute("SELECT COUNT(*) FROM report_definitions WHERE owner_id=?", (actor["id"],)).fetchone()[0] >= 100:
                raise WorkbenchError("definition_limit", "Remove an old definition before saving another.", 409)
            definition_id = str(uuid.uuid4())
            db.execute("INSERT INTO report_definitions(id,owner_id,name,query_json,created_at,updated_at) VALUES (?,?,?,?,?,?)", (definition_id, actor["id"], label, packed(query), now, now))
        audit(db, actor, "report_definition_saved", target=definition_id)
        return dict(db.execute("SELECT id,name,version,created_at,updated_at FROM report_definitions WHERE id=?", (definition_id,)).fetchone())


def delete_definition(*, user: dict, definition_id: str) -> dict:
    module_on("REPORTING_ENABLED", "reporting")
    with db_session(immediate=True) as db:
        actor = identity(db, user, "reporting.read", "reporting.write")
        removed = db.execute("DELETE FROM report_definitions WHERE id=? AND owner_id=?", (definition_id, actor["id"])).rowcount
        if not removed:
            raise WorkbenchError("not_found", "Report definition not found.", 404)
        audit(db, actor, "report_definition_deleted", target=definition_id)
    return {"deleted": True}
