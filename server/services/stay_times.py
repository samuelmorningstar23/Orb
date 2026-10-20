"""Orb: when a hospital stay began and when it ended (adt-8c, W4-B).

WHY ONE HELPER. Before the beds and admissions module (ORB_ADT, default off)
every stay that ended in Orb ended through discharge_service.discharge_patient,
which always writes a discharge summary, so a dozen readers took "a discharge
summary exists" to mean "the stay ended, at the summary's time". With ORB_ADT on
that stopped being true: POST /api/adt/admissions/{id}/end records a death, a
LAMA, an absconded closure or a routine departure with NO summary. Each of
those readers then missed the end entirely: forecast's 'discharges today', the
impact throughput count, the NABH readmission and disposition coverage counts,
the patient portal's discharge view, the /discharged list's time and the
housekeeping turnaround sweep. This module is the one place that knows both
kinds of end, and every one of those readers asks it.

  stay_start(admission)        when the stay began: admissions.admitted_at
                               (the arrival time ADT records, which may be
                               back-timed) else admissions.created_at.
  stay_end(db, admission)      when the stay ended, or None while it is
                               active: admissions.discharged_at when set, else
                               the earliest discharge summary for the patient
                               at or after the stay start and before the next
                               admission's start.
  stay_start_sql / stay_end_sql  the same two as SQL fragments over an
                               admissions alias.
  summary_less_ends(db, ...)   every stay ADT ended with no summary, live
                               (not reopened as recorded in error).
  admission_for_summary(...)   the stay a discharge summary ended.

WHAT COUNTS AS A SUMMARY-LESS END. Exactly the ends the beds and admissions
module recorded: an ended admission whose latest 'discharge' movement in the
patient_movements ledger carries no discharge summary id and has not been
corrected by a reopen (adt-8b). It is keyed on the ledger, not on
admissions.discharged_at alone, on purpose: an imported historical encounter
also carries discharged_at (migration 15), and counting those as today's
discharges would change every figure on a site that never turned ORB_ADT on.
With ORB_ADT off no such movement exists, so every reader here gives exactly
the answer it gave before.

THE MIGRATION 15 NOTE, CORRECTED HERE (migrations.py is not edited). The note
beside admissions.discharged_at in migrations._m15_history_import says the
column is NULL for every Orb-native row and populated only by imports, and
must never be used as a "was this discharged" test. Since migration 58 and
adt-8a the first half is no longer true: with ORB_ADT on, every end of stay
(the End of stay route, the doctor's discharge route and the roster close)
writes admissions.discharged_at in the same transaction as the status flip.
The second half still holds: admissions.status is THE discharge test. A stay
is ended when its status is no longer 'active'; discharged_at says WHEN, and
is read only through this module. A stay reopened as recorded in error
(adt-8b) has its discharged_at cleared with its status restored, and its old
end stays in the ledger and the audit trail.

Every function takes the caller's connection and never commits.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from services.clinical_time import parse_to_utc

__all__ = [
    "stay_start_sql",
    "stay_end_sql",
    "stay_start",
    "stay_start_stored",
    "stay_end",
    "stay_end_stored",
    "ledger_ready",
    "summary_less_ends_sql",
    "summary_less_ends",
    "count_summary_less_ends",
    "admission_for_summary",
    "latest_admission",
]


def _parse(value: Any) -> datetime | None:
    """Any stored spelling as an aware UTC datetime; None when blank or
    unreadable. A stored time with no offset is UTC, as every Orb writer
    stores it."""
    if value is None or value == "":
        return None
    try:
        return parse_to_utc(value)
    except (TypeError, ValueError):
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  SQL fragments
# ═════════════════════════════════════════════════════════════════════════════


def stay_start_sql(alias: str = "a") -> str:
    """When the stay began, as SQL over the admissions alias `alias`."""
    return f"COALESCE({alias}.admitted_at, {alias}.created_at)"


def stay_end_sql(alias: str = "a") -> str:
    """When the stay ended, as SQL over the admissions alias `alias`: NULL for
    an active stay, else admissions.discharged_at, else the earliest discharge
    summary at or after the stay start that comes before the patient's next
    admission. A correlated subquery with no placeholders."""
    start = stay_start_sql(alias)
    return (
        f"(CASE WHEN {alias}.status = 'active' THEN NULL ELSE COALESCE({alias}.discharged_at, "
        f"(SELECT ds.discharged_at FROM discharge_summaries ds "
        f"WHERE ds.patient_id = {alias}.patient_id AND ds.discharged_at IS NOT NULL "
        f"AND julianday(ds.discharged_at) >= julianday({start}) "
        f"AND NOT EXISTS (SELECT 1 FROM admissions nx WHERE nx.patient_id = {alias}.patient_id "
        f"AND nx.id <> {alias}.id "
        f"AND julianday(COALESCE(nx.admitted_at, nx.created_at)) > julianday({start}) "
        f"AND julianday(COALESCE(nx.admitted_at, nx.created_at)) <= julianday(ds.discharged_at)) "
        f"ORDER BY julianday(ds.discharged_at) ASC, ds.id ASC LIMIT 1)) END)"
    )


# ═════════════════════════════════════════════════════════════════════════════
#  One admission
# ═════════════════════════════════════════════════════════════════════════════


def stay_start_stored(admission: Mapping) -> str | None:
    """The stay start as stored (admitted_at, else created_at)."""
    return admission.get("admitted_at") or admission.get("created_at") or None


def stay_start(admission: Mapping) -> datetime | None:
    """When the stay began, as an aware UTC datetime, or None if unreadable."""
    return _parse(admission.get("admitted_at")) or _parse(admission.get("created_at"))


def stay_end_stored(db, admission: Mapping) -> str | None:
    """When the stay ended, as stored, or None while the stay is active (or
    when no end time was recorded anywhere)."""
    if (admission.get("status") or "active") == "active":
        return None
    if admission.get("discharged_at"):
        return admission["discharged_at"]
    start = stay_start_stored(admission)
    if not start or not admission.get("patient_id"):
        return None
    row = db.execute(
        """SELECT ds.discharged_at FROM discharge_summaries ds
            WHERE ds.patient_id = ? AND ds.discharged_at IS NOT NULL
              AND julianday(ds.discharged_at) >= julianday(?)
              AND NOT EXISTS (
                  SELECT 1 FROM admissions nx
                   WHERE nx.patient_id = ds.patient_id AND nx.id <> ?
                     AND julianday(COALESCE(nx.admitted_at, nx.created_at)) > julianday(?)
                     AND julianday(COALESCE(nx.admitted_at, nx.created_at))
                         <= julianday(ds.discharged_at))
            ORDER BY julianday(ds.discharged_at) ASC, ds.id ASC LIMIT 1""",
        (admission["patient_id"], start, admission.get("id") or "", start),
    ).fetchone()
    return row["discharged_at"] if row is not None else None


def stay_end(db, admission: Mapping) -> datetime | None:
    """stay_end_stored as an aware UTC datetime."""
    return _parse(stay_end_stored(db, admission))


def latest_admission(db, patient_id: str) -> dict | None:
    """The patient's most recent admission by stay start, active or ended."""
    row = db.execute(
        f"""SELECT a.* FROM admissions a WHERE a.patient_id = ?
             ORDER BY julianday({stay_start_sql("a")}) DESC, a.rowid DESC LIMIT 1""",
        (patient_id,),
    ).fetchone()
    return dict(row) if row is not None else None


