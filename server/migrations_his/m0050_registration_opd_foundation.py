"""Migration 50: the registration and outpatient schema foundation (opd-1).

Additive only. Every table is new or created only when absent, every column is
ALTER TABLE ADD COLUMN with a constant default, and nothing here is a foreign
key, so no table becomes an FK parent. One statement per conn.execute, never
executescript (it would commit the runner's transaction).

WHAT IT DOES
1. opd_visits moves into the migration runner. Until now only
   services/frontdesk_service.ensure_frontdesk_schema created it, lazily, on the
   first front-desk call. The DDL below is a FROZEN copy of that lazy CREATE as
   of 3 October 2026, so a fresh appliance and one where the desk already ran
   end up with the same table, and the columns this migration adds can never be
   ALTERed onto a table that does not exist yet. The lazy path stays as an
   idempotent fallback and calls ensure_opd_visits_on below, so both paths
   produce one shape. patient_movements is NOT created here: migration 51 owns
   the movement ledger.
2. opd_visits gains the queue, outcome, link and control columns later OPD
   increments write (priority, room, disposition, cancel_reason_code,
   identity_check_json, scope_self_registered and the rest). Every one is NULL
   or a constant default, and nothing reads them while the registration and
   OPD features are off.
3. admissions.opd_visit_id and orders.opd_visit_id (indexed): the link from an
   admission or an order to the outpatient visit it came from.
4. document_number_series gains resets_yearly (1, so every existing series keeps
   its financial-year reset) and check_digit, and a UHID series row is seeded
   UNCONFIGURED: it never resets, carries a Verhoeff check digit, and
   registration refuses to issue an Orb-mode number until a site configures it.
5. Five new tables, empty until a site switches registration on:
   patient_demographics (one row per patient; patients itself gets NO new
   columns, which keeps the migration 52 rebuild's column list frozen),
   patient_identity_changes (append-only: UPDATE and DELETE are refused by
   triggers), patient_duplicate_candidates, patient_match_keys and
   opd_display_boards.

With the registration and OPD features off nothing writes any of this; the
visible effect on the Ward Record and the demo databases is the wider schema.
The retention review already lists the new PHI tables (consent_service.
_RETENTION_SOURCES, registered in wave 1).
"""
from migrations import Migration

# ── 1. opd_visits, frozen as services/frontdesk_service wrote it on 3 October 2026 ──
# The columns, types, defaults and CHECKs are that CREATE's exactly; only its SQL
# comments are left out. What they said still holds:
#   department is the CLINIC the visit belongs to and the queue's scope key (an
#     outpatient has no admission, so permissions.PATIENT_DEPARTMENT_SQL cannot
#     derive one). It is compared against permissions.department_scope(user),
#     never against a department the request names.
#   doctor_name is a display snapshot taken at registration, never a scope
#     source; doctor_id is the identity.
#   visit_date is YYYY-MM-DD, hospital-local.
#   consultation_charge_id is encounter_charges.id once the charge is raised.
OPD_VISITS_DDL = """CREATE TABLE IF NOT EXISTS opd_visits (
        id TEXT PRIMARY KEY,
        patient_id TEXT NOT NULL,
        department TEXT NOT NULL,
        doctor_id TEXT,
        doctor_name TEXT,
        visit_date TEXT NOT NULL,
        token_number INTEGER NOT NULL,
        visit_type TEXT NOT NULL DEFAULT 'new' CHECK (visit_type IN (
            'new','follow_up','review','procedure')),
        chief_complaint TEXT,
        status TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN (
            'waiting','in_consultation','completed','cancelled')),
        appointment_id TEXT,
        registered_by TEXT,
        registered_at TIMESTAMP NOT NULL,
        called_at TIMESTAMP,
        completed_at TIMESTAMP,
        cancelled_at TIMESTAMP,
        cancel_reason TEXT,
        consultation_charge_id INTEGER,
        notes TEXT,
        idempotency_key TEXT
    )"""

