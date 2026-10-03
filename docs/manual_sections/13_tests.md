## 13. The Test Suite (pytest)

This section is the complete reference for Orb's automated test suite. Read it before you change any module, and re-read the relevant sub-section after you finish. The goal is that you can answer "which tests should I run after touching X?" without asking anyone.

---

### 13.1 Plain-English Glossary

Before anything else, here are the concepts the test code relies on, explained without assuming you have a testing background.

**pytest**, the test runner. You point it at a directory, it collects every file named `test_*.py`, finds every function named `test_*` or method inside a class whose name starts with `Test`, runs them, and reports pass/fail. It is the industry standard for Python.

**assertion**, a line like `assert x == 42`. If the condition is false, pytest marks the test as failed and prints what the actual value was. Assertions are how tests communicate "this is what I expect."

**fixture**, a reusable setup-and-teardown function. Instead of writing the same database-insert or HTTP-login code in every test, you write it once as a fixture and pytest injects it wherever it is declared as a parameter. After the test finishes, any teardown code in the fixture (after its `yield` statement) runs automatically.

**autouse fixture**, a fixture that runs for every test in its scope *automatically*, without any test needing to ask for it by name. Orb uses autouse fixtures to guarantee a clean database state before every single test.

**scope**, how long a fixture lives. `scope="session"` means it runs once for the entire test run (e.g. creating the temp database). The default scope (no argument) means it runs once per test function.

**conftest.py**, a special file pytest looks for automatically. Fixtures defined in `conftest.py` are available to every test file in the same directory (and subdirectories) without needing to import them. Think of it as the "shared toolbox" for the whole suite.

**test isolation**, the property that each test starts with a known, predictable state and does not leave pollution that breaks the next test. Without isolation, test A passing can cause test B to fail, which makes bugs very hard to diagnose.

**parametrized test**, one test function that runs multiple times, once for each set of parameters. Instead of writing 11 nearly identical functions to verify 11 boundary values of a scoring algorithm, you write one function with a `@pytest.mark.parametrize` decorator and a list of `(input, expected_output)` tuples. Pytest runs each tuple as a separate test case.

**mock / patch**, replacing a real function or object with a controlled fake for the duration of one test. Used heavily in Orb to avoid hitting a real GPU or a real Ollama server during CI. `unittest.mock.patch` is the standard Python tool for this.

**FK constraint**, "foreign key constraint." When a table row references another table (e.g. `scribe_notes.author_id` points at `users.id`), SQLite will reject the insert if the referenced row does not exist. This is why the test suite seeds a known set of users before every test.

---

### 13.2 How to Run the Tests

#### 13.2.1 Required environment variables

| Variable | Value for tests | Why it matters |
|---|---|---|
| `ORB_DEPLOYMENT_PROFILE` | `dev` | Selects the 4B model branch in config; prevents the suite from trying to load the 27B model. |
| `ORB_JWT_SECRET` | Any non-empty string (e.g. `test-secret`) | The auth module requires a secret to sign JWTs. Without it, every login test fails with a configuration error. |
| `ORB_SKIP_KNOWLEDGE_BASE` | `1` | Skips loading the RAG knowledge base at startup. The knowledge base requires disk assets not present in CI. |
| `ORB_DB_PATH` | Leave unset, conftest sets it | See §13.3. Do not set this yourself unless you are deliberately targeting a specific database file. |

#### 13.2.2 The exact command

> **Corrected 2026-08-15.** The repository path in this block was stale (`/Users/samuelmorningstar/Orb Project`), and `python` must be the venv interpreter or the suite runs against the wrong environment.

From the repository root:

```bash
/Users/samuelmorningstar/Documents/Projects/Orb/server/.venv/bin/python -m pytest server/tests/<yours> -q
```

`server/pytest.ini` pins the collection root to `tests/` (keeping ad-hoc scripts under `scripts/` and `dev_tools/` out) and sets `asyncio_mode = auto`, before that setting existed, four async tests were silently **skipped** rather than executed.

Add `-x` to stop on the first failure. Add `-k test_news2` to run only tests whose name matches.

> **Run your own test files, not the whole suite.** 6,161 tests is not a fast loop, and the working convention on this project is to run the files you touched plus the ones that cover the invariant you changed.

**`conftest.py` sets most of the environment for you** (`server/tests/conftest.py:24-61`): `ORB_DB_PATH` (a throwaway file, see §13.3), `ORB_KNOWLEDGE_INPROCESS=1`, `ORB_WATCHDOG_DIR` and `ORB_FHIR_EXPORT_DIR` (temp dirs), each guarded so a test that needs its own value can override it. There is also a live guard that refuses to run against the demo DB if `ORB_DB_PATH` is misconfigured (`:159-162`).

#### 13.2.3 Current status

**6,161 tests collected across 181 files** (`pytest --collect-only`, verified 2026-08-15).

> **Correction.** This section previously said **"285 tests passing"** and told the reader that fewer than 285 meant a test file had been deleted. That figure was out by a factor of ~22 and had been wrong for a long time, it is the clearest single example of rot in this manual. Treat any count written here as needing re-verification, not as a target.

**A note the count does not capture.** A green suite is not evidence of correctness for a failure class its fixtures cannot express. Two defects found by execution rather than by the suite are worth remembering: `except sqlite3.IntegrityError` was dead code on every encrypted deployment while the unencrypted suite stayed green (§2.10b), and the audit trail recorded a NULL actor for every browser mutation while the chain kept verifying (§10.5a). **A test must assert the invariant and must be proven able to fail**, break the code, watch it go red, then fix it.

---

### 13.3 The Test Database Isolation Model

This is the most important concept to understand before writing or modifying any test.

#### The problem it solves

When the server starts, `config.py` reads `ORB_DB_PATH` from the environment (defaulting to `orb.db` in the server directory). `database.py` binds to that path at import time. If the test suite ran against `orb.db`, every `INSERT` and `DELETE` in a test would corrupt the live demo database. This happened before the current isolation model was added, and it wiped demo data.

#### Why not an in-memory database?

SQLite's `:memory:` mode creates a database that lives only for a single connection. The Orb server opens multiple short-lived connections via `get_db()`, one per request. An in-memory database is not shared across connections, so the schema that one connection creates is invisible to the next. A file-backed database on disk *is* shared across all connections to the same file.

