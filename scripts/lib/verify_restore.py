#!/usr/bin/env python3
"""Orb — the one verifier that decides whether a database copy is a good restore.

WHY THIS FILE EXISTS
--------------------
Three separate places had to answer the same question — "is this file something
we could actually come back from?" — and each answered it with its own inline
copy of the same Python:

    scripts/backup.sh          (gates rotation: an unverified backup must never
                                cause a verified one to be pruned)
    scripts/restore-drill.sh   (the monthly drill, rehearsed and signed off with
                                hospital IT — the compliance artefact)
    scripts/standby-restore.sh (the nightly warm-standby install gate)

Three copies drifted, exactly as three copies do. All three carried a comment
saying their table list was "kept in agreement with encrypt_db.py::VERIFY_TABLES"
while checking 4 tables against that list's 14 — a comment asserting a
consistency that had not been true for a while. So the checks live here once,
and the scripts call this file.

THE DEFECT THIS WAS WRITTEN FOR (2026-08-14)
--------------------------------------------
Every one of those three verifiers asserted row counts in FOUR tables —
patients, vitals, audit_logs, users. Those four are the pre-spine schema.
Migrations 20-26 added 62 more (billing, payments, claims, procurement, front
desk, biomedical, housekeeping, ABDM, workforce, dietary, NABH). That 62 is the
admin spine alone. Counting the pre-spine migrations (4-19) and migration 29's
Sage assessment tables as well, the manifest below lists 88 tables across 17
groups at schema v33 (as of 2026-09-04). MODULE_TABLES, and the
ALL_MIGRATED_TABLES derived from it, is the authority for what "every table"
means. The numbers in this paragraph are a snapshot, and
test_manifest_covers_every_migrated_table in server/tests/test_warm_standby.py
fails the moment the manifest and migrations.py disagree. A backup taken before
migration 20 does not contain a single one of the spine tables, and every check
above passed on it: it decrypts, integrity_check says ok, it has patients and
vitals and users, and its audit chain is intact. `RESULT: PASS`.

Restore that file and the appliance comes back with the entire administrative
spine gone — no invoices, no payments, no claim history, no stock, no incident
register — and the drill that certified it said nothing. That is not a
hypothetical path: F4 in docs/INSTALL_RUNBOOK.md tells an operator whose newest
backup failed to walk BACKWARDS through the generations until one passes. That
loop is precisely how you land on an ancient backup, and it is the moment the
drill most needs to be able to say "this one is too old to be whole".

So this verifier asserts three things the old ones did not:

  1. the SCHEMA VERSION recorded in the backup's own `schema_migrations` ledger,
     against the highest migration this checkout ships (read live from
     server/migrations.py, so it can never go stale);
  2. the PRESENCE of every table any migration creates — which catches the same
     loss even when the ledger is missing, reset, or lying;
  3. row floors on reference rows that the MIGRATIONS THEMSELVES write, where a
     count of zero can only mean loss.

WHAT IT DELIBERATELY DOES NOT ASSERT
------------------------------------
That the module tables hold data. `invoices` is legitimately empty on an
appliance that has not started billing, and legitimately catastrophic if it is
empty the day after go-live. Orb cannot tell those apart from inside a backup
file; the site can. So module row counts are REPORTED, with the ambiguity
stated in the output rather than resolved by guessing, and a site past go-live
promotes any group to a hard check with ORB_BACKUP_REQUIRE_NONEMPTY.

POINT THIS AT A COPY, NOT AT AN ARTEFACT
----------------------------------------
Opening a SQLCipher database creates -wal/-shm sidecars beside it and can write
to the file itself. All three callers copy to scratch first and pass the copy;
anyone invoking this by hand must do the same. Handing it a file in backups/ (or
a mounted offsite share) means mutating the thing you are validating — and on a
shared sync volume, mutating the primary's backup.

Usage:
    ORB_DB_ENCRYPTION_KEY=... verify_restore.py --db SCRATCH_COPY [options]
    ... --receipt <stamp>.verified.json [--backup-file NAME]

THE RECEIPT
-----------
With --receipt, the verdict is also written as JSON (schema
RECEIPT_SCHEMA below) beside the backup, on PASS and on FAIL. It is what the
operations status page (server/services/ops_health_service.py) reads to say a
backup was checked. Until 2026-10-02 nothing wrote one, so the page could only
ever call a good nightly backup "unchecked". No receipt is written when the
check could not run at all (exit 1): that is not a verdict on the file.

Exit codes: 0 verified · 2 verification failed · 1 could not run the check.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent
REPO_ROOT = LIB_DIR.parent.parent

# The receipt format ops_health_service.VERIFICATION_SCHEMA reads. Versioned so a
# later field (a restore rehearsal, an off-box confirmation) cannot make an old
# receipt be misread as a new one.
RECEIPT_SCHEMA = "orb.backup.verification/1"


# ── The manifest ─────────────────────────────────────────────────────────────
# Every table created by a migration in server/migrations.py, grouped the way an
# operator thinks about them. This list is EXPLICIT rather than parsed out of
# migrations.py at run time on purpose: drill day is the worst possible moment
# for a regex over a 4,000-line file to quietly match fewer tables than it did
# last month. It cannot silently fall behind either —
# server/tests/test_warm_standby.py::test_manifest_covers_every_migrated_table
# re-derives the list from migrations.py's AST and fails if a new migration adds
# a table that never got added here.
#
# From migration 48 on, migrations are one file each in server/migrations_his/
# (m<NNNN>_<name>.py). The same test walks every one of those files too, so a
# table created there must be added here exactly like one in migrations.py.
#
# Baseline tables from database.init_db (patients, vitals, users, audit_logs,
# medications, ...) are NOT listed here; the clinical floor below covers the
# ones whose absence matters, and the baseline predates every backup that could
# plausibly be restored.
MODULE_TABLES: dict[str, tuple[str, ...]] = {
    # ── pre-spine migrations (4-19) ──
    "clinical rails": (
        "sage_insights", "medication_allergy_overrides", "medication_dose_overrides",
        "message_room_fit_flags", "channel_members", "clinical_escalations",
        "escalation_events", "order_set_applications", "patient_review_marks",
    ),
    "shadow instrument": (
        "shadow_events",
    ),
    "dpdp consent": (
        "consent_records", "consent_withdrawals", "erasure_requests",
        "erasure_decisions", "erasure_executions", "retention_policies",
        "breach_records", "breach_notifications",
    ),
    "history import": (
        "history_import_runs", "history_import_rejects", "history_import_patient_xref",
    ),
    "identity + roster": (
        "enrolled_devices", "roster_import_runs",
    ),
    # ── migration 20: the hospital admin spine ──
    "billing": (
        "service_items", "tariff_plans", "tariff_rates", "package_rates",
        "payers", "patient_payer_policies", "encounter_charges",
        "invoices", "invoice_lines", "document_number_series", "discount_approvals",
    ),
    "payments": (
        "payments", "payment_allocations", "patient_deposits", "refunds",
    ),
    "claims": (
        "claims", "claim_events",
    ),
    "procurement": (
        "vendors", "stores", "inventory_items", "indents", "indent_lines",
        "purchase_orders", "purchase_order_lines", "goods_receipts",
        "goods_receipt_lines", "stock_movements",
    ),
    "front desk": (
        "cash_counter_sessions", "discharge_financial_checks",
    ),
    # ── migrations 21-26 ──
    "biomedical": (
        "biomedical_assets", "biomedical_schedules", "biomedical_schedule_events",
        "biomedical_service_calls", "biomedical_contracts", "biomedical_contract_assets",
    ),
    "housekeeping": (
        "housekeeping_units", "housekeeping_unit_events", "housekeeping_tasks",
        "housekeeping_linen_items", "housekeeping_linen_movements",
    ),
    "abdm": (
        "abha_identities", "abha_verifications", "abdm_care_contexts",
        "abdm_consent_artefacts", "abdm_events",
    ),
    "workforce": (
        "staff_employment", "duty_shift_types", "duty_roster", "attendance_days",
        "leave_types", "hospital_holidays", "payroll_export_runs",
    ),
    "dietary": (
        "diet_orders", "meal_plans", "meal_plan_lines", "kitchen_indents",
        "kitchen_indent_lines",
    ),
    "nabh": (
        "nabh_incidents", "nabh_incident_actions", "nabh_incident_events",
        "nabh_discharge_dispositions", "nabh_evidence_exports",
    ),
    # ── migration 29: Sage's drafted assessment on a critical Vigil alert ──
    # `sage_assessments` holds machine-authored clinical reasoning a clinician
    # read and either endorsed or rejected, and `sage_assessment_events` is the
    # operational record of that verdict. Losing them on a restore loses the
    # answer to "what was the doctor shown when they made this decision?" —
    # which is the question an incident review starts from.
    "sage assessments": (
        "sage_assessments", "sage_assessment_events", "sage_assessment_actions",
    ),
    # ── migration 49 (server/migrations_his/m0049_helix_tables_in_runner.py) ──
    # The Helix pharmacy chain: the drug master, stock lots, every pharmacy
    # order with its verification, dispense movement and state transition.
    # These used to be created only by helix_db.init_helix_db at boot, outside
    # the migration runner, so they were treated as baseline and never checked
    # here. From migration 49 a migration creates them, and a restore that came
    # back without them would lose who verified, dispensed and received each
    # dose.
    "helix pharmacy": (
        "helix_medication_master", "helix_inventory_lots", "helix_patient_med_orders",
        "helix_pharmacy_verifications", "helix_stock_movements", "helix_transitions",
    ),
    # ── migrations 50 and 51 (server/migrations_his/m0050_registration_opd_foundation.py,
    # m0051_adt_movement_ledger.py) ──
    # The outpatient visit register and the patient movement ledger used to be
    # created lazily by the front desk, outside the runner, so they were never
    # checked here. The registration tables are empty until a site switches
    # registration on. A restore without them would lose who was seen in
    # which clinic, every ward transfer, and every correction to a patient's
    # identity.
    "registration and outpatients": (
        "opd_visits", "patient_demographics", "patient_identity_changes",
        "patient_duplicate_candidates", "patient_match_keys", "opd_display_boards",
    ),
    "movement ledger": (
        "patient_movements",
    ),
    # ── migration 54 (server/migrations_his/m0054_pharmacy_store_schema.py) ──
    # The pharmacy store: par levels, transfers between drug stores, lot
    # status events, stock takes, ward and vendor returns, two-person drug
    # master requests, and the NDPS register with its variances. Empty until a
    # site switches ORB_PHARMACY_STORE on. A restore that came back without the
    # register would lose a statutory controlled-drug record.
    "pharmacy store": (
        "pharmacy_par_levels", "pharmacy_transfers", "pharmacy_transfer_lines",
        "pharmacy_lot_events", "pharmacy_stock_counts", "pharmacy_stock_count_lines",
        "pharmacy_ward_returns", "pharmacy_vendor_returns", "pharmacy_vendor_return_lines",
        "pharmacy_master_change_requests", "ndps_variances", "ndps_register",
    ),
    # ── migration 55 (server/migrations_his/m0055_lab_contract.py) ──
    # The laboratory information system: the test master with its approved
    # reference ranges and critical limits, specimens, order items, results,
    # critical-result callbacks and their append-only events. Empty until a
    # site switches ORB_LAB on. A restore that came back without them would
    # lose which range a released result was flagged against and who was told
    # about a critical result, and when.
    "laboratory": (
        "lab_sections", "lab_specimen_types", "lab_tests", "lab_panel_components",
        "lab_reference_ranges", "lab_critical_limits", "lab_rejection_reasons",
        "lab_accession_counters", "lab_specimens", "lab_specimen_events",
        "lab_order_items", "lab_results", "lab_result_events",
        "lab_critical_notifications", "lab_critical_events", "lab_exceptions",
        "lab_module_events",
    ),
    # ── migration 53 (server/migrations_his/m0053_adt_bed_master.py) ──
    # The bed master: wards, their rooms, bed classes and the append-only
    # record of who changed them. Empty until a site switches ORB_ADT on and
    # sets up its beds. A restore that came back without them would leave
    # every registered bed pointing at a ward that no longer exists.
    "bed master": (
        "wards", "ward_rooms", "bed_classes", "bed_master_events",
    ),
    # ── migration 57 (server/migrations_his/m0057_clinic_sessions.py) ──
    # Clinic timings: each doctor's weekly outpatient sessions and the days a
    # session does not run. Configuration, no patient data. Empty until a site
    # switches ORB_REGISTRATION on and sets up its clinics. A restore that came
    # back without them would lose every clinic timetable and its history.
    "clinic schedule": (
        "clinic_sessions", "clinic_session_exceptions",
    ),
    # ── migration 67 (server/migrations_his/m0067_opd_charge_rules.py) ──
    # Which code an outpatient consultation, a follow-up inside its window and
    # the registration fee are charged against. Configuration, no patient
    # data. Empty until a site switches ORB_OPD on and sets its rules. A
    # restore that came back without it would charge every clinic the plain
    # consultation code again and drop the registration fee unnoticed.
    "outpatient charge rules": (
        "opd_charge_rules",
    ),
    # ── migration 60 (server/migrations_his/m0060_result_quarantine.py) ──
    # Interfaced results that matched no patient, held for laboratory staff to
    # attach to the right patient or discard with a reason, and every decision
    # taken on them. Empty until a site switches ORB_RESULT_QUARANTINE on. A
    # restore that came back without it would lose results nobody has matched
    # yet, and the record of which patient each attached result was given to.
    "result quarantine": (
        "result_quarantine",
    ),
    # ── migration 64 (server/migrations_his/m0064_lab_analysers.py) ──
    # The analyser registry: each analyser allowed to send results, the hash
    # of its key, and which Orb test each of its codes is. Configuration, no
    # patient data. Empty until a site switches ORB_LAB on and registers an
    # analyser. A restore that came back without them would leave every
    # analyser refused at intake and lose who approved each code's mapping.
    "lab analysers": (
        "lab_instruments", "lab_instrument_test_map",
    ),
    # ── migration 66 (server/migrations_his/m0066_housekeeping_movement_links.py) ──
    # Which bed movements housekeeping has acted on: the turnaround clean it
    # raised, or the bed it marked occupied, for each movement and side. No
    # patient data. Empty until a site switches ORB_ADT on. A restore that
    # came back without it would have the next sync act on every movement in
    # its window again.
    "housekeeping links": (
        "housekeeping_movement_links",
    ),
    # ── migration 69 (server/migrations_his/m0069_bed_reservations.py) ──
    # Beds held for a patient on the way (an admission, a transfer, a return
    # from surgery), each with who held it, until when, and how the hold
    # ended. Empty until a site switches ORB_ADT on. A restore that came back
    # without it would offer a held bed to the next patient and lose who
    # reserved which bed.
    "bed reservations": (
        "bed_reservations",
    ),
    # ── migration 61 (server/migrations_his/m0061_med_order_store.py) ──
    # The medication-order store: one row per medication order and its
    # append-only ledger of who prescribed, verified, held, stopped and gave
    # it. Empty until a site switches ORB_MED_ORDER_STORE on. A restore that
    # came back without them would lose which drugs are on hold and who
    # signed and verified each order.
    "medication orders": (
        "med_orders", "med_order_events",
    ),
    # ── migration 71 (server/migrations_his/m0071_med_order_schedule_times.py) ──
    # The times a prescriber set for each medication order, and who set them.
    # Empty until a site switches ORB_MED_ORDER_LIFECYCLE on and a prescriber
    # sets times. A restore that came back without it would put every drug
    # back on the times worked out from its frequency, silently moving doses
    # the prescriber had timed.
    "medication order times": (
        "med_order_schedule_times",
    ),
    # ── migration 62 (server/migrations_his/m0062_opd_consultations.py) ──
    # The outpatient consultation note for each OPD visit, signed and then
    # final, and its append-only addenda. Empty until a site switches ORB_OPD
    # on. A restore that came back without them would lose what the clinic
    # doctor found, diagnosed and advised, and who signed it.
    "outpatient consultations": (
        "opd_consultations", "opd_consultation_addenda",
        # migration 63 (m0063_patient_allergy_assessments.py): each allergy
        # review, 'no known allergies' included. Without it a restore could
        # not tell "asked, none known" from "never asked" again.
        "patient_allergy_assessments",
    ),
    # ── migration 78 (the Orb Ward Record's rounds file) ──
    # Each ward's observation interval and grace window, set in writing by the
    # nursing superintendent, and the record of every monthly rounds file
    # produced. A restore that came back without them would lose the interval
    # every due time and every late round is counted against, and which file
    # the quality coordinator was given.
    "ward record": (
        "ward_observation_settings", "ward_record_exports",
    ),
    # ── migration 70 (server/migrations_his/m0070_facilities_spine.py) ──
    # The facility spine: the hospital sites of this legal entity (row 1,
    # MAIN, always), the departments master, which site each department-less
    # account belongs to, and the named group-wide access grants. No patient
    # data. A restore that came back without them would refuse to start (no
    # facility 1) and lose which site every department and account belongs to.
    "facilities": (
        "facilities", "departments", "user_facilities", "enterprise_access_grants",
    ),
    # Migrations 81-86: default-off local modules still create their schemas.
    # Presence is mandatory even before activation; site-configured nonempty
    # checks retain their existing meaning after a module goes live.
    "feedback": ("feedback_cases", "feedback_events"),
    "mortuary": ("mortuary_slots", "mortuary_cases", "mortuary_events"),
    "cssd": (
        "cssd_sets", "cssd_set_items", "cssd_packs", "cssd_cycles",
        "cssd_cycle_packs", "cssd_issues", "cssd_events",
    ),
    "reporting": ("report_definitions",),
    "finance": (
        "finance_account_mappings", "finance_charge_attributions",
        "finance_journal_exports", "finance_journal_export_patients",
        "finance_payout_rules", "finance_payout_statements",
        "finance_payout_lines", "finance_payout_claims",
    ),
    "radiology": ("ris_studies", "ris_studies_events"),
    "care plans": ("care_plans", "care_plans_events"),
    "emergency": ("emergency_visits", "emergency_visits_events"),
    "perioperative": ("perioperative_records", "perioperative_records_events"),
    "dialysis": ("dialysis_sessions", "dialysis_sessions_events"),
    "record governance": (
        "record_legal_holds", "mrd_files", "mrd_release_requests", "record_governance_events",
    ),
    "infection surveillance": (
        "infection_definitions", "infection_windows", "infection_cases",
        "infection_exposure_days", "infection_susceptibility",
        "infection_isolation_records", "infection_events", "infection_requests",
    ),
    "radiation safety": ("radiation_safety_records", "radiation_safety_events"),
}

ALL_MIGRATED_TABLES: tuple[str, ...] = tuple(
    t for tables in MODULE_TABLES.values() for t in tables
)

# The pre-spine clinical floor, unchanged since the zero-byte-backup rewrite:
# a database that opens but holds no patients is not a backup.
CLINICAL_FLOOR: dict[str, int] = {
    "patients": 1, "vitals": 1, "audit_logs": 1, "users": 1,
}

# Rows the MIGRATIONS write, not rows a hospital types. Migration 20 seeds the
# self-pay tariff plan, the SELF payer, the CENTRAL store and seven document
# number series; 22 seeds the linen catalogue; 24 seeds four shift types and six
# leave types; 25 and 26 add three more series. Because the migration puts them
# there, zero rows in these tables cannot mean "this module is not in use yet" —
# it can only mean the rows were lost. That makes them the one honest row-count
# assertion available for the post-spine schema.
#
# The floor is 1, not the seeded count: a site is free to delete the placeholder
# series it does not use, or the SELF payer once it has typed its own.
SEEDED_REFERENCE: dict[str, int] = {
    "tariff_plans": 1,
    "payers": 1,
    "stores": 1,
    "document_number_series": 1,
    "housekeeping_linen_items": 1,
    "duty_shift_types": 1,
    "leave_types": 1,
    # Migration 70 seeds facility 1 (MAIN), and facilities rows are never
    # deleted. Orb refuses to start without facility 1, so a restore with none
    # is not one it could come back from.
    "facilities": 1,
}


def expected_schema_version() -> int:
    """The highest migration version THIS checkout ships.

    Read from server/migrations.py itself rather than copied into a constant
    here, because a constant is a thing that gets forgotten: the whole point of
    the version check is to notice a backup that predates the code, and a stale
    expectation would defeat it silently. Loaded by file location (not `import
    migrations`) so a same-named package in site-packages can never be picked up
    instead. The module imports only json/logging/typing, so this is cheap and
    has no side effects.
    """
    path = REPO_ROOT / "server" / "migrations.py"
    spec = importlib.util.spec_from_file_location("orb_migrations_manifest", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return max(m.version for m in module.MIGRATIONS)


def key_hex(key: str) -> str:
    """Mirror database.py::_sqlcipher_key_hex exactly.

    Orb passes an explicit raw key (PRAGMA key = "x'<64 hex>'"), which skips
    SQLCipher's per-open KDF and accepts operator key material of any length.
    Deriving it any other way here would fail to open a perfectly good backup
    and report a false disaster on the night the primary actually dies.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def open_encrypted(path: str, key: str):
    """Open a SQLCipher database and PROVE the key is right.

    connect() succeeds with a wrong key; the first real read is the proof, so
    this does one before returning.
    """
    from sqlcipher3 import dbapi2 as sq

    conn = sq.connect(path)
    conn.execute(f"PRAGMA key = \"x'{key_hex(key)}'\"")
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return conn


