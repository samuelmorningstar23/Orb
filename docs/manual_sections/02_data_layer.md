## 2. The Data Layer (SQLite Schema & Access)

Orb stores all patient data in a single SQLite database file (`orb.db`, path from `config.DB_PATH`). SQLite is a file-based relational database, there is no separate database server process to start or manage. Every table, index, trigger, and migration lives in two files:

- `server/database.py`, the core Orb schema (patients, vitals, messaging, audit, etc.)
- `server/services/helix_db.py`, the pharmacy (Helix) sub-schema, which extends the same database with medication-specific tables

This section documents every table, every index, every trigger, and all the connection helpers in plain language.

---

### Plain-English Glossary of DB Terms

Before diving in, here is what the jargon means:

| Term | Plain meaning |
|------|--------------|
| **Primary key** | The unique ID for a row. No two rows in the same table can share it. |
| **Foreign key** | A column that holds the primary key of a row in *another* table. It creates a link between them. Example: `patient_id REFERENCES patients(id)` means "this row belongs to that patient." |
| **Index** | A lookup shortcut the database builds automatically so queries on that column are fast without scanning every row. |
| **WAL mode** | Write-Ahead Logging. SQLite's concurrency mode where writes go to a small log file first, letting readers proceed without being blocked. Critical for a busy web server with many concurrent requests. |
| **Trigger** | Automatic code the database runs when you try to INSERT, UPDATE, or DELETE a row. Used here to block mutations on the audit table. |
| **Transaction** | A group of SQL statements that all succeed or all fail together. If something goes wrong mid-way, everything rolls back. |
| **Vector embedding** | A list of numbers that represents the *meaning* of text. Two pieces of similar text will have vectors that are numerically close. Used for the RAG (Retrieval-Augmented Generation) search in Sage. |
| **MRN** | Medical Record Number. A hospital-assigned unique identifier for a patient, distinct from their database `id`. |
| **SOAP note** | A structured clinical note format: Subjective, Objective, Assessment, Plan. |
| **WAL/SHM sidecars** | The `-wal` and `-shm` files SQLite creates alongside the main `.db` file in WAL mode. They also contain PHI and must be protected. |
| **MEWS** | Modified Early Warning Score. A numeric score computed from vitals that indicates how urgently a patient needs clinical attention. |

---

### 2.1 `server/database.py`: Core Schema

This file contains:
1. `SCHEMA`, a multiline string holding all `CREATE TABLE` and `CREATE INDEX` and `CREATE TRIGGER` SQL statements.
2. `get_db()`, opens a raw connection with performance PRAGMAs applied.
3. `db_session()`, a context manager wrapping `get_db()` with auto-commit/rollback.
4. `_harden_db_file_permissions()`, restricts the `.db`, `-wal`, and `-shm` files to owner-only access (0o600).
5. `init_db()`, runs `SCHEMA` on startup and applies all additive migrations.
6. `verify_audit_chain()`, walks the audit log and verifies the cryptographic hash chain.

---

### 2.2 Connection Helpers

#### `get_db() -> sqlite3.Connection`

Opens a connection to `orb.db` and immediately applies a set of PRAGMAs (SQLite configuration settings) before returning the connection.

```python
conn = sqlite3.connect(str(DB_PATH), timeout=30)
conn.row_factory = sqlite3.Row
```

**`row_factory = sqlite3.Row`**: This makes query results behave like dictionaries so you can access columns by name (`row["patient_id"]`) rather than by index (`row[0]`). Every service in Orb depends on this.

**PRAGMAs applied and why:**

| PRAGMA | Value | Purpose |
|--------|-------|---------|
| `journal_mode=WAL` | WAL | Enables Write-Ahead Logging so multiple readers can proceed while a write is in progress. Essential for a FastAPI server with concurrent requests. |
| `foreign_keys=ON` | ON | Enforces the `REFERENCES` constraints in the schema. Without this, SQLite silently ignores foreign key violations. |
| `busy_timeout=30000` | 30 seconds | If the database is locked (another writer), wait up to 30 seconds before returning an error instead of failing immediately. |
| `page_size = 16384` | 16 KB | Larger page size improves performance for the BLOBs and JSON text stored in clinical fields. (Only effective before the database is first created.) |
| `synchronous = NORMAL` | NORMAL | A balance between durability and speed. The database won't be corrupted by a crash, but a power loss mid-write could lose the last transaction. |
| `mmap_size = 30000000000` | ~28 GB | Tells SQLite it can memory-map up to 28 GB of the database for faster reads on machines with large RAM. |
| `cache_size = -256000` | 256 MB | In-memory page cache size. Keeps hot pages in RAM to avoid repeated disk reads. |
| `temp_store = MEMORY` | MEMORY | Temporary tables and sort results go in RAM instead of a temp file. |

**⚠️ If you change this:** Do not remove `foreign_keys=ON` or `journal_mode=WAL`. Removing foreign keys will silently allow orphan rows. Removing WAL mode will make the server slower and increase lock contention under load. Every new connection from every service calls `get_db()`, so changes here affect the whole system.

---

#### `db_session()`: Context Manager

```python
@contextmanager
def db_session():
    conn = get_db()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()
```

**How to use it:**
```python
with db_session() as conn:
    conn.execute("INSERT INTO ...")
    # If this block exits normally, conn.commit() is called automatically.
    # If an exception is raised, conn.rollback() undoes all changes in this block.
```

This is the standard way every Orb service accesses the database. The `yield conn` hands the open connection to the `with` block. When the block exits cleanly, the changes are committed; if anything raises an exception, all changes in that session are rolled back, so the database never ends up in a half-written state.

**⚠️ If you change this:** Do not add `conn.execute("PRAGMA foreign_keys=ON")` here separately, it is already in `get_db()`. Do not call `conn.commit()` manually inside a `db_session()` block unless you are dealing with the specific migration pattern that needs mid-migration commits (see Helix migration helpers).

---

#### `_harden_db_file_permissions()`

Calls `os.chmod(path, 0o600)` on the `.db`, `-wal`, `-shm`, and `-journal` files. `0o600` means only the OS user running the Orb process can read or write these files, other users on the same machine cannot access them.

**Why this exists:** The SQLite database stores PHI (Protected Health Information). The WAL and SHM sidecars also contain PHI during active writes. Without this, the files would be world-readable by default on many Linux/macOS configurations.

**What this is and is not:** a filesystem-level access control, not encryption in itself. **Corrected 2026-08-15:** it is no longer the *only* at-rest control, with `ORB_DB_ENCRYPTION_KEY` set, the file is SQLCipher-encrypted and a removed disk or a copied backup is **not** readable without the key. See **§2.10**, which replaces the "still readable" claim this line used to make and the `DEFERRED` block it used to point at (that block now reads `ENCRYPTION AT REST, IMPLEMENTED`, `server/database.py:1301`).

Called twice: once before `init_db()` runs (to protect an existing database) and once after (because WAL/SHM sidecars may have been created during the `executescript`).

**⚠️ If you change this:** Do not remove the `-wal` and `-shm` suffix handling, those files contain PHI during active writes. A previous version of this function only chmod'd the main `.db` file, which left sidecars exposed.

---

#### `init_db()`

Called once at server startup. It:

1. Calls `_harden_db_file_permissions()`.
2. Runs `conn.executescript(SCHEMA)`, creates all tables and indexes if they do not already exist (every statement uses `CREATE TABLE IF NOT EXISTS`).
3. Applies a series of **additive migrations**, each one checks whether a column or table already exists before running `ALTER TABLE`, making them safe to re-run on every startup.
4. Seeds a system user (`id='system'`, name=`'Orb Sage'`) for AI-generated background log entries.
5. Records the current `SCHEMA_VERSION` in the `schema_version` table.
6. Calls `_harden_db_file_permissions()` again.

**Migrations are additive and idempotent**, they only add columns or tables, never remove them. Each migration checks the existing columns with `PRAGMA table_info(tablename)` before running `ALTER TABLE`. This means restarting the server on an already-migrated database is always safe.

**⚠️ If you change this:** When you add a new column to any table, follow the existing pattern: add an `ALTER TABLE` migration block inside `init_db()` guarded by a column-existence check. Never drop or rename a column in a migration, that requires a table-rebuild (the rename-old, create-new, insert-select, drop-old pattern used for `messages` and `surgeries`). Bump `SCHEMA_VERSION` when you add a significant migration.

---

#### `verify_audit_chain(conn=None) -> dict`

Verifies that no `audit_logs` rows have been tampered with after being written. It reads every row in `id` order and recomputes each row's `row_hash` using `compute_audit_row_hash()` from `audit_service`. It also verifies that each row's `prev_hash` matches the `row_hash` of the previous row, forming an unbroken chain.

Returns a dict:
```python
{
    "ok": True/False,
    "rows_total": 1500,
    "rows_checked": 1498,
    "rows_skipped_legacy": 2,      # rows with NULL row_hash (pre-hash-chain)
    "broken_row_ids": []            # IDs of any rows where hash doesn't match
}
```

Rows with `row_hash = NULL` are legacy rows written before the hash chain was implemented. The chain restarts after a legacy gap, so legacy rows do not cause false positives. Never raises on a broken chain, it returns `ok: False` with the broken row IDs instead.

---

### 2.3 Schema Version: and the migration ledger the manual never documented

> **Correction (2026-08-15).** This section said `SCHEMA_VERSION = 1`. It is **5** (`server/database.py:17`). More importantly, `schema_version` is **not** how Orb migrates its schema any more, and has not been for some time. There is a second, real migration system in `server/migrations.py`, **31 versioned migrations** with their own ledger table, which no printing of this manual has ever mentioned. A reader who trusted §2.3 would have believed the schema had changed once since inception.

#### 2.3.1 `schema_version`: the coarse marker