# ═════════════════════════════════════════════════════════════════════════════
#  Ends the beds and admissions module recorded with no summary
# ═════════════════════════════════════════════════════════════════════════════

_LEDGER_COLUMNS = frozenset(
    {"admission_id", "movement_type", "discharge_summary_id", "corrects_movement_id", "moved_at"}
)


def ledger_ready(db) -> bool:
    """True when patient_movements has the columns migration 51 gives it. A
    database without them has no ADT end to find (the same answer as an empty
    ledger), so callers return what they returned before."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(patient_movements)").fetchall()}
    return _LEDGER_COLUMNS <= cols


def summary_less_ends_sql() -> str:
    """A SELECT of every live summary-less end, one row per ended admission:
    admission_id, patient_id, ended_at, end_type, department, ward, bed,
    ward_id, bed_id, isolation_precautions, movement_id, status. No
    placeholders; wrap it as a subquery and filter on ended_at.

    Live: the admission is no longer active, and its latest discharge
    movement that no reopen has corrected (the latest WRITTEN, by rowid, never
    by its back-timeable moved_at) carries no summary id."""
    return (
        "SELECT a.id AS admission_id, a.patient_id AS patient_id, "
        "COALESCE(a.discharged_at, m.moved_at) AS ended_at, a.discharge_type AS end_type, "
        "a.department AS department, a.ward AS ward, a.bed AS bed, a.ward_id AS ward_id, "
        "a.bed_id AS bed_id, a.isolation_precautions AS isolation_precautions, "
        "m.id AS movement_id, a.status AS status "
        "FROM admissions a JOIN patient_movements m ON m.admission_id = a.id "
        "WHERE a.status <> 'active' AND m.movement_type = 'discharge' "
        "AND m.discharge_summary_id IS NULL "
        "AND m.rowid = (SELECT MAX(m2.rowid) FROM patient_movements m2 "
        "WHERE m2.admission_id = a.id AND m2.movement_type = 'discharge' "
        "AND NOT EXISTS (SELECT 1 FROM patient_movements c "
        "WHERE c.corrects_movement_id = m2.id))"
    )


def summary_less_ends(
    db, *, since: Any = None, until: Any = None, patient_id: str | None = None
) -> list[dict]:
    """The live summary-less ends whose end falls in [since, until), oldest
    first. Either bound may be None. Bounds are compared by julianday, so any
    stored spelling of either side compares correctly."""
    if not ledger_ready(db):
        return []
    sql = f"SELECT e.* FROM ({summary_less_ends_sql()}) e WHERE e.ended_at IS NOT NULL"
    params: list[Any] = []
    if since is not None:
        sql += " AND julianday(e.ended_at) >= julianday(?)"
        params.append(_bound(since))
    if until is not None:
        sql += " AND julianday(e.ended_at) < julianday(?)"
        params.append(_bound(until))
    if patient_id is not None:
        sql += " AND e.patient_id = ?"
        params.append(patient_id)
    sql += " ORDER BY julianday(e.ended_at) ASC, e.admission_id ASC"
    return [dict(r) for r in db.execute(sql, tuple(params)).fetchall()]


def count_summary_less_ends(db, *, since: Any = None, until: Any = None) -> int:
    """How many live summary-less ends fall in [since, until)."""
    return len(summary_less_ends(db, since=since, until=until))


def _bound(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ═════════════════════════════════════════════════════════════════════════════
#  The stay a discharge summary ended
# ═════════════════════════════════════════════════════════════════════════════


def admission_for_summary(db, summary: Mapping) -> dict | None:
    """The admission a discharge summary row ended, or None.

    1. With ORB_ADT on, the discharge movement that carries this summary's id
       names its admission exactly.
    2. Otherwise the patient's latest admission that began at or before the
       summary was written and is no longer active: discharge_patient closes
       the stay in the same transaction that writes the summary, and a later
       readmission begins after it, so it can never be taken for this one.
    None for a patient with no admission row at all (records written before
    admissions existed); the caller keeps its older behaviour for those."""
    summary_id = summary.get("id")
    patient_id = summary.get("patient_id")
    if summary_id is not None and ledger_ready(db):
        row = db.execute(
            """SELECT a.* FROM patient_movements m JOIN admissions a ON a.id = m.admission_id
                WHERE m.movement_type = 'discharge' AND m.discharge_summary_id = ?
                ORDER BY m.rowid DESC LIMIT 1""",
            (summary_id,),
        ).fetchone()
        if row is not None:
            return dict(row)
    when = summary.get("discharged_at")
    if not patient_id or not when:
        return None
    row = db.execute(
        f"""SELECT a.* FROM admissions a
             WHERE a.patient_id = ? AND a.status <> 'active'
               AND julianday({stay_start_sql("a")}) <= julianday(?)
             ORDER BY julianday({stay_start_sql("a")}) DESC, a.rowid DESC LIMIT 1""",
        (patient_id, when),
    ).fetchone()
    return dict(row) if row is not None else None