#: The opd_visits columns as the lazy CREATE above defines them, in order. The
#: front desk's API projects visits onto exactly these, so the columns this
#: migration adds never change what an existing screen receives.
OPD_VISITS_BASE_COLUMNS: tuple = (
    "id", "patient_id", "department", "doctor_id", "doctor_name", "visit_date",
    "token_number", "visit_type", "chief_complaint", "status", "appointment_id",
    "registered_by", "registered_at", "called_at", "completed_at", "cancelled_at",
    "cancel_reason", "consultation_charge_id", "notes", "idempotency_key",
)

OPD_VISITS_INDEXES: tuple = (
    "CREATE INDEX IF NOT EXISTS idx_opd_queue ON opd_visits("
    "visit_date, department, status, token_number)",
    "CREATE INDEX IF NOT EXISTS idx_opd_doctor_day ON opd_visits("
    "doctor_id, visit_date, token_number)",
    "CREATE INDEX IF NOT EXISTS idx_opd_patient ON opd_visits(patient_id, visit_date DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_opd_idempotency ON opd_visits(idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    # One token number per doctor per day, over live rows only: a cancelled
    # visit must not hold its token hostage. KNOWN GAP, stated rather than
    # papered over: SQLite treats NULLs as distinct in a unique index, so this
    # does not constrain the unassigned-doctor queue (doctor_id IS NULL). What
    # actually serialises token allocation is db_session(immediate=True) in
    # frontdesk_service.register_opd_visit; the index is the second line of
    # defence, and it covers every named doctor.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_opd_token_unique ON opd_visits("
    "visit_date, department, doctor_id, token_number) WHERE status != 'cancelled'",
)

# ── 2. The columns opd-1 adds to opd_visits (name, declaration) ──────────────
# Vocabularies (priority is a site list, disposition, arrival_source,
# cancel_reason_code) are validated by the services that write them, so a site
# list can grow without a table rebuild.
OPD_VISITS_ADDED_COLUMNS: tuple = (
    # queue
    ("priority", "TEXT DEFAULT 'normal'"),
    ("priority_reason", "TEXT"),
    ("room", "TEXT"),
    ("arrival_source", "TEXT DEFAULT 'walk_in'"),
    ("checked_in_at", "TIMESTAMP"),
    ("call_count", "INTEGER DEFAULT 0"),
    ("skipped_at", "TIMESTAMP"),
    # outcome: disposition is home, follow_up, referred_internal,
    # referred_external or admitted; a 'left without being seen' is a cancel
    # with cancel_reason_code 'left_without_being_seen' and no charge.
    ("disposition", "TEXT"),
    ("disposition_at", "TIMESTAMP"),
    ("admission_id", "TEXT"),
    ("follow_up_of_visit_id", "TEXT"),
    ("cancel_reason_code", "TEXT"),
    # links
    ("session_id", "TEXT"),
    ("payer_id", "INTEGER"),
    ("tariff_plan_id", "INTEGER"),
    ("registration_charge_id", "INTEGER"),
    # control: what identity check the desk ran when it picked an existing
    # record, and whether the person who registered the visit is a clinician
    # whose own registration opens the outpatient record to them (OPD-N2).
    ("identity_check_json", "TEXT"),
    ("scope_self_registered", "INTEGER DEFAULT 0"),
)

