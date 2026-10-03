## 7. Ingestion: HL7 / FHIR / CSV → Vitals → Scoring

This section explains how external patient data enters Orb, gets cleaned up, stored, and immediately triggers a NEWS2 risk score recompute. All three files work together as a pipeline: the **routes** file is the door, the **adapters** file is the translator, and the **service** file is the orchestrator that puts everything in the database and fires downstream systems.

---

### Background Concepts (Plain English)

**HL7 v2** is the old-but-universal messaging format that virtually every hospital system speaks. It is a pipe-delimited text format, fields separated by `|`, sub-components separated by `^`. A message type is identified by its message header (e.g. `ORU^R01` = "Observation Result, Unsolicited"). An ORU^R01 message carries lab results and vital signs. An ADT message (Admit, Discharge, Transfer) carries patient movement events. HL7 is decades old, ugly, and everywhere.

**FHIR** (Fast Healthcare Interoperability Resources) is the modern standard, it uses JSON over a REST API and is far easier to work with. Orb does not yet have a FHIR adapter, but the adapter architecture in this codebase is designed to add one with minimal effort (implement `IngestionAdapter`, register in the `ADAPTERS` list, done).

**CSV/JSON ingestion** is used for demo backfill, device webhooks, and testing. The JSON adapter accepts a structured webhook format; the CSV adapter parses flat rows with a header.

**Adapter**, a class that knows whether it can handle a particular payload, and that can translate that payload into Orb's internal `CanonicalEvent` shape. There is one adapter per input format (HL7, JSON, CSV). Adapters hide all the messiness of the external format from the rest of the codebase.

**CanonicalEvent**, the internal "universal observation" shape that all adapters produce. Once a payload becomes a list of `CanonicalEvent` objects, downstream code does not need to know or care whether the original message was HL7, JSON, or CSV.

**Correlation ID**, a UUID string that gets stamped on every audit log entry, every database row, and every log message related to a single inbound request. If something goes wrong, you can search the audit table for `correlation_id = 'abc-123'` and see the entire chain of events in sequence, received, parsed, inserted, scored, alerted. If the caller does not supply one, the route generates one automatically.

**Idempotency / deduplication**, the same message arriving twice should not insert duplicate vital rows or fire duplicate alerts. Orb prevents this by computing a SHA-256 fingerprint (hash) of the raw payload. Before doing any work, it checks whether a row with that fingerprint already exists in `ingestion_events`. If it does, the second request is acknowledged with HTTP 200 but no new data is inserted.

**Audit trail**, every significant step of ingestion writes an entry to the audit log via `log_audit_event`. The events in order for a successful ingestion are: `ingestion_request_received` → `ingestion_parse_success` → `vital_inserted` → `news2_recomputed` → (if non-stable) `relay_alert_generated` + `vigil_alert_generated`.

**Vigil / NEWS2 recompute**, immediately after a vital row is inserted into the database, the ingestion service calls `compute_news2(hr, sbp, rr, temp, spo2, consciousness, on_oxygen)`. This is the same function that Vigil's bot service calls. The result (a score, risk level, and list of drivers) is stored as a new row in `risk_scores`, emitted over Socket.IO so the frontend updates instantly, and checked against alert thresholds.

---

### End-to-End Concrete Example

An HL7 ORU^R01 message arrives from a bedside monitor for patient `PAT-001`.

1. The hospital's integration engine POSTs the raw HL7 text to `POST /api/ingest/hl7` with `X-API-Key: orb-ingest-dev-key` and `X-Correlation-Id: abc-123`.
2. The route validates the API key, extracts the correlation ID, and calls `ingest_payload(raw_payload, adapter=HL7Adapter(), ...)`.
3. The service fires audit event `ingestion_request_received`.
4. It computes a SHA-256 fingerprint of the raw text. It checks `ingestion_events`, no match, so this is not a duplicate.
5. It inserts a row into `ingestion_events` with `parse_status = 'received'` and gets back an `event_id`, say `42`.
6. `HL7Adapter.parse()` runs. It splits the message into segments, reads `PID-3` to get `PAT-001`, reads each `OBX` segment. One OBX has LOINC code `8867-4` with value `88`, that maps to vital key `HR`. Another OBX has `9279-1` with value `18`, that maps to `RR`. Both pass plausibility checks. Two `CanonicalEvent` objects are returned, both with `observation_type = VITAL` and `mapped_vital_key` set.
7. The service routes both events to `_insert_vitals`. It resolves `PAT-001` to an internal patient UUID. It carries forward the most recent values for `SBP`, `DBP`, `SpO2`, `Temp` from the last vitals row. It inserts one new row into `vitals` with all six values. Audit event `vital_inserted` is written with `patient_id` and `source_event_id = 42`.
8. `compute_news2(88, carried_sbp, 18, carried_temp, carried_spo2, "alert", False)` is called. The result might be `{"score": 0.05, "level": "stable", "raw_news2": 1, "drivers": [...]}`. A new row is inserted into `risk_scores`. Audit event `news2_recomputed` is written.
9. A Socket.IO `vitals:update` event is emitted with the full vitals payload and risk result, so the frontend dashboard updates without a page refresh.
10. Because `level == "stable"`, `_check_ingested_alert` exits early, no alert is dispatched.
11. The service updates `ingestion_events` row 42 to `parse_status = 'parsed'`, `vitals_inserted = 1`. Audit event `ingestion_parse_success` is written.
12. The route returns HTTP 200 with the `IngestionResult` JSON including the `event_id`, `correlation_id`, and counts.

