"""Laboratory turnaround times (lis-18): intervals, aggregates, the live
overdue list and the CSV export.

Behind ORB_LAB (default off): the route answers 404 while the laboratory is
off and is served while it drains (ruling LIS-R21). Nothing here writes.

WHAT IS MEASURED. One row per TEST reported (a lab_order_items row with a
released result). Its instants come from the laboratory's own records, never
from a clock reading at report time:
  ordered    the order's activation (its first order_events row with
             new_status 'active', else orders.created_at)
  collected  lab_specimens.collected_at
  received   lab_specimens.received_at
  reported   the FIRST release of the test (the earliest released_at of any
             version). An amendment later does not move the turnaround time.
Four intervals, in minutes: order to collection, collection to receipt,
receipt to report and order to report. An interval whose end is before its
start (a back-dated collection, a clock fault) or that cannot be read is left
out of the figures and counted, never shown as zero.

THE TARGET is the test's own turnaround time for the sample's priority
(lab_tests.tat_stat_min, tat_urgent_min, tat_routine_min, set by a
pathologist; none ships), measured from RECEIPT to REPORT, the same clock the
worklist shows. A test reported in exactly its target is within it. A test with
no target for its priority is counted as 'no target', NEVER as 0 percent or
100 percent: a group whose tests have no target reports no percentage at all.

FIGURES: count, median (the middle value, or the mean of the two middle values)
and the 90th percentile (nearest rank: the smallest value with at least 90
percent of values at or below it), in minutes to one decimal place.

DAYS are hospital-local calendar days, midnight to midnight
(services/hospital_time), so a test reported at 00:10 IST belongs to that day,
not to the UTC date. A period is whole hospital days, at most MAX_PERIOD_DAYS.

SIMULATED RESULTS (source 'simulated') are left out of every figure and
counted, because a simulator's timings say nothing about the laboratory. The
export lists them, marked.

TIME TO CRITICAL ACKNOWLEDGEMENT is read from lab_critical_events (written by
the critical-result lane, lis-15): from 'raised' to the first 'acknowledged'
event, which the lane writes for both closures ruling LIS-R3 names: the
laboratory's phone call with a read-back (metadata closure
'laboratory_callback') and the ward's in-app read-back. A 'callback_logged'
event alone is NOT an acknowledgement: the lane also writes one for a call
that reached nobody. A critical still open is counted as open and is never
given a time.

THE OVERDUE LIST is live: tests received in the laboratory and not yet
released whose receipt-to-report target has passed, STAT first, then the most
overdue. It names patients, so the route audits it (phi_access, fail-closed),
and a department account sees only its own department's patients.

Every function takes the caller's open connection and never commits.
"""
from __future__ import annotations

import csv
import io
import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from permissions import scope_patient_ids_sql
from services import lab_master_service as lm
from services.clinical_time import parse_to_utc
from services.hospital_time import hospital_now, hospital_today, local_day_bounds_utc, to_hospital

#: The longest period one read covers.
MAX_PERIOD_DAYS = 92

#: The period when none is asked for: the last seven hospital days, today
#: included.
DEFAULT_PERIOD_DAYS = 7

#: The most rows the overdue list returns.
OVERDUE_CAP = 200

#: Values a SQL IN (...) list carries at once.
_CHUNK = 400

_TABLES = ("lab_order_items", "lab_results", "lab_specimens", "lab_tests")

PRIORITIES = ("stat", "urgent", "routine")
PRIORITY_WORDS = {"stat": "STAT", "urgent": "Urgent", "routine": "Routine"}
_TARGET_COLUMN = {"stat": "tat_stat_min", "urgent": "tat_urgent_min",
                  "routine": "tat_routine_min"}

#: (key, words, start instant, end instant). The receipt-to-report interval is
#: the one a target applies to.
INTERVALS = (
    ("order_to_collection", "Order to collection", "ordered_at", "collected_at"),
    ("collection_to_receipt", "Collection to receipt", "collected_at", "received_at"),
    ("receipt_to_report", "Receipt to report", "received_at", "reported_at"),
    ("order_to_report", "Order to report", "ordered_at", "reported_at"),
)
TARGET_INTERVAL = "receipt_to_report"