#### How conftest.py solves it

`conftest.py` runs before any test file is imported. At the top of the file, before any `from database import ...` can happen:

```python
if not os.environ.get("ORB_DB_PATH"):
    _tmp = tempfile.NamedTemporaryFile(prefix="orb_test_", suffix=".db", delete=False)
    _tmp.close()
    os.environ["ORB_DB_PATH"] = _tmp.name
```

This creates a uniquely named temporary file like `/tmp/orb_test_a1b2c3d4.db` and sets `ORB_DB_PATH` to point at it. Because this runs before anything else, when `config.py` is later imported (as a side-effect of importing `database.py`, which is imported by `main.py`, which is imported by the test files), it reads the temp path from the environment. Every database call for the entire test run goes to the throwaway file.

The prefix `orb_test_` is the safety key, see below.

#### The safety assertion

The session-scoped `init_test_db` fixture contains this guard:

```python
assert "orb_test_" in os.path.basename(str(DB_PATH)) or str(DB_PATH) == ":memory:", (
    f"Refusing to run tests against non-test DB at {DB_PATH}. "
    "Set ORB_DB_PATH to a temp/test database."
)
```

If `ORB_DB_PATH` is accidentally set to point at `orb.db` or any other production-looking path, pytest aborts immediately with a clear error message. The suite never silently runs against real data.

After the safety check, `init_test_db` calls `init_db()` to create the full schema in the throwaway file, then seeds the four baseline test users.

#### Teardown

At the end of the session, `init_test_db` deletes the temp file:

```python
if "orb_test_" in os.path.basename(db_file) and os.path.exists(db_file):
    os.unlink(db_file)
```

---

### 13.4 `conftest.py`: Shared Fixtures in Detail

**File:** `server/tests/conftest.py`

#### `init_test_db` (session scope, autouse)

Runs once for the entire pytest session. Responsibilities:
1. Verifies the active `DB_PATH` has the `orb_test_` prefix (safety assertion).
2. Calls `init_db()` to build the full schema.
3. Seeds four users, `D001` (doctor), `D002` (doctor), `N001` (nurse), `DOC001` (doctor), all with password `password`. These satisfy the `author_id` FK in tables like `scribe_notes`.
4. On session teardown, deletes the temp file.

#### `_reseed_users` (per-test, autouse)

Runs before **every single test function**. Some tests (particularly `test_hospital_nervous_system.py` and `test_optimized_agentic_orders.py`) do a complete table wipe including the `users` table. Without this fixture, those tests would leave the database empty of users, and subsequent tests that try to insert a `scribe_note` with `author_id = 'DOC001'` would fail with a FK constraint violation.

The `_SEED_USERS_HASH` module-level variable caches the bcrypt hash so that `hash_password("password")` is only called once per session rather than once per test (bcrypt is intentionally slow).

**Pattern:**
```python
@pytest.fixture(autouse=True)
def _reseed_users():
    # ... inserts 4 users with INSERT OR IGNORE ...
    yield  # test runs here
    # no teardown needed
```

`INSERT OR IGNORE` means the insert is a no-op if the user already exists, safe to call repeatedly.

---

### 13.5 Module-to-Test Map

Use this table to find which test to run after editing a given module.

| Module / file you changed | Test file(s) to run |
|---|---|
| `services/vigil_service.py` (`compute_news2`, `compute_mews`) | `test_news2.py` |
| `services/ingestion_adapters.py` | `test_ingestion.py` |
| `routes/ingestion_routes.py`, `services/ingestion_service.py` | `test_audit.py` |
| `services/sage_handoff_service.py`, `routes/relay_routes.py` | `test_handoff.py` |
| `services/event_bus.py`, `services/nervous_system_triggers.py` | `test_hospital_nervous_system.py` |
| `services/gpu_coordinator.py` | `test_gpu_coordinator.py` |
| `services/lens_service.py`, `config.py` (VISION_MODEL) | `test_lens.py` |
| `permissions.py`, `routes/*.py` (auth middleware) | `test_rbac.py` |
| `routes/discharge_routes.py`, `services/discharge_service.py` | `test_discharge_access.py` |
| `services/scribe_service.py`, `routes/scribe_routes.py`, `services/scribe_providers.py` | `test_scribe_pipeline.py` |
| `services/rxnorm_reconciler.py`, `routes/relay_routes.py` (agentic confirm) | `test_optimized_agentic_orders.py` |
| `database.py` (schema changes) | All files, run the full suite |
| `config.py` (env-var names, defaults) | `test_scribe_pipeline.py`, `test_lens.py`, full suite recommended |
| `auth.py` | `test_rbac.py`, `test_audit.py` |

---

### 13.6 `test_audit.py`

**Module guarded:** `routes/ingestion_routes.py`, `routes/auth_routes.py`, `services/ingestion_service.py`, `database.py` (`audit_logs` table)

**What it tests:** That every significant action, data ingestion, failed ingestion, duplicate detection, critical-alert generation, login success, login failure, is correctly written to the `audit_logs` table with the right `action_type`, `correlation_id`, and `outcome`. It also verifies that the `/api/audit/logs` endpoint is admin-only and returns the correct records.

#### The `audit_logs` append-only problem

`audit_logs` is protected by two SQLite triggers that fire `RAISE(ABORT, ...)` on any `UPDATE` or `DELETE`. This is a security feature, once an audit record is written, it cannot be modified or erased. But tests need a clean table between runs. The `clear_audit_logs()` helper solves this by:

1. Dropping both triggers.
2. Running `DELETE FROM audit_logs`.
3. Recreating the triggers.

This approach is explicit and safe, the triggers are restored immediately after the wipe, so the protection is never left off for longer than the duration of the `clear_audit_logs()` call.

The `setup_teardown` fixture (autouse, per-test) calls `clear_audit_logs()` both before and after each test, and also seeds two test patients.

#### Key test cases

