"""Orb: adopting existing chart drugs into the medication-order store, and the
parity report (medorders-2, W4-E).

ADOPTION. With ORB_MED_ORDER_STORE on, every medications row that has no order
gets one, so the store and the ward agree. It runs:
  * at boot (main.py lifespan, boot_adoption), only with the store switch on;
  * from server/scripts/medorder_parity.py --adopt, also only with the switch on;
  * from the admin action POST /api/medorders/admin/adopt (W5-E), which 404s
    while the switch is off.
NEVER ON A GET PATH. SQLite has one writer: a read that sometimes writes turns
every chart open into a queue for the write lock (the main.py rule above the
model registry reconcile).

The mapping is legacy_status_for in core, and it changes nothing the bedside
decides: the ward row is left exactly as it is, and the order adopted for it
allows a dose in every case the ward row's own checks allow one (pinned by
tests/test_medorder_adoption.py for every status, source and verifier
combination). Rows are adopted 500 to a transaction with one clinical_access
audit row per batch. A second run finds nothing to do.

Not adopted, and listed by the parity report instead:
  * pharmacy (Helix) rows: a Sage pair (chart row plus Helix row) cannot be
    matched reliably, so only its chart row is adopted;
  * a chart row whose status the mapping cannot read (never guessed);
  * a chart row an existing order already names although the row does not name
    the order back (a broken link is reported, never repaired by a guess).

THE PARITY REPORT is read-only. It counts what is not yet in the store and
every place the store and the ward disagree, with a sample of ids (no drug
names) for each.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Mapping, Optional, Sequence

from database import db_session
from services.audit_service import log_audit_event
from services.clinical_time import to_utc_iso, utc_iso_now
from services.medorders import core

logger = logging.getLogger("orb.medorders.adoption")

__all__ = ["BATCH_SIZE", "SYSTEM_ACTOR", "adopt_legacy", "boot_adoption", "parity_report",
           "AdoptionRefused"]

#: Chart rows adopted per transaction: short enough that a ward write waits a
#: moment at most, long enough that a large chart adopts in a few batches.
BATCH_SIZE = 500

#: The actor recorded for adoption at boot, and the entered_by of an adopted
#: row whose prescriber the chart never recorded. It is the seeded system user.
SYSTEM_ACTOR = "system"

_TRIGGERS = ("boot", "script", "admin")
_HELIX_OPEN = ("pending", "reviewing", "verified", "substitute_suggested", "scheduled",
               "dispensed")


class AdoptionRefused(RuntimeError):
    """Adoption was asked for while the store switch is off."""


def _canonical(value: Any) -> Optional[str]:
    """A stored instant in the canonical UTC spelling, or None when it cannot
    be read (the raw value is kept in the adoption event's payload)."""
    if value in (None, ""):
        return None
    try:
        return to_utc_iso(value)
    except (ValueError, TypeError):
        return None


def _adopt_row(conn, row: Mapping, *, actor_id: Optional[str], actor_role: Optional[str],
               entry_point: str) -> tuple[str, Optional[str]]:
    """Adopt one unlinked chart row. Returns (outcome, order status), outcome
    being 'adopted', 'unadoptable' or 'conflict'."""
    mapping = core.legacy_status_for(row)
    if not mapping["adoptable"]:
        return "unadoptable", None
    named = conn.execute("SELECT id FROM med_orders WHERE legacy_medication_id = ?",
                         (row["id"],)).fetchone()
    if named is not None:
        return "conflict", None
    # One row the store cannot hold (a blank drug name, say) must not stop
    # every later row at every boot: its partial writes are undone and it is
    # left unlinked, with today's behaviour, for the parity report.
    conn.execute("SAVEPOINT medorder_adopt_row")
    try:
        _create_and_link(conn, row, mapping, actor_id=actor_id, actor_role=actor_role,
                         entry_point=entry_point)
    except core.MedOrderError as exc:
        conn.execute("ROLLBACK TO SAVEPOINT medorder_adopt_row")
        conn.execute("RELEASE SAVEPOINT medorder_adopt_row")
        logger.warning("Chart medication %s was not adopted: %s", row["id"], exc.code)
        return "unadoptable", None
    conn.execute("RELEASE SAVEPOINT medorder_adopt_row")
    return "adopted", mapping["status"]


def _create_and_link(conn, row: Mapping, mapping: Mapping, *, actor_id: Optional[str],
                     actor_role: Optional[str], entry_point: str) -> None:
    write = core.create_in_txn(
        conn,
        patient_id=row["patient_id"],
        drug_name=row["drug_name"],
        source="legacy_adopted",
        status=mapping["status"],
        needs_verification=bool(mapping["needs_verification"]),
        dose=row["dosage"],
        route=row["route"],
        frequency=row["frequency"],
        is_prn=bool(mapping["is_prn"]),
        entered_by=row["prescribed_by"] or SYSTEM_ACTOR,
        prescriber_id=row["prescribed_by"],
        prescribed_at=_canonical(row["started_at"]),
        verified_by=mapping["verified_by"],
        verified_at=_canonical(mapping["verified_at"]),
        status_reason=mapping["status_reason"],
        projection_status=mapping["projection_status"],
        legacy_medication_id=row["id"],
        event_kind="adopted",
        event_payload={
            "legacy_status": row["status"],
            "legacy_source": core._field(row, "source"),
            "legacy_verified_by": core._field(row, "verified_by"),
            "legacy_verified_at": core._field(row, "verified_at"),
            "legacy_started_at": row["started_at"],
        },
        actor_id=actor_id,
        actor_role=actor_role,
        entry_point=entry_point,
        audit=False,
    )
    core.link_projection_in_txn(conn, write.order["id"], row["id"])


def adopt_legacy(*, actor_id: Optional[str] = SYSTEM_ACTOR, actor_role: Optional[str] = "system",
                 trigger: str = "boot", batch_size: int = BATCH_SIZE,
                 patient_ids: Optional[Sequence[str]] = None,
                 request_meta: Optional[Mapping] = None, require_switch: bool = True) -> dict:
    """Give every unlinked chart row an order. Opens its own immediate
    sessions, one per batch, so call it through run_db from async code.

    Refuses (AdoptionRefused) while ORB_MED_ORDER_STORE is off: an adopted link
    is what the later bedside check follows whatever the switch says, so links
    are made only on a site that turned the store on. `require_switch=False`
    exists for tests that drive adoption on a scratch database.

    `patient_ids` limits adoption to those patients (an operator adopting one
    ward first, or a test); None, the default and what boot uses, adopts every
    unlinked chart row."""
    if trigger not in _TRIGGERS:
        raise ValueError(f"trigger must be one of {_TRIGGERS}")
    if require_switch and not core.store_on():
        raise AdoptionRefused(
            "The medication-order store is switched off (ORB_MED_ORDER_STORE), so "
            "nothing was adopted.")
    batch_size = max(1, int(batch_size))
    entry_point = f"medorder.adopt.{trigger}"
    scope_params: tuple = ()
    scope_sql = ""
    if patient_ids is not None:
        scope_params = tuple(dict.fromkeys(str(p) for p in patient_ids))
        if not scope_params:
            return {"adopted": 0, "unadoptable": 0, "conflict": 0, "batches": 0,
                    "by_status": {}, "unadoptable_medication_ids": [],
                    "conflict_medication_ids": []}
        scope_sql = f" AND patient_id IN ({', '.join('?' for _ in scope_params)})"
    totals = {"adopted": 0, "unadoptable": 0, "conflict": 0, "batches": 0, "by_status": {}}
    unadoptable_ids: list[int] = []
    conflict_ids: list[int] = []
    last_id = 0
    while True:
        with db_session(immediate=True) as conn:
            rows = conn.execute(
                "SELECT * FROM medications WHERE med_order_id IS NULL AND id > ?"
                + scope_sql + " ORDER BY id LIMIT ?",
                (last_id, *scope_params, batch_size),
            ).fetchall()
            if not rows:
                break
            batch = {"adopted": 0, "unadoptable": 0, "conflict": 0, "by_status": {}}
            for row in rows:
                outcome, status = _adopt_row(conn, row, actor_id=actor_id,
                                             actor_role=actor_role, entry_point=entry_point)
                batch[outcome] += 1
                if outcome == "adopted":
                    batch["by_status"][status] = batch["by_status"].get(status, 0) + 1
                elif outcome == "unadoptable":
                    unadoptable_ids.append(row["id"])
                else:
                    conflict_ids.append(row["id"])
            first, last_id = rows[0]["id"], rows[-1]["id"]
            if batch["adopted"]:
                meta = dict(request_meta or {})
                log_audit_event(
                    correlation_id=meta.get("correlation_id") or str(uuid.uuid4()),
                    event_category="clinical_access",
                    action_type="med_orders_adopted",
                    outcome="success",
                    db=conn,
                    summary_message=(
                        f"Adopted {batch['adopted']} chart medication(s) into the "
                        f"medication-order store ({trigger})"),
                    actor_user_id=actor_id,
                    actor_role=actor_role,
                    target_type="medications",
                    target_id=f"{first}-{last_id}",
                    request_path=meta.get("request_path"),
                    http_method=meta.get("http_method"),
                    ip_address=meta.get("ip_address"),
                    user_agent=meta.get("user_agent"),
                    metadata={
                        "trigger": trigger,
                        "batch": totals["batches"] + 1,
                        "adopted": batch["adopted"],
                        "by_status": batch["by_status"],
                        "unadoptable": batch["unadoptable"],
                        "conflict": batch["conflict"],
                        "first_medication_id": str(first),
                        "last_medication_id": str(last_id),
                    },
                )
        totals["batches"] += 1
        for key in ("adopted", "unadoptable", "conflict"):
            totals[key] += batch[key]
        for status, n in batch["by_status"].items():
            totals["by_status"][status] = totals["by_status"].get(status, 0) + n
        if len(rows) < batch_size:
            break
    totals["unadoptable_medication_ids"] = unadoptable_ids[:50]
    totals["conflict_medication_ids"] = conflict_ids[:50]
    return totals


def boot_adoption() -> Optional[dict]:
    """The lifespan hook: adopt with the store on, do nothing with it off.
    Never raises: a failed adoption leaves unlinked rows with exactly today's
    behaviour, so the wards must still open; it is logged loudly instead."""
    if not core.store_on():
        return None
    try:
        result = adopt_legacy(trigger="boot")
    except Exception:
        logger.exception("Medication-order adoption at boot failed; unadopted chart rows "
                         "keep today's behaviour. Run scripts/medorder_parity.py.")
        return {"error": "adoption_failed"}
    logger.info("Medication-order store: adopted %d chart row(s) in %d batch(es); "
                "%d unreadable, %d with a broken link",
                result["adopted"], result["batches"], result["unadoptable"], result["conflict"])
    return result


# ── The parity report (read-only) ─────────────────────────────────────────────

def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def _section(rows: list, sample_limit: int, **extra) -> dict:
    return {"count": len(rows), "sample": rows[:sample_limit], **extra}


def _projection_mismatch(order: Mapping, med: Mapping) -> list[str]:
    """Which ward-row facts disagree with the order: status, verified_by,
    held_at."""
    if order["status"] == "draft":
        return ["draft_has_ward_row"]
    want = core.projection_for(order)
    fields = []
    if med["status"] != want["status"]:
        fields.append("status")
    if bool(med["held_at"]) != bool(want["held_at"]):
        fields.append("held_at")
    if want.get("verified_by") and med["verified_by"] != want["verified_by"]:
        fields.append("verified_by")
    return fields


def parity_report(conn=None, *, sample_limit: int = 50) -> dict:
    """Where the store and the ward disagree, and what is not in the store yet.

    problems (need a person):
      projection_mismatches   a linked ward row whose status, verifier or hold
                              marker differs from what its order says
      broken_links            a ward row naming no order, or an order naming a
                              ward row that does not name it back
      terminal_orders_with_open_supply
                              a pharmacy supply row still open for an order
                              that has ended
    pending (adoption has not reached them):
      unlinked_live_rows      active or prn chart rows with no order
      unadoptable_rows        unlinked chart rows whose status cannot be mapped
    informational (expected while only record mode runs):
      helix_rows_without_order, cpoe_medication_rows_without_order
    """
    if conn is None:
        with db_session() as own:
            return parity_report(own, sample_limit=sample_limit)
    if not _table_exists(conn, "med_orders"):
        raise RuntimeError("The medication-order store tables are missing: migration 61 "
                           "has not run on this database.")

    unlinked = [dict(r) for r in conn.execute(
        "SELECT id AS medication_id, patient_id, status, "
        "TRIM(COALESCE(drug_name, '')) = '' AS blank_drug_name FROM medications "
        "WHERE med_order_id IS NULL ORDER BY id").fetchall()]
    unadoptable = [r for r in unlinked
                   if r["blank_drug_name"] or not core.legacy_status_for(r)["adoptable"]]
    for r in unlinked:
        r["blank_drug_name"] = bool(r["blank_drug_name"])
    unlinked_live = [r for r in unlinked if r["status"] in core.PROJECTION_STATUSES]

    mismatches = []
    for r in conn.execute(
        "SELECT o.id AS order_id, o.status AS order_status, o.projection_status, o.is_prn, "
        "o.status_changed_at, o.status_reason, o.verified_by AS order_verified_by, "
        "o.verified_at AS order_verified_at, o.patient_id, "
        "m.id AS medication_id, m.status AS med_status, m.held_at, m.verified_by AS med_verified_by "
        "FROM med_orders o JOIN medications m ON m.med_order_id = o.id ORDER BY m.id"
    ).fetchall():
        order = {"id": r["order_id"], "status": r["order_status"],
                 "projection_status": r["projection_status"], "is_prn": r["is_prn"],
                 "status_changed_at": r["status_changed_at"], "status_reason": r["status_reason"],
                 "verified_by": r["order_verified_by"], "verified_at": r["order_verified_at"]}
        med = {"status": r["med_status"], "held_at": r["held_at"],
               "verified_by": r["med_verified_by"]}
        fields = _projection_mismatch(order, med)
        if fields:
            mismatches.append({"med_order_id": r["order_id"], "medication_id": r["medication_id"],
                               "patient_id": r["patient_id"], "fields": fields})

    broken = [dict(r, problem="ward_row_names_missing_order") for r in conn.execute(
        "SELECT m.id AS medication_id, m.patient_id, m.med_order_id FROM medications m "
        "WHERE m.med_order_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM med_orders o WHERE o.id = m.med_order_id) "
        "ORDER BY m.id").fetchall()]
    broken += [dict(r, problem="order_names_unlinked_ward_row") for r in conn.execute(
        "SELECT o.id AS med_order_id, o.patient_id, o.legacy_medication_id AS medication_id "
        "FROM med_orders o WHERE o.legacy_medication_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM medications m WHERE m.id = o.legacy_medication_id "
        "AND m.med_order_id = o.id) ORDER BY o.created_at, o.id").fetchall()]

    helix_without, terminal_open = [], []
    if _table_exists(conn, "helix_patient_med_orders"):
        helix_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(helix_patient_med_orders)").fetchall()}
        if "med_order_id" in helix_cols:
            helix_without = [dict(r) for r in conn.execute(
                "SELECT id AS helix_order_id, patient_id, status FROM helix_patient_med_orders "
                "WHERE med_order_id IS NULL ORDER BY created_at, id").fetchall()]
            marks = ", ".join("?" for _ in _HELIX_OPEN)
            ended = ", ".join("?" for _ in core.TERMINAL_STATUSES)
            terminal_open = [dict(r) for r in conn.execute(
                "SELECT h.id AS helix_order_id, h.status AS supply_status, "
                "o.id AS med_order_id, o.status AS order_status, o.patient_id "
                "FROM helix_patient_med_orders h JOIN med_orders o ON o.id = h.med_order_id "
                f"WHERE h.status IN ({marks}) AND o.status IN ({ended}) ORDER BY h.id",
                (*_HELIX_OPEN, *core.TERMINAL_STATUSES)).fetchall()]

    cpoe_without = [dict(r) for r in conn.execute(
        "SELECT ord.id AS cpoe_order_id, ord.patient_id, ord.status FROM orders ord "
        "WHERE ord.order_type = 'medication' AND NOT EXISTS ("
        "SELECT 1 FROM med_orders o WHERE o.cpoe_order_id = ord.id) "
        "ORDER BY ord.created_at, ord.id").fetchall()]

    report = {
        "generated_at": utc_iso_now(),
        "store_on": core.store_on(),
        "lifecycle_on": core.lifecycle_on(),
        "orders": conn.execute("SELECT COUNT(*) FROM med_orders").fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM med_order_events").fetchone()[0],
        "problems": {
            "projection_mismatches": _section(mismatches, sample_limit),
            "broken_links": _section(broken, sample_limit),
            "terminal_orders_with_open_supply": _section(terminal_open, sample_limit),
        },
        "pending": {
            "unlinked_live_rows": _section(unlinked_live, sample_limit),
            "unadoptable_rows": _section(unadoptable, sample_limit),
        },
        "informational": {
            "helix_rows_without_order": _section(
                helix_without, sample_limit,
                open_count=sum(1 for h in helix_without if h["status"] in _HELIX_OPEN)),
            "cpoe_medication_rows_without_order": _section(
                cpoe_without, sample_limit,
                open_count=sum(1 for c in cpoe_without if c["status"] != "cancelled")),
        },
    }
    report["clean"] = not any(s["count"] for s in report["problems"].values())
    return report