#: Items still in the laboratory (received, not yet released).
_IN_LAB_STATUSES = ("received", "resulted", "tech_validated")

NO_TARGET_TEXT = "No target"

#: The event that closes a critical result (ruling LIS-R3: the laboratory's
#: phone call with a read-back, or the ward's in-app read-back). A Relay post
#: never does, and neither does a logged call that reached nobody.
CRITICAL_CLOSING_EVENTS = ("acknowledged",)

#: metadata_json closure of an 'acknowledged' event written by the
#: laboratory's callback (lab_critical_service.log_callback).
LABORATORY_CALLBACK_CLOSURE = "laboratory_callback"

EXCEPTION_WORDS = {
    "unmapped_code": "Code not in the laboratory catalogue",
    "intake_failed": "Not taken into the laboratory yet",
    "order_state_conflict": "Order changed outside the laboratory",
    "rejected_no_decision": "Rejected specimen waiting for a decision",
}


class LabTatError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ════════════════════════════════════════════════════════════════════════════
#  Pure helpers
# ════════════════════════════════════════════════════════════════════════════

def tables_present(conn) -> bool:
    return lm.tables_present(conn, _TABLES)


def _instant(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return parse_to_utc(value)
    except (TypeError, ValueError):
        return None


def median(values: list[float]) -> Optional[float]:
    """The middle value, or the mean of the two middle values. None for none."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def percentile(values: list[float], pct: float) -> Optional[float]:
    """Nearest rank: the smallest value with at least `pct` percent of the
    values at or below it. None for none."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _minutes(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 1)


def target_minutes(row: dict, priority: Optional[str]) -> Optional[int]:
    """The test's receipt-to-report target for this priority, or None."""
    value = row.get(_TARGET_COLUMN.get(priority or "routine", "tat_routine_min"))
    try:
        value = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return value if value and value > 0 else None


def hospital_day_of(value: Any) -> Optional[str]:
    local = to_hospital(value)
    return local.date().isoformat() if local else None


def resolve_period(start: Optional[str], end: Optional[str], *,
                   today: Optional[date] = None) -> tuple[date, date]:
    """(first day, last day), hospital-local, both included. Defaults to the
    last DEFAULT_PERIOD_DAYS days, today included. Raises LabTatError 422
    'bad_period' for an unreadable day, a start after the end, an end after
    today or a period longer than MAX_PERIOD_DAYS."""
    today = today or hospital_today()

    def _day(text: Optional[str], field: str) -> Optional[date]:
        if not text:
            return None
        try:
            return date.fromisoformat(str(text).strip())
        except ValueError:
            raise LabTatError("bad_period", f"Give the {field} day as YYYY-MM-DD.")

    last = _day(end, "last") or today
    first = _day(start, "first") or (last - timedelta(days=DEFAULT_PERIOD_DAYS - 1))
    if first > last:
        raise LabTatError("bad_period", "The first day must not be after the last day.")
    if last > today:
        raise LabTatError("bad_period", "The last day cannot be after today.")
    if (last - first).days + 1 > MAX_PERIOD_DAYS:
        raise LabTatError("bad_period",
                          f"Choose a period of at most {MAX_PERIOD_DAYS} days.")
    return first, last


def period_bounds(first: date, last: date) -> tuple[datetime, datetime]:
    """The [start, end) UTC instants of hospital days first..last."""
    return local_day_bounds_utc(first)[0], local_day_bounds_utc(last)[1]


def _chunks(values: Iterable[Any]) -> Iterable[list]:
    items = list(dict.fromkeys(v for v in values if v is not None))
    for start in range(0, len(items), _CHUNK):
        yield items[start:start + _CHUNK]


def _marks(values) -> str:
    return ", ".join("?" for _ in values)


def _scope_clause(column: str, dept: Optional[str]) -> tuple[str, tuple]:
    if dept is None:
        return "", ()
    sql, params = scope_patient_ids_sql(dept)
    return f" AND {column} IN ({sql})", tuple(params)


def _ward_clause(column: str, ward_key: Optional[str]) -> tuple[str, tuple]:
    if not ward_key:
        return "", ()
    return (f" AND {column} IN (SELECT id FROM patients WHERE UPPER(TRIM(ward)) = ?)",
            (str(ward_key).strip().upper(),))


# ════════════════════════════════════════════════════════════════════════════
#  The reported tests of a period
# ════════════════════════════════════════════════════════════════════════════

def reported_tests(conn, start: datetime, end: datetime, *, dept: Optional[str] = None,
                   ward_key: Optional[str] = None) -> list[dict]:
    """One dict per test first reported in [start, end): its instants, its
    four intervals in minutes (None where unreadable or out of order), its
    target and whether it was within it. Oldest report first."""
    if not tables_present(conn):
        return []
    scope_sql, scope_params = _scope_clause("r.patient_id", dept)
    ward_sql, ward_params = _ward_clause("r.patient_id", ward_key)
    # Candidates: any test with a release inside the window (julianday reads
    # every spelling Orb writes). Its FIRST release is decided below in Python,
    # so a test first reported before the window and amended inside it is not
    # counted again.
    candidates = [r[0] for r in conn.execute(
        "SELECT DISTINCT r.order_item_id FROM lab_results r WHERE r.released_at IS NOT NULL "
        "AND julianday(r.released_at) >= julianday(?) AND julianday(r.released_at) < julianday(?)"
        + scope_sql + ward_sql,
        (start.isoformat(), end.isoformat(), *scope_params, *ward_params)).fetchall()]
    if not candidates:
        return []
    first_release: dict[int, tuple[datetime, str]] = {}
    simulated_items: set[int] = set()
    for chunk in _chunks(candidates):
        for r in conn.execute(
                f"SELECT order_item_id, released_at, source FROM lab_results "
                f"WHERE released_at IS NOT NULL AND order_item_id IN ({_marks(chunk)})",
                tuple(chunk)).fetchall():
            key = int(r["order_item_id"])
            if r["source"] == "simulated":
                simulated_items.add(key)
            at = _instant(r["released_at"])
            if at is None:
                continue
            held = first_release.get(key)
            if held is None or at < held[0]:
                first_release[key] = (at, r["released_at"])
    wanted = [k for k, (at, _) in first_release.items() if start <= at < end]
    if not wanted:
        return []
    items: list[dict] = []
    for chunk in _chunks(wanted):
        items.extend(dict(r) for r in conn.execute(
            f"""SELECT i.id AS item_id, i.order_id, i.specimen_id, i.section_id, i.priority,
                       i.test_id, i.patient_id, t.name AS test_name, t.short_name,
                       t.tat_routine_min, t.tat_urgent_min, t.tat_stat_min,
                       sec.name AS section_name, sec.sort_order AS section_order,
                       s.collected_at, s.received_at, o.created_at AS order_created_at
                FROM lab_order_items i
                LEFT JOIN lab_tests t ON t.id = i.test_id
                LEFT JOIN lab_sections sec ON sec.id = i.section_id
                LEFT JOIN lab_specimens s ON s.id = i.specimen_id
                LEFT JOIN orders o ON o.id = i.order_id
                WHERE i.id IN ({_marks(chunk)})""", tuple(chunk)).fetchall())
    activation: dict[str, str] = {}
    for chunk in _chunks(i["order_id"] for i in items):
        for r in conn.execute(
                f"SELECT order_id, created_at FROM order_events WHERE new_status = 'active' "
                f"AND order_id IN ({_marks(chunk)}) ORDER BY id ASC", tuple(chunk)).fetchall():
            activation.setdefault(r["order_id"], r["created_at"])
    out = []
    for item in items:
        at, raw = first_release[int(item["item_id"])]
        simulated = int(item["item_id"]) in simulated_items
        instants = {
            "ordered_at": activation.get(item["order_id"]) or item.get("order_created_at"),
            "collected_at": item.get("collected_at"),
            "received_at": item.get("received_at"),
            "reported_at": raw,
        }
        intervals: dict[str, Optional[float]] = {}
        out_of_order: list[str] = []
        for key, _, a, b in INTERVALS:
            ta, tb = _instant(instants[a]), _instant(instants[b])
            if ta is None or tb is None:
                intervals[key] = None
            elif tb < ta:
                intervals[key] = None
                out_of_order.append(key)
            else:
                intervals[key] = (tb - ta).total_seconds() / 60.0
        priority = item.get("priority") or "routine"
        target = target_minutes(item, priority)
        tat = intervals[TARGET_INTERVAL]
        within: Optional[bool] = None
        if target is not None and tat is not None:
            within = tat <= target
        out.append({
            "item_id": int(item["item_id"]),
            "order_id": item["order_id"],
            "specimen_id": item.get("specimen_id"),
            "patient_id": item.get("patient_id"),
            "test_id": item["test_id"],
            "test_name": item.get("test_name") or item["test_id"],
            "short_name": item.get("short_name") or item.get("test_name") or item["test_id"],
            "section_id": item.get("section_id"),
            "section_name": item.get("section_name") or item.get("section_id") or "No section",
            "section_order": int(item.get("section_order") or 0),
            "priority": priority,
            "instants": instants,
            "reported_day": to_hospital(at).date().isoformat(),
            "intervals": intervals,
            "out_of_order": out_of_order,
            "target_minutes": target,
            "within_target": within,
            "simulated": simulated,
        })
    out.sort(key=lambda r: (_instant(r["instants"]["reported_at"]), r["item_id"]))
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Aggregates
# ════════════════════════════════════════════════════════════════════════════

def _figures(rows: list[dict]) -> dict:
    """Count, median and 90th percentile of every interval, and the target
    figures, for real (not simulated) rows."""
    out: dict[str, Any] = {"count": len(rows), "intervals": {}}
    for key, _, _, _ in INTERVALS:
        values = [r["intervals"][key] for r in rows if r["intervals"][key] is not None]
        out["intervals"][key] = {"n": len(values), "median": _minutes(median(values)),
                                 "p90": _minutes(percentile(values, 90))}
    with_target = [r for r in rows if r["target_minutes"] is not None
                   and r["intervals"][TARGET_INTERVAL] is not None]
    within = sum(1 for r in with_target if r["within_target"])
    no_target = sum(1 for r in rows if r["target_minutes"] is None)
    target: dict[str, Any] = {
        "with_target": len(with_target),
        "within": within,
        "no_target": no_target,
        "unreadable": sum(1 for r in rows if r["target_minutes"] is not None
                          and r["intervals"][TARGET_INTERVAL] is None),
        "percent": round(100.0 * within / len(with_target), 1) if with_target else None,
    }
    target["state"] = "reported" if with_target else "no_target"
    target["text"] = (f"{target['percent']:.1f}%" if with_target else NO_TARGET_TEXT)
    out["target"] = target
    out["out_of_order"] = sum(1 for r in rows if r["out_of_order"])
    return out


def _priority_rank(priority: str) -> int:
    return PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)


