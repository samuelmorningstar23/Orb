"""Local complaint, mortuary custody and CSSD registers. No clinical writers.

Every record is local, facility-labelled and revisioned. Event rows are
append-only; explicit retention/erasure may DELETE them. No foreign keys are
introduced into the clinical schema. References are checked in the writer's
own transaction. Physical allocation is protected by partial unique indexes.
"""
from migrations import Migration


DDL = (
    """CREATE TABLE IF NOT EXISTS feedback_cases (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1,
        department TEXT, patient_id TEXT, kind TEXT NOT NULL CHECK(kind IN ('feedback','complaint')),
        channel TEXT NOT NULL CHECK(channel IN ('in_person','telephone','written','other')),
        subject TEXT NOT NULL, statement TEXT NOT NULL, reporter_name TEXT,
        contact_reference TEXT, rating INTEGER CHECK(rating BETWEEN 1 AND 5),
        owner_id TEXT, status TEXT NOT NULL DEFAULT 'recorded'
          CHECK(status IN ('recorded','investigating','resolved','closed','void')),
        resolution TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        client_key TEXT NOT NULL, request_hash TEXT NOT NULL,
        UNIQUE(facility_id, created_by, client_key))""",
    """CREATE TABLE IF NOT EXISTS feedback_events (
        id TEXT PRIMARY KEY, case_id TEXT NOT NULL, patient_id TEXT, action TEXT NOT NULL,
        from_status TEXT, to_status TEXT, note TEXT NOT NULL,
        actor_id TEXT NOT NULL, occurred_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS mortuary_slots (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1,
        department TEXT, code TEXT NOT NULL, name TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(facility_id, code))""",
    """CREATE TABLE IF NOT EXISTS mortuary_cases (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1,
        department TEXT, patient_id TEXT, body_tag TEXT NOT NULL,
        identity_label TEXT NOT NULL, authority_reference TEXT NOT NULL,
        received_at TEXT NOT NULL, slot_id TEXT, status TEXT NOT NULL DEFAULT 'stored'
          CHECK(status IN ('stored','held','released')),
        hold_reason TEXT, release_authority_reference TEXT, recipient_name TEXT,
        recipient_relationship TEXT, identity_check_reference TEXT, witness_id TEXT,
        released_at TEXT, released_by TEXT,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        client_key TEXT NOT NULL, request_hash TEXT NOT NULL,
        UNIQUE(facility_id, body_tag), UNIQUE(facility_id, created_by, client_key),
        CHECK((status = 'released' AND released_at IS NOT NULL AND slot_id IS NULL)
           OR (status <> 'released' AND released_at IS NULL AND slot_id IS NOT NULL)))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_mortuary_occupied_slot ON mortuary_cases(slot_id) WHERE status <> 'released'",
    """CREATE TABLE IF NOT EXISTS mortuary_events (
        id TEXT PRIMARY KEY, case_id TEXT NOT NULL, patient_id TEXT, action TEXT NOT NULL,
        from_slot_id TEXT, to_slot_id TEXT, note TEXT NOT NULL,
        actor_id TEXT NOT NULL, occurred_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS cssd_sets (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1,
        department TEXT, code TEXT NOT NULL, name TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(facility_id, code))""",
    """CREATE TABLE IF NOT EXISTS cssd_set_items (
        id TEXT PRIMARY KEY, set_id TEXT NOT NULL, name TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK(quantity > 0), UNIQUE(set_id,name))""",
    """CREATE TABLE IF NOT EXISTS cssd_packs (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1,
        department TEXT, set_id TEXT NOT NULL, tag TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'received'
          CHECK(status IN ('received','cleaned','packed','in_cycle','quarantined','released','issued','recall_pending','retired')),
        cycle_id TEXT, expires_at TEXT, created_by TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0), UNIQUE(facility_id,tag))""",
    """CREATE TABLE IF NOT EXISTS cssd_cycles (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1,
        department TEXT, reference TEXT NOT NULL, sterilizer_reference TEXT NOT NULL,
        sop_reference TEXT NOT NULL, method TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','released','failed','recalled')),
        started_at TEXT NOT NULL, completed_at TEXT,
        physical_result TEXT CHECK(physical_result IN ('pass','fail','pending')),
        chemical_result TEXT CHECK(chemical_result IN ('pass','fail','pending')),
        biological_result TEXT CHECK(biological_result IN ('pass','fail','pending','not_required')),
        biological_basis TEXT, measurements TEXT, completion_note TEXT,
        released_by TEXT, released_at TEXT, release_reference TEXT,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        client_key TEXT NOT NULL, request_hash TEXT NOT NULL,
        UNIQUE(facility_id,reference), UNIQUE(facility_id,created_by,client_key))""",
    """CREATE TABLE IF NOT EXISTS cssd_cycle_packs (
        cycle_id TEXT NOT NULL, pack_id TEXT NOT NULL, PRIMARY KEY(cycle_id,pack_id))""",
    """CREATE TABLE IF NOT EXISTS cssd_issues (
        id TEXT PRIMARY KEY, pack_id TEXT NOT NULL, surgery_id TEXT, patient_id TEXT,
        destination TEXT NOT NULL, reference TEXT NOT NULL,
        issued_by TEXT NOT NULL, issued_at TEXT NOT NULL,
        returned_by TEXT, returned_at TEXT, return_note TEXT,
        CHECK((surgery_id IS NULL AND patient_id IS NULL) OR (surgery_id IS NOT NULL AND patient_id IS NOT NULL)))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cssd_active_issue ON cssd_issues(pack_id) WHERE returned_at IS NULL",
    """CREATE TABLE IF NOT EXISTS cssd_events (
        id TEXT PRIMARY KEY, pack_id TEXT, cycle_id TEXT, patient_id TEXT,
        action TEXT NOT NULL, note TEXT NOT NULL, actor_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_feedback_register ON feedback_cases(facility_id,department,status,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_events ON feedback_events(case_id,occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_events_patient ON feedback_events(patient_id)",
    "CREATE INDEX IF NOT EXISTS idx_mortuary_register ON mortuary_cases(facility_id,department,status,received_at)",
    "CREATE INDEX IF NOT EXISTS idx_mortuary_events ON mortuary_events(case_id,occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_mortuary_events_patient ON mortuary_events(patient_id)",
    "CREATE INDEX IF NOT EXISTS idx_cssd_packs ON cssd_packs(facility_id,department,status)",
    "CREATE INDEX IF NOT EXISTS idx_cssd_cycles ON cssd_cycles(facility_id,department,status,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_cssd_issues_patient ON cssd_issues(patient_id)",
    "CREATE INDEX IF NOT EXISTS idx_cssd_events_patient ON cssd_events(patient_id)",
)


def apply(conn):
    for statement in DDL:
        conn.execute(statement)
    for table in ("feedback_events", "mortuary_events", "cssd_events"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable
            BEFORE UPDATE ON {table} BEGIN
            SELECT RAISE(ABORT, 'operational events are append-only'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_mortuary_released_immutable
        BEFORE UPDATE ON mortuary_cases WHEN OLD.status = 'released' BEGIN
        SELECT RAISE(ABORT, 'released custody record is immutable'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_feedback_statement_immutable
        BEFORE UPDATE OF patient_id,statement,subject,kind,channel,rating,reporter_name,contact_reference
        ON feedback_cases BEGIN
        SELECT RAISE(ABORT, 'original feedback is immutable; append a note'); END""")


MIGRATION = Migration(81, "local_operations", apply)