| Test | What it asserts |
|---|---|
| `test_json_ingestion_lifecycle` | A valid JSON payload produces audit entries: `ingestion_request_received`, `ingestion_parse_success`, `vital_inserted`, `news2_recomputed`, all sharing the same `correlation_id`. |
| `test_hl7_ingestion_lifecycle` | Same lifecycle for an HL7 ORU message. |
| `test_malformed_payload_parse_failure` | A bad payload produces `ingestion_request_received` + `ingestion_parse_failure`. Even failures are audited. |
| `test_duplicate_payload_detected` | Sending the same payload twice produces `ingestion_duplicate_detected` on the second call. |
| `test_alert_generation` | A critically abnormal payload (HR 150, SBP 70, SpO2 85) produces `vigil_alert_generated` and `relay_alert_generated`. |
| `test_login_success` | Successful login writes `login_success` to audit with `actor_user_id` and `outcome = "success"`. |
| `test_login_failure` | Failed login writes `login_failure` with `outcome = "failure"` and the attempted ID in `metadata_json`. |
| `test_audit_query_endpoint` | An admin user can query `/api/audit/logs?correlation_id=...` and get back the relevant records. |

**A failure here means:** audit trail integrity is broken. In a clinical system, this is a regulatory issue, missing or wrong audit records can violate HIPAA access log requirements. Treat these failures as critical.

**⚠️ If you change this:** If you rename an `action_type` event string (e.g. change `"vital_inserted"` to `"vital_written"`), every assertion in `test_json_ingestion_lifecycle` and `test_hl7_ingestion_lifecycle` that checks that string will fail. Search for the old string in both the source file and the test file before renaming. If you add a new event type to the ingestion pipeline, add an assertion for it here.

---

### 13.7 `test_discharge_access.py`

**Module guarded:** `routes/discharge_routes.py` (or equivalent), `services/discharge_service.py`, `config.py` (`DISCHARGE_ACCESS_MAX_ATTEMPTS`)

**What it tests:** The patient-facing discharge access flow. After discharge, a patient receives a QR code containing a `token_id`. They redeem it by providing their date of birth. This module ensures the access control around that flow is correct.

#### Fixtures

**`auth_headers`**, logs in as `DOC001` (seeded by conftest) and returns the bearer token header. Used wherever a doctor needs to be authenticated.

**`test_patient`**, creates patient `PAT001` with user `RBAC-PAT` and date of birth `1990-01-01`. Returns the patient row.

**`setup_discharge`**, builds a complete discharge scenario: sets `PAT001` to admitted, generates a `token_id`, stores the `SHA-256(token_id)` hash in `discharge_access_tokens`, inserts a discharge summary, and flips the patient to `discharged`. Returns the `token_id`. Note that the token hash is computed correctly, an earlier version used a bogus literal `"test_hash_123"` which left the hashing/lookup path unexercised.

#### Key test cases

| Test | What it asserts |
|---|---|
| `test_verify_discharge_success` | Correct token + correct DOB returns `status = "success"` and an `access_grant` JWT. That JWT can then be used to fetch the discharge summary. |
| `test_verify_discharge_invalid_dob` | Correct token + wrong DOB returns `401` with `reason = "invalid_dob"`. |
| `test_verify_discharge_lockout` | After `DISCHARGE_ACCESS_MAX_ATTEMPTS` failed tries, the next attempt returns `401` with `reason = "locked"`. The lockout count is read from `config.py` so it stays in sync with the real limit. |
| `test_verify_discharge_expired` | The test manually backdates the token's `expires_at` to yesterday, then verifies the endpoint returns `401` with `reason = "expired"`. |
| `test_verify_discharge_unknown_token_rejected` | A token_id that was never issued (not stored in the DB) is rejected with `401`, even with a valid DOB. Guards the hashing/lookup path. |
| `test_legacy_endpoint_removed` | `GET /api/vigil/patients/PAT001/discharge-summary/view` returns `404`, confirming an old insecure endpoint was not accidentally re-registered. |

**A failure here means:** a patient could access another patient's discharge records, or a lockout mechanism has been bypassed. Treat security failures in this file as critical.

**⚠️ If you change this:** If you change `DISCHARGE_ACCESS_MAX_ATTEMPTS` in `config.py`, the lockout test automatically uses the new value (it reads from config at runtime). If you change the token hashing scheme (currently SHA-256), update the hash computation in `setup_discharge` to match. If you add a new error reason code, add a test for it.

---

### 13.8 `test_gpu_coordinator.py`

**Module guarded:** `services/gpu_coordinator.py`, specifically `VramGuard` (context manager) and `MetalQueueCoordinator` / `umsq` (priority-aware async GPU lock)

**What it tests:** That the GPU resource management layer behaves correctly in isolation, without needing a real GPU. All hardware calls are mocked.

This file does not use a FastAPI test client or a database, it tests pure Python classes directly.

#### Test classes

**`TestVramGuard`**, tests the `vram_guard` context manager:
- `test_basic_execution`, the wrapped block runs and returns its value.
- `test_gc_collect_called`, `gc.collect()` is called on exit (frees Python memory).
- `test_mps_cache_flush_called`, `torch.mps.empty_cache()` is called if a mock `torch` module is injected.
- `test_mlx_cache_flush_called`, `mx.metal.clear_cache()` is called if a mock `mlx.core` module is injected.
- `test_exception_propagation`, exceptions from inside the block are re-raised (not swallowed).
- `test_cleanup_runs_on_exception`, cleanup still runs even if the block raises. This is the critical case: a GPU crash must not leave the cache dirty.
- `test_no_crash_without_torch_or_mlx`, if neither framework is installed, `vram_guard` completes without error (graceful no-op).

**`TestMetalQueueCoordinator`**, tests the async priority queue:
- `test_serialization`, two concurrent GPU tasks are never overlapping. The test schedules task A and B simultaneously and asserts the execution log shows A ending before B starts (or vice versa). This is the core correctness guarantee: the GPU is never double-booked.
- `test_priority_ordering`, while a blocker holds the GPU, three tasks queue up: one at `PRIORITY_SAGE` (P1), one at `PRIORITY_SCRIBE` (P2), one at `PRIORITY_BACKGROUND` (P3). When the blocker releases, they must execute in that exact order.
- `test_exception_releases_lock`, an exception inside an acquired context releases the GPU lock. Without this, a single crashed inference call would deadlock every future GPU request.
- `test_basic_acquire_release`, `is_busy` and `active_task` reflect the correct state.

**`TestPriorityConstants`**, verifies the numeric values: `PRIORITY_SAGE == 1`, `PRIORITY_SCRIBE == 2`, `PRIORITY_BACKGROUND == 3`, and that lower numbers mean higher priority (`SAGE < SCRIBE < BACKGROUND`).

