"""Migration 65: stay keying (opd-17b, W6-B; also adt-18a's index).

Until this migration Orb could not tell one stay from the next for the same
patient. Every patients row came from a fresh admission, so nobody needed to:
the patient was the stay. Readmission onto the same record (opd-17a, opd-17c,
and the Ward Record's returning patient) breaks that, so two things are keyed
on the admission here, once, for every database:

1. discharge_summaries.admission_id (TEXT, NULL allowed): the admission the
   summary ended. discharge_service.discharge_patient writes it from this
   migration on. Existing rows are filled only where the answer is certain:
     * the patient has exactly one admission row: that admission;
     * otherwise, a 'discharge' movement in the patient_movements ledger
       (ORB_ADT) names this summary and an admission of the same patient:
       that admission.
   Every other row stays NULL, including a patient with no admission row at
   all (records written before admissions existed) and a patient with several
   admissions and no ledger link. Readers treat NULL as "not known", never as
   "the latest stay" (the written audit in tests/test_stay_keying.py).

2. idx_admissions_one_active: a partial UNIQUE index on admissions(patient_id)
   WHERE status = 'active'. One active admission per patient, refused by the
   database whatever the code above it does: two admissions racing for one
   patient give one admission and an integrity error, never two open stays.
   Built only after a pre-check: if any patient already holds two or more
   active admissions the migration REFUSES, names those patient ids (ids
   only, never a name) and changes nothing, so the duplicates are closed by a
   person who can tell which stay is real. A silently half-built index is the
   failure this avoids. The index is always on, whatever the switches say.

idx_discharge_summaries_admission indexes the new column for the per-stay
readers (NABH inpatient days, the shadow scorecard census).

A NOTE FOR ANY FUTURE ADMISSIONS REBUILD, beside m0058's: the new table must
carry ONE_ACTIVE_INDEX below as well as m0058.ADMISSION_TRIGGERS and
ADMISSION_INDEXES. A rebuild that drops it silently allows two open stays for
one patient again.

Additive throughout: ALTER TABLE ADD COLUMN with no default, guarded by PRAGMA
table_info, one statement per execute, no foreign key (admissions stays a
table nothing references, so it can still be rebuilt in a transaction).
Idempotent: the column, the backfill (only NULL rows are filled) and both
indexes are safe to run twice.
"""

from __future__ import annotations

import logging

from migrations import Migration

logger = logging.getLogger("orb.migrations")

#: One active admission per patient (adt-18a's M-g, opd-17b's index).
ONE_ACTIVE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_admissions_one_active "
    "ON admissions(patient_id) WHERE status = 'active'"
)
ONE_ACTIVE_INDEX_NAME = "idx_admissions_one_active"

SUMMARY_ADMISSION_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_discharge_summaries_admission "
    "ON discharge_summaries(admission_id)"
)
SUMMARY_ADMISSION_INDEX_NAME = "idx_discharge_summaries_admission"

#: How many patient ids the refusal names in its message (the count is always
#: given in full).
NAMED_IN_REFUSAL = 20


class OneActiveAdmissionRefused(RuntimeError):
    """Some patient already holds two or more active admissions, so the
    one-active-admission index cannot be built. Nothing was changed."""

    def __init__(self, patient_ids: list[str]):
        self.patient_ids = list(patient_ids)
        named = ", ".join(self.patient_ids[:NAMED_IN_REFUSAL])
        more = len(self.patient_ids) - NAMED_IN_REFUSAL
        super().__init__(
            f"Migration 65 refused: {len(self.patient_ids)} patient(s) hold more than one "
            f"active admission ({named}{f', and {more} more' if more > 0 else ''}). Close the "
            "admissions that are not the patient's current stay, then start Orb again. "
            "Nothing was changed."
        )


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def patients_with_several_active_admissions(conn) -> list[str]:
    """Patient ids holding two or more active admissions, sorted. Empty on a
    database with no admissions table."""
    if not _columns(conn, "admissions"):
        return []
    rows = conn.execute(
        "SELECT patient_id FROM admissions WHERE status = 'active' "
        "GROUP BY patient_id HAVING COUNT(*) > 1 ORDER BY patient_id"
    ).fetchall()
    return [r[0] for r in rows]


def add_summary_admission_id(conn) -> bool:
    """The column. False when there is no discharge_summaries table."""
    cols = _columns(conn, "discharge_summaries")
    if not cols:
        return False
    if "admission_id" not in cols:
        conn.execute("ALTER TABLE discharge_summaries ADD COLUMN admission_id TEXT")
    return True


def backfill_summary_admission_id(conn) -> dict:
    """Fill admission_id where the stay is certain (module docstring). Only
    NULL rows are touched, so a value discharge_service wrote is never
    overwritten. Returns {single_admission, ledger}: rows filled by each rule."""
    filled = {"single_admission": 0, "ledger": 0}
    if not _columns(conn, "admissions"):
        return filled
    cur = conn.execute(
        "UPDATE discharge_summaries SET admission_id = ("
        "SELECT a.id FROM admissions a WHERE a.patient_id = discharge_summaries.patient_id) "
        "WHERE admission_id IS NULL "
        "AND (SELECT COUNT(*) FROM admissions a "
        "WHERE a.patient_id = discharge_summaries.patient_id) = 1 "
        "AND (SELECT a.id FROM admissions a "
        "WHERE a.patient_id = discharge_summaries.patient_id) IS NOT NULL"
    )
    filled["single_admission"] = max(0, cur.rowcount or 0)
    if {"admission_id", "movement_type", "discharge_summary_id"} <= _columns(
        conn, "patient_movements"
    ):
        linked = (
            "SELECT m.admission_id FROM patient_movements m "
            "JOIN admissions a ON a.id = m.admission_id "
            "WHERE m.movement_type = 'discharge' "
            "AND m.discharge_summary_id = discharge_summaries.id "
            "AND a.patient_id = discharge_summaries.patient_id "
            "ORDER BY m.rowid DESC LIMIT 1"
        )
        cur = conn.execute(
            f"UPDATE discharge_summaries SET admission_id = ({linked}) "
            f"WHERE admission_id IS NULL AND ({linked}) IS NOT NULL"
        )
        filled["ledger"] = max(0, cur.rowcount or 0)
    return filled


def apply(conn) -> None:
    # The pre-check first, before any change: a refusal leaves the database
    # exactly as it was (the runner also rolls back the whole boot's batch).
    several = patients_with_several_active_admissions(conn)
    if several:
        raise OneActiveAdmissionRefused(several)
    if add_summary_admission_id(conn):
        filled = backfill_summary_admission_id(conn)
        if filled["single_admission"] or filled["ledger"]:
            logger.info(
                "Migration 65: %d summary(ies) keyed to their only admission, %d "
                "through the movement ledger",
                filled["single_admission"],
                filled["ledger"],
            )
        conn.execute(SUMMARY_ADMISSION_INDEX)
    if _columns(conn, "admissions"):
        conn.execute(ONE_ACTIVE_INDEX)


MIGRATION = Migration(65, "stay_keying", apply)
