"""Migration 71: the times a medication order is due (medorders-19, W8-I).

Ruling MO-R8 (W117), a safety ruling held for Samuel: a prescriber may set the
times an order is due, defaulted from the hospital's signed dose times
(server/site_config/dose_times.json), and the nurse board uses them instead of
the times it works out from the frequency. One table, empty until a site turns
ORB_MED_ORDER_LIFECYCLE on and a prescriber sets times
(services/medorders/schedule.py is the only writer).

med_order_schedule_times, one row per set time of one version of an order's
schedule:
  id                      integer key
  med_order_id            med_orders.id the times belong to
  patient_id              the order's patient, kept beside it so a patient's
                          erasure, export and reseed reach these rows like
                          every patient table (admin_routes._build_erase_plan
                          purges every table with a patient_id column)
  schedule_version        1 for the first set of times, then 2, 3, ...: the
                          rows written together share it
  due_time                'HH:MM' on the hospital's 24-hour clock
  basis                   'site_times' (the hospital's signed times for the
                          frequency, accepted unchanged) or 'prescriber'
                          (typed by the prescriber)
  site_times_version      the signed file's version, when basis is site_times
  site_times_fingerprint  the signed file's SHA-256, when basis is site_times
  authored_by, authored_at  who set these times, and when (UTC)
  superseded_at, superseded_by  set once when a later version replaces them,
                          the prescriber clears them, or the order's
                          frequency changes; NULL while in force
  created_at              when the row was written (UTC)

A row is written once and superseded once: the trigger below refuses any other
change, and any change at all to a superseded row, so who set which times
stays readable. DELETE is not refused, so the erase plan keeps working (as for
m61, m69). No FOREIGN KEY (a set time must never make an order undeletable),
no CURRENT_TIMESTAMP default (the writer stamps every time through
services/clinical_time).

The rules every migration here follows: one statement per conn.execute (never
executescript, which commits the runner's transaction), IF NOT EXISTS so a
second run changes nothing, and an index or trigger is created only when the
table has every column it names, so an unusual table of the same name cannot
stop boot.
"""
from migrations import Migration

#: Where a set of times came from. Shared with the writer.
BASES = ("site_times", "prescriber")

SCHEDULE_TIMES_DDL = """CREATE TABLE IF NOT EXISTS med_order_schedule_times (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    med_order_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    schedule_version INTEGER NOT NULL CHECK (schedule_version >= 1),
    due_time TEXT NOT NULL CHECK (
        length(due_time) = 5 AND due_time GLOB '[0-2][0-9]:[0-5][0-9]'
        AND due_time <= '23:59'),
    basis TEXT NOT NULL CHECK (basis IN ('site_times', 'prescriber')),
    site_times_version TEXT,
    site_times_fingerprint TEXT,
    authored_by TEXT NOT NULL,
    authored_at TEXT NOT NULL,
    superseded_at TEXT,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    CHECK ((superseded_at IS NULL) = (superseded_by IS NULL)),
    CHECK (basis = 'site_times' OR site_times_version IS NULL)
)"""

TABLE_NAMES = ("med_order_schedule_times",)

# (index name, key columns, unique, WHERE clause, every column it names)
_INDEXES = (
    # One row per time per version of an order's schedule.
    ("ux_med_order_schedule_times_slot", "med_order_id, schedule_version, due_time", True,
     None, ("med_order_id", "schedule_version", "due_time")),
    # The times in force, read by the nurse board for every linked drug.
    ("idx_med_order_schedule_times_current", "med_order_id", False,
     "superseded_at IS NULL", ("med_order_id", "superseded_at")),
    ("idx_med_order_schedule_times_patient", "patient_id, created_at", False, None,
     ("patient_id", "created_at")),
)

INDEX_NAMES = frozenset(entry[0] for entry in _INDEXES)

_IMMUTABLE = ("id", "med_order_id", "patient_id", "schedule_version", "due_time", "basis",
              "site_times_version", "site_times_fingerprint", "authored_by", "authored_at",
              "created_at")

#: A set time is superseded once and otherwise never changes.
WRITTEN_ONCE_TRIGGER = (
    "CREATE TRIGGER IF NOT EXISTS trg_med_order_schedule_times_written_once "
    "BEFORE UPDATE ON med_order_schedule_times "
    "WHEN OLD.superseded_at IS NOT NULL OR "
    + " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in _IMMUTABLE) + " "
    "BEGIN SELECT RAISE(ABORT, 'a set dose time is never rewritten: "
    "supersede it with a new version'); END"
)

TRIGGER_NAMES = frozenset({"trg_med_order_schedule_times_written_once"})


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply(conn) -> None:
    conn.execute(SCHEDULE_TIMES_DDL)
    present = _columns(conn, "med_order_schedule_times")
    for name, keyed, unique, where, needs in _INDEXES:
        if not set(needs) <= present:
            continue
        kind = "UNIQUE INDEX" if unique else "INDEX"
        clause = f" WHERE {where}" if where else ""
        conn.execute(
            f"CREATE {kind} IF NOT EXISTS {name} ON med_order_schedule_times({keyed}){clause}")
    if set(_IMMUTABLE) | {"superseded_at"} <= present:
        conn.execute(WRITTEN_ONCE_TRIGGER)


MIGRATION = Migration(71, "med_order_schedule_times", apply)
