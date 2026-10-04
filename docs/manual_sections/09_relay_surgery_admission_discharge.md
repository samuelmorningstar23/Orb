## 9. Relay, Surgical Suite, Admission & Discharge

This section covers four modules that work together around the arc of a patient's hospital stay: **Relay** (the real-time messaging backbone that all other modules post alerts into), **Surgical Suite** (OR scheduling, emergency escalation, and team coordination), **Admission** (the moment a patient enters the hospital and all their records spring into existence), and **Discharge** (the AI-generated summary document and the secure token system that lets a patient read it after they leave).

---

### Module A: Relay

Relay is the care-team messaging system. Think of it as an internal Slack, but every conversation ("channel") is tied to a specific patient case, a department, or a broadcast feed. Messages can be plain text, structured system alerts, or AI-generated "ambient action cards" that a clinician can approve with a single click. Relay is also the delivery pipe that Surgery, Vigil, and Sage use when they need to notify humans about something important.

---

#### `server/services/relay_service.py`

**Role:** Pure data layer for Relay. No HTTP, no WebSocket, just database reads and writes. The routes call these functions and then do the Socket.IO broadcasting.

**Libraries used:**
- `json`, Alert message bodies are stored as JSON strings in the `content` column so the frontend can distinguish a plain text chat message from a structured surgery-prep alert.
- `database.get_db` / `database.db_session`, Standard Orb database helpers (see Section 2).

---

##### `get_channels_for_user(user_id, user_role) -> list`

**What it does:** Returns the list of messaging channels this user is allowed to see, decorated with live patient risk data so the frontend can sort critical patients to the top.

**How it works:**

1. Looks up the user's department from the `users` table, this is used later to filter department-level channels.
2. Runs a single large SQL query that joins `channels` → `patients` → `risk_scores`. The query uses four correlated subqueries to compute, without a separate Python pass:
   - `unread_count`, messages where `read_by` does not contain the user's ID (stored as a substring-matched string like `%user123%`)
   - `last_message`, the most recent message text (for the channel list preview)
   - `alert_count`, how many `alert`-type messages arrived in the last 24 hours
   - `last_activity`, timestamp of the most recent message (used for sorting)
3. Sorts results: `critical` risk patients first, then `elevated`, then everyone else, then by recency of last activity.
4. Applies a **department filter** in Python:
   - Orphaned `case_room` channels (no patient linked and no messages ever sent) are dropped.
   - `department` channels are shown only if the user's department name matches the channel name, OR if the user is an admin. Surgery department channels are also shown to Anesthesiology users (they share the same OR context).

**Output:** A list of dicts, one per channel, with fields: `id`, `name`, `type`, `patient_id`, `mrn`, `diagnosis`, `ward`, `bed`, `risk_score`, `risk_level`, `unread_count`, `last_message`, `last_activity`, `alert_count`, `has_alert`.

⚠️ **If you change this:** Adding a new channel `type` requires adding filter logic here or the new type will be shown to everyone (or no one). Changing the `read_by` storage format from a substring-matched string to a proper join table means `unread_count` breaks, you must update the subquery.

---

##### `get_user_alerts(user_id, user_role) -> list`

**What it does:** Returns all active, unresolved alert messages in the last 48 hours that are specifically targeted at this user.

**How it works:**

1. Fetches all messages with `message_type = 'alert'` from the last 48 hours.
2. Parses each message's JSON `content` blob. If it doesn't have `is_structured: true`, it is skipped, only structured alerts (created by services like Surgery) are included.
3. Filters by `status`: alerts already marked `resolved` or `escalated` are excluded.
4. Determines if the current user is a target by checking three fallback levels:
   - **Explicit user match**, the alert's `target_users` list contains this user's ID.
   - **Role + department match**, the alert's `target_roles` contains this user's role AND `target_departments` contains their department.
   - **Admin catch-all**, admins see all active alerts regardless.

**Output:** A list of alert message dicts (same shape as `get_channel_messages` output).

⚠️ **If you change this:** If you add a new alert targeting mechanism (e.g., on-call schedule), add the check inside the `is_target` block. If you change the alert JSON schema (remove `is_structured`, rename `target_users`), old alerts in the database will become invisible.

---

##### `get_channel_messages(channel_id, limit=500, offset=0) -> list`

**What it does:** Paginates messages for a channel in chronological order.

**How it works:** Uses a double-query trick to get the N most recent messages but return them oldest-first. The inner query fetches the latest N rows with `ORDER BY timestamp DESC LIMIT ? OFFSET ?`; the outer `sub` query re-sorts them ascending. This avoids loading the entire message history into memory.

**Output:** List of message dicts: `id`, `channel_id`, `sender_id`, `sender_name`, `sender_role`, `content`, `message_type`, `timestamp`.

---

##### `send_message(channel_id, sender_id, sender_name, sender_role, content, message_type='text') -> dict`

**What it does:** Inserts a new message row and returns the inserted record.

**How it works:** Single `INSERT INTO messages` then an immediate `SELECT` on the new row ID to return the full record (including the auto-set `timestamp`). This returned dict is what the routes then broadcast over Socket.IO.

**Message types used across Orb:** `text`, `alert`, `system`, `ambient_action`, `surgical_alert`. The `message_type` column drives how the frontend renders the message, plain bubble, red alert card, actionable prep card, etc.

⚠️ **If you change this:** If you add a new `message_type`, you must also update the frontend `RelayPanel` message renderer; otherwise the new type will display as raw JSON text.

---

#### `server/routes/relay_routes.py`

**Role:** FastAPI router that exposes Relay's REST API at the `/api/relay` prefix, and fires Socket.IO events to push updates to connected browser clients. This is the largest file in this group (~620 lines) because it hosts not just the messaging endpoints but also the full Sage agentic order workflow.

**Libraries used:**
- `fastapi`, `APIRouter`, `Depends`, `HTTPException`, `BackgroundTasks`
- `pydantic`, Request body models
- `auth.require_permission`, All Relay endpoints require specific permission strings checked against the user's role
- `ws.realtime.sio`, The shared Socket.IO server instance (see Section 1); used to broadcast events to connected browser clients

**Router prefix:** `/api/relay`

---

##### `GET /api/relay/channels`: `list_channels`

**Permission required:** `relay.read`

Calls `get_channels_for_user` and returns the result directly. No pagination, the full channel list is always returned.

---

##### `GET /api/relay/alerts`: `list_alerts`

**Permission required:** `relay.read`

