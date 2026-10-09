"""Reset database patient-related tables and run the clean clinical seeder."""
import os
import sys
import sqlite3

# Ensure server root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import DB_PATH
from database import _build_connection
from db_backend import DBIntegrityError, DBOperationalError
from scripts.seed_clinical_data import seed
from scripts.seed_helix import seed_helix

# Tables that must SURVIVE a reseed even though the schema walk below would
# otherwise select them.
TABLES_TO_KEEP = {
    # Tamper-evident hash chain. A reseed RE-SEALS it (step 8) rather than
    # wiping it, so the demo DB keeps a verifiable audit history.
    "audit_logs",
}

# Patient-derived tables the schema walk cannot reach on its own: no patient_id
# column and no foreign key into a patient-scoped table.
EXTRA_TABLES_TO_CLEAR = [
    "patients",             # the root row itself — its key is `id`, not patient_id
    "hospital_forecasts",   # ward-level aggregates, rebuilt by the forecast service
    "admission_history",    # census history aggregates
    "world_model_state",    # keyed entity_type/entity_id, holds PHI-derived state
    "event_deliveries",     # keyed on event_log.event_id, no FK declared
    "ingestion_events",     # raw HL7/FHIR payloads — the PARENT of vitals/observations
    # ── Sage's conversational record (2026-09-07) ──
    # These four hold patient data as FREE TEXT. None has a patient_id and none
    # has a foreign key into anything that does, so the walk below is blind to
    # them BY CONSTRUCTION — and unlike the tables above, no future migration
    # will ever bring them into range, because the property the walk keys on is
    # one they will never have. Left out, a reseed deleted every patient and
    # kept the transcripts describing them.
    #
    # Two were already being orphaned by the walk's own success. Its docstring
    # names "orphan sage_insights … referencing patients that no longer exist"
    # as the failure it was written against, and it was producing the same shape
    # one table over: `sage_provenance_turns` HAS a patient_id and is wiped, so
    # its refs (turn_id) and their blobs (cid) lost their parent every reseed.
    # `sage_history_summaries` keys to `sage_conversations` rows the same way,
    # which is the concern `services/sage_service.clear_sage_history` states out
    # loud: "a summary that outlives its deleted conversation is retained PHI".
    # A reseed was producing that one level up — a transcript that outlives its
    # deleted PATIENT.
    #
    # Wiping provenance is not a new policy call: `sage_provenance_turns` is
    # already wiped today, so these two only stop it being wiped by halves.
    "sage_conversations",      # clinician/Sage transcripts; PHI in free text
    "sage_history_summaries",  # cached summaries of those turns; same PHI
    "sage_provenance_refs",    # turn_id -> cid; child of the wiped provenance turns
    "sage_provenance_blobs",   # content-addressed prompt/answer bodies
    # ── Module documents whose stock or bed state the wipe already resets ──
    # stock_movements carries a (nullable) patient_id, so the walk wipes it, GRN
    # receipts included. The purchase orders and goods receipts those receipts
    # posted from have no patient_id and survived, so every reseed showed a PO
    # "partially received" against a store holding none of it. The housekeeping
    # tables have no patient_id either, and kept weeks-old bed state (669 h in
    # one state) and labels from earlier seeds (CCU 16 beds of 15). seed_modules
    # recreates all of these, so wiping them keeps each module agreeing with
    # itself. Masters (vendors, stores, items, linen items) are upserted and kept.
    "indents",
    "indent_lines",
    "purchase_orders",
    "purchase_order_lines",
    "goods_receipts",
    "goods_receipt_lines",
    "housekeeping_units",
    "housekeeping_unit_events",
    "housekeeping_tasks",
    "housekeeping_linen_movements",
    # ── Pharmacy store documents (migration 54) ──
    # seed_helix deletes every drug lot on each reseed, so a transfer, stock
    # take, lot event or vendor return that survived would describe lots that
    # no longer exist. None of them has a patient_id, and their links use the
    # _ref suffix on purpose (see the migration), so the walk below cannot
    # reach them: they are named here. Pharmacy purchase orders, receipts and
    # indents are rows in the procurement tables above. pharmacy_ward_returns
    # carries a patient_id and is wiped by the walk. The masters
    # (pharmacy_par_levels, pharmacy_master_change_requests) are kept, as the
    # stores masters are. ndps_register is a patient-scoped table whose live
    # rows a trigger guards: see _CONDITIONAL_DELETE_PREDICATES.
    "pharmacy_transfers",
    "pharmacy_transfer_lines",
    "pharmacy_lot_events",
    "pharmacy_stock_counts",
    "pharmacy_stock_count_lines",
    "pharmacy_vendor_returns",
    "pharmacy_vendor_return_lines",
    "ndps_variances",
]


# Sentinels for the protection map returned by delete_protected_tables().
_UNPROTECTED = object()      # no guard — plain DELETE
_FULLY_PROTECTED = object()  # unconditional guard — never delete anything

# For tables whose guard is CONDITIONAL, the predicate selecting the rows a
# reseed is still allowed to remove. Anything not listed here is treated as
# fully protected, so an unrecognised conditional guard fails SAFE (keeps the
# data) instead of aborting the run.
_CONDITIONAL_DELETE_PREDICATES = {
    # trg_shadow_no_delete aborts WHEN OLD.lane <> 'simulator'. The simulator
    # lane is disposable demo noise; the 'sweeper' and 'chat' lanes are the
    # pilot's actual shadow-mode evidence and are immutable by design.
    "shadow_events": "lane = 'simulator'",
    # trg_ndps_register_no_delete aborts WHEN OLD.lane <> 'non_production'.
    # ndps_service writes 'non_production' on any profile but prod, so a dev
    # reseed clears the register rows its own testing wrote (the lots they
    # balanced against are deleted by seed_helix), while a live register row,
    # a statutory record, can never be deleted.
    "ndps_register": "lane = 'non_production'",
}


