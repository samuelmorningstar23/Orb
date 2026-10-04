"""Migration 57: the clinic session master (opd-15, W2-E).

Two tables, both CONFIGURATION, neither holds patient data:

  clinic_sessions            a doctor's weekly outpatient clinic: which weekday,
                             from when to when in hospital-local wall-clock time,
                             how long each slot is and how many patients a slot
                             takes, in which room, between which dates.
  clinic_session_exceptions  a day a session does not run (leave, a holiday, a
                             cancelled clinic): one session, or every session
                             the doctor holds that day. Withdrawn, never
                             deleted, so the history of a cancelled clinic stays
                             readable.

SLOTS ARE NOT STORED. services/clinic_schedule_service computes them from these
rows in hospital-local time (services.hospital_time) whenever they are asked
for, so there is no slot table that could drift from its session. Overlapping
sessions for one doctor are refused by the service, not here: the rule spans
weekday, time and validity dates and a CHECK cannot see other rows.

Times are 'HH:MM' strings (24-hour, hospital-local) and dates 'YYYY-MM-DD'
strings (hospital-local calendar days). Both compare correctly as text, which is
what the CHECKs and the indexes rely on. Stamps (created_at and the rest) are
UTC, in the canonical spelling services.clinical_time writes.

Text ids (never integers), so an audit row's target_id is the stored id
exactly (the int-versus-TEXT audit chain trap). No foreign keys, like every
other hospital-system table.

Idempotent: CREATE TABLE and CREATE INDEX IF NOT EXISTS only, one statement per
conn.execute (executescript would commit the runner's transaction).
"""
from migrations import Migration

_TABLES = (
    """CREATE TABLE IF NOT EXISTS clinic_sessions (
    id TEXT PRIMARY KEY,
    doctor_id TEXT NOT NULL,
    department TEXT NOT NULL,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL CHECK (start_time GLOB '[0-2][0-9]:[0-5][0-9]'),
    end_time TEXT NOT NULL CHECK (end_time GLOB '[0-2][0-9]:[0-5][0-9]'),
    slot_minutes INTEGER NOT NULL CHECK (slot_minutes BETWEEN 5 AND 240),
    capacity_per_slot INTEGER NOT NULL DEFAULT 1 CHECK (capacity_per_slot BETWEEN 1 AND 50),
    room TEXT,
    valid_from TEXT,
    valid_to TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    notes TEXT,
    row_version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_by TEXT,
    updated_at TEXT,
    retired_by TEXT,
    retired_at TEXT,
    retired_reason TEXT,
    CHECK (start_time < end_time),
    CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_from <= valid_to)
)""",
    """CREATE TABLE IF NOT EXISTS clinic_session_exceptions (
    id TEXT PRIMARY KEY,
    doctor_id TEXT NOT NULL,
    session_id TEXT,
    exception_date TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'cancelled',
    reason TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL,
    withdrawn_by TEXT,
    withdrawn_at TEXT,
    withdrawn_reason TEXT
)""",
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_clinic_sessions_doctor "
    "ON clinic_sessions(doctor_id, weekday)",
    "CREATE INDEX IF NOT EXISTS idx_clinic_sessions_department "
    "ON clinic_sessions(department, weekday)",
    "CREATE INDEX IF NOT EXISTS idx_clinic_session_exceptions_day "
    "ON clinic_session_exceptions(exception_date, doctor_id)",
)


def apply(conn) -> None:
    for statement in _TABLES:
        conn.execute(statement)
    for statement in _INDEXES:
        conn.execute(statement)


MIGRATION = Migration(57, "clinic_sessions", apply)
