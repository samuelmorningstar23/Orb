"""Laboratory result entry: section worklists and manual entry with flags
(lis-10).

Behind ORB_LAB (default off): every route that reaches this module answers
404 while the laboratory is off, and nothing here writes while it is. Entry
and pre-validation edits finish open work, so they are served while the
module drains (ruling LIS-R21).

THE WORKLIST of a section (biochemistry, haematology...) holds every test on
a RECEIVED sample that is waiting for a result or has one entered and not yet
technically validated: STAT first, then the oldest receipt. A cancelled test
leaves it at once, and the worklist says who cancelled it and when, so the
bench stops.

WHAT A VALUE IS
  * numeric: typed as text at the test's reporting precision. More decimal
    places than the test reports is refused (never rounded); fewer is the
    same number ("5.1" for a two-place test is 5.10). Stored as value_milli
    (thousandths) with value_text at the reporting precision, because
    trailing zeros are part of the report.
  * a comparator result ('<0.01', '>600'): the comparator and the bound are
    kept in comparator and value_text, and value_milli stays NULL, as
    migration 55 defines the column, so no reader can ever treat a bound as
    an exact value (the chart cannot plot it, a delta check skips it). The
    bound is still held to the test's precision.
  * coded: one of the test's coded values, in the catalogue's spelling;
    anything else is refused.
  * text: free text.

FLAGS (rulings LIS-R2 and LIS-R7, strictest options, held for Samuel to
confirm before go-live): H and L come ONLY from an approved reference range
(lab_master_service.applicable_range), compared at the test's reporting
precision: L below low, H above high, so a value equal to a limit is within
the range. No range ships, so until a pathologist approves one nothing is
flagged and the result says "Reference range not configured". A patient with
no date of birth gets only a range that covers every age; one with no
recorded sex only a range for any sex; otherwise no range, no flag, and the
reason in words. The age is the age when the sample was collected (from
patients.date_of_birth, never the static patients.age); the range version is
the one approved when the value is entered. A comparator result, a coded
result and a text result carry no H or L.
Critical limits (HH, LL) are NOT decided here: lis-11 and lis-15 raise
criticals at technical validation (ruling LIS-R4). Entry stores
is_critical 0 and no critical_limit_id.

EVERY WRITE is one BEGIN IMMEDIATE transaction with its clinical_access audit
row inside it (fail-closed) and a lab_result_events row: 'entered' with the
value after, 'edited' with the value before and after. An edit is allowed
only before technical validation; after that a change is an amendment
(lis-13). An edit says what the editor's screen showed (value, comment and
flag); if the stored result no longer matches, someone else changed it, and
the edit is refused 409 'result_changed' with the current row, so a stale
screen never overwrites another person's correction in silence. One live
result per test is guaranteed by the partial unique index
uq_lab_results_live_item: a second entry is refused, never doubled.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

from database import db_session
from db_backend import DBIntegrityError
from permissions import LAB_RESULT, has_permission
from services import lab_master_service as lm
from services import lab_order_service as los
from services.audit_service import log_audit_event
from services.clinical_time import utc_iso_now

logger = logging.getLogger("orb.lab.results")

#: The most rows one worklist returns.
WORKLIST_CAP = 500
#: How far back the worklist lists tests cancelled after their sample was
#: received ("Cancelled by ... at ...").
CANCELLED_LOOKBACK_HOURS = 24
#: The most recently cancelled tests listed.
CANCELLED_LIST_CAP = 100
#: The longest text or coded result kept.
TEXT_RESULT_MAX = 2000
#: Thousandths. Far beyond any laboratory value, small enough to stay exact.
_MILLI_LIMIT = 10 ** 15

#: Item statuses on a section's worklist: waiting for a result, or entered
#: and not yet technically validated.
WORKLIST_ITEM_STATUSES = ("received", "resulted")
#: Specimen statuses whose tests are on the bench.
_BENCH_SPECIMEN_STATUSES = ("received", "in_process")
#: Result statuses an edit accepts.
_EDITABLE_RESULT_STATUSES = ("entered",)

_COMPARATOR = re.compile(r"^(<=|>=|<|>|≤|≥)\s*(.*)$")
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_COMPARATOR_SPELLING = {"≤": "<=", "≥": ">="}

_PRIORITY_RANK_SQL = "CASE s.priority WHEN 'stat' THEN 0 WHEN 'urgent' THEN 1 ELSE 2 END"

_RESULT_TABLES = ("lab_tests", "lab_specimens", "lab_order_items", "lab_results",
                  "lab_result_events", "lab_sections", "lab_reference_ranges")


class LabResultError(Exception):
    """A refusal with a machine code, a sentence for the screen, the HTTP
    status the route answers with, and any extra fields for the body."""

    def __init__(self, code: str, message: str, *, status: int = 409, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


# ════════════════════════════════════════════════════════════════════════════
#  Pure helpers: parsing, precision and flags
# ════════════════════════════════════════════════════════════════════════════

def _places_words(decimal_places: int) -> str:
    if decimal_places == 0:
        return "whole numbers"
    return f"{decimal_places} decimal place" + ("" if decimal_places == 1 else "s")


def parse_numeric(text: Any, decimal_places: int, *, test_name: str = "This test") -> dict:
    """A typed numeric result, checked at the test's reporting precision.

    Returns {comparator, value_milli, value_text}. A comparator result keeps
    its bound in value_text ('<0.01') with value_milli None. Raises
    LabResultError (422): 'value_required', 'not_a_number' or 'precision'
    (more decimal places than the test reports; never rounded)."""
    raw = str(text if text is not None else "").strip()
    if not raw:
        raise LabResultError("value_required", "Type the result.", status=422)
    comparator = None
    match = _COMPARATOR.match(raw)
    if match:
        comparator = _COMPARATOR_SPELLING.get(match.group(1), match.group(1))
        raw = match.group(2).strip()
    if not _NUMBER.match(raw):
        raise LabResultError(
            "not_a_number",
            "Type a number, such as 4.2, or a bound such as <0.01 or >600.", status=422)
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise LabResultError("not_a_number", "Type a number.", status=422)
    places = int(decimal_places)
    scaled = number * (Decimal(10) ** places)
    if scaled != scaled.to_integral_value():
        raise LabResultError(
            "precision",
            f"{test_name} is reported in {_places_words(places)}. Type the value as the "
            "report prints it; Orb does not round a result.",
            status=422, decimal_places=places)
    milli = number * 1000
    if abs(milli) >= _MILLI_LIMIT:
        raise LabResultError("out_of_range", "This value is out of range.", status=422)
    shown = f"{number:.{places}f}"
    if shown.startswith("-") and Decimal(shown) == 0:
        shown = shown[1:]
    if comparator:
        return {"comparator": comparator, "value_milli": None, "value_text": f"{comparator}{shown}"}
    return {"comparator": None, "value_milli": int(milli), "value_text": shown}


def parse_coded(text: Any, allowed: list[str]) -> str:
    """One of the test's coded values, matched without regard to case or
    surrounding spaces and stored in the catalogue's spelling. Anything else
    is refused (422 'not_in_list', with the allowed values)."""
    raw = re.sub(r"\s+", " ", str(text if text is not None else "")).strip()
    if not raw:
        raise LabResultError("value_required", "Choose the result.", status=422)
    for value in allowed:
        if value.casefold() == raw.casefold():
            return value
    raise LabResultError("not_in_list", "Choose one of this test's results.", status=422,
                         allowed=allowed)


def parse_text(text: Any) -> str:
    raw = str(text if text is not None else "").strip()
    if not raw:
        raise LabResultError("value_required", "Type the result.", status=422)
    if len(raw) > TEXT_RESULT_MAX:
        raise LabResultError("too_long",
                             f"A text result is at most {TEXT_RESULT_MAX} characters.",
                             status=422)
    return raw


def at_precision(milli: Optional[int], decimal_places: int) -> Optional[int]:
    """Thousandths rounded half-up to the reporting precision, the rule the
    NEWS2 charted-value ruling uses: a flag is decided on the number the
    report prints, never on a finer one."""
    if milli is None:
        return None
    step = Decimal(10) ** (3 - int(decimal_places))
    value = Decimal(int(milli)) / step
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP) * step)


def range_flag(value_milli: Optional[int], *, low_milli: Optional[int],
               high_milli: Optional[int], decimal_places: int) -> str:
    """'L', 'H' or '' for a value against an approved range, at the
    reporting precision. Within means low <= value <= high, so a value equal
    to a limit is NOT flagged. A missing value (a comparator result) is never
    flagged. Pure."""
    value = at_precision(value_milli, decimal_places)
    if value is None:
        return ""
    low = at_precision(low_milli, decimal_places)
    high = at_precision(high_milli, decimal_places)
    if low is not None and value < low:
        return "L"
    if high is not None and value > high:
        return "H"
    return ""


def flag_text(result: dict) -> str:
    """The flag in words for the bench. Pure."""
    flag = result.get("abnormal_flag") or ""
    if flag == "H":
        return "High"
    if flag == "L":
        return "Low"
    if flag == "HH":
        return "Critically high"
    if flag == "LL":
        return "Critically low"
    if flag == "A":
        return "Abnormal"
    if result.get("result_type") not in (None, "numeric"):
        return "No flag"
    if result.get("comparator"):
        return "No flag: the value is a bound"
    if not result.get("reference_range_id"):
        return "No flag: reference range not configured"
    return "Within range"


# ════════════════════════════════════════════════════════════════════════════
#  Reads
# ════════════════════════════════════════════════════════════════════════════

def _tables_present(conn) -> bool:
    return lm.tables_present(conn, _RESULT_TABLES)


def _placeholders(values) -> str:
    return ", ".join("?" for _ in values)


def _test_block(row: dict) -> dict:
    return {"id": row["test_id"], "name": row.get("test_name") or row["test_id"],
            "short_name": row.get("test_short_name") or row.get("test_name") or row["test_id"],
            "result_type": row.get("result_type") or "numeric",
            "unit": row.get("unit") or "",
            "decimal_places": int(row.get("decimal_places") or 0),
            "coded_values": lm.coded_values({"coded_values_json": row.get("coded_values_json")})}


def _tat_minutes(row: dict) -> Optional[int]:
    key = {"stat": "tat_stat_min", "urgent": "tat_urgent_min"}.get(row.get("priority"),
                                                                    "tat_routine_min")
    value = row.get(key)
    return int(value) if value else None


def _result_block(row: dict, result_type: str) -> Optional[dict]:
    if not row.get("result_id"):
        return None
    out = {
        "id": row["result_id"],
        "status": row["result_status"],
        "version": row["result_version"],
        "value_text": row["value_text"],
        "comparator": row["comparator"],
        "unit": row["result_unit"],
        "abnormal_flag": row["abnormal_flag"] or "",
        "reference_range_id": row["reference_range_id"],
        "reference_range_text": row["reference_range_text"],
        "comment": row["result_comment"],
        "entered_by_name": row["entered_by_name"],
        "entered_at": row["entered_at"],
    }
    out["flag_text"] = flag_text({**out, "result_type": result_type})
    return out


_WORKLIST_SELECT = """
    SELECT i.id AS item_id, i.order_id, i.specimen_id, i.section_id, i.status AS item_status,
           i.test_id, s.priority, s.received_at, s.collected_at, s.patient_id,
           t.name AS test_name, t.short_name AS test_short_name, t.result_type, t.unit,
           t.decimal_places, t.coded_values_json, t.tat_routine_min, t.tat_urgent_min,
           t.tat_stat_min,
           p.name AS patient_name, p.mrn, p.ward, p.bed,
           r.id AS result_id, r.status AS result_status, r.version AS result_version,
           r.value_text, r.comparator, r.unit AS result_unit, r.abnormal_flag,
           r.reference_range_id, r.reference_range_text, r.comment AS result_comment,
           r.entered_at, eu.name AS entered_by_name
    FROM lab_order_items i
    JOIN lab_specimens s ON s.id = i.specimen_id
    JOIN patients p ON p.id = s.patient_id
    LEFT JOIN lab_tests t ON t.id = i.test_id
    LEFT JOIN lab_results r ON r.order_item_id = i.id
         AND r.status NOT IN ('superseded', 'withdrawn')
    LEFT JOIN users eu ON eu.id = r.entered_by"""


def list_sections(conn) -> list[dict]:
    """Every active section with its counts: tests waiting for a result,
    results entered and not yet validated, and STAT tests waiting. Counts
    only, no patient data."""
    if not _tables_present(conn):
        return []
    rows = conn.execute(
        f"""SELECT sec.id, sec.name, sec.sort_order,
                   SUM(CASE WHEN w.status = 'received' THEN 1 ELSE 0 END) AS waiting,
                   SUM(CASE WHEN w.status = 'resulted' THEN 1 ELSE 0 END) AS entered,
                   SUM(CASE WHEN w.status = 'received' AND w.priority = 'stat'
                            THEN 1 ELSE 0 END) AS stat_waiting
            FROM lab_sections sec
            LEFT JOIN (SELECT i.section_id, i.status, s.priority
                       FROM lab_order_items i
                       JOIN lab_specimens s ON s.id = i.specimen_id
                       WHERE i.status IN ({_placeholders(WORKLIST_ITEM_STATUSES)})
                         AND s.status IN ({_placeholders(_BENCH_SPECIMEN_STATUSES)})) w
                 ON w.section_id = sec.id
            WHERE sec.active = 1
            GROUP BY sec.id
            ORDER BY sec.sort_order, sec.name COLLATE NOCASE""",
        (*WORKLIST_ITEM_STATUSES, *_BENCH_SPECIMEN_STATUSES)).fetchall()
    return [{"id": r["id"], "name": r["name"], "waiting": int(r["waiting"] or 0),
             "entered": int(r["entered"] or 0), "stat_waiting": int(r["stat_waiting"] or 0)}
            for r in rows]


def _range_preview(conn, cache: dict, test_id: str, patient_id: str,
                   collected_at: Optional[str], result_type: str) -> dict:
    """The range a value entered now would be flagged against, for the
    bench to see before typing. The server decides again at entry."""
    if result_type != "numeric":
        return {"id": None, "text": None, "configured": False}
    key = (test_id, patient_id, collected_at)
    if key not in cache:
        choice = lm.applicable_range_for_patient(conn, test_id, patient_id,
                                                 at=collected_at or None,
                                                 version_at=utc_iso_now())
        cache[key] = {"id": choice.row["id"] if choice.row else None,
                      "text": choice.display_text, "configured": choice.row is not None}
    return cache[key]


def worklist(conn, section_id: str, *, dept: Optional[str], limit: int = WORKLIST_CAP) -> dict:
    """One section's bench: every test on a received sample waiting for a
    result or with one entered and not yet validated, STAT first, then the
    oldest receipt, then the sample and the test in order. Each row carries
    the patient, the test (unit, precision, coded values), the range a value
    would be flagged against now, the turnaround target and any entered
    result. Also the tests cancelled after their sample was received, within
    CANCELLED_LOOKBACK_HOURS, with who cancelled them and when.

    `dept` is the caller's department_scope and is required: None (the
    laboratory roles) is hospital-wide; a department keeps only its own
    patients, filtered in SQL before the LIMIT. 404 for an unknown section."""
    if not _tables_present(conn):
        raise LabResultError("not_found", "Section not found", status=404)
    section = conn.execute("SELECT id, name FROM lab_sections WHERE id = ?",
                           (section_id,)).fetchone()
    if section is None:
        raise LabResultError("not_found", "Section not found", status=404)
    from permissions import scope_patient_ids_sql

    limit = max(1, min(int(limit or WORKLIST_CAP), WORKLIST_CAP))
    scope_where, scope_params = "", ()
    if dept is not None:
        scope_sql, params = scope_patient_ids_sql(dept)
        scope_where = f" AND s.patient_id IN ({scope_sql})"
        scope_params = tuple(params)
    rows = [dict(r) for r in conn.execute(
        f"""{_WORKLIST_SELECT}
            WHERE i.section_id = ?
              AND i.status IN ({_placeholders(WORKLIST_ITEM_STATUSES)})
              AND s.status IN ({_placeholders(_BENCH_SPECIMEN_STATUSES)})
              {scope_where}
            ORDER BY {_PRIORITY_RANK_SQL}, julianday(s.received_at) ASC, s.id ASC, i.id ASC
            LIMIT ?""",
        (section_id, *WORKLIST_ITEM_STATUSES, *_BENCH_SPECIMEN_STATUSES, *scope_params,
         limit)).fetchall()]
    cache: dict = {}
    items = []
    for row in rows:
        test = _test_block(row)
        items.append({
            "item_id": row["item_id"],
            "order_id": row["order_id"],
            "specimen_id": row["specimen_id"],
            "priority": row["priority"],
            "status": row["item_status"],
            "patient": {"id": row["patient_id"], "name": row["patient_name"],
                        "mrn": row["mrn"], "ward": row["ward"], "bed": row["bed"]},
            "test": test,
            "received_at": row["received_at"],
            "collected_at": row["collected_at"],
            "tat_minutes": _tat_minutes(row),
            "range": _range_preview(conn, cache, row["test_id"], row["patient_id"],
                                    row["collected_at"], test["result_type"]),
            "result": _result_block(row, test["result_type"]),
        })
    since = (datetime.now(timezone.utc) - timedelta(hours=CANCELLED_LOOKBACK_HOURS)).isoformat()
    cancelled = [dict(r) for r in conn.execute(
        f"""SELECT i.id AS item_id, i.order_id, i.specimen_id, i.test_id,
                   i.cancelled_reason AS reason, i.updated_at AS cancelled_at,
                   t.name AS test_name, u.name AS cancelled_by_name,
                   p.id AS patient_id, p.name AS patient_name, p.mrn
            FROM lab_order_items i
            JOIN lab_specimens s ON s.id = i.specimen_id
            JOIN patients p ON p.id = s.patient_id
            LEFT JOIN lab_tests t ON t.id = i.test_id
            LEFT JOIN users u ON u.id = i.cancelled_by
            WHERE i.section_id = ? AND i.status = 'cancelled'
              AND s.received_at IS NOT NULL
              AND julianday(i.updated_at) >= julianday(?)
              {scope_where}
            ORDER BY julianday(i.updated_at) DESC, i.id DESC
            LIMIT ?""",
        (section_id, since, *scope_params, CANCELLED_LIST_CAP)).fetchall()]
    return {
        "section": {"id": section["id"], "name": section["name"]},
        "items": items,
        "cancelled": [{"item_id": c["item_id"], "order_id": c["order_id"],
                       "specimen_id": c["specimen_id"],
                       "test_name": c["test_name"] or c["test_id"],
                       "patient": {"id": c["patient_id"], "name": c["patient_name"],
                                   "mrn": c["mrn"]},
                       "cancelled_by_name": c["cancelled_by_name"],
                       "cancelled_at": c["cancelled_at"], "reason": c["reason"]}
                      for c in cancelled],
    }


# ════════════════════════════════════════════════════════════════════════════
#  Writes
# ════════════════════════════════════════════════════════════════════════════

def _require_result_permission(actor: dict) -> None:
    if not actor or not actor.get("id") or not has_permission(actor.get("role", ""), LAB_RESULT):
        raise LabResultError("permission_denied",
                             "Only laboratory staff enter results.", status=403)


def _audit(conn, *, action: str, summary: str, actor: dict, result_id: Any,
           patient_id: str, metadata: dict, request_meta: Optional[dict]) -> None:
    meta = dict(request_meta or {})
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_access",
        action_type=action,
        outcome="success",
        db=conn,
        summary_message=summary,
        actor_user_id=(actor or {}).get("id"),
        actor_role=(actor or {}).get("role"),
        target_type="lab_result",
        target_id=str(result_id),
        patient_id=patient_id,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata=metadata,
    )


def _item_for_entry(conn, order_item_id: Any) -> dict:
    try:
        item_id = int(order_item_id)
    except (TypeError, ValueError):
        raise LabResultError("not_found", "Test not found", status=404)
    row = conn.execute(
        """SELECT i.id AS item_id, i.order_id, i.patient_id, i.test_id, i.specimen_id,
                  i.status AS item_status, i.cancelled_reason, i.updated_at AS item_updated_at,
                  cu.name AS cancelled_by_name,
                  s.status AS specimen_status, s.collected_at, s.received_at,
                  t.name AS test_name, t.result_type, t.unit, t.decimal_places,
                  t.coded_values_json
           FROM lab_order_items i
           LEFT JOIN lab_specimens s ON s.id = i.specimen_id
           LEFT JOIN lab_tests t ON t.id = i.test_id
           LEFT JOIN users cu ON cu.id = i.cancelled_by
           WHERE i.id = ?""", (item_id,)).fetchone()
    if row is None:
        raise LabResultError("not_found", "Test not found", status=404)
    return dict(row)


def _refuse_item_state(item: dict) -> None:
    status = item["item_status"]
    if status == "received" and item["specimen_status"] in _BENCH_SPECIMEN_STATUSES:
        return
    if status == "cancelled":
        raise LabResultError(
            "item_cancelled",
            "This test was cancelled" + (f" by {item['cancelled_by_name']}"
                                         if item.get("cancelled_by_name") else "")
            + ". Do not enter a result.",
            cancelled_by_name=item.get("cancelled_by_name"),
            cancelled_at=item.get("item_updated_at"),
            reason=item.get("cancelled_reason"))
    if status == "rejected":
        raise LabResultError("specimen_rejected",
                             "The sample for this test was rejected. Do not enter a result.")
    if status == "resulted":
        raise LabResultError("result_exists",
                             "A result is already entered for this test. Edit it instead.")
    if status in ("tech_validated", "released"):
        raise LabResultError("already_validated",
                             "This result has been validated. A change now is an amendment.")
    raise LabResultError("not_received",
                         "This test's sample has not been received in the laboratory yet.",
                         item_status=status)


def _value_fields(conn, item: dict, value: Any) -> dict:
    """Parse the typed value for this test and decide its flag and range.
    Returns the lab_results columns that depend on the value."""
    result_type = item.get("result_type") or "numeric"
    dp = int(item.get("decimal_places") or 0)
    fields = {"value_milli": None, "comparator": None, "value_text": None,
              "unit": item.get("unit") or None, "decimal_places": dp,
              "reference_range_id": None, "reference_range_text": None, "abnormal_flag": ""}
    if result_type == "coded":
        fields["value_text"] = parse_coded(value, lm.coded_values(item))
        return fields
    if result_type == "text":
        fields["value_text"] = parse_text(value)
        return fields
    parsed = parse_numeric(value, dp, test_name=item.get("test_name") or "This test")
    fields.update(parsed)
    choice = lm.applicable_range_for_patient(
        conn, item["test_id"], item["patient_id"],
        at=item.get("collected_at") or None, version_at=utc_iso_now())
    fields["reference_range_text"] = choice.display_text
    if choice.row is not None:
        fields["reference_range_id"] = int(choice.row["id"])
        fields["abnormal_flag"] = range_flag(
            parsed["value_milli"], low_milli=choice.row.get("low_milli"),
            high_milli=choice.row.get("high_milli"), decimal_places=dp)
    return fields


def _clean_comment(comment: Any) -> Optional[str]:
    text = str(comment if comment is not None else "").strip()
    if len(text) > 1000:
        raise LabResultError("too_long", "A comment is at most 1000 characters.", status=422)
    return text or None


def _result_row(conn, result_id: int) -> dict:
    row = conn.execute(
        """SELECT r.*, u.name AS entered_by_name, t.result_type
           FROM lab_results r
           LEFT JOIN users u ON u.id = r.entered_by
           LEFT JOIN lab_tests t ON t.id = r.test_id
           WHERE r.id = ?""", (result_id,)).fetchone()
    out = dict(row)
    out["flag_text"] = flag_text(out)
    return out


_SNAPSHOT_KEYS = ("value_text", "comparator", "value_milli", "unit", "decimal_places",
                  "abnormal_flag", "reference_range_id", "reference_range_text", "comment")


def _snapshot(row: dict) -> dict:
    return {k: row.get(k) for k in _SNAPSHOT_KEYS}


#: What an edit must say it last saw. The comparator is part of value_text
#: ('<0.01'), so these three are everything the bench shows of a result.
EXPECTED_KEYS = ("value_text", "comment", "abnormal_flag")


def _seen_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _refuse_if_changed(conn, current: dict, expected: Any) -> None:
    """Refuse an edit made from a screen that is out of date (409
    'result_changed'), so a stale bench never overwrites another person's
    change in silence.

    `expected` is the value_text, comment and abnormal_flag the editor's
    screen showed. Each is compared with the stored row (blank and missing
    are the same; nothing else is forgiven). A missing or malformed
    `expected` is refused the same way: an edit that cannot say what it saw
    is treated as stale. The refusal carries the current row and who last
    saved it, so the bench can show what is now true."""
    seen = expected if isinstance(expected, dict) else {}
    differs = [k for k in EXPECTED_KEYS
               if k not in seen or _seen_text(seen.get(k)) != _seen_text(current.get(k))]
    if not differs:
        return
    last = conn.execute(
        """SELECT e.event_type, e.occurred_at, u.name AS actor_name
           FROM lab_result_events e
           LEFT JOIN users u ON u.id = e.actor
           WHERE e.result_id = ?
           ORDER BY e.id DESC LIMIT 1""", (current["id"],)).fetchone()
    by = last["actor_name"] if last is not None else None
    now_reads = _seen_text(current.get("value_text")) or "blank"
    raise LabResultError(
        "result_changed",
        "This result has changed since your screen loaded: it now reads "
        f"{now_reads}" + (f" (last saved by {by})" if by else "")
        + ". Nothing was saved. Check it and make your change again.",
        changed_fields=differs,
        changed_by_name=by,
        changed_at=last["occurred_at"] if last is not None else None,
        current=_result_row(conn, int(current["id"])))


def enter_result(order_item_id: Any, *, value: Any, comment: Any = None, actor: dict,
                 dept: Optional[str] = None,
                 request_meta: Optional[dict] = None) -> dict:
    """Enter one result by hand, in ONE immediate transaction: the
    lab_results row (status 'entered', version 1, source 'lis_manual'), the
    test to 'resulted', the 'entered' result event and the clinical_access
    audit row. Refused: a test not on a received sample, cancelled, rejected
    or already resulted; a value the test cannot take (precision, coded list).
    The partial unique index refuses a second live result even under a race.
    """
    from permissions import patient_in_scope

    _require_result_permission(actor)
    comment = _clean_comment(comment)
    actor_id = actor["id"]
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Test not found", status=404)
        item = _item_for_entry(conn, order_item_id)
        if not patient_in_scope(conn, item["patient_id"], dept):
            raise LabResultError("not_found", "Test not found", status=404)
        _refuse_item_state(item)
        fields = _value_fields(conn, item, value)
        now = utc_iso_now()
        try:
            cur = conn.execute(
                """INSERT INTO lab_results
                   (order_item_id, order_id, specimen_id, patient_id, test_id, version,
                    value_milli, comparator, value_text, unit, decimal_places, comment,
                    reference_range_id, reference_range_text, abnormal_flag,
                    critical_limit_id, is_critical, entry_method, entered_by, entered_at,
                    status, source)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 'manual', ?, ?,
                           'entered', 'lis_manual')""",
                (item["item_id"], item["order_id"], item["specimen_id"], item["patient_id"],
                 item["test_id"], fields["value_milli"], fields["comparator"],
                 fields["value_text"], fields["unit"], fields["decimal_places"], comment,
                 fields["reference_range_id"], fields["reference_range_text"],
                 fields["abnormal_flag"], actor_id, now))
        except DBIntegrityError:
            raise LabResultError("result_exists",
                                 "A result is already entered for this test. Edit it instead.")
        result_id = int(cur.lastrowid)
        moved = conn.execute(
            "UPDATE lab_order_items SET status = 'resulted', updated_at = ? "
            "WHERE id = ? AND status = 'received'", (now, item["item_id"]))
        if moved.rowcount != 1:
            raise LabResultError("result_exists",
                                 "A result is already entered for this test. Edit it instead.")
        after = {**fields, "comment": comment}
        conn.execute(
            "INSERT INTO lab_result_events (result_id, patient_id, event_type, actor, "
            "occurred_at, before_json, after_json) VALUES (?, ?, 'entered', ?, ?, NULL, ?)",
            (result_id, item["patient_id"], actor_id, now, json.dumps(after, default=str)))
        los.reconcile_order_status(conn, item["order_id"], actor_id=actor_id,
                                   reason="Laboratory result entered")
        _audit(conn, action="lab_result_entered",
               summary=f"Laboratory result entered: {item.get('test_name') or item['test_id']}",
               actor=actor, result_id=result_id, patient_id=item["patient_id"],
               metadata={"order_item_id": item["item_id"], "order_id": item["order_id"],
                         "specimen_id": item["specimen_id"], "test_id": item["test_id"],
                         "after": after},
               request_meta=request_meta)
        return {"result": _result_row(conn, result_id)}


def edit_result(result_id: Any, *, value: Any, expected: Any, comment: Any = None,
                reason: Any = None, actor: dict, dept: Optional[str] = None,
                request_meta: Optional[dict] = None) -> dict:
    """Change an entered result before technical validation, in ONE immediate
    transaction: the row is updated in place (same version, the flag decided
    again against the range approved now), and the 'edited' result event and
    the clinical_access audit row keep the value before and after, with the
    reason when one is given. A validated, reported, withdrawn or superseded
    result is refused: after validation a change is an amendment (lis-13).

    `expected` is required: the value_text, comment and abnormal_flag the
    editor's screen showed. If the stored result differs (someone else
    changed it since), the edit is refused 409 'result_changed' with the
    current row, and nothing is written; this check runs before the new value
    is read, so a stale screen is never told its value was saved, even when
    it matches. An edit that changes nothing writes nothing."""
    from permissions import patient_in_scope

    _require_result_permission(actor)
    comment = _clean_comment(comment)
    reason = str(reason if reason is not None else "").strip()[:500] or None
    actor_id = actor["id"]
    try:
        rid = int(result_id)
    except (TypeError, ValueError):
        raise LabResultError("not_found", "Result not found", status=404)
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Result not found", status=404)
        current = conn.execute("SELECT * FROM lab_results WHERE id = ?", (rid,)).fetchone()
        if current is None or not patient_in_scope(conn, current["patient_id"], dept):
            raise LabResultError("not_found", "Result not found", status=404)
        current = dict(current)
        if current["status"] not in _EDITABLE_RESULT_STATUSES:
            if current["status"] in ("tech_validated", "released"):
                raise LabResultError(
                    "already_validated",
                    "This result has been validated. A change now is an amendment.")
            raise LabResultError("not_live", "This result is no longer current.",
                                 result_status=current["status"])
        item = _item_for_entry(conn, current["order_item_id"])
        if item["item_status"] != "resulted":
            if item["item_status"] in ("cancelled", "rejected"):
                _refuse_item_state(item)
            raise LabResultError("not_live", "This result is no longer current.",
                                 item_status=item["item_status"])
        _refuse_if_changed(conn, current, expected)
        fields = _value_fields(conn, item, value)
        before = _snapshot(current)
        after = {**fields, "comment": comment}
        if _snapshot(after) == before:
            return {"result": _result_row(conn, rid), "unchanged": True}
        now = utc_iso_now()
        cur = conn.execute(
            """UPDATE lab_results
               SET value_milli = ?, comparator = ?, value_text = ?, unit = ?,
                   decimal_places = ?, comment = ?, reference_range_id = ?,
                   reference_range_text = ?, abnormal_flag = ?
               WHERE id = ? AND status = 'entered'""",
            (fields["value_milli"], fields["comparator"], fields["value_text"], fields["unit"],
             fields["decimal_places"], comment, fields["reference_range_id"],
             fields["reference_range_text"], fields["abnormal_flag"], rid))
        if cur.rowcount != 1:
            raise LabResultError("not_live", "This result is no longer current.")
        conn.execute(
            "INSERT INTO lab_result_events (result_id, patient_id, event_type, actor, "
            "occurred_at, before_json, after_json) VALUES (?, ?, 'edited', ?, ?, ?, ?)",
            (rid, current["patient_id"], actor_id, now, json.dumps(before, default=str),
             json.dumps({**after, "reason": reason}, default=str)))
        _audit(conn, action="lab_result_edited",
               summary=(f"Laboratory result changed before validation: "
                        f"{item.get('test_name') or item['test_id']}"),
               actor=actor, result_id=rid, patient_id=current["patient_id"],
               metadata={"order_item_id": current["order_item_id"],
                         "test_id": current["test_id"], "before": before,
                         "after": _snapshot(after), "reason": reason},
               request_meta=request_meta)
        return {"result": _result_row(conn, rid), "unchanged": False}