def delete_protected_tables(conn) -> dict:
    """Map every table carrying a DELETE-abort trigger to how it may be wiped.

    Derived from the LIVE schema rather than hand-listed, for the same reason
    `patient_scoped_tables` is: the hand-maintained constant is what goes stale.
    Orb protects its compliance and tamper-evidence tables with
    `RAISE(ABORT, ...)` triggers — audit_logs, shadow_events, consent_records,
    consent_withdrawals, erasure_decisions, erasure_executions,
    breach_notifications — and a reseed that walks the schema will march
    straight into each one in turn.

    Returns {table: _FULLY_PROTECTED | "<sql predicate>"}.
    """
    protection: dict = {}
    for name, tbl_name, sql in conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger'"
    ).fetchall():
        text = (sql or "").replace("\n", " ")
        header = text.upper().split("BEGIN")[0]
        if "RAISE(ABORT" not in text.upper() or "DELETE" not in header:
            continue
        if "WHEN" in header:
            predicate = _CONDITIONAL_DELETE_PREDICATES.get(tbl_name)
            protection[tbl_name] = predicate if predicate else _FULLY_PROTECTED
        else:
            protection[tbl_name] = _FULLY_PROTECTED
    return protection


def patient_scoped_tables(conn) -> list[str]:
    """Every table a reseed must wipe, derived from the LIVE schema.

    Hand-maintaining this list is exactly what broke it: the previous constant
    predated a dozen migrations, so a reseed left verified orphans behind —
    phantom 'active' care_bundles that the sepsis compliance dashboard still
    counts, orphan sage_insights / patient_snapshots / orders / event_log rows
    referencing patients that no longer exist. Walking sqlite_master picks up
    whatever a future migration adds:

      1. every table with a patient_id column;
      2. the transitive closure of tables that reach one of those (or `patients`
         itself) through a foreign key — messages→channels, order_events→orders,
         helix_transitions→helix_patient_med_orders, …
      3. the same closure over UNDECLARED references: a `<parent>_id` column on a
         table that declares no foreign key for it.

    Step 3 exists because step 2 was not enough, and the gap was live. A reseed on
    2026-09-07 left 7 of 7 `invoice_lines` rows pointing at invoices it had just
    deleted, and 56 of 61 `claim_events` rows pointing at deleted claims. Neither
    table has a patient_id, neither declares a single foreign key, and neither was
    in EXTRA_TABLES_TO_CLEAR, so nothing could reach them: exactly the orphan class
    this function was written to end, surviving in the one blind spot a
    declaration-based walk has. `payment_allocations` is the same shape and was
    merely empty on the day, which is why the rule is derived rather than a list of
    the two tables that happened to have rows.

    A DECLARED FOREIGN KEY WINS over the name match, and that is what keeps step 3
    safe. `helix_inventory_lots.medication_id` looks like it names `medications`
    (patient-scoped, wiped) but declares itself against `helix_medication_master`,
    which is not. Honouring the declaration and name-matching only where none
    exists leaves the pharmacy catalogue alone; a bare name match would wipe the
    hospital's stock on every reseed.

    Delete order does not matter here: the wipe runs with foreign_keys=OFF.
    """
    names = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]

    columns: dict[str, list[str]] = {}
    parents: dict[str, set[str]] = {}
    fk_columns: dict[str, set[str]] = {}
    for table in names:
        try:
            columns[table] = [c[1] for c in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
            fks = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        except DBOperationalError:
            # A virtual table whose module is not loaded (sqlite-vec shadow
            # tables) cannot be introspected; it holds no patient rows.
            continue
        parents[table] = {r[2] for r in fks}      # r[2] = referenced table
        fk_columns[table] = {r[3] for r in fks}   # r[3] = the referencing column

    # Undeclared references: `<parent>_id` naming a real table, on a column with no
    # foreign key of its own. Only consulted where nothing is declared, so a table
    # that states its parent is always believed over its column names.
    implied: dict[str, set[str]] = {}
    for table, cols in columns.items():
        declared = fk_columns.get(table, set())
        for col in cols:
            if col == "id" or not col.endswith("_id") or col in declared:
                continue
            stem = col[:-3]
            for candidate in (stem + "s", stem, stem + "es"):
                if candidate in columns and candidate != table:
                    implied.setdefault(table, set()).add(candidate)
                    break

    targets = {t for t, cols in columns.items() if "patient_id" in cols}
    targets |= {t for t in EXTRA_TABLES_TO_CLEAR if t in columns}
    # Bounded rather than a true fixpoint so an accidental FK cycle terminates.
    for _ in range(8):
        grown = {
            t for t in columns
            if (parents.get(t, set()) | implied.get(t, set())) & targets
        } - targets
        if not grown:
            break
        targets |= grown

    return sorted(targets - TABLES_TO_KEEP)


def wipe_patient_tables(conn) -> None:
    """Steps 1 to 3 of a reseed: empty every table the reseed owns, then commit.

    A function of its own so tests can run the real wipe on a scratch database
    and check that each module still agrees with itself after the seed reruns.
    """
    cursor = conn.cursor()

    # 1. Disable FK constraints for clean wipe
    print("  Disabling foreign key constraints...")
    cursor.execute("PRAGMA foreign_keys = OFF")

    # 2. Delete all records from specified tables
    tables_to_clear = patient_scoped_tables(conn)
    protected = delete_protected_tables(conn)
    print(f"  Wiping {len(tables_to_clear)} patient-related tables...")
    for table in tables_to_clear:
        predicate = protected.get(table, _UNPROTECTED)
        if predicate is _FULLY_PROTECTED:
            print(f"    - Table {table} PRESERVED (append-only / tamper-evident)")
            continue
        where = f" WHERE {predicate}" if predicate is not _UNPROTECTED else ""
        try:
            cursor.execute(f'DELETE FROM "{table}"{where}')
            if where:
                print(f"    - Wiped {table} (only{where.lower()}; evidence rows preserved)")
            else:
                print(f"    - Wiped {table}")
        except (DBOperationalError, DBIntegrityError) as e:
            # A missing table, or a protective trigger this script did not
            # anticipate. Skipping is REQUIRED, not merely tidy: this used to
            # catch OperationalError only, while the guards raise
            # IntegrityError — so the very first protected table aborted the
            # whole run and left the database HALF-WIPED, which is a worse
            # state than either finishing or never starting.
            #
            # DBIntegrityError, not sqlite3.IntegrityError: on an encrypted
            # deployment the driver raises sqlcipher3.dbapi2.IntegrityError,
            # which is NOT a subclass of the stdlib one (see db_backend.py).
            # The stdlib spelling would sail straight past the very guard this
            # handler exists to absorb.
            print(f"    - Table {table} skipped: {e}")

    conn.commit()

    # 3. Re-enable FK constraints
    print("  Re-enabling foreign key constraints...")
    cursor.execute("PRAGMA foreign_keys = ON")


# ── Masters the seed did not write ───────────────────────────────────────────
#
# The wipe above reaches patient data and nothing else, so anything an ad hoc
# test or a manual verification run added to a master table survived every
# reseed: three load-test doctors bookable at the front desk, a "Workflow Test
# Patient" portal login, price items typed by hand (one a second CBC, one with
# a stray dash in its name) and a roster import run called noop-verify.csv. The
# website fixtures were recorded off that database and copied all of it. The
# seed manifest is the authority for what a seeded box holds, so these three
# tables are cut back to it.

def seeded_user_ids() -> frozenset:
    """Logins a reseed keeps: the seeded staff and portal accounts, plus the
    service identities the server itself recreates (Sage's audit identity and
    the Relay bots)."""
    from database import SERVICE_ACCOUNT_IDS
    from scripts.seed_clinical_data import USERS

    return frozenset(row[0] for row in USERS) | SERVICE_ACCOUNT_IDS


def seeded_service_codes() -> frozenset:
    """Price-list codes a reseed keeps: the module seed's items and the code the
    front desk registers for an OPD consultation."""
    from scripts.seed_modules import SEEDED_SERVICE_CODES
    from services.frontdesk_service import OPD_CONSULTATION_SERVICE_CODE

    return SEEDED_SERVICE_CODES | {OPD_CONSULTATION_SERVICE_CODE}


def sweep_unseeded_masters(conn) -> dict:
    """Step 3b of a reseed: delete users, service items (with their tariff rows)
    and roster import runs that the seed manifest does not name.

    Dev profile only. This deletes staff logins, and config.py resolves an unset
    profile to 'prod', so a box that has not declared itself dev keeps every
    account. Runs with foreign keys off, like the wipe: the rows that pointed at
    a removed login (admissions, notes, messages) are patient data the wipe has
    already emptied. Returns {table: rows removed}.
    """
    import config

    profile = getattr(config, "DEPLOYMENT_PROFILE", "prod")
    if profile != "dev":
        print(f"  Skipped: the deployment profile is {profile!r}, not 'dev', "
              "and this step deletes staff logins.")
        return {}

    keep_users = sorted(seeded_user_ids())
    keep_codes = sorted(seeded_service_codes())
    users_in = ",".join("?" for _ in keep_users)
    codes_in = ",".join("?" for _ in keep_codes)
    protected = delete_protected_tables(conn)
    steps = (
        ("users", f"DELETE FROM users WHERE id NOT IN ({users_in})", keep_users),
        ("tariff_rates", f"DELETE FROM tariff_rates WHERE service_code NOT IN ({codes_in})", keep_codes),
        ("service_items", f"DELETE FROM service_items WHERE code NOT IN ({codes_in})", keep_codes),
        ("roster_import_runs", "DELETE FROM roster_import_runs", []),
    )

    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = OFF")
    removed: dict = {}
    for table, sql, params in steps:
        if table in protected:
            print(f"    - {table} PRESERVED (append-only / tamper-evident)")
            continue
        try:
            removed[table] = cursor.execute(sql, tuple(params)).rowcount
            print(f"    - {table}: removed {removed[table]} row(s) the seed did not write")
        except (DBOperationalError, DBIntegrityError) as e:
            # A table an older schema does not have yet: nothing to sweep.
            print(f"    - {table} skipped: {e}")
    conn.commit()
    cursor.execute("PRAGMA foreign_keys = ON")
    return removed


def reset_and_seed():
    print(f"═══ Connecting to DB: {DB_PATH} ═══")
    # _build_connection() (not raw sqlite3.connect) so the script also works
    # against a SQLCipher-encrypted DB: it applies PRAGMA key before anything else.
    conn = _build_connection()
    wipe_patient_tables(conn)
    print("  Sweeping logins, price items and import runs the seed did not write...")
    sweep_unseeded_masters(conn)
    conn.close()
    print("═══ Wipe Complete! ═══\n")
    
    # 4. Invoke the official clinical seeder
    print("═══ Running Clinical Data Seeder ═══")
    seed()
    print("═══ Seeding Complete! ═══\n")
    
    # 4.5. Seed Helix Pharmacy Catalog & Inventory
    print("═══ Seeding Helix Pharmacy Catalog ═══")
    seed_helix()
    print("═══ Helix Seeding Complete! ═══\n")

    # 4.6. The pharmacy store (grn-18): its dispensing pharmacy adopts the lots
    #      seed_helix just opened, then one licensed vendor and one received
    #      order. After seed_helix (which replaces every lot) and before
    #      seed_pharmacy_queue (whose dispenses then come off the adopted
    #      shelf). Only on a dev box with ORB_PHARMACY_STORE on, and silent
    #      otherwise, so a reseed without the module writes and prints exactly
    #      what it did before.
    from scripts.seed_pharmacy_store import seed_pharmacy_store, should_seed
    if should_seed():
        print("═══ Seeding the pharmacy store ═══")
        try:
            seed_pharmacy_store()
        except Exception as exc:  # a dev seed; the rest of the reseed still runs
            print(f"  x Pharmacy store not seeded: {exc}")
        print("═══ Pharmacy store seeding complete ═══\n")
    
    # 5. Randomize admission dates across a ROLLING 3-day window ending now.
    #    (The banner used to read "May 21-23, 2026" — a leftover from when the
    #    window was an absolute date. The function itself is relative; the stale
    #    banner just made a correct reseed look like it had re-aged the ward.)
    print("═══ Randomizing Admission Dates (rolling: last 3 days) ═══")
    randomize_dates()
    print("═══ Date Randomization Complete! ═══")

    # 5b. Clip the seeded vitals at the admission that step 5 just moved. The
    #     seeder's 24h trend ends at its own clock, so a patient now admitted six
    #     hours ago would otherwise carry readings from before they arrived (see
    #     the vitals-window note above clip_vitals_at_admission).
    print("═══ Clipping vitals at the admission ═══")
    clip_vitals_at_admission()

    # 6. Link the demo patient login (P001) to a real admitted patient so the
    #    Bridge patient portal resolves (_resolve_patient_id reads patients.user_id).
    print("═══ Linking patient login P001 → admitted patient ═══")
    link_patient_login()

    # 7. Backfill labs, progress notes and surgeries so Vigil's Lab/Diagnostic/Notes
    #    panels and the Surgical Suite are populated on a clean seed (instead of waiting
    #    on the ~0.2%-per-tick simulator bot, which leaves them empty for the whole demo).
    #    Each chart gets only the part of the schedule that has happened by now: the
    #    admissions above are spread over three days, and a document dated after the
    #    wall clock is a bug the Patient Story shows first (see plan_clinical_documents).
    print("═══ Backfilling labs / progress notes / surgeries ═══")
    seed_clinical_history()

    # 7b. Allergies. Without these `patient_allergies` is EMPTY on a fresh seed,
    #     so the allergy interlock has nothing to fire on anywhere in a
    #     walkthrough — every order and every order-set preview clears, and a
    #     gate that never fires looks like a gate that does not exist.
    print("═══ Seeding patient allergies (the interlock needs something to catch) ═══")
    seed_allergies()

    # 7c. Pharmacy work. After the allergies, because every order below goes
    #     through the drug-safety screen and must be screened against them.
    print("═══ Seeding pharmacy orders and the AI medication verify queue ═══")
    seed_pharmacy_queue()

    # 8. Re-seal the audit hash chain. Seeding writes audit rows through several
    #    code paths, which can leave the demo DB's tamper-evidence chain broken at
    #    the root; a freshly-seeded demo database must verify clean.
    print("═══ Re-sealing audit hash chain ═══")
    import subprocess
    subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), "reseal_audit_chain.py"), "--force"],
        check=False,
    )


