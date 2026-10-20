"""Orb: medication-order reads for the /api/medorders routes (medorders-4, W5-E).

Every function here takes the caller's connection and writes nothing. A GET
never adopts (the main.py rule: SQLite has one writer, and a read that
sometimes writes turns every chart open into a queue for the write lock), so a
chart row the store has not adopted yet is shown through the pure mapping
core.legacy_status_for, marked as a legacy item, with no order id.

Scope is the caller's business: the routes check a patient with
permissions.require_patient_access or patient_in_scope, and the queues pass
permissions.scope_patient_ids_sql for the caller's department here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from services.clinical_time import format_utc
from services.medorders import core

__all__ = [
    "DRAFT_WAIT_HOURS",
    "QUEUE_LIMIT",
    "active_admission_id",
    "order_scope_row",
    "serialize_order",
    "serialize_event",
    "list_for_patient",
    "same_ingredient_live",
    "to_sign_queue",
]

#: Ruling MO-R10 (a): unsigned drafts are never cancelled by the system; a
#: draft waiting longer than this is marked so the to-sign queue can say so.
#: The queue is oldest first, so those drafts are already at its top.
DRAFT_WAIT_HOURS = 24

#: Rows per to-sign list. A ward's unsigned drafts and nurse-charted live
#: orders fit easily; the bound keeps one runaway list from filling a reply.
QUEUE_LIMIT = 200


def active_admission_id(conn, patient_id: str) -> str | None:
    """The patient's active admission (the newest, if several), or None."""
    row = conn.execute(
        "SELECT id FROM admissions WHERE patient_id = ? AND status = 'active' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (patient_id,),
    ).fetchone()
    if row is None or row["id"] is None:
        return None
    return str(row["id"])


def order_scope_row(conn, order_id: str) -> dict | None:
    """The order's id and patient, for the scope check before any read of it."""
    row = conn.execute("SELECT id, patient_id FROM med_orders WHERE id = ?", (order_id,)).fetchone()
    return dict(row) if row is not None else None