Calls `get_user_alerts` and returns targeted active alerts for the current user. The frontend uses this to populate the alert badge / notification panel.

---

##### `GET /api/relay/channels/{channel_id}/messages`: `get_messages`

**Permission required:** `relay.read`

**Query params:** `limit` (default 50), `offset` (default 0).

Calls `get_channel_messages`. No channel membership check, any user with `relay.read` can read any channel. (Access is governed by which channels `list_channels` exposes to a given user, not by per-message enforcement.)

---

##### `GET /api/relay/channels/{channel_id}/sage_memory`: `get_sage_memory`

**Permission required:** `relay.read`

Returns the Sage Ambient memory object for the patient linked to this channel. This is the running AI summary of what has been discussed: patient summary, discussion summary, current concern, likely next steps, unresolved questions, active intents, and risk flags.

First **checks that the channel is in the user's accessible channel list** (by calling `get_channels_for_user` and scanning the result), this is the nearest thing to a per-channel access gate. Returns 403 if not found.

Queries `sage_room_memory WHERE room_id = ?` and returns parsed JSON fields.

---

##### `GET /api/relay/channels/{channel_id}/sage_actions`: `get_sage_actions`

**Permission required:** `relay.read`

Returns the list of actions Sage Ambient has suggested for this channel (e.g., "Add metformin 500mg twice daily"). Each action has a `state` (suggested / acknowledged / dismissed / confirmed / failed). If a `sage_handoffs` row exists for the action, the handoff status is embedded inline (so the frontend can show whether the downstream order was placed).

Same channel-membership check as `get_sage_memory`.

---

##### `POST /api/relay/channels/{channel_id}/sage_actions/{action_id}/transition`: `transition_sage_action`

**Permission required:** `relay.execute_action`

Moves a Sage action through its lifecycle: `suggested → acknowledged → confirmed / dismissed`. Contains several safety rules:
- Cannot transition from a terminal state (`confirmed` or `dismissed`).
- Cannot confirm a `failed` action.
- Uses **optimistic locking** via a `version` integer on the `sage_actions` row. The client must send back the version it last read; if another user changed the action in the meantime, the UPDATE affects 0 rows and the server returns HTTP 409 ("conflict, please refresh").

On success: inserts a `sage_action_events` row for audit. If the new state is `confirmed`, immediately calls `create_and_dispatch_handoff` to propagate the confirmed action to the relevant downstream module (e.g., Helix for medication orders).

**Real-time event emitted:** `sio.emit("sage:action_update", {"channel_id": ...}, room=channel_id)`, tells all browsers in that channel room to refresh their action list.

---

##### `POST /api/relay/channels/{channel_id}/messages`: `post_message`

**Permission required:** `relay.send`

The main send-message endpoint. Calls `send_message` service function, then fires `sage_ambient_observe` as a background task (see Section 5, Sage will passively read the new message and potentially generate a new action suggestion). Returns the saved message dict.

⚠️ **If you change this:** The route does NOT emit a WebSocket event after saving, real-time delivery of new messages is handled by the Socket.IO event layer in `ws/realtime.py` separately (via the `message:new` event that gets emitted by the Socket.IO `send_message` handler, not this REST endpoint). If you bypass the Socket.IO layer (e.g., direct DB insert), connected browsers will not see the message in real time.

---

##### `POST /api/relay/actions/{message_id}/execute`: removed 2026-10-03

**This endpoint no longer exists.** It executed legacy `ambient_action` cards from a message row (add, stop or replace a medication, discharge, surgery prep) under `relay.execute_action`. No screen had called it since the cards were retired, nothing writes new cards, and clients can no longer post a message of that type (`server/models/message.py`: `MessageCreate.message_type` accepts `"text"` only), so it was an unused door onto the chart. Legacy `ambient_action` rows stay in `messages`; the Relay panel and Sage's room context skip them. The removal is recorded in a comment in `server/routes/relay_routes.py`, just above `resolve_alert`. A Sage-suggested action is acted on through `transition_sage_action` and `confirm_agentic_action`, both documented in this section.

---

##### `POST /api/relay/alerts/{message_id}/resolve`: `resolve_alert`

**Permission required:** `relay.send`

Marks an active alert as `resolved` (if `action == "confirm"`) or `escalated` (any other action). Mutates the alert message's JSON `content` in-place by reading it, updating the `status` and `resolved_by` fields, and writing it back.

**Real-time event emitted:** `sio.emit("message:update", msg_data, room=channel_id)`, pushes the updated alert to all browsers in the channel so the alert card changes state immediately without a page refresh.

---

##### `GET /api/relay/channels/{channel_id}/sage_actions/{action_id}/pre_flight`: `pre_flight_sage_action`

**Permission required:** `relay.execute_action`

Before a clinician commits an agentic medication order, this endpoint runs a pre-check:
1. Resolves the target drug name to the Helix master medication catalog via phonetic/fuzzy matching (`rxnorm_reconciler.reconcile_medication`).
2. Checks live inventory via `helix_service.pre_flight_inventory_check`.
3. If out of stock, finds a therapeutic alternative.
4. Flags the drug as high-risk if it matches a hard-coded list of dangerous drug names (epinephrine, heparin, insulin, potassium, tPA, morphine, fentanyl, oxycodone, hydromorphone, opioid).

Returns all findings to the frontend so the clinician sees a full pre-flight summary before clicking confirm.

---

##### `POST /api/relay/channels/{channel_id}/sage_actions/{action_id}/confirm_agentic`: `confirm_agentic_action`

**Permission required:** `relay.execute_action`

The most security-sensitive Relay endpoint, this is what actually places a medication order into Helix after a clinician enters their PIN. Steps:

1. **PIN verification**, Computes SHA-256 of the submitted PIN and compares to `users.pin_hash`.
2. **Reconciliation**, Re-runs medication reconciliation to get the canonical Helix drug ID.
3. **High-risk flag**, Sets `is_high_risk` metadata (currently does not block single-doctor sign-off, but is recorded in the audit log).
4. **Stock check**, If out of stock, attempts automatic therapeutic substitution; if no alternative and no `override_reason` is given, returns HTTP 400.
5. **Helix order**, Calls `helix_service.create_order` to place the actual order.
6. **Cryptographic audit hash chain**, Builds a delegation hash by chaining: source message ID → SHA-256 of (drug + rationale) → SHA-256 of (user ID + PIN). This hash chain is stored in `audit_logs` so that the exact sequence of LLM output → human verification → order can be reconstructed forensically.
7. **Audit log**, Inserts into `audit_logs` with `event_category = 'clinical_mutation'` and `action_type = 'agentic_med_confirm'`.