**`TestSingleton`**, confirms the module-level `umsq` instance is a `MetalQueueCoordinator` and can be acquired normally.

Each `TestMetalQueueCoordinator` test creates a fresh `MetalQueueCoordinator()` instance (not the global singleton) to avoid cross-test state leakage.

**A failure here means:** two AI modules could run on the GPU simultaneously, causing out-of-memory crashes or corrupted model outputs. On the 8GB Mac Mini this is particularly severe, even a brief overlap can crash the process.

**⚠️ If you change this:** If you rename priority constants or change their numeric values, update `TestPriorityConstants` immediately. If you change the queuing algorithm, the serialization and priority-ordering tests will catch regressions.

---

### 13.9 `test_handoff.py`

**Module guarded:** `services/sage_handoff_service.py`, `routes/relay_routes.py` (transition endpoint), `services/helix_db.py`

**What it tests:** The "Stage 4" downstream handoff, when a doctor confirms a Sage action (prescribe, document, transfer, escalate), the handoff service fans out to the correct downstream module (Helix, Scribe, Admissions). This file tests both the service function directly and the HTTP transition endpoint.

#### `setup_handoff_data` fixture (autouse, per-test)

Seeds a complete isolated environment: test doctor `D-HANDOFF-DOC`, two patients (`PAT-HANDOFF-1`, `PAT007`), two case-room channels, a medication in the Helix master catalog with stock, an active admission per patient, and a pre-seeded emergency surgery for `PAT007`. After the test, deletes all created rows. This is a full teardown (not just `DELETE OR IGNORE`), tables like `sage_handoffs` and `sage_actions` are completely emptied.

#### Key test cases

| Test | Action type | Expected downstream | Key assertion |
|---|---|---|---|
| `test_create_and_dispatch_handoff_prescribe_success` | `prescribe` | Helix | `helix_patient_med_orders` row created with correct `patient_id`, `route` parsed from rationale (e.g. "IV"), `status = "pending"`. Handoff `status = "accepted"`. |
| `test_create_and_dispatch_handoff_document_success` | `document` | Scribe | `scribe_notes` row created with `author_id = "D-HANDOFF-DOC"`, target in `raw_transcript`, `status = "draft"`. |
| `test_create_and_dispatch_handoff_transfer_success` | `transfer` | Admissions | Active admission's `department` updated to the transfer target. New surgery scheduled with `status = "emergency_pending"`. A system message posted to the case room. |
| `test_create_and_dispatch_handoff_transfer_special_patient_007` | `transfer` | Admissions | For `PAT007`, the pre-seeded surgery `SURG-ALEX-EM` is transitioned to `"emergency_ready"` rather than creating a new one. |
| `test_create_and_dispatch_handoff_prescribe_failure_missing_med` | `prescribe` | Helix | Drug not in Helix catalog → handoff `status = "failed"`, `error_summary` contains "not found in Helix master catalog". The original `sage_action` stays `confirmed` (no rollback). |
| `test_create_and_dispatch_handoff_unsupported_action` | `escalate` | n/a | Falls through with `status = "unsupported"` and "No downstream route for action_type" in `error_summary`. |
| `test_create_and_dispatch_handoff_idempotency` | `document` | Scribe | Calling `create_and_dispatch_handoff` twice with the same action returns the same `handoff.id`. Only one `scribe_notes` row is created. |
| `test_relay_routes_transition_endpoint` | `document` (via HTTP) | Scribe | `POST /api/relay/channels/{room}/sage_actions/{id}/transition` returns `status = "ok"`, `action.state = "confirmed"`, `handoff.status = "accepted"`, and the Scribe note is in the database. |

**A failure here means:** a confirmed doctor action is not propagated to the downstream system (pharmacy, notes, admissions). Prescriptions would not reach Helix; clinical notes would not be created; transfers would not be recorded.

**⚠️ If you change this:** If you add a new `action_type` (beyond `prescribe`, `document`, `transfer`, `escalate`), add a test case that confirms its downstream routing. If you rename the `confirmed` state, the transition endpoint test will break. If you change the IV/route parsing logic in the prescribe handler, the `assert order["route"] == "IV"` assertion will fail.

---

### 13.10 `test_hospital_nervous_system.py`

**Module guarded:** `services/event_bus.py` (`LocalEventBus`, `StateEvent`), `services/nervous_system_triggers.py`

**What it tests:** The internal event-routing layer, publish/subscribe, wildcard topic matching, database durability, and the three reactive triggers that wire modules together.

**Important:** this file uses `@pytest.mark.asyncio`, the tests are `async def` functions. They call `await event_bus.publish(event)` and then `await asyncio.sleep(0.3)` to let background async tasks complete before asserting.

#### `setup_hns_test_data` fixture (autouse, per-test)

This fixture is more aggressive than others: it turns off FK enforcement (`PRAGMA foreign_keys = OFF`), deletes every row from every major table in the database (a list of 35+ tables), then turns FK enforcement back on and inserts fresh baseline data. This nuclear wipe is necessary because the event bus triggers may touch many tables (e.g. `sage_decision_cache`, `medications`, `vitals`) and any leftover rows from a previous test would produce false-positive or false-negative assertions.

#### Key test cases

**`test_event_bus_pub_sub_wildcard`**

Registers two subscribers: one for the exact topic `"vigil.alert.critical"`, one for the wildcard `"vigil.alert.*"`. Publishes a `StateEvent` with topic `"vigil.alert.critical"`. Asserts:
- Both callbacks received the event.
- The event was durably written to `event_log` in the database (with matching `event_id`, `topic`, `source_node`, `correlation_id`).

This verifies both the in-memory routing and the write-ahead log (WAL) durability guarantee.

**`test_trigger_vigil_alert_preloads_sage_cache`**

When a `vigil.alert.critical` event is published, a background trigger should compile a Sage decision context (patient history, guidelines) and write it to `sage_decision_cache`. The test:
1. Publishes the event.
2. Waits 0.3 seconds (the trigger runs in a thread).
3. Asserts `sage_decision_cache` has a row for `PAT-HNS-1` with `mews_score = 0.85` and a `compiled_context` JSON that has `channel_a_facts` and `channel_b_c_guidelines`.