# ── 5. The new tables ────────────────────────────────────────────────────────
PATIENT_DEMOGRAPHICS_DDL = """CREATE TABLE IF NOT EXISTS patient_demographics (
        patient_id TEXT PRIMARY KEY,
        -- provenance
        registered_at TIMESTAMP,
        registered_by TEXT,
        registration_source TEXT CHECK (registration_source IS NULL OR registration_source IN (
            'desk','admission','scribe','import','er')),
        uhid_source TEXT CHECK (uhid_source IS NULL OR uhid_source IN ('hospital','orb')),
        -- The day a stated age was recorded. A stated age is never turned into
        -- a date of birth: an estimated DOB would become the discharge-link
        -- second factor.
        age_recorded_on DATE,
        guardian_relation TEXT,
        guardian_name TEXT,
        marital_status TEXT,
        preferred_language TEXT,
        address_line TEXT,
        city TEXT,
        district TEXT,
        state_code TEXT,
        pin_code TEXT,
        alt_phone TEXT,
        email TEXT,
        -- An identity document is stored as its type and last four characters
        -- only, never a full number (OPD-N10); the database refuses more.
        id_document_type TEXT,
        id_document_last4 TEXT CHECK (id_document_last4 IS NULL OR length(id_document_last4) <= 4),
        occupation TEXT,
        row_version INTEGER NOT NULL DEFAULT 1,
        updated_at TIMESTAMP,
        updated_by TEXT
    )"""

PATIENT_IDENTITY_CHANGES_DDL = """CREATE TABLE IF NOT EXISTS patient_identity_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        field TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        changed_by TEXT NOT NULL,
        changed_at TIMESTAMP NOT NULL,
        correlation_id TEXT
    )"""

PATIENT_DUPLICATE_CANDIDATES_DDL = """CREATE TABLE IF NOT EXISTS patient_duplicate_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        -- The pair, stored in one canonical order so (A, B) and (B, A) are the
        -- same row and the unique index below holds.
        patient_id TEXT NOT NULL,
        candidate_patient_id TEXT NOT NULL,
        score INTEGER NOT NULL,
        reasons_json TEXT,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
            'open','not_duplicate','confirmed_duplicate','merged')),
        source TEXT NOT NULL CHECK (source IN ('registration','admission','sweep','import')),
        detected_at TIMESTAMP NOT NULL,
        detected_by TEXT,
        -- Who registered past the warning, with their reason. The resolver must
        -- not be this person.
        overridden_by TEXT,
        override_reason TEXT,
        resolved_by TEXT,
        resolved_at TIMESTAMP,
        resolution_reason TEXT,
        CHECK (patient_id < candidate_patient_id)
    )"""

PATIENT_MATCH_KEYS_DDL = """CREATE TABLE IF NOT EXISTS patient_match_keys (
        patient_id TEXT PRIMARY KEY,
        name_key TEXT,
        phone_key TEXT,
        dob TEXT,
        sex TEXT,
        -- The source values the keys were computed from, so a sync can find
        -- every row whose patient changed since, whoever changed it.
        src_name TEXT,
        src_phone TEXT,
        src_dob TEXT,
        src_sex TEXT,
        normalizer_version INTEGER NOT NULL DEFAULT 1,
        computed_at TIMESTAMP NOT NULL
    )"""

OPD_DISPLAY_BOARDS_DDL = """CREATE TABLE IF NOT EXISTS opd_display_boards (
        id TEXT PRIMARY KEY,
        department TEXT NOT NULL,
        label TEXT NOT NULL,
        -- The board's device key is stored only as a hash; the key itself is
        -- shown once, when the board is created.
        key_hash TEXT NOT NULL,
        created_by TEXT,
        created_at TIMESTAMP NOT NULL,
        revoked_by TEXT,
        revoked_at TIMESTAMP
    )"""

NEW_TABLE_DDL: tuple = (
    PATIENT_DEMOGRAPHICS_DDL,
    PATIENT_IDENTITY_CHANGES_DDL,
    PATIENT_DUPLICATE_CANDIDATES_DDL,
    PATIENT_MATCH_KEYS_DDL,
    OPD_DISPLAY_BOARDS_DDL,
)