# ── Clinical history backfill ────────────────────────────────────────────────
#
# THE CHART IS ANCHORED TO THE ADMISSION AND CLIPPED AT THE CLOCK.
#
# randomize_dates() spreads admissions uniformly over the last three days, so a
# seeded patient has been on the ward for anything from a few seconds to 72
# hours. The documents below follow a fixed schedule relative to that admission
# (admission note and admission labs in the first hours, then morning labs and
# a rounds note on every hospital day after the first). The seed used to file
# the whole schedule for everyone regardless: on a ward seeded on 4 Sept the
# Patient Story opened on a "Progress Note: Day 2" dated 6 Sept, and 48 of the
# 150 documents on the ward carried timestamps ahead of the wall clock.
#
# The two obvious shortcuts are both wrong. Anchoring the documents at "now
# minus three days" files them BEFORE the admission event the Story timeline
# leads with, so the chart reads as a previous visit. Backdating the admissions
# themselves collapses the three-day spread of the ward that the round sheet,
# the bundle clocks and the surgical schedule all assume. So the anchor stays
# the admission, and each patient gets the part of the schedule that has
# already happened: a patient admitted ten hours ago has an admission note and
# the admission labs, and the first rounds note appears once 08:00 of their
# second hospital day has passed. That is what a real chart looks like. The
# admission note is what keeps the Notes panel populated for the fresh
# admissions that do not yet have a rounds note.
#
# HOSPITAL DAYS ARE CALENDAR DAYS, AND THE ADMISSION DAY IS DAY 1.
#
# That is the convention the product displays: the round sheet's
# admissionDay() (src/components/round/roundSummary.ts) counts local calendar
# days from the admission and adds one. The rounds notes used to be filed at
# fixed offsets, +32h as "Day 1" and +56h as "Day 2", which counted elapsed
# 24-hour blocks from a Day 0: the sheet always read one day ahead of the
# newest note, and a patient admitted late in the evening had no note on their
# first morning. Now each rounds note is filed at ROUNDS_AT on the morning of
# hospital day N, after that morning's labs at MORNING_LABS_AT, and titled
# "Progress Note: Day N" with N counted the way the sheet counts it. Calendar
# days are taken in the ward's own zone: the sheet uses the browser's local
# day, and on an appliance the server and its browsers share the hospital's
# zone, so the server's local zone is the default (hl7v2.py makes the same
# choice for offset-less monitor timestamps). The planner takes an explicit
# zone so tests do not depend on the machine they run on.
#
# The random draws for every lab in the schedule are made even for documents
# that are then dropped, so the values on a chart depend only on the ward
# position (random.seed(42) in seed_clinical_history), never on the clock at
# seed time.

