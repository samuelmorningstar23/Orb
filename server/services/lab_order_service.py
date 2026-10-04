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

This module writes only lab_ tables. It never changes orders.status: that is
lis-7's guard and lifecycle, through orders_service's helpers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from database import db_session
from services import lab_accession, lab_module
from services.audit_service import log_audit_event
from services.clinical_time import parse_to_utc, utc_iso_now

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
