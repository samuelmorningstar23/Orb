"""Manual local infection surveillance; no inferred cases or treatment decisions."""
from migrations import Migration

DDL = (
    """CREATE TABLE IF NOT EXISTS infection_definitions (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1, department TEXT,
        code TEXT NOT NULL, name TEXT NOT NULL, case_definition TEXT NOT NULL,
        counting_policy TEXT NOT NULL, denominator_unit TEXT NOT NULL CHECK(denominator_unit IN ('patient_days','device_days')),
        device_label TEXT, evidence_reference TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(facility_id,code))""",
    """CREATE TABLE IF NOT EXISTS infection_windows (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1, department TEXT,
        definition_id TEXT NOT NULL, label TEXT NOT NULL, population_description TEXT NOT NULL,
        start_date TEXT NOT NULL, end_date TEXT NOT NULL, source_reference TEXT NOT NULL,
        rate_multiplier INTEGER NOT NULL CHECK(rate_multiplier > 0),
        minimum_cell INTEGER NOT NULL CHECK(minimum_cell > 0),
        isolate_policy TEXT NOT NULL CHECK(isolate_policy IN ('all_isolates','first_per_patient')),
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','finalized')),
        finalized_at TEXT, finalized_by TEXT, finalization_note TEXT, finalized_counts_json TEXT,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        CHECK(start_date <= end_date))""",
    """CREATE TABLE IF NOT EXISTS infection_cases (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1, department TEXT,
        window_id TEXT NOT NULL, patient_id TEXT NOT NULL, episode_reference TEXT NOT NULL,
        event_date TEXT NOT NULL, evidence_reference TEXT NOT NULL, statement TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'under_review' CHECK(status IN ('under_review','included','excluded')),
        classification_reason TEXT, reviewer_reference TEXT,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        UNIQUE(window_id,patient_id,episode_reference))""",
    """CREATE TABLE IF NOT EXISTS infection_exposure_days (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1, department TEXT,
        window_id TEXT NOT NULL, patient_id TEXT NOT NULL, observed_date TEXT NOT NULL,
        device_reference TEXT NOT NULL DEFAULT '', day_count INTEGER NOT NULL DEFAULT 1 CHECK(day_count = 1),
        evidence_reference TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','void')),
        void_reason TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_infection_exposure_unique ON infection_exposure_days(window_id,patient_id,observed_date,device_reference) WHERE status='active'",
    """CREATE TABLE IF NOT EXISTS infection_susceptibility (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1, department TEXT,
        window_id TEXT NOT NULL, patient_id TEXT NOT NULL, isolate_reference TEXT NOT NULL,
        specimen_date TEXT NOT NULL, organism_code TEXT NOT NULL, specimen_code TEXT NOT NULL,
        antibiotic_code TEXT NOT NULL, result TEXT NOT NULL CHECK(result IN ('S','I','R')),
        standard_reference TEXT NOT NULL, method TEXT NOT NULL, evidence_reference TEXT NOT NULL,
        validator_reference TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','void')),
        void_reason TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_infection_susceptibility_unique ON infection_susceptibility(window_id,isolate_reference,antibiotic_code) WHERE status='active'",
    """CREATE TABLE IF NOT EXISTS infection_isolation_records (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1, department TEXT,
        patient_id TEXT NOT NULL, case_id TEXT, precautions TEXT NOT NULL,
        started_at TEXT NOT NULL, start_reason TEXT NOT NULL, authority_reference TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','ended')),
        ended_at TEXT, end_reason TEXT, end_authority_reference TEXT,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
        CHECK((status='active' AND ended_at IS NULL) OR (status='ended' AND ended_at IS NOT NULL)))""",
    """CREATE TABLE IF NOT EXISTS infection_events (
        id TEXT PRIMARY KEY, record_kind TEXT NOT NULL, record_id TEXT NOT NULL,
        window_id TEXT, patient_id TEXT, action TEXT NOT NULL, note TEXT NOT NULL,
        actor_id TEXT NOT NULL, occurred_at TEXT NOT NULL, version INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS infection_requests (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL DEFAULT 1, actor_id TEXT NOT NULL,
        client_key TEXT NOT NULL, request_hash TEXT NOT NULL, record_kind TEXT NOT NULL,
        record_id TEXT NOT NULL, patient_id TEXT, created_at TEXT NOT NULL,
        UNIQUE(facility_id,actor_id,client_key))""",
    "CREATE INDEX IF NOT EXISTS idx_infection_windows_scope ON infection_windows(facility_id,department,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_infection_cases_patient ON infection_cases(patient_id,event_date)",
    "CREATE INDEX IF NOT EXISTS idx_infection_cases_window ON infection_cases(window_id,status,event_date)",
    "CREATE INDEX IF NOT EXISTS idx_infection_exposures_patient ON infection_exposure_days(patient_id,observed_date)",
    "CREATE INDEX IF NOT EXISTS idx_infection_susceptibility_patient ON infection_susceptibility(patient_id,specimen_date)",
    "CREATE INDEX IF NOT EXISTS idx_infection_isolation_patient ON infection_isolation_records(patient_id,started_at)",
    "CREATE INDEX IF NOT EXISTS idx_infection_events_record ON infection_events(record_kind,record_id,occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_infection_events_patient ON infection_events(patient_id,occurred_at)",
)


def apply(conn):
    for statement in DDL:
        conn.execute(statement)
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_infection_events_immutable
        BEFORE UPDATE ON infection_events BEGIN SELECT RAISE(ABORT,'infection events are append-only'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_infection_definition_immutable
        BEFORE UPDATE ON infection_definitions BEGIN SELECT RAISE(ABORT,'create a new definition for a changed surveillance policy'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_infection_window_policy_immutable
        BEFORE UPDATE OF definition_id,population_description,start_date,end_date,rate_multiplier,minimum_cell,isolate_policy
        ON infection_windows BEGIN SELECT RAISE(ABORT,'observation policy is immutable; create a new window'); END""")


MIGRATION = Migration(85, 'infection_surveillance', apply)