def _json(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def serialize_order(row: Mapping) -> dict:
    """An order as the API returns it: every column, the stored safety screen
    as an object rather than a JSON string, and its FHIR status."""
    out = dict(row)
    out["last_safety"] = _json(out.pop("last_safety_json", None))
    out["is_prn"] = bool(out.get("is_prn"))
    out["needs_verification"] = bool(out.get("needs_verification"))
    status = out.get("status")
    out["fhir_status"] = core.fhir_status_for(status)["status"] if status in core.STATUSES else None
    return out


def serialize_event(row: Mapping) -> dict:
    out = dict(row)
    out["payload"] = _json(out.pop("payload_json", None))
    return out


#: The ledger events behind an order's supply line (medorders-10, W9-D).
SUPPLY_EVENT_KINDS = (
    "supply_requested",
    "supply_reviewing",
    "supply_verified",
    "supply_rejected",
    "dispensed",
    "received",
)
#: The bedside events behind an order's last dose.
DOSE_EVENT_KINDS = ("administered", "not_given")


def _user_names(conn, ids) -> dict:
    wanted = sorted({str(i) for i in ids if i})
    if not wanted:
        return {}
    marks = ", ".join("?" for _ in wanted)
    return {
        r["id"]: r["name"]
        for r in conn.execute(
            f"SELECT id, name FROM users WHERE id IN ({marks})", tuple(wanted)
        ).fetchall()
    }


def _ledger_lines(conn, patient_id: str) -> dict:
    """{order id: {supply, last_dose}} from the patient's ledger in one read
    (medorders-10, W9-D): the chart's supply line (the latest supply row's
    state, who dispensed it and who received it on the ward) and the last
    bedside record. An order with no supply event has supply None, which the
    chart says as 'supply not linked'."""
    kinds = SUPPLY_EVENT_KINDS + DOSE_EVENT_KINDS
    marks = ", ".join("?" for _ in kinds)
    rows = [
        dict(r)
        for r in conn.execute(
            f"SELECT med_order_id, kind, actor_id, actor_role, reason, ref_type, ref_id, "
            f"created_at FROM med_order_events WHERE patient_id = ? AND kind IN ({marks}) "
            f"ORDER BY created_at, id",
            (patient_id, *kinds),
        ).fetchall()
    ]
    names = _user_names(conn, (r["actor_id"] for r in rows))
    out: dict = {}
    for r in rows:
        line = out.setdefault(r["med_order_id"], {"supply": None, "last_dose": None})
        who = {
            "by": r["actor_id"],
            "by_name": names.get(r["actor_id"]),
            "by_role": r["actor_role"],
            "at": r["created_at"],
        }
        if r["kind"] in DOSE_EVENT_KINDS:
            # ref_type/ref_id name the medication_administrations row (the
            # eMAR writes them, emar_check.record_bedside_event_in_txn), so the
            # chart can join this dose to its administration record and mark
            # a dose given across a charted allergy, as the old table did.
            line["last_dose"] = {
                "kind": r["kind"],
                "reason": r["reason"],
                "ref_type": r["ref_type"],
                "ref_id": r["ref_id"],
                **who,
            }
            continue
        supply_id = r["ref_id"] if r["ref_type"] == "helix_order" else None
        current = line["supply"]
        if current is None or (supply_id and current.get("helix_order_id") != supply_id):
            current = line["supply"] = {
                "helix_order_id": supply_id,
                "state": None,
                "at": None,
                "requested_at": None,
                "dispensed": None,
                "received": None,
            }
        current["state"] = r["kind"]
        current["at"] = r["created_at"]
        if r["kind"] == "supply_requested":
            current["requested_at"] = r["created_at"]
        elif r["kind"] in ("dispensed", "received"):
            current[r["kind"]] = who
    return out


def list_for_patient(conn, patient_id: str) -> dict:
    """The patient's medication orders, newest first, plus every chart row the
    store has not adopted, shown as the order it would become (read-only).

    Each order also carries (medorders-10, W9-D) the names of the people on it
    (prescriber_name, verified_by_name, entered_by_name,
    status_changed_by_name), its supply line ('supply': the latest supply row's
    state with who dispensed and received it, or None for 'supply not linked')
    and 'last_dose' (the last bedside record on the order's ledger, or None,
    with the ref_type and ref_id of the administration record it points at)."""
    orders = [
        serialize_order(r)
        for r in conn.execute(
            "SELECT * FROM med_orders WHERE patient_id = ? ORDER BY created_at DESC, id DESC",
            (patient_id,),
        ).fetchall()
    ]
    if orders:
        names = _user_names(
            conn,
            (
                o.get(c)
                for o in orders
                for c in ("prescriber_id", "verified_by", "entered_by", "status_changed_by")
            ),
        )
        lines = _ledger_lines(conn, patient_id)
        for o in orders:
            o["prescriber_name"] = names.get(o.get("prescriber_id"))
            o["verified_by_name"] = names.get(o.get("verified_by"))
            o["entered_by_name"] = names.get(o.get("entered_by"))
            o["status_changed_by_name"] = names.get(o.get("status_changed_by"))
            line = lines.get(o["id"]) or {}
            o["supply"] = line.get("supply")
            o["last_dose"] = line.get("last_dose")
    legacy = []
    for r in conn.execute(
        "SELECT id, drug_name, dosage, route, frequency, status, source, verified_by, "
        "verified_at, started_at FROM medications WHERE patient_id = ? "
        "AND med_order_id IS NULL ORDER BY id DESC",
        (patient_id,),
    ).fetchall():
        mapping = core.legacy_status_for(r)
        legacy.append(
            {
                "legacy": True,
                "medication_id": r["id"],
                "drug_name": r["drug_name"],
                "dose": r["dosage"],
                "route": r["route"],
                "frequency": r["frequency"],
                "chart_status": r["status"],
                "status": mapping["status"],
                "is_prn": bool(mapping["is_prn"]),
                "needs_verification": bool(mapping["needs_verification"]),
                "verified_by": mapping["verified_by"],
                "verified_at": mapping["verified_at"],
                "started_at": r["started_at"],
                "adoptable": mapping["adoptable"],
            }
        )
    return {"patient_id": patient_id, "orders": orders, "legacy": legacy}


def same_ingredient_live(
    conn, patient_id: str, drug_name: str, *, exclude_order_id: str | None = None
) -> list[dict]:
    """Live orders and unadopted live chart rows for the same product, for the
    duplicate NOTICE at create and sign. Compared through
    drug_safety_service.same_product, the vocabulary the bedside repeat-dose
    check uses, so 'Paracetamol' and 'Paracetamol 650 mg' are one drug and two
    insulins are two. A notice only: whether a duplicate needs a reason or is
    refused is ruling MO-R6, built in a later increment."""
    from services.drug_safety_service import same_product

    found = []
    marks = ", ".join("?" for _ in core.LIVE_STATUSES)
    for r in conn.execute(
        f"SELECT id, drug_name, status, legacy_medication_id FROM med_orders "
        f"WHERE patient_id = ? AND status IN ({marks}) ORDER BY created_at, id",
        (patient_id, *core.LIVE_STATUSES),
    ).fetchall():
        if r["id"] == exclude_order_id:
            continue
        if same_product([drug_name], [r["drug_name"]]):
            found.append(
                {
                    "med_order_id": r["id"],
                    "medication_id": r["legacy_medication_id"],
                    "drug_name": r["drug_name"],
                    "status": r["status"],
                }
            )
    for r in conn.execute(
        "SELECT id, drug_name, status FROM medications WHERE patient_id = ? "
        "AND status IN ('active', 'prn') AND med_order_id IS NULL ORDER BY id",
        (patient_id,),
    ).fetchall():
        if same_product([drug_name], [r["drug_name"]]):
            found.append(
                {
                    "med_order_id": None,
                    "medication_id": r["id"],
                    "drug_name": r["drug_name"],
                    "status": r["status"],
                }
            )
    return found


def to_sign_queue(
    conn,
    scope_sql: str,
    scope_params: tuple,
    *,
    limit: int = QUEUE_LIMIT,
    now: datetime | None = None,
) -> dict:
    """What a prescriber has to look at, inside the caller's department.

      drafts              unsigned orders, oldest first (MO-R10 (a): a draft
                          waiting over DRAFT_WAIT_HOURS is marked, and being
                          oldest it is already at the top)
      entered_by_a_nurse  live orders a nurse charted on the chart lane, for
                          visibility only: the chart lane does not wait for a
                          signature until ruling R010 says it should

    `scope_sql` and `scope_params` come from permissions.scope_patient_ids_sql
    for the caller's own department."""
    cutoff = format_utc((now or datetime.now(UTC)) - timedelta(hours=DRAFT_WAIT_HOURS))
    select = (
        "SELECT o.*, p.name AS patient_name, p.ward AS patient_ward, p.bed AS patient_bed, "
        "u.name AS entered_by_name FROM med_orders o "
        "JOIN patients p ON p.id = o.patient_id "
        "LEFT JOIN users u ON u.id = o.entered_by "
        f"WHERE o.patient_id IN ({scope_sql}) "
    )
    drafts = []
    for r in conn.execute(
        select + "AND o.status = 'draft' ORDER BY o.created_at, o.id LIMIT ?",
        (*scope_params, limit),
    ).fetchall():
        item = serialize_order(r)
        item["waiting_over_24h"] = bool(item.get("created_at")) and item["created_at"] < cutoff
        drafts.append(item)
    marks = ", ".join("?" for _ in core.LIVE_STATUSES)
    nurse = [
        serialize_order(r)
        for r in conn.execute(
            select + f"AND o.status IN ({marks}) AND o.entered_by_role = 'nurse' "
            "AND p.status = 'admitted' ORDER BY o.created_at DESC, o.id DESC LIMIT ?",
            (*scope_params, *core.LIVE_STATUSES, limit),
        ).fetchall()
    ]
    return {
        "drafts": drafts,
        "entered_by_a_nurse": nurse,
        "draft_wait_hours": DRAFT_WAIT_HOURS,
        "counts": {
            "drafts": len(drafts),
            "waiting_over_24h": sum(1 for d in drafts if d["waiting_over_24h"]),
            "entered_by_a_nurse": len(nurse),
        },
    }