**`test_trigger_scribe_note_signed_syncs_ehr`**

This is a **fail-closed regression guard**. When a Scribe note is signed, a trigger previously injected hardcoded fake medications and vitals, a patient-safety hazard that was removed. This test:
1. Mocks the LLM queue and the HTTP client (Ollama is not available in tests).
2. Inserts a draft SOAP note with clinical content.
3. Publishes a `scribe.note.signed` event.
4. Waits 0.5 seconds.
5. Asserts that **no** medication or vitals rows were created for the patient.

The assertion is `assert med is None`, the test passes when nothing was written. If the fabrication bug is re-introduced, this test fails.

**A failure here means:**
- `test_event_bus_pub_sub_wildcard` failing → the event bus topic routing is broken. Modules that subscribe to events will not receive them.
- `test_trigger_vigil_alert_preloads_sage_cache` failing → critical-patient alerts no longer pre-warm the AI context, adding latency at the worst possible moment.
- `test_trigger_scribe_note_signed_syncs_ehr` failing → fabricated clinical data may be entering the patient record. This is a patient-safety issue.

**⚠️ If you change this:** If you add a new event topic, register a test here that verifies the corresponding trigger fires. If you rename a topic string (e.g. `"vigil.alert.critical"` → `"vigil.critical.alert"`), all subscribers that used the old string will stop receiving events, and this test will catch it.

---

### 13.11 `test_ingestion.py`

**Module guarded:** `services/ingestion_adapters.py`, `HL7Adapter`, `JSONAdapter`, `CSVAdapter`, `compute_fingerprint`, `map_to_vital_key`, `normalize_temp_to_celsius`, `select_adapter`

**What it tests:** The parsing and normalization layer that converts raw hospital data feeds (HL7 v2.5 ORU messages, JSON webhooks, CSV replay files) into the internal `CanonicalEvent` format.

This file uses `unittest.TestCase` (the older Python test style) rather than plain pytest functions, both styles work under pytest.

**Fixture files required:** This file relies on three fixture files under `server/scripts/fixtures/`:
- `sample.hl7`, a multi-OBR, multi-OBX HL7 message with 6 vitals and 5 lab values.
- `sample.json`, a JSON webhook with 6 vitals and 1 lab value.
- `sample.csv`, a CSV replay file with 14 rows.

If these files are missing, the tests fail with explicit messages like "Required fixture missing: ... the HL7 ingestion path (partner-hospital onboarding) must be exercised, not skipped." They must not be deleted.

#### Test classes

**`TestVitalKeyMapping`**, verifies LOINC codes and human-readable names map to internal keys (`"HR"`, `"SBP"`, `"SpO2"`, `"Temp"`, `"RR"`, `"DBP"`). Checks that LOINC code takes priority over name when both are supplied, and that unknown codes return `None`.

**`TestTempNormalization`**, verifies Fahrenheit-to-Celsius conversion (98.6°F → 37.0°C) and that Celsius values pass through unchanged.

**`TestFingerprinting`**, verifies the deduplication hash: same payload → same hash; different payload → different hash.

**`TestAdapterSelection`**, verifies `select_adapter()` correctly identifies the format from the payload string (HL7 starts with `"MSH|"`, JSON starts with `{`, CSV has a known header).

**`TestHL7Adapter`**, parses `sample.hl7` and verifies: 11 total OBX events, 6 vitals + 5 labs, all with `patient_identifier = "PAT001"`, 2 distinct OBR groups, HR value = 112.0. Also tests edge cases: missing PID raises `ParseError`, malformed OBX segments are skipped without crashing, OBX without a preceding OBR still parses.

**`TestJSONAdapter`**, parses `sample.json` and verifies: 7 total events (6 vitals + 1 Hemoglobin lab), missing `patient_id` raises `ParseError`, empty `observations` array raises `ParseError`, numeric strings like `"88"` parse to `88.0`.

**`TestCSVAdapter`**, parses `sample.csv` and verifies: 14 events, all with `source_type = "csv_replay"`, vital mapping still works (LOINC code in the `code` column).

**`TestSourceLabeling`**, asserts `adapter.source_type` is `"hl7"`, `"json"`, or `"csv_replay"` respectively. This value ends up in `audit_logs` and is how operations staff trace which hospital feed produced a given record.

**A failure here means:** hospital data is being dropped, misclassified (vital stored as lab or vice versa), or misidentified (LOINC code not mapping to internal key). Downstream NEWS2 scoring would produce incorrect risk scores.

**⚠️ If you change this:** If you add a new LOINC code mapping in `ingestion_adapters.py`, add a test for it in `TestVitalKeyMapping`. If you add a new adapter format, add a `TestXxxAdapter` class. If you change the fingerprinting algorithm, `test_duplicate_payload_detected` in `test_audit.py` may also need updating (it relies on the same fingerprint logic to produce a duplicate).

---

### 13.12 `test_lens.py`

**Module guarded:** `services/lens_service.py` (`analyze_image_stream`, `analyze_image_full`), `config.py` (`LLM_MODEL`, `VISION_MODEL`)

**What it tests:** The Lens medical image analysis service and the architectural guarantee that Sage and Lens share a single model instance.

#### `test_lens_service_calls_correct_model`

Uses a 1×1 transparent PNG as a dummy image (stored as a base64 literal, no file loading required). Mocks `httpx.AsyncClient` to intercept the Ollama API call and assert:
- The HTTP method is `POST` and the URL ends with `/api/chat`.
- `json_payload["model"] == "medgemma:27b"`, the correct model is named.
- `keep_alive` is set (model stays loaded in memory between calls).
- `options` is set (inference parameters are passed).
- The first message has `role = "system"` and the prompt contains `"FINDINGS:"` (enforcing the structured output format).
- The user message contains `"images"` with exactly one image.

The mock response streams back four JSON lines that produce `"FINDINGS: ... IMPRESSION: ... Normal cardiac silhouette ..."`. The test verifies both the stream interface (`analyze_image_stream`) and the full-text wrapper (`analyze_image_full`).

#### `test_single_shared_model_instance`

```python
assert config.LLM_MODEL == config.VISION_MODEL
assert "medgemma" in config.LLM_MODEL.lower()
```