NEW_TABLE_INDEXES: tuple = (
    "CREATE INDEX IF NOT EXISTS idx_identity_changes_patient ON "
    "patient_identity_changes(patient_id, changed_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_duplicate_candidates_pair ON "
    "patient_duplicate_candidates(patient_id, candidate_patient_id)",
    "CREATE INDEX IF NOT EXISTS idx_duplicate_candidates_status ON "
    "patient_duplicate_candidates(status, detected_at)",
    "CREATE INDEX IF NOT EXISTS idx_duplicate_candidates_candidate ON "
    "patient_duplicate_candidates(candidate_patient_id)",
    "CREATE INDEX IF NOT EXISTS idx_match_keys_name ON patient_match_keys(name_key)",
    "CREATE INDEX IF NOT EXISTS idx_match_keys_phone ON patient_match_keys(phone_key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_display_boards_key ON opd_display_boards(key_hash)",
    "CREATE INDEX IF NOT EXISTS idx_display_boards_department ON "
    "opd_display_boards(department, revoked_at)",
)

#: The identity change log is append-only, enforced by the database. A wrong
#: correction is undone by a NEW change row, never by editing or deleting one.
IDENTITY_CHANGES_TRIGGERS: tuple = (
    """CREATE TRIGGER IF NOT EXISTS trg_patient_identity_changes_no_update
       BEFORE UPDATE ON patient_identity_changes
       BEGIN
           SELECT RAISE(ABORT, 'patient_identity_changes is append-only: record a new change instead');
       END""",
    """CREATE TRIGGER IF NOT EXISTS trg_patient_identity_changes_no_delete
       BEFORE DELETE ON patient_identity_changes
       BEGIN
           SELECT RAISE(ABORT, 'patient_identity_changes is append-only: rows are never deleted');
       END""",
)


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def ensure_opd_visits_on(conn) -> None:
    """opd_visits in its migration 50 shape: created from the frozen DDL when
    absent, every added column ALTERed on when missing, every index ensured.
    Idempotent. Shared with frontdesk_service.ensure_frontdesk_schema so the
    lazy fallback and the runner cannot drift apart."""
    conn.execute(OPD_VISITS_DDL)
    present = _columns(conn, "opd_visits")
    for name, declaration in OPD_VISITS_ADDED_COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE opd_visits ADD COLUMN {name} {declaration}")
    for statement in OPD_VISITS_INDEXES:
        conn.execute(statement)


def apply(conn) -> None:
    # 1 and 2
    ensure_opd_visits_on(conn)

    # 3. Links from an admission or an order back to its outpatient visit.
    if "opd_visit_id" not in _columns(conn, "admissions"):
        conn.execute("ALTER TABLE admissions ADD COLUMN opd_visit_id TEXT")
    if "opd_visit_id" not in _columns(conn, "orders"):
        conn.execute("ALTER TABLE orders ADD COLUMN opd_visit_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_opd_visit ON orders(opd_visit_id) "
        "WHERE opd_visit_id IS NOT NULL"
    )

    # 4. Numbering. resets_yearly defaults to 1 so every existing series keeps
    # resetting on the financial year exactly as billing_masters does today.
    series = _columns(conn, "document_number_series")
    if "resets_yearly" not in series:
        conn.execute(
            "ALTER TABLE document_number_series ADD COLUMN resets_yearly INTEGER NOT NULL DEFAULT 1"
        )
    if "check_digit" not in series:
        conn.execute("ALTER TABLE document_number_series ADD COLUMN check_digit TEXT")
    # A hospital number is for life: it never resets and never embeds a year.
    # is_configured 0 until a site sets its prefix; Orb-mode registration
    # refuses to issue a number from an unconfigured series.
    conn.execute(
        "INSERT OR IGNORE INTO document_number_series "
        "(series_code, document_type, prefix, next_number, is_configured, "
        "resets_yearly, check_digit) VALUES ('UHID', 'uhid', '', 1, 0, 0, 'verhoeff')"
    )

    # 5. The new tables.
    for statement in NEW_TABLE_DDL:
        conn.execute(statement)
    for statement in NEW_TABLE_INDEXES:
        conn.execute(statement)
    for statement in IDENTITY_CHANGES_TRIGGERS:
        conn.execute(statement)


MIGRATION = Migration(50, "registration_opd_foundation", apply)