**Real-time events emitted:**
- `sio.emit("sage:action_update", ...)`, refreshes the action card state in the Relay panel.
- `sio.emit("patient:timeline_updated", ...)`, tells the patient timeline to reload.

⚠️ **If you change this:** The delegation hash chain links the LLM prompt content to the final order. If you change the hash inputs (e.g., add a field), existing audit records will have hashes that cannot be recomputed with the new formula, document the version change. If you add co-signer enforcement for high-risk drugs (currently deferred), the co-signer PIN verification must happen here before the Helix call.

---

### Module B: Surgical Suite

Surgical Suite manages elective OR scheduling and emergency surgery escalation. There are three files: a general service (schedule queries and risk assessment), an escalation service (the emergency prep pipeline), and a route file.

---

#### `server/services/surgery_service.py`

**Role:** Provides three capabilities: (1) reading the surgery schedule grouped by date, (2) reading full detail for a single surgery, and (3) streaming a MedGemma pre-operative risk assessment over SSE, and (4) triggering a team-wide surgery prep alert via Relay.

**Libraries used:**
- `json`, Parses JSON columns stored in `surgeries` rows (checklist, instruments, staff list, complications).
- `httpx`, Async HTTP client used to call the local Ollama LLM endpoint.
- `datetime` / `timedelta` / `timezone`, Date arithmetic for weekly schedule windowing.
- `services.llm_queue_service.llm_queue`, Orb's LLM concurrency limiter (prevents multiple simultaneous LLM calls from overloading the local model).

---

##### `get_weekly_schedule(surgeon_id, days=14) -> list`

Returns upcoming surgeries for a specific surgeon, grouped by date into a "week view" structure.

**How it works:**
1. Computes a date window: today through `today + days`.
2. Queries the `surgeries` table joined with `patients` and the most recent `risk_scores` row per patient.
3. Groups results into a dict keyed by date string. For each date, builds a `{date, day_label, is_today, surgeries: [...]}` dict.
4. Returns the dates as a list in ascending order.

Each surgery entry in the list contains patient demographics, procedure name, scheduled time, estimated duration in minutes, OR name, current status, and risk level/score from the latest risk assessment.

---

##### `get_surgery_detail(surgery_id) -> dict`

Returns the complete record for a single surgery, including all JSON-encoded clinical arrays and a live import of the patient's Sage context.

**How it works:**
1. Queries `surgeries` joined with `patients` (demographics + diagnosis + admission date) and the most recent `risk_scores` row.
2. Parses five JSON columns using a helper `_parse_json(val, default)` that returns an empty list on failure, so a NULL or malformed JSON column never crashes the response.
3. Calls `sage_service._assemble_patient_context(db, patient_id)` to pull in the AI-generated patient context (medications, vitals, clinical notes in a structured form). This is stored in `imported_context` and surfaced in the Surgical Suite's "Handoff Context" panel.

**Output fields include:** all demographic fields, `procedure_name`, `diagnosis`, `scheduled_date`, `scheduled_time`, `estimated_duration_min`, `operating_room`, `status`, `co_morbidities` (list), `surgical_checklist` (list), `instruments_required` (list), `staff_required` (list of `{role, name}` dicts), `risk_assessment` (text), `possible_complications` (list), `success_probability` (number), and `imported_context`.

⚠️ **If you change this:** If you rename any JSON column in `surgeries` (e.g., `staff_required` → `team`), the `_parse_json` calls here must be updated, and so must `get_emergency_queue` in the escalation service. The frontend SurgicalSuitePanel depends on these exact field names.

---

##### `generate_risk_assessment(surgery_id) -> async generator`

An `async` generator that streams a pre-operative risk assessment from MedGemma, formatted as Server-Sent Events (SSE).

**How it works:**
1. Calls `get_surgery_detail` to get all clinical context.
2. Builds a structured clinical prompt including: patient demographics, MRN, admission and surgical diagnoses, co-morbidities, current risk level and score, estimated duration in minutes, and the full patient context. Asks for eight specific sections: ASA classification, surgical risk score, pre-op concerns, anesthetic considerations, intra-operative risk factors, post-op monitoring priorities, recommended optimizations, and an overall risk-benefit summary.
3. Acquires the LLM queue at priority 2 (medium, below urgent clinical alerts).
4. Streams the Ollama response line by line, yielding each content chunk as `data: {"content": "..."}`.
5. If the LLM call fails for any reason, yields a pre-written fallback assessment instead of an error, so the UI always shows something clinically useful.
6. Always ends with `data: [DONE]`.

**SSE format:** Each yielded string is a complete SSE chunk starting with `data:`. The route wraps this generator in a `StreamingResponse`.

---

##### `trigger_surgery_prep(surgery_id, sender_id, sender_name, sender_role) -> dict`

Sends a structured `surgery_prep` alert into Relay channels to notify the surgical team that a procedure is imminent.

**How it works:**
1. Gets surgery detail.
2. Builds a JSON alert payload with: `type: "surgery_prep"`, surgery and patient identifiers, OR name, scheduled time, team member names, risk level, target user IDs (from `staff_required`), role fallback (`target_roles: ["doctor", "nurse"]`), department target (`["Surgery", "Anesthesiology"]`), urgency (`"critical"`), and a human-readable action directive.
3. **Deduplication check**, queries `dept-surgery` channel for any `surgery_prep` alert for this surgery created in the last 4 hours. If one exists, returns `{"status": "duplicate"}` immediately without creating a second alert.
4. Sends the alert to the **`dept-surgery`** channel as `message_type: "alert"`, this is the primary target so the whole surgery department sees it.
5. Also attempts to send to the patient's individual **`case-{patient_id}`** channel for patient-level visibility. If that channel doesn't exist yet, the error is silently swallowed.

⚠️ **If you change this:** The channel ID `"dept-surgery"` is hard-coded. If you rename or replace this channel in the database seed, the deduplication check and the primary alert send will silently fail.

---

#### `server/services/surgery_escalation_service.py`

**Role:** Manages the full emergency surgery pipeline, from a surgeon drafting a prep request, to a clinician confirming it, to MedGemma generating the complete surgical package (risk assessment, checklist, instruments, team). Uses an audit trail in `surgery_prep_requests` and an idempotency key to prevent double-booking if a network glitch causes the confirm button to be clicked twice.

