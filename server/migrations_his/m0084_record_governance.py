"""Local legal holds and medical-record custody/release registers."""
from migrations import Migration


def apply(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS record_legal_holds (
        id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, facility_id INTEGER NOT NULL,
        department TEXT, categories_json TEXT NOT NULL, reason TEXT NOT NULL,
        authority_reference TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL,
        released_at TEXT, released_by TEXT, release_reason TEXT, release_reference TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_legal_holds_patient ON record_legal_holds(patient_id, released_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS mrd_files (
        id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, admission_id TEXT,
        facility_id INTEGER NOT NULL, department TEXT, file_number TEXT NOT NULL,
        medium TEXT NOT NULL CHECK(medium IN ('paper','digital_reference')),
        location TEXT NOT NULL, custodian TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('archived','checked_out','in_transit','missing')),
        created_at TEXT NOT NULL, created_by TEXT NOT NULL,
        updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        UNIQUE(facility_id, file_number))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mrd_release_requests (
        id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, facility_id INTEGER NOT NULL,
        department TEXT, purpose TEXT NOT NULL, recipient TEXT NOT NULL,
        documents_json TEXT NOT NULL, authority_reference TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('requested','approved','released','denied','cancelled')),
        decision_reason TEXT, handover_reference TEXT,
        created_at TEXT NOT NULL, created_by TEXT NOT NULL, updated_at TEXT NOT NULL,
        decided_by TEXT, released_by TEXT, version INTEGER NOT NULL DEFAULT 1)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS record_governance_events (
        id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, facility_id INTEGER NOT NULL,
        department TEXT, record_type TEXT NOT NULL, record_id TEXT NOT NULL,
        action TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL,
        actor_id TEXT NOT NULL, version INTEGER NOT NULL)""")
    for table in ("mrd_files", "mrd_release_requests", "record_governance_events"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_patient ON {table}(patient_id)")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_governance_events_no_update
        BEFORE UPDATE ON record_governance_events BEGIN
        SELECT RAISE(ABORT, 'Governance events cannot be edited'); END""")


MIGRATION = Migration(84, "record_governance", apply)
