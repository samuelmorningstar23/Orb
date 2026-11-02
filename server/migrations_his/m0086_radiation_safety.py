"""Local equipment-linked licence and radiation safety officer evidence."""
from migrations import Migration


def apply(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS radiation_safety_records (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL, asset_id INTEGER NOT NULL,
        asset_snapshot_json TEXT NOT NULL, record_kind TEXT NOT NULL CHECK(record_kind IN ('licence','officer')),
        reference TEXT NOT NULL, authority TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
        officer_name TEXT, qualification_reference TEXT, scope_description TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'recorded' CHECK(state IN ('recorded','withdrawn')),
        withdrawal_reason TEXT, version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, created_by TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(facility_id,asset_id,record_kind,reference))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_radiation_safety_expiry ON radiation_safety_records(facility_id,state,valid_to)")
    conn.execute("""CREATE TABLE IF NOT EXISTS radiation_safety_events (
        id TEXT PRIMARY KEY, record_id TEXT NOT NULL, actor_id TEXT NOT NULL,
        action TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL,
        snapshot_json TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_radiation_safety_events_record ON radiation_safety_events(record_id,occurred_at)")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_radiation_safety_events_no_update
        BEFORE UPDATE ON radiation_safety_events BEGIN SELECT RAISE(ABORT,'Safety history cannot be edited'); END""")


MIGRATION = Migration(86, "radiation_safety", apply)