This is an architectural constraint: the Mac Studio has ~17GB of VRAM. Running `medgemma:27b` for Sage (text) and a separate vision model for Lens simultaneously would exceed the budget. The solution is that both point to the same `medgemma:27b` tag, Ollama loads it once and serves both. If anyone changes `VISION_MODEL` to a different model tag, this test fails and the VRAM budget comment in the error message explains why.

**A failure here means:**
- `test_lens_service_calls_correct_model` → the Lens API call is malformed, pointing at the wrong model, or missing required parameters. Radiology image analysis would fail in production.
- `test_single_shared_model_instance` → a second model was loaded, doubling VRAM usage and likely causing OOM crashes on the Mac Studio.

**⚠️ If you change this:** If you change `LLM_MODEL` or `VISION_MODEL` in `config.py`, `test_single_shared_model_instance` will fail if they diverge. If you change the Ollama request format (e.g. rename a field), update the assertions inside the mock's `stream()` method.

---

### 13.13 `test_news2.py`

**Module guarded:** `services/vigil_service.py`, `compute_news2`, `compute_mews`

**What it tests:** Every scoring band and escalation rule of the NEWS2 (National Early Warning Score 2) algorithm. This is described in the file's docstring as a "release gate", the NEWS2 scorer is the live clinical decision logic for all patient risk scores while the ML models are not yet trained.

#### The baseline pattern

A module-level `NORMAL` dict holds a set of vitals that score 0 on every parameter:

```python
NORMAL = dict(hr=70, sbp=120, rr=16, temp=37.0, spo2=98, consciousness="alert", on_oxygen=False)
```

A helper function `score_with(**overrides)` merges overrides into `NORMAL` and calls `compute_news2`. This means every test isolates exactly one parameter change from a known-good baseline.

#### Parametrized band tests

Instead of 11 individual functions to test respiration rate, there is one:

```python
@pytest.mark.parametrize("rr,points", [
    (8, 3),    # <=8 bradypnea
    (9, 1), (11, 1),
    (12, 0), (16, 0), (20, 0),
    (21, 2), (24, 2),
    (25, 3), (30, 3),
])
def test_respiration_bands(rr, points):
    assert score_with(rr=rr)["raw_news2"] == points
```

This runs as 10 separate test cases. The same pattern covers SpO2, systolic BP, pulse rate, and temperature, every boundary in the published UK NEWS2 table is tested.

#### Key edge cases

**`test_single_param_3_escalates_to_elevated`**, a single parameter scoring 3 (e.g. HR 35 = severe bradycardia, which scores 3) must escalate the patient to at least `"elevated"` even if the total aggregate is only 3. This is the NEWS2 "single extreme parameter" rule.

**`test_score_three_without_single_param_3_stays_stable`**, a total of 3 built from three parameters scoring 1 each (no parameter scores 3) stays `"stable"`. Proves the escalation logic keys off the per-parameter score, not the aggregate.

**`test_normalized_index_does_not_flatten_high_scores`**, regression guard for a bug where the normalized `score` was computed as `min(1.0, raw/10)`, which meant a raw-10 and raw-20 patient both got `score = 1.0`. Now the normalized scale uses a true max aggregate so high-severity patients can be distinguished.

**`test_compute_mews_alias_matches_news2_for_shared_params`**, `compute_mews` is a deprecated alias that drops the `on_oxygen` and `consciousness` parameters. For equivalent inputs it must return the same `raw_news2` and `level`.

**`test_missing_parameters_are_skipped`**, passing `None` for every parameter returns `raw_news2 = 0` and `level = "stable"`. Missing vitals do not crash the scorer.

**A failure here means:** patients are being mis-triaged. An incorrect band boundary could mean a patient with a dangerously low respiratory rate is scored as stable, or a stable patient triggers a critical alert. This is the most clinically dangerous failure mode in the entire test suite.

**⚠️ If you change this:** If you adjust any threshold in `compute_news2` (e.g. change the respiration rate boundary from 8 to 7), the parametrized test for that parameter will catch it. If you add a new scoring parameter (e.g. urine output), add its band table here. If you change the level banding thresholds (currently `>=7 critical`, `>=5 OR any single param 3 → elevated`), the aggregate banding tests will fail.

---

### 13.14 `test_optimized_agentic_orders.py`

**Module guarded:** `services/rxnorm_reconciler.py`, `routes/relay_routes.py` (pre-flight and `confirm_agentic` endpoints), `services/helix_db.py` (pharmacy inventory)

**What it tests:** The agentic medication ordering flow, from fuzzy drug name recognition through PIN authentication, inventory checks, high-risk flagging, and cryptographic audit trail generation.

#### `setup_agentic_order_data` fixture (autouse, per-test)

Performs a full table wipe (same aggressive approach as `test_hospital_nervous_system.py`), then seeds:
- Two doctors with PINs: `D-PRIMARY-DOC` (PIN `1234`) and `D-COSIGN-DOC` (PIN `5678`).
- One patient with an active channel.
- Four medications in the Helix master catalog: Epinephrine (high-risk, in stock), Amoxicillin 500mg (standard, in stock), Cefadin 500mg (generic substitute, in stock), Amoxicillin 250mg (out of stock, quantity = 0).
- Corresponding inventory lots.

#### Key test cases

| Test | What it asserts |
|---|---|
| `test_formulary_reconciliation` | Exact name `"Amoxicillin 500mg Oral Capsule"` resolves to `MED-AMOX` with score ≥ 0.95. ASR misspelling `"amoxcilin"` still resolves to `MED-AMOX` with score ≥ 0.8 (Double Metaphone + Levenshtein fuzzy match). |
| `test_pre_flight_checks` | Pre-flight endpoint for a misspelled drug returns `resolved_medication.id = "MED-AMOX"`, `stock_status.in_stock = true`, `stock_status.total_quantity = 20`, `is_high_risk = false`. |
| `test_pre_flight_out_of_stock_substitute` | When Amoxicillin 500mg is manually set to quantity 0, pre-flight flags `in_stock = false` and returns `alternative_suggestion.id = "MED-CEFADIN"` (same generic). |
| `test_confirm_standard_medication_success` | Standard drug confirmed with correct PIN: returns `status = "ok"`, `order_id`, `correlation_id`, `delegation_hash`. The `sage_action` state → `"confirmed"`, a `sage_action_events` row is created, a `helix_patient_med_orders` row exists, and the audit log contains the delegation hash. |
| `test_confirm_high_risk_medication_success` | Epinephrine (high-risk) confirmed with a single doctor's PIN succeeds immediately without requiring a co-signer. Returns `status = "ok"` and an `order_id`. |