class Reporter:
    """Prints one aligned line per check and remembers what failed."""

    def __init__(self, prefix: str):
        self.prefix = prefix
        self.failures: list[str] = []
        self.warnings: list[str] = []
        # Kept for the receipt; the console lines are for a person.
        self.integrity: str | None = None

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        dots = "." * max(1, 20 - len(label))
        suffix = f" — {detail}" if detail else ""
        print(f"[{self.prefix}]   {label} {dots} {'PASS' if ok else 'FAIL'}{suffix}")
        if not ok:
            self.failures.append(label)
        return ok

    def warn(self, label: str, detail: str) -> None:
        print(f"[{self.prefix}]   {label} ... WARN — {detail}")
        self.warnings.append(f"{label}: {detail}")

    def info(self, text: str) -> None:
        print(f"[{self.prefix}]   {text}")

    def note(self, text: str) -> None:
        print(f"[{self.prefix}] {text}")


def _count(conn, table: str):
    """Row count, or None if the table cannot be read at all."""
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return None


def _existing_tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    return {r[0] for r in rows}


# ── The checks ───────────────────────────────────────────────────────────────
def check_schema_version(conn, rep: Reporter, policy: str) -> None:
    """Assert the backup's own migration ledger against the shipped code.

    `policy` is "fail" for the paths that are deciding whether a file is a good
    thing to RESTORE (the drill, the standby), and "warn" for scripts/backup.sh,
    which is deciding whether to keep a snapshot it just took of the LIVE
    database. Those are different questions: a snapshot of a behind-schema live
    database is still the best copy of that database in existence, and refusing
    to keep it — which in backup.sh also means refusing to rotate — would invent
    a new way to have no backups at all.
    """
    try:
        expected = expected_schema_version()
    except Exception as exc:
        rep.check("schema version", False, f"cannot read server/migrations.py: {exc}")
        return

    if "schema_migrations" not in _existing_tables(conn):
        rep.check(
            "schema version", False,
            f"no schema_migrations ledger — this file predates the migration "
            f"framework; the code ships v{expected}",
        )
        return

    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    found = int(row[0]) if row and row[0] is not None else 0

    if found == expected:
        rep.check("schema version", True, f"v{found}, matching this build")
    elif found < expected:
        detail = (
            f"backup is at v{found}, this build ships v{expected} — it predates "
            f"migration(s) {found + 1}-{expected} and everything they hold"
        )
        if policy == "warn":
            rep.warn("schema version", detail)
        else:
            rep.check("schema version", False, detail)
    else:
        # Newer than the code reading it. Not this file's data at risk — the
        # RESTORE is, because the running build may not understand the schema.
        rep.warn(
            "schema version",
            f"backup is at v{found} but this build only ships v{expected} — it was "
            f"written by a NEWER Orb; restore it onto that build, not this one",
        )