**Libraries used:**
- `json`, Serializes surgical package fields to the `surgeries` table.
- `uuid`, Generates unique surgery IDs.
- `asyncio`, `wait_for` imposes a hard 60-second timeout on MedGemma generation.
- `httpx`, Would be used for the real Ollama call (currently replaced with a demo override).

---

##### `create_surgery_prep_request(patient_id, channel_id, requested_by, requested_by_name, procedure_name, indication, urgency, clinical_context) -> dict`

Creates an audit trail record in `surgery_prep_requests` with `state = 'drafted'`.

Generates an `idempotency_key` from `prep_{channel_id}_{unix_timestamp}`. This key is returned to the caller and must be passed back to `confirm_surgery_prep` to prevent accidental double-confirmation.

Returns `{id, idempotency_key}`.

---

##### `confirm_surgery_prep(prep_id, confirmed_by, confirmed_by_name, idempotency_key) -> dict`

Idempotent confirmation step, if called twice with the same key, the second call returns the same `surgery_id` without creating a duplicate row.

**Steps:**
1. Checks if a `surgery_prep_requests` row with this `idempotency_key` already has `state = 'resolved'`. If so, returns `{surgery_id, status: "already_created"}`.
2. Looks up the prep request and the patient.
3. **Demo override for PAT007 (Alex):** Checks for the pre-seeded `SURG-ALEX-EM` row and reuses it if present, to keep the demo recording deterministic.
4. For any other patient, generates a new `surgery_id` and inserts a `surgeries` row with `status = 'emergency_pending'`, assigned to OR-E (Emergency), 120 minutes estimated duration, and default surgeon `DOC004`.
5. Updates the prep request to `state = 'resolved'`, recording who confirmed and when.

Returns `{surgery_id, status: "created"}` or `{surgery_id, status: "already_created"}`.

---

##### `generate_surgical_prep_package_async(surgery_id, prep_id)`: async

Background task that populates the surgical package fields on the `surgeries` row and transitions the surgery status to `emergency_ready`. Always completes within 60 seconds (hard timeout via `asyncio.wait_for`).

**Internal helper `_finalize_surgery(data, ai_status, reason)`:** writes all package fields to the `surgeries` row in a single `UPDATE` and sets `status = 'emergency_ready'`. Stores a JSON blob in the `notes` column including `ai_package_status` (`"generated"` or `"failed"`) and `finalized_at` timestamp, the frontend reads these to show a "AI Generated" or "Fallback" badge.

**Fallback data (`FALLBACK_DATA`):** A complete but generic surgical package (standard checklist, generic team roles, placeholder risk text) used if MedGemma fails or times out. The UI always shows a complete package, it is never left blank.

**`_do_generation()` inner async function:** Currently a **demo override**, instantly resolves with a hard-coded high-fidelity package for Alex's Hartmann's procedure (laparotomy for faecal peritonitis). In production this would be replaced with a real Ollama call. The function first sets the surgery status to `emergency_context_generating` so the frontend can show a spinner, waits 1.5 seconds for visual polish, then calls `_finalize_surgery` with the perfect demo data.

**Surgery status lifecycle for emergency cases:**
```
emergency_pending
    → emergency_context_generating  (while MedGemma runs)
    → emergency_ready               (package complete, team can view and go)
    → in_progress                   (surgery started, all team confirmed)
    → completed
```

⚠️ **If you change this:** If you replace the demo override with a real LLM call, keep the `_finalize_surgery` inner function and the 60-second `asyncio.wait_for` ceiling, the fallback path is what guarantees the UI is never stuck showing a spinner forever.

---

##### `get_emergency_queue() -> list`

Queries all surgeries with `status LIKE 'emergency_%'` and returns them with patient demographics. Parses all JSON columns inline (same five fields as `get_surgery_detail`). This is polled by the frontend emergency dashboard.

---

##### `acknowledge_emergency(surgery_id)`

Sets `status = 'emergency_acknowledged'` on a surgery row. A simple state transition called when a surgeon confirms they have seen the emergency alert.

---

#### `server/routes/surgery_routes.py`

**Role:** FastAPI router at `/api/surgery`. Exposes schedule reads, emergency queue, team assignment, and surgery lifecycle transitions.

**Router prefix:** `/api/surgery`

---

##### `GET /api/surgery/schedule`: `get_surgery_schedule`

**Permission required:** `surgery.read`

**Query param:** `days` (default 7). Returns the full schedule as a day-by-day array, including empty days (so the frontend can show "no surgeries scheduled" for a day rather than silently skipping it). Pulls surgeon name via a LEFT JOIN to `users`.

---

##### `GET /api/surgery/emergency-queue`: `get_emergency_queue`

**Permission required:** `surgery.read`

Queries surgeries with status `emergency_pending`, `emergency_ready`, or `emergency_context_generating`. The frontend polls this to show the live emergency queue and spinner states.

---

##### `GET /api/surgery/cases`: `get_surgery_cases`

**Permission required:** `surgery.read`

Returns all active surgical cases (status not `completed` or `cancelled`) with their team assignments embedded. Fetches team assignments in one bulk query (building a `WHERE surgery_id IN (...)` clause dynamically) and assigns them to cases in Python, avoiding N+1 queries.

---

##### `GET /api/surgery/{surgery_id}`: `get_surgery_detail`

**Permission required:** `surgery.read`

Route-level surgery detail (simpler than `surgery_service.get_surgery_detail`, no Sage context import). Joins `patient_profiles` to get `comorbidities` and `allergies`. Parses all JSON columns with try/except. Also checks the `notes` column for a JSON object containing `imported_context` (placed there by the escalation package generator) and surfaces it at the top level.

---

##### `POST /api/surgery/{surgery_id}/alert-team`: `alert_surgical_team`

**Permission required:** `surgery.write`

**Body:** `{"team": [{"user_id": "...", "role": "..."}]}`

Assigns a list of staff members to a surgery and notifies each one via a `surgical_alert` message in the patient's case room channel.

**Steps:**
1. Looks up the patient's case room channel (type `case_room`).
2. Ensures the `surgery_team_assignments` table exists (defensive `CREATE TABLE IF NOT EXISTS`, this table is used to track per-person confirm/decline status).
3. Clears any previous assignments for this surgery with `DELETE`.
4. For each team member: inserts a `surgery_team_assignments` row with `status = 'alerted'`, and sends a `surgical_alert` message to the case room channel containing a JSON payload with `target_user_id`, `surgery_id`, and the assigned role.
5. Sets the surgery status to `emergency_ready`.

---

##### `POST /api/surgery/{surgery_id}/team-response`: `surgical_team_response`