---

### File: `server/routes/ingestion_routes.py`

**Role:** FastAPI router that exposes the public HTTP endpoints for data ingestion. It is the entry point, it authenticates callers, reads the request body, generates or relays the correlation ID, and delegates all actual work to `ingestion_service.ingest_payload`. It also owns the status/debug endpoint and the dev replay endpoint.

**Libraries used:**
- `fastapi`, `APIRouter`, `Request`, `HTTPException`, `Header`, `Depends` for defining REST endpoints and injecting headers as function arguments.
- `uuid`, generates a UUID if the caller doesn't provide a correlation ID.
- `config`, reads `INGESTION_API_KEY` and `INGESTION_MODE` from environment variables.
- `services.ingestion_adapters`, imports the three concrete adapter classes (`HL7Adapter`, `JSONAdapter`, `CSVAdapter`) to pre-select the adapter for typed endpoints.
- `services.ingestion_service`, the actual pipeline; routes call `ingest_payload`.
- `services.audit_service`, used directly only in the replay endpoint to log a replay-specific audit event.
- `permissions`, `require_permission("ingest.monitor")` is a FastAPI dependency used on the status endpoint to restrict access.
- `database`, `db_session` for the status endpoint's direct DB query.

---

#### `_verify_api_key(x_api_key)`

**Purpose:** Authenticate incoming requests. Every ingest endpoint calls this before doing anything.

**How it works:** Compares the `X-API-Key` header value to `INGESTION_API_KEY` from config. If they don't match (or the header is missing), it raises `HTTPException(status_code=401)`.

**Where `INGESTION_API_KEY` comes from:** The `config.py` reads `os.getenv("ORB_INGESTION_API_KEY")`. If that environment variable is not set (i.e. in development), config falls back to the hardcoded string `"orb-ingest-dev-key"` and logs a `CRITICAL SECURITY ERROR` warning. In production the environment variable must be set to a secret value.

⚠️ **If you change this:** Changing the auth mechanism (e.g. switching from API key to JWT, or changing the header name from `X-API-Key`) will break every integration that currently sends this header. You would also need to update the admin IngestionMonitor UI if it shows the key name, and update any integration documentation.

---

#### `_check_ingestion_enabled()`

**Purpose:** Block ingestion requests when the system is running in simulation mode.

**How it works:** Reads `INGESTION_MODE` from config (env var `ORB_INGESTION_MODE`, default `"ingestion"`). If the mode is `"simulation"`, all ingest endpoints return HTTP 503 with an explanation. Hybrid mode (`"hybrid"`) allows both simulation and ingestion to coexist.

---

#### `POST /api/ingest/json`

**Purpose:** Accept a structured JSON webhook payload from a bedside monitor, integration engine, or test harness.

**Headers accepted:**
- `X-API-Key` (required), ingestion API key.
- `X-Correlation-Id` (optional), caller-provided trace ID; auto-generated if absent.
- `X-Message-Id` (optional), sender-provided deduplication key passed through to the service.
- `X-Source-Name` (optional), human label for the data source (e.g. `"Philips-Monitor-Bed3"`).

**Flow:** Calls `_check_ingestion_enabled()` and `_verify_api_key()`, reads the raw body, generates a correlation ID if needed, imports the Socket.IO instance from `ws.realtime` (gracefully falls back to `None` if unavailable), then calls `ingest_payload` with `adapter=JSONAdapter()` pre-set. Returns the `_build_response` dict.

---

#### `POST /api/ingest/hl7`

**Purpose:** Accept a raw HL7 v2 ORU message string from an integration engine or mirth connect.

**Headers accepted:** Same as the JSON endpoint.

**Body format:** Can be either a raw HL7 string (segments separated by `\r`, `\n`, or `\r\n`) or a JSON wrapper `{"message": "MSH|^~\\&|..."}`. The route detects the JSON wrapper by checking if the body starts with `{` and unwraps it automatically. This is a convenience for callers that can only send JSON payloads.

**Flow:** Same pattern as the JSON endpoint but uses `adapter=HL7Adapter()` pre-selected.

---

#### `GET /api/ingest/status`

**Purpose:** Admin and governance endpoint for monitoring recent ingestion activity.

**Auth:** Uses `Depends(require_permission("ingest.monitor"))`, a FastAPI dependency that checks the session user has the `ingest.monitor` permission. This is different from the API key used on ingest endpoints; this endpoint uses Orb's normal session auth.

**Query parameters:**
- `limit` (default 20, capped at 100), how many rows to return.
- `status` (optional), filter by `parse_status` value (`received`, `parsed`, `failed`, `duplicate`, `partially_parsed`).

**Returns:** A list of `ingestion_events` rows (excluding the raw payload for size reasons) plus the current `ingestion_mode` value. Used by the admin IngestionMonitor frontend component.

⚠️ **If you change this:** The admin IngestionMonitor frontend reads specific field names from this response (`id`, `received_at`, `source_type`, `source_name`, `parse_status`, `parse_error`, `correlation_id`, `vitals_inserted`, `observations_inserted`, `processed_at`). Renaming any of these will break that UI component.

---

#### `POST /api/ingest/replay`