def check_tables_present(conn, rep: Reporter, policy: str) -> None:
    """Every table any migration creates must exist.

    This is the check that does not depend on the ledger being honest. A DB
    whose `schema_migrations` says v26 but which is missing `invoices` is a
    half-applied migration or a hand-edited file, and either way restoring it
    would come back short.

    Follows the same `policy` as the version check, and for the same reason:
    under "warn" (scripts/backup.sh) a live database that is behind the shipped
    code must still get backed up. Refusing to keep the snapshot would be the
    larger data-loss event, and backup.sh's job is to capture the live database
    faithfully, not to grade its schema.
    """
    present = _existing_tables(conn)
    missing = [t for t in ALL_MIGRATED_TABLES if t not in present]
    total = len(ALL_MIGRATED_TABLES)
    if not missing:
        rep.check("module tables", True, f"all {total} present")
        return

    by_group = [
        f"{group} ({sum(1 for t in tables if t in missing)}/{len(tables)})"
        for group, tables in MODULE_TABLES.items()
        if any(t in missing for t in tables)
    ]
    head = ", ".join(missing[:6])
    more = f" (+{len(missing) - 6} more)" if len(missing) > 6 else ""
    detail = f"{len(missing)} of {total} MISSING: {head}{more}"
    if policy == "warn":
        rep.warn("module tables", detail)
    else:
        rep.check("module tables", False, detail)
    rep.note(f"  missing by module: {'; '.join(by_group)}")
    rep.note("  A restore from this file brings those modules back EMPTY.")


