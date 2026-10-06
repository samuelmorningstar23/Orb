"""Migration 62: the outpatient consultation record (opd-10).

Two new tables, empty until a site switches the 'opd' feature on (ORB_OPD,
which also needs ORB_REGISTRATION and the front desk). Nothing is altered, no
column is added to an existing table, and nothing here is a foreign key, so no
table becomes an FK parent. One statement per conn.execute, never
executescript (it would commit the runner's transaction).

1. opd_consultations: one consultation note per OPD visit.

   Columns:
     id                   'OPC-' plus 8 hex characters
     visit_id             opd_visits.id; unique, so a visit has one note
     patient_id           the visit's patient
     department           the visit's clinic when the note was started; the
                          note is read and written through the visit, whose
                          department is the scope key
     author_id            who started the note
     status               'draft' (editable, saved with row_version) or
                          'signed' (final; corrections go in an addendum)
     chief_complaint, history, examination, plan, advice
                          free text
     vitals_json          {values, measured_at, measured_by}: what the
                          consulting doctor measured, kept here and NEVER
                          written to vitals or risk_scores and never scored
                          (rulings OPD-S3 and OPD-S6; tests/test_opd_no_scoring.py)
     diagnoses_json       a list of {text, code_system, code, code_status}.
                          Free text is the diagnosis; a code is optional and
                          code_status is 'validated' (found in terminology),
                          'unvalidated' (kept as typed, never refused) or
                          'none' (ruling OPD-N11)
     follow_up_date, follow_up_note, referral_department, admission_advised
     row_version          bumped on every save; a save that names an older
                          version is refused (409), so two doctors cannot
                          silently overwrite each other
     created_at, updated_at, updated_by, signed_by, signed_at

   A signed note cannot change: an UPDATE of a row whose status is 'signed'
   is refused by a trigger, whoever issues it.

2. opd_consultation_addenda: append-only additions to a signed note. An
   UPDATE is refused by a trigger.

DELETE IS NOT REFUSED ON EITHER TABLE, on purpose, the same choice migration
60 made for result_quarantine: routes/admin_routes._build_erase_plan deletes
every patient-keyed table it is not told to keep, and
tests/test_erase_plan_retained.py fails the plan if it would touch a table
with a delete-refusing trigger. Nothing in Orb deletes from these tables; a
retention order signed by a person (consent_service) still can.

Both tables are registered for retention review in consent_service
(_RETENTION_SOURCES, wave 1) and in the restore verifier's manifest
(scripts/lib/verify_restore.py, group 'outpatient consultations').
"""
from migrations import Migration

CONSULTATIONS_DDL = """CREATE TABLE IF NOT EXISTS opd_consultations (
    id TEXT PRIMARY KEY,
    visit_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    department TEXT NOT NULL,
    author_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'signed')),
    chief_complaint TEXT,
    history TEXT,
    examination TEXT,
    vitals_json TEXT,
    diagnoses_json TEXT,
    plan TEXT,
    advice TEXT,
    follow_up_date TEXT,
    follow_up_note TEXT,
    referral_department TEXT,
    admission_advised INTEGER NOT NULL DEFAULT 0 CHECK (admission_advised IN (0, 1)),
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    signed_by TEXT,
    signed_at TEXT,
    CHECK (status = 'draft' OR (signed_by IS NOT NULL AND signed_at IS NOT NULL))
)"""

ADDENDA_DDL = """CREATE TABLE IF NOT EXISTS opd_consultation_addenda (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    consultation_id TEXT NOT NULL,
    visit_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    body TEXT NOT NULL CHECK (length(trim(body)) > 0),
    created_at TEXT NOT NULL
)"""

TABLE_NAMES = ("opd_consultations", "opd_consultation_addenda")

INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_opd_consultations_visit "
    "ON opd_consultations(visit_id)",
    "CREATE INDEX IF NOT EXISTS idx_opd_consultations_patient "
    "ON opd_consultations(patient_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_opd_consultations_signed "
    "ON opd_consultations(patient_id, signed_at) WHERE status = 'signed'",
    "CREATE INDEX IF NOT EXISTS idx_opd_consultation_addenda_consultation "
    "ON opd_consultation_addenda(consultation_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_opd_consultation_addenda_patient "
    "ON opd_consultation_addenda(patient_id, created_at)",
)

TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS trg_opd_consultations_signed_is_final
       BEFORE UPDATE ON opd_consultations WHEN OLD.status = 'signed'
       BEGIN
           SELECT RAISE(ABORT, 'opd_consultations: a signed consultation cannot change; add an addendum');
       END""",
    """CREATE TRIGGER IF NOT EXISTS trg_opd_consultation_addenda_no_update
       BEFORE UPDATE ON opd_consultation_addenda
       BEGIN
           SELECT RAISE(ABORT, 'opd_consultation_addenda is append-only: add a new addendum instead');
       END""",
)


def apply(conn) -> None:
    conn.execute(CONSULTATIONS_DDL)
    conn.execute(ADDENDA_DDL)
    for statement in INDEXES:
        conn.execute(statement)
    for statement in TRIGGERS:
        conn.execute(statement)


MIGRATION = Migration(62, "opd_consultations", apply)