**Auth:** Any logged-in user (`get_current_user`, no specific permission required, the user's own assignment is the gate).

**Body:** `{"action": "confirm"}` or `{"action": "decline"}`

Handles a team member accepting or declining their surgical assignment.
- Updates their `surgery_team_assignments.status` to `confirmed` or `declined`.
- Posts a system message to the case room: "SYSTEM: Dr. X (Anesthesiologist) has confirmed/declined the surgical assignment."
- If `declined`: attempts to re-route the role to a fallback user. Currently hardcoded for demo purposes (`anesthetist_2` replaces `anesthetist_1`). In a real deployment this would query an on-call schedule. The replacement assignment is posted as a `surgical_alert` attributed to "Sage Ambient".

---

##### `POST /api/surgery/{surgery_id}/start`: `start_surgery`

**Permission required:** `surgery.write`

Enforces a readiness gate before surgery can begin: there must be zero `alerted` assignments remaining (everyone has responded) and at least one `confirmed` assignment. If either condition fails, returns HTTP 400. On success, sets status to `in_progress`.

---

##### `POST /api/surgery/{surgery_id}/complete`: `complete_surgery`

**Permission required:** `surgery.write`

Marks the surgery `completed` and auto-generates a mock post-operative Scribe note using `save_scribe_note` with a canned SOAP-formatted text. The note is saved as `note_format = 'soap'` and `ai_generated = True`. The surgery then disappears from `/api/surgery/cases` because the route filters on `status NOT IN ('completed', 'cancelled')`.

---

### Module C: Admission

Admission is a single-function module. When a new patient arrives, one call to `admit_patient` atomically creates every record Orb needs across all its modules.

---

#### `server/services/admission_service.py`

**Role:** Transactional orchestrator that creates a patient from scratch across six tables in a single database session.

**Libraries used:**
- `uuid`, Generates unique IDs for patient, admission record, and channel.
- `json`, Serializes list fields (comorbidities, allergies, medications, isolation precautions, vitals, risk flags).
- `datetime`, UTC timestamp used consistently for all `created_at` fields.
- `ws.realtime.sio`, Imported at module level (though not currently called inside `admit_patient`, real-time notification of admission is handled by the caller or a background task).

---

##### `admit_patient(data: dict, user: dict) -> dict`

**What it does:** Creates all records for a newly admitted patient in one atomic database transaction. If any insert fails, the entire transaction rolls back.

**Inputs:**
- `data`, the admission form fields (from `AdmissionRequest.dict()`)
- `user`, the logged-in staff member performing the admission (`id`, `name`)

**Six tables written in sequence:**

1. **`patients`**, The master identity record. Generates `patient_id` as `PAT-{uuid8}` and `mrn` as `MRN-{uuid6}` (unless the form supplies an existing MRN). Sets `status = 'admitted'`.

2. **`admissions`**, The admission context record. Stores where the patient came from (`admission_source`), what type of admission it is (`admission_type`), ward/bed assignment, attending doctor, chief complaint, initial diagnosis, triage severity (1–5), admitting notes, nursing notes, isolation precautions (JSON list), code status, and initial vitals as a JSON blob.

3. **`patient_profiles`**, The longitudinal clinical history record. Stores comorbidities, past medical history, past surgical history, allergies, current medications, family history, and social history, all as JSON arrays or text.

4. **`vitals`**, If the admission form includes at least one vital sign (HR, systolic BP, or SpO2), a vitals row is inserted immediately. This makes the patient visible in Vigil's monitoring view right away, without waiting for the next vitals chart to be entered.

5. **`channels`**, A `case_room` channel is created with the name "Case: {patient name}" and linked to the new `patient_id`. The `channel_id` is returned to the caller so the frontend can navigate there directly.

   An initial system message is also inserted into this channel summarizing the admission: name, age, sex, ward/bed, source/type, chief complaint, initial diagnosis, plus allergies and comorbidities if provided. This means the Relay channel is never empty, it always starts with a useful admission summary.

6. **`sage_room_memory`**, Seeds the Sage Ambient memory for this channel. Writes a patient summary sentence and a discussion summary (from `admission_source` + `admitting_notes`). If triage severity is 1 or 2 (high acuity), immediately adds a risk flag so Sage knows to be alert from the start.

**Returns:** `{success: true, patient_id, admission_id, channel_id, mrn}`

**ADT event:** This function is the Orb equivalent of an ADT ("Admit, Discharge, Transfer") A01 event (patient admission) in HL7/FHIR terminology. All six inserts together constitute the admission event.

⚠️ **If you change this:** All six inserts run inside a single `db_session()` context manager. If you add a seventh table, add it inside the same `with db_session() as db:` block to keep it transactional. If you add a Socket.IO emit inside this function, be careful, the `sio` import at the top is async and `emit` is a coroutine; this function is synchronous, so you cannot `await` it here. Emit from the route instead.

---

#### `server/routes/admission_routes.py`

**Role:** FastAPI router at `/api/admissions` with a single endpoint.

---

##### `POST /api/admissions/admit`: `process_admission`

**Auth:** Any logged-in user (`get_current_user`).

Validates the request body against `AdmissionRequest`, converts it to a plain dict, and calls `admit_patient`. On success, returns the new IDs. On any exception, prints a full traceback to the server log and returns HTTP 500 with the error message.

**`AdmissionRequest` model fields** (all optional except `name`, `admission_source`, `admission_type`, `department`, `ward`, `bed`, `attending_doctor_id`, `chief_complaint`):
- Patient identity: `name`, `age`, `sex`, `phone`, `emergency_contact_name`, `emergency_contact_phone`, `address`, `mrn`
- Admission context: `admission_source`, `admission_type`, `department`, `ward`, `bed`, `attending_doctor_id`, `referring_clinician`
- Clinical: `chief_complaint`, `initial_diagnosis`, `triage_severity`
- History: `comorbidities`, `past_medical_history`, `past_surgical_history`, `allergies`, `current_medications`, `family_history`, `social_history`
- Vitals: `vitals` (dict with keys `hr`, `bp_sys`, `bp_dia`, `spo2`, `temperature`, `rr`)
- Notes: `admitting_notes`, `nursing_notes`, `isolation_precautions`, `code_status`

---

### Module D: Discharge

Discharge has two services and one route file. The split is deliberate: `discharge_service.py` is used by hospital staff to generate and issue a discharge summary; `discharge_access_service.py` and `discharge_access_routes.py` are a completely separate, authentication-free public API used by the patient (via a QR code on their phone) to read the document after they have left the hospital.

---

#### `server/services/discharge_service.py`

**Role:** Staff-facing discharge tools. Builds a patient timeline, calls MedGemma to write the discharge summary, generates a QR code, creates the secure access token, and updates the patient's status to `discharged`.

**Libraries used:**
- `json`, Serializes the timeline to the `discharge_summaries` table.
- `uuid`, Not used for the token (which uses `secrets`), used for the QR filename.
- `hashlib.sha256`, Hashes the raw token for storage (the raw token is never stored).
- `secrets.token_urlsafe(32)`, Cryptographically secure random token (256 bits of entropy).
- `qrcode` / `qrcode.image.pure.PyPNGImage`, Optional dependency; absent if not installed (checked via `HAS_QR`).
- `httpx`, Calls Ollama for summary generation (non-streaming; waits for the full response).
- `config.DISCHARGE_ACCESS_TTL_DAYS`, Controls how long the patient's link remains valid.
- `services.discharge_access_service.public_link_base_status`, Reads `config.PUBLIC_APP_BASE_URL` (env `ORB_PUBLIC_APP_BASE_URL`, the address a patient's phone opens) at discharge time and decides whether a link may be built on it.
- `services.audit_service.log_audit_event`, Records the discharge event in the audit ledger.

---

##### `build_patient_timeline(patient_id) -> list`

Assembles a chronological list of all significant events in the patient's hospital stay from five sources:

| Source table | Event type | Content |
|---|---|---|
| `patients` | `admission` | "Admitted to ward X, Bed Y, Diagnosis Z" |
| `clinical_documents` | `note` | First 200 chars of the note + "…" if truncated |
| `scribe_notes` | `scribe` | First 200 chars of structured note or raw transcript |
| `lens_analyses` | `imaging` | First 200 chars of the analysis |
| `risk_scores` (critical only) | `alert` | Risk score and driver list |

All events are collected into a flat list, then sorted by timestamp. This timeline is saved alongside the discharge summary and is also returned to the patient's discharge access portal as a visual journey log.

Note: this function uses `get_db()` (raw connection) rather than `db_session()` (context manager) and must close the connection manually with `db.close()`. Do not add new queries using `db` after the `db.close()` call.

---

##### `generate_discharge_summary(patient_id) -> str`: async

Generates the discharge summary document by calling the local LLM.

**How it works:**
1. Loads patient demographics, all clinical documents (ordered chronologically), the most recent vitals, and the five most recent risk scores.
2. Builds a prompt that includes all clinical notes as `[doc_type] timestamp: content` lines, and specifies a rigid document format (discharge summary header, admission diagnosis, hospital course, procedures, condition at discharge, discharge medications, follow-up instructions, patient education, attending physician).
3. Calls Ollama via `httpx` in **non-streaming mode** (waits for the full response). Uses `llm_queue.acquire(priority=2)` to queue behind urgent requests.
4. Returns the raw LLM text string.

Returns an empty string if the patient is not found, or a partial/fallback string if the LLM call fails (callers should handle empty returns).

---

##### `generate_qr_code(patient_id, summary_url) -> str`

Generates a PNG QR code image pointing to the patient's discharge summary URL. Saves the file to `UPLOAD_DIR` with a filename of `qr_{patient_id}_{random6}.png`. Returns the file path (stored in `discharge_summaries.qr_code_path`). Returns `""` if the `qrcode` library is not installed.

---

##### `discharge_patient(patient_id, author_id, summary, timeline) -> dict`

The main discharge action function. Called after both the summary and timeline are ready.

**The link is conditional, the discharge is not.** The discharge always completes (patient status, summary, timeline, the active admission and any open care bundles closed). A link and QR code are issued only when both of these hold:

- the patient has a usable date of birth on file, the second factor the portal checks (`discharge_access_service.second_factor_status`);
- the address the link is built on can be opened from a patient's phone (`discharge_access_service.public_link_base_status`). In the `prod` profile it refuses an `ORB_PUBLIC_APP_BASE_URL` that is unset, not `https`, or names this machine (`localhost`, `*.localhost`, `127.0.0.0/8`, `::1`, `0.0.0.0`). The shipped default, `http://localhost:5173`, is the Vite dev server and is refused. The `dev` profile accepts it.

When either fails, no token, URL or QR file is produced, the response carries `secure_link_issued: false` and a `secure_link_reason` for the discharge desk (both reasons when both fail), and a `discharge_link_not_issued` audit event names the cause.

**Security model, how the token works:**

1. `secrets.token_urlsafe(32)` generates a 256-bit cryptographically random token (the raw token). This is what goes into the URL and the QR code.
2. `hashlib.sha256(raw_token.encode()).hexdigest()` produces a hash of the raw token. **Only this hash is stored in the database** (in both `token_id` and `token_hash`). This means that even if someone reads the `discharge_access_tokens` table, they cannot reconstruct valid URLs.
3. The summary URL is `{ORB_PUBLIC_APP_BASE_URL}/discharge/{raw_token}`, with any trailing slash on the setting dropped.
4. The QR code is generated from this URL.
5. The token hash, patient ID, expiry time and issuing user are written to `discharge_access_tokens`.
6. The discharge summary and timeline are written to `discharge_summaries` as a draft; the patient sees the text only after a clinician has reviewed it.
7. `patients.status` is set to `'discharged'`.
8. An audit event `discharge_link_created` is logged.

**Expiry:** Controlled by `DISCHARGE_ACCESS_TTL_DAYS` from config. Default behavior is that the link expires after this many days and becomes permanently inaccessible.

Returns `{status, summary_id, discharged_at, review_status, summary_url, qr_code_url, secure_link_issued, secure_link_reason}`. `summary_url` and `qr_code_url` are null when no link was issued; `qr_code_url` is an API path, never the PNG's location on disk.

⚠️ **If you change this:** Never store the raw token, only the hash. If you add multi-use token support (the link can be opened more than once), keep this function the same and only change `discharge_access_service.py`. If you change the URL pattern from `/discharge/{token}` to anything else, update the frontend DischargeAccess page route to match.

---

#### `server/services/discharge_access_service.py`

**Role:** The patient-facing token verification and summary retrieval system. No hospital login is needed to use this service, authentication is by the token in the URL plus the patient's date of birth. This service has multiple security layers to prevent brute-force attacks.

**Libraries used:**
- `jwt` (PyJWT), Used to issue and verify short-lived access grants after successful DOB verification.
- `hashlib.sha256`, Verifies the stored token hash.
- `hmac.compare_digest`, Constant-time string comparison for DOB verification (prevents timing attacks).
- `threading.Lock`, Protects the in-process IP throttle state from race conditions.
- `time`, Unix timestamps used in the sliding-window rate limiter.
- `config.DISCHARGE_ACCESS_MAX_ATTEMPTS`, Maximum failed DOB guesses before per-token lockout.
- `config.DISCHARGE_ACCESS_LOCK_MINUTES`, Lockout duration in minutes after too many failures.
- `config.JWT_SECRET` / `config.JWT_ALGORITHM`, Signs the short-lived access grant.

---

##### IP Throttle State (module-level)

```python
_VERIFY_IP_ATTEMPTS: Dict[str, list]   # {ip: [unix_timestamp, ...]}
_VERIFY_IP_LOCK: threading.Lock
_VERIFY_IP_MAX_ATTEMPTS = max(DISCHARGE_ACCESS_MAX_ATTEMPTS * 3, 15)
_VERIFY_IP_WINDOW_SECONDS = DISCHARGE_ACCESS_LOCK_MINUTES * 60
```

This is a **global, in-memory, sliding-window per-IP rate limiter** across ALL discharge tokens. It exists because per-token lockout alone is bypassable: an attacker holding many different discharge tokens (obtained by eavesdropping on QR codes) could guess DOBs across all of them without any single token accumulating enough failures to trigger its lockout.

**Important limitation:** This state lives in the Python process. If the server restarts (e.g., for a code deploy), the in-memory throttle resets. The code documents this as a known deferral, it should be moved to SQLite or Redis for durability. The current single-worker Uvicorn setup makes in-process state safe for now.

---

##### `_is_verify_ip_throttled(ip) -> bool` and `_record_verify_ip_attempt(ip)`

Helper pair for the sliding-window throttle. `_is_verify_ip_throttled` cleans the bucket (drops timestamps older than the window), then checks whether the count of remaining timestamps meets or exceeds `_VERIFY_IP_MAX_ATTEMPTS`. `_record_verify_ip_attempt` adds the current timestamp to the bucket. Both are protected by `_VERIFY_IP_LOCK`.

---

##### `second_factor_status(patient_id)` and `public_link_base_status()`

The two checks `discharge_service.discharge_patient` makes before it issues a link. Each returns `available` plus a `reason` written for the discharge desk.

- `second_factor_status`: is a date of birth on file in a shape the portal's date picker can send? `usable_dob` decides, and verification uses the same definition, so a link can never issue that verification would refuse.
- `public_link_base_status`: can a patient's phone open links built on `config.PUBLIC_APP_BASE_URL`? `public_address_problem` names the fault (not set, not https, this machine), and only the `prod` profile refuses. `log_public_address_problem` runs once in `main.lifespan` and logs an error at boot while it refuses. The Trust Center shows the same check as the "Patient discharge link address" row, in `prod` only: green on an https network address, amber otherwise.

It checks the address's shape, not the route to it: a patient's phone still needs a network path to the appliance (install runbook, section 7, item 6).

---

##### `get_token_record(db, token_id) -> Optional[dict]`

Simple helper: SELECT from `discharge_access_tokens WHERE token_id = ?`. Returns a plain dict or None.

---

##### `verify_discharge_access(token_id, dob, ip_address=None) -> dict`

The verification step a patient must pass to receive their discharge summary. Called when the patient opens the QR code link and submits their date of birth.

**Validation sequence (fail-fast order):**

1. **IP throttle check**, Checked first, before any database read, so a throttled IP cannot even probe whether a token exists.
2. **IP attempt recorded**, Every call (including throttled calls that pass the IP check) increments the per-IP sliding window.
3. **Token existence**, Returns `{status: "error", reason: "invalid_token"}` if no matching row. Does not distinguish "token was never issued" from "token is wrong", both look the same to the caller.
4. **Revocation**, Returns `{reason: "revoked"}` if `revoked_at` is set.
5. **Expiry**, Checks `expires_at` against current UTC time. Returns `{reason: "expired"}`.
6. **Per-token lockout**, If `locked_until` is in the future, returns `{reason: "locked", locked_until: ...}`. If the lockout has expired, clears it and resets `failed_attempts` to 0.
7. **DOB verification**, Uses `hmac.compare_digest(stored_dob, submitted_dob)` for constant-time comparison (prevents timing attacks on the low-entropy DOB second factor).
8. **On DOB failure**, Uses a single SQL `UPDATE … SET failed_attempts = failed_attempts + 1` followed by an immediate `SELECT` to read back the authoritative new value. This atomic increment prevents a race condition where two simultaneous wrong-DOB submissions both read `attempts = 4` and neither triggers the lockout. If `failed_attempts >= DISCHARGE_ACCESS_MAX_ATTEMPTS`, sets `locked_until`. Does NOT return the remaining attempts count (which would help an attacker know how many guesses remain).
9. **On DOB success**, Clears `failed_attempts` and sets `verified_at`. Issues a **short-lived JWT access grant** (1-hour expiry) with:
   - `sub: "discharge_access"`
   - `aud: "discharge"`, prevents staff JWTs (which use a different audience) from being replayed here
   - `token_id` and `patient_id` embedded in claims
   Returns `{status: "success", access_grant: "<jwt>", patient_id}`.

All outcomes (success and failure) are logged to the audit trail via `log_audit_event`.

⚠️ **If you change this:** The `aud: "discharge"` claim in the JWT payload is a security control, it prevents a staff authentication token (signed with the same `JWT_SECRET`) from being used to access the discharge summary endpoint. If you change `JWT_SECRET` for staff auth, you must also generate a new JWT for any existing valid discharge sessions. The code documents a deferred improvement: discharge grants should use a separate signing secret so the two trust domains are fully independent.

---

##### `get_verified_discharge_summary(token_id, access_grant, ip_address=None) -> dict`

Called after the patient has successfully verified their DOB and received an access grant. The grant is passed on every subsequent request to fetch the summary.

**How it works:**
1. Decodes the JWT with `audience="discharge"`, a staff token with a different `aud` would fail here even if signed with the same secret.
2. Verifies `sub == "discharge_access"` and `token_id` matches the URL parameter (prevents swapping the grant from one patient to read another patient's summary).
3. Re-checks the token record for revocation (handles the edge case where a token was revoked after the grant was issued but before this call).
4. Fetches the most recent discharge summary and timeline from `discharge_summaries`.
5. Fetches patient demographics (name, age, sex, ward, bed, diagnosis, admission date, attending doctor name via LEFT JOIN to `users`).
6. Fetches active medications from `medications WHERE status = 'active'`.
7. Updates `last_accessed_at` on the token record.
8. Logs `discharge_summary_viewed` to the audit trail.
9. Returns `{status: "success", summary, timeline, patient, medications}`.

---

#### `server/routes/discharge_access_routes.py`

**Role:** The **public** FastAPI router at `/api/discharge-access`. No JWT middleware is applied here, these endpoints are designed to be called by a patient's phone browser without any prior login.

**Router prefix:** `/api/discharge-access`

---

##### `POST /api/discharge-access/verify`: `verify_access`

**No authentication required.**

**Body:** `{"token": "<raw_token>", "dob": "YYYY-MM-DD"}`

Reads the client IP from `request.client.host`, calls `verify_discharge_access`, and returns the result. If the status is `"error"`, always returns **HTTP 401** regardless of the specific reason (`invalid_token`, `expired`, `locked`, `invalid_dob`), this prevents the caller from distinguishing between "no such token" and "wrong DOB," which would be an information leak.

---

##### `GET /api/discharge-access/{token}/summary`: `get_summary`

**No authentication required, the access grant IS the authentication.**

Reads the `Authorization: Bearer <grant>` header. If absent, returns HTTP 401 with `{reason: "missing_grant"}`. Extracts the JWT from the header, calls `get_verified_discharge_summary`, and returns the full summary payload.

Maps error reasons to HTTP status codes:
- `revoked` or `invalid_grant` → HTTP 403
- `grant_expired` or `not_found` → HTTP 401

---

### End-to-End Flow Summaries

#### Care-Team Messaging (Relay): Real-Time Flow

```
1. Clinician opens RelayPanel in the browser.
2. Browser connects to Socket.IO server; joins the channel room via sio.enter_room(sid, channel_id).
3. Browser calls GET /api/relay/channels → list_channels → sorted channel list.
4. Browser calls GET /api/relay/channels/{id}/messages → paginated history.
5. Clinician types a message, browser calls POST /api/relay/channels/{id}/messages.
6. Server inserts message → saves to DB → starts sage_ambient_observe as background task.
7. Socket.IO event "message:new" is emitted to the channel room by ws/realtime.py.
8. All other browsers in the room receive the event and append the message instantly.
9. sage_ambient_observe analyses the message; if it produces a new action, emits "sage:action_update".
10. RelayPanel reloads the action list via GET /api/relay/channels/{id}/sage_actions.
```

#### Emergency Surgery Escalation: State Flow

```
1. Clinician in case room types about emergency surgery → Sage suggests "prep_surgery" action card.
2. Clinician clicks approve on the ambient action card.
   → POST /api/relay/actions/{msg_id}/execute (action: "prep_surgery")
   → confirm_surgery_prep() creates surgery row, status = emergency_pending
   → generate_surgical_prep_package_async() runs in background
3. Surgery status → emergency_context_generating (frontend shows spinner)
4. MedGemma (or demo override) generates full package in < 60s.
5. Surgery status → emergency_ready; _finalize_surgery() writes all fields.
6. Surgeon views emergency queue (GET /api/surgery/emergency-queue), opens detail.
7. Surgeon assigns team (POST /api/surgery/{id}/alert-team).
   → surgery_team_assignments rows inserted, status = 'alerted'
   → surgical_alert messages sent to case room channel
8. Each team member sees the alert in Relay, clicks confirm.
   → POST /api/surgery/{id}/team-response (action: "confirm")
   → system message posted in case room: "Dr. X has confirmed"
9. All confirmed, no one pending → Surgeon clicks "Start Surgery".
   → POST /api/surgery/{id}/start → status = in_progress
10. Surgery done → POST /api/surgery/{id}/complete → status = completed, Scribe note saved.
```

#### Patient Discharge: Security Token Flow

```
1. Doctor triggers discharge (via ambient action or directly via API).
   → discharge_service.generate_discharge_summary() → LLM generates document
   → discharge_service.discharge_patient()
       ├── no date of birth on file, or (prod) no patient-reachable ORB_PUBLIC_APP_BASE_URL
       │     → discharge completes, no token, URL or QR; secure_link_reason for the desk
       ├── secrets.token_urlsafe(32) → raw_token
       ├── sha256(raw_token) → token_hash (only this is stored in DB)
       ├── summary URL = ORB_PUBLIC_APP_BASE_URL/discharge/{raw_token}
       ├── QR code PNG generated, saved to UPLOAD_DIR
       ├── discharge_access_tokens row inserted (token_hash, patient_id, expires_at)
       ├── discharge_summaries row inserted (summary, timeline JSON)
       └── patients.status = 'discharged'

2. Patient scans QR code on their phone. Opens /discharge/{raw_token} in browser.
3. Frontend DischargeAccess page shows DOB entry form.
4. Patient submits DOB:
   → POST /api/discharge-access/verify {token: raw_token, dob: "YYYY-MM-DD"}
   → IP throttle checked (global sliding window)
   → token_hash looked up, expiry checked, lockout checked
   → hmac.compare_digest(stored_dob, submitted_dob) [constant-time]
   → if match: JWT issued (aud="discharge", exp=+1h)
   → {status: "success", access_grant: "<jwt>"}

5. Frontend stores the access_grant in memory (not localStorage, it expires in 1h).
6. Frontend fetches summary:
   → GET /api/discharge-access/{raw_token}/summary
   → Authorization: Bearer <jwt>
   → JWT decoded (aud="discharge" enforced)
   → Discharge summary, timeline, patient info, active meds returned.
7. Patient sees their discharge summary, timeline, and medication list.
8. Every access logged to audit_logs (action_type: "discharge_summary_viewed").
```

---

### Cross-Module Dependencies

| This module needs | From module |
|---|---|
| Relay `send_message` | Surgery (`trigger_surgery_prep`), Sage Ambient (alert cards) |
| Sage `_assemble_patient_context` | Surgery service (`get_surgery_detail`) |
| Helix `create_order`, `pre_flight_*` | Relay agentic confirm endpoint |
| `llm_queue` | Surgery risk assessment, Discharge summary generation |
| `audit_service.log_audit_event` | Discharge service and discharge access service |
| `scribe_service.save_scribe_note` | Surgery routes (post-operative note on complete) |
| `sage_handoff_service.create_and_dispatch_handoff` | Relay action transition (on confirm) |
| `rxnorm_reconciler.reconcile_medication` | Relay pre-flight and agentic confirm |