def check_clinical_floor(conn, rep: Reporter) -> None:
    """An empty-but-valid database must FAIL. The zero-byte-backup lesson."""
    for table, minimum in sorted(CLINICAL_FLOOR.items()):
        n = _count(conn, table)
        if n is None:
            rep.check(f"rows:{table}", False, "unreadable or absent")
        else:
            rep.check(f"rows:{table}", n >= minimum,
                      f"{n:,} rows" if n >= minimum else f"{n} rows (need >= {minimum})")


def check_seeded_reference(conn, rep: Reporter, policy: str) -> None:
    """Reference rows the migrations write themselves; zero can only mean loss.

    Two different situations are separated deliberately. A table that is ABSENT
    is the schema-behind story the check above already told, so it follows
    `policy`. A table that is PRESENT and EMPTY is not a schema story at all —
    the migration that created it also filled it, so something deleted those
    rows. That fails everywhere, including in backup.sh, because a live database
    that has lost its document number series is a live database with a problem
    tonight, not a stale artefact.
    """
    counts: list[str] = []
    absent: list[str] = []
    emptied: list[str] = []
    for table, minimum in sorted(SEEDED_REFERENCE.items()):
        n = _count(conn, table)
        if n is None:
            absent.append(table)
        elif n < minimum:
            emptied.append(f"{table}={n}")
        else:
            counts.append(f"{table} {n}")

    if emptied:
        rep.check("seeded reference", False,
                  f"migration-written rows are GONE from: {', '.join(emptied)}")
    elif absent and policy == "warn":
        rep.warn("seeded reference", f"table(s) absent (schema behind): {', '.join(absent)}")
    elif absent:
        rep.check("seeded reference", False,
                  f"table(s) absent entirely: {', '.join(absent)}")
    else:
        rep.check("seeded reference", True, ", ".join(counts))


