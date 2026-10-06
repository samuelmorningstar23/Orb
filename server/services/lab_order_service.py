"""Laboratory order intake: requisition lines, specimen planning and accession
numbers (lis-6).

Behind ORB_LAB (default off). With the module off, or draining, nothing here
runs and no laboratory row is written: a laboratory order stays exactly the
order it is today, with its free-text result on the Orders screen.

HOW AN ORDER REACHES THE LABORATORY
  * The hook. orders_service calls intake_after_activation() AFTER the commit
    that makes a laboratory order 'active' (created active, or moved from
    draft to active, which is how an order set's drafts are signed). It runs
    after the commit on purpose: a laboratory failure must never undo or delay
    the order itself. It never raises.
  * The sweep. ensure_intake() is the backstop for an order the hook missed
    (the process stopped between the commit and the hook, or the hook failed).
    services/lab_module.startup runs it once at boot while the module is on,
    and a background loop repeats it every INTAKE_SWEEP_SECONDS.
  Both call intake(), which is idempotent: it runs in one BEGIN IMMEDIATE
  transaction and does nothing when the order already has items, so a replay,
  a double activation or the hook racing the sweep all end with one set of
  items.

WHAT IS NEVER TAKEN IN
  * An order activated before the cutover (lab_module.current_enable_instant):
    months of historical laboratory orders never flood the collection lists,
    and an order activated while the module was off stays on the free-text
    path for good. No cutover (module never switched on) takes nothing.
  * A bedside test (performed_at 'ward_poct', ruling LIS-R18: capillary
    ketones, VBG, ABG): no items, no specimen, no exception. The order keeps
    the free-text path, because the DKA and asthma order sets repeat these
    hourly at the bedside. The classification is read when the order is
    activated, so a later reclassification applies only to orders activated
    afterwards (see the sweep rule below).
  * An order no longer 'active' (cancelled, or moved along by hand before the
    laboratory saw it) or that is not a laboratory order.

UNMAPPED CODES ARE NEVER SILENT. A code the catalogue does not hold, a test
switched off or not orderable on its own, a panel with no active component, or
a test with no specimen type: the order gets no items and one open
lab_exceptions row ('unmapped_code') the laboratory sees on its exceptions
list. The sweep does not retry an order with an open unmapped exception.

THE SWEEP RULE (why the sweep is stricter than the hook). The hook decides
with the catalogue as it stands at activation. The sweep runs later, so:
  * an order with an open 'intake_failed' exception (the hook tried and
    failed) is retried with the current catalogue, and the exception is
    resolved when the retry takes it in;
  * otherwise the sweep takes an order only when its catalogue entry has not
    changed since the order was activated. If it has changed, the sweep cannot
    know how the order was classified at activation (a bedside test since
    reclassified as a laboratory test, say), so it leaves the order where the
    hook left it. The order stays visible on the Orders screen either way.
  A retry that finds a bedside test or an unmapped code closes the open
  'intake_failed' entry, so the laboratory's list never keeps a dead one.
  Orders the sweep would leave alone never use up its batch (see
  ensure_intake), so a missed order is never starved behind them.

PANELS EXPLODE into one item per active analyte component (UNIQUE(order_id,
test_id)), each keeping the panel's id. The panel is collected as one tube:
its components take the PANEL's specimen type (a basic metabolic panel's
glucose comes from the panel's serum tube, not a separate fluoride tube),
falling back to the component's own when the panel names none. A component
that is itself a bedside test is left out.

SPECIMEN PLANNING. Items are grouped by specimen type, container and
priority (a STAT tube is never the routine tube). Each group merges into an
existing specimen only when all of these hold, else a new specimen is planned
with a new accession number:
  * same patient, specimen type, container and priority;
  * still 'planned', with label_print_count 0 and no label printed: a printed
    label lists its tests, so adding one would make the tube's label wrong
    (critique m16, hazard 1);
  * it does not already carry any of the group's tests. Two orders for the
    same test are two draws (a serial troponin, a second blood culture), or a
    duplicate order the ward should see, never one tube answering both.

AUDIT. Each intake writes one clinical_access event (fail-closed) inside its
transaction: an intake whose audit row cannot be written commits nothing, and
the hook records an 'intake_failed' exception for the sweep to retry.

Intake writes only lab_ tables and never changes orders.status.

THE ORDER LIFECYCLE (lis-7, below). An order with lab_order_items is the
laboratory's for life, whatever ORB_LAB says: orders_service refuses a manual
status change or free-text result on it (OrderManagedByLab, 409). Only this
module moves it, through orders_service.transition_in_conn and
set_lab_result_text_in_conn on its own transaction, and always through
reconcile_order_status, the completion rule (ruling LIS-R16, option a):
  * every item cancelled: the order is cancelled, so it never bills;
  * at least one item released and every other released or cancelled: the
    order is resulted, with the fixed LAB_RESULT_TEXT (no accession, no
    version, so an amendment never makes it stale);
  * a rejected item with no recollect or cancel decision: the order stays
    in_progress and the laboratory's list carries 'rejected_no_decision';
  * otherwise in_progress once any specimen is collected, else active.
  An order already moved outside the laboratory (a manual completion from
  before the guard) is never forced: the laboratory's own write still commits,
  and an 'order_state_conflict' exception and an error log say so.
cancel_order is the one way a ward or the laboratory cancels such an order:
it cancels every test not yet released, withdraws their unreleased results,
cancels specimens left with nothing on them and keeps released results.

COLLECTION (lis-8, below). The ward Samples view and the laboratory's
phlebotomy list read list_collection (department accounts see their own
department only). Labels print one patient at a time, only after the person
at the bedside has typed the patient's UHID and name and both match the
record (ruling LIS-R6, strictest option until it is confirmed), and each
print is counted (label_print_count, label_printed_at), which also closes the
tube to later merges. collect_specimen needs the same two identifiers and a
printed label, refuses back-dating while the site limit is 0 (the shipped
default) and never allows more than BACKDATE_CEILING_MINUTES, and writes the
specimen, its items, each order's move to in_progress, the order_events rows,
the specimen event and the clinical_access audit row in ONE immediate
transaction: an audit row that cannot be written leaves nothing collected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from database import db_session
from services import lab_accession, lab_module, orders_service
from services.audit_service import log_audit_event
from services.clinical_time import parse_to_utc, to_utc_iso, utc_iso_now

logger = logging.getLogger("orb.lab.intake")

#: How often the background sweep looks for an order the hook missed.
INTAKE_SWEEP_SECONDS = 300
#: The most orders one sweep takes, so a backlog never holds the write lock
#: for long; the next sweep takes the rest.
SWEEP_BATCH = 200
#: The most candidate orders one sweep reads (a read, no write lock) to find
#: its batch among orders it would leave alone. Far above any real ward load;
#: reaching it is logged.
SWEEP_SCAN_CAP = 5000

_INTAKE_TABLES = ("lab_tests", "lab_panel_components", "lab_specimen_types",
                  "lab_specimens", "lab_specimen_events", "lab_order_items",
                  "lab_exceptions", "lab_module_events", "lab_accession_counters")

#: lab_order_items statuses that no longer hold a test on a specimen.
_DEAD_ITEM_STATUSES = ("cancelled",)

# Exception kinds that stop the sweep from retrying an order.
_BLOCKING_EXCEPTION_KINDS = ("unmapped_code", "order_state_conflict")


class IntakeRefused(Exception):
    """Internal: the order maps to nothing the laboratory can collect. The
    message is the sentence the exceptions list shows."""


# ════════════════════════════════════════════════════════════════════════════
#  Small reads (take an open connection, never commit)
# ════════════════════════════════════════════════════════════════════════════

def _tables_present(conn) -> bool:
    placeholders = ", ".join("?" for _ in _INTAKE_TABLES)
    found = conn.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
        _INTAKE_TABLES).fetchone()[0]
    return int(found) == len(_INTAKE_TABLES)


def activation_instant(conn, order_id: str) -> Optional[str]:
    """When the order became 'active': its order_events row with new_status
    'active' (written by create_order for an order created active, and by
    transition_order for draft to active). An order can only enter 'active'
    once, so the first such row is the one."""
    row = conn.execute(
        "SELECT created_at FROM order_events WHERE order_id = ? AND new_status = 'active' "
        "ORDER BY id ASC LIMIT 1", (order_id,)).fetchone()
    return row[0] if row else None


def _instant(value: Optional[str]):
    """A stored instant as an aware UTC datetime, to the microsecond, or None.
    Compared in Python, never with julianday(), which keeps milliseconds only
    and would call an order activated a fraction of a millisecond before the
    cutover 'at the cutover'."""
    if not value:
        return None
    try:
        return parse_to_utc(value)
    except (TypeError, ValueError):
        return None


def _at_or_after(instant: Optional[str], cutover: Optional[str]) -> bool:
    """instant >= cutover. An unreadable instant is never at or after."""
    a, b = _instant(instant), _instant(cutover)
    return a is not None and b is not None and a >= b


def _changed_after(updated_at: Optional[str], activated_at: Optional[str]) -> bool:
    """Did the catalogue row change after the order was activated? An
    unreadable time reads as changed, so the sweep leaves the order alone."""
    a, b = _instant(updated_at), _instant(activated_at)
    return a is None or b is None or a > b


def _test(conn, test_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT id, code, code_system, name, short_name, kind, performed_at, section_id, "
        "specimen_type_code, orderable, active, updated_at FROM lab_tests WHERE id = ?",
        (test_id,)).fetchone()
    return dict(row) if row else None


def _components(conn, panel_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT t.id, t.name, t.short_name, t.kind, t.performed_at, t.section_id,
                  t.specimen_type_code, t.active
           FROM lab_panel_components c
           JOIN lab_tests t ON t.id = c.component_test_id
           WHERE c.panel_test_id = ?
           ORDER BY c.sort_order, c.component_test_id""", (panel_id,)).fetchall()]


