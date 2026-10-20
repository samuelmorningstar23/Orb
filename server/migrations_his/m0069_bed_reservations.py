"""Migration 69: bed reservations (adt-12, W7-D).

One table, empty until a site switches Beds and admissions (ORB_ADT) on and a
bed manager, a ward clerk or a doctor holds a bed for a patient who is on the
way: an admission from the emergency room, a transfer from another ward, a
patient coming back from surgery, a planned admission.

bed_reservations
  id              'RES-' plus 12 hex
  bed_id          housekeeping_units.id of the master bed held
  ward_id         wards.id of that bed's ward, kept beside it so a ward's
                  reservations are read without a join
  patient_id      the patient the bed is held for, or NULL for a bed held for
                  a patient not yet named (it is then placeable by nobody
                  until it is released or expires)
  admission_id    the active admission the bed is held for (a transfer, a
                  return from surgery), or NULL. When set, patient_id is that
                  admission's patient.
  purpose         admission, transfer, post_op, elective or other
  note            free text the person reserving typed, or NULL
  status          active, fulfilled (the patient was placed in the bed),
                  released (a person let it go, with a reason) or expired
                  (its time ran out). At most ONE active row per bed (the
                  partial unique index below).
  reserved_by     users.id of who reserved it
  reserved_at     when it was reserved (UTC, canonical spelling)
  expires_at      when it stops holding the bed (UTC). An active row past this
                  time holds nothing: every reader goes through
                  services/adt/reservations.py, which treats it as expired,
                  and the next reservation write on that bed records it so.
  closed_at       when it stopped being active (NULL while active)
  closed_by       who closed it (NULL for an expiry)
  close_reason    why: the release reason, 'Patient placed in the bed', or
                  'Expired'
  movement_id     the patient_movements row that fulfilled it, or NULL
  idempotency_key the Idempotency-Key of the request that made it, so a
                  retried request reserves once

A row is written once and closed once: the trigger below refuses any change
to a row that is no longer active, so a released or fulfilled reservation
can never become active again (a new reservation is a new row). DELETE is not
refused, so the patient erase plan (admin_routes._build_erase_plan, which
purges every table with a patient_id column) keeps working, as for m60 to
m66.

No FOREIGN KEY (a reservation must never make a bed, a patient or an
admission undeletable), no REAL column, no CURRENT_TIMESTAMP default (the
writer stamps every time through services/clinical_time).

The rules every migration here follows: one statement per conn.execute
(never executescript, which commits the runner's transaction), IF NOT EXISTS
so a second run changes nothing, and an index or trigger is created only when
the table has every column it names, so an unusual table of the same name
cannot stop boot.
"""

from migrations import Migration

#: Why a bed is held. Shared with the writer (services/adt/reservations.py).
PURPOSES = ("admission", "transfer", "post_op", "elective", "other")

#: A reservation's life: active, then exactly one of the other three.
STATUSES = ("active", "fulfilled", "released", "expired")

RESERVATIONS_DDL = """CREATE TABLE IF NOT EXISTS bed_reservations (
    id TEXT PRIMARY KEY,
    bed_id TEXT NOT NULL,
    ward_id TEXT NOT NULL,
    patient_id TEXT,
    admission_id TEXT,
    purpose TEXT NOT NULL CHECK (purpose IN (
        'admission', 'transfer', 'post_op', 'elective', 'other')),
    note TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
        'active', 'fulfilled', 'released', 'expired')),
    reserved_by TEXT,
    reserved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    closed_by TEXT,
    close_reason TEXT,
    movement_id TEXT,
    idempotency_key TEXT,
    CHECK (status = 'active' OR closed_at IS NOT NULL),
    CHECK (admission_id IS NULL OR patient_id IS NOT NULL)
)"""

TABLE_NAMES = ("bed_reservations",)

# (index name, key columns, unique, WHERE clause, every column it names)
_INDEXES = (
    # One active reservation per bed: a second is refused underneath the
    # writer's own check.
    ("idx_bed_reservations_active_bed", "bed_id", True, "status = 'active'", ("bed_id", "status")),
    (
        "idx_bed_reservations_key",
        "idempotency_key",
        True,
        "idempotency_key IS NOT NULL",
        ("idempotency_key",),
    ),
    ("idx_bed_reservations_patient", "patient_id", False, None, ("patient_id",)),
    ("idx_bed_reservations_admission", "admission_id", False, None, ("admission_id",)),
    ("idx_bed_reservations_ward_status", "ward_id, status", False, None, ("ward_id", "status")),
)

INDEX_NAMES = frozenset(entry[0] for entry in _INDEXES)

#: A closed reservation never changes again.
CLOSED_ROW_TRIGGER = (
    "CREATE TRIGGER IF NOT EXISTS trg_bed_reservations_closed_final "
    "BEFORE UPDATE ON bed_reservations "
    "WHEN OLD.status <> 'active' "
    "BEGIN SELECT RAISE(ABORT, 'a closed bed reservation cannot be changed'); END"
)

TRIGGER_NAMES = frozenset({"trg_bed_reservations_closed_final"})


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_reservations_on(conn) -> None:
    """Create the table, its indexes and its trigger if absent, on the
    caller's connection. Idempotent."""
    conn.execute(RESERVATIONS_DDL)
    present = _columns(conn, "bed_reservations")
    for name, keyed, unique, where, needs in _INDEXES:
        if not set(needs) <= present:
            continue
        kind = "UNIQUE INDEX" if unique else "INDEX"
        clause = f" WHERE {where}" if where else ""
        conn.execute(f"CREATE {kind} IF NOT EXISTS {name} ON bed_reservations({keyed}){clause}")
    if "status" in present:
        conn.execute(CLOSED_ROW_TRIGGER)


def apply(conn) -> None:
    ensure_reservations_on(conn)


MIGRATION = Migration(69, "bed_reservations", apply)