def aggregate(rows: list[dict], first: date, last: date) -> dict:
    """The dashboard figures of a period: overall, by section and priority, and
    by hospital day (every day of the period, an empty day included)."""
    real = [r for r in rows if not r["simulated"]]
    groups: dict[tuple, list[dict]] = {}
    for r in real:
        groups.setdefault((r["section_order"], r["section_name"], r["section_id"],
                           r["priority"]), []).append(r)
    by_group = []
    for (order, name, section_id, priority), members in sorted(
            groups.items(), key=lambda kv: (kv[0][0], kv[0][1], _priority_rank(kv[0][3]))):
        by_group.append({"section_id": section_id, "section_name": name,
                         "priority": priority,
                         "priority_text": PRIORITY_WORDS.get(priority, priority),
                         **_figures(members)})
    by_day = []
    day = first
    while day <= last:
        key = day.isoformat()
        by_day.append({"day": key, **_figures([r for r in real if r["reported_day"] == key])})
        day += timedelta(days=1)
    return {"overall": _figures(real), "by_section_priority": by_group, "by_day": by_day,
            "simulated_excluded": len(rows) - len(real)}


# ════════════════════════════════════════════════════════════════════════════
#  Critical acknowledgement
# ════════════════════════════════════════════════════════════════════════════