from datetime import datetime as _datetime, time as _time, timedelta as _timedelta, timezone as _timezone

#: Hospital days that can hold a rounds note inside the 72h seed window. A stay
#: admitted late on day 1 reaches the morning of day 4 within 72 hours.
ROUNDS_DAYS = (2, 3, 4)
MORNING_LABS_AT = _time(6, 0)
ROUNDS_AT = _time(8, 0)


def ward_timezone():
    """The zone hospital days are counted in: the server's own, as the browsers' is."""
    return _datetime.now().astimezone().tzinfo or _timezone.utc


def hospital_day(ts, admit, tz):
    """Day N of the stay that `ts` falls on: calendar days of `tz`, admission day = 1.

    The round sheet's admissionDay() rule, so a "Progress Note: Day N" title and
    the sheet's "Day N" badge agree on the morning the note is written.
    """
    return (ts.astimezone(tz).date() - admit.astimezone(tz).date()).days + 1


def _lab_doc(name, panel, rows):
    lines = [f"🔬 LAB RESULT: {panel}", f"Patient: {name}", ""]
    for n, (val, unit, flag, ref) in rows.items():
        lines.append(f"  {n}: {val} {unit}  {flag}  (ref: {ref})")
    return "\n".join(lines)


def _cbc(rng):
    wbc = round(rng.uniform(4, 19), 1); hgb = round(rng.uniform(7.5, 15.5), 1); plt = rng.randint(80, 420)
    return {"WBC": (wbc, "10³/µL", "HIGH" if wbc > 11 else "", "4.0-11.0"),
            "Hemoglobin": (hgb, "g/dL", "LOW" if hgb < 12 else "", "12.0-16.0"),
            "Platelets": (plt, "10³/µL", "LOW" if plt < 150 else "", "150-400")}


