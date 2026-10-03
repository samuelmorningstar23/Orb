## 11. Conversational Bots & Authentication Routes

# 11. Conversational Bots & Authentication Routes

This section covers three files:

| File | What it is |
|---|---|
| `server/services/bot_service.py` | Demo simulation engine, generates fake vitals, lab results, and imaging reports continuously in the background |
| `server/services/chat_bot_service.py` | Demo conversation engine, simulates realistic doctor/nurse/specialist chat threads in patient case rooms |
| `server/routes/auth_routes.py` | Real production HTTP routes, handles login, rate limiting, and JWT issuance for all users |

**A note on "bots" in Orb:** In this codebase a "bot" means an automated agent that generates data or messages on a timer, not an AI chatbot. The two bot services exist purely to make the demo look alive. They write rows to SQLite and push Socket.IO events so a visitor sees realistic activity without any real hospital connected. In a real deployment with live data feeds (`INGESTION_MODE=ingestion`), both bot services are disabled entirely (see `main.py` loop below).

---

### 11.1 `server/services/bot_service.py`

**Role:** The main simulation engine. Every 5 seconds `main.py` calls three functions from this file: `run_vitals_bot`, `run_reports_bot`, and `run_lab_bot`. Together they make the dashboard look like a live ward, patients' vitals drift, labs arrive, and imaging reports appear.

**Libraries:**
- `asyncio`, the bot functions are `async` so they can `await sio.emit(...)` (Socket.IO push) without blocking the web server
- `random`, Gaussian noise, crisis spikes, stochastic throttling (the 0.2% chance gates)
- `datetime / timezone`, UTC timestamps for every inserted row
- `logging`, structured log lines prefixed `[Vigil Alert]`, `[Reports Bot]`, `[Lab Bot]`
- `database.db_session`, context-manager that opens a SQLite connection, commits, and closes cleanly (see Section 02)
- `services.vigil_service.compute_news2`, the real NEWS2 clinical scoring function; called on every synthetic vital so the risk display is always correct even for simulated data
- `services.vigil_service.generate_ai_recommendation`, async; attaches an MedGemma AI recommendation to critical-level alert messages

### Module-level constants and state

```
MAX_VITALS_PER_PATIENT = 2000
PRUNE_EVERY_N_CYCLES   = 60   # prune once every ~5 min
_cycle_counter         = 0    # increments each run_vitals_bot call
_alert_cooldowns       = {}   # patient_id → (timestamp, level)
ALERT_COOLDOWN_SECONDS = 120
_severity_cache        = {}   # patient_id → "critical"|"elevated"|"stable"
```

These are **process-local** module globals. They do not survive a server restart, and they are not shared across multiple workers. This is fine for the current single-process demo deployment but would break under `uvicorn --workers 2`. The code has inline comments acknowledging this.

**`SEVERITY_PROFILES`** is a dict of three profiles (`"critical"`, `"elevated"`, `"stable"`). Each profile defines:
- `targets`, ideal steady-state values the patient drifts toward (ICU patients drift toward abnormal targets)
- `reversion`, how strongly vitals are pulled toward the target each tick (weak for critical = stays sick, strong for stable = self-correcting)
- `noise`, standard deviation of random Gaussian jitter per vital sign
- `clamp`, hard minimum/maximum per vital (prevents impossible values like SpO2 of 200%)
- `spike_chance`, probability per cycle of a sudden crisis event (critical: 15%, elevated: 8%, stable: 0%)

**`CRISIS_SPIKES`** is a list of five delta-dicts. When a spike fires, one is chosen randomly and its deltas are added on top of the normal random walk. They model: acute decompensation, respiratory distress, septic shock, hypertensive crisis, and bradycardic event.

---

### `_get_patient_severity(db, pid) → str`

**Purpose:** Returns the severity label (`"critical"`, `"elevated"`, or `"stable"`) for a patient. Used to pick the right simulation profile.

**Inputs:** An open DB connection; a patient ID string.

**Logic:**
1. Check `_severity_cache`, if already looked up, return immediately (avoids per-cycle DB queries).
2. Query the `patients` table joined to `risk_scores` to get the ward and the most recent risk level.
3. A patient is `"critical"` if their ward is `ICU` or their risk level is already `"critical"`. They are `"elevated"` if risk level is `"elevated"`. Everything else is `"stable"`.
4. Cache and return the result.

**Output:** A string, one of `"critical"`, `"elevated"`, `"stable"`.

**Demo vs production:** This is simulation-only. In production you would not pre-classify patients for simulation purposes; severity comes from real clinical data.

---

### `run_vitals_bot(sio)`: async

**Purpose:** The heartbeat of the simulation. Generates and persists new vital signs for every admitted patient, then pushes the updates via WebSocket. Called every 5 seconds by `main.py`.

