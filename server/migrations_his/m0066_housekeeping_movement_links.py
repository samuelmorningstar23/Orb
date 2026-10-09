"""Migration 66: which bed movements housekeeping has acted on (adt-7).

One table, empty until a site switches Beds and admissions (ORB_ADT) on. The
housekeeping turnaround sync (services/housekeeping_service.
sync_adt_movements) reads the movement ledger (patient_movements) and, for
each movement that takes a patient out of a bed or puts one into a bed,
raises the turnaround clean or records the bed occupied. This table is how it
knows it already has: one row per movement and side, written in the same
transaction as what it did, so running the sync ten times acts once.

housekeeping_movement_links
  movement_id   patient_movements.id
  side          'from' (the bed the patient left) or 'to' (the bed the
                patient went into). One movement can do both (a transfer from
                bed 3 to bed 7), so the key is the movement and the side.
  unit_id       the housekeeping unit (the bed) the side named, or NULL when
                the bed is not on the housekeeping register
  effect        what the sync did:
                  vacated           a turnaround clean was raised and the bed
                                    marked as needing cleaning
                  task_raised       a turnaround clean was raised; the bed was
                                    already dirty, being cleaned or out of
                                    service, so its state was left alone
                  occupied          a bed recorded clean was marked occupied
                  occupied_unclean  a bed NOT recorded clean was marked
                                    occupied, the skipped clean recorded on
                                    the bed's own ledger naming the movement
                                    (housekeeping's force_occupied record)
                  skipped           nothing to do, `note` says why (the bed is
                                    not on the register, its ward's cleaning is
                                    not tracked, a clean was already raised for
                                    this end of stay, the bed was already
                                    occupied)
  task_id       the turnaround task raised or found, when there is one
  note          the reason a side was skipped, in a word
  applied_at    when the sync acted (UTC, canonical spelling)

No patient column: a movement id names no patient by itself, and nothing a
housekeeper reads carries a patient field. No FOREIGN KEY (a link must never
make a movement or a unit undeletable), no REAL column, no CURRENT_TIMESTAMP
default (the writer stamps the time through services/clinical_time).

The rules every migration here follows: one statement per conn.execute
(never executescript, which commits the runner's transaction), IF NOT EXISTS
so a second run changes nothing, and an index is created only when its table
has every column it names, so an unusual table of the same name cannot stop
boot.
"""
from migrations import Migration

#: The vocabulary of `effect`, shared with the writer.
LINK_EFFECTS = ("occupied", "occupied_unclean", "vacated", "task_raised", "skipped")

LINKS_DDL = """CREATE TABLE IF NOT EXISTS housekeeping_movement_links (
    movement_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('from', 'to')),
    unit_id TEXT,
    effect TEXT NOT NULL CHECK (effect IN (
        'occupied', 'occupied_unclean', 'vacated', 'task_raised', 'skipped')),
    task_id TEXT,
    note TEXT,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (movement_id, side)
)"""

TABLE_NAMES = ("housekeeping_movement_links",)

# (index name, table, columns, unique)
_INDEXES = (
    ("idx_hk_movement_links_unit", "housekeeping_movement_links", ("unit_id",), False),
)


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_links_on(conn) -> None:
    """Create the table and its index if absent, on the caller's connection.
    The migration's apply and the sync's lazy fallback share it."""
    conn.execute(LINKS_DDL)
    for name, table, columns, unique in _INDEXES:
        if not set(columns) <= _columns(conn, table):
            continue
        kind = "UNIQUE INDEX" if unique else "INDEX"
        conn.execute(f"CREATE {kind} IF NOT EXISTS {name} ON {table}({', '.join(columns)})")


def apply(conn) -> None:
    ensure_links_on(conn)


MIGRATION = Migration(66, "housekeeping_movement_links", apply)
