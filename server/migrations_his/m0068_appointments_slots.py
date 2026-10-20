"""Migration 68: appointments with a doctor and a slot (opd-16, W7-A).

appointments (database.SCHEMA, plus migration 28's origin and department) had
no doctor and no slot, and only three statuses. SQLite cannot ALTER a CHECK
constraint, so this is a table rebuild: create appointments_new, copy every
row, drop the old table, rename, recreate the indexes.

IN THE RUNNER'S TRANSACTION, NOT database._atomic_table_rebuild. appointments
is not an FK parent (no table references it; opd_visits.appointment_id is a
plain TEXT column), so the drop and rename need no PRAGMA foreign_keys window
and the whole rebuild commits or rolls back with every other pending
migration. Every statement is its own conn.execute (executescript would commit
the runner's transaction and leave a half-built table on a crash).

WHAT THE NEW TABLE ADDS
  * statuses 'checked_in' (the patient arrived and an OPD visit was opened for
    them) and 'no_show' (marked by the desk); 'booked', 'cancelled' and
    'completed' keep their meaning;
  * doctor_id, session_id (clinic_sessions, migration 57) and slot_position:
    a doctor's slot that takes N patients holds positions 1 to N;
  * checked_in_at and checked_in_by, opd_visit_id (the visit check-in opened),
    no_show_at, no_show_by, cancel_reason, cancelled_at, cancelled_by,
    completed_at, completed_by, and rescheduled_from_id (a reschedule is a new
    row pointing at the one it replaced; the old row is kept).
  * origin NOT NULL DEFAULT 'staff' and department (migration 28) are carried
    with their values; every existing row keeps every value it had, and every
    new column starts NULL.

GUARDS, all checked before anything changes:
  * a live appointments column this file does not know aborts the rebuild (it
    would be dropped);
  * an index or trigger on appointments other than the two known indexes
    aborts it (it would be dropped with the old table);
  * ORPHANS: a row whose patient_id names no patient, or whose
    booked_by_user_id names no user, aborts the rebuild with the appointment
    ids listed (ids only), because the new table keeps both foreign keys and
    the copy would fail on them halfway. Nothing changes until a person fixes
    or removes those rows;
  * CLASHES: before the partial unique index on live doctor slots is built,
    two live rows holding the same doctor, day, time and position abort it,
    listed by id;
  * the copy's row count must equal the source's, and PRAGMA
    foreign_key_check(appointments) must find nothing, inside the transaction.

Indexes after the rebuild: idx_appointments_patient and idx_appointments_idem
exactly as database.init_db writes them (its CREATE INDEX IF NOT EXISTS then
finds them present on every later boot), plus idx_appointments_doctor_slot
(UNIQUE over live doctor slots: a slot position is held by one live booking),
idx_appointments_day (the day view) and idx_appointments_visit.

Idempotent: on a table that already has the new shape it only makes sure the
indexes exist (after the clash pre-check).
"""

from __future__ import annotations

from migrations import Migration

STATUSES: tuple = ("booked", "cancelled", "completed", "checked_in", "no_show")

#: Every appointments column before this migration, in the order a fresh
#: database has them: database.SCHEMA's ten, then migration 28's two.
OLD_COLUMNS: tuple = (
    "id",
    "patient_id",
    "booked_by_user_id",
    "scheduled_date",
    "scheduled_time",
    "appointment_type",
    "reason",
    "status",
    "idempotency_key",
    "created_at",
    "origin",
    "department",
)

#: The columns this migration adds, all NULL on every existing row.
ADDED_COLUMNS: tuple = (
    "doctor_id",
    "session_id",
    "slot_position",
    "checked_in_at",
    "checked_in_by",
    "opd_visit_id",
    "no_show_at",
    "no_show_by",
    "cancel_reason",
    "cancelled_at",
    "cancelled_by",
    "completed_at",
    "completed_by",
    "rescheduled_from_id",
)

NEW_COLUMNS: tuple = OLD_COLUMNS + ADDED_COLUMNS

