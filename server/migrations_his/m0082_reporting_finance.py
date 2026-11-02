"""Migration 82: local report definitions and attributable finance work.

No account mapping, tariff, revenue share, tax rate or doctor attribution is
seeded. These administrative tables cannot constrain a clinical write. Journal
exports and approved payout statements keep their exact source snapshots in
the encrypted application database; no report is written to a public directory.
"""
from migrations import Migration

DDL = (
    """CREATE TABLE IF NOT EXISTS report_definitions (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
        query_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_report_definitions_owner ON report_definitions(owner_id, updated_at)",
    """CREATE TABLE IF NOT EXISTS finance_account_mappings (
        facility_id INTEGER NOT NULL, account_role TEXT NOT NULL,
        account_code TEXT NOT NULL, account_name TEXT NOT NULL,
        updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (facility_id, account_role)
    )""",
    """CREATE TABLE IF NOT EXISTS finance_charge_attributions (
        charge_id INTEGER PRIMARY KEY, doctor_id TEXT NOT NULL, reason TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS finance_payout_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, facility_id INTEGER NOT NULL,
        doctor_id TEXT NOT NULL, selector_type TEXT NOT NULL
            CHECK(selector_type IN ('service','category','all')),
        selector_value TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('percentage','fixed')),
        basis TEXT NOT NULL CHECK(basis IN ('gross_ex_tax','net_ex_tax')),
        recognition TEXT NOT NULL CHECK(recognition IN ('billed','fully_collected')),
        percent_bp INTEGER, fixed_paise INTEGER,
        effective_from TEXT NOT NULL, effective_to TEXT, is_active INTEGER NOT NULL DEFAULT 1,
        created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        CHECK((mode = 'percentage' AND percent_bp BETWEEN 0 AND 10000 AND fixed_paise IS NULL)
           OR (mode = 'fixed' AND fixed_paise >= 0 AND percent_bp IS NULL))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_finance_rules_lookup ON finance_payout_rules(facility_id, doctor_id, effective_from, effective_to)",
    """CREATE TABLE IF NOT EXISTS finance_payout_statements (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL,
        from_day TEXT NOT NULL, to_day TEXT NOT NULL, doctor_id TEXT,
        status TEXT NOT NULL CHECK(status IN ('draft','approved','paid','cancelled')),
        total_paise INTEGER NOT NULL, deduction_paise INTEGER NOT NULL,
        net_paise INTEGER NOT NULL, deduction_reason TEXT,
        preview_hash TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        approved_by TEXT, approved_at TEXT, paid_by TEXT, paid_at TEXT,
        payment_reference TEXT, cancelled_by TEXT, cancelled_at TEXT,
        idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
        UNIQUE(created_by, idempotency_key)
    )""",
    """CREATE TABLE IF NOT EXISTS finance_payout_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT, statement_id TEXT NOT NULL,
        source_line_id INTEGER NOT NULL, patient_id TEXT NOT NULL,
        doctor_id TEXT NOT NULL, attribution_version INTEGER NOT NULL,
        rule_id INTEGER NOT NULL, source_kind TEXT NOT NULL CHECK(source_kind IN ('earning','credit')),
        basis_paise INTEGER NOT NULL, amount_paise INTEGER NOT NULL,
        snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(statement_id, source_line_id, doctor_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_finance_payout_lines_statement ON finance_payout_lines(statement_id)",
    """CREATE TABLE IF NOT EXISTS finance_payout_claims (
        source_line_id INTEGER NOT NULL, doctor_id TEXT NOT NULL,
        statement_id TEXT NOT NULL, claimed_at TEXT NOT NULL,
        PRIMARY KEY(source_line_id, doctor_id), UNIQUE(source_line_id)
    )""",
    """CREATE TABLE IF NOT EXISTS finance_journal_export_patients (
        export_id TEXT NOT NULL, patient_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(export_id, patient_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_finance_journal_export_patients_patient ON finance_journal_export_patients(patient_id, created_at)",
    """CREATE TABLE IF NOT EXISTS finance_journal_exports (
        id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL,
        from_day TEXT NOT NULL, to_day TEXT NOT NULL,
        rows_json TEXT NOT NULL, mappings_json TEXT NOT NULL,
        debit_paise INTEGER NOT NULL, credit_paise INTEGER NOT NULL,
        content_sha256 TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
        UNIQUE(created_by, idempotency_key)
    )""",
)


def apply(conn) -> None:
    for statement in DDL:
        conn.execute(statement)


MIGRATION = Migration(82, "reporting_finance", apply)