def check_audit_chain(conn, rep: Reporter) -> None:
    """Hash-chain LINKAGE, not just readability: each row's prev_hash must equal
    the previous row's row_hash. A backup whose chain is broken restores into
    something indistinguishable from a tampered record, which is worse than no
    backup because it would be trusted."""
    try:
        rows = conn.execute(
            "SELECT id, prev_hash, row_hash FROM audit_logs ORDER BY id"
        ).fetchall()
    except Exception as exc:
        rep.check("audit chain", False, f"unreadable: {exc}")
        return
    breaks: list[int] = []
    prev = None
    for rid, prev_hash, row_hash in rows:
        if prev is not None and prev_hash != prev:
            breaks.append(rid)
        prev = row_hash
    if breaks:
        head = ", ".join(str(b) for b in breaks[:5])
        more = f" (+{len(breaks) - 5} more)" if len(breaks) > 5 else ""
        rep.check("audit chain", False, f"{len(breaks)} broken link(s) at id {head}{more}")
    else:
        rep.check("audit chain", True, f"{len(rows):,} rows, linkage intact")


def check_admin_present(conn, rep: Reporter, hard: bool) -> None:
    """Somebody must be able to sign in, or the switchover ends at a login screen
    nobody can pass. Hard-required on the standby (it is the machine that would
    be taking over); informational elsewhere."""
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND id!='system' "
            "AND COALESCE(is_active,1)=1"
        ).fetchone()[0]
    except Exception as exc:
        if hard:
            rep.check("admin present", False, f"unreadable: {exc}")
        else:
            rep.info(f"(info) admin present: unreadable — {exc}")
        return
    if hard:
        rep.check("admin present", n >= 1, f"{n} login-capable admin(s)")
    else:
        rep.info(f"(info) login-capable admins: {n}")