def _specimen_type(conn, code: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT code, display, container, active FROM lab_specimen_types WHERE code = ?",
        (code,)).fetchone()
    return dict(row) if row else None


def _open_exception(conn, order_id: str, kinds: tuple[str, ...]) -> Optional[dict]:
    placeholders = ", ".join("?" for _ in kinds)
    row = conn.execute(
        f"SELECT id, kind, detail, created_at FROM lab_exceptions "
        f"WHERE order_id = ? AND kind IN ({placeholders}) AND resolved_at IS NULL "
        f"ORDER BY id LIMIT 1", (order_id, *kinds)).fetchone()
    return dict(row) if row else None


def _actor_role(conn, actor_id: Optional[str]) -> Optional[str]:
    if not actor_id:
        return None
    row = conn.execute("SELECT role FROM users WHERE id = ?", (actor_id,)).fetchone()
    return row[0] if row else None


# ════════════════════════════════════════════════════════════════════════════
#  Exceptions (the laboratory's own list)
# ════════════════════════════════════════════════════════════════════════════

def _add_exception(conn, kind: str, order_id: Optional[str], detail: str, *,
                   specimen_id: Optional[str] = None) -> Optional[int]:
    """One open row per order and kind; a repeat adds nothing. On the caller's
    transaction."""
    if order_id and _open_exception(conn, order_id, (kind,)):
        return None
    cur = conn.execute(
        "INSERT INTO lab_exceptions (kind, order_id, specimen_id, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (kind, order_id, specimen_id, detail[:1000], utc_iso_now()))
    return int(cur.lastrowid)


def record_exception(kind: str, order_id: Optional[str], detail: str) -> Optional[int]:
    """Write an exception in a transaction of its own, for a failure whose own
    transaction has already rolled back. Never raises: the failure it records
    has been logged already, and a second failure here must not hide it."""
    try:
        with db_session() as conn:
            if not _tables_present(conn):
                return None
            return _add_exception(conn, kind, order_id, detail)
    except Exception:
        logger.exception("Could not record laboratory exception %s for order %s", kind, order_id)
        return None


def list_open_exceptions(conn, *, dept: Optional[str], limit: int = 200) -> list[dict]:
    """The laboratory's open exceptions, oldest first, with the order and the
    patient they concern.

    `dept` is the caller's permissions.department_scope(user) and is required,
    so no caller can forget the ward scope. None (an account with no
    department on record, which is how the laboratory roles are seeded) reads
    the laboratory's hospital-wide list. A department keeps only the rows whose
    patient is in that department, by the same predicate patient_in_scope uses
    (permissions.scope_patient_ids_sql). That is fail-closed: a row with no
    order, or an order whose patient no ward owns, is not shown to a
    department account. The filter runs in SQL, before the LIMIT, so another
    ward's rows can never push this ward's own rows off the page."""
    if not _tables_present(conn):
        return []
    from permissions import scope_patient_ids_sql

    limit = max(1, min(int(limit or 200), 500))
    where = "x.resolved_at IS NULL"
    params: tuple = ()
    if dept is not None:
        scope_sql, scope_params = scope_patient_ids_sql(dept)
        where += f" AND o.patient_id IN ({scope_sql})"
        params = tuple(scope_params)
    rows = conn.execute(
        f"""SELECT x.id, x.kind, x.order_id, x.specimen_id, x.detail, x.created_at,
                  o.patient_id, o.code, o.code_system, o.description, o.priority,
                  o.status AS order_status, p.name AS patient_name, p.mrn
           FROM lab_exceptions x
           LEFT JOIN orders o ON o.id = x.order_id
           LEFT JOIN patients p ON p.id = o.patient_id
           WHERE {where}
           ORDER BY julianday(x.created_at) ASC, x.id ASC
           LIMIT ?""", params + (limit,)).fetchall()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
#  Resolving an order to tests
# ════════════════════════════════════════════════════════════════════════════

def _requisition(conn, order: dict) -> tuple[Optional[dict], list[dict]]:
    """(ordered test, lines). Lines are dicts of test_id, panel_test_id,
    section_id, specimen_type_code and container.

    Returns (test, []) for a bedside test. Raises IntakeRefused for a code the
    laboratory cannot collect, with the sentence for the exceptions list."""
    from services.lab_master_service import test_id_for

    code = (order.get("code") or "").strip()
    system = (order.get("code_system") or "").strip()
    if not code or not system:
        raise IntakeRefused("This laboratory order has no code, so the laboratory cannot "
                            "tell which test to run.")
    label = f"{system.upper()} {code}"
    test = _test(conn, test_id_for(system, code))
    if test is None:
        raise IntakeRefused(f"{label} is not in the laboratory catalogue.")
    if test["performed_at"] == "ward_poct":
        return test, []
    if not test["active"]:
        raise IntakeRefused(f"{label} ({test['name']}) is switched off in the laboratory "
                            "catalogue.")
    if not test["orderable"]:
        raise IntakeRefused(f"{label} ({test['name']}) is not orderable on its own in the "
                            "laboratory catalogue.")

    if test["kind"] == "panel":
        members = [c for c in _components(conn, test["id"])
                   if c["active"] and c["kind"] == "analyte" and c["performed_at"] == "lab"]
        if not members:
            raise IntakeRefused(f"The panel {test['name']} has no active laboratory component.")
        pairs = [(c, test) for c in members]
    else:
        pairs = [(test, None)]

    lines = []
    for member, panel in pairs:
        specimen_code = ((panel or {}).get("specimen_type_code")
                         or member.get("specimen_type_code"))
        if not specimen_code:
            raise IntakeRefused(f"{member['name']} has no specimen type in the laboratory "
                                "catalogue.")
        specimen = _specimen_type(conn, specimen_code)
        if specimen is None or not specimen["active"]:
            raise IntakeRefused(f"The specimen type {specimen_code} for {member['name']} is "
                                "not in the laboratory catalogue.")
        lines.append({
            "test_id": member["id"],
            "panel_test_id": panel["id"] if panel else None,
            "section_id": member.get("section_id") or (panel or {}).get("section_id"),
            "specimen_type_code": specimen_code,
            "container": specimen.get("container"),
        })
    return test, lines


# ════════════════════════════════════════════════════════════════════════════
#  Specimen planning
# ════════════════════════════════════════════════════════════════════════════

def _mergeable_specimen(conn, patient_id: str, specimen_type_code: str,
                        container: Optional[str], priority: str,
                        test_ids: list[str]) -> Optional[str]:
    """An existing planned, unlabelled specimen this group can join, or None."""
    test_marks = ", ".join("?" for _ in test_ids)
    dead_marks = ", ".join("?" for _ in _DEAD_ITEM_STATUSES)
    row = conn.execute(
        f"""SELECT s.id FROM lab_specimens s
            WHERE s.patient_id = ? AND s.specimen_type_code = ?
              AND COALESCE(s.container, '') = COALESCE(?, '')
              AND s.priority = ? AND s.status = 'planned'
              AND s.label_print_count = 0 AND s.label_printed_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM lab_order_items i
                  WHERE i.specimen_id = s.id AND i.test_id IN ({test_marks})
                    AND i.status NOT IN ({dead_marks}))
            ORDER BY julianday(s.created_at) DESC, s.id DESC
            LIMIT 1""",
        (patient_id, specimen_type_code, container, priority, *test_ids,
         *_DEAD_ITEM_STATUSES)).fetchone()
    return row[0] if row else None


def _specimen_event(conn, specimen_id: str, patient_id: str, event_type: str, *,
                    actor_id: Optional[str], actor_role: Optional[str], now: str,
                    detail: str, metadata: dict) -> None:
    seq = int(conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM lab_specimen_events WHERE specimen_id = ?",
        (specimen_id,)).fetchone()[0])
    conn.execute(
        """INSERT INTO lab_specimen_events
           (specimen_id, patient_id, seq, event_type, actor_user_id, actor_role,
            occurred_at, detail, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (specimen_id, patient_id, seq, event_type, actor_id, actor_role, now, detail,
         json.dumps(metadata, default=str)))


def _plan_specimens(conn, order: dict, lines: list[dict], *, actor_id: Optional[str],
                    actor_role: Optional[str], now: str) -> list[dict]:
    """Assign each line a specimen (merged or new). Returns one entry per
    specimen touched: {id, new, specimen_type_code, container, test_ids}."""
    priority = order["priority"] if order.get("priority") in ("stat", "urgent", "routine") \
        else "routine"
    groups: dict[tuple, list[dict]] = {}
    for line in lines:
        groups.setdefault((line["specimen_type_code"], line["container"] or ""), []).append(line)

    touched = []
    for (specimen_code, container_key), group in groups.items():
        container = group[0]["container"]
        test_ids = [g["test_id"] for g in group]
        specimen_id = _mergeable_specimen(conn, order["patient_id"], specimen_code,
                                          container, priority, test_ids)
        new = specimen_id is None
        if new:
            specimen_id = lab_accession.allocate(conn, at=now)
            conn.execute(
                """INSERT INTO lab_specimens
                   (id, patient_id, specimen_type_code, container, priority, status,
                    planned_at, label_print_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'planned', ?, 0, ?, ?)""",
                (specimen_id, order["patient_id"], specimen_code, container, priority,
                 now, now, now))
            _specimen_event(conn, specimen_id, order["patient_id"], "planned",
                            actor_id=actor_id, actor_role=actor_role, now=now,
                            detail="Specimen planned from a laboratory order",
                            metadata={"order_id": order["id"], "tests": test_ids})
        else:
            conn.execute("UPDATE lab_specimens SET updated_at = ? WHERE id = ?",
                         (now, specimen_id))
            _specimen_event(conn, specimen_id, order["patient_id"], "items_added",
                            actor_id=actor_id, actor_role=actor_role, now=now,
                            detail="Tests from another laboratory order added before the "
                                   "label was printed",
                            metadata={"order_id": order["id"], "tests": test_ids})
        for g in group:
            g["specimen_id"] = specimen_id
        touched.append({"id": specimen_id, "new": new, "specimen_type_code": specimen_code,
                        "container": container, "test_ids": test_ids})
    return touched


# ════════════════════════════════════════════════════════════════════════════
#  Intake
# ════════════════════════════════════════════════════════════════════════════

def _audit_intake(conn, order: dict, *, actor_id: Optional[str], actor_role: Optional[str],
                  outcome: str, summary: str, metadata: dict) -> None:
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_access",
        action_type="lab_order_intake",
        outcome=outcome,
        db=conn,
        summary_message=summary,
        actor_user_id=actor_id or "system",
        actor_role=actor_role or ("system" if not actor_id else None),
        target_type="order",
        target_id=str(order["id"]),
        patient_id=order.get("patient_id"),
        metadata=metadata,
    )


def _assess(conn, order_id: str, *, trigger: str) -> dict:
    """The intake decision for one order, with NO write, so the sweep can ask
    it on a plain read before taking the write lock and intake() asks it again
    under that lock (the answer under the lock is the one that counts).

    status is 'proceed' (plan specimens), 'unmapped' (record the refusal),
    'skipped' (a bedside test), or a final 'already' or 'not_eligible' with a
    reason. 'proceed', 'unmapped' and 'skipped' also carry the order, the
    open 'intake_failed' exception (or None) and, for 'proceed', the test and
    lines; those keys never leave this module."""
    if not _tables_present(conn):
        return {"status": "not_eligible", "reason": "tables_absent"}
    order = conn.execute(
        "SELECT id, patient_id, order_type, code, code_system, priority, status "
        "FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return {"status": "not_eligible", "reason": "not_found"}
    order = dict(order)
    if order["order_type"] != "lab":
        return {"status": "not_eligible", "reason": "not_lab"}
    if conn.execute("SELECT 1 FROM lab_order_items WHERE order_id = ? LIMIT 1",
                    (order_id,)).fetchone():
        return {"status": "already", "reason": "items_exist"}
    if order["status"] != "active":
        return {"status": "not_eligible", "reason": f"status_{order['status']}"}
    cutover = lab_module.current_enable_instant(conn)
    if cutover is None:
        return {"status": "not_eligible", "reason": "no_cutover"}
    activated_at = activation_instant(conn, order_id)
    if activated_at is None:
        return {"status": "not_eligible", "reason": "no_activation_event"}
    if not _at_or_after(activated_at, cutover):
        return {"status": "not_eligible", "reason": "before_cutover"}
    if _open_exception(conn, order_id, _BLOCKING_EXCEPTION_KINDS):
        return {"status": "not_eligible", "reason": "open_exception"}

    failed = _open_exception(conn, order_id, ("intake_failed",))
    try:
        test, lines = _requisition(conn, order)
    except IntakeRefused as refusal:
        return {"status": "unmapped", "reason": str(refusal), "order": order, "failed": failed}

    if (trigger == "sweep" and failed is None
            and _changed_after(test.get("updated_at"), activated_at)):
        # The catalogue entry changed after the order was activated: how the
        # hook classified it then cannot be read from the row now.
        return {"status": "not_eligible", "reason": "catalogue_changed_after_activation"}
    if not lines:
        return {"status": "skipped", "reason": "ward_poct", "order": order, "failed": failed}
    return {"status": "proceed", "order": order, "test": test, "lines": lines,
            "failed": failed}


def _sweep_should_write(decision: dict) -> bool:
    """Would intake() write anything for this decision? Only these orders are
    worth the sweep's write lock and a place in its batch; every other order
    stays exactly as it is however often the sweep asks."""
    status = decision.get("status")
    if status in ("proceed", "unmapped"):
        return True
    # A bedside test whose hook failed: the retry closes the stale exception.
    return status == "skipped" and decision.get("failed") is not None


def _resolve_exception(conn, exception_id: int, now: str, resolution: str) -> None:
    conn.execute(
        "UPDATE lab_exceptions SET resolved_by = 'system', resolved_at = ?, resolution = ? "
        "WHERE id = ? AND resolved_at IS NULL", (now, resolution, exception_id))


def _intake_in_conn(conn, order_id: str, *, actor_id: Optional[str], trigger: str) -> dict:
    decision = _assess(conn, order_id, trigger=trigger)
    if decision["status"] not in ("proceed", "unmapped", "skipped"):
        return decision
    order, failed = decision["order"], decision["failed"]
    actor_role = _actor_role(conn, actor_id)
    now = utc_iso_now()

    if decision["status"] == "unmapped":
        refusal = decision["reason"]
        _add_exception(conn, "unmapped_code", order_id, refusal)
        if failed is not None:
            # The retry found the real reason; one open entry per order is
            # enough for the laboratory's list.
            _resolve_exception(conn, failed["id"], now,
                               "Replaced by this order's entry for a code not in the "
                               "laboratory catalogue")
        _audit_intake(conn, order, actor_id=actor_id, actor_role=actor_role, outcome="failure",
                      summary="Laboratory order not taken in: its code maps to nothing the "
                              "laboratory collects",
                      metadata={"reason": "unmapped_code", "detail": refusal,
                                "trigger": trigger,
                                "resolved_exception": failed["id"] if failed else None})
        return {"status": "unmapped", "reason": refusal}

    if decision["status"] == "skipped":
        if failed is not None:
            _resolve_exception(conn, failed["id"], now,
                               "A bedside test: it stays on the ward and never reaches the "
                               "laboratory")
        return {"status": "skipped", "reason": "ward_poct"}

    test, lines = decision["test"], decision["lines"]
    specimens = _plan_specimens(conn, order, lines, actor_id=actor_id,
                                actor_role=actor_role, now=now)
    priority = order["priority"] if order["priority"] in ("stat", "urgent", "routine") \
        else "routine"
    for line in lines:
        conn.execute(
            """INSERT INTO lab_order_items
               (order_id, patient_id, test_id, panel_test_id, specimen_id, section_id,
                priority, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_collection', ?, ?)""",
            (order_id, order["patient_id"], line["test_id"], line["panel_test_id"],
             line["specimen_id"], line["section_id"], priority, now, now))
    if failed is not None:
        _resolve_exception(conn, failed["id"], now, "Taken into the laboratory on a later attempt")
    _audit_intake(
        conn, order, actor_id=actor_id, actor_role=actor_role, outcome="success",
        summary=(f"Laboratory order taken in: {test['name']}, {len(lines)} test(s) on "
                 f"{len(specimens)} specimen(s)"),
        metadata={"test_id": test["id"], "trigger": trigger,
                  "items": [line["test_id"] for line in lines],
                  "specimens": [{"id": s["id"], "new": s["new"],
                                 "specimen_type": s["specimen_type_code"]} for s in specimens],
                  "resolved_exception": failed["id"] if failed else None})
    return {"status": "taken", "items": len(lines),
            "specimens": [{"id": s["id"], "new": s["new"]} for s in specimens]}


def intake(order_id: str, *, actor_id: Optional[str] = None, trigger: str = "activation") -> dict:
    """Take one activated laboratory order into the laboratory, idempotently,
    in one BEGIN IMMEDIATE transaction. Returns {status, ...}; status is
    'taken', 'already', 'skipped' (bedside test), 'unmapped' or
    'not_eligible' (with a reason).

    Raises AccessionOverflow after recording an 'intake_failed' exception (its
    transaction has rolled back), and any unexpected error unrecorded."""
    if not lab_module.module_on():
        return {"status": "not_eligible", "reason": "module_not_on"}
    try:
        with db_session(immediate=True) as conn:
            return _intake_in_conn(conn, order_id, actor_id=actor_id, trigger=trigger)
    except lab_accession.AccessionOverflow as overflow:
        record_exception("intake_failed", order_id,
                         f"Not taken into the laboratory: {overflow}")
        raise


def intake_after_activation(order_id: str, *, actor_id: Optional[str] = None) -> Optional[dict]:
    """The post-commit hook orders_service calls when a laboratory order
    becomes active. Never raises: a failure is logged, recorded as an
    'intake_failed' exception, and left for the sweep."""
    try:
        return intake(order_id, actor_id=actor_id, trigger="activation")
    except lab_accession.AccessionOverflow:
        return {"status": "failed", "reason": "accession_overflow"}
    except Exception as exc:
        logger.exception("Laboratory intake failed for order %s; the sweep will retry it",
                         order_id)
        record_exception("intake_failed", order_id,
                         f"Not taken into the laboratory yet ({type(exc).__name__}). "
                         "Orb retries it automatically.")
        return {"status": "failed", "reason": type(exc).__name__}


def _sweep_candidates(conn, cutover: str, limit: int) -> list[str]:
    """Active laboratory orders with no items, activated around or after the
    cutover, oldest first. The SQL filter keeps a second of slack below the
    cutover (julianday() keeps milliseconds only); intake() then decides to
    the microsecond, so an order activated just before the cutover is refused
    there.

    A bedside test is left out here unless its hook failed: it stays 'active'
    with no items for as long as the ward keeps it open (the DKA and asthma
    sets repeat them hourly), and intake() would leave it alone every time.
    The id below is test_id_for() spelt in SQL; an order it does not match
    exactly stays in the list and intake() decides."""
    blocking = ", ".join("?" for _ in _BLOCKING_EXCEPTION_KINDS)
    rows = conn.execute(
        f"""SELECT o.id FROM orders o
            JOIN order_events e ON e.id = (
                SELECT MIN(e2.id) FROM order_events e2
                WHERE e2.order_id = o.id AND e2.new_status = 'active')
            WHERE o.order_type = 'lab' AND o.status = 'active'
              AND julianday(e.created_at) >= julianday(?) - 1.0 / 86400
              AND NOT EXISTS (SELECT 1 FROM lab_order_items i WHERE i.order_id = o.id)
              AND NOT EXISTS (SELECT 1 FROM lab_exceptions x
                              WHERE x.order_id = o.id AND x.resolved_at IS NULL
                                AND x.kind IN ({blocking}))
              AND NOT (
                  EXISTS (SELECT 1 FROM lab_tests t
                          WHERE t.id = upper(trim(o.code_system)) || ':' || trim(o.code)
                            AND t.performed_at = 'ward_poct')
                  AND NOT EXISTS (SELECT 1 FROM lab_exceptions f
                                  WHERE f.order_id = o.id AND f.kind = 'intake_failed'
                                    AND f.resolved_at IS NULL))
            ORDER BY julianday(e.created_at) ASC, o.id ASC
            LIMIT ?""",
        (cutover, *_BLOCKING_EXCEPTION_KINDS, limit)).fetchall()
    return [r[0] for r in rows]


def ensure_intake(*, limit: int = SWEEP_BATCH) -> dict:
    """The backstop sweep: take in every active laboratory order activated
    since the cutover that has no items yet, by the sweep rule in the module
    docstring. Sync and blocking (run_db or a worker thread). Never raises;
    returns counts by outcome.

    NO STARVATION. Orders intake() would leave alone (activated as a bedside
    test, a catalogue entry changed after activation) stay active with no
    items, so a plain oldest-first batch would fill with them and never reach
    an order the hook really missed. The sweep therefore asks each candidate
    the intake decision on a read first (counted as 'settled', no write lock
    taken) and spends its batch of `limit` only on orders intake() would
    write for. intake() decides again under its own lock."""
    summary: dict[str, Any] = {"considered": 0, "taken": 0, "skipped": 0, "unmapped": 0,
                               "not_eligible": 0, "already": 0, "failed": 0, "settled": 0}
    if not lab_module.module_on():
        summary["reason"] = "module_not_on"
        return summary
    batch = max(1, int(limit))
    candidates: list[str] = []
    try:
        with db_session() as conn:
            if not _tables_present(conn):
                summary["reason"] = "tables_absent"
                return summary
            cutover = lab_module.current_enable_instant(conn)
            if cutover is None:
                summary["reason"] = "no_cutover"
                return summary
            scanned = _sweep_candidates(conn, cutover, SWEEP_SCAN_CAP)
            if len(scanned) >= SWEEP_SCAN_CAP:
                logger.warning(
                    "Laboratory intake sweep found %d or more active laboratory orders with no "
                    "items since the cutover; it reads the oldest %d each time",
                    SWEEP_SCAN_CAP, SWEEP_SCAN_CAP)
            for order_id in scanned:
                try:
                    worth_it = _sweep_should_write(_assess(conn, order_id, trigger="sweep"))
                except Exception:
                    # One unreadable order never stops the sweep: intake()
                    # tries it under its lock and records any failure.
                    logger.warning("Laboratory intake sweep could not assess order %s",
                                   order_id, exc_info=True)
                    worth_it = True
                if worth_it:
                    candidates.append(order_id)
                    if len(candidates) >= batch:
                        break
                else:
                    summary["settled"] += 1
    except Exception:
        logger.exception("Laboratory intake sweep could not list its candidates")
        summary["reason"] = "list_failed"
        return summary
    for order_id in candidates:
        summary["considered"] += 1
        try:
            outcome = intake(order_id, trigger="sweep")
        except lab_accession.AccessionOverflow:
            summary["failed"] += 1
            continue
        except Exception as exc:
            logger.exception("Laboratory intake sweep failed for order %s", order_id)
            record_exception("intake_failed", order_id,
                             f"Not taken into the laboratory yet ({type(exc).__name__}). "
                             "Orb retries it automatically.")
            summary["failed"] += 1
            continue
        summary[outcome.get("status", "not_eligible")] = \
            summary.get(outcome.get("status", "not_eligible"), 0) + 1
    if summary["taken"] or summary["failed"]:
        logger.info("Laboratory intake sweep: %s", summary)
    return summary


async def _intake_sweep_loop() -> None:
    """Repeat the sweep while the module is on (registered with lab_module,
    started at boot only in the 'on' state). Startup has already swept once,
    so it waits first."""
    from database import run_db

    while True:
        await asyncio.sleep(INTAKE_SWEEP_SECONDS)
        try:
            await run_db(ensure_intake)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Laboratory intake sweep failed; will retry", exc_info=True)


lab_module.register_loop("intake-sweep", _intake_sweep_loop, states=("on",))


# ════════════════════════════════════════════════════════════════════════════
#  Refusals the routes turn into HTTP answers (lis-7, lis-8)
# ════════════════════════════════════════════════════════════════════════════

class LabOrderError(Exception):
    """A refusal with a machine code, a sentence for the screen, the HTTP
    status the route answers with, and any extra fields for the body."""

    def __init__(self, code: str, message: str, *, status: int = 409, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


class _IdentityRefused(Exception):
    """Internal: the two identifiers did not both match. Raised inside the
    write transaction so it rolls back; the caller audits the refusal on a
    transaction of its own and raises LabOrderError."""

    def __init__(self, checks: dict, patient_id: str, specimen_ids: list[str]):
        super().__init__("identity not confirmed")
        self.checks = checks
        self.patient_id = patient_id
        self.specimen_ids = specimen_ids


def _not_found(what: str = "Specimen") -> LabOrderError:
    # The same answer for "no such thing" and "not on your ward", so the
    # refusal confirms nothing (the scoping law: 404, never 403).
    return LabOrderError("not_found", f"{what} not found", status=404)


def _placeholders(values) -> str:
    return ", ".join("?" for _ in values)


def _chunks(values: list, size: int = 400):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _audit(conn, *, category: str, action: str, outcome: str, summary: str,
           actor: dict, target_type: str, target_id: Any, patient_id: Optional[str],
           metadata: dict, request_meta: Optional[dict]) -> None:
    """One audit row on the caller's transaction (db=conn), so the change and
    its record land together or not at all. target_id is always text."""
    meta = dict(request_meta or {})
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category=category,
        action_type=action,
        outcome=outcome,
        db=conn,
        summary_message=summary,
        actor_user_id=(actor or {}).get("id"),
        actor_role=(actor or {}).get("role"),
        target_type=target_type,
        target_id=str(target_id),
        patient_id=patient_id,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata=metadata,
    )


def _scope_metadata(dept: Optional[str]) -> dict:
    """What permissions.log_phi_read records about the reader's scope."""
    return {"department": dept, "hospital_wide": dept is None,
            "scope_source": "authenticated_user"}


# ════════════════════════════════════════════════════════════════════════════
#  The order lifecycle (lis-7)
# ════════════════════════════════════════════════════════════════════════════

#: What a laboratory-reported order says on the Orders screen. Fixed: no
#: accession and no version, so an amended laboratory result never makes it
#: stale and one order spanning several tubes reads the same.
LAB_RESULT_TEXT = ("Reported in the laboratory system. Open the laboratory report "
                   "for values.")

#: Item statuses that mean a specimen has been taken from the patient.
_COLLECTED_OR_LATER = ("collected", "received", "resulted", "tech_validated", "released",
                       "rejected")
#: Item statuses a cancellation leaves alone.
_FINISHED_ITEM_STATUSES = ("released", "cancelled")
#: Result statuses not yet released, which a cancellation withdraws.
_UNRELEASED_RESULT_STATUSES = ("entered", "tech_validated")
#: Order statuses the CPOE state machine never leaves.
_TERMINAL_ORDER_STATUSES = ("completed", "resulted", "cancelled")
#: Specimen statuses a cancellation leaves as they are: already cancelled, or
#: a rejection, which stays on the record as what happened.
_SPECIMEN_KEEP_ON_CANCEL = ("cancelled", "rejected")

#: Each item status in the ward's words, and the order the summary lists them.
WARD_ITEM_WORDS = {
    "released": "Reported",
    "received": "In the laboratory",
    "resulted": "In the laboratory",
    "tech_validated": "In the laboratory",
    "collected": "Collected",
    "awaiting_collection": "Waiting for collection",
    "rejected": "Rejected, recollection needed",
    "cancelled": "Cancelled",
}
_SUMMARY_ORDER = ("Reported", "In the laboratory", "Collected", "Waiting for collection",
                  "Rejected, recollection needed", "Cancelled")

_INTAKE_WORDS = {
    "unmapped_code": "The laboratory could not take this order in: its code is not in the "
                     "laboratory catalogue. The laboratory has been told.",
    "intake_failed": "Not taken into the laboratory yet. Orb retries it automatically; if "
                     "this stays, call the laboratory.",
}


def order_target_status(item_statuses: list[str]) -> Optional[str]:
    """The completion rule (ruling LIS-R16, option a): what an order's status
    should be, given its items' statuses. None for an order with no items.
    Pure."""
    if not item_statuses:
        return None
    if all(s == "cancelled" for s in item_statuses):
        return "cancelled"
    if any(s == "rejected" for s in item_statuses):
        # A rejected tube blocks completion until it is recollected or cancelled.
        return "in_progress"
    if (any(s == "released" for s in item_statuses)
            and all(s in _FINISHED_ITEM_STATUSES for s in item_statuses)):
        return "resulted"
    if any(s in _COLLECTED_OR_LATER for s in item_statuses):
        return "in_progress"
    return "active"


def _sync_rejected_exception(conn, order_id: str, *, rejected: bool, actor_id: Optional[str],
                             now: str) -> None:
    """Keep the laboratory's 'Rejected, no decision' entry true: one open row
    while the order has a rejected item, closed once none is left."""
    if rejected:
        _add_exception(conn, "rejected_no_decision", order_id,
                       "A specimen for this order was rejected. Recollect it or cancel the test.")
        return
    conn.execute(
        "UPDATE lab_exceptions SET resolved_by = ?, resolved_at = ?, resolution = ? "
        "WHERE order_id = ? AND kind = 'rejected_no_decision' AND resolved_at IS NULL",
        (actor_id or "system", now, "Recollected or cancelled", order_id))


def reconcile_order_status(conn, order_id: str, *, actor_id: Optional[str] = None,
                           reason: Optional[str] = None) -> dict:
    """Bring orders.status in line with the order's laboratory items, on the
    caller's write transaction. Never raises because of the order's state: a
    laboratory step (collection, release, cancellation) must commit even when
    the order was moved outside the laboratory, which is then recorded as an
    open 'order_state_conflict' exception and logged as an error.

    Returns {previous, status, target, changed, conflict}."""
    order = conn.execute(
        "SELECT id, status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return {"previous": None, "status": None, "target": None, "changed": False,
                "conflict": False}
    current = order["status"]
    statuses = [r[0] for r in conn.execute(
        "SELECT status FROM lab_order_items WHERE order_id = ?", (order_id,)).fetchall()]
    target = order_target_status(statuses)
    out = {"previous": current, "status": current, "target": target, "changed": False,
           "conflict": False}
    if target is None:
        return out
    now = utc_iso_now()
    _sync_rejected_exception(conn, order_id, rejected="rejected" in statuses,
                             actor_id=actor_id, now=now)
    if target == current:
        return out
    if target == "active":
        # Never backwards: an order in progress stays in progress.
        return out
    if current in _TERMINAL_ORDER_STATUSES:
        return _order_state_conflict(conn, order_id, out, current, target)
    try:
        if target == "resulted":
            moved = orders_service.set_lab_result_text_in_conn(
                conn, order_id, LAB_RESULT_TEXT, actor_id=actor_id,
                detail=reason or "reported by the laboratory")
        else:
            moved = orders_service.transition_in_conn(
                conn, order_id, target, actor_id=actor_id, detail=reason)
    except ValueError:
        return _order_state_conflict(conn, order_id, out, current, target)
    out.update(status=moved["status"], changed=True)
    return out


def _order_state_conflict(conn, order_id: str, out: dict, current: str, target: str) -> dict:
    logger.error(
        "Laboratory order %s is '%s' on the Orders screen but its laboratory tests say "
        "'%s'; it was changed outside the laboratory. The laboratory's own change stands.",
        order_id, current, target)
    _add_exception(
        conn, "order_state_conflict", order_id,
        f"The order shows '{current.replace('_', ' ')}' on the Orders screen, but its "
        f"laboratory tests say '{target.replace('_', ' ')}'. It was changed outside the "
        "laboratory; check it.")
    out["conflict"] = True
    return out


def cancel_order(order_id: str, *, actor: dict, reason: str, dept: Optional[str],
                 request_meta: Optional[dict] = None) -> dict:
    """Cancel an order the laboratory manages, whoever asks (the ward with
    order.write, or the laboratory with lab.receive), in ONE immediate
    transaction:
      * every test not yet released is cancelled, with the reason and who;
      * its entered or technically validated results are withdrawn;
      * a specimen left with no live test is cancelled (a rejected one keeps
        its rejection), so the bench stops; one that keeps tests records
        which were cancelled;
      * released results are kept, and the order is reconciled: all
        cancelled gives cancelled (never billed), some released gives
        resulted;
      * the clinical_access audit row lands in the same transaction.
    Served while the module drains (ruling LIS-R21): finishing work includes
    cancelling it. `dept` is the caller's department_scope (None means
    hospital-wide); an order outside it is 'not found'."""
    from permissions import patient_in_scope

    reason = (reason or "").strip()
    if not reason:
        raise LabOrderError("reason_required", "Say why the laboratory order is cancelled.",
                            status=422)
    reason = reason[:500]
    actor_id = (actor or {}).get("id")
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabOrderError("not_lab_managed", "The laboratory does not manage this order. "
                                "Cancel it from the Orders screen.")
        order = conn.execute(
            "SELECT id, patient_id, order_type, description, status FROM orders WHERE id = ?",
            (order_id,)).fetchone()
        if order is None or not patient_in_scope(conn, order["patient_id"], dept):
            raise _not_found("Order")
        order = dict(order)
        items = [dict(r) for r in conn.execute(
            "SELECT id, test_id, specimen_id, status FROM lab_order_items WHERE order_id = ? "
            "ORDER BY id", (order_id,)).fetchall()]
        if not items:
            raise LabOrderError("not_lab_managed", "The laboratory does not manage this order. "
                                "Cancel it from the Orders screen.")
        open_items = [i for i in items if i["status"] not in _FINISHED_ITEM_STATUSES]
        if not open_items:
            if all(i["status"] == "cancelled" for i in items):
                # A replay of a cancellation that already happened.
                return {"order": dict(conn.execute("SELECT * FROM orders WHERE id = ?",
                                                   (order_id,)).fetchone()),
                        "already_cancelled": True, "cancelled_items": 0,
                        "withdrawn_results": 0, "kept_released": 0,
                        "cancelled_specimens": []}
            raise LabOrderError("nothing_to_cancel", "Every test on this order has been "
                                "reported. There is nothing left to cancel.")

        now = utc_iso_now()
        item_ids = [i["id"] for i in open_items]
        conn.execute(
            f"UPDATE lab_order_items SET status = 'cancelled', cancelled_reason = ?, "
            f"cancelled_by = ?, updated_at = ? WHERE id IN ({_placeholders(item_ids)})",
            (reason, actor_id, now, *item_ids))

        withdrawn = [dict(r) for r in conn.execute(
            f"SELECT id, status, patient_id FROM lab_results "
            f"WHERE order_item_id IN ({_placeholders(item_ids)}) "
            f"AND status IN ({_placeholders(_UNRELEASED_RESULT_STATUSES)})",
            (*item_ids, *_UNRELEASED_RESULT_STATUSES)).fetchall()]
        for result in withdrawn:
            conn.execute("UPDATE lab_results SET status = 'withdrawn', amend_reason = ? "
                         "WHERE id = ?", ("Order cancelled", result["id"]))
            conn.execute(
                "INSERT INTO lab_result_events (result_id, patient_id, event_type, actor, "
                "occurred_at, before_json, after_json) VALUES (?, ?, 'withdrawn', ?, ?, ?, ?)",
                (result["id"], result["patient_id"], actor_id, now,
                 json.dumps({"status": result["status"]}),
                 json.dumps({"status": "withdrawn", "reason": "Order cancelled",
                             "cancel_reason": reason})))

        actor_role = (actor or {}).get("role")
        cancelled_specimens: list[str] = []
        for specimen_id in sorted({i["specimen_id"] for i in open_items if i["specimen_id"]}):
            specimen = conn.execute("SELECT status, patient_id FROM lab_specimens WHERE id = ?",
                                    (specimen_id,)).fetchone()
            if specimen is None:
                continue
            live = int(conn.execute(
                "SELECT COUNT(*) FROM lab_order_items WHERE specimen_id = ? "
                "AND status != 'cancelled'", (specimen_id,)).fetchone()[0])
            tests = [i["test_id"] for i in open_items if i["specimen_id"] == specimen_id]
            if live == 0 and specimen["status"] not in _SPECIMEN_KEEP_ON_CANCEL:
                conn.execute("UPDATE lab_specimens SET status = 'cancelled', updated_at = ? "
                             "WHERE id = ?", (now, specimen_id))
                cancelled_specimens.append(specimen_id)
                event, words = "cancelled", "Specimen cancelled: every test on it was cancelled"
            else:
                event, words = "items_cancelled", "Tests on this specimen were cancelled"
            _specimen_event(conn, specimen_id, specimen["patient_id"], event,
                            actor_id=actor_id, actor_role=actor_role, now=now,
                            detail=f"{words}. Reason: {reason}",
                            metadata={"order_id": order_id, "tests": tests, "reason": reason})

        reconciled = reconcile_order_status(
            conn, order_id, actor_id=actor_id,
            reason=f"Cancelled through the laboratory request: {reason}")
        kept = sum(1 for i in items if i["status"] == "released")
        _audit(conn, category="clinical_access", action="lab_order_cancelled",
               outcome="success",
               summary=(f"Laboratory order cancelled: {len(open_items)} test(s) cancelled, "
                        f"{kept} reported test(s) kept"),
               actor=actor, target_type="order", target_id=order_id,
               patient_id=order["patient_id"],
               metadata={"reason": reason, "cancelled_items": [i["test_id"] for i in open_items],
                         "withdrawn_results": [r["id"] for r in withdrawn],
                         "kept_released": kept, "cancelled_specimens": cancelled_specimens,
                         "order_status": {"from": reconciled["previous"],
                                          "to": reconciled["status"]},
                         "conflict": reconciled["conflict"]},
               request_meta=request_meta)
        after = dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
    # Cancelling closes open work; a draining module may now be finished.
    lab_module.refresh_drain_state()
    return {"order": after, "already_cancelled": False, "cancelled_items": len(open_items),
            "withdrawn_results": len(withdrawn), "kept_released": kept,
            "cancelled_specimens": cancelled_specimens}


def _summary_text(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    parts = [f"{counts[w]} {w.lower()}" for w in _SUMMARY_ORDER if counts.get(w)]
    noun = "test" if total == 1 else "tests"
    return f"{total} {noun}: " + ", ".join(parts) if parts else f"{total} {noun}"


def annotate_orders(conn, orders: list[dict]) -> list[dict]:
    """Add the laboratory's view to each order of a list, in place.

    Every order gains lab_managed (does the laboratory manage it, keyed on
    lab_order_items, never on ORB_LAB). A managed order gains `lab`: its
    tests with their status in the ward's words, counts, a one-line summary,
    the number rejected, and who cancelled it when every test was cancelled.
    A laboratory order the laboratory could not take in gains `lab_intake`
    with the reason in plain words. A database without the laboratory tables
    marks nothing managed."""
    for order in orders:
        order["lab_managed"] = False
    if not orders or not _tables_present(conn):
        return orders
    by_id = {o["id"]: o for o in orders}
    lab_ids = [o["id"] for o in orders if o.get("order_type") == "lab"]
    if not lab_ids:
        return orders
    items: dict[str, list[dict]] = {}
    for chunk in _chunks(lab_ids):
        for r in conn.execute(
                f"""SELECT i.order_id, i.id, i.test_id, i.status, i.specimen_id,
                           i.cancelled_reason, i.updated_at, t.name, t.short_name,
                           u.name AS cancelled_by_name
                    FROM lab_order_items i
                    LEFT JOIN lab_tests t ON t.id = i.test_id
                    LEFT JOIN users u ON u.id = i.cancelled_by
                    WHERE i.order_id IN ({_placeholders(chunk)})
                    ORDER BY i.order_id, i.id""", tuple(chunk)).fetchall():
            items.setdefault(r["order_id"], []).append(dict(r))
    for order_id, rows in items.items():
        counts: dict[str, int] = {}
        tests = []
        for r in rows:
            words = WARD_ITEM_WORDS.get(r["status"], "In the laboratory")
            counts[words] = counts.get(words, 0) + 1
            tests.append({"test_id": r["test_id"], "name": r["name"] or r["test_id"],
                          "short_name": r["short_name"], "status": r["status"],
                          "status_text": words, "specimen_id": r["specimen_id"]})
        cancelled = None
        if rows and all(r["status"] == "cancelled" for r in rows):
            latest = max(rows, key=lambda r: (r["updated_at"] or "", r["id"]))
            cancelled = {"by_name": latest["cancelled_by_name"], "at": latest["updated_at"],
                         "reason": latest["cancelled_reason"]}
        order = by_id[order_id]
        order["lab_managed"] = True
        order["lab"] = {"tests": tests, "counts": counts, "summary": _summary_text(counts),
                        "rejected": sum(1 for r in rows if r["status"] == "rejected"),
                        "cancelled": cancelled}
    unmanaged = [oid for oid in lab_ids if oid not in items]
    for chunk in _chunks(unmanaged):
        for r in conn.execute(
                f"""SELECT order_id, kind FROM lab_exceptions
                    WHERE order_id IN ({_placeholders(chunk)}) AND resolved_at IS NULL
                      AND kind IN ('unmapped_code', 'intake_failed')
                    ORDER BY id""", tuple(chunk)).fetchall():
            order = by_id.get(r["order_id"])
            if order is not None and "lab_intake" not in order:
                order["lab_intake"] = {"kind": r["kind"], "message": _INTAKE_WORDS[r["kind"]]}
    return orders


# ════════════════════════════════════════════════════════════════════════════
#  Collection (lis-8)
# ════════════════════════════════════════════════════════════════════════════

#: The longest a collection may ever be back-dated, whatever the site sets
#: (ruling LIS-R6 recommends up to 2 hours with a reason). The site setting
#: config.LAB_COLLECTION_BACKDATE_MINUTES ships at 0, which refuses any
#: back-dating until Samuel confirms the ruling.
BACKDATE_CEILING_MINUTES = 120
#: Clock difference tolerated for a collection time a little in the future.
_FUTURE_SKEW = timedelta(seconds=60)
#: The most specimens one label print may cover.
MAX_LABELS_PER_PRINT = 20
#: The most rows the collection list returns.
COLLECTION_LIST_CAP = 500

IDENTITY_MATCH = "match"
IDENTITY_NO_MATCH = "no_match"
IDENTITY_NOT_GIVEN = "not_given"
IDENTITY_NOT_ON_RECORD = "not_on_record"

_PRIORITY_RANK_SQL = "CASE s.priority WHEN 'stat' THEN 0 WHEN 'urgent' THEN 1 ELSE 2 END"


def backdate_limit_minutes() -> int:
    """How far back a collection time may be set at this site: the site
    setting, never above BACKDATE_CEILING_MINUTES. 0 (the shipped default)
    refuses any back-dating."""
    import config
    try:
        value = int(getattr(config, "LAB_COLLECTION_BACKDATE_MINUTES", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(value, BACKDATE_CEILING_MINUTES))


def _uhid_key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _name_key(value: Any) -> list[str]:
    return sorted(re.findall(r"[0-9a-z]+", str(value or "").lower()))


def collection_identity_checks(patient: dict, identity: Optional[dict]) -> dict[str, str]:
    """The two identifiers the person at the bedside typed, against the
    record: the UHID (patients.mrn, spaces and case ignored) and the name
    (case, punctuation and word order ignored). Pure. Each is 'match',
    'no_match', 'not_given' or, for a record with no UHID, 'not_on_record'.
    No stored value is ever returned."""
    identity = identity if isinstance(identity, dict) else {}
    out: dict[str, str] = {}
    typed_uhid = _uhid_key(identity.get("uhid"))
    record_uhid = _uhid_key(patient.get("mrn"))
    if not typed_uhid:
        out["uhid"] = IDENTITY_NOT_GIVEN
    elif not record_uhid:
        out["uhid"] = IDENTITY_NOT_ON_RECORD
    else:
        out["uhid"] = IDENTITY_MATCH if typed_uhid == record_uhid else IDENTITY_NO_MATCH
    typed_name = _name_key(identity.get("name"))
    if not typed_name:
        out["name"] = IDENTITY_NOT_GIVEN
    else:
        out["name"] = (IDENTITY_MATCH if typed_name == _name_key(patient.get("name"))
                       else IDENTITY_NO_MATCH)
    return out


def identity_confirmed(checks: dict[str, str]) -> bool:
    """Both identifiers given and both match the record."""
    return checks.get("uhid") == IDENTITY_MATCH and checks.get("name") == IDENTITY_MATCH


def _require_identity_payload(identity: Any) -> dict:
    if not isinstance(identity, dict) or not any(
            str(identity.get(k) or "").strip() for k in ("uhid", "name")):
        raise LabOrderError(
            "identity_required",
            "Check two identifiers first: type the UHID from the wristband and the "
            "patient's name.", status=422)
    return identity


def _audit_identity_refusal(refusal: _IdentityRefused, *, actor: dict, step: str,
                            request_meta: Optional[dict]) -> None:
    """Record a refused identity check, on a transaction of its own (the
    refused step's transaction has rolled back). Fail-closed: if this row
    cannot be written, AuditWriteError reaches the route."""
    with db_session() as conn:
        _audit(conn, category="clinical_access", action="lab_collection_identity_refused",
               outcome="failure",
               summary=f"Specimen {step} refused: the two identifiers did not both match",
               actor=actor, target_type="lab_specimen",
               target_id=",".join(refusal.specimen_ids)[:200],
               patient_id=refusal.patient_id,
               metadata={"step": step, "checks": refusal.checks,
                         "specimens": refusal.specimen_ids},
               request_meta=request_meta)


def _identity_refusal_error(checks: dict) -> LabOrderError:
    return LabOrderError(
        "identity_not_confirmed",
        "The identifiers do not both match this patient's record. Check the wristband "
        "and ask the patient again. Nothing was printed or recorded.",
        status=409, checks=checks)


def _collection_rows(conn, where: str, params: tuple, *, limit: int) -> list[dict]:
    from permissions import PATIENT_DEPARTMENT_SQL

    return [dict(r) for r in conn.execute(
        f"""SELECT s.id, s.patient_id, s.specimen_type_code, s.container, s.priority,
                   s.status, s.planned_at, s.label_print_count, s.label_printed_at,
                   s.rejection_code, s.rejection_note, s.created_at,
                   st.display AS specimen_display, st.container_colour_name,
                   p.name AS patient_name, p.mrn, p.bed, p.ward,
                   {PATIENT_DEPARTMENT_SQL} AS department
            FROM lab_specimens s
            JOIN patients p ON p.id = s.patient_id
            LEFT JOIN lab_specimen_types st ON st.code = s.specimen_type_code
            WHERE {where}
            ORDER BY {_PRIORITY_RANK_SQL}, COALESCE(p.ward, ''), COALESCE(p.bed, ''),
                     p.name COLLATE NOCASE, julianday(s.planned_at), s.id
            LIMIT ?""", params + (limit,)).fetchall()]


def _specimen_tests(conn, specimen_ids: list[str], statuses: tuple[str, ...]) -> dict:
    out: dict[str, list[dict]] = {}
    for chunk in _chunks(specimen_ids):
        for r in conn.execute(
                f"""SELECT i.specimen_id, i.id AS item_id, i.order_id, i.test_id, i.status,
                           t.name, t.short_name
                    FROM lab_order_items i
                    LEFT JOIN lab_tests t ON t.id = i.test_id
                    WHERE i.specimen_id IN ({_placeholders(chunk)})
                      AND i.status IN ({_placeholders(statuses)})
                    ORDER BY i.id""", (*chunk, *statuses)).fetchall():
            out.setdefault(r["specimen_id"], []).append({
                "item_id": r["item_id"], "order_id": r["order_id"], "test_id": r["test_id"],
                "status": r["status"], "name": r["name"] or r["test_id"],
                "short_name": r["short_name"] or r["name"] or r["test_id"]})
    return out


def _collection_entry(row: dict, tests: list[dict]) -> dict:
    return {
        "id": row["id"],
        "patient": {"id": row["patient_id"], "name": row["patient_name"], "mrn": row["mrn"],
                    "bed": row["bed"], "ward": row["ward"], "department": row["department"]},
        "specimen_type_code": row["specimen_type_code"],
        "specimen_display": row["specimen_display"] or row["specimen_type_code"],
        "container": row["container"],
        "container_colour_name": row["container_colour_name"],
        "priority": row["priority"],
        "status": row["status"],
        "planned_at": row["planned_at"],
        "label_print_count": int(row["label_print_count"] or 0),
        "label_printed_at": row["label_printed_at"],
        "recollection_needed": row["status"] == "rejected",
        "rejection_code": row["rejection_code"],
        "rejection_note": row["rejection_note"],
        "tests": tests,
    }


def list_collection(conn, *, dept: Optional[str], limit: int = COLLECTION_LIST_CAP) -> list[dict]:
    """Specimens waiting to be collected, STAT first, then by ward, bed and
    patient, plus rejected specimens whose tests wait for a recollection
    ('Recollection needed', informational until lis-9 builds the decision).

    `dept` is the caller's permissions.department_scope(user) and is required:
    a department account (a ward nurse or doctor) sees only patients in its
    department, by the same predicate as patient_in_scope, filtered in SQL
    before the LIMIT; None (the laboratory and phlebotomy roles) is the
    hospital-wide phlebotomy list."""
    if not _tables_present(conn):
        return []
    from permissions import scope_patient_ids_sql

    limit = max(1, min(int(limit or COLLECTION_LIST_CAP), COLLECTION_LIST_CAP))
    where = ("((s.status = 'planned' AND EXISTS (SELECT 1 FROM lab_order_items i "
             "WHERE i.specimen_id = s.id AND i.status = 'awaiting_collection')) "
             "OR (s.status = 'rejected' AND EXISTS (SELECT 1 FROM lab_order_items i "
             "WHERE i.specimen_id = s.id AND i.status = 'rejected')))")
    params: tuple = ()
    if dept is not None:
        scope_sql, scope_params = scope_patient_ids_sql(dept)
        where += f" AND s.patient_id IN ({scope_sql})"
        params = tuple(scope_params)
    rows = _collection_rows(conn, where, params, limit=limit)
    tests = _specimen_tests(conn, [r["id"] for r in rows], ("awaiting_collection", "rejected"))
    return [_collection_entry(r, tests.get(r["id"], [])) for r in rows]


def _scoped_specimens(conn, specimen_ids: list[str], dept: Optional[str]) -> list[dict]:
    """The named specimens with their patient, or 404 when any is missing or
    outside the caller's department (one answer for both)."""
    from permissions import patient_in_scope

    rows = _collection_rows(conn, f"s.id IN ({_placeholders(specimen_ids)})",
                            tuple(specimen_ids), limit=len(specimen_ids))
    found = {r["id"]: r for r in rows}
    out = []
    for specimen_id in specimen_ids:
        row = found.get(specimen_id)
        if row is None or not patient_in_scope(conn, row["patient_id"], dept):
            raise _not_found()
        out.append(row)
    return out


def _awaiting(conn, specimen_id: str) -> list[dict]:
    return _specimen_tests(conn, [specimen_id], ("awaiting_collection",)).get(specimen_id, [])


def print_labels(specimen_ids: list, *, actor: dict, identity: Any, dept: Optional[str],
                 request_meta: Optional[dict] = None) -> dict:
    """Print the labels for one patient's planned specimens, after the two
    identifiers match (ruling LIS-R6, strictest option: confirm on screen
    before labels print, and no batch pre-printing until the ruling is
    confirmed).

    Each print is counted on the specimen (label_print_count, and
    label_printed_at the first time), which also closes the tube to later
    merges, writes a 'label_printed' specimen event, and is audited as
    phi_access 'lab_labels_printed' in the same transaction (a label carries
    the name and UHID). Returns {labels, printed_at}."""
    ids = []
    for value in specimen_ids or []:
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)
    if not ids:
        raise LabOrderError("no_specimens", "Choose the samples to print labels for.",
                            status=422)
    if len(ids) > MAX_LABELS_PER_PRINT:
        raise LabOrderError("too_many_specimens",
                            f"Print at most {MAX_LABELS_PER_PRINT} labels at a time.", status=422)
    identity = _require_identity_payload(identity)
    try:
        with db_session(immediate=True) as conn:
            if not _tables_present(conn):
                raise _not_found()
            rows = _scoped_specimens(conn, ids, dept)
            patients = {r["patient_id"] for r in rows}
            if len(patients) != 1:
                raise LabOrderError("one_patient_at_a_time",
                                    "Print one patient's labels at a time, at the bedside.",
                                    status=422)
            for row in rows:
                if row["status"] != "planned":
                    raise LabOrderError("specimen_not_planned",
                                        "This sample is no longer waiting for collection.",
                                        specimen_id=row["id"], specimen_status=row["status"])
            awaiting = {r["id"]: _awaiting(conn, r["id"]) for r in rows}
            for row in rows:
                if not awaiting[row["id"]]:
                    raise LabOrderError("nothing_to_collect",
                                        "Every test on this sample was cancelled.",
                                        specimen_id=row["id"])
            patient = {"name": rows[0]["patient_name"], "mrn": rows[0]["mrn"]}
            checks = collection_identity_checks(patient, identity)
            if not identity_confirmed(checks):
                raise _IdentityRefused(checks, rows[0]["patient_id"], ids)

            now = utc_iso_now()
            actor_id = (actor or {}).get("id")
            labels = []
            for row in rows:
                print_number = int(row["label_print_count"] or 0) + 1
                conn.execute(
                    "UPDATE lab_specimens SET label_print_count = ?, "
                    "label_printed_at = COALESCE(label_printed_at, ?), updated_at = ? "
                    "WHERE id = ? AND status = 'planned'",
                    (print_number, now, now, row["id"]))
                _specimen_event(conn, row["id"], row["patient_id"], "label_printed",
                                actor_id=actor_id, actor_role=(actor or {}).get("role"),
                                now=now,
                                detail=("Label printed" if print_number == 1
                                        else f"Label printed again (print {print_number})"),
                                metadata={"print_number": print_number, "identity": checks})
                labels.append({
                    "specimen_id": row["id"],
                    "patient": {"name": row["patient_name"], "mrn": row["mrn"],
                                "bed": row["bed"], "ward": row["ward"]},
                    "specimen_display": row["specimen_display"] or row["specimen_type_code"],
                    "container": row["container"],
                    "container_colour_name": row["container_colour_name"],
                    "priority": row["priority"],
                    "tests": [t["short_name"] for t in awaiting[row["id"]]],
                    "print_number": print_number,
                })
            _audit(conn, category="phi_access", action="lab_labels_printed", outcome="success",
                   summary=f"Specimen labels printed ({len(labels)})",
                   actor=actor, target_type="lab_specimen", target_id=",".join(ids)[:200],
                   patient_id=rows[0]["patient_id"],
                   metadata={**_scope_metadata(dept), "specimens": ids,
                             "print_numbers": {l["specimen_id"]: l["print_number"]
                                               for l in labels},
                             "identity": checks},
                   request_meta=request_meta)
    except _IdentityRefused as refusal:
        _audit_identity_refusal(refusal, actor=actor, step="label print",
                                request_meta=request_meta)
        raise _identity_refusal_error(refusal.checks)
    return {"labels": labels, "printed_at": now}


def _collection_instant(collected_at: Any, backdate_reason: Any, *, now: datetime,
                        activated: list[str]) -> tuple[str, int, Optional[str]]:
    """(collected_at in the canonical spelling, minutes back-dated, reason).
    No time given means now. A given time is refused while the site limit is
    0 (the shipped default, ruling LIS-R6 held), and otherwise must carry its
    time zone, not be in the future, not be older than the limit, not be
    before the order was placed, and carry a reason when it is earlier than
    now."""
    text = str(collected_at or "").strip()
    if not text:
        return to_utc_iso(now), 0, None
    limit = backdate_limit_minutes()
    if limit == 0:
        raise LabOrderError(
            "backdating_refused",
            "The collection time is recorded as now. Setting an earlier time is switched "
            "off on this site.", status=422, limit_minutes=0)
    try:
        if not re.search(r"(?:[zZ]|[+-]\d{2}:?\d{2})$", text):
            raise ValueError("no time zone")
        when = parse_to_utc(text)
    except (TypeError, ValueError):
        raise LabOrderError("bad_collected_at",
                            "Give the collection time as a date and time with its time zone.",
                            status=422)
    if when > now + _FUTURE_SKEW:
        raise LabOrderError("collected_in_future", "The collection time is in the future.",
                            status=422)
    when = min(when, now)
    minutes = int((now - when).total_seconds() // 60)
    if now - when > timedelta(minutes=limit):
        raise LabOrderError(
            "backdating_refused",
            f"A collection time can be set at most {limit} minutes back on this site.",
            status=422, limit_minutes=limit)
    reason = str(backdate_reason or "").strip()[:500] or None
    if when < now and reason is None:
        raise LabOrderError("backdate_reason_required",
                            "Say why the collection time is earlier than now.", status=422)
    for instant in activated:
        placed = _instant(instant)
        if placed is not None and when < placed:
            raise LabOrderError("before_order",
                                "The collection time is before the test was ordered.",
                                status=422)
    return to_utc_iso(when), minutes, reason


def collect_specimen(specimen_id: str, *, actor: dict, identity: Any, dept: Optional[str],
                     collected_at: Any = None, backdate_reason: Any = None,
                     collection_location: Any = None,
                     request_meta: Optional[dict] = None) -> dict:
    """Record a specimen as collected, in ONE immediate transaction: the
    specimen, its waiting tests, each order's move to in_progress (through
    orders_service.transition_in_conn, with its order_events row), the
    specimen event and the clinical_access audit row. An audit row that cannot
    be written leaves nothing collected.

    Needs the two identifiers (UHID and name, both matching the record), a
    printed label, and a specimen still waiting for collection. The time is
    now unless the site allows back-dating (it does not by default)."""
    identity = _require_identity_payload(identity)
    actor_id = (actor or {}).get("id")
    try:
        with db_session(immediate=True) as conn:
            if not _tables_present(conn):
                raise _not_found()
            row = _scoped_specimens(conn, [str(specimen_id or "").strip()], dept)[0]
            if row["status"] != "planned":
                raise LabOrderError(
                    "specimen_not_planned",
                    ("This sample is already recorded as collected." if row["status"] == "collected"
                     else "This sample is no longer waiting for collection."),
                    specimen_id=row["id"], specimen_status=row["status"])
            awaiting = _awaiting(conn, row["id"])
            if not awaiting:
                raise LabOrderError("nothing_to_collect",
                                    "Every test on this sample was cancelled.",
                                    specimen_id=row["id"])
            if int(row["label_print_count"] or 0) < 1:
                raise LabOrderError("labels_not_printed",
                                    "Print the label at the bedside before recording the "
                                    "sample as collected.", specimen_id=row["id"])
            order_ids = sorted({t["order_id"] for t in awaiting})
            activated = [a for a in (activation_instant(conn, oid) for oid in order_ids) if a]
            now_dt = datetime.now(timezone.utc)
            when, minutes_back, reason = _collection_instant(
                collected_at, backdate_reason, now=now_dt, activated=activated)
            checks = collection_identity_checks(
                {"name": row["patient_name"], "mrn": row["mrn"]}, identity)
            if not identity_confirmed(checks):
                raise _IdentityRefused(checks, row["patient_id"], [row["id"]])

            now = utc_iso_now()
            location = str(collection_location or "").strip()[:120] or None
            identity_record = {"uhid": checks["uhid"], "name": checks["name"],
                               "checked_by": actor_id, "checked_at": now}
            cur = conn.execute(
                """UPDATE lab_specimens
                   SET status = 'collected', collected_at = ?, collected_by = ?,
                       collection_location = ?, identity_check_json = ?,
                       collection_backdate_reason = ?, updated_at = ?
                   WHERE id = ? AND status = 'planned'""",
                (when, actor_id, location, json.dumps(identity_record), reason, now, row["id"]))
            if cur.rowcount != 1:
                raise LabOrderError("specimen_not_planned",
                                    "This sample is no longer waiting for collection.",
                                    specimen_id=row["id"])
            item_ids = [t["item_id"] for t in awaiting]
            conn.execute(
                f"UPDATE lab_order_items SET status = 'collected', updated_at = ? "
                f"WHERE id IN ({_placeholders(item_ids)}) AND status = 'awaiting_collection'",
                (now, *item_ids))
            _specimen_event(conn, row["id"], row["patient_id"], "collected",
                            actor_id=actor_id, actor_role=(actor or {}).get("role"), now=now,
                            detail=("Specimen collected" if not minutes_back
                                    else f"Specimen collected {minutes_back} minutes before "
                                         f"it was recorded. Reason: {reason}"),
                            metadata={"collected_at": when, "minutes_back": minutes_back,
                                      "location": location, "identity": checks,
                                      "tests": [t["test_id"] for t in awaiting]})
            orders = []
            for order_id in order_ids:
                outcome = reconcile_order_status(conn, order_id, actor_id=actor_id,
                                                 reason=f"Specimen {row['id']} collected")
                orders.append({"id": order_id, "status": outcome["status"],
                               "conflict": outcome["conflict"]})
            _audit(conn, category="clinical_access", action="lab_specimen_collected",
                   outcome="success",
                   summary=(f"Specimen collected: {len(awaiting)} test(s)"
                            + (f", time set {minutes_back} minutes back" if minutes_back else "")),
                   actor=actor, target_type="lab_specimen", target_id=row["id"],
                   patient_id=row["patient_id"],
                   metadata={"orders": order_ids, "items": [t["test_id"] for t in awaiting],
                             "collected_at": when, "minutes_back": minutes_back,
                             "backdate_reason": reason, "location": location,
                             "identity": checks},
                   request_meta=request_meta)
            specimen = dict(conn.execute("SELECT * FROM lab_specimens WHERE id = ?",
                                         (row["id"],)).fetchone())
    except _IdentityRefused as refusal:
        _audit_identity_refusal(refusal, actor=actor, step="collection",
                                request_meta=request_meta)
        raise _identity_refusal_error(refusal.checks)
    specimen.pop("identity_check_json", None)
    return {"specimen": specimen, "items_collected": len(awaiting), "orders": orders}