def _bmp(rng):
    na = rng.randint(130, 146); k = round(rng.uniform(3.1, 5.4), 1); cr = round(rng.uniform(0.6, 2.6), 1)
    glu = rng.randint(78, 290); lac = round(rng.uniform(0.7, 4.8), 1)
    return {"Sodium": (na, "mEq/L", "LOW" if na < 135 else ("HIGH" if na > 145 else ""), "135-145"),
            "Potassium": (k, "mEq/L", "LOW" if k < 3.5 else ("HIGH" if k > 5.1 else ""), "3.5-5.1"),
            "Creatinine": (cr, "mg/dL", "HIGH" if cr > 1.3 else "", "0.6-1.3"),
            "Glucose": (glu, "mg/dL", "HIGH" if glu > 140 else "", "70-110"),
            "Lactate": (lac, "mmol/L", "HIGH" if lac > 2 else "", "0.5-2.0")}


def plan_clinical_documents(admit, now, name, dx, rng, tz=None):
    """The documents a seeded chart holds at `now`, oldest first.

    `admit` and `now` are aware datetimes. Returns
    ``[(doc_type, title, content, created_at), ...]`` with every ``created_at``
    an aware UTC datetime in ``[admit, now)``: nothing before the admission,
    nothing ahead of the clock. See the note above for why the schedule is
    anchored to the admission and clipped rather than shifted, and for the
    hospital-day convention. `rng` is anything with ``uniform`` and ``randint``
    (the ``random`` module, or a ``random.Random``). `tz` is the zone hospital
    days are counted in; the ward's own by default.
    """
    tz = tz or ward_timezone()
    dx = dx or "admitted patient"
    day1 = admit.astimezone(tz).date()

    def on_day(n, at):
        """The instant `at` (a wall-clock time) on hospital day `n`, as UTC."""
        return _datetime.combine(day1 + _timedelta(days=n - 1), at, tzinfo=tz).astimezone(_timezone.utc)

    # Chronological by construction (the admission documents all fall within
    # five hours, before day 2's 06:00 labs), and sorted below regardless. The
    # lab generators are called in this fixed order for every patient, dropped
    # documents included, so the draw sequence is stable.
    schedule = [
        (admit + _timedelta(hours=1), "note", "Admission Note",
         f"S: Admitted with {dx}. O: Initial vitals per admission record; examination consistent with {dx}. "
         f"A: {dx}. P: Admit for monitoring and treatment; admission labs sent; start management per protocol."),
        (admit + _timedelta(hours=2), "lab_result", "Complete Blood Count",
         _lab_doc(name, "Complete Blood Count", _cbc(rng))),
        (admit + _timedelta(hours=5), "lab_result", "Basic Metabolic Panel",
         _lab_doc(name, "Basic Metabolic Panel", _bmp(rng))),
    ]
    for n in ROUNDS_DAYS:
        schedule.append((on_day(n, MORNING_LABS_AT), "lab_result", "Basic Metabolic Panel",
                         _lab_doc(name, "Basic Metabolic Panel", _bmp(rng))))
        if n == ROUNDS_DAYS[0]:
            text = (f"S: Reviewed on morning rounds. O: {dx}; vitals per chart, morning labs reviewed. "
                    f"A: {dx}, responding to current management. P: Continue therapy, monitor labs/vitals, reassess AM.")
        else:
            text = (f"S: Overnight stable, no acute events. O: Exam unchanged; morning labs reviewed. "
                    f"A: {dx}. P: Continue plan; consider step-down if trend continues.")
        schedule.append((on_day(n, ROUNDS_AT), "note", f"Progress Note: Day {n}", text))
    schedule.sort(key=lambda entry: entry[0])
    return [(doc_type, title, content, ts.astimezone(_timezone.utc))
            for ts, doc_type, title, content in schedule
            if ts < now]