def report_module_data(conn, rep: Reporter, require_nonempty: set[str]) -> None:
    """Row counts per module — REPORTED, and hard-checked only where the site has
    said the module is live.

    The honest position, stated in the output because operators read the output
    and not this file: a module group with zero rows is exactly what a correct
    backup looks like before that module goes live, and exactly what a
    catastrophic one looks like after. Nothing inside the file distinguishes
    them. Orb will not guess, so it prints the counts and names the ambiguity.
    """
    rep.note("Module data (counts, not verdicts):")
    empties: list[str] = []
    for group, tables in MODULE_TABLES.items():
        counts = {t: _count(conn, t) for t in tables}
        readable = {t: n for t, n in counts.items() if n is not None}
        total = sum(readable.values())
        nonempty = sum(1 for n in readable.values() if n)
        label = group + " " + "." * max(1, 22 - len(group))
        if total:
            rep.info(f"{label} {total:>9,} rows across {nonempty}/{len(tables)} tables")
        else:
            rep.info(f"{label} {'EMPTY':>9}  ({len(readable)}/{len(tables)} tables present)")
            empties.append(group)

    if empties:
        rep.note(
            "  EMPTY above means: expected if that module has not gone live, and a "
            "REAL LOSS if it has."
        )
        rep.note(
            "  Nothing in the file can tell those apart — only the site can. Once a "
            "module is live, set"
        )
        rep.note(
            "  ORB_BACKUP_REQUIRE_NONEMPTY=billing,payments (group names as printed, "
            "spaces or underscores)"
        )
        rep.note("  so an empty one FAILS here instead of being a line of text.")

    for group in sorted(require_nonempty):
        tables = MODULE_TABLES.get(group)
        if tables is None:
            rep.check(f"required:{group}", False,
                      f"not a module group — known groups: "
                      f"{', '.join(g.replace(' ', '_') for g in MODULE_TABLES)}")
            continue
        total = sum(n for n in (_count(conn, t) for t in tables) if n)
        rep.check(f"required:{group}", total > 0,
                  f"{total:,} rows" if total else
                  "0 rows, but this site declared the module LIVE")