**Logic walkthrough:**

1. **Forecast refresh (every ~10 min):** Every 120 calls, calls `forecast_service.refresh_all_predictions()` to update LSTM forecast rows. This runs inside a try/except so a broken model file never crashes the vitals loop.

2. **Batch fetch all patients:** One SQL query fetches all admitted patient IDs. Another query fetches the most recent vitals row for every patient simultaneously (a `INNER JOIN` on `MAX(timestamp)`), building a `latest_map` dict keyed by patient ID.

3. **Generate new vitals per patient:** For each patient:
   - Looks up their severity profile.
   - Applies **mean-reverting random walk**: each vital shifts by `random.gauss(0, noise)` plus `reversion * (target - current)`. The Gaussian provides moment-to-moment jitter; the reversion term slowly pulls the value toward the profile's target range.
   - Optionally fires a **crisis spike** (random choice from `CRISIS_SPIKES`, added on top).
   - A per-patient **trajectory modifier** (`hash(pid) % 3`) adds a small consistent improving or stable drift for variety across patients.
   - All six vitals are **clamped** to the profile's allowed range.
   - Carries forward `consciousness` (ACVPU score) and `on_oxygen` from the previous row, the simulator does not generate these per tick; they only change via the real ingestion path.
   - Calls `compute_news2(...)` to score the new vitals. The resulting `risk` dict (`{level, score, drivers, raw_news2}`) is bundled into the emit payload.

4. **Batch INSERT:** All new vitals rows are written in a single `executemany` call inside one DB transaction (fast; avoids per-patient round-trips).

5. **Periodic pruning (every ~5 min):** Deletes simulated vitals rows beyond the most recent 2000 per patient (`WHERE source = 'simulated'`). Also prunes `risk_scores` to the most recent 100 per patient. **Critical**: only rows with `source = 'simulated'` are pruned. Real clinical rows (`source = 'ingested'` or equivalent) must never be deleted, they are the longitudinal record that Vigil, Forecast, and medico-legal audit depend on.

6. **Emit:** For each patient in the emit queue, calls `sio.emit("vitals:update", payload)`. The frontend Vigil dashboard and patient cards listen on this Socket.IO event.

7. **Alert bridge:** Calls `evaluate_and_dispatch_alerts(sio, emit_queue)` to check if any updated patient warrants a Relay alert.