#: The two indexes database.init_db puts on appointments, exactly as it writes
#: them. Dropped with the old table and recreated after the rename.
KNOWN_INDEXES: dict = {
    "idx_appointments_patient": (
        "CREATE INDEX IF NOT EXISTS idx_appointments_patient "
        "ON appointments(patient_id, scheduled_date)"
    ),
    "idx_appointments_idem": (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_idem "
        "ON appointments(idempotency_key) WHERE idempotency_key IS NOT NULL"
    ),
}

#: A live doctor slot position is held by one booking. 'cancelled' frees it
#: (a cancelled or rescheduled booking gives its slot back).
DOCTOR_SLOT_INDEX_NAME = "idx_appointments_doctor_slot"
DOCTOR_SLOT_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_doctor_slot "
    "ON appointments(doctor_id, scheduled_date, scheduled_time, slot_position) "
    "WHERE doctor_id IS NOT NULL AND status != 'cancelled'"
)

NEW_INDEXES: tuple = (
    "CREATE INDEX IF NOT EXISTS idx_appointments_day "
    "ON appointments(scheduled_date, department, doctor_id)",
    "CREATE INDEX IF NOT EXISTS idx_appointments_visit "
    "ON appointments(opd_visit_id) WHERE opd_visit_id IS NOT NULL",
)


class AppointmentsRebuildRefused(RuntimeError):
    """The rebuild would lose or break rows; nothing was changed. `ids` names
    the appointment rows (ids only) a person must look at first."""

    def __init__(self, message: str, ids: list | None = None):
        self.ids = list(ids or [])
        super().__init__(message + (f": {', '.join(self.ids)}" if self.ids else ""))


def _columns(conn) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(appointments)").fetchall()]


def _table_sql(conn) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'appointments'"
    ).fetchone()
    return (row[0] or "") if row else ""


def has_new_shape(conn) -> bool:
    """True when appointments already carries the doctor and slot columns and
    the widened status CHECK."""
    cols = set(_columns(conn))
    return set(ADDED_COLUMNS) <= cols and "'no_show'" in _table_sql(conn)


def live_slot_clashes(conn) -> list[str]:
    """Ids of live rows sharing a doctor, day, time and slot position."""
    rows = conn.execute(
        "SELECT a.id FROM appointments a JOIN ("
        "  SELECT doctor_id, scheduled_date, scheduled_time, slot_position"
        "    FROM appointments"
        "   WHERE doctor_id IS NOT NULL AND status != 'cancelled'"
        "   GROUP BY doctor_id, scheduled_date, scheduled_time, slot_position"
        "  HAVING COUNT(*) > 1) d"
        "  ON d.doctor_id = a.doctor_id AND d.scheduled_date = a.scheduled_date"
        " AND d.scheduled_time = a.scheduled_time"
        " AND d.slot_position IS a.slot_position"
        " WHERE a.status != 'cancelled' ORDER BY a.id"
    ).fetchall()
    return [str(r[0]) for r in rows]


def orphan_rows(conn) -> list[str]:
    """Ids of rows whose patient or booking user does not exist."""
    rows = conn.execute(
        "SELECT a.id FROM appointments a"
        " WHERE NOT EXISTS (SELECT 1 FROM patients p WHERE p.id = a.patient_id)"
        "    OR (a.booked_by_user_id IS NOT NULL"
        "        AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id = a.booked_by_user_id))"
        " ORDER BY a.id"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _ensure_indexes(conn) -> None:
    for statement in KNOWN_INDEXES.values():
        conn.execute(statement)
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
        (DOCTOR_SLOT_INDEX_NAME,),
    ).fetchone()
    if present is None:
        clashes = live_slot_clashes(conn)
        if clashes:
            raise AppointmentsRebuildRefused(
                "Migration 68 cannot build the one-booking-per-slot index: these "
                "live appointments hold the same doctor's slot",
                clashes,
            )
        conn.execute(DOCTOR_SLOT_INDEX)
    for statement in NEW_INDEXES:
        conn.execute(statement)