def report_search_indexes(conn, rep: Reporter) -> None:
    """Informational only — a restore is usable without these, but a missing
    vector/FTS index means Sage retrieval is degraded until a reindex."""
    for label, sql in (
        ("vector index", "SELECT COUNT(*) FROM clinical_knowledge_vec_rowids"),
        ("fts index", "SELECT COUNT(*) FROM clinical_knowledge_fts"),
    ):
        try:
            n = conn.execute(sql).fetchone()[0]
            rep.info(f"(info) {label}: {n:,} rows")
        except Exception as exc:
            rep.info(f"(info) {label}: not queryable here — {exc}")


def verify(conn, *, prefix: str, schema_policy: str = "fail",
           require_admin: bool = False,
           require_nonempty: set[str] | None = None) -> Reporter:
    """Run every check against an already-open, already-keyed connection.

    Returns the Reporter; `rep.failures` empty means verified. Ordered so the
    cheap structural checks print before the ones that scan tables.
    """
    rep = Reporter(prefix)
    try:
        status = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except Exception as exc:
        status = f"unreadable: {exc}"
    rep.integrity = str(status)
    rep.check("integrity_check", status == "ok", "" if status == "ok" else str(status))

    check_schema_version(conn, rep, schema_policy)
    check_tables_present(conn, rep, schema_policy)
    check_clinical_floor(conn, rep)
    check_seeded_reference(conn, rep, schema_policy)
    check_audit_chain(conn, rep)
    check_admin_present(conn, rep, hard=require_admin)
    report_module_data(conn, rep, require_nonempty or set())
    report_search_indexes(conn, rep)
    return rep