**Purpose:** Developer and testing endpoint that accepts any format and auto-detects the adapter.

**Auth:** Requires the API key but does NOT check `INGESTION_MODE` (it intentionally bypasses the simulation guard so you can test replay in any mode).

**Flow:** Calls `ingest_payload` with `adapter=None`, which triggers auto-detection. Also writes a specific audit event `ingestion_replay_triggered` after the pipeline completes, so replay runs are distinguishable in the audit log from live ingestion events.

---

#### `_http_status_from_result(result)` and `_build_response(result, status_code)`

**Purpose:** Convert an `IngestionResult` dataclass into a consistent HTTP response shape.

**Status code mapping:**
- `parsed` → 200 OK
- `partially_parsed` → 207 Multi-Status (some events succeeded, some failed)
- `duplicate` → 200 OK (idempotent, the caller doesn't need to retry)
- anything else (`failed`, `received`) → 422 Unprocessable Entity

**Response shape:** `{"status": "ok"|"error", "event_id": int, "correlation_id": str, "parse_status": str, "counts": {...}, "errors": [...]|null}`

---

### File: `server/services/ingestion_adapters.py`

**Role:** Contains everything needed to translate a raw incoming payload into the internal `CanonicalEvent` shape. This file defines the abstract adapter interface, all three concrete adapters, shared utility functions, and the adapter registry. The service file imports from here but never knows anything about HL7 syntax or JSON schemas.

**Libraries used:**
- `csv` + `io`, standard library; the CSV adapter uses these to parse CSV text without writing a temp file.
- `hashlib`, SHA-256 fingerprinting for deduplication.
- `re`, regex for splitting HL7 segments and stripping comparison operators from numeric strings.
- `uuid`, generates fallback group IDs when OBR segments lack a sequence number.
- `abc`, `ABC` and `abstractmethod` to define the adapter interface pattern.
- `dataclasses`, `@dataclass`, `field`, `asdict` for the `CanonicalEvent` model.
- `datetime`, `timezone`, timestamp handling throughout.
- `enum`, `Enum` for `ObservationType` and `ValueType` constants.

---

#### `ObservationType` (Enum)

Values: `VITAL`, `LAB`, `SCORE`, `DEVICE_METRIC`, `NOTE_STUB`, `UNKNOWN`.

Used to classify each `CanonicalEvent`. The service uses this to decide which table to route the event into, `VITAL` events go to the `vitals` table and trigger NEWS2; all others go to the `observations` table.

---

#### `ValueType` (Enum)

Values: `NUMERIC`, `TEXT`, `COMPOSITE`.

Tracks whether the observation value was parsed as a number. Only `NUMERIC` values get stored in `value_numeric` and fed into scoring formulas.

---

#### `CanonicalEvent` (dataclass)

The universal observation shape. Every adapter produces a list of these. Key fields:

| Field | Meaning |
|---|---|
| `patient_identifier` | Raw patient ID from the source (MRN or internal ID). Resolved to a DB UUID by the service. |
| `observation_name` | Human-readable name (e.g. `"Heart Rate"`). |
| `value` | Always stored as a string. |
| `value_numeric` | Parsed float, or `None` if the value isn't a number. |
| `observation_type` | `VITAL` or `LAB` etc., determines routing. |
| `mapped_vital_key` | If this observation maps to one of the 6 vital signs, this is set to `HR`, `SBP`, `DBP`, `RR`, `Temp`, or `SpO2`. `None` for lab results. |
| `observation_code` | LOINC or other code system code. |
| `observed_at` | ISO 8601 timestamp when the reading was taken. |
| `raw_event_id` | FK to `ingestion_events.id`, set by the service after the raw event is recorded. |
| `correlation_id` | Trace ID propagated from the original request. |
| `panel_name` | For HL7: the name of the OBR group (e.g. "CBC" or "Vitals Panel"). |
| `obr_group_id` | For HL7: the OBR sequence number or a generated UUID, for grouping related OBXs. |
| `raw_segment_ref` | For HL7: the raw OBX string, kept for debugging. |

⚠️ **If you change CanonicalEvent's shape:** The service reads `mapped_vital_key`, `observation_type`, `value_numeric`, `patient_identifier`, `observed_at`, `unit`, `observation_code`, `abnormal_flag`, `status`, `reference_range`, `panel_name`, `obr_group_id`, `raw_segment_ref` directly. Renaming any field breaks `_route_events`, `_insert_vitals`, and `_insert_observation` in `ingestion_service.py`.

---

#### `_VITAL_CODE_MAP` and `map_to_vital_key(code, name)`

**Purpose:** Map from a LOINC code OR a plain-text observation name to one of Orb's six internal vital keys (`HR`, `SBP`, `DBP`, `RR`, `Temp`, `SpO2`).

**How it works:** The dict maps both LOINC codes (e.g. `"8867-4"` → `"HR"`) and lowercase plain-English names (e.g. `"heart rate"` → `"HR"`, `"pulse"` → `"HR"`). `map_to_vital_key` checks the code first (exact match), then the name (case-insensitive after `strip().lower()`). Returns `None` if neither matches, that observation will be classified as a lab result.

⚠️ **If you add a new vital key here** (e.g. `"ACVPU"` for consciousness): you must also add handling in `_insert_vitals` in the service, add the column to the `vitals` table schema in `database.py`, and update `compute_news2` if it affects scoring.

---

#### `normalize_temp_to_celsius(value, unit)`

**Purpose:** Convert Fahrenheit readings to Celsius.

**How it works:** Checks the unit string against a set of Fahrenheit identifiers (`F`, `°F`, `DEG F`, `[DEGF]`). If matched, applies the standard `(F - 32) × 5/9` formula and rounds to 1 decimal place. Returns the value unchanged for Celsius or unknown units.

**When it runs:** All three adapters call this if `vital_key == "Temp"` and a numeric value was parsed.

---

#### `_VITAL_PLAUSIBLE_RANGE` and `validate_vital_value(vital_key, value)`

**Purpose:** Reject physiologically impossible vital sign readings before they can corrupt NEWS2 scoring or trigger spurious alerts.

**Ranges:**
- HR: 10–300 bpm
- SBP: 40–300 mmHg
- DBP: 10–200 mmHg
- RR: 2–80 breaths/min
- Temp: 20–45 °C (post-Fahrenheit-conversion)
- SpO2: 10–100 %

**How it works:** If the value is outside the plausible range for its vital key, returns `None`. The service then carries forward the patient's previous value for that vital rather than using the implausible reading. This prevents sensor glitches (e.g. an SpO2 of 900 from a disconnected probe) from driving a critical alert.

All three adapters call this after `map_to_vital_key` and after Fahrenheit conversion.

---

#### `compute_fingerprint(raw_payload)`

**Purpose:** Generate a SHA-256 hash of the raw payload string for deduplication.

**How it works:** Encodes the string as UTF-8 (replacing any encoding errors), then returns the hex digest. This is stored in `ingestion_events.fingerprint`. The service queries this column before doing any processing, so identical payloads that arrive twice are detected immediately without re-parsing.

---

#### `parse_hl7_timestamp(ts)`

**Purpose:** Convert an HL7 datetime string to ISO 8601 format.

**How it works:** HL7 timestamps look like `20240101120000` (yyyyMMddHHmmss). The function strips timezone offsets (HL7 uses `+0500` suffixes), then tries three formats in descending precision: `yyyyMMddHHmmss`, `yyyyMMddHHmm`, `yyyyMMdd`. Returns an ISO 8601 string with UTC timezone, or `None` if parsing fails. Called for `MSH-6` (message time), `OBR-7` (observation time), and `OBX-14` (result time).

---

#### `IngestionAdapter` (Abstract Base Class)

**Purpose:** Defines the interface that every adapter must implement.

**Required properties:**
- `adapter_name`, a stable string identifier (e.g. `"hl7_oru"`). Stored in `ingestion_events.adapter_name` for debugging.
- `source_type`, a short type string (e.g. `"hl7"`, `"json"`, `"csv_replay"`). Used as a routing key and appears in audit logs and the `vitals.source` column.

**Required methods:**
- `can_handle(raw_payload, content_type)`, returns `True` if this adapter can parse the given payload. Must be cheap (no full parsing).
- `parse(raw_payload, source_name, correlation_id)`, returns a list of `CanonicalEvent` objects. Should not crash on malformed input; internal errors should be collected and surfaced gracefully. May raise `ParseError` if the entire message is unrecognizable.

#### `ParseError` (Exception)

A custom exception type. Adapters raise this for structural failures (missing required segment, completely invalid format). The service catches it and records a `failed` parse status without crashing.

---

#### `HL7Adapter`

**Purpose:** Parses HL7 v2 ORU^R01 messages, the standard lab results and vital signs message type used by virtually all hospital information systems.

**`can_handle`:** Returns `True` if the stripped payload starts with `"MSH|"`. This is sufficient because every valid HL7 message starts with the Message Segment Header.

**`parse`, top-level flow:**

1. Splits the raw text on `\r`, `\n`, or `\r\n` to get individual segments.
2. Validates that the first segment starts with `MSH|`. Raises `ParseError` if not.
3. Parses the **MSH segment** (Message Header):
   - `MSH-2` first character → the component separator (usually `^`).
   - `MSH-3` → sending application name (used as `source_name` if none provided).
   - `MSH-9` → message type (e.g. `ORU^R01`).
   - `MSH-10` → message control ID (unique per-message from the sender).
   - `MSH-7` → message timestamp.
4. Scans for the **PID segment** (Patient Identification) and extracts `PID-3` (Patient Identifier List). Takes the first component (before `^`) as the patient ID. Raises `ParseError` if no patient ID is found.
5. Optionally scans for the **PV1 segment** (Patient Visit) and extracts `PV1-19` (Visit Number / Encounter ID).
6. Scans all remaining segments for **OBR** (Observation Request) and **OBX** (Observation Result) segments:
   - Each OBR creates a new group with a panel name (`OBR-4`), a group timestamp (`OBR-7`), and an empty list of OBX segments.
   - Each OBX is appended to the most recent OBR group.
   - OBX segments with no preceding OBR get their own implicit group.
7. For each OBX in each group, calls `_parse_obx`.
8. If all OBX segments fail but some errors were collected, raises `ParseError` with all errors joined. If some succeed and some fail, the successful events are returned and errors are noted.

**`_parse_obx`, parsing a single OBX segment:**

HL7 OBX fields (1-based):
- Field 2 → Value Type (`NM` for numeric, `ST` for string, `SN` for structured numeric, etc.)
- Field 3 → Observation Identifier (e.g. `8867-4^Heart Rate^LN`, code, name, code system separated by `^`)
- Field 5 → Observation Value
- Field 6 → Units
- Field 7 → Reference Range
- Field 8 → Abnormal Flags (`H` for high, `L` for low, etc.)
- Field 11 → Observation Result Status (`F` = final, `P` = preliminary, `C` = corrected)
- Field 14 → Date/Time of Observation

The method:
1. Splits field 3 on the component separator to get code, human name, and code system.
2. If value type is `NM` or `SN`, strips comparison operators (`<`, `>`, `=`) and parses as float.
3. Calls `map_to_vital_key(code, name)` to determine if this is a vital sign.
4. Calls `normalize_temp_to_celsius` if it's a temperature.
5. Calls `validate_vital_value` to reject implausible readings.
6. Determines the effective timestamp: OBX-14 → OBR group time → MSH message time, in that preference order.
7. Returns a fully-populated `CanonicalEvent`.

**`_field(fields, index)`:** A defensive helper that returns an empty string instead of raising `IndexError` when a segment is shorter than expected. Accepts a zero-based list index (callers pass the HL7 field number directly, relying on field 0 being the segment name).

---

#### `JSONAdapter`

**Purpose:** Parses structured JSON webhook payloads from devices, integration engines, or test scripts.

**`can_handle`:** Returns `True` if `content_type` contains `"json"`, or if the payload starts with `{` and ends with `}`.

**Expected payload shape:**
```json
{
  "patient_id": "PAT001",
  "encounter_id": "ENC001",
  "source_name": "BedsideMonitor-ICU-3",
  "message_id": "abc-123",
  "observed_at": "2024-01-01T12:00:00Z",
  "observations": [
    {
      "code": "8867-4",
      "name": "Heart Rate",
      "value": 88,
      "unit": "bpm",
      "reference_range": "60-100",
      "abnormal_flag": "",
      "status": "final"
    }
  ]
}
```

**`parse` flow:**
1. Decodes JSON with `json.loads`. Raises `ParseError` on invalid JSON.
2. Requires `patient_id` at the top level. Raises `ParseError` if absent.
3. Extracts optional top-level fields: `encounter_id`, `source_name`, `observed_at`.
4. Requires a non-empty `observations` list. Raises `ParseError` if absent or not a list.
5. For each observation dict:
   - Skips entries missing both `name` and `value`.
   - Parses `value` as float if it's a Python `int` or `float`, or if it's a string that converts cleanly. Otherwise keeps as text.
   - Calls `map_to_vital_key`, `normalize_temp_to_celsius`, `validate_vital_value`.
   - Uses observation-level `observed_at` if present, falling back to the message-level `observed_at`, then to `now`.
6. If no valid observations were produced, raises `ParseError`.

⚠️ **If you change the expected JSON shape** (e.g. rename `observations` to `results`): all existing callers of `POST /api/ingest/json` will break silently, their payloads will fail with `ParseError: Missing or invalid 'observations' array`. Update the adapter, update any integration documentation, and update the demo/test fixtures.

---

#### `CSVAdapter`

**Purpose:** Parses CSV replay files used for demo backfill and offline testing. Not intended for production live feeds.

**`can_handle`:** Returns `True` if `content_type` contains `"csv"`, or if the first line of the payload contains `"patient_id"` and there are at least 2 lines.

**Expected column header:** `patient_id, name, code, value, unit, observed_at`

`observed_at` is optional, if absent or blank, the current UTC time is used.

**`parse` flow:** Uses `csv.DictReader` to iterate rows. For each row:
1. Skips rows with empty `patient_id`.
2. Parses `value` as float if possible.
3. Calls `map_to_vital_key`, `normalize_temp_to_celsius`, `validate_vital_value`.
4. Appends a `CanonicalEvent`.

Raises `ParseError` if no valid rows were found.

---

#### `ADAPTERS` list and `select_adapter(raw_payload, content_type)`

**Purpose:** The adapter registry and auto-detection function.

`ADAPTERS` is a module-level list: `[HL7Adapter(), JSONAdapter(), CSVAdapter()]`. Order matters, `select_adapter` returns the first adapter that returns `True` from `can_handle`. HL7 is checked first because its heuristic (`starts with "MSH|"`) is unambiguous and distinct from JSON/CSV.

`select_adapter` iterates the list and returns the first matching adapter, or `None` if no adapter matches. A `None` return causes the service to record the event as `failed` with the error `"No suitable adapter found"`.

⚠️ **If you add a new adapter** (e.g. FHIR): implement `IngestionAdapter`, append an instance to `ADAPTERS`, and ensure `can_handle` is reliable and doesn't overlap with existing adapters.

---

### File: `server/services/ingestion_service.py`

**Role:** The core pipeline orchestrator. It takes a raw payload (already selected adapter is optional) and drives it through all stages: raw event recording, fingerprint dedup check, adapter parsing, routing into DB tables, NEWS2 recompute, Socket.IO emission, and Vigil alert check. It also owns all the database write functions for ingestion.

**Libraries used:**
- `json`, serialize risk score drivers for storage in `risk_scores.drivers`.
- `logging`, the `orb.ingestion` logger; warnings on Socket.IO failures and unresolved patients.
- `time`, imported but not used in the current file (may be vestigial).
- `dataclasses`, `@dataclass` for `IngestionResult`.
- `datetime`, `timezone`, fallback timestamps when observations have no timestamp.
- `database.db_session`, Orb's context-manager database session; used for all DB reads and writes.
- `services.ingestion_adapters`, the adapter classes, `CanonicalEvent`, `ObservationType`, `ParseError`, `compute_fingerprint`, `select_adapter`.
- `services.vigil_service.compute_news2`, called immediately after each vital row is inserted.
- `services.audit_service.log_audit_event`, writes every audit event in the pipeline.

---

#### `IngestionResult` (dataclass)

**Purpose:** A summary object returned to the HTTP route after the pipeline completes.

**Fields:**
- `event_id`, the `ingestion_events.id` of the row created for this payload.
- `correlation_id`, the trace ID for this request.
- `parse_status`, final status: `received`, `parsed`, `partially_parsed`, `failed`, `duplicate`.
- `received`, number of `CanonicalEvent` objects produced by the adapter.
- `inserted_vitals`, number of vital rows inserted.
- `inserted_observations`, number of observation rows inserted.
- `duplicates`, 1 if this was a duplicate, 0 otherwise.
- `errors`, list of error strings collected during routing.

`to_dict()` serializes to a plain dict for JSON responses.

---

#### `_SOURCE_LABELS`

Maps `source_type` strings to the label stored in `vitals.source` and `observations.source`.

```python
{"hl7": "ingested_hl7", "json": "ingested_json", "csv_replay": "csv_replay"}
```

Any unknown `source_type` falls back to `f"ingested_{source_type}"`. These labels appear in the `vitals.source` column and in the frontend's "Source" display. If you add a new adapter with a new `source_type`, this dict will handle it gracefully with the fallback.

---

#### `_resolve_patient_id(db, raw_identifier)`

**Purpose:** Translate a raw patient identifier from the external system (which might be an MRN, a patient UUID, or something else) into Orb's internal patient UUID.

**How it works:** Three lookups in order:
1. Exact match on `patients.id` (the Orb UUID), fastest, handles cases where the feed already uses Orb UUIDs.
2. Exact match on `patients.mrn`, the standard case for hospital feeds using MRN.
3. Case-insensitive match on `patients.mrn`, handles feeds that send `PAT001` when the DB has `pat001`.

Returns the internal `patients.id` UUID, or `None` if no match is found. A `None` result causes `_insert_vitals` to raise `ValueError` (the whole patient batch is skipped with an error), while `_insert_observation` logs a warning and inserts the observation with a NULL `patient_id` so nothing is silently dropped.

---

#### `ingest_payload(raw_payload, adapter, source_name, content_type, correlation_id, external_message_id, sio)`

**Purpose:** The main pipeline function. Everything flows through here.

**Arguments:**
- `raw_payload`, the raw string received from the network.
- `adapter`, a pre-selected `IngestionAdapter`, or `None` to trigger auto-detection.
- `source_name`, a human label for the sender (stored for audit purposes).
- `content_type`, HTTP Content-Type, passed to `select_adapter` as a hint.
- `correlation_id`, trace ID; the route generates one if the caller didn't provide it.
- `external_message_id`, optional sender-assigned dedup key stored in `ingestion_events`.
- `sio`, the Socket.IO server instance for emitting real-time events; `None` disables emission.

**Step-by-step flow:**

**Step 1, Adapter selection:** If `adapter` is `None`, calls `select_adapter(raw_payload, content_type)`. The first audit event `ingestion_request_received` is fired immediately, before any parsing, so every inbound request is recorded even if it later fails.

**Step 2, Fingerprint:** Calls `compute_fingerprint(raw_payload)` to get a SHA-256 hash.

**Step 3, Deduplication check:** Queries `ingestion_events WHERE fingerprint = ? AND parse_status != 'failed'`. If a match exists, records the event as `duplicate` (audit event `ingestion_duplicate_detected`), updates the result, and returns early without re-parsing.

**Step 4, Raw event recording:** Calls `_record_raw_event` to insert a row in `ingestion_events` with `parse_status = 'received'`. Gets back the `event_id`.

**Step 5, Parsing:** Calls `adapter.parse(raw_payload, source_name, correlation_id)`. This can raise `ParseError` (handled: writes `ingestion_parse_failure` audit event, updates event to `failed`, returns) or any other exception (also handled the same way).

**Step 6, Provenance tagging:** Stamps each `CanonicalEvent` with `raw_event_id = event_id` and `source_type = adapter.source_type`.

**Step 7, Routing:** Calls `_route_events(canonical_events, event_id, source_type, sio, correlation_id)`, which separates vital events from non-vital events and processes them separately.

**Step 8, Status update:** Determines the final parse status based on route errors and counts:
- No errors → `parsed`, audit `ingestion_parse_success`
- Some errors but some successes → `partially_parsed`, audit `ingestion_parse_partial_success`
- All errors → `failed`, audit `ingestion_parse_failure`

Updates `ingestion_events` and fires the final audit event.

---

#### `_record_raw_event(...)`

**Purpose:** Insert a row into the `ingestion_events` table to preserve a provenance record of every inbound payload.

**Key columns written:** `source_type`, `source_name`, `external_message_id`, `patient_identifier_raw` (currently always `None` at this stage, the raw identifier is not extracted before parsing), `raw_payload` (capped at 50,000 characters), `payload_format` (set to `adapter_name`), `parse_status`, `parse_error`, `fingerprint`, `correlation_id`, `adapter_name`.

**Important note in the source code:** The comment in this function explicitly calls out a deferred regulatory concern, `raw_payload` stores the full PHI from the inbound message with no expiry or purge. For GDPR data minimisation compliance, a retention/purge job should eventually null out `raw_payload` after successful processing or after N days. This is known technical debt.

Returns the new row's `id` (used as `event_id` throughout the rest of the pipeline).

---

#### `_update_event_status(event_id, status, parse_error, vitals_inserted, observations_inserted)`

**Purpose:** Update the `ingestion_events` row once parsing and routing are complete.

Updates `parse_status`, `parse_error`, `vitals_inserted`, `observations_inserted`, and `processed_at = CURRENT_TIMESTAMP`. Called twice per request: once if parsing fails immediately, once at the end of the pipeline with final counts.

---

#### `_route_events(events, event_id, source_type, sio, correlation_id)`

**Purpose:** Sort a list of `CanonicalEvent` objects into vitals vs. non-vital observations, then route each group to the appropriate insert function.

**How it works:**
1. Builds a dict keyed by `patient_identifier` containing all vital events for each patient, because the `vitals` table stores one row per patient per timestamp with all six parameters, vitals for the same patient are batched together.
2. Non-vital events (labs, scores, notes) go into a separate flat list.
3. Calls `_insert_vitals` for each patient's batch.
4. Calls `_insert_observation` for each non-vital event.
5. Collects and returns any errors alongside the counts.

Returns `(vitals_inserted, observations_inserted, errors)`.

---

#### `_insert_vitals(patient_raw_id, vital_events, event_id, source_label, sio, correlation_id)`

**Purpose:** Merge a batch of individual vital observations into a single `vitals` table row, then trigger downstream scoring and alerting.

This is the most important function in the ingestion pipeline, it is where data becomes clinical risk.

**Flow:**

1. **Patient resolution:** Calls `_resolve_patient_id`. Raises `ValueError` if the patient cannot be found, the error propagates to `_route_events` which collects it and continues with other patients.

2. **Vital collection:** Iterates the `vital_events` list, filling a dict `{HR, SBP, DBP, SpO2, Temp, RR}` from each event's `mapped_vital_key` and `value_numeric`. Records the first non-None `observed_at`.

3. **Carry-forward:** Queries the patient's most recent `vitals` row. For any vital key that the incoming feed did not provide (value is still `None`), fills it from the previous row. This ensures a partial vital update (e.g. a feed that only sends HR and SpO2) doesn't wipe out the other parameters. Also carries forward `consciousness` (the ACVPU consciousness level string) and `on_oxygen` (supplemental O2 flag). **Age-limited since 2026-10-03 (ruled by Samuel):** a value is carried only while its *measurement* is younger than its limit (HR, BP, SpO2 and RR 4 h, temperature 8 h, ACVPU and supplemental O2 12 h; `services/vitals_carry.py`). Each row records in `vitals.carried_from` (migration 47) when every value it copied was really measured, so a stream of HR-only messages cannot keep re-stamping an old BP as current. Past its limit a value is stored as `None`: not measured, scored 0, NEWS2 marked incomplete.

4. **Guard:** If every vital value is still `None` after carry-forward (the batch had no numeric values), returns 0 without inserting.

5. **DB insert:** Inserts one row into `vitals` with all six numeric fields, `consciousness`, `source = source_label`, and `source_event_id = event_id`. Fires audit event `vital_inserted`.

6. **NEWS2 recompute:** Calls `compute_news2(hr, sbp, rr, temp, spo2, consciousness, on_oxygen)`, the full 7-parameter NEWS2 function. The return value is a dict:
   - `score`, normalized 0–1 risk index.
   - `raw_news2`, the raw NEWS2 ordinal aggregate (the number clinicians recognize, e.g. 3 or 7).
   - `level`, `"stable"`, `"elevated"`, or `"critical"`.
   - `drivers`, up to 3 strings explaining which parameters contributed (e.g. `"SpO2 low"`, `"Altered consciousness (ACVPU)"`).

7. **Risk score storage:** Inserts a row into `risk_scores (patient_id, score, level, drivers)`. `drivers` is stored as a JSON string. Fires audit event `news2_recomputed`.

8. **Socket.IO emission:** Emits `vitals:update` with the full payload (all 6 vitals + consciousness + on_oxygen + risk dict + source label). If `sio` is `None` or emit fails, logs a warning and continues.

9. **Alert check:** Calls `_check_ingested_alert`.

Returns `1` (one vital row inserted).

**Important design note on 7-parameter NEWS2:** There is a comment in the code explaining that when HL7/JSON feeds only provide the 6 measurable vitals (HR, SBP, DBP, RR, Temp, SpO2), the `consciousness` and `on_oxygen` parameters are carried forward from the previous row rather than being freshly measured. This means a patient whose consciousness changed since their last obs would temporarily be scored with the old value until the next full assessment is charted. The comment flags this as a known limitation and marks the proper fix (mapping ACVPU/O2 from the feed and adding `on_oxygen` as a dedicated column) as deferred work.

---

#### `_check_ingested_alert(patient_id, vitals_payload, risk, sio, correlation_id)`

**Purpose:** Determine whether the newly computed NEWS2 result warrants a Vigil alert in the patient's Relay case room.

**Key design decision:** The NEWS2 `level` is the sole gating criterion. If `level == "stable"`, the function returns immediately with no action. The reason line is generated purely for human-readable alert text, it does NOT gate the alert. This is explicitly documented in the code to prevent a future developer from accidentally gating dispatch on the reason text, which would suppress alerts for patients whose deterioration is driven by consciousness or borderline parameters.

**Alert text construction:**
1. Builds the reason line with `vigil_service.news2_reason_line`, shared by all three alert lanes since 2026-10-02, from the NEWS2 `breakdown`: every parameter that scored, worst first (by points, then Severe before Moderate, with a new ACVPU change counted as Severe, since 2026-10-03), ties in RCP chart order, the first three named and the rest counted (e.g. `"Moderate hypoxia (SpO2 88%); Tachypnea (RR 24); Hypotension (SBP 92) (+2 more)"`). The words are the NEWS2 driver text, and since `drivers` is sorted the same way (2026-10-02) the driver strip names the reason line's first three parameters; a CfC overlay, where one is enabled, can put its own line first on display surfaces. A parameter that scored 0 is never a reason. It reads `breakdown` rather than `drivers` because `drivers` stops at three and the "(+N more)" count needs every parameter that scored.
2. If the result carries no `breakdown` (a hand-built dict), falls back to the `drivers` list, then to the band. If the helper itself raises, each lane logs the error and sends the alert with the band alone ("NEWS2 critical"), so the wording can never cost the alert.
3. Composes a Relay message with format: `"⚠️ VIGIL ALERT [SOURCE]: reason; reason; reason (+N more)\nCurrent vitals: ...\nNEWS2 N (level)."`.
4. Uses `raw_news2` (the integer aggregate) in the text, explicitly not a "fabricated probability percentage" as the code comment notes.

**Relay message insert:** Inserts into `messages` with `message_type = 'alert'`, `sender_id = 'bot_vigil'`. Looks up the patient's `case_room` channel. If found, inserts the message and emits `message:new` over Socket.IO to the channel room. Also emits `patient_status_update` globally so the nurses station view can highlight the patient.

**Audit events:** If the Relay message was successfully posted, fires `relay_alert_generated`. Regardless, fires `vigil_alert_generated` to confirm the alert path was reached.

---

#### `_insert_observation(evt, event_id, source_label, correlation_id)`

**Purpose:** Insert a non-vital observation (lab result, score, etc.) into the `observations` table.

**Flow:** Resolves the patient ID using `_resolve_patient_id`. If the patient cannot be found, logs a warning but still inserts the row with `patient_id = NULL`. This is intentional, no data is silently dropped, but the code comment flags this as needing a proper quarantine queue in a future version. Inserts all `CanonicalEvent` fields into the corresponding `observations` columns. Fires audit event `observation_inserted`.

---

### Audit Event Reference

The table below lists all audit event `action_type` values emitted by the ingestion pipeline, in the order they appear for a successful ingestion. These names are the ones you would search for in the audit log or filter on in the admin UI.

| Event | When emitted | Outcome |
|---|---|---|
| `ingestion_request_received` | Before any processing | `success` |
| `ingestion_duplicate_detected` | Fingerprint matches existing event | `success` |
| `ingestion_parse_failure` | Adapter raises ParseError or other exception | `failure` |
| `ingestion_parse_partial_success` | Some events routed, some failed | `partial_success` |
| `ingestion_parse_success` | All events routed without errors | `success` |
| `vital_inserted` | Per-patient vital row written | `success` |
| `news2_recomputed` | NEWS2 called and risk_scores row written | `success` |
| `observation_inserted` | Per-observation row written to observations | `success` |
| `relay_alert_generated` | Alert message written to Relay case room | `success` |
| `vigil_alert_generated` | Alert path completed (room may not exist) | `success` |
| `ingestion_replay_triggered` | Dev replay endpoint fired | `success`/`failure` |

⚠️ **If you rename any of these event strings:** The admin IngestionMonitor frontend and any analytics that filter the audit log by `action_type` will stop working. Search the codebase for each string before renaming.

---

### Cross-System Dependencies

**→ `vigil_service.compute_news2`:** Called in `_insert_vitals`. Signature: `compute_news2(hr, sbp, rr, temp, spo2, consciousness="alert", on_oxygen=False) -> dict`. The dict must have `score`, `level`, `raw_news2`, and `drivers` keys. If you change this return shape, `_insert_vitals` breaks (it reads `risk["score"]`, `risk["level"]`, `risk["drivers"]`, `risk.get("raw_news2")`).

**→ `audit_service.log_audit_event`:** Called at every step. Any change to the function signature propagates across this entire file.

**→ `database.db_session`:** Used for all writes. Schema changes to `vitals`, `observations`, `ingestion_events`, `risk_scores`, `messages`, or `channels` will affect this file.

**→ Socket.IO (`ws.realtime.sio`):** Events emitted: `vitals:update`, `message:new`, `patient_status_update`. The frontend listens for exactly these event names. Renaming them breaks live updates.

**→ Admin IngestionMonitor UI:** Reads `GET /api/ingest/status` response shape. The frontend filters and displays `parse_status` values and the field names listed in the status endpoint section above.