**A failure here means:** drug name recognition is broken (an ASR-misheard drug name won't resolve to the correct formulary entry); a medication order is placed without the required PIN; inventory is not checked before dispensing; the cryptographic audit chain is incomplete.

**⚠️ If you change this:** If you add a new drug to the Helix catalog in the test fixture, make sure out-of-stock substitution tests still point at `MED-CEFADIN`. If you change the PIN hashing algorithm, update `_create_test_user` here. If you rename the `confirm_agentic` endpoint, the URL in `test_confirm_standard_medication_success` will return 404.

---

### 13.15 `test_rbac.py`

**Module guarded:** `permissions.py`, auth middleware in `routes/*.py`, every protected API endpoint

**What it tests:** Role-Based Access Control, that each role (admin, doctor, nurse, patient) can access exactly the routes it should, and is blocked from routes it should not.

#### `setup_users` fixture (autouse, per-test)

Creates four test users (one per role) with `INSERT OR IGNORE`:
- `RBAC-DOC` (doctor)
- `RBAC-NRS` (nurse)
- `RBAC-ADM` (admin)
- `RBAC-PAT` (patient, linked to patient record `PAT-RBAC-1`)

Does not delete them on teardown (comment: "they may be needed by other tests in the same run").

#### Test groups

**Admin can access governance routes**, `GET /api/audit/logs`, `GET /api/admin/overview`, `GET /api/ingest/status` all return `200` for admin.

**Doctor/nurse cannot access admin routes**, same routes return `403` with `detail.error = "permission_denied"` and `detail.missing` listing the required permission string. This verifies the error format as well as the status code.

**Patient role restrictions**, patient cannot list all patients, cannot access Sage query, cannot access Relay channels, cannot access hospital forecast.

**Clinician workflows still work**, doctor and nurse can list patients and access forecast. Nurse cannot discharge a patient (no `discharge.generate`). Admin cannot prescribe medication (no `medication.write`).

**Permission denial generates audit entry**, triggering a 403 creates a row in `audit_logs` with `action_type = "permission_denied"`, `actor_user_id`, `actor_role`, `outcome = "denied"`, `event_category = "authorization"`, and `missing_permissions` in `metadata_json`. This verifies the audit trail captures not just successful actions but also blocked attempts.

**Bridge patient self-access**, patient can access `/api/bridge/status` (their own portal); doctor cannot (no `bridge.read_own`).

**Unauthenticated**, no token returns 401 or 403 on protected routes.

**A failure here means:** access control is broken. A patient could query the Sage AI about other patients; a nurse could discharge patients; an admin could prescribe medications. These are serious clinical and regulatory violations.

**⚠️ If you change this:** If you add a new route and protect it with a permission, add two tests: one showing the role that should have access gets `200`, one showing a role that should not gets `403`. If you change a permission string (e.g. `"audit.read"` → `"audit.logs.read"`), every test that checks `detail.missing` for the old string will fail.

---

### 13.16 `test_scribe_pipeline.py`

**Module guarded:** `services/scribe_service.py`, `services/scribe_providers.py`, `routes/scribe_routes.py`, `config.py` (SCRIBE dict, WHISPER_MODEL, LLM_MODEL), `permissions.py` (SCRIBE_* constants), `database.py` (scribe_notes schema)

This is the largest test file in the suite (~1066 lines). It covers the entire Scribe pipeline from config loading through note lifecycle state machine.

#### Test classes overview

**`TestScribeConfig`**, verifies `config.SCRIBE` has four stages (`asr`, `correction`, `draft`, `verify`), each with `model` and `provider`. Checks specific defaults (`asr.model = "large-v3"`, `correction.enabled = True`, `verify.enabled = False`). Checks backward-compat top-level vars `WHISPER_MODEL` and `LLM_MODEL` still exist.

**`TestScribeProviders`**, `get_provider("ollama")` returns an `OllamaProvider`; `get_provider("nonexistent")` raises `ValueError`. Tests `StageTimer`, start/stop/accumulate timings across multiple stages; result must be JSON-serializable.

**`TestScribeService`**, empty and whitespace transcripts pass through `correct_transcript` without calling the provider. `verify_note` returns `None` when verification is disabled (default). Patching `SCRIBE["correction"]["enabled"] = False` makes `correct_transcript` skip the provider.

**`TestPipelineMeta`**, `build_pipeline_meta(timer)` returns a dict with `timings`, `models`, and `stages_enabled` keys, is JSON-serializable, and round-trips through `json.dumps` / `json.loads`.

**`TestVerificationSchema`**, documents the expected shape of a verification report (not a code test, but a living specification): `verified` (bool), `confidence` (0–1), `warnings` (list), `omissions` (list), `sections_verified` (list). Warning severity must be `low/medium/high`.

**`TestDatabaseSchema`**, reads `database.py` source via `inspect.getsource` and asserts `"verification_json TEXT"` and `"pipeline_meta_json TEXT"` appear in the schema DDL. Also checks `save_scribe_note`'s signature includes both params. This catches schema drift where the code adds a column but the `CREATE TABLE` statement is not updated.

**`TestBackwardCompat`**, asserts function signatures: `transcribe_audio(audio_path, apply_correction, hints)`, `structure_transcript(transcript, format)`, `structure_transcript_stream` (async generator), `build_whisper_hints` (function), `get_scribe_notes` (function). Checks `STRUCTURING_PROMPTS` has `soap`, `discharge`, `handoff` keys and each contains `{transcript}`.

**`TestFixtures`**, verifies the `server/scripts/fixtures/scribe/` directory exists with at least 3 JSON files, each containing `case_id`, `transcript` (> 100 chars), `expected_entities` (≥ 5), and `expected_sections`.

**`TestAsyncVerificationService`**, tests the full async verification lifecycle in the database: save a note with `verification_json = {"status": "pending"}`, update to `complete`, verify the update persisted. Tests that a failed verification still leaves the note's `structured_note` and `raw_transcript` intact.

**`TestVerificationRouteConstants`**, imports and verifies constants from `routes/scribe_routes.py`: `VERIFY_NOT_REQUESTED`, `VERIFY_PENDING`, `VERIFY_COMPLETE`, `VERIFY_FAILED`, `_TERMINAL_STATES`, `_verification_registry`, `STALE_PENDING_THRESHOLD_MINUTES`. Also checks the `/api/scribe/notes/{note_id}/verification` and `/api/scribe/notes/{note_id}/verify` routes are registered.

**`TestVerificationStatusModel`**, structural tests on the JSON shapes for each verification state (not_requested, pending, complete, failed).

**`TestStateTransitionGuards`**, the most important tests in this class:
- `complete` cannot be overwritten by `failed` (stale task writes are ignored).
- `complete` cannot be overwritten by `pending` (retry does not reopen a done note).
- `failed` can be overwritten by `pending` (retry is allowed).
- `pending` can transition to `complete` or `failed`.
- Writing `complete` over `complete` is idempotent and does not corrupt.

**`TestDuplicateDispatchPrevention`**, verifies the in-memory `_verification_registry` correctly tracks and cleans up async tasks (finished tasks are removed from the registry on the next `_is_verification_in_flight` call).

**`TestStalePendingReconciliation`**, verifies `reconcile_stale_pending_verifications()`: a note stuck in `pending` for > `STALE_PENDING_THRESHOLD_MINUTES` is moved to `failed` with `"orphaned"` in the error. A recently-created pending note is untouched. A complete note is untouched.

**`TestNoteLifecycleSchema`**, verifies the `scribe_notes` table has all lifecycle columns: `status`, `version`, `supersedes_note_id`, `signed_by`, `signed_at`, `finalized_at`, `rejected_by`, `rejected_at`, `rejection_reason`, `last_edited_by`, `last_edited_at`, `ai_generated`. New note defaults: `status = "draft"`, `version = 1`, `ai_generated = 1`.

**`TestNoteLifecyclePermissions`**, imports `SCRIBE_EDIT_DRAFT`, `SCRIBE_SIGN`, `SCRIBE_REJECT` from `permissions.py` and checks their string values. Checks `has_permission` matrix: doctor can sign and reject; nurse cannot sign or reject; nurse can edit drafts; admin cannot edit, sign, or reject; patient has no scribe access.

**`TestNoteLifecycleTransitions`**, state machine tests via the service layer:
- Draft → signed (sets `signed_by`, `signed_at`).
- Draft → rejected (sets `rejected_by`, `rejected_at`, `rejection_reason`).
- Signed → direct edit blocked (`"Only drafts"` in error).
- Signed → second sign blocked.
- Rejected → sign blocked.
- Draft → edit succeeds (updates `structured_note`, sets `last_edited_by`).

**`TestNoteLifecycleVersioning`**, revision workflow:
- `create_revision_from_signed` on a signed note creates a new draft with `version = 2`, `supersedes_note_id = orig_id`. Original stays `signed`.
- Signing the revision moves original to `superseded` and revision to `signed`.
- Cannot revise a draft or a rejected note.

**`TestNoteLifecycleRoutes`**, asserts edit, sign, reject, and revise endpoints are registered on the scribe router.

**`TestNoteStatusFiltering`**, `get_scribe_notes(patient_id=None, status="draft")` returns only draft notes; `status="signed"` returns only signed.

**`TestVerificationStillWorks`**, regression guard that adding the lifecycle state machine did not break the async verification flow: updating `verification_json` on a draft note does not change the note's `status`; signing a note preserves its `verification_json`.

**A failure here means:** depending on the test class, either the Scribe config is wrong (ASR or LLM will use the wrong model/provider), the verification async flow is broken (notes will never complete verification), the lifecycle state machine has a bug (notes can be signed without authorization, or a signed note can be overwritten), or schema columns are missing (the server will crash on startup).

**⚠️ If you change this:** This file is the most tightly coupled to the codebase. Key change scenarios:
- **Rename `SCRIBE` config keys** → `TestScribeConfig` fails.
- **Change `WHISPER_MODEL` default** → `test_legacy_whisper_model_still_exists` fails.
- **Add/remove a stage** → `test_config_has_all_four_stages` fails.
- **Add a column to `scribe_notes`** → add it to `test_lifecycle_columns_exist`.
- **Change a permission string** → `TestNoteLifecyclePermissions` fails.
- **Change a lifecycle state name** → `TestNoteLifecycleTransitions` and `TestStateTransitionGuards` fail.
- **Rename a route** → `TestNoteLifecycleRoutes` fails.
- **Change `STALE_PENDING_THRESHOLD_MINUTES`** → `TestStalePendingReconciliation` may pass or fail depending on the new value.
- **Change `_TERMINAL_STATES`** → `test_terminal_states_defined` fails.

---

### 13.17 Common Troubleshooting

**"Refusing to run tests against non-test DB"**, `ORB_DB_PATH` in your environment is set to something without `orb_test_` in the filename. Unset it and let `conftest.py` create a temp file.

**"FK constraint failed" on a test that doesn't touch users**, the previous test wiped the `users` table (common in HNS and agentic order tests) and the `_reseed_users` autouse fixture didn't run because of an import error earlier in the session. Fix the import error first, then re-run.

**A single test fails but the full suite passes**, this usually means the test depends on ordering (shared state leaked from a previous test). Each test should be runnable in isolation with `pytest tests/test_xxx.py::test_function_name -v`. If it fails alone but passes in the suite, there is a hidden dependency, file a bug.

**`@pytest.mark.asyncio` tests hang**, `pytest-asyncio` must be installed. Check with `pip show pytest-asyncio`. Also ensure `asyncio_mode = "auto"` is set in `pytest.ini` or use the `@pytest.mark.asyncio` decorator explicitly.

**`test_lens_service_calls_correct_model` fails with `AssertionError: medgemma not in ...`**, `config.LLM_MODEL` is set to a non-MedGemma value. Check `ORB_DEPLOYMENT_PROFILE`, in `prod` profile the model may differ. Run with `ORB_DEPLOYMENT_PROFILE=dev`.

**"Required fixture missing: sample.hl7"**, the fixture files under `server/scripts/fixtures/` were deleted or moved. They are required for partner-hospital onboarding tests. Restore from git.