def critical_times(conn, start: datetime, end: datetime, *, dept: Optional[str] = None,
                   ward_key: Optional[str] = None) -> list[dict]:
    """One dict per critical result raised in [start, end): raised_at, the
    first 'acknowledged' event after it, how it was closed ('phone_log' for
    the laboratory's callback with a read-back, 'ward_read_back' otherwise)
    and the minutes between, None while it is still open."""
    present = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'lab_critical_events'"
    ).fetchone()[0]
    if not present:
        return []
    scope_sql, scope_params = _scope_clause("e.patient_id", dept)
    ward_sql, ward_params = _ward_clause("e.patient_id", ward_key)
    raised = [dict(r) for r in conn.execute(
        "SELECT e.notification_id, e.patient_id, e.occurred_at FROM lab_critical_events e "
        "WHERE e.event_type = 'raised' AND julianday(e.occurred_at) >= julianday(?) "
        "AND julianday(e.occurred_at) < julianday(?)" + scope_sql + ward_sql
        + " ORDER BY e.id ASC",
        (start.isoformat(), end.isoformat(), *scope_params, *ward_params)).fetchall()]
    first_raised: dict[str, dict] = {}
    for r in raised:
        at = _instant(r["occurred_at"])
        if at is None or not (start <= at < end):
            continue
        first_raised.setdefault(r["notification_id"], {**r, "at": at})
    if not first_raised:
        return []
    closing: dict[str, list[tuple[datetime, str]]] = {}
    for chunk in _chunks(first_raised):
        for r in conn.execute(
                f"SELECT notification_id, event_type, occurred_at, metadata_json "
                f"FROM lab_critical_events "
                f"WHERE notification_id IN ({_marks(chunk)}) AND event_type IN "
                f"({_marks(CRITICAL_CLOSING_EVENTS)}) ORDER BY id ASC",
                (*chunk, *CRITICAL_CLOSING_EVENTS)).fetchall():
            at = _instant(r["occurred_at"])
            if at is not None:
                closing.setdefault(r["notification_id"], []).append(
                    (at, _closure_kind(r["metadata_json"])))
    out = []
    for nid, row in first_raised.items():
        after = sorted((c for c in closing.get(nid, []) if c[0] >= row["at"]),
                       key=lambda c: c[0])
        ack_at, how = after[0] if after else (None, None)
        out.append({"notification_id": nid, "patient_id": row["patient_id"],
                    "raised_at": row["occurred_at"],
                    "acknowledged_at": ack_at.isoformat() if ack_at else None,
                    "closed_by": how,
                    "minutes": ((ack_at - row["at"]).total_seconds() / 60.0
                                if ack_at else None)})
    return out