def _schema_version_found(conn) -> int | None:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def write_receipt(path: str, *, backup_file: str, ok: bool, integrity_check: str | None,
                  size_bytes: int | None, row_counts: dict | None,
                  schema_version: int | None, failures: list[str],
                  warnings: list[str], encrypted: bool = True) -> None:
    """Write the verdict beside the backup, atomically and owner-only.

    Atomic (temp file, then rename) so the status page can never read half a
    receipt; a torn file would read as "unchecked", which is wrong in the safe
    direction, but a backup that verified deserves to say so.
    """
    try:
        expected = expected_schema_version()
    except Exception:
        expected = None
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "backup_file": backup_file,
        "ok": bool(ok),
        "verified_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "integrity_check": integrity_check,
        "size_bytes": size_bytes,
        "row_counts": row_counts or {},
        "encrypted": encrypted,
        "schema_version": schema_version,
        "expected_schema_version": expected,
        "failures": failures,
        "warnings": warnings,
    }
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def _parse_groups(raw: str) -> set[str]:
    """Accept `billing,front_desk` or `billing, front desk` — operators type both."""
    out = set()
    for chunk in (raw or "").replace(";", ",").split(","):
        name = chunk.strip().lower().replace("_", " ")
        if name:
            out.add(name)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True,
                    help="a SCRATCH COPY to verify — never the live DB, and never the "
                         "backup artefact itself (opening it creates -wal/-shm beside it)")
    ap.add_argument("--prefix", default="verify", help="log prefix, e.g. drill/backup/standby")
    ap.add_argument("--schema-policy", choices=("fail", "warn"), default="fail")
    ap.add_argument("--require-admin", action="store_true",
                    help="fail unless a login-capable admin exists (the standby)")
    ap.add_argument("--require-nonempty", default="",
                    help="comma-separated module groups that MUST hold rows")
    ap.add_argument("--receipt", default="",
                    help="also write the verdict as JSON here (pass and fail), for the "
                         "operations status page: <backup stem>.verified.json")
    ap.add_argument("--backup-file", default="",
                    help="the artefact's file name to record in the receipt, when --db "
                         "is a scratch copy of it")
    args = ap.parse_args(argv)

    def receipt(ok: bool, *, integrity=None, rows=None, version=None,
                failures=(), warnings=()) -> None:
        if not args.receipt:
            return
        try:
            size = os.path.getsize(args.db)
        except OSError:
            size = None
        try:
            write_receipt(args.receipt, backup_file=args.backup_file or os.path.basename(args.db),
                          ok=ok, integrity_check=integrity, size_bytes=size, row_counts=rows,
                          schema_version=version, failures=list(failures),
                          warnings=list(warnings))
            print(f"[{args.prefix}] receipt written: {args.receipt}")
        except Exception as exc:
            # The verdict stands either way; only the page's view of it is lost.
            print(f"[{args.prefix}][warn] could not write receipt {args.receipt}: {exc}",
                  file=sys.stderr)

    key = os.environ.get("ORB_DB_ENCRYPTION_KEY", "")
    if not key:
        print(f"[{args.prefix}][fail] ORB_DB_ENCRYPTION_KEY is not set.", file=sys.stderr)
        return 1

    required = _parse_groups(args.require_nonempty) | _parse_groups(
        os.environ.get("ORB_BACKUP_REQUIRE_NONEMPTY", "")
    )

    try:
        conn = open_encrypted(args.db, key)
    except ImportError as exc:  # no sqlcipher3 in this interpreter
        print(f"[{args.prefix}][fail] {exc} — use server/.venv/bin/python.", file=sys.stderr)
        return 1
    except Exception as exc:
        # Name the variable. On the standby this is the single most common
        # failure and the fix is "compare .env on both machines" — an operator
        # reading "the key is wrong" at 3 a.m. should not have to guess which key.
        print(f"[{args.prefix}]   decrypt+open ........ FAIL — {exc}")
        print(f"[{args.prefix}] The ORB_DB_ENCRYPTION_KEY in this environment is not the key "
              f"this file was written")
        print(f"[{args.prefix}] with, or the file is damaged. On a standby, compare .env on "
              f"both machines.")
        receipt(False, failures=["decrypt+open"])
        return 2
    print(f"[{args.prefix}]   decrypt+open ........ PASS")

    try:
        rep = verify(conn, prefix=args.prefix, schema_policy=args.schema_policy,
                     require_admin=args.require_admin, require_nonempty=required)
        rows = {t: _count(conn, t) for t in sorted(CLINICAL_FLOOR)}
        version = _schema_version_found(conn)
    finally:
        conn.close()
    receipt(not rep.failures, integrity=rep.integrity, rows=rows, version=version,
            failures=rep.failures, warnings=rep.warnings)

    if rep.warnings:
        print(f"[{args.prefix}] {len(rep.warnings)} warning(s): "
              f"{'; '.join(rep.warnings)}")
    if rep.failures:
        print(f"[{args.prefix}] VERIFY FAILED — {len(rep.failures)} check(s): "
              f"{', '.join(rep.failures)}")
        return 2

    # The summary must not claim more than was actually established. Under
    # --schema-policy warn a file can pass while being schema-incomplete, and a
    # closing line reading "schema complete" on such a file would be the same
    # species of lie this whole verifier exists to stop telling.
    if rep.warnings:
        print(f"[{args.prefix}] VERIFY PASSED WITH WARNINGS — it decrypts, is internally "
              f"consistent, holds")
        print(f"[{args.prefix}] real clinical rows and its audit chain is intact. It is NOT "
              f"established as")
        print(f"[{args.prefix}] schema-complete: read the {len(rep.warnings)} warning(s) above "
              f"before treating it as a restore candidate.")
        return 0
    print(f"[{args.prefix}] VERIFY PASSED — decrypts, consistent, schema complete, "
          f"real clinical rows, chain intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
