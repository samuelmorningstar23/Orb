"""Migration 83: local clinical documentation and its version histories.

These are records entered by staff, not orders, device data or decision support.
No existing patient, admission, surgery, observation or medication row is changed.
"""
from migrations import Migration

TABLES = (
    "ris_studies", "care_plans", "emergency_visits", "perioperative_records", "dialysis_sessions",
)


def apply(conn):
    for table in TABLES:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            id TEXT PRIMARY KEY,
            patient_id TEXT NOT NULL,
            admission_id TEXT,
            surgery_id TEXT,
            facility_id INTEGER NOT NULL,
            department TEXT,
            title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
            state TEXT NOT NULL,
            payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
            created_by TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            signed_by TEXT,
            signed_at TEXT,
            idempotency_key TEXT,
            request_fingerprint TEXT NOT NULL
        )""")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_patient ON {table}(patient_id, updated_at DESC)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_worklist ON {table}(facility_id, state, updated_at DESC)")
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_once ON {table}(created_by, idempotency_key) WHERE idempotency_key IS NOT NULL")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {table}_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL,
            patient_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('created','updated','signed','addendum')),
            prior_state TEXT,
            new_state TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            reason TEXT,
            snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
            UNIQUE(record_id, version)
        )""")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_events_record ON {table}_events(record_id, version)")
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_events_no_update
            BEFORE UPDATE ON {table}_events BEGIN SELECT RAISE(ABORT, 'Clinical history cannot be edited'); END""")


MIGRATION = Migration(83, "offline_clinical_records", apply)