def _closure_kind(metadata_json: Any) -> str:
    try:
        meta = json.loads(metadata_json) if metadata_json else {}
    except (TypeError, ValueError):
        meta = {}
    closure = meta.get("closure") if isinstance(meta, dict) else None
    return "phone_log" if closure == LABORATORY_CALLBACK_CLOSURE else "ward_read_back"


def critical_summary(rows: list[dict], *, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    done = [r["minutes"] for r in rows if r["minutes"] is not None]
    open_rows = [r for r in rows if r["minutes"] is None]
    oldest = None
    for r in open_rows:
        at = _instant(r["raised_at"])
        if at is not None:
            age = (now - at).total_seconds() / 60.0
            oldest = age if oldest is None else max(oldest, age)
    return {
        "raised": len(rows),
        "acknowledged": len(done),
        "by_phone_log": sum(1 for r in rows if r["closed_by"] == "phone_log"),
        "by_ward_read_back": sum(1 for r in rows if r["closed_by"] == "ward_read_back"),
        "open": len(open_rows),
        "oldest_open_minutes": _minutes(oldest),
        "median_minutes": _minutes(median(done)),
        "p90_minutes": _minutes(percentile(done, 90)),
        "state": "reported" if done else ("open_only" if open_rows else "none"),
    }


# ════════════════════════════════════════════════════════════════════════════
#  Live lists
# ════════════════════════════════════════════════════════════════════════════

def overdue_tests(conn, *, dept: Optional[str], now: Optional[datetime] = None,
                  limit: int = OVERDUE_CAP) -> dict:
    """Tests in the laboratory (received, not released) whose receipt-to-report
    target has passed, STAT first, then the most overdue. Also counts the
    tests in the laboratory that have no target, which can never be overdue."""
    if not tables_present(conn):
        return {"items": [], "total": 0, "in_lab": 0, "in_lab_without_target": 0}
    now = now or datetime.now(timezone.utc)
    scope_sql, scope_params = _scope_clause("i.patient_id", dept)
    rows = [dict(r) for r in conn.execute(
        f"""SELECT i.id AS item_id, i.order_id, i.specimen_id, i.priority, i.status,
                   i.test_id, i.section_id, i.patient_id,
                   t.name AS test_name, t.short_name, t.tat_routine_min, t.tat_urgent_min,
                   t.tat_stat_min, sec.name AS section_name,
                   s.received_at, s.collected_at,
                   p.name AS patient_name, p.mrn, p.ward, p.bed
            FROM lab_order_items i
            JOIN lab_specimens s ON s.id = i.specimen_id
            LEFT JOIN lab_tests t ON t.id = i.test_id
            LEFT JOIN lab_sections sec ON sec.id = i.section_id
            LEFT JOIN patients p ON p.id = i.patient_id
            WHERE i.status IN ({_marks(_IN_LAB_STATUSES)}) AND s.received_at IS NOT NULL"""
        + scope_sql, (*_IN_LAB_STATUSES, *scope_params)).fetchall()]
    overdue = []
    without_target = 0
    for r in rows:
        target = target_minutes(r, r.get("priority"))
        received = _instant(r.get("received_at"))
        if target is None:
            without_target += 1
            continue
        if received is None:
            continue
        waited = (now - received).total_seconds() / 60.0
        if waited <= target:
            continue
        overdue.append({
            "item_id": int(r["item_id"]),
            "specimen_id": r["specimen_id"],
            "order_id": r["order_id"],
            "priority": r.get("priority") or "routine",
            "priority_text": PRIORITY_WORDS.get(r.get("priority") or "routine", "Routine"),
            "status": r["status"],
            "test": {"id": r["test_id"], "name": r.get("test_name") or r["test_id"],
                     "short_name": r.get("short_name") or r.get("test_name") or r["test_id"]},
            "section_name": r.get("section_name") or r.get("section_id") or "No section",
            "patient": {"id": r["patient_id"], "name": r.get("patient_name"),
                        "mrn": r.get("mrn"), "ward": r.get("ward"), "bed": r.get("bed")},
            "received_at": r.get("received_at"),
            "target_minutes": target,
            "waiting_minutes": int(waited),
            "over_by_minutes": int(waited - target),
        })
    overdue.sort(key=lambda o: (_priority_rank(o["priority"]), -o["over_by_minutes"],
                                o["item_id"]))
    cap = max(1, min(int(limit or OVERDUE_CAP), OVERDUE_CAP))
    return {"items": overdue[:cap], "total": len(overdue), "in_lab": len(rows),
            "in_lab_without_target": without_target}


def open_exception_counts(conn, *, dept: Optional[str]) -> dict:
    """Open laboratory exceptions by kind, counts only. A department account
    counts only rows whose order's patient is in its department, by the same
    predicate as the exceptions list (lab_order_service.list_open_exceptions):
    a row with no order is not counted for it."""
    present = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'lab_exceptions'"
    ).fetchone()[0]
    if not present:
        return {"open": 0, "by_kind": []}
    where = "x.resolved_at IS NULL"
    params: tuple = ()
    if dept is not None:
        scope_sql, scope_params = scope_patient_ids_sql(dept)
        where += f" AND o.patient_id IN ({scope_sql})"
        params = tuple(scope_params)
    rows = conn.execute(
        f"SELECT x.kind, COUNT(*) AS n FROM lab_exceptions x "
        f"LEFT JOIN orders o ON o.id = x.order_id WHERE {where} GROUP BY x.kind",
        params).fetchall()
    by_kind = [{"kind": r["kind"], "label": EXCEPTION_WORDS.get(r["kind"], "Exception"),
                "count": int(r["n"])} for r in rows]
    by_kind.sort(key=lambda k: k["label"])
    return {"open": sum(k["count"] for k in by_kind), "by_kind": by_kind}


