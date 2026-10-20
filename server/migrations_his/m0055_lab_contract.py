"""Migration 55: the laboratory information system's tables (lis-3).

Creates every lab_ table the LIS design names (docs/his_design/lis.json,
section 4, M-A): the test master and its versioned reference ranges and
critical limits, specimen types, sections, rejection reasons, the accession
counter, specimens and their events, order items, results and their events,
critical-result notifications and their events, the exceptions list and the
module's own on and off events.

WHAT THIS MIGRATION DOES NOT DO
  * It writes no rows. The starter catalogue is loaded by
    services/lab_master_service.py at startup, and only while ORB_LAB is on,
    so a site that never turns the laboratory on keeps every lab_ table empty
    and its terminology table unchanged. No reference range and no critical
    limit ships at all (rulings LIS-R1 and LIS-R2): a pathologist approves
    each one.
  * It touches no existing table. orders keeps its status and order_type
    checks (it is a foreign-key parent, so widening them would mean a rebuild);
    the laboratory's own state lives here, keyed on orders.id.

THE RULES EVERY TABLE HERE FOLLOWS
  * No FOREIGN KEY clause. A rebuild of a parent table elsewhere can never
    break these tables, and these tables can never block one.
  * No REAL column. A value is an INTEGER in thousandths (value_milli,
    low_milli, high_milli) plus the test's reporting precision
    (decimal_places, 0 to 3), so a flag is decided on the number the report
    prints, never on a binary fraction.
  * Instants are TEXT in the one canonical UTC spelling written by
    services/clinical_time.utc_iso_now() and to_utc_iso(). No column defaults
    to CURRENT_TIMESTAMP, whose spelling is not the canonical one, so every
    writer must say when.
  * The event tables (specimen, result, critical and module events) are
    append-only: an UPDATE is refused by a trigger. A DELETE is not refused,
    so a retention order signed by a person, and a demo reseed, still work.

One statement per conn.execute, never executescript: executescript commits
the runner's transaction (see migrations.run_pending_migrations). Every
statement is IF NOT EXISTS, so running it twice, or on a database where an
earlier attempt got part of the way, changes nothing the second time. Each
index is created only when its table has every column it names, so an
unusual table of the same name cannot stop boot here.
"""

from migrations import Migration