**⚠️ If you change this:**
- Adding new vital columns here requires also updating the `INSERT` statement and the `emit_queue` dict. The frontend parses these fields by name.
- Changing `PRUNE_EVERY_N_CYCLES` or `MAX_VITALS_PER_PATIENT` affects DB size growth rate. Reducing `MAX_VITALS_PER_PATIENT` too aggressively will erase trend history that the Forecast LSTM needs.
- The pruning `DELETE` is a raw SQL string with an f-string interpolation for `MAX_VITALS_PER_PATIENT`, safe (it's an int constant, not user input), but be aware if you ever change how this value is set.
- This function holds a `db_session()` context for the duration of all inserts. Long-running logic inside that block delays the commit and ties up the SQLite file lock.

---

### `evaluate_and_dispatch_alerts(sio, emit_queue) → None`: async

**Purpose:** Inspects every vitals update in the emit queue, decides whether to post an automated alert message into the patient's Relay case room channel, and emits the message via WebSocket. This is the sole bridge from Vigil scoring → Relay messaging, called by the bot loop and can also be called by a deterministic demo trigger.

**Inputs:** Socket.IO server object; the list of vitals dicts that were just computed by `run_vitals_bot`.

**Logic walkthrough:**

1. **Skip stable patients:** Any patient whose computed NEWS2 level is `"stable"` is skipped entirely.

2. **Cooldown logic with escalation override:**
   - Each patient has an entry in `_alert_cooldowns`: `(last_alert_timestamp, last_level)`.
   - By default, a patient in cooldown (alerted in the last 2 minutes) is skipped.
   - Two bypass rules: (a) if the patient's level has _escalated_ (e.g., elevated → critical), the cooldown is bypassed; (b) patients already at `"critical"` are always exempt from cooldown because further deterioration could be life-threatening.

3. **Build the reason line:** `vigil_service.news2_reason_line(risk)`, shared with the nurse and feed lanes since 2026-10-02: the NEWS2 parameters that scored, worst first (points, then Severe before Moderate, then RCP chart order), three named and the rest counted as "(+N more)". It is **wording only** and never gates dispatch; NEWS2 is the sole dispatch gate. If the helper raises, the alert goes out with the band alone ("NEWS2 critical").

4. **Format the alert message:** Picks one of three template strings at random (avoids every alert looking identical). Reports NEWS2 as `"NEWS2 {raw_score} ({level})"`, not a percentage (raw NEWS2 is an ordinal, not a probability).

5. **MedGemma AI recommendation:** If the level is `"critical"`, awaits `generate_ai_recommendation(...)` and appends the result to the message. This is an async call and may involve an on-device LLM; see `vigil_service.py` for details.

6. **Persist and emit:** Opens a DB session, ensures the `bot_vigil` system user exists, finds the patient's `case_room` channel, inserts the alert as a `messages` row with `message_type='alert'`, then emits `"message:new"` via Socket.IO so the Relay UI updates instantly.

7. **Event bus publication:** After each alert insert, publishes a `StateEvent` to the `LocalEventBus` with topic `"vigil.alert.critical"`. This is the hook for the reactive nervous system (preloading Sage context, etc.). Publish failures are logged as warnings and swallowed, the alert is already in the DB.

**⚠️ If you change this:**
- The cooldown dict `_alert_cooldowns` is process-local. Restarting the server resets all cooldowns.
- `_LEVEL_RANK` defines the ordinal order. If you add a new risk level (e.g., `"watch"`), add it here and verify the escalation logic still holds.
- The alert is always posted to the `case_room` channel. If a patient has no case room, they are silently skipped. Ensure seed data always creates case rooms for admitted patients.
- Changing the `bot_vigil` user's `role` field changes how it appears in the Relay UI's sender role display.

---

### `get_dynamic_finding(db, pid, scan_type) → (str, str)`

**Purpose:** Returns a randomised clinical text interpretation for a diagnostic image, paired with an urgency label. Pure simulation, does not use a real ML model.

**Inputs:** Open DB connection; patient ID; scan type string (`"ECG"`, `"XRAY"`, `"MRI"`, `"CT"`).

**Logic:** Fetches the patient's most recent heart rate (used to personalise ECG text). Looks up a hard-coded dict of template strings per scan type. Picks one at random. Returns a `(analysis_text, urgency)` tuple where `urgency` is `"info"` or `"alert"`.

**Output:** Two strings, the analysis text to display, and the urgency level (used as the `message_type` in Relay).

**Demo vs production:** Entirely fake. In production, the Lens service calls a real vision model. This function is only invoked by `run_reports_bot`.

---

### `run_reports_bot()`: async

**Purpose:** Occasionally generates a fake diagnostic imaging report for a random admitted patient and posts it to their Relay case room. Simulates the Orb Lens AI feature in demo mode.

**Logic:**
1. Hard 99.8% skip gate (`random.random() > 0.002`), fires roughly once every 500 calls, or about once every ~40 minutes at a 5-second tick interval.
2. Checks that the `uploads/` directory exists and that at least one of the four mock image files (`mock_xray.png`, `mock_mri.png`, `mock_ct.png`, `mock_ecg.png`) is present on disk. If not, returns silently.
3. Picks a random admitted patient and a random available scan file.
4. Calls `get_dynamic_finding(...)` to generate analysis text.
5. Ensures the `bot_lens` system user exists. Inserts a row into `lens_analyses`.
6. Finds the patient's case room channel. Calls `relay_service.send_message(...)` to insert a `messages` row, then emits `"message:new"` via Socket.IO.

**⚠️ If you change this:**
- The bot silently does nothing if the mock image files are missing. If reports never appear in a demo, check that `UPLOAD_DIR` is set correctly and the mock files are present.
- `bot_lens` is inserted as role `"doctor"`, this controls the avatar styling in the Relay UI.

---

### Lab bot state constants

```python
_lab_cooldown: dict[str, float] = {}   # patient_id → last lab timestamp
LAB_COOLDOWN_SECONDS = 180             # 3 min between lab deliveries per patient
```

**`LAB_PANELS`** is a nested dict with three keys (`"critical"`, `"elevated"`, `"stable"`). Under each key is a list of panel configs, each with a `"panel"` name string and a `"generate"` lambda that returns a dict of `{analyte: (value, unit, flag, reference_range)}`. Critical patients get STAT CBC, BMP, ABG, and coagulation panels with genuinely abnormal values (e.g., potassium 6.8 mEq/L). Stable patients only get a routine CBC with normal ranges.

---

### `run_lab_bot()`: async

**Purpose:** Simulates automated lab result delivery. Occasionally picks an admitted patient and posts a formatted lab report to their case room.

**Logic:**
1. Hard 99.8% skip gate, same as reports bot.
2. Fetches all admitted patients with their most recent risk score level.
3. Filters out patients who received a lab result in the last 3 minutes.
4. **Weighted selection:** Critical patients are 3x more likely to be selected, elevated patients 2x, mimics real-world STAT lab priority.
5. **Diagnosis-specific panels:** Tries to import `DIAGNOSIS_LAB_SCHEDULE` from `seed_labs.py`. If the patient's diagnosis contains a recognised keyword (e.g., `"sepsis"`, `"dka"`, `"pneumonia"`), picks a panel from that schedule rather than the generic severity fallback. Falls back to `LAB_PANELS` if no match or import fails.
6. Calls the panel's `generate()` lambda to produce concrete numeric values.
7. Formats a multi-line text result. Checks each analyte's flag for the string `"CRITICAL"`, if any flag is critical, appends a warning footer and sets `has_critical = True`.
8. Inserts the result into both `clinical_documents` (for the document timeline) and `lens_analyses` (so the patient timeline widget also shows it).
9. Ensures the `bot_lab` system user exists.
10. Finds the patient's case room. Posts via `relay_service.send_message(...)` with `message_type='alert'` for critical results or `'system'` otherwise. Emits `"message:new"`.
11. Updates `_lab_cooldown[pid]`.

**⚠️ If you change this:**
- Adding a new analyte to a `LAB_PANELS` panel requires adding a `(value, unit, flag, reference_range)` tuple. The formatter iterates the dict in insertion order.
- The `"CRITICAL"` string in a flag triggers the footer and the `message_type='alert'` path. Any other flag text (e.g., `"[H]"`) does not.
- Lab results are dual-written to `clinical_documents` and `lens_analyses`. If the schema of either table changes, update both inserts.
- `_lab_cooldown` is process-local. Restarting the server resets all lab delivery timers.

---

### 11.2 `server/services/chat_bot_service.py`

**Role:** Generates realistic multi-turn clinical conversations and inserts them into patient Relay case rooms. Every message looks like it comes from a named doctor, nurse, or specialist. This is entirely a demo layer, it makes the Relay feature feel populated.

**Libraries:**
- `random`, scenario selection, weighted choices, all the "pick one of several phrasings" logic
- `time`, channel cooldown timer
- `datetime / timezone`, timestamps in the handoff scenario
- `database.db_session`, DB access

This service does **not** call any LLM. All text is built from Python string templates with patient context inserted. The conversations look clinically plausible because the templates branch on actual vital values (e.g., if `spo2 < 94`, the nurse increases oxygen; if `temp > 38.0`, blood cultures are ordered).

### Clinician identity constants

Four module-level lists define the cast of characters:

- **`DOCTORS`**, 3 doctors (Internal Medicine, Cardiology, Pulmonology), each a dict with `id`, `name`, `role`, `specialty`
- **`NURSES`**, 2 nurses (ICU, Ward 3)
- **`SPECIALISTS`**, 5 specialists (Nephrology, Neurology, Infectious Disease, Cardiology, Pulmonology)
- **`PHARMACISTS`**, 2 pharmacists (stored with `role: "nurse"` for display purposes)

These identities are upserted as real `users` rows (`INSERT OR IGNORE`) before any messages are inserted. This means their names appear correctly in the Relay sender UI.

### Module-level state

```python
_last_thread_time: dict[str, float] = {}   # channel_id → last thread timestamp
THREAD_COOLDOWN_SECONDS = 40               # minimum gap between threads per channel
```

Prevents the same case room from getting flooded with back-to-back threads.

---

### `_build_context(patient, vital) → dict`

**Purpose:** Assembles a flat context dict used by all scenario generators. Extracts patient name, diagnosis, and all six vital values. Falls back to safe normal values if no vitals row exists.

**Inputs:** A patient DB row; a vitals DB row (may be `None`).

**Output:** A dict with keys `name`, `diag`, `hr`, `sbp`, `dbp`, `spo2`, `temp`, `rr`.

---

### Scenario generator functions

Each scenario function takes a `ctx` dict, a `doctor` identity dict, and a `nurse` identity dict (or specialist/pharmacist as appropriate). Each returns a list of `(sender_dict, content_string)` tuples forming one conversation thread. The tuples are later inserted as individual `messages` rows.

#### `_scenario_morning_rounds(ctx, doctor, nurse)`

Simulates a morning handover from nurse to doctor. The nurse reports overnight vitals. The doctor's reply branches on what's most abnormal: fever → blood cultures; low SpO2 → ABG and chest X-ray; tachycardia → hydration and ECG; otherwise → routine review. Typically 4 messages.

#### `_scenario_lab_review(ctx, doctor, nurse)`

Nurse announces labs are available. Generated lab values are influenced by patient state (e.g., higher WBC if febrile, higher lactate if tachycardic). Doctor identifies and names specific flags (leukocytosis, AKI, hyperkalaemia, lactic acidosis, CRP elevation). Typically 4–5 messages.

#### `_scenario_medication_adjustment(ctx, doctor, nurse)`

Branches deeply on diagnosis keyword:
- `"diabet"` / `"dka"`, insulin infusion titration and anion gap discussion
- `"sepsis"` / `"infection"` / `"pneumonia"`, antibiotic de-escalation based on culture sensitivity
- `"mi"` / `"stemi"` / `"angina"`, DAPT and heparin management
- `"stroke"` / `"ischemic"`, tPA monitoring, permissive hypertension, neuro checks
- `"copd"` / `"asthma"`, bronchodilator and steroid protocol
- default, generic pain assessment and analgesic optimisation

Typically 4 messages.

#### `_scenario_deterioration_response(ctx, doctor, nurse)`

Uses structured SBAR communication (Situation, Background, Assessment, Recommendation). Nurse delivers the SBAR; doctor immediately orders fluid bolus, oxygen escalation, STAT labs, and ICU transfer prep; nurse confirms line access and asks about Rapid Response Team activation; doctor activates RRT and adds further orders. Includes MAP calculation inline. Typically 5 messages.

#### `_scenario_shift_handoff(ctx, doctor, nurse)`

Uses ISBAR handoff framework. Picks a second nurse from `NURSES` to receive the handoff. Reports I/O balance (randomly generated), pending labs, pending antibiotics, drip status, and attending name. Lists concerns for the incoming shift (SpO2 trends, fall risk, family contact). Typically 5 messages (two from the outgoing nurse, one from the incoming nurse).

#### `_scenario_procedure_prep(ctx, doctor, nurse)`

Branches on diagnosis to select a relevant procedure (cardiac catheterisation, bronchoscopy, CT abdomen, MRI brain, or generic workup). Lists preparation steps (NPO status, consent, renal function clearance, metal screening). Doctor confirms slot and writes orders. Typically 4 messages.

#### `_scenario_family_update(ctx, doctor, nurse)`

Doctor describes a family conversation about prognosis and expected length of stay. The length-of-stay estimate branches on current vitals, if the patient is doing well, it's a short stay; if vitals are poor, it's open-ended. Nurse reports visitor calls and flags an anxiety concern. Doctor optionally refers to counsellor or social work. Typically 5 messages.

#### `_scenario_discharge_planning(ctx, doctor, nurse)`

Doctor evaluates the patient against a discharge checklist (afebrile, SpO2 on room air, mobility, diet, follow-up). Items are shown as ✓ or ✗ based on current vitals. Nurse coordinates pharmacy TTO pack, social work home assessment, and follow-up booking. Doctor sets a target discharge date. Typically 5 messages.

#### `_scenario_alert_response(ctx, doctor, nurse)`

Designed to follow a Vigil auto-alert. The nurse confirms the alert at bedside. The doctor acknowledges it and orders specific interventions matched to the alert type: desaturation → O2 escalation + ABG; tachycardia → 12-lead ECG + fluid challenge; hypotension → bolus + ICU standby; fever → sepsis screen + antibiotics. Doctor always closes with a 30-minute recheck and clinical incident documentation. Typically 5–6 messages.

#### `_scenario_specialist_consult(ctx, doctor, nurse)`

Picks the most appropriate specialist based on diagnosis keyword matching (cardiology for cardiac diagnoses, nephrology for renal, neurology for stroke, etc.; falls back to random). The requesting doctor writes the formal consult. The specialist responds with specialty-specific recommendations:
- Cardiology, PCI timing, troponin series, heparin protocol
- Nephrology, nephrotoxin avoidance, fluid targets, RRT thresholds
- Neurology, NIHSS, tPA post-monitoring protocol, permissive hypertension targets
- Infectious Disease, de-escalation, antibiotic duration
- Others, generic optimisation advice

Typically 3–6 messages.

#### `_scenario_code_response(ctx, doctor, nurse)`

Most dramatic scenario. Nurse calls a Rapid Response (or Code Blue). Doctor arrives at bedside and orders adrenaline, fluid bolus, bag-valve-mask, central line prep (all conditional on actual vitals, hypotension, desaturation). Nurse confirms crash cart, executes orders, documents the response timeline, and reports whether ICU bed is available. Typically 5 messages.

#### `_scenario_nursing_assessment(ctx, doctor, nurse)`

Detailed 12-hour nursing assessment between two nurses (picks a second nurse from the list). Covers GCS, pain score, skin assessment, I/O balance with breakdown, wound/IV-site assessment, medication compliance, fall risk. Closes with a shift concern (watch SpO2, next medication time). Typically 5–6 messages.

#### `_scenario_pharmacy_consult(ctx, doctor, nurse)`

Clinical pharmacist reviews medications. Branches on diagnosis:
- Infectious, antibiotic stewardship (renal dose check, drug interactions, de-escalation timing, probiotic for C. diff prevention)
- Cardiac, DAPT compliance, statin intensity, beta-blocker titration, ACE inhibitor initiation
- Diabetic, insulin protocol review, basal-bolus transition criteria
- Generic, medication reconciliation, VTE prophylaxis check, TTO planning

Typically 4 messages.

---

### `_select_scenario(ctx, has_recent_alert) → function`

**Purpose:** Chooses which scenario to run for a given patient, using weighted random selection based on clinical state.

**Logic:**
- All scenarios start with a base weight (7–13). Higher weight = more likely.
- If the patient has deteriorating vitals (`hr > 110` or `spo2 < 92` or `sbp < 90` or `temp > 38.5`), `_scenario_deterioration_response` (weight 20) and `_scenario_code_response` (weight 10) are added; `_scenario_discharge_planning` is excluded.
- If a Vigil alert was posted in the last 5 minutes, `_scenario_alert_response` is added with a very high weight (30), making it by far the most likely choice, creating a realistic "alert → team response" sequence.
- Uses `random.choices(funcs, weights=weights, k=1)`.

**Output:** A scenario function reference.

---

### `run_chat_bot(sio)`: async

**Purpose:** The main entry point for the conversation bot. Called from `main.py`'s simulation loop (currently **commented out**, see note below).

**Logic walkthrough:**

1. **Hard gate:** `random.random() > 0.08`, fires approximately 8% of the time, or about once every 12 calls. At a 5-second tick that is roughly one conversation thread per minute.

2. **Patient selection:** Queries all admitted patients that have a case room channel. Picks one at random.

3. **Channel cooldown:** Checks `_last_thread_time[channel_id]`. If fewer than 40 seconds have passed since the last thread in this channel, skips and returns.

4. **Context building:** Fetches the latest vitals row. Checks the `messages` table for a Vigil alert in the last 5 minutes on this channel.

5. **Scenario generation:** Calls `_select_scenario(ctx, has_recent_alert)` to pick a function, picks a random doctor and nurse, calls the scenario function to get a list of `(sender, content)` tuples.

6. **Upsert all senders:** Every unique sender identity in the thread is upserted to `users` with `INSERT OR IGNORE`. This ensures the messages have valid `sender_id` foreign keys.

7. **Batch insert messages:** Iterates the thread, inserts each message with `message_type='text'`, immediately re-reads the inserted row to get the auto-generated timestamp and ID.

8. **Emit:** Iterates the saved rows and emits each as `"message:new"` on the patient's case room Socket.IO room. All messages are emitted as fast as Python can iterate, they all have the same timestamp in the DB, but the frontend renders them in order.

9. **Console log:** Prints the scenario name and message count for debugging.

> **Note:** In `main.py`, the call to `run_chat_bot(sio)` is currently commented out (`# await run_chat_bot(sio)`). The vitals and lab bots still run. Uncomment that line in `main.py` to re-enable conversation threads.

**⚠️ If you change this:**
- Each call potentially inserts 4–6 rows into `messages` and emits 4–6 Socket.IO events. Running this bot at high frequency (or lowering `THREAD_COOLDOWN_SECONDS`) will rapidly grow the `messages` table and generate a lot of network traffic.
- The `_last_thread_time` dict is process-local. It resets on restart, meaning multiple threads can fire in the same channel briefly after a redeploy.
- All scenario functions expect `doctor` and `nurse` to be dicts with keys `id`, `name`, `role`. Adding a new sender type (e.g., a radiologist) requires ensuring its identity dict is included in the `all_senders` upsert pass.
- Changing the `THREAD_COOLDOWN_SECONDS` constant affects all channels globally.
- The `has_recent_alert` check looks back only 5 minutes. If the vitals bot is slower than usual, the alert response scenario may not fire in time to feel reactive.

---

### 11.3 `server/routes/auth_routes.py`

**Role:** Real production HTTP routes. Handles user login (credential verification, JWT issuance) and rate limiting. Every logged-in screen in the Orb frontend, Vigil, Relay, Lens, Sage, Pulse, depends on the token issued here.

**Libraries:**
- `fastapi.APIRouter`, groups routes under `/api/auth`
- `fastapi.HTTPException`, returns structured 4xx errors to the client
- `fastapi.Request`, access to `request.client.host` (caller IP) and headers
- `collections.defaultdict`, backing store for the rate-limit IP table
- `uuid`, generates a `correlation_id` for every request (links login to audit trail)
- `time`, timestamps for the rate-limiter sliding window
- `models.user.UserLogin / UserOut / TokenResponse`, Pydantic request/response schemas
- `auth.hash_password / verify_password / create_token`, password and JWT utilities (see `auth.py` above)
- `database.get_db`, raw DB connection (used here instead of `db_session` context manager)
- `services.audit_service.log_audit_event`, writes every login attempt (success or failure) to the audit log

**Prefix:** All routes in this file are served under `/api/auth`.

---

### Rate limiter state

```python
LOGIN_ATTEMPTS = defaultdict(list)   # IP → [unix timestamps of attempts]
MAX_LOGIN_ATTEMPTS      = 5
LOCKOUT_DURATION_SECONDS = 900       # 15 minutes
_MAX_TRACKED_IPS        = 10000
```

This is a **sliding-window rate limiter** stored entirely in Python memory. Each IP gets a list of timestamps. An IP is locked out when its list has 5 or more timestamps within the most recent 15-minute window.

**Known limitations** (documented in-file):
- Process-local: lost on restart
- Keys on raw TCP peer IP, broken behind a load balancer or reverse proxy where all traffic shares one IP
- Only guards per-IP, not per-account (credential stuffing across many IPs is unthrottled)
- In `dev` mode, localhost addresses are exempt from rate limiting

These limitations are intentional deferred work, acceptable for a single-process, on-premises demo deployment.

---

### `_prune_login_attempts(now) → None`

**Purpose:** Garbage-collects the rate-limiter dict to prevent unbounded memory growth.

**Logic:**
1. Identifies IP buckets where all recorded attempts are older than `LOCKOUT_DURATION_SECONDS`, removes them.
2. If the dict still exceeds `_MAX_TRACKED_IPS` entries (active spray attack with unique IPs), evicts the IP buckets with the oldest most-recent-attempt timestamp.

Called automatically at the start of every `is_ip_rate_limited` check.

---

### `is_ip_rate_limited(ip) → bool`

**Purpose:** Returns `True` if the given IP should be blocked from attempting another login.

**Logic:**
1. Returns `False` immediately if `ip` is empty.
2. In `dev` deployment profile, always returns `False` for localhost, developers can test without waiting out lockouts.
3. Calls `_prune_login_attempts` to clean stale state.
4. Filters the IP's attempt list to only timestamps within the lockout window.
5. Returns `True` if 5 or more attempts remain in that window.

---

### `record_login_attempt(ip) → None`

**Purpose:** Adds the current timestamp to the IP's attempt list. Called only on failed logins.

---

### `POST /api/auth/login`: the JWT login flow

This is the most important endpoint in the file. Here is the full flow from a client request to an authenticated token:

**Step 1, Client sends credentials**

The frontend posts JSON: `{ "employee_id": "DOC001", "password": "secret" }`. This matches the `UserLogin` Pydantic model.

**Step 2, Rate limit check**

`is_ip_rate_limited(client_host)` is called. If blocked, the audit service logs a `login_rate_limited` failure event and the endpoint raises HTTP 429. The client sees `"Too many login attempts. Please try again in 15 minutes."`.

**Step 3, DB lookup**

Opens a DB connection with `get_db()` and queries `SELECT * FROM users WHERE id = ?` using `credentials.employee_id`. This works for both clinicians (whose `id` is an employee ID like `DOC001`) and patients (whose `id` is their patient ID). Closes the connection immediately after.

**Step 4, Credential verification**

If no user row is found, or if `verify_password(credentials.password, user["password_hash"])` returns `False`:
- `record_login_attempt(client_host)` increments the IP's failure count.
- Audit log records a `login_failure` event with the attempted ID.
- HTTP 401 is raised: `"Invalid credentials"`.

`verify_password` (defined in `auth.py`) supports two hash formats:
- **bcrypt** (`$2b$...` prefix), the current standard, uses `bcrypt.checkpw`
- **Legacy SHA-256**, for accounts that predate the bcrypt migration; uses `hmac.compare_digest` (constant-time comparison to prevent timing attacks)

**Step 5, Clear failed attempts on success**

If login succeeds, the IP's failure list is deleted from `LOGIN_ATTEMPTS`. This means a correct login from a rate-limited IP resets the counter.

**Step 6, Legacy hash auto-upgrade**

Checks if the stored hash does not start with `$2` (i.e., it's a legacy SHA-256 hash). If so, immediately re-hashes the plaintext password with bcrypt and updates the `users` row. This is a transparent migration, users never notice, and their account is silently upgraded to bcrypt on first successful login.

**Step 7, Token issuance**

Calls `create_token(user["id"], user["role"])` (defined in `auth.py`). This creates a JWT with:
- `sub`, the user ID
- `role`, the user's role at time of login (embedded for convenience; **not trusted for authorisation**, `get_current_user` re-reads the live DB row)
- `jti`, a unique UUID identifying this specific token (hook for future revocation)
- `exp`, expiry time based on `JWT_EXPIRY_HOURS` from config
- `iat`, issued-at timestamp

The token is signed with `JWT_SECRET` and `JWT_ALGORITHM` (from `config.py`).

**Step 8, Audit log success**

Logs a `login_success` event including actor ID, role, IP, and user agent.

**Step 9, Response**

Returns a `TokenResponse` JSON:
```json
{
  "token": "<jwt>",
  "user": {
    "id": "DOC001",
    "name": "Dr. Meera Sharma",
    "role": "doctor",
    "department": "Internal Medicine",
    "registration_number": "MCI-12345"
  }
}
```

The frontend (see `frontend/src/api/client.ts`) stores the token and attaches it as a `Bearer` header on every subsequent request: `Authorization: Bearer <token>`.

**Step 10, All subsequent authenticated requests**

Any route protected with the `get_current_user` dependency in `auth.py`:
1. Extracts the `Bearer` token from the `Authorization` header via `HTTPBearer()`.
2. Calls `decode_token(token)`, verifies the JWT signature and expiry.
3. Reads `sub` (user ID) from the payload.
4. Queries `SELECT * FROM users WHERE id = ?` to get the live user row.
5. Returns the user dict to the route handler.

The role used for permission checks comes from this live DB row, not from the JWT payload. This means changing a user's role in the DB takes effect immediately without requiring them to re-login.

---

### `GET /api/auth/me`

**Purpose:** Returns the currently authenticated user's profile. Requires authentication.

**Status:** The handler body is a `pass` stub, the actual implementation is wired by `main.py` which injects `get_current_user` as a dependency on this route. The response model is `UserOut`. This pattern means the handler function itself does no DB work; the dependency injects the user dict.

---

### `auth.py` supporting functions (referenced by auth_routes but defined in `server/auth.py`)

These are not in `auth_routes.py` but are called from it and are critical to understand:

- **`hash_password(password)`**, bcrypt with auto-generated salt. Returns a `$2b$...` string. Used during user creation and legacy hash upgrade.
- **`verify_password(password, hashed)`**, dual-path: bcrypt via `bcrypt.checkpw` for modern hashes; `hmac.compare_digest` of SHA-256 for legacy. Always constant-time.
- **`create_token(user_id, role)`**, builds and signs the JWT payload. Note: `role` in the JWT is embedded for logging convenience only; authorisation always re-reads from the DB.
- **`decode_token(token)`**, verifies signature and expiry; raises HTTP 401 on failure.
- **`get_current_user(credentials)`**, FastAPI dependency; the universal "who is calling this?" function used by every protected route.
- **`require_role(*roles)`**, deprecated role-based access dependency. Kept for backward compatibility. Do not use for new routes; use `permissions.require_permission` instead.

**⚠️ If you change auth_routes.py:**
- Adding a new field to `UserOut` changes the `/login` and `/me` response shape. The frontend `api/client.ts` parses this response and stores the `user` object in app state. Removing or renaming a field will break the frontend without a matching frontend change.
- Changing `MAX_LOGIN_ATTEMPTS` or `LOCKOUT_DURATION_SECONDS` affects every user on the system simultaneously.
- The `LOGIN_ATTEMPTS` dict is not persisted. An attacker who triggers a lockout can bypass it by waiting for the server to restart. This is acceptable for the current on-premises, single-location deploy but would not be acceptable for a public-facing system.
- The `GET /api/auth/me` stub must remain a `pass`, the actual user lookup is injected by the FastAPI dependency system in `main.py`. If you add logic to the function body, it will not receive the authenticated user dict without adding a `user: dict = Depends(get_current_user)` parameter.
- JWT tokens currently have no server-side revocation. Logging out on the frontend just deletes the stored token. If a token is stolen, it remains valid until it expires. The `jti` field in the JWT payload is the planned hook for a future revocation table, see the deferred comment in `auth.py`.

---

### How the bot loop and auth fit together in `main.py`

The simulation loop in `main.py` is structured roughly as:

```python
async def simulation_loop(sio):
    bots_enabled = INGESTION_MODE in ("simulation", "hybrid")
    if not bots_enabled:
        return
    while True:
        await run_vitals_bot(sio)    # bot_service.py
        await run_reports_bot()       # bot_service.py
        await run_lab_bot()           # bot_service.py
        # await run_chat_bot(sio)     # chat_bot_service.py: currently disabled
        await asyncio.sleep(VITALS_INTERVAL_SECONDS)  # default: 5 seconds
```

The auth routes are registered separately as a FastAPI router and have no connection to the simulation loop. They run in the same process as normal HTTP route handlers whenever a login request arrives.

Setting `INGESTION_MODE=ingestion` or `INGESTION_MODE=hybrid` in the environment will disable or modify the simulation bots without touching auth at all.