# ── Surgical cases ───────────────────────────────────────────────────────────
#
# A seeded operation belongs to a patient whose own diagnosis calls for it, and
# to that patient's own surgeon. The backfill used to hand five canned
# procedures to whichever five admitted patients came back first, with the
# surgeon picked round-robin from every account whose role is doctor: a
# cholecystectomy for diabetic ketoacidosis, an ORIF femur for dengue, cases
# assigned to the Vigil and Lab bots and to load-test logins. The Surgical Suite
# showed all of it. Keyed by the seeded patient's name, so a ward without these
# patients (a test fixture) simply gets no cases.
#
#: patient name -> (procedure, estimated minutes, theatre)
SURGICAL_CASES = {
    "Arun Krishnan": ("Relook laparotomy", 150, "OR-1"),
    "Geeta Sharma": ("Roux-en-Y hepaticojejunostomy (bile duct repair)", 240, "OR-2"),
    "Ajay Tiwari": ("Intramedullary nailing, femur and tibia", 210, "OR-3"),
    "Siddharth Nair": ("Debridement and external fixation, tibia", 120, "OR-3"),
}


def plan_surgical_cases(pts):
    """[(patient row, procedure, minutes, theatre), ...] for the admitted
    patients SURGICAL_CASES names, in its order. A patient with no attending
    doctor on the admission gets no case: an operation with nobody to do it is
    not a case."""
    by_name = {p['name']: p for p in pts}
    return [
        (by_name[name], proc, minutes, theatre)
        for name, (proc, minutes, theatre) in SURGICAL_CASES.items()
        if name in by_name and by_name[name]['attending_doctor_id']
    ]