_TABLES = (
    # ── The test master ──────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS lab_sections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    site_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0
)""",
    """CREATE TABLE IF NOT EXISTS lab_specimen_types (
    code TEXT PRIMARY KEY,
    display TEXT NOT NULL,
    container TEXT,
    container_colour_name TEXT,
    min_volume_ul INTEGER CHECK (min_volume_ul IS NULL OR min_volume_ul > 0),
    stability_minutes INTEGER CHECK (stability_minutes IS NULL OR stability_minutes > 0),
    handling_note TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
)""",
    """CREATE TABLE IF NOT EXISTS lab_tests (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    code_system TEXT NOT NULL CHECK (code_system IN ('LOINC', 'LOCAL')),
    name TEXT NOT NULL,
    short_name TEXT NOT NULL CHECK (length(short_name) BETWEEN 1 AND 16),
    kind TEXT NOT NULL DEFAULT 'analyte' CHECK (kind IN ('analyte', 'panel')),
    performed_at TEXT NOT NULL DEFAULT 'lab' CHECK (performed_at IN ('lab', 'ward_poct')),
    section_id TEXT,
    specimen_type_code TEXT,
    result_type TEXT NOT NULL DEFAULT 'numeric' CHECK (result_type IN ('numeric', 'text', 'coded')),
    unit TEXT,
    decimal_places INTEGER NOT NULL DEFAULT 0 CHECK (decimal_places BETWEEN 0 AND 3),
    coded_values_json TEXT,
    orderable INTEGER NOT NULL DEFAULT 1 CHECK (orderable IN (0, 1)),
    tat_routine_min INTEGER CHECK (tat_routine_min IS NULL OR tat_routine_min > 0),
    tat_urgent_min INTEGER CHECK (tat_urgent_min IS NULL OR tat_urgent_min > 0),
    tat_stat_min INTEGER CHECK (tat_stat_min IS NULL OR tat_stat_min > 0),
    delta_kind TEXT CHECK (delta_kind IS NULL OR delta_kind IN ('absolute', 'percent')),
    delta_threshold_milli INTEGER CHECK (delta_threshold_milli IS NULL OR delta_threshold_milli > 0),
    delta_window_hours INTEGER CHECK (delta_window_hours IS NULL OR delta_window_hours > 0),
    is_send_out INTEGER NOT NULL DEFAULT 0 CHECK (is_send_out IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_by TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (code_system, code)
)""",
    """CREATE TABLE IF NOT EXISTS lab_panel_components (
    panel_test_id TEXT NOT NULL,
    component_test_id TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (panel_test_id, component_test_id),
    CHECK (panel_test_id <> component_test_id)
)""",
    # A range is never edited in value: a change closes effective_to on the
    # old row and inserts a new one, so every released result can name the
    # exact range it was flagged against. Age is in days at collection, the
    # band is [age_min_days, age_max_days), and NULL age_max_days means no
    # upper age.
    """CREATE TABLE IF NOT EXISTS lab_reference_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id TEXT NOT NULL,
    sex TEXT NOT NULL DEFAULT 'any' CHECK (sex IN ('any', 'male', 'female')),
    age_min_days INTEGER NOT NULL DEFAULT 0 CHECK (age_min_days >= 0),
    age_max_days INTEGER CHECK (age_max_days IS NULL OR age_max_days > age_min_days),
    low_milli INTEGER,
    high_milli INTEGER,
    display_text TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    method_note TEXT NOT NULL,
    CHECK (low_milli IS NOT NULL OR high_milli IS NOT NULL),
    CHECK (low_milli IS NULL OR high_milli IS NULL OR low_milli <= high_milli)
)""",
    # The same shape for critical limits, with the clinicians the limit was
    # agreed with (ruling LIS-R1: the pathologist names them).
    """CREATE TABLE IF NOT EXISTS lab_critical_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id TEXT NOT NULL,
    sex TEXT NOT NULL DEFAULT 'any' CHECK (sex IN ('any', 'male', 'female')),
    age_min_days INTEGER NOT NULL DEFAULT 0 CHECK (age_min_days >= 0),
    age_max_days INTEGER CHECK (age_max_days IS NULL OR age_max_days > age_min_days),
    critical_low_milli INTEGER,
    critical_high_milli INTEGER,
    display_text TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    agreed_with TEXT NOT NULL,
    method_note TEXT NOT NULL,
    CHECK (critical_low_milli IS NOT NULL OR critical_high_milli IS NOT NULL),
    CHECK (critical_low_milli IS NULL OR critical_high_milli IS NULL
           OR critical_low_milli < critical_high_milli)
)""",
    """CREATE TABLE IF NOT EXISTS lab_rejection_reasons (
    code TEXT PRIMARY KEY,
    display TEXT NOT NULL,
    incident_prompt INTEGER NOT NULL DEFAULT 0 CHECK (incident_prompt IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
)""",
    # One row per hospital-local day. The sequence never wraps: at 99999 the
    # allocator refuses (ruling LIS-R13).
    """CREATE TABLE IF NOT EXISTS lab_accession_counters (
    hospital_day TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_seq BETWEEN 0 AND 99999)
)""",
    # ── Specimens ────────────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS lab_specimens (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    specimen_type_code TEXT NOT NULL,
    container TEXT,
    priority TEXT NOT NULL DEFAULT 'routine' CHECK (priority IN ('stat', 'urgent', 'routine')),
    status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN (
        'planned', 'collected', 'received', 'rejected', 'in_process', 'completed', 'cancelled')),
    planned_at TEXT,
    label_printed_at TEXT,
    label_print_count INTEGER NOT NULL DEFAULT 0 CHECK (label_print_count >= 0),
    collected_at TEXT,
    collected_by TEXT,
    collection_location TEXT,
    identity_check_json TEXT,
    collection_backdate_reason TEXT,
    received_at TEXT,
    received_by TEXT,
    receipt_note TEXT,
    rejected_at TEXT,
    rejected_by TEXT,
    rejection_code TEXT,
    rejection_note TEXT,
    recollect_of TEXT,
    site_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
)""",
    """CREATE TABLE IF NOT EXISTS lab_specimen_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    event_type TEXT NOT NULL,
    actor_user_id TEXT,
    actor_role TEXT,
    occurred_at TEXT NOT NULL,
    detail TEXT,
    metadata_json TEXT,
    UNIQUE (specimen_id, seq)
)""",
    # ── Order items and results ──────────────────────────────────────────────
    # 'rejected' means the specimen was rejected and the item waits for a
    # recollect or cancel decision.
    """CREATE TABLE IF NOT EXISTS lab_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    test_id TEXT NOT NULL,
    panel_test_id TEXT,
    specimen_id TEXT,
    section_id TEXT,
    priority TEXT NOT NULL DEFAULT 'routine' CHECK (priority IN ('stat', 'urgent', 'routine')),
    status TEXT NOT NULL DEFAULT 'awaiting_collection' CHECK (status IN (
        'awaiting_collection', 'collected', 'received', 'resulted', 'tech_validated',
        'released', 'cancelled', 'rejected')),
    cancelled_reason TEXT,
    cancelled_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    UNIQUE (order_id, test_id)
)""",
    # value_milli is NULL for a comparator result ('<0.01'), whose text is in
    # value_text; delta checks skip it. The flag comes only from an approved
    # range (H, L) or an approved critical limit (HH, LL); blank means within
    # the range, or no range configured (reference_range_text says which).
    """CREATE TABLE IF NOT EXISTS lab_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_item_id INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    specimen_id TEXT,
    patient_id TEXT NOT NULL,
    test_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    value_milli INTEGER,
    comparator TEXT CHECK (comparator IS NULL OR comparator IN ('<', '>', '<=', '>=')),
    value_text TEXT,
    unit TEXT,
    decimal_places INTEGER CHECK (decimal_places IS NULL OR decimal_places BETWEEN 0 AND 3),
    comment TEXT,
    reference_range_id INTEGER,
    reference_range_text TEXT,
    abnormal_flag TEXT NOT NULL DEFAULT '' CHECK (abnormal_flag IN ('', 'L', 'H', 'LL', 'HH', 'A')),
    critical_limit_id INTEGER,
    is_critical INTEGER NOT NULL DEFAULT 0 CHECK (is_critical IN (0, 1)),
    delta_flag INTEGER NOT NULL DEFAULT 0 CHECK (delta_flag IN (0, 1)),
    delta_json TEXT,
    delta_ack_by TEXT,
    delta_ack_at TEXT,
    delta_ack_reason TEXT,
    entry_method TEXT NOT NULL DEFAULT 'manual' CHECK (entry_method IN ('manual', 'analyser')),
    instrument_id TEXT,
    instrument_flag TEXT,
    ingestion_event_id INTEGER,
    entered_by TEXT,
    entered_at TEXT NOT NULL,
    tech_validated_by TEXT,
    tech_validated_at TEXT,
    released_by TEXT,
    released_at TEXT,
    status TEXT NOT NULL DEFAULT 'entered' CHECK (status IN (
        'entered', 'tech_validated', 'released', 'superseded', 'withdrawn')),
    amend_reason TEXT,
    superseded_by_result_id INTEGER,
    observation_id INTEGER,
    source TEXT NOT NULL DEFAULT 'lis_manual' CHECK (source IN ('lis_manual', 'lis_analyser', 'simulated'))
)""",
    """CREATE TABLE IF NOT EXISTS lab_result_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL,
    patient_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT,
    occurred_at TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT
)""",
    # ── Critical results ─────────────────────────────────────────────────────
    # raised_in_department is a display snapshot, never a scope: the ward card
    # follows the patient's current department at read time. A notification is
    # never closed by discharge or transfer (ruling LIS-R19), and a Relay post
    # never closes it (ruling LIS-R3).
    """CREATE TABLE IF NOT EXISTS lab_critical_notifications (
    id TEXT PRIMARY KEY,
    source_lane TEXT NOT NULL CHECK (source_lane IN ('lis', 'interfaced')),
    result_id INTEGER,
    observation_id INTEGER,
    patient_id TEXT NOT NULL,
    test_code TEXT,
    test_name TEXT NOT NULL,
    value_display TEXT NOT NULL,
    preliminary INTEGER NOT NULL DEFAULT 0 CHECK (preliminary IN (0, 1)),
    raised_at TEXT NOT NULL,
    raised_by TEXT,
    raised_in_department TEXT,
    current_tier INTEGER NOT NULL DEFAULT 1 CHECK (current_tier >= 1),
    tier_due_at TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (delivery_status IN (
        'pending', 'posted', 'no_case_room', 'failed')),
    relay_message_id INTEGER,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    recipient_name TEXT,
    recipient_role TEXT,
    method TEXT CHECK (method IS NULL OR method IN ('phone', 'in_person', 'in_app')),
    read_back INTEGER CHECK (read_back IS NULL OR read_back IN (0, 1)),
    closed_at TEXT,
    closed_by TEXT,
    close_reason TEXT,
    policy_json TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'closed_unreachable')),
    CHECK ((source_lane = 'lis' AND result_id IS NOT NULL)
           OR (source_lane = 'interfaced' AND observation_id IS NOT NULL))
)""",
    """CREATE TABLE IF NOT EXISTS lab_critical_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'raised', 'posted', 'delivery_failed', 'callback_logged', 'acknowledged',
        'escalated', 'closed')),
    tier INTEGER,
    actor_user_id TEXT,
    occurred_at TEXT NOT NULL,
    detail TEXT,
    metadata_json TEXT
)""",
    # ── The laboratory's own lists ───────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS lab_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN (
        'unmapped_code', 'order_state_conflict', 'intake_failed', 'rejected_no_decision')),
    order_id TEXT,
    specimen_id TEXT,
    detail TEXT,
    created_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution TEXT
)""",
    # The cutover: services/lab_module.py writes 'enabled' at the first boot
    # with ORB_LAB on and 'disabled' at the first boot with it off again.
    # Intake only ever considers orders activated after the latest 'enabled'.
    """CREATE TABLE IF NOT EXISTS lab_module_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL CHECK (event IN ('enabled', 'disabled')),
    occurred_at TEXT NOT NULL,
    detail TEXT
)""",
)

TABLE_NAMES = (
    "lab_sections",
    "lab_specimen_types",
    "lab_tests",
    "lab_panel_components",
    "lab_reference_ranges",
    "lab_critical_limits",
    "lab_rejection_reasons",
    "lab_accession_counters",
    "lab_specimens",
    "lab_specimen_events",
    "lab_order_items",
    "lab_results",
    "lab_result_events",
    "lab_critical_notifications",
    "lab_critical_events",
    "lab_exceptions",
    "lab_module_events",
)

# (index name, table, columns, WHERE clause or "", unique)
_INDEXES = (
    ("idx_lab_tests_section", "lab_tests", ("section_id",), "", False),
    (
        "idx_lab_panel_components_component",
        "lab_panel_components",
        ("component_test_id",),
        "",
        False,
    ),
    (
        "idx_lab_reference_ranges_test",
        "lab_reference_ranges",
        ("test_id", "effective_to"),
        "",
        False,
    ),
    ("idx_lab_critical_limits_test", "lab_critical_limits", ("test_id", "effective_to"), "", False),
    ("idx_lab_specimens_status", "lab_specimens", ("status", "priority", "received_at"), "", False),
    ("idx_lab_specimens_patient", "lab_specimens", ("patient_id", "created_at"), "", False),
    (
        "idx_lab_specimen_events_patient",
        "lab_specimen_events",
        ("patient_id", "occurred_at"),
        "",
        False,
    ),
    ("idx_lab_order_items_order", "lab_order_items", ("order_id",), "", False),
    ("idx_lab_order_items_patient", "lab_order_items", ("patient_id", "created_at"), "", False),
    ("idx_lab_order_items_specimen", "lab_order_items", ("specimen_id",), "", False),
    ("idx_lab_order_items_status", "lab_order_items", ("status",), "", False),
    ("idx_lab_results_patient", "lab_results", ("patient_id", "entered_at"), "", False),
    ("idx_lab_results_specimen", "lab_results", ("specimen_id",), "", False),
    ("idx_lab_results_status", "lab_results", ("status",), "", False),
    # One live result per order item: a correction supersedes, never doubles.
    (
        "uq_lab_results_live_item",
        "lab_results",
        ("order_item_id",),
        "status NOT IN ('superseded', 'withdrawn')",
        True,
    ),
    ("idx_lab_result_events_result", "lab_result_events", ("result_id",), "", False),
    (
        "idx_lab_result_events_patient",
        "lab_result_events",
        ("patient_id", "occurred_at"),
        "",
        False,
    ),
    (
        "idx_lab_critical_notifications_patient",
        "lab_critical_notifications",
        ("patient_id", "raised_at"),
        "",
        False,
    ),
    (
        "idx_lab_critical_notifications_due",
        "lab_critical_notifications",
        ("status", "tier_due_at"),
        "",
        False,
    ),
    # One open notification per result and per interfaced observation.
    (
        "uq_lab_critical_open_result",
        "lab_critical_notifications",
        ("result_id",),
        "status = 'open' AND result_id IS NOT NULL",
        True,
    ),
    (
        "uq_lab_critical_open_observation",
        "lab_critical_notifications",
        ("observation_id",),
        "status = 'open' AND observation_id IS NOT NULL",
        True,
    ),
    (
        "idx_lab_critical_events_notification",
        "lab_critical_events",
        ("notification_id",),
        "",
        False,
    ),
    (
        "idx_lab_critical_events_patient",
        "lab_critical_events",
        ("patient_id", "occurred_at"),
        "",
        False,
    ),
    ("idx_lab_exceptions_open", "lab_exceptions", ("created_at",), "resolved_at IS NULL", False),
)

# The append-only event tables: history is added to, never rewritten.
_APPEND_ONLY = (
    "lab_specimen_events",
    "lab_result_events",
    "lab_critical_events",
    "lab_module_events",
)


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply(conn) -> None:
    for ddl in _TABLES:
        conn.execute(ddl)
    for name, table, columns, where, unique in _INDEXES:
        if not set(columns) <= _columns(conn, table):
            continue
        kind = "UNIQUE INDEX" if unique else "INDEX"
        clause = f" WHERE {where}" if where else ""
        conn.execute(f"CREATE {kind} IF NOT EXISTS {name} ON {table}({', '.join(columns)}){clause}")
    for table in _APPEND_ONLY:
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update "
            f"BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )


MIGRATION = Migration(55, "lab_contract", apply)