# ════════════════════════════════════════════════════════════════════════════
#  The dashboard and the export
# ════════════════════════════════════════════════════════════════════════════

def dashboard(conn, *, first: date, last: date, dept: Optional[str],
              now: Optional[datetime] = None) -> dict:
    start, end = period_bounds(first, last)
    rows = reported_tests(conn, start, end, dept=dept)
    figures = aggregate(rows, first, last)
    crit = critical_times(conn, start, end, dept=dept)
    local_now = hospital_now()
    return {
        "period": {"first_day": first.isoformat(), "last_day": last.isoformat(),
                   "days": (last - first).days + 1, "start": start.isoformat(),
                   "end": end.isoformat(),
                   "timezone": local_now.tzname() or str(local_now.tzinfo)},
        "intervals": [{"key": key, "label": label} for key, label, _, _ in INTERVALS],
        "target_interval": TARGET_INTERVAL,
        **figures,
        "critical": critical_summary(crit, now=now),
        "overdue": overdue_tests(conn, dept=dept, now=now),
        "exceptions": open_exception_counts(conn, dept=dept),
        "limits": {"max_period_days": MAX_PERIOD_DAYS, "overdue_cap": OVERDUE_CAP},
    }


EXPORT_COLUMNS = (
    "Hospital day", "Accession", "Test", "Section", "Priority",
    "Ordered (hospital time)", "Collected (hospital time)", "Received (hospital time)",
    "Reported (hospital time)",
    "Order to collection (min)", "Collection to receipt (min)", "Receipt to report (min)",
    "Order to report (min)", "Target receipt to report (min)", "Within target", "Simulated",
)