def _rebuild_in_transaction(conn, *, create_new_sql: str) -> None:
    """Create appointments_new, copy, prove, drop, rename. Every statement in
    the caller's (the runner's) transaction."""
    conn.execute("DROP TABLE IF EXISTS appointments_new")
    conn.execute(create_new_sql)
    shared = ", ".join(OLD_COLUMNS)
    conn.execute(f"INSERT INTO appointments_new ({shared}) SELECT {shared} FROM appointments")
    src = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    dst = conn.execute("SELECT COUNT(*) FROM appointments_new").fetchone()[0]
    if src != dst:
        raise AppointmentsRebuildRefused(
            f"Migration 68 copied {dst} of {src} appointments; nothing was changed"
        )
    conn.execute("DROP TABLE appointments")
    conn.execute("ALTER TABLE appointments_new RENAME TO appointments")


def apply(conn) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'appointments'"
    ).fetchone()
    if exists is None:
        # database.SCHEMA creates appointments before any migration runs; a
        # database without it is not one this migration can reason about.
        raise AppointmentsRebuildRefused(
            "Migration 68 found no appointments table; nothing was changed"
        )

    if has_new_shape(conn):
        _ensure_indexes(conn)
        return

    cols = _columns(conn)
    unknown = [c for c in cols if c not in OLD_COLUMNS]
    if unknown:
        raise AppointmentsRebuildRefused(
            "Migration 68 would drop appointments columns it does not know "
            f"({', '.join(unknown)}); nothing was changed"
        )
    missing = [c for c in OLD_COLUMNS if c not in cols]
    if missing:
        raise AppointmentsRebuildRefused(
            "Migration 68 expects migration 28's appointments columns "
            f"({', '.join(missing)} missing); nothing was changed"
        )

    others = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE tbl_name = 'appointments' "
        "AND type IN ('index', 'trigger') AND name NOT LIKE 'sqlite_autoindex_%' "
        "ORDER BY type, name"
    ).fetchall()
    unknown_objects = [f"{r[0]} {r[1]}" for r in others if r[1] not in KNOWN_INDEXES]
    if unknown_objects:
        raise AppointmentsRebuildRefused(
            "Migration 68 would drop objects on appointments it does not know "
            f"({', '.join(unknown_objects)}); nothing was changed"
        )

    orphans = orphan_rows(conn)
    if orphans:
        raise AppointmentsRebuildRefused(
            "Migration 68 cannot copy appointments whose patient or booking user no "
            "longer exists; fix or remove them first",
            orphans,
        )

    _rebuild_in_transaction(
        conn,
        create_new_sql="""CREATE TABLE appointments_new (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    booked_by_user_id TEXT REFERENCES users(id),
    scheduled_date TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    appointment_type TEXT NOT NULL DEFAULT 'Follow-up',
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'booked' CHECK (status IN (
        'booked', 'cancelled', 'completed', 'checked_in', 'no_show')),
    idempotency_key TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    origin TEXT NOT NULL DEFAULT 'staff',
    department TEXT,
    doctor_id TEXT,
    session_id TEXT,
    slot_position INTEGER CHECK (slot_position IS NULL OR slot_position >= 1),
    checked_in_at TEXT,
    checked_in_by TEXT,
    opd_visit_id TEXT,
    no_show_at TEXT,
    no_show_by TEXT,
    cancel_reason TEXT,
    cancelled_at TEXT,
    cancelled_by TEXT,
    completed_at TEXT,
    completed_by TEXT,
    rescheduled_from_id TEXT,
    CHECK (doctor_id IS NULL OR slot_position IS NOT NULL)
)""",
    )
    _ensure_indexes(conn)

    broken = conn.execute("PRAGMA foreign_key_check(appointments)").fetchall()
    if broken:
        raise AppointmentsRebuildRefused(
            "Migration 68 left appointments pointing at missing rows; rolled back",
            sorted(
                {
                    str(
                        conn.execute(
                            "SELECT id FROM appointments WHERE rowid = ?", (r[1],)
                        ).fetchone()[0]
                    )
                    for r in broken
                }
            ),
        )


MIGRATION = Migration(68, "appointments_slots", apply)
