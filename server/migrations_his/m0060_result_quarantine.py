"""Migration 60: the unmatched-results queue (lis-19), and what a correction
needs to find the result it corrects (lis-20).

1. result_quarantine (lis-19, W109)
   An interfaced result whose patient identifier matches nobody used to be
   stored in observations with patient_id NULL, where no chart, no ward pack
   and no Tier 1 lane ever sees it and nobody is asked to look. With
   ORB_RESULT_QUARANTINE on, services/ingestion_service.py writes such a
   result here instead, and laboratory staff (ingest.reconcile, ruling
   LIS-R17) either attach it to a patient, after a two-identifier check
   against the identifiers the sending system put in the message, or discard
   it with a reason. The administrator sees the list and the counts only.

   Columns:
     lane                   'ingestion' (an HL7, JSON or FHIR feed) or
                            'analyser' (the LIS analyser router, lis-24)
     reason                 why it is here, e.g. 'unmatched_patient'
     payload_json           the parsed result, everything the observations
                            row would have held
     source                 the provenance label it would have carried
     ingestion_event_id     the raw message (ingestion_events.id)
     source_observation_id  set only on a row the one-time sweep copied from
                            an observations row stored with no patient before
                            the queue was switched on; unique, so the sweep
                            can run at every boot and copy each row once
     raw_patient_*          the identifiers the sender put in the message
                            (PID-3, PID-5, PID-7), shown to laboratory staff
                            and checked by the attach step
     observation_code/name, observed_at
                            for the lists, without parsing the payload
     status                 'open', then 'attached' or 'discarded', once
     attached_*             the patient and the observations row (or LIS
                            result) it became
     resolved_by/at, resolution_reason, identity_confirmation_json
                            who decided, when, why, and which identifiers
                            were confirmed

   A resolved row cannot change again: an UPDATE of a row whose status is
   no longer 'open' is refused by a trigger. A DELETE is not refused, so a
   retention order signed by a person still works (consent_service holds
   every overdue row back as age-unknown, because a row with no patient
   cannot be aged).

2. observations gains obr_placer, obr_filler, obx_sub_id and hl7_sender
   (lis-20)
   An HL7 OBX-11 'C' replaces the earlier final result for the same patient,
   test code, OBX-4 sub-id and the same order: the filling laboratory's own
   number (OBR-3), or a placer number the sender supplied (OBR-2). The HL7
   adapter used to keep only OBR-2, and invented a random group id when OBR-2
   was empty, so a correction could not tell a real order number from an
   invented one. obr_placer holds OBR-2 only when the sender sent it; the
   random fallback never reaches it, so a correction can never match on one.
   obr_group_id keeps exactly what it always held. hl7_sender is the sending
   application and facility (MSH-3 and MSH-4), the namespace both order
   numbers belong to, so two laboratories that happen to use the same number
   are never confused. Every existing row reads NULL in all four, so no
   result stored before this migration can ever be superseded by a
   correction: it is stored beside it, as before.

The rules every migration here follows: one statement per conn.execute
(never executescript, which commits the runner's transaction), IF NOT EXISTS
and PRAGMA guards so a second run changes nothing, ALTER TABLE ADD COLUMN with
no default, no FOREIGN KEY, and no CURRENT_TIMESTAMP default (the writer
stamps every instant in the canonical spelling from services/clinical_time).
"""

from migrations import Migration

QUARANTINE_DDL = """CREATE TABLE IF NOT EXISTS result_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lane TEXT NOT NULL CHECK (lane IN ('ingestion', 'analyser')),
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source TEXT,
    ingestion_event_id INTEGER,
    source_observation_id INTEGER,
    raw_patient_identifier TEXT,
    raw_patient_name TEXT,
    raw_patient_dob TEXT,
    raw_specimen_id TEXT,
    instrument_id TEXT,
    observation_code TEXT,
    observation_name TEXT,
    observed_at TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'attached', 'discarded')),
    attached_patient_id TEXT,
    attached_observation_id INTEGER,
    attached_result_id INTEGER,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution_reason TEXT,
    identity_confirmation_json TEXT,
    created_at TEXT NOT NULL,
    CHECK (status = 'open' OR (resolved_by IS NOT NULL AND resolved_at IS NOT NULL
                               AND TRIM(COALESCE(resolution_reason, '')) != '')),
    CHECK (status != 'attached' OR attached_patient_id IS NOT NULL),
    CHECK (status = 'attached' OR attached_patient_id IS NULL)
)"""

TABLE_NAMES = ("result_quarantine",)

#: Correction identity columns added to observations (lis-20).
OBSERVATION_COLUMNS = (
    ("obr_placer", "TEXT"),
    ("obr_filler", "TEXT"),
    ("obx_sub_id", "TEXT"),
    ("hl7_sender", "TEXT"),
)

# (index name, table, columns, WHERE clause or "", unique)
_INDEXES = (
    ("idx_result_quarantine_status", "result_quarantine", ("status", "created_at"), "", False),
    (
        "idx_result_quarantine_event",
        "result_quarantine",
        ("ingestion_event_id",),
        "ingestion_event_id IS NOT NULL",
        False,
    ),
    (
        "ux_result_quarantine_source_observation",
        "result_quarantine",
        ("source_observation_id",),
        "source_observation_id IS NOT NULL",
        True,
    ),
    ("idx_observations_filler", "observations", ("obr_filler",), "obr_filler IS NOT NULL", False),
    ("idx_observations_placer", "observations", ("obr_placer",), "obr_placer IS NOT NULL", False),
)


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply(conn) -> None:
    conn.execute(QUARANTINE_DDL)
    observation_columns = _columns(conn, "observations")
    if observation_columns:
        # observations is a baseline table init_db creates before any
        # migration; a database without it has nothing to correct.
        for name, sql_type in OBSERVATION_COLUMNS:
            if name not in observation_columns:
                conn.execute(f"ALTER TABLE observations ADD COLUMN {name} {sql_type}")
    for name, table, columns, where, unique in _INDEXES:
        if not set(columns) <= _columns(conn, table):
            continue
        kind = "UNIQUE INDEX" if unique else "INDEX"
        clause = f" WHERE {where}" if where else ""
        conn.execute(f"CREATE {kind} IF NOT EXISTS {name} ON {table}({', '.join(columns)}){clause}")
    # Once attached or discarded, a decision is final: a second decision on
    # the same result would be a second, unreviewed wrong-patient decision.
    if {"status"} <= _columns(conn, "result_quarantine"):
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_result_quarantine_resolved_is_final "
            "BEFORE UPDATE ON result_quarantine WHEN OLD.status != 'open' "
            "BEGIN SELECT RAISE(ABORT, 'result_quarantine: a resolved result cannot change'); END"
        )


MIGRATION = Migration(60, "result_quarantine", apply)