#: A cell a spreadsheet would read as a formula is written with a leading
#: apostrophe, so a test name typed as '=1+1' can never run as one.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_LEAD) else text


def _local_text(value: Any) -> str:
    local = to_hospital(value)
    return local.strftime("%Y-%m-%d %H:%M") if local else ""


def _num(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.1f}"


def export_csv(conn, *, first: date, last: date, dept: Optional[str]) -> dict:
    """Every test first reported in the period, one CSV row each, oldest
    first. No patient name or UHID: the accession links a row to the record
    for anyone entitled to open it."""
    start, end = period_bounds(first, last)
    rows = reported_tests(conn, start, end, dept=dept)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    for r in rows:
        within = ("No target" if r["target_minutes"] is None
                  else ("" if r["within_target"] is None
                        else ("Yes" if r["within_target"] else "No")))
        writer.writerow([_cell(v) for v in (
            r["reported_day"], r["specimen_id"], r["test_name"], r["section_name"],
            PRIORITY_WORDS.get(r["priority"], r["priority"]),
            _local_text(r["instants"]["ordered_at"]), _local_text(r["instants"]["collected_at"]),
            _local_text(r["instants"]["received_at"]), _local_text(r["instants"]["reported_at"]),
            _num(r["intervals"]["order_to_collection"]),
            _num(r["intervals"]["collection_to_receipt"]),
            _num(r["intervals"]["receipt_to_report"]),
            _num(r["intervals"]["order_to_report"]),
            r["target_minutes"] if r["target_minutes"] is not None else "",
            within, "Yes" if r["simulated"] else "No")])
    return {"filename": f"lab-turnaround-{first.isoformat()}-to-{last.isoformat()}.csv",
            "content_type": "text/csv",
            "rows": len(rows),
            "specimen_ids": sorted({r["specimen_id"] for r in rows if r["specimen_id"]}),
            "patient_ids": sorted({r["patient_id"] for r in rows if r["patient_id"]}),
            "csv": buffer.getvalue()}
