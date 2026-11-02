"""Durable, versioned AI-summary drafts and a bounded refresh work ledger.

The data stays in Orb's configured database (including SQLCipher), not a
second plaintext cache. Source revisions are changed in the SAME transaction
as the clinical write, including corrections/deletions and patient reassignment.
They deliberately over-invalidate: a correction to older history also makes a
draft unavailable until regenerated. Routine observations are a separate soft
revision: they mark an existing draft explicitly out of date, without making
continuous monitors prevent every background generation from ever publishing.
Critical risk inserts use Orb's existing classification and hard-invalidate.
New clinical source tables must be added to this contract in a later
migration before a summarizer starts reading them.
"""

from __future__ import annotations

from typing import Any

from migrations import Migration

# Only table names declared here enter SQL. The service reads this same fixed
# contract. This is not discovery of arbitrary tables or client-controlled SQL.
SOURCE_TABLES = (
    "patients",
    "admissions",
    "patient_profiles",
    "vitals",
    "risk_scores",
    "medications",
    "med_orders",
    "medication_administrations",
    "patient_allergies",
    "patient_allergy_assessments",
    "problem_list",
    "orders",
    "lab_results",
    "clinical_documents",
    "scribe_notes",
    "lens_analyses",
    "nursing_notes",
    "care_bundles",
    "observations",
)


def apply(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS patient_summary_source_versions (
        patient_id TEXT PRIMARY KEY,
        version INTEGER NOT NULL DEFAULT 0,
        hard_version INTEGER NOT NULL DEFAULT 0,
        changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS patient_summary_snapshots (
        facility_id INTEGER NOT NULL,
        admission_id TEXT NOT NULL,
        patient_id TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        hard_source_version INTEGER NOT NULL,
        prompt_version TEXT NOT NULL,
        model TEXT NOT NULL,
        summary_json TEXT NOT NULL,
        source_metadata_json TEXT NOT NULL,
        generated_at REAL NOT NULL,
        source_captured_at REAL NOT NULL,
        PRIMARY KEY (facility_id, admission_id, patient_id, prompt_version, model)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS patient_summary_jobs (
        facility_id INTEGER NOT NULL,
        admission_id TEXT NOT NULL,
        patient_id TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        model TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        hard_source_version INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'ready', 'failed', 'inactive')),
        requested_at REAL NOT NULL,
        next_run_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        claim_token TEXT,
        claim_owner TEXT,
        claimed_at REAL,
        error_code TEXT,
        updated_at REAL NOT NULL,
        PRIMARY KEY (facility_id, admission_id, patient_id, prompt_version, model)
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summary_jobs_due "
        "ON patient_summary_jobs(state, next_run_at)"
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS patient_summary_scheduler_lease (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        owner TEXT NOT NULL,
        expires_at REAL NOT NULL
    )""")
    for table in SOURCE_TABLES:
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
        key = "id" if table == "patients" else "patient_id"
        if key not in cols:
            continue  # Older/minimal databases can lack an optional source.
        for event in ("INSERT", "UPDATE", "DELETE"):
            if table == "patients" and event == "DELETE":
                conn.execute(
                    'CREATE TRIGGER IF NOT EXISTS "summary_revision_patients_delete" '
                    'AFTER DELETE ON "patients" BEGIN '
                    "DELETE FROM patient_summary_snapshots WHERE patient_id = OLD.id; "
                    "DELETE FROM patient_summary_jobs WHERE patient_id = OLD.id; "
                    "DELETE FROM patient_summary_source_versions WHERE patient_id = OLD.id; END"
                )
                continue
            aliases = (
                ("OLD", "NEW")
                if event == "UPDATE"
                else ("OLD" if event == "DELETE" else "NEW",)
            )
            statements = []
            for alias in aliases:
                # UPDATE bumps both sides if a row is moved to another patient.
                # Two increments for an ordinary UPDATE are harmless: revisions
                # are opaque monotonic tokens, never a count of clinical events.
                hard = "1"
                if event == "INSERT" and table == "vitals":
                    hard = "0"
                elif event == "INSERT" and table == "risk_scores":
                    hard = f"CASE WHEN {alias}.level = 'critical' THEN 1 ELSE 0 END"
                statements.append(
                    "INSERT INTO patient_summary_source_versions(patient_id, version, hard_version, changed_at) "
                    f"SELECT {alias}.{key}, 1, {hard}, CURRENT_TIMESTAMP WHERE {alias}.{key} IS NOT NULL "
                    f"AND EXISTS (SELECT 1 FROM patients WHERE id = {alias}.{key}) "
                    "ON CONFLICT(patient_id) DO UPDATE SET version = version + 1, "
                    "hard_version = hard_version + excluded.hard_version, "
                    "changed_at = CURRENT_TIMESTAMP;"
                )
            conn.execute(
                f'CREATE TRIGGER IF NOT EXISTS "summary_revision_{table}_{event.lower()}" '
                f'AFTER {event} ON "{table}" BEGIN ' + " ".join(statements) + " END"
            )


MIGRATION = Migration(89, "patient_summary_snapshots", apply)