**`SCHEMA_VERSION = 5`** (module-level constant, `server/database.py:17`)

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    note TEXT
);
```

**What it stores:** One row per schema version that has been applied to this database. Every call to `init_db()` inserts `(SCHEMA_VERSION, 'init_db additive migrations applied')` with `INSERT OR IGNORE` (so it only writes once per version).

**Why it exists:** Provides an auditable record of which schema version a given `orb.db` is at. Without this, there would be no way to tell whether a database file has been migrated to the latest schema or not, short of inspecting every column.

**⚠️ If you change this:** Bump `SCHEMA_VERSION` whenever you add meaningful migrations to `init_db()`. Do not reuse version numbers. **But do not add new schema changes to `init_db()` at all**, see §2.3.2.

#### 2.3.2 `server/migrations.py`: the real migration system

**What it is** (`server/migrations.py:1-18`): a small, dependency-free migration framework, Alembic-style versioning without the SQLAlchemy weight the app does not use. Each migration is a `Migration(version: int, name: str, apply: Callable)` NamedTuple (`:28-32`). The runner applies any not recorded in the `schema_migrations` ledger (`:35-42`), in ascending order, and records each on success.

> **This is the forward mechanism for ALL new schema changes.** Add a migration to `MIGRATIONS` rather than another ad-hoc block in `database.init_db`. The historical additive blocks in `init_db` remain as the proven baseline, they are idempotent and introspection-guarded, but everything new goes here so the schema has an auditable, ordered, re-runnable history that also works against a Postgres backend.

Migrations **must be idempotent** (`IF NOT EXISTS`, `PRAGMA table_info` guards): the ledger prevents re-application, but idempotency keeps them safe even if the ledger is reset (`:16-18`).

**The ledger table**

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)                                                    -- server/migrations.py:36-41
```

**All 31 migrations** (`server/migrations.py:4656-4689`, verified 2026-08-15). Migrations **20–31** are the twelve that landed with the hospital-administration modules and the Sage assessment work.

| # | Name | # | Name |
|---|---|---|---|
| 1 | `hot_path_indexes` | 16 | `sage_conversation_threads` |
| 2 | `messages_idempotency` | 17 | `patient_review_marks` |
| 3 | `sage_insights_auto_resolved` | 18 | `shadow_none_confirmed_once` |
| 4 | `shadow_events` | 19 | `scribe_unverified_ack_columns` |
| 5 | `roster_import_runs` | **20** | **`hospital_admin_spine`** |
| 6 | `allergy_interlock` | **21** | **`biomedical_assets`** |
| 7 | `message_room_fit_flags` | **22** | **`housekeeping`** |
| 8 | `channel_members` | **23** | **`abdm_abha_rails`** |
| 9 | `recover_stranded_sage_insights` | **24** | **`workforce_attendance`** |
| 10 | `dose_guard` | **25** | **`dietary`** |
| 11 | `escalation_chain` | **26** | **`nabh_evidence`** |
| 12 | `device_pin_identity` | **27** | **`surgery_timing`** |
| 13 | `order_sets` | **28** | **`patient_appointment_requests_bridge`** |
| 14 | `dpdp_act_machinery` | **29** | **`sage_assessments`** |
| 15 | `history_import` | **30** | **`sage_assessment_tiers`** |
| | | **31** | **`sage_patient_specific`** |

`migrations.py` defines **89 `CREATE TABLE` statements** (count taken at migration 30; migration 31 is column-additive) on top of the 52 in `database.py`'s base `SCHEMA` and 7 in `services/helix_db.py`. §2.4's table catalogue therefore covers only the base schema; the migration-created tables are **not yet documented table-by-table** in this manual (§17).

**`run_pending_migrations(conn)`, read the docstring before you touch it** (`server/migrations.py:4712-4772`). It is one of the sharpest pieces of reasoning in the codebase and it is the reason schema changes are safe:

- **All-or-nothing.** Either every pending migration is applied and ledgered, or the database is exactly as it was.
- **It owns its transaction, deliberately.** The function used to document that it "runs inside the caller's open transaction (the caller commits)", and that was true of `init_db` **by luck, not by construction**. Python's `sqlite3` opens an implicit transaction only before DML, **never before DDL**. The only thing keeping the migration run atomic was an unrelated `INSERT OR IGNORE INTO users` a few dozen lines earlier that happened to leave a transaction open.
- **The concrete hazard, reproduced end-to-end 2026-08-06 with 500 rows.** Migration 3 is a four-statement rebuild (RENAME → CREATE → INSERT SELECT → DROP). SIGKILL it after the RENAME and `sage_insights` is gone with all its rows in `sage_insights_old`; the next boot recreates the table empty, migration 3 reads the fresh DDL and no-ops, and the ledger records v3, **the migration is skipped forever over a half-built schema, with no error anywhere.** See `tests/test_migration_atomicity.py`.
- **The resolution:** if the caller already holds a transaction (`init_db`'s `db_session`, the tests), **join** it and let the caller commit, preserving the existing contract including nested/pooled `SAVEPOINT` frames. Otherwise take an explicit `BEGIN IMMEDIATE` and commit or roll back here.

**`_atomic_table_rebuild`** (`server/database.py:1327-…`) exists for the same class of reason and carries the same warning: **do not go back to `executescript`**. `Connection.executescript()` implicitly COMMITs whatever transaction is pending and then runs in autocommit, so the old create-copy-drop-rename form was four separately-committed statements, not one. Reproduced: force the RENAME to fail after the DROP and the live `messages` table is simply gone, with `db_session`'s rollback a no-op because there is nothing left to roll back.

---

### 2.4 Core Tables (Orb Schema)

The tables below are defined in `SCHEMA` inside `database.py`. They are documented in the order they appear in the file, grouped by functional area.

---

#### `users`: Clinicians and Patients (System Accounts)

**Owned by:** Authentication service / all modules (universal dependency)

```sql
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('doctor', 'nurse', 'admin', 'patient', 'pharmacist', 'cashier')),
    department TEXT,
    registration_number TEXT,
    password_hash TEXT NOT NULL,
    pin_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**What it stores:** Every account that can log into Orb, doctors, nurses, admins, patients, and pharmacists. The `id` column is a TEXT UUID (not an auto-increment integer) because accounts may be provisioned from an external HR system. The `role` column has a `CHECK` constraint enforcing the allowed values at the database level.

| Column | Type | Meaning |
|--------|------|---------|
| `id` | TEXT PK | UUID string (e.g., `"usr_abc123"`) |
| `name` | TEXT | Full display name |
| `role` | TEXT | One of: `doctor`, `nurse`, `admin`, `patient`, `pharmacist` |
| `department` | TEXT | Optional hospital department (e.g., `"Cardiology"`) |
| `registration_number` | TEXT | Professional registration number (GMC, NMC, etc.) |
| `password_hash` | TEXT | Bcrypt hash of the login password |
| `pin_hash` | TEXT | SHA-256 hash of a 4-digit PIN for quick bedside unlock |
| `created_at` | TIMESTAMP | When the account was created |

**Special row:** `init_db()` seeds a `system` user (id=`'system'`, role=`admin`) so AI-generated audit entries have a valid `actor_user_id`.

**Migration history:** `pin_hash` was added as an additive migration (not in the original schema). `init_db()` back-fills all clinician rows with a default PIN hash for `'1234'` if `pin_hash IS NULL`. Helix's `init_helix_db()` also contains a migration that rebuilt this table to add the `pharmacist` role to the `CHECK` constraint.

**⚠️ If you change this:** The role list renders from `server/roles.py` `STORABLE_ROLES`, the single authority shared by this SCHEMA, the admin staffing route's validation, and the newest users-rebuild migration. Adding a role is a three-step change (add to `STORABLE_ROLES`, grant a set in `ROLE_PERMISSIONS`, add a rebuild migration calling `migrations._rebuild_users_role_check` with the new frozen list); a parity test in `tests/test_migrations.py` fails the suite if any step is missed. The rebuild cannot use the rename-old-first pattern on `users`: it is the FK parent of ~20 tables, and both in-transaction rebuild shapes fail under enforced FKs (`defer_foreign_keys` can never credit back the violations the parent DROP registers, and `legacy_alter_table`, like `foreign_keys`, is a silent no-op inside an open transaction, verified empirically 2026-08-19 on stdlib sqlite3 and sqlcipher3). Migration 33's helper therefore delegates to `database._atomic_table_rebuild`, which owns an autocommit window, then re-opens the runner's transaction.

---

#### `patients`: Patient Registry

**Owned by:** Admissions / Vigil / all clinical modules

```sql
CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    name TEXT NOT NULL,
    mrn TEXT UNIQUE NOT NULL,
    bed TEXT,
    ward TEXT,
    admission_date TIMESTAMP,
    diagnosis TEXT,
    status TEXT DEFAULT 'admitted' CHECK (status IN ('admitted', 'discharged', 'transferred')),
    age INTEGER,
    date_of_birth DATE,
    sex TEXT,
    phone TEXT,
    emergency_contact_name TEXT,
    emergency_contact_phone TEXT,
    address TEXT,
    attending_doctor_id TEXT REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**What it stores:** The hospital's patient registry. Every patient seen by any Orb module has a row here. The MRN (Medical Record Number) is the hospital-assigned identifier that persists across visits; the `id` column is Orb's internal UUID.

| Column | Type | Meaning |
|--------|------|---------|
| `id` | TEXT PK | Orb internal UUID |
| `user_id` | TEXT FK→users | If the patient has an Orb login, links to their user account |
| `mrn` | TEXT UNIQUE | Hospital Medical Record Number, must be unique |
| `bed` | TEXT | Current bed (e.g., `"4B"`) |
| `ward` | TEXT | Current ward |
| `admission_date` | TIMESTAMP | Date/time of current admission |
| `diagnosis` | TEXT | Primary diagnosis (free text) |
| `status` | TEXT | `admitted`, `discharged`, or `transferred` |
| `age` | INTEGER | Age in years (may be stale if not recomputed from DOB) |
| `date_of_birth` | DATE | Date of birth, added via migration |
| `sex` | TEXT | Biological sex |
| `phone` | TEXT | Patient phone, added via migration |
| `emergency_contact_name` | TEXT | Next of kin name, added via migration |
| `emergency_contact_phone` | TEXT | Next of kin phone, added via migration |
| `address` | TEXT | Home address, added via migration |
| `attending_doctor_id` | TEXT FK→users | The responsible doctor |

**⚠️ If you change this:** `patient_id` is a foreign key in nearly every other table in the schema. Any migration that alters this table can break those foreign key links. The `mrn` column is `UNIQUE NOT NULL`, never remove this constraint; MRN uniqueness is clinically important. The `sage_decision_cache` table has an `ON DELETE CASCADE` FK here, so deleting a patient will cascade-delete their cache entry.

---

#### `admissions`: Admission Records

**Owned by:** Admissions module

```sql
CREATE TABLE IF NOT EXISTS admissions (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    admitted_by TEXT NOT NULL REFERENCES users(id),
    ...
    triage_severity INTEGER,
    admitting_notes TEXT,
    nursing_notes TEXT,
    isolation_precautions TEXT,
    code_status TEXT DEFAULT 'full_code',
    initial_vitals_json TEXT,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'discharged', 'transferred')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**What it stores:** Each hospital visit/admission episode for a patient. A patient can have multiple admissions over time. Unlike `patients` (which stores the current state), `admissions` stores the snapshot at admission time including triage severity, the admitting clinician, initial vitals, isolation requirements, and code status (whether to attempt resuscitation).

| Key Column | Meaning |
|-----------|---------|
| `triage_severity` | Integer (e.g., 1–5), how urgently care is needed |
| `code_status` | `'full_code'` = attempt all resuscitation; other values indicate DNR/DNI |
| `initial_vitals_json` | JSON snapshot of vitals at admission time |
| `isolation_precautions` | Infection control requirements (e.g., contact, droplet, airborne) |

**⚠️ If you change this:** `code_status` is a clinically critical field. Do not change its default or allowed values without clinical review.

---

#### `patient_profiles`: Longitudinal Medical History

**Owned by:** Admissions / Scribe / clinical review

```sql
CREATE TABLE IF NOT EXISTS patient_profiles (
    patient_id TEXT PRIMARY KEY REFERENCES patients(id),
    comorbidities TEXT,
    past_medical_history TEXT,
    past_surgical_history TEXT,
    allergies TEXT,
    current_medications TEXT,
    family_history TEXT,
    social_history TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**What it stores:** One row per patient, updated in place, containing the patient's persistent medical background. Unlike `admissions` (per-visit), this table holds information that is true across all visits: chronic conditions, past surgeries, allergies, and family history.

`patient_id` is both the primary key and a foreign key to `patients`, enforcing a one-to-one relationship. All text fields store free text or JSON strings.

**⚠️ If you change this:** `allergies` is safety-critical. Helix (pharmacy) may reference this through the service layer when checking for contraindications. Do not remove or rename this column without updating those checks.

---

#### `vitals`: Time-Series Vital Signs

**Owned by:** Vigil module / ingestion service

```sql
CREATE TABLE IF NOT EXISTS vitals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    timestamp TIMESTAMP NOT NULL,
    heart_rate REAL,
    systolic_bp REAL,
    diastolic_bp REAL,
    spo2 REAL,
    temperature REAL,
    respiratory_rate REAL,
    consciousness TEXT DEFAULT 'alert',
    -- Added via migration:
    source TEXT DEFAULT 'simulated',
    source_event_id INTEGER REFERENCES ingestion_events(id)
);

CREATE INDEX IF NOT EXISTS idx_vitals_patient ON vitals(patient_id, timestamp DESC);
```

**What it stores:** The raw time-series vitals stream for every patient. Each row is one set of measurements at one point in time. Values are stored as REAL (floating point) and can be NULL if a measurement was not taken.

| Column | Meaning |
|--------|---------|
| `heart_rate` | Beats per minute |
| `systolic_bp` / `diastolic_bp` | Blood pressure in mmHg |
| `spo2` | Blood oxygen saturation (%) |
| `temperature` | Body temperature (unit depends on ingestion source) |
| `respiratory_rate` | Breaths per minute |
| `consciousness` | Text level (e.g., `'alert'`, `'confused'`, `'unconscious'`) |
| `source` | Where this reading came from: `'simulated'`, `'ingested_hl7'`, etc. Added via migration. |
| `source_event_id` | FK to the raw `ingestion_events` row that produced this vital. Added via migration. |

**Index:** `idx_vitals_patient` on `(patient_id, timestamp DESC)`, makes "get the last N vitals for patient X" fast.

**⚠️ If you change this:** Vigil reads vitals to compute MEWS and risk scores. The `source` column is important for distinguishing real HL7 data from simulation. If you rename columns, update the Vigil service and any ingestion adapters.

---

#### `risk_scores`: Patient Risk Assessments

**Owned by:** Vigil module

```sql
CREATE TABLE IF NOT EXISTS risk_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    score REAL NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('critical', 'elevated', 'stable')),
    drivers TEXT  -- JSON array of top risk drivers
);

CREATE INDEX IF NOT EXISTS idx_risk_patient ON risk_scores(patient_id, timestamp DESC);
```

**What it stores:** Computed risk scores for patients, generated by Vigil after each vitals update. `score` is a numeric value; `level` is the human-readable triage category. `drivers` is a JSON array of strings explaining *why* the score is high (e.g., `["low SpO2", "elevated heart rate"]`).

**⚠️ If you change this:** The Vigil module writes here; the React frontend reads `level` to color-code patients on the ward dashboard. Changing the allowed `level` values requires updating frontend color mapping logic.

---

#### `channels`: Communication Rooms (Relay)

**Owned by:** Relay module

```sql
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('case_room', 'department', 'direct')),
    patient_id TEXT REFERENCES patients(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**What it stores:** Chat rooms. A `case_room` is tied to a specific patient (`patient_id` is set). A `department` channel is ward-wide (e.g., "ICU"). A `direct` channel is a 1:1 message thread between two users.

**⚠️ If you change this:** `sage_room_memory` and `surgery_prep_requests` reference `channel_id`. If a case room channel is deleted, those references become orphans (SQLite does not cascade by default unless specified).

---

#### `messages`: Chat Messages (Relay)

**Owned by:** Relay module / Sage Ambient

```sql
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL REFERENCES channels(id),
    sender_id TEXT NOT NULL REFERENCES users(id),
    sender_name TEXT NOT NULL,
    sender_role TEXT NOT NULL,
    content TEXT NOT NULL,
    message_type TEXT DEFAULT 'text' CHECK (message_type IN ('text', 'alert', 'system', 'ambient_action', 'surgical_alert')),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    read_by TEXT DEFAULT '[]'  -- JSON array of user IDs
);

CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, timestamp DESC);
```

**What it stores:** Every message sent in any channel. `sender_name` and `sender_role` are denormalized (stored directly rather than looked up via `sender_id`) so historical messages display correctly even if a user's name or role changes later.

`read_by` is a JSON array of user IDs who have seen the message (e.g., `["usr_abc", "usr_xyz"]`).

`message_type` controls how the frontend renders the message:
- `text`, normal chat
- `alert`, highlighted alert bubble
- `system`, automated system message (e.g., "Patient transferred")
- `ambient_action`, a Sage Ambient suggested action card
- `surgical_alert`, emergency surgery escalation

**Migration history:** This table has been rebuilt twice via migration to expand the `message_type` CHECK constraint (first to add `ambient_action`, then `surgical_alert`). The rebuild pattern renames the old table, creates the new one, copies all rows, and drops the old one.

**⚠️ If you change this:** Adding a new `message_type` requires the rename-rebuild migration pattern because SQLite cannot `ALTER` a CHECK constraint. Update the frontend message renderer and any code that filters by `message_type`.

---

#### `sage_conversations`: Sage Q&A History

**Owned by:** Sage module

```sql
CREATE TABLE IF NOT EXISTS sage_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    sources TEXT,  -- JSON array of source references
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sage_user ON sage_conversations(user_id, timestamp);
```

**What it stores:** The turn-by-turn conversation history between clinicians and the Sage AI assistant. Each row is one message turn: `role='user'` for the clinician's question, `role='assistant'` for Sage's response. `sources` is a JSON array listing what documents or records Sage cited.

---

#### `clinical_documents`: Indexed Clinical Text (RAG)

**Owned by:** Sage / document ingestion

```sql
CREATE TABLE IF NOT EXISTS clinical_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    doc_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    author_id TEXT REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_docs_patient ON clinical_documents(patient_id);
```

**What it stores:** Clinical text documents attached to patients (discharge letters, referral letters, lab reports, etc.) that are ingested for RAG (Retrieval-Augmented Generation). `doc_type` categorizes the document (e.g., `"discharge_letter"`, `"referral"`, `"lab_result"`). The `content` field holds the full text.

**sqlite-vec / Vector embeddings:** The sqlite-vec extension (`sqlite-vec==0.1.6`) adds a virtual table for storing and searching vector embeddings. When a document is ingested, Sage computes a vector embedding of the text and stores it in a sqlite-vec virtual table (defined separately, not in this SCHEMA string). Vector similarity search allows Sage to find documents whose *meaning* is close to a user's query, even if the exact words differ. This is the backbone of Sage's RAG system.

**⚠️ If you change this:** sqlite-vec is loaded as a C extension at runtime. **Corrected 2026-08-18:** earlier printings said it was "incompatible with SQLCipher" and pointed at a `DEFERRED` encryption block, both stale. The incompatibility was with the **`pysqlcipher3`** driver specifically (`server/database.py:1316-1317`); the shipped driver (`sqlcipher3-wheels`, pinned in requirements.txt) coexists with sqlite-vec, which is exactly why it was chosen when encryption at rest was implemented on 2026-07-30 (`server/database.py:868`, and the block now titled `ENCRYPTION AT REST, IMPLEMENTED` at `server/database.py:1301`). The caution that survives: if you ever swap the SQLCipher driver, re-validate that sqlite-vec still loads before shipping it.

---

#### `scribe_notes`: AI-Transcribed Clinical Notes

**Owned by:** Scribe module

```sql
CREATE TABLE IF NOT EXISTS scribe_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT REFERENCES patients(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    raw_transcript TEXT NOT NULL,
    structured_note TEXT,       -- JSON structured output
    note_format TEXT CHECK (note_format IN ('soap', 'discharge', 'handoff', 'raw')),
    audio_path TEXT,
    verification_json TEXT,     -- JSON verification report (Stage 4), added via migration
    pipeline_meta_json TEXT,    -- JSON pipeline timing + model info, added via migration
    -- Lifecycle columns, all added via migration:
    status TEXT DEFAULT 'draft',
    version INTEGER DEFAULT 1,
    supersedes_note_id INTEGER,
    signed_by TEXT,
    signed_at TIMESTAMP,
    finalized_at TIMESTAMP,
    rejected_by TEXT,
    rejected_at TIMESTAMP,
    rejection_reason TEXT,
    last_edited_by TEXT,
    last_edited_at TIMESTAMP,
    ai_generated INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scribe_patient ON scribe_notes(patient_id);
```

**What it stores:** Clinical notes produced by the Scribe module, which transcribes audio (or text) into structured SOAP/discharge/handoff notes using an on-device LLM. Each row holds the raw transcript, the structured JSON output, and the full lifecycle of the note.

**Note lifecycle states** (`status`):
- `draft`, AI has generated the note; clinician has not yet reviewed it
- `signed`, clinician has approved and signed the note (immutable after signing)
- Other states (rejected, etc.) tracked via `rejected_by`, `rejected_at`, `rejection_reason`

`supersedes_note_id` allows a corrected note to point back to the note it replaces, creating a version chain.

**Legacy migration:** When the lifecycle columns were first added, all existing notes (written before the lifecycle system) were back-filled to `status='signed'` so they could not be accidentally edited as drafts.

**⚠️ If you change this:** Signed notes must remain immutable, do not allow any service to `UPDATE` a row where `status='signed'` without a clinical workflow review. The `ai_generated` flag is important for audit: regulators may require knowing which notes were authored by AI vs. humans.

---

#### `discharge_summaries`: Patient Discharge Documents

**Owned by:** Discharge module

```sql
CREATE TABLE IF NOT EXISTS discharge_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    summary TEXT NOT NULL,
    timeline_json TEXT,     -- JSON array of chronological events
    qr_code_path TEXT,
    discharged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_discharge_patient ON discharge_summaries(patient_id);
```

**What it stores:** The formal discharge summary generated when a patient leaves the hospital. `timeline_json` holds a chronological list of key events during the admission. `qr_code_path` points to a QR code image file that lets the patient (or a receiving provider) access their summary securely via a token.

---

#### `discharge_access_tokens`: Secure Discharge Summary Access

**Owned by:** Discharge module

```sql
CREATE TABLE IF NOT EXISTS discharge_access_tokens (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    expires_at TIMESTAMP NOT NULL,
    failed_attempts INTEGER DEFAULT 0,
    locked_until TIMESTAMP NULL,
    verified_at TIMESTAMP NULL,
    last_accessed_at TIMESTAMP NULL,
    revoked_at TIMESTAMP NULL,
    created_by_user_id TEXT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**What it stores:** Single-use or time-limited tokens that allow external access to a specific patient's discharge summary (e.g., via the QR code). The actual token value is never stored, only its SHA-256 hash (`token_hash`), so that even a database dump cannot be used to forge access.

| Column | Meaning |
|--------|---------|
| `token_id` | The public identifier (not secret) for the token |
| `token_hash` | SHA-256 of the secret token value |
| `expires_at` | Token is invalid after this timestamp |
| `failed_attempts` | Count of wrong token submissions |
| `locked_until` | If too many wrong attempts, access is blocked until this time |
| `verified_at` | When the token was first successfully used |
| `revoked_at` | If the token was manually revoked, when |

**⚠️ If you change this:** Never change `token_hash` to store the token in plaintext. The lockout mechanism (`failed_attempts`, `locked_until`) is a brute-force protection; do not remove it.

---

#### `lens_analyses`: Medical Image Analysis

**Owned by:** Lens module

```sql
CREATE TABLE IF NOT EXISTS lens_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT REFERENCES patients(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    image_path TEXT NOT NULL,
    image_type TEXT,   -- 'xray', 'ecg', 'ct', 'mri', 'photo', 'other'
    clinical_context TEXT,
    analysis TEXT NOT NULL,
    findings_json TEXT,    -- JSON structured findings
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_lens_patient ON lens_analyses(patient_id);
```

**What it stores:** Records from Lens, the module that sends medical images (X-rays, ECGs, CTs, MRIs) to an on-device vision model for interpretation. `image_path` is the filesystem path to the image file on the Orb server. `analysis` is the raw text interpretation. `findings_json` holds the same interpretation in structured form.

**⚠️ If you change this:** Image files live on disk at `image_path`. If you move the uploads directory, existing rows will have stale paths. Any cleanup job must delete both the database row and the file.

---

#### `medications`: Active Medication Orders

**Owned by:** Prescribing / clinical (separate from Helix pharmacy tables)

```sql
CREATE TABLE IF NOT EXISTS medications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    drug_name TEXT NOT NULL,
    dosage TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT 'Oral',
    frequency TEXT NOT NULL,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'discontinued', 'prn')),
    prescribed_by TEXT REFERENCES users(id),
    notes TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_meds_patient ON medications(patient_id);
```

**What it stores:** The active medication list for a patient as seen by clinical staff. This is a simpler clinical view, the full pharmacy workflow (verification, dispensing, stock movements) lives in the Helix tables. `route` is the administration route (e.g., `'Oral'`, `'IV'`, `'Topical'`). `prn` (Pro Re Nata) means "as needed."

**Relationship to Helix:** A prescription may start here (clinical order) and then flow into `helix_patient_med_orders` for pharmacy processing. They are not automatically linked by foreign key, the relationship is managed at the service layer.

---

#### `forecast_predictions`: Per-Patient Discharge & Acuity Forecasts

**Owned by:** Forecast module (LSTM model)

```sql
CREATE TABLE IF NOT EXISTS forecast_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    predicted_los_days REAL NOT NULL,
    discharge_probability REAL NOT NULL,
    acuity_trajectory TEXT NOT NULL CHECK (acuity_trajectory IN ('improving', 'stable', 'worsening')),
    confidence REAL NOT NULL,
    feature_snapshot TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_forecast_patient ON forecast_predictions(patient_id, created_at DESC);
```

**What it stores:** Cached output from the LSTM (Long Short-Term Memory) neural network that predicts each patient's likely length of stay and discharge probability. `feature_snapshot` stores the input feature vector used for that prediction run (as JSON), so predictions can be audited and re-evaluated. New rows are appended as predictions are refreshed, the index retrieves the latest prediction.

---

#### `hospital_forecasts`: Hospital-Level Daily Forecasts

**Owned by:** Forecast module

```sql
CREATE TABLE IF NOT EXISTS hospital_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_date DATE NOT NULL,
    hour INTEGER DEFAULT 0,
    predicted_admissions REAL,
    predicted_discharges REAL,
    predicted_occupancy REAL,
    ward_breakdown TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hospital_forecast ON hospital_forecasts(forecast_date);
```

**What it stores:** Aggregate hospital-level forecasts, how many admissions, discharges, and what total occupancy is expected on a given date. `ward_breakdown` is a JSON object breaking down the predictions by ward. Used by bed management and the operations dashboard.

---

#### `admission_history`: Historical Admissions (Poisson Model Training Data)

**Owned by:** Forecast module

```sql
CREATE TABLE IF NOT EXISTS admission_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admission_date DATE NOT NULL,
    patient_name TEXT NOT NULL,
    age INTEGER,
    sex TEXT,
    ward TEXT,
    diagnosis TEXT,
    severity TEXT CHECK (severity IN ('critical', 'elevated', 'stable')),
    length_of_stay_days REAL,
    discharged INTEGER DEFAULT 0,
    discharge_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_admission_history ON admission_history(admission_date);
```

**What it stores:** A rolling 30-day (or longer) history of admissions used to fit the Poisson arrival model that underlies the hospital-level forecasts. `discharged` is a boolean flag (0/1, SQLite has no native boolean type). This table may contain de-identified or synthetic data alongside real admissions.

---

#### `surgeries`: Scheduled & Emergency Surgeries

**Owned by:** Theatre / Surgery module

```sql
CREATE TABLE IF NOT EXISTS surgeries (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    surgeon_id TEXT NOT NULL REFERENCES users(id),
    procedure_name TEXT NOT NULL,
    diagnosis TEXT NOT NULL,
    scheduled_date DATE NOT NULL,
    scheduled_time TEXT NOT NULL,
    estimated_duration_min INTEGER,
    operating_room TEXT,
    status TEXT DEFAULT 'scheduled' CHECK (status IN (
        'scheduled','in_progress','completed','cancelled',
        'emergency_pending','emergency_context_generating',
        'emergency_ready','emergency_acknowledged','emergency_staged'
    )),
    co_morbidities TEXT,
    surgical_checklist TEXT,
    instruments_required TEXT,
    staff_required TEXT,
    risk_assessment TEXT,
    possible_complications TEXT,
    success_probability REAL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_surgeries_surgeon ON surgeries(surgeon_id, scheduled_date);
CREATE INDEX IF NOT EXISTS idx_surgeries_patient ON surgeries(patient_id);
CREATE INDEX IF NOT EXISTS idx_surgeries_date ON surgeries(scheduled_date);
```

**What it stores:** All surgical procedures, both elective (scheduled) and emergency. The status field tracks the full lifecycle including the emergency escalation states: when an emergency surgery is detected, the system generates context (`emergency_context_generating`), makes it available to the surgical team (`emergency_ready`), the team acknowledges it (`emergency_acknowledged`), and prepares the theatre (`emergency_staged`).

**Migration history:** The `status` CHECK constraint was rebuilt twice via migration to add the emergency states. The rebuild used the rename-old/create-new pattern.

**⚠️ If you change this:** Adding new status values requires the table-rebuild migration pattern. The `surgical_checklist`, `instruments_required`, and `staff_required` columns hold JSON text. Update the surgery service when adding new columns.

---

#### `surgery_team_assignments`: Surgical Team Members

**Owned by:** Theatre / Surgery module

```sql
CREATE TABLE IF NOT EXISTS surgery_team_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    surgery_id TEXT NOT NULL REFERENCES surgeries(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role_name TEXT NOT NULL,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'alerted', 'confirmed', 'declined')),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**What it stores:** The assignment of each clinician to a specific surgery, and their acknowledgement status. `role_name` is the role within the surgical team (e.g., `"Scrub Nurse"`, `"Anaesthetist"`). When the system alerts a team member, their row updates to `alerted`; they confirm or decline via the app.

---

#### `surgery_prep_requests`: Emergency Surgery Escalation

**Owned by:** Theatre / Surgery module

```sql
CREATE TABLE IF NOT EXISTS surgery_prep_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    channel_id TEXT REFERENCES channels(id),
    requested_by TEXT NOT NULL,
    requested_by_name TEXT NOT NULL,
    procedure_name TEXT,
    indication TEXT,
    urgency TEXT DEFAULT 'emergency',
    clinical_context TEXT,
    state TEXT DEFAULT 'drafted' CHECK (state IN ('drafted', 'resolved')),
    idempotency_key TEXT UNIQUE,
    surgery_id TEXT REFERENCES surgeries(id),
    resolved_by TEXT,
    resolved_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**What it stores:** Requests to escalate and prepare for an emergency surgery. Created when a clinician triggers an emergency prep via the chat interface. `idempotency_key` is `UNIQUE`, if the same escalation is triggered twice (e.g., due to a double-tap or network retry), only one row is created. `channel_id` links to the case room channel where the escalation message was sent.

---

#### `ingestion_events`: Raw Data Provenance (Dead-Letter Table)

**Owned by:** Ingestion service / HL7 adapter

```sql
CREATE TABLE IF NOT EXISTS ingestion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source_type TEXT NOT NULL,              -- hl7, json, csv_replay, simulated
    source_name TEXT,
    external_message_id TEXT,
    patient_identifier_raw TEXT,
    encounter_identifier_raw TEXT,
    raw_payload TEXT,                        -- full original payload
    payload_format TEXT,                     -- hl7_oru, json_webhook, csv
    parse_status TEXT DEFAULT 'received'
        CHECK (parse_status IN ('received','parsed','partially_parsed','failed','duplicate')),
    parse_error TEXT,
    fingerprint TEXT,                        -- SHA-256 for dedup
    correlation_id TEXT,
    processed_at TIMESTAMP,
    adapter_name TEXT,
    vitals_inserted INTEGER DEFAULT 0,
    observations_inserted INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ingestion_events_fp ON ingestion_events(fingerprint);
CREATE INDEX IF NOT EXISTS idx_ingestion_events_status ON ingestion_events(parse_status, received_at DESC);
```

**What it stores:** Every raw message received from an external system (HL7 feeds, JSON webhooks, CSV replays), with its full payload preserved verbatim. Acts as a dead-letter queue: even if parsing fails (`parse_status='failed'`), the original data is kept so the ingestion can be retried or debugged.

`fingerprint` is a SHA-256 hash of the message content used for deduplication, if the same message arrives twice (e.g., due to a retry), the second is marked `'duplicate'` and not processed.

`vitals_inserted` and `observations_inserted` are counters showing how many downstream rows were produced from this event.

---

#### `observations`: Structured Lab & OBX Results

**Owned by:** Ingestion service

```sql
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT REFERENCES patients(id),
    encounter_id TEXT,
    source TEXT NOT NULL DEFAULT 'unknown',
    source_event_id INTEGER REFERENCES ingestion_events(id),
    observation_code TEXT,          -- LOINC or local code
    observation_code_system TEXT,   -- LOINC, LOCAL, etc.
    observation_name TEXT NOT NULL,
    value_text TEXT,
    value_numeric REAL,
    unit TEXT,
    reference_range TEXT,
    abnormal_flag TEXT,
    status TEXT DEFAULT 'final',
    observed_at TIMESTAMP,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    panel_name TEXT,
    obr_group_id TEXT,
    raw_segment_ref TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_observations_patient ON observations(patient_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_source ON observations(source_event_id);
```

**What it stores:** Structured lab results and clinical observations parsed from HL7 OBX segments or JSON payloads. Each row is one result (e.g., a haemoglobin reading, a creatinine level). `observation_code` uses LOINC (a standard international coding system for lab tests). `abnormal_flag` carries the source system's flag (e.g., `'H'` for high, `'L'` for low, `'C'` for critical).

`patient_id` is nullable because some messages arrive before the patient can be resolved from the MRN (the ingestion service may fill it in as a second step). `encounter_id` similarly may be filled in post-resolution.

---

#### `audit_logs`: Immutable HIPAA Audit Trail

**Owned by:** `audit_service`, all modules write to it, nobody may UPDATE or DELETE from it

```sql
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    correlation_id TEXT,
    event_category TEXT NOT NULL,
    actor_user_id TEXT NULL,
    actor_role TEXT NULL,
    target_type TEXT NULL,
    target_id TEXT NULL,
    patient_id TEXT NULL,
    encounter_id TEXT NULL,
    source_type TEXT NULL,
    source_event_id INTEGER NULL,
    request_path TEXT NULL,
    http_method TEXT NULL,
    ip_address TEXT NULL,
    user_agent TEXT NULL,
    session_id TEXT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'partial_success', 'denied')),
    action_type TEXT NOT NULL,
    summary_message TEXT,
    metadata_json TEXT,
    prev_hash TEXT,
    row_hash TEXT
);
```

**Indexes:**
```sql
CREATE INDEX IF NOT EXISTS idx_audit_correlation_id ON audit_logs(correlation_id);
CREATE INDEX IF NOT EXISTS idx_audit_action_type ON audit_logs(action_type);
CREATE INDEX IF NOT EXISTS idx_audit_patient_id ON audit_logs(patient_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_category_timestamp ON audit_logs(event_category, timestamp);
```

**What it stores:** Every significant action performed in Orb: logins, patient record access, note signing, medication orders, image analyses, and more. This is the HIPAA audit trail.

**Why it must be append-only:** HIPAA §164.312(b) and §164.312(c) require that audit records be protected from alteration or destruction. An audit log that can be edited is not an audit log, it is just a log that someone might have edited. Making the table structurally immutable at the database layer (not just in application code) ensures the guarantee holds even if the application server is compromised.

**Tamper-evidence hash chain:** Each row contains:
- `prev_hash`, the `row_hash` of the immediately preceding row (NULL for the first hashed row, or after a legacy gap)
- `row_hash`, SHA-256 of `prev_hash` concatenated with a canonical serialization of this row's fields

If any row is modified after being written, its `row_hash` will not match when `verify_audit_chain()` recomputes it. If any row is deleted, the chain breaks at the gap. This makes tampering cryptographically detectable.

**The append-only triggers:**

```sql
CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_update
BEFORE UPDATE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_logs is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_delete
BEFORE DELETE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_logs is append-only: DELETE is forbidden');
END;
```

These triggers fire *before* any `UPDATE` or `DELETE` on `audit_logs` and immediately abort the operation with an error. Even if a developer or a compromised service calls `conn.execute("UPDATE audit_logs SET ...")`, the database will refuse it. This is enforced at the DB engine level, below the application code.

**Column reference:**

| Column | Meaning |
|--------|---------|
| `event_category` | High-level category (e.g., `'patient_data_access'`, `'auth'`, `'medication'`) |
| `actor_user_id` | Who performed the action (NULL for system-generated events) |
| `target_type` | The type of record acted on (e.g., `'patient'`, `'scribe_note'`) |
| `target_id` | The ID of the specific record |
| `patient_id` | The patient involved (if applicable) |
| `outcome` | Whether it succeeded, failed, was denied, or partially succeeded |
| `action_type` | Specific action (e.g., `'view_patient_record'`, `'sign_note'`) |
| `correlation_id` | Links events that are part of the same clinical workflow |
| `metadata_json` | Arbitrary additional context as a JSON object |

**⚠️ If you change this:** Do NOT add UPDATE or DELETE permissions for any service on this table, ever. Do not drop the triggers. If you need to add columns, use the `ALTER TABLE ... ADD COLUMN` pattern (additive only); existing hash-chain rows will have NULL for new columns, which is fine for legacy rows. If you rename the table, update `verify_audit_chain()`, `audit_service`, and every place that audits an action.

---

#### `event_log`: Cross-Module Event Bus Ledger

**Owned by:** All modules (Vigil, Relay, Sage, Scribe)

```sql
CREATE TABLE IF NOT EXISTS event_log (
    event_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    source_node TEXT NOT NULL,   -- 'vigil', 'relay', 'sage', 'scribe'
    event_type TEXT NOT NULL,    -- e.g., 'patient_deteriorated', 'note_signed'
    patient_id TEXT,             -- Nullable for system-wide events
    payload TEXT NOT NULL,       -- JSON payload
    timestamp TEXT NOT NULL,     -- ISO-8601 UTC
    correlation_id TEXT NOT NULL -- Traces a single clinical workflow across multiple nodes
);

CREATE INDEX IF NOT EXISTS idx_event_log_topic_timestamp ON event_log(topic, timestamp);
CREATE INDEX IF NOT EXISTS idx_event_log_patient ON event_log(patient_id);
CREATE INDEX IF NOT EXISTS idx_event_log_correlation ON event_log(correlation_id);
```

**What it stores:** A persistent ledger of all state-transition events across the Orb system. This is the "hospital nervous system", when Vigil detects deterioration, it writes a `patient_deteriorated` event here; Sage can read it; Relay can broadcast it. `correlation_id` links together all the events that are part of a single clinical workflow (e.g., all events triggered by one deterioration detection).

`event_id` is a TEXT UUID (not auto-increment) assigned by the emitting module, ensuring uniqueness even if events are generated offline and batched.

---

### 2.5 Sage Ambient Tables

Sage Ambient is the always-listening AI layer that monitors case room conversations and suggests clinical actions. It uses five tables.

---

#### `sage_room_memory`: Per-Room AI Context (One Row Per Room)

```sql
CREATE TABLE IF NOT EXISTS sage_room_memory (
    room_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    patient_summary TEXT,
    discussion_summary TEXT,
    current_concern TEXT,
    likely_next_steps TEXT,
    unresolved_questions TEXT,
    active_intents TEXT,
    risk_flags TEXT,
    participants TEXT,
    last_processed_msg_id INTEGER,
    last_processed_at TIMESTAMP,
    message_count INTEGER DEFAULT 0,
    llm_call_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**What it stores:** A single rolling summary of everything Sage knows about an active case room discussion. This is intentionally bounded to one row per room, Sage does not store a full conversation log here (that lives in `messages`). Instead, it maintains a compressed, structured understanding: what is being discussed, what the current concern is, what the unresolved questions are, and who is in the room.

`last_processed_msg_id` tells Sage which message it last processed, so it can pick up incrementally. `llm_call_count` tracks how many times the LLM has been invoked for this room (useful for cost/rate monitoring).

---

#### `sage_observations`: Clinically Meaningful Moments

```sql
CREATE TABLE IF NOT EXISTS sage_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    source_message_id INTEGER,
    observation_type TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sage_obs_type ON sage_observations(observation_type, created_at DESC);
```

**What it stores:** Discrete clinical facts extracted from room conversations by Sage (e.g., "clinician mentioned worsening SpO2", "patient reported chest pain"). Unlike `sage_room_memory` which is overwritten, this table is append-only, each meaningful observation gets its own row. `confidence` is a 0–1 score from the model. `source_message_id` links back to the message that triggered this observation.

---

#### `sage_actions`: AI-Suggested Clinical Actions

```sql
CREATE TABLE IF NOT EXISTS sage_actions (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target TEXT NOT NULL,
    rationale TEXT,
    confidence REAL,
    state TEXT DEFAULT 'suggested' CHECK (state IN (
        'suggested', 'acknowledged', 'dismissed', 'confirmed', 'failed', 'superseded'
    )),
    version INTEGER DEFAULT 1,
    source_msg_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sage_actions_room ON sage_actions(room_id, created_at DESC);
```

**What it stores:** Concrete actions Sage recommends to clinicians (e.g., "order an ECG", "increase O2 flow", "escalate to ICU"). `action_type` names the type of action; `target` is what or who the action applies to; `rationale` is Sage's explanation.

**Action state machine:**
- `suggested` → `acknowledged` (clinician has seen it) → `confirmed` (clinician approved) or `dismissed`
- `confirmed` → `failed` (if the downstream action could not execute)
- Any state → `superseded` (if a newer version of this action replaces it)

`version` increments if the same logical action is re-suggested with updated confidence or rationale.

---

#### `sage_action_events`: Action State Transition Audit

```sql
CREATE TABLE IF NOT EXISTS sage_action_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL REFERENCES sage_actions(id),
    prior_state TEXT,
    new_state TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(id),
    actor_name TEXT NOT NULL,
    actor_role TEXT,
    reason TEXT,
    source TEXT DEFAULT 'ui' CHECK (source IN ('ui', 'system')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sage_action_events_action ON sage_action_events(action_id, created_at DESC);
```

**What it stores:** Every state change on a `sage_actions` row, with who made the change and why. This is the audit trail for AI-suggested actions. `source` distinguishes whether a state transition was triggered by a human (`'ui'`) or by the system automatically (`'system'`).

---

#### `sage_handoffs`: Downstream Module Handoffs

```sql
CREATE TABLE IF NOT EXISTS sage_handoffs (
    id TEXT PRIMARY KEY,
    source_action_id TEXT NOT NULL REFERENCES sage_actions(id),
    room_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    target_module TEXT NOT NULL,
    handoff_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'failed', 'unsupported')),
    downstream_ref_id TEXT,
    error_summary TEXT,
    created_by TEXT NOT NULL REFERENCES users(id),
    last_attempted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sage_handoffs_action ON sage_handoffs(source_action_id, created_at DESC);
```

**What it stores:** When a Sage action is confirmed, the system may hand off to another Orb module (e.g., creating a medication order in Helix, triggering a scribe note, or scheduling a surgery). Each handoff attempt is a row here. `target_module` names the receiving module; `payload_json` is the data passed to it; `downstream_ref_id` is the ID created in the target module.

---

#### `sage_handoff_events`: Handoff Status Audit

```sql
CREATE TABLE IF NOT EXISTS sage_handoff_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id TEXT NOT NULL REFERENCES sage_handoffs(id),
    prior_status TEXT,
    new_status TEXT NOT NULL,
    actor_id TEXT,
    source TEXT DEFAULT 'system' CHECK (source IN ('system', 'user', 'module_callback')),
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sage_handoff_events_handoff ON sage_handoff_events(handoff_id, created_at DESC);
```

**What it stores:** Every status change on a `sage_handoffs` row. `module_callback` is used when the downstream module reports back asynchronously (e.g., "the medication order was accepted").

---

#### `sage_decision_cache`: Preloaded Sage Context Cache

```sql
CREATE TABLE IF NOT EXISTS sage_decision_cache (
    patient_id TEXT PRIMARY KEY,
    mews_score REAL NOT NULL,
    compiled_context TEXT NOT NULL,  -- Pre-assembled JSON: Channel A, B, and C data
    preloaded_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE
);
```

**What it stores:** A precomputed context bundle for each patient, ready for Sage to use without re-querying. The `compiled_context` JSON contains the patient's clinical summary from multiple "channels" (A = demographics/vitals, B = medications/labs, C = recent conversations). `mews_score` is cached alongside for fast triage filtering. The cache expires at `expires_at` and is regenerated by a background task.

`ON DELETE CASCADE`, if the patient row is deleted from `patients`, their cache entry is automatically deleted.

---

### 2.6 `server/services/helix_db.py`: Helix (Pharmacy) Sub-Schema

Helix is Orb's pharmacy management module. Its tables are stored in the same `orb.db` file but are defined separately in `helix_db.py`. They are initialized by calling `init_helix_db()` during startup (after `init_db()`).

The Helix schema uses `db_session()` from `database.py`, there is no separate database connection or file.

---

#### `helix_medication_master`: Drug Catalog

```sql
CREATE TABLE IF NOT EXISTS helix_medication_master (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    generic_name TEXT,
    form TEXT,
    strength TEXT
);
```

**What it stores:** The hospital's master catalog of drugs. Each row is one drug product. `name` is the brand name; `generic_name` is the international non-proprietary name. `form` is the formulation (e.g., `'Tablet'`, `'IV Solution'`); `strength` is the concentration (e.g., `'500mg'`).

This table is the reference that all other Helix tables point to via `medication_id`.

**⚠️ If you change this:** Changing a drug's `id` (primary key) will break all foreign key references in `helix_inventory_lots`, `helix_patient_med_orders`, and `helix_stock_movements`. Drug records should be deprecated, not deleted.

---

#### `helix_inventory_lots`: Physical Stock by Lot Number

```sql
CREATE TABLE IF NOT EXISTS helix_inventory_lots (
    id TEXT PRIMARY KEY,
    medication_id TEXT REFERENCES helix_medication_master(id),
    lot_number TEXT NOT NULL,
    quantity INTEGER DEFAULT 0,
    expiration_date DATE
);
```

**What it stores:** Physical inventory, tracking each distinct lot (batch) of a drug separately. A lot is a manufacturer batch, tracking by lot is required so that if a drug recall occurs, every affected vial or packet can be identified. `quantity` is the current count of units on hand. `expiration_date` is used to flag expired stock.

**⚠️ If you change this:** Stock movements (dispensing, receiving) must always update `quantity` via `helix_stock_movements` to maintain a complete audit trail of how quantity changed. Never update `quantity` directly without creating a corresponding stock movement row.

---

#### `helix_patient_med_orders`: Pharmacy Medication Orders

```sql
CREATE TABLE IF NOT EXISTS helix_patient_med_orders (
    id TEXT PRIMARY KEY,
    patient_id TEXT REFERENCES patients(id),
    medication_id TEXT REFERENCES helix_medication_master(id),
    status TEXT DEFAULT 'pending' CHECK (status IN (
        'pending', 'reviewing', 'verified', 'rejected',
        'substitute_suggested', 'scheduled', 'dispensed', 'delivered'
    )),
    prescribed_by TEXT REFERENCES users(id),
    dosage TEXT,
    frequency TEXT,
    route TEXT,
    substitute_medication_id TEXT REFERENCES helix_medication_master(id),
    pharmacist_notes TEXT,
    reviewed_by TEXT REFERENCES users(id),
    reviewed_at TIMESTAMP,
    scheduled_at TIMESTAMP,
    last_transition_id TEXT,
    is_mock_event INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_helix_orders_patient ON helix_patient_med_orders(patient_id);
CREATE INDEX IF NOT EXISTS idx_helix_orders_status ON helix_patient_med_orders(status);
```

**What it stores:** The pharmacy's view of each medication order, from initial prescription through to delivery. Distinct from the `medications` table in the core schema (which is a simpler clinical view), this table tracks the full pharmacy workflow.

**Order status lifecycle:**
```
pending → reviewing → verified → scheduled → dispensed → delivered
                    ↓           ↓
                 rejected   substitute_suggested
```

`substitute_medication_id` is used when the requested drug is unavailable and the pharmacist suggests an equivalent alternative. `last_transition_id` links to the most recent `helix_transitions` row, allowing the current state to be cross-referenced with the transition audit.

`is_mock_event` flags whether this order was generated for a simulation/demo, preventing mock data from appearing as real clinical orders in reports.

**⚠️ If you change this:** The `_migrate_helix_orders()` function handles the rebuild migration for this table. Do not add new statuses to the `CHECK` constraint without also handling the rename-rebuild pattern in the migration, SQLite cannot `ALTER` a `CHECK` constraint in-place. Update the Helix service's state machine logic in parallel.

---

#### `helix_pharmacy_verifications`: Pharmacist Sign-Offs

```sql
CREATE TABLE IF NOT EXISTS helix_pharmacy_verifications (
    id TEXT PRIMARY KEY,
    order_id TEXT REFERENCES helix_patient_med_orders(id),
    pharmacist_id TEXT REFERENCES users(id),
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);
```

**What it stores:** A record of each time a pharmacist formally verified a medication order. One row per verification event. This is separate from the order's `status` column, it provides a durable, auditable record of *who* verified *which* order and *when*, with optional free-text notes.

---

#### `helix_stock_movements`: Inventory Audit Trail

```sql
CREATE TABLE IF NOT EXISTS helix_stock_movements (
    id TEXT PRIMARY KEY,
    lot_id TEXT REFERENCES helix_inventory_lots(id),
    medication_id TEXT REFERENCES helix_medication_master(id),
    order_id TEXT REFERENCES helix_patient_med_orders(id),
    quantity_change INTEGER,
    reason TEXT,
    is_mock INTEGER DEFAULT 1,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**What it stores:** Every change to inventory, dispensing, receiving new stock, wastage, returns. `quantity_change` is negative for stock going out (dispensed) and positive for stock coming in. `reason` is a text description. `order_id` links a dispensing event back to the prescription.

`is_mock` defaults to `1`, meaning new rows are mock/simulated by default. Real stock movements must explicitly set `is_mock=0`. This is a safety valve to prevent simulation data from contaminating real inventory counts.

**⚠️ If you change this:** The `_migrate_stock_movements()` function adds `medication_id`, `order_id`, and `is_mock` as additive column migrations. If you add further columns here, follow the same pattern. Never change `quantity` in `helix_inventory_lots` without creating a corresponding row here.

---

#### `helix_transitions`: Order State Transition Log (Idempotency)

```sql
CREATE TABLE IF NOT EXISTS helix_transitions (
    id TEXT PRIMARY KEY,
    order_id TEXT REFERENCES helix_patient_med_orders(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    actor_id TEXT REFERENCES users(id),
    payload TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_helix_transitions_order ON helix_transitions(order_id);
CREATE INDEX IF NOT EXISTS idx_helix_transitions_idem ON helix_transitions(idempotency_key);
```

**What it stores:** Every state transition for every medication order: from which status, to which status, who made the change, and when. This is the complete audit trail for the pharmacy workflow.

`idempotency_key` is `UNIQUE`, if a client sends the same state transition twice (e.g., due to a retry after a network timeout), the second insert will fail with a unique constraint violation, which the service catches and treats as "already processed." This prevents a double-dispense or double-verification from occurring.

`payload` stores any additional JSON context about the transition (e.g., substitution details, rejection reason).

**⚠️ If you change this:** The `idempotency_key` uniqueness constraint is safety-critical for medication workflows. Do not remove it. If you add new transition types, update the Helix service's state machine.

---

### 2.7 Helix Initialization: `init_helix_db()`

Called during server startup, after `init_db()`.

```python
def init_helix_db():
    with db_session() as conn:
        # 1. Migration: add 'pharmacist' role to users CHECK constraint (if needed)
        # 2. _migrate_helix_orders(conn): rebuild orders table if missing columns
        # 3. _migrate_stock_movements(conn): add missing columns to stock movements
        # 4. conn.executescript(HELIX_SCHEMA): create tables if they don't exist
```

**Step 1, users migration:** The `users` table's `CHECK` constraint originally did not include `pharmacist`. Helix tests whether it can insert a test user with `role='pharmacist'` and, if not, rebuilds the `users` table with the new constraint. The core `database.py` schema now includes `pharmacist` in the constraint, so this migration only fires on databases created before that change.

**Step 2, `_migrate_helix_orders(conn)`:** Checks whether `helix_patient_med_orders` has the `substitute_medication_id` column (added in a later version). If not, renames the old table, runs `HELIX_SCHEMA` to create the full schema, copies data over (mapping old status values like `'cancelled'` to `'rejected'`), and drops the old table.

**Step 3, `_migrate_stock_movements(conn)`:** Adds `medication_id`, `order_id`, and `is_mock` columns if they are missing, using `ALTER TABLE ... ADD COLUMN` (additive, nullable).

**Step 4, `conn.executescript(HELIX_SCHEMA)`:** Creates all Helix tables using `IF NOT EXISTS`, which is a no-op for tables that already exist.

**⚠️ If you change this:** Migration failures in `_migrate_helix_orders` log loudly (`logger.error`) and do not re-raise, the system will attempt to continue but with potentially outdated schema. Check startup logs for Helix migration errors. If you add new Helix tables, add them to `HELIX_SCHEMA`; if you add new columns to existing Helix tables, add an `ALTER TABLE` migration block in `_migrate_helix_orders` or `_migrate_stock_movements` (or a new helper function following the same pattern).

---

### 2.8 WAL Mode: How It Works and Why Orb Uses It

By default, SQLite uses a rollback journal: before modifying any page, it copies the original to a journal file so it can undo the change. This means readers must wait for writers to finish, only one writer or multiple readers can be active at once.

**WAL (Write-Ahead Logging)** changes this: writes go to a separate `-wal` file first. The main database file is not changed during a write. Readers continue reading the main file (unaffected by the in-progress write). Only at a "checkpoint" are the WAL contents transferred to the main file.

**Why this matters for Orb:** FastAPI is an async web server that handles many concurrent requests. Without WAL, a slow write (e.g., Sage composing a response while Vigil inserts a vitals row) would block all readers across the application. WAL allows reads to continue uninterrupted during writes.

**The `-wal` and `-shm` sidecar files:** When WAL mode is active, SQLite creates `orb.db-wal` and `orb.db-shm` alongside `orb.db`. These files contain PHI during active writes. They are protected by `_harden_db_file_permissions()` and must be included in any backup or at-rest encryption strategy.

---

### 2.9 Index Summary

Every table that is frequently queried by `patient_id` or `timestamp` has an index. Below is a full list:

| Index Name | Table | Columns | Purpose |
|-----------|-------|---------|---------|
| `idx_vitals_patient` | vitals | patient_id, timestamp DESC | Fast time-series queries per patient |
| `idx_messages_channel` | messages | channel_id, timestamp DESC | Load chat history for a room |
| `idx_risk_patient` | risk_scores | patient_id, timestamp DESC | Latest risk score per patient |
| `idx_docs_patient` | clinical_documents | patient_id | RAG document lookup |
| `idx_scribe_patient` | scribe_notes | patient_id | Load notes for a patient |
| `idx_sage_user` | sage_conversations | user_id, timestamp | Load conversation history |
| `idx_discharge_patient` | discharge_summaries | patient_id | Load discharge summaries |
| `idx_lens_patient` | lens_analyses | patient_id | Load image analyses |
| `idx_meds_patient` | medications | patient_id | Active medication list |
| `idx_forecast_patient` | forecast_predictions | patient_id, created_at DESC | Latest forecast per patient |
| `idx_hospital_forecast` | hospital_forecasts | forecast_date | Date-range queries |
| `idx_admission_history` | admission_history | admission_date | Poisson model date range |
| `idx_surgeries_surgeon` | surgeries | surgeon_id, scheduled_date | Surgeon's schedule |
| `idx_surgeries_patient` | surgeries | patient_id | Patient's surgery history |
| `idx_surgeries_date` | surgeries | scheduled_date | Theatre schedule by date |
| `idx_ingestion_events_fp` | ingestion_events | fingerprint | Deduplication lookup |
| `idx_ingestion_events_status` | ingestion_events | parse_status, received_at DESC | Dead-letter queue management |
| `idx_observations_patient` | observations | patient_id, observed_at DESC | Lab results per patient |
| `idx_observations_source` | observations | source_event_id | Traceability to ingestion event |
| `idx_audit_correlation_id` | audit_logs | correlation_id | Workflow-level audit queries |
| `idx_audit_action_type` | audit_logs | action_type | Filter by action type |
| `idx_audit_patient_id` | audit_logs | patient_id | All events for a patient |
| `idx_audit_timestamp` | audit_logs | timestamp | Time-range audit queries |
| `idx_audit_category_timestamp` | audit_logs | event_category, timestamp | Category + time range |
| `idx_sage_obs_type` | sage_observations | observation_type, created_at DESC | Sage ambient observation queries |
| `idx_sage_actions_room` | sage_actions | room_id, created_at DESC | Active actions per room |
| `idx_sage_action_events_action` | sage_action_events | action_id, created_at DESC | Action state history |
| `idx_sage_handoffs_action` | sage_handoffs | source_action_id, created_at DESC | Handoffs from an action |
| `idx_sage_handoff_events_handoff` | sage_handoff_events | handoff_id, created_at DESC | Handoff status history |
| `idx_event_log_topic_timestamp` | event_log | topic, timestamp | Event bus topic queries |
| `idx_event_log_patient` | event_log | patient_id | All events for a patient |
| `idx_event_log_correlation` | event_log | correlation_id | Workflow correlation |
| `idx_helix_orders_patient` | helix_patient_med_orders | patient_id | Patient's medication orders |
| `idx_helix_orders_status` | helix_patient_med_orders | status | Pending/dispensed queue |
| `idx_helix_transitions_order` | helix_transitions | order_id | All transitions for an order |
| `idx_helix_transitions_idem` | helix_transitions | idempotency_key | Idempotency dedup lookup |

**⚠️ If you add a new table:** Add indexes on any column that will appear in a `WHERE` clause in application queries. The most important are `patient_id` (universally) and any time-based column used for "get latest" queries (index descending: `column DESC`).

---

### 2.10 Encryption at Rest: IMPLEMENTED (2026-07-30)

> ### ⚠️ This section was materially wrong and is the manual's most serious correction.
>
> Every printing until 2026-08-15 said: *"Orb currently does **not** encrypt the database at rest"* and *"Why SQLCipher is not in use yet"*. **That is false and has been since 2026-07-30.** The source comment block it cited was itself rewritten on that date to say `ENCRYPTION AT REST, IMPLEMENTED` (`server/database.py:1301-1324`); the manual quoted the historical note underneath it as if it were the current state.
>
> This is exactly the failure mode the manual's own rule exists to prevent, and it is the worst possible one to get wrong: a security control that **exists** was documented as **absent**, in the section a CIO or an auditor greps first. Anyone who answered a security questionnaire from this section understated Orb's posture. `docs/CIO_SECURITY_QUESTIONNAIRE.md` and `docs/SECURITY_ONEPAGER.md` should be re-checked against this section, not the reverse.

#### What is encrypted, and how

**1. The database, SQLCipher via `PRAGMA key`.** `server/database.py:854-893`.

When `ORB_DB_ENCRYPTION_KEY` is set **and** a SQLCipher driver is importable, `_open_raw_connection()` opens `orb.db` through that driver and applies the key **before any other statement**:

```python
conn = driver.connect(str(DB_PATH), timeout=30, factory=factory)
conn.execute(f"PRAGMA key = \"x'{_sqlcipher_key_hex(DB_ENCRYPTION_KEY)}'\"")   # database.py:887-889
```

- Driver preference: `pysqlcipher3` first, then `sqlcipher3` (`:877-884`). The pinned dependency is **`sqlcipher3-wheels==0.5.7`** (`server/requirements.txt:47`), the only distribution with a CPython 3.12 arm64 wheel.
- `_sqlcipher_key_hex` (`:896-903`) derives a 32-byte raw key by SHA-256 over the operator-supplied material, so any-length key material is accepted and SQLCipher's per-open KDF is skipped.
- The `-wal` and `-shm` sidecars are encrypted along with the main file.
- **Absent the key or the driver, the app falls back to plaintext `sqlite3` and logs a one-time warning** (`_warn_db_encryption_unavailable`, `:908-919`). It does **not** refuse to boot. That is a deliberate availability choice and it is also a real risk: a misconfigured appliance runs unencrypted and only says so once, in a log line. Treat the warning as a P1.

**2. Uploaded PHI media, AES-256-GCM.** `server/services/crypto_service.py:1-21`. Lens images and Scribe audio are written in a single self-describing envelope:

```
b"ORBENC1\n" + 12-byte nonce + AES-256-GCM ciphertext(+16-byte tag)
```

The key is SHA-256 of `ORB_UPLOAD_ENCRYPTION_KEY`. AES-GCM is authenticated, so tampering with a file on disk **fails decryption** rather than returning corrupted plaintext. Encryption is opt-in and degrades gracefully: enabled only when the key is set *and* `cryptography` is importable; otherwise plaintext with a one-time warning. **Reads are format-sniffing**, any file starting with the `ORBENC1` magic is decrypted, anything else is returned as-is, so turning encryption on does not orphan previously-written plaintext files, and turning it off still reads already-encrypted files as long as the key is configured. `/uploads/{filename}` decrypts transparently for the authorised caller (§1).

**3. Field-level crypto**, `server/services/field_crypto.py` (*not yet documented in detail, §17*).

#### The concern that used to block this, and how it was resolved

**VALIDATED 2026-07-30 on Apple Silicon / Python 3.12 with `sqlcipher3-wheels`** (`server/database.py:867-873`): SQLCipher coexists with the sqlite-vec loadable extension (`vec0` vector search) **and** FTS5 **and** WAL mode. The earlier concern about an extension conflict does not hold on this build. Encryption stays opt-in, it is a native dependency and needs a key, but enabling it is a supported configuration.

#### Two things that will bite you

**Migration is mandatory.** A pre-existing plaintext `orb.db` **cannot be opened by SQLCipher**. Run `scripts/encrypt_db.py --migrate` once; it backs up, verifies row counts + audit-chain linkage + vector/FTS search, then swaps (`server/database.py:1303-1307`).

**`sqlite3.Row` breaks.** `_row_factory_for(conn)` (`server/database.py:922-946`) exists because stdlib `sqlite3.Row` is a C type that validates its argument is a `sqlite3.Cursor` and raises `TypeError: Row() argument 1 must be sqlite3.Cursor, not sqlcipher3.dbapi2.Cursor`. Hard-coding `sqlite3.Row` therefore broke **every query** the moment encryption was actually switched on, which is how nobody noticed: the path had never run in anger. It walks the MRO, not just `type(conn).__module__`, because a pooled connection is a subclass defined in `database.py` and the direct module check would misread a pooled SQLCipher handle as stdlib and reintroduce the bug.

#### The honest limitation: state this, do not soften it

`server/database.py:1309-1312`:

> With the key in `.env` on the same disk, this protects **database copies**, backups, exports, a stolen disk image read on another machine, **but not a running or stolen appliance whose `.env` an attacker can also read.** Pair it with FileVault, and move the key to the OS keychain for a stronger posture.

Key escrow / key ceremony is tracked in `docs/KEY_CEREMONY.md` and is **still an open item**, not a shipped control.

#### The layers that remain regardless

1. **Filesystem permissions** `0o600` via `_harden_db_file_permissions()`, prevents other OS users reading the files. Failure to set them is logged as a security-relevant warning but is non-fatal (`server/database.py:1293-1297`).
2. **Full-disk encryption** at the OS level (FileVault), which is now a *complement* to SQLCipher rather than the substitute the old text proposed.

---

### 2.10a `run_db` and the event-loop rule

> **New (2026-08-15).** `run_db` appeared **zero times** in every previous printing of this manual, despite being the single most-enforced house rule in the codebase and the fix for three production freezes.

**The rule:** blocking DB work called from an `async def` goes through `await run_db(fn, …)`.

```python
_DB_EXECUTOR = _TPE(max_workers=4, thread_name_prefix="orb-db")     # database.py:1201

async def run_db(fn, *args, **kwargs):
    """Run blocking DB work (a sync function that opens its own db_session)
    off the event loop. The function must be PURE SYNC, it must not touch
    asyncio, and it must open/close its own session so the transaction begins
    and ends inside the worker thread."""                            # database.py:1205-1209
    return await loop.run_in_executor(_DB_EXECUTOR, _partial(fn, *args, **kwargs))
```

**Why it matters more since encryption shipped:** every page SQLCipher touches costs HMAC-SHA512 + AES. A blocking query on the loop no longer merely delays one request, it stalls the whole appliance. The audit middleware, which is the highest-traffic DB write in the codebase, records this explicitly: *"three production freezes came from exactly this shape"* (`server/services/audit_middleware.py:15-21`).

**Three corollaries that are easy to get wrong:**

- **Off-loop is not fire-and-forget.** The audit middleware still `await`s the write; the response is not returned until the audit row is durable (`audit_middleware.py:19-21`).
- **A plain `def` route is often the right answer instead.** FastAPI runs `def` endpoints in the anyio worker threadpool for free. `/uploads/{filename}` is `def` deliberately, and `server/main.py:890-903` explains why converting it to `async def` + `run_db` would be worse on two counts, see §1.
- **`_DB_EXECUTOR` has only four workers** (`database.py:1201`), sized for short DB statements. Pushing bulk work through it convoys every interactive query in the appliance behind one long job.

---

### 2.10b `server/db_backend.py`: backend seam, pooling, and the exception aliases

> **New (2026-08-15).** This file did not exist in any previous printing's repository map or table of contents.

**Backend selection** (`server/db_backend.py:22-45`). `ORB_DB_BACKEND` defaults to `sqlite`. `postgres`/`postgresql`/`pg` are *known but unwired* and **fail fast at import** with an actionable message rather than silently falling back to the wrong store for a PHI database. `PLACEHOLDER` (`?` vs `%s`) is exported so query builders can stay backend-agnostic.

**The DB-API exception aliases, a live, silent, product-wide bug** (`server/db_backend.py:47-…`):

> `sqlcipher3` defines its **own** DB-API exception hierarchy rooted at `builtins.Exception`, it does **not** subclass the stdlib's:
>
> ```python
> issubclass(sqlcipher3.dbapi2.IntegrityError, sqlite3.IntegrityError)  # -> False
> ```
>
> So on **every encrypted deployment**, each `except sqlite3.IntegrityError` in this codebase was **dead code**. The handlers looked correct, passed review, and passed the unencrypted test suite, and in production a duplicate insert escaped as an unhandled traceback instead of the intended message. Reproduced 2026-07-31.

**House rule, therefore:** catch DB exceptions via the `db_backend` aliases (`DBIntegrityError`, etc.), **never** via stdlib `sqlite3`. `server/services/audit_service.py:22` is an example of a call site doing it correctly.

---

### 2.11 Quick Reference: Which Service Owns Which Table

| Table | Primary Owner / Module |
|-------|----------------------|
| `users` | Auth service (all modules depend on it) |
| `patients` | Admissions / all clinical modules |
| `admissions` | Admissions module |
| `patient_profiles` | Admissions / Scribe |
| `vitals` | Vigil / ingestion service |
| `risk_scores` | Vigil module |
| `channels` | Relay module |
| `messages` | Relay module / Sage Ambient |
| `sage_conversations` | Sage module |
| `clinical_documents` | Sage / document ingestion |
| `scribe_notes` | Scribe module |
| `discharge_summaries` | Discharge module |
| `discharge_access_tokens` | Discharge module |
| `lens_analyses` | Lens module |
| `medications` | Clinical prescribing (all modules) |
| `forecast_predictions` | Forecast module (LSTM) |
| `hospital_forecasts` | Forecast module |
| `admission_history` | Forecast module (Poisson model) |
| `surgeries` | Theatre / Surgery module |
| `surgery_team_assignments` | Theatre / Surgery module |
| `surgery_prep_requests` | Theatre / Surgery module |
| `ingestion_events` | Ingestion service / HL7 adapter |
| `observations` | Ingestion service |
| `audit_logs` | `audit_service` (all modules write; none may update/delete) |
| `event_log` | All modules (Vigil, Relay, Sage, Scribe) |
| `schema_version` | `init_db()` only |
| `sage_room_memory` | Sage Ambient |
| `sage_observations` | Sage Ambient |
| `sage_actions` | Sage Ambient |
| `sage_action_events` | Sage Ambient |
| `sage_handoffs` | Sage Ambient |
| `sage_handoff_events` | Sage Ambient |
| `sage_decision_cache` | Sage Ambient (background preload task) |
| `helix_medication_master` | Helix / Pharmacy |
| `helix_inventory_lots` | Helix / Pharmacy |
| `helix_patient_med_orders` | Helix / Pharmacy |
| `helix_pharmacy_verifications` | Helix / Pharmacy |
| `helix_stock_movements` | Helix / Pharmacy |
| `helix_transitions` | Helix / Pharmacy |