def seed_clinical_history():
    import json, random
    from datetime import datetime, timedelta, timezone
    random.seed(42)
    conn = _build_connection()  # sets row_factory + applies the SQLCipher key
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    pts = cur.execute(
        "SELECT p.id, p.name, p.diagnosis, a.attending_doctor_id, a.created_at "
        "FROM patients p JOIN admissions a ON a.patient_id = p.id WHERE p.status='admitted'"
    ).fetchall()
    docs = [r['id'] for r in cur.execute("SELECT id FROM users WHERE role='doctor'").fetchall()] or ['DOC001']

    nlabs = nnotes = 0
    for p in pts:
        # The anchor is admissions.created_at, which randomize_dates() has
        # already spread over the last three days. It arrives in either
        # timestamp dialect (SQLite's bare "YYYY-MM-DD HH:MM:SS" is UTC by
        # definition). A row without a readable one gets a two-day-old anchor
        # rather than an empty chart.
        try:
            admit = datetime.fromisoformat((p['created_at'] or '').replace('Z', '+00:00'))
        except ValueError:
            admit = now - timedelta(days=2)
        if admit.tzinfo is None:
            admit = admit.replace(tzinfo=timezone.utc)
        author = p['attending_doctor_id'] if p['attending_doctor_id'] in docs else docs[0]
        for doc_type, title, content, ts in plan_clinical_documents(admit, now, p['name'], p['diagnosis'], random):
            cur.execute("INSERT INTO clinical_documents (patient_id, doc_type, title, content, author_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (p['id'], doc_type, title, content, author, ts.isoformat()))
            if doc_type == 'lab_result':
                nlabs += 1
            else:
                nnotes += 1

    nsurg = 0
    for i, (p, proc, dur, orr) in enumerate(plan_surgical_cases(pts)):
        sched = (now + timedelta(days=i % 5)).date().isoformat()
        sid = f"SURG-{p['id'][-6:]}-{i}"
        profile = cur.execute(
            "SELECT comorbidities FROM patient_profiles WHERE patient_id = ?", (p['id'],)
        ).fetchone()
        cur.execute(
            "INSERT OR IGNORE INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis, scheduled_date, scheduled_time, estimated_duration_min, operating_room, status, co_morbidities, success_probability, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)",
            (sid, p['id'], p['attending_doctor_id'], proc, p['diagnosis'], sched, f"{8 + i}:00", dur, orr,
             (profile[0] if profile and profile[0] else "[]"), round(random.uniform(0.82, 0.97), 2),
             f"Pre-op workup complete for {proc}.")); nsurg += 1

    conn.commit(); conn.close()
    print(f"  Backfilled {nlabs} labs, {nnotes} notes, {nsurg} surgeries across {len(pts)} patients")


def link_patient_login():
    conn = _build_connection()
    cur = conn.cursor()
    row = (
        cur.execute(
            "SELECT p.id FROM patients p JOIN admissions a ON a.patient_id = p.id ORDER BY p.id LIMIT 1"
        ).fetchone()
        or cur.execute("SELECT id FROM patients ORDER BY id LIMIT 1").fetchone()
    )
    if row:
        cur.execute("UPDATE patients SET user_id = ? WHERE id = ?", ("P001", row[0]))
        # KEEP THE ACCOUNT AND THE RECORD IN AGREEMENT.
        # This linked P001 to "whichever admitted patient sorts first" while the
        # P001 user row kept its own hard-coded name. The Bridge then greeted
        # one person in the header and another in the body — on a patient-facing
        # portal that reads as "the patient is looking at someone else's chart",
        # which is the first thing anyone opening Bridge saw. The PATIENT RECORD
        # is the authority (every vital, medication and care-team line comes
        # from it), so the account is renamed to match it, not the reverse.
        prow = cur.execute(
            "SELECT name FROM patients WHERE id = ?", (row[0],)
        ).fetchone()
        if prow and prow[0]:
            cur.execute("UPDATE users SET name = ? WHERE id = ?", (prow[0], "P001"))
            print(f"  Linked patient login P001 → patient {row[0]} ({prow[0]}), account renamed to match")
        else:
            print(f"  Linked patient login P001 → patient {row[0]}")
    conn.commit()
    conn.close()


def randomize_dates():
    import random
    from datetime import datetime, timedelta, timezone
    
    conn = _build_connection()
    cursor = conn.cursor()

    # Every patient, with the clock its record was stamped at
    cursor.execute("SELECT id, created_at FROM patients")
    patients = cursor.fetchall()
    
    # Admission dates are anchored RELATIVE TO NOW, not to a fixed calendar date.
    #
    # This used to be `datetime(2026, 5, 21, ...)`, an absolute window. Every day
    # that passed after a seed pushed the whole ward further into the past, and
    # because a dozen screens derive elapsed time from these rows, the entire
    # demo aged badly in one direction: "Day 79 of admission" on the round sheet,
    # "10d 8h" on a Sepsis Six clock with a 60-minute target, "250h late" on the
    # med round, and a surgical schedule with nothing in its next-7-days window.
    # None of those were display bugs — the arithmetic was right and the data was
    # eleven weeks old. Re-seeding did not help either, because the anchor was
    # absolute: a reseed today reproduced the same 80-day-old ward.
    #
    # A rolling 3-day window means a reseed always yields a ward admitted over
    # the last three days, which is what every one of those screens assumes.
    #
    # "Now" is the ward's own clock, patients.created_at, not the wall clock at
    # the moment this loop runs. The clinical seeder stamps ONE instant on the
    # patient row, the admission row and the patient's current vitals reading,
    # and the window ends one second before it. Ending it at the wall clock
    # instead let an admission land a few seconds after the reading the seeder
    # had already filed as current, which is the one pre-admission reading that
    # clip_vitals_at_admission() must never be asked to drop. A row whose
    # created_at cannot be read falls back to the wall clock as before.
    for row in patients:
        pid = row[0]
        clock = _parse_aware(row[1]) or datetime.now(timezone.utc)
        window_start = clock - timedelta(days=3)
        random_seconds = random.randint(0, 3 * 24 * 3600 - 1)
        random_dt = (window_start + timedelta(seconds=random_seconds)).isoformat()
        
        # Update patients
        cursor.execute("UPDATE patients SET admission_date = ? WHERE id = ?", (random_dt, pid))
        
        # Update admissions
        cursor.execute("UPDATE admissions SET created_at = ? WHERE patient_id = ?", (random_dt, pid))
        
        # Update channels
        cursor.execute("UPDATE channels SET created_at = ? WHERE patient_id = ?", (random_dt, pid))
        
        # Update sage_room_memory
        cursor.execute("UPDATE sage_room_memory SET created_at = ?, updated_at = ? WHERE patient_id = ?", (random_dt, random_dt, pid))
        
    conn.commit()
    conn.close()

def _parse_aware(raw):
    """An aware UTC datetime from either timestamp dialect in this database, or None.

    Rows arrive as the application's isoformat ("2026-09-04T19:08:51.876348+00:00")
    or as SQLite's bare CURRENT_TIMESTAMP ("2026-09-04 19:08:51", UTC by
    definition). Comparing the two as strings is wrong: a space sorts before a
    "T", so a bare stamp reads as EARLIER than an ISO one for the same instant.
    """
    from datetime import datetime, timezone
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Vitals window ────────────────────────────────────────────────────────────
#
# THE TREND IS ANCHORED TO THE CLOCK AND CLIPPED AT THE ADMISSION.
#
# The clinical seeder gives every patient a current reading plus 24 hourly
# readings for the day before it, and randomize_dates() then moves the
# admission back by up to 72 hours. The readings used to stay where they were,
# so a patient admitted six hours ago carried a trend that began 18 hours
# before they arrived: on the 2026-09-05 reseed, 130 of the 750 seeded readings
# preceded their admission. The Story does not list vitals, so it never showed
# there; the Vigil trend does.
#
# This is the mirror image of the documents rule above. A chart's documents
# follow the admission, so their schedule is anchored there and clipped at the
# clock. A trend shows what the ward is watching NOW, so it stays anchored at
# the clock and loses whatever falls before the admission. Shifting it to end
# at the admission instead would leave a three-day-old admission with no
# reading in the last two days, which reads as a monitoring gap and starves
# NEWS2 and the risk score, both of which take the newest row. The readings
# that survive are untouched, so the values a chart shows depend on the ward
# position the seeder drew, never on the clock. The current reading always
# survives because randomize_dates() draws the admission strictly before the
# clock that reading is stamped with.

def clip_vitals_at_admission():
    """Drop every reading timestamped before its patient's admission.

    Runs on a freshly wiped ward, so every reading it sees is the seeder's.
    A chart whose admission stamp cannot be read is left alone rather than
    clipped against a guess.
    """
    conn = _build_connection()  # sets row_factory + applies the SQLCipher key
    cur = conn.cursor()
    pts = cur.execute(
        "SELECT p.id, a.created_at FROM patients p JOIN admissions a ON a.patient_id = p.id "
        "WHERE p.status = 'admitted'"
    ).fetchall()
    dropped = touched = 0
    for p in pts:
        admit = _parse_aware(p["created_at"])
        if admit is None:
            continue
        doomed = [
            r["id"] for r in cur.execute(
                "SELECT id, timestamp FROM vitals WHERE patient_id = ?", (p["id"],)
            ).fetchall()
            if (ts := _parse_aware(r["timestamp"])) is not None and ts < admit
        ]
        if doomed:
            cur.executemany("DELETE FROM vitals WHERE id = ?", [(i,) for i in doomed])
            dropped += len(doomed)
            touched += 1
    conn.commit()
    conn.close()
    print(f"  Dropped {dropped} readings that preceded their admission on {touched} of {len(pts)} charts")


def seed_allergies():
    """Give the allergy interlock something to actually interlock.

    `patient_allergies` was seeded EMPTY — zero rows for every admitted patient
    — so the allergy interlock, the dose guard's companion and one of the two
    hardest-won safety gates in the product, had nothing to fire on anywhere in
    a walkthrough. Every order and every order-set preview cleared, which reads
    as "the gate does nothing" rather than "no patient here has an allergy".

    The three below are chosen to be clinically coherent with each patient's own
    diagnosis, and the first one is deliberately load-bearing: a cephalosporin
    anaphylaxis on the patient whose diagnosis is early sepsis means applying
    the Sepsis Six order set to him is STOPPED by the interlock (the set
    contains ceftriaxone) — the gate refusing a protocol on a real patient,
    which is the thing worth showing.

    Idempotent: matched on (patient, substance), so re-running adds nothing.
    """
    conn = _build_connection()
    cur = conn.cursor()

    wanted = [
        # (name fragment, substance, class, reaction, severity)
        ("Suresh Reddy", "Ceftriaxone", "cephalosporins",
         "Anaphylaxis: urticaria, angio-oedema and hypotension after IV ceftriaxone in 2023",
         "severe"),
        ("Ananya Kapoor", "Penicillin", "penicillins",
         "Widespread maculopapular rash 48h into a course of amoxicillin", "moderate"),
        ("Priya Nair", "Ibuprofen", "nsaids",
         "Bronchospasm and wheeze", "moderate"),
    ]

    added = 0
    for name_fragment, substance, cls, reaction, severity in wanted:
        row = cur.execute(
            "SELECT id FROM patients WHERE name LIKE ? AND status='admitted' LIMIT 1",
            (f"%{name_fragment}%",),
        ).fetchone()
        if not row:
            continue
        pid = row[0] if not isinstance(row, dict) else row["id"]
        exists = cur.execute(
            "SELECT 1 FROM patient_allergies WHERE patient_id = ? "
            "AND LOWER(substance) = LOWER(?)",
            (pid, substance),
        ).fetchone()
        if exists:
            continue
        cur.execute(
            "INSERT INTO patient_allergies "
            "(patient_id, substance, substance_class, reaction, severity, status, "
            " source, recorded_by) "
            "VALUES (?, ?, ?, ?, ?, 'active', 'seed', 'DOC001')",
            (pid, substance, cls, reaction, severity),
        )
        added += 1

    conn.commit()
    conn.close()
    print(f"  Seeded {added} patient allergy record(s)")



# ── Pharmacy work ────────────────────────────────────────────────────────────
#
# A fresh seed left the Helix order board and the pharmacist's Verify Queue
# empty, so neither screen could show what it is for. Two medications arrive the
# way an agentic Sage order does (screened, charted as unverified AI entries with
# no invented dose, and sent to Helix as pending orders); three more are ward
# orders a pharmacist has already started on, one at each later step. Every
# order passes the same non-interactive drug-safety gate the agentic path uses,
# and one the gate stops is skipped rather than forced.

#: (patient name, drug, why) for the two AI medications awaiting verification.
AI_MEDICATIONS = (
    ("Vikram Singh", "Pantoprazole", "GI protection while on dual antiplatelet therapy"),
    ("Rajesh Iyer", "Paracetamol", "Low-grade fever overnight; paracetamol if needed"),
)

#: (patient name, drug, dose, route, frequency, prescriber, steps past pending)
WARD_HELIX_ORDERS = (
    ("Kavitha Menon", "Potassium Chloride", "20 mEq", "IV", "Once", "DOC002",
     ("reviewing",)),
    ("Ananya Kapoor", "Insulin Aspart", "4 units", "SC", "Before meals", "DOC001",
     ("reviewing", "verified")),
    ("Lakshmi Devi", "Monocef", "1 g", "IV", "Q24H", "DOC001",
     ("reviewing", "verified", "scheduled", "dispensed")),
)

PHARMACIST = "PHA001"


def seed_pharmacy_queue():
    """Seed the Helix order board across statuses and two unverified AI meds.

    Runs on a freshly wiped ward. A patient who already has a Helix order for the
    drug is skipped, so a second run adds nothing.
    """
    from database import db_session
    from services import helix_service
    from services.clinical_time import utc_iso_now
    from services.medication_safety_gate import MedicationSafetyBlocked, enforce_medication_safety
    from services.sage_handoff_service import HandoffBlocked, _dispatch_to_helix

    with db_session() as db:
        ids = {r["name"]: r["id"] for r in db.execute(
            "SELECT id, name FROM patients WHERE status = 'admitted'").fetchall()}

    def already_ordered(pid, drug):
        with db_session() as db:
            return db.execute(
                "SELECT 1 FROM helix_patient_med_orders o JOIN helix_medication_master m "
                "ON m.id = o.medication_id WHERE o.patient_id = ? "
                "AND (m.name LIKE ? OR m.generic_name LIKE ?)",
                (pid, f"%{drug}%", f"%{drug}%"),
            ).fetchone() is not None

    ai = orders = 0
    for name, drug, why in AI_MEDICATIONS:
        pid = ids.get(name)
        if not pid or already_ordered(pid, drug):
            continue
        try:
            _dispatch_to_helix({"patient_id": pid, "target": drug, "rationale": why}, "DOC001")
            ai += 1
        except HandoffBlocked as exc:
            print(f"    - {drug} for {name} not seeded: {exc}")

    for name, drug, dose, route, freq, prescriber, steps in WARD_HELIX_ORDERS:
        pid = ids.get(name)
        if not pid or already_ordered(pid, drug):
            continue
        try:
            enforce_medication_safety(
                pid, drug, actor_id=prescriber, entry_point="seed.pharmacy_queue",
                interactive=False,
            )
            order = helix_service.create_order(pid, drug, dose, route, freq, prescriber)
            for step in steps:
                helix_service.transition_order_status(
                    order["id"], step, PHARMACIST,
                    idempotency_key=f"seed-{order['id']}-{step}",
                    scheduled_at=(utc_iso_now() if step == "scheduled" else None),
                )
            orders += 1
        except (MedicationSafetyBlocked, ValueError) as exc:
            print(f"    - {drug} for {name} not seeded: {exc}")

    print(f"  Seeded {ai} AI medication(s) awaiting verification and {orders} ward Helix order(s)")


if __name__ == "__main__":
    reset_and_seed()
