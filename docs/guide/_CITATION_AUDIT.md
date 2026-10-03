# Wave 1: Inventory and Citation Audit

**Generated:** 2026-08-09, from the live code in `/Users/samuelmorningstar/Documents/Projects/Orb`.
**Audience:** the three agents who will extend and correct `docs/guide/`.
**This file is not a chapter.** It is the work list. Do not publish it. Do not link to it from a chapter.

> **STATUS, 2026-08-09, after the verification sweep.** The work list below has been worked.
> Every `path:line` in `docs/guide/**` and `docs/MANUAL_PLAIN_ENGLISH.md` was re-extracted and
> re-checked mechanically. Result: **20,722 citations**, of which **0** point past the end of the
> file they name and **0** name a symbol that does not exist. The load-bearing subset, the
> 1,338 places where a heading or a table row asserts *"symbol X is defined at line N"*, was
> checked exactly: **126 were stale and are now repaired**, and every remaining flag was opened
> by hand and is a prose heading ("Types", "State", "Animations and motion") or a
> multiple-anchor heading the checker mis-pairs, not a wrong line. All **441** file-map line
> counts are now exact against `wc -l`; **79** of them were wrong by more than 15% before the
> sweep (`sage_routes.py` claimed 454 lines against an actual 2,811).
>
> **The counts *inside this file*, including the "502 confirmed-wrong citations" and the
> "42 routers" (there are 43), are the pre-sweep snapshot and are no longer current.** Re-run
> the check rather than trusting a number here.

---

## 0. Read this before you touch a chapter

Four rules, in order of importance.

**Rule 1, every `file:line` you write must be opened and read first.**
The single fastest way to destroy this manual's value is a citation that points at nothing.
The audit below found **502 confirmed-wrong citations** in the 31 existing chapters. That is
45% of every symbol anchor in the guide. An investor who checks three citations and finds one
wrong stops trusting all 4,438.

**Rule 2, line numbers in *this file* are also perishable.**
Every line number here was read from disk between 23:58 on 2026-08-08 and 00:15 on 2026-08-09.
While this audit ran, several files under `src/` changed on disk (`src/api/rosterImport.ts`,
`src/pages/panels/admin/RosterImport.tsx`, `src/pages/panels/AdminPanel.tsx` all have mtimes
inside the audit window). The repo is **not a git repo**, so there is no commit to pin against.
Re-verify before you write. The script in §6.3 re-runs the whole audit in about 40 seconds.

**Rule 3, the founder's acceptance test is the frontend link.**
> "In the technical manual, I want you to reference what each code does to the front end. So I
> know exactly which part of what I'm seeing is related to what code."

Concretely, every chapter section that documents a backend function must answer: *what does a
user see when this runs, and where is the component that shows it?* Every chapter section that
documents a component must answer: *what endpoint feeds it, and what service produces that
payload?* §2.5 gives the exact chain format to use.

**Rule 4, plain English.** Short sentences. Say what the thing does and what breaks if you
change it. Assume a competent engineer who has never seen this codebase.

---

## 1. What Orb actually is, enumerated from running code

Method: imported the FastAPI app (`ORB_DEPLOYMENT_PROFILE=dev python -c "import main"`) and
walked `app.routes`, reading each endpoint's source file and line via `inspect`, and each
guard's permission strings out of the `require_permission` closure. Services and schema were
parsed with `ast` / regex over the source, not guessed from filenames.

### 1.1 Headline numbers

| Thing | Count |
|---|---|
| Registered routes on the app object | 273 |
|, real API endpoints (`APIRoute`) | 268 |
|, FastAPI's own `/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect` | 4 |
|, SPA static mount (`_SpaStaticFiles`, serves `dist/`) | 1 |
| Distinct URL paths | 251 |
| Method + path pairs | 269 |
| Router modules under `server/routes/` | 42 files, 42 routers, all mounted in `server/main.py:681-724` |
| Service modules under `server/services/` | 92 (excluding `__init__.py`), 1,157 top-level functions |
| Tables created in `server/database.py` | 48 |
| Tables created in `server/migrations.py` | 24 |
| Numbered migrations | 17 (`MIGRATIONS` list, `server/migrations.py:1609`) |
| Frontend route entries (`react-router`) | 7 (`src/App.tsx:186-195`) |
| Clinician shell tabs | 17 (`TABS`, `src/pages/ClinicianShell.tsx:25`) |
| Admin sub-tabs | 14 declared (`ADMIN_TABS`, `src/pages/panels/AdminPanel.tsx:20`) |
| Socket.IO event names on the wire | 14 |
| SSE (`text/event-stream`) endpoints | 10 live + 1 commented out |
| Screenshots on disk | 463 PNG (223 in `docs/screenshots/`, 73 in `wavec/`, 167 in `wavee/`) |
| Screenshots referenced by any chapter | **2** (both in `12-lens.md`) |

### 1.2 How routes are guarded

Counted from the dependency graph of each route, not from prose.

| Guard | Routes |
|---|---|
| `require_permission(...)`, one or more permission strings | 184 |
| `require_patient_access(...)`, patient-scoped, derives ward from the JWT | 41 |
| `get_current_user` only, logged in, no permission check | 14 |
| `require_patient_self(...)`, Bridge, patient may only see themselves | 13 |
| `_enrolment_principal`, MFA enrolment ticket | 2 |
| No FastAPI dependency at all | 14 |

The 14 with no dependency are **not all unauthenticated**. Four of them (`/api/ingest/json`,
`/api/ingest/hl7`, `/api/ingest/fhir`, `/api/ingest/replay`) take `X-API-Key` as a `Header(None)`
parameter and call `_verify_api_key` (`server/routes/ingestion_routes.py:29`) inside the handler
body, a constant-time `hmac.compare_digest` against `INGESTION_API_KEY`. Any chapter that says
"the ingest routes are unauthenticated" is wrong. The genuinely open ones are:

| Method | Path | Handler | Why it is open |
|---|---|---|---|
| POST | `/api/auth/login` | `server/routes/auth_routes.py:361` | it *is* the authentication |
| POST | `/api/auth/pin-login` | `server/routes/auth_routes.py:1228` | PIN-on-device login |
| POST | `/api/auth/device/bind` | `server/routes/auth_routes.py:1143` | binds a device with a device secret |
| GET | `/api/auth/device` | `server/routes/auth_routes.py:1199` | tells the login page whether this device is enrolled |
| POST | `/api/discharge-access/verify` | `server/routes/discharge_access_routes.py:14` | patient token exchange |
| GET | `/api/discharge-access/{token}/summary` | `server/routes/discharge_access_routes.py:29` | the token *is* the credential |
| GET | `/api/health` | `server/main.py:864` | liveness |
| GET | `/api/health/loop` | `server/main.py:869` | event-loop liveness |
| GET | `/metrics` | `server/routes/metrics_routes.py:46` | Prometheus scrape |
| GET/POST | `/socket.io/{_path:path}` | `server/main.py:1092` | a deliberate 500 that shouts "you started `main:app` instead of `python main.py`, all realtime is dead" |

Roles and their permissions live in `ROLE_PERMISSIONS` (`server/permissions.py:104-163`), with
`clinician` aliased to `doctor` at `server/permissions.py:165`. Ward scoping is derived
server-side only, in `department_scope` (`server/permissions.py`, search `def department_scope`), 
the client never names its own scope.

### 1.3 Every registered route

Full table in **§6.1**, grouped by router module, with guard and `handler, file:line`.
Kept out of this section so §1 stays readable.

**One thing to know about those line numbers.** They come from
`inspect.getsourcelines(route.endpoint)`, which for a decorated function returns the **decorator
line**, not the `def` line, so `server/routes/auth_routes.py:361` is `@router.post("/login"…)`
and `def login` is at `:362`. That is the better citation for a route (it is where the path,
method and response model are declared), so use it as given. Do not "correct" it to the `def`
line, and do not be surprised when §6.2's symbol table reports the `def` one line lower for the
same function.

### 1.4 Realtime: Socket.IO, not WebSocket routes

There are **zero** `WebSocketRoute` entries on the app. Realtime is python-socketio, mounted as
an ASGI app; `server/main.py:99` imports `sio` from `server/ws/realtime.py`. That is why starting
the server with `uvicorn main:app` silently kills all realtime, and why `main.py:1092` exists to
catch it.

**Server handlers** (`server/ws/realtime.py`, 479 lines):

| Event | Handler | Line |
|---|---|---|
| `connect` | `connect` | `server/ws/realtime.py:151` |
| `enter_patient` | `enter_patient` | `server/ws/realtime.py:310` |
| `leave_patient` | `leave_patient` | `server/ws/realtime.py:336` |
| `disconnect` | `disconnect` | `server/ws/realtime.py:351` |
| `join_channel` | `join_channel` | `server/ws/realtime.py:381` |
| `leave_channel` | `leave_channel` | `server/ws/realtime.py:400` |
| `typing` | `typing` | `server/ws/realtime.py:408` |
| `new_message` | `new_message` | `server/ws/realtime.py:428` |

**Scribe handlers** (`server/ws/scribe_stream.py`): `scribe:start_session` (:168),
`scribe:audio_chunk` (:195), `scribe:end_utterance` (:209), `scribe:stop_session` (:220),
`disconnect` (:233).

**Events emitted, and the component that listens**, this is the table the frontend-link
requirement wants:

| Event | Emitted from | Listened by |
|---|---|---|
| `vitals:update` | `services/bot_service.py:347`, `services/ingestion_service.py:833` (via `emit_patient_vitals`) | `src/pages/panels/VigilPanel.tsx:142`, `:606`; `src/services/criticalAlerts.ts:296` |
| `presence:update` | `_broadcast_presence`, `server/ws/realtime.py:302` | `src/pages/panels/VigilPanel.tsx:574` |
| `message:new` | `routes/relay_routes.py:434`, `:780`; `routes/surgery_routes.py:398`, `:400`; `routes/lens_routes.py:271`; `services/bot_service.py:654` | `src/pages/panels/RelayPanel.tsx:153`; `src/services/criticalAlerts.ts:295` |
| `message:update` | `routes/relay_routes.py:1087`, `:1220` | `src/pages/panels/RelayPanel.tsx:154` |
| `typing` | `server/ws/realtime.py:424` | `src/pages/panels/RelayPanel.tsx:181` (emitted at `:270`) |
| `relay:room_fit` | `routes/relay_routes.py:579` | `src/pages/panels/RelayPanel.tsx:155` |
| `relay:room_fit_resolved` | `routes/relay_routes.py:809` | `src/pages/panels/RelayPanel.tsx:156` |
| `sage:insight` | `server/main.py:133`; `routes/sage_proactive_routes.py:54`, `:135`, `:155` | `src/components/sage/SageInsightsFeed.tsx:70` |
| `sage:action_update` | `routes/relay_routes.py:378`, `:1737`; `routes/sage_proactive_routes.py:134`; `services/sage_ambient_service.py:1952` | `src/components/relay/SageAmbientSidebar.tsx:60` |
| `sage:memory_update` | `services/sage_ambient_service.py:1983` | `src/components/relay/SageAmbientSidebar.tsx:59` |
| `sage:ward_brief` | `services/sage_narrator_service.py:297` | `src/components/sage/WardBrief.tsx:28` |
| `patient:timeline_updated` | `routes/relay_routes.py:1738`; `services/nervous_system_triggers.py:793` | `src/components/vigil/PatientStory.tsx:112` |
| `surgery:emergency_intake` | `services/sage_handoff_service.py:552`, `:602` | `src/pages/panels/SurgicalSuitePanel.tsx:172`; `src/services/criticalAlerts.ts:294` |
| `scribe:transcript` / `scribe:status` / `scribe:error` | `server/ws/scribe_stream.py:142` / `:95:150:155:192` / `:154:172` | `src/hooks/useStreamingTranscription.ts:101-103` |

**SSE endpoints** (`media_type="text/event-stream"`):
`routes/sage_routes.py:1591` (refusal stream), `:2366` (main Sage answer), `:2546` (voice
refusal), `:2696`; `routes/vigil_routes.py:256`, `:676`, `:687` (one more at `:653` is commented
out); `routes/bridge_routes.py:130`; `routes/surgery_routes.py:680`;
`routes/scribe_routes.py:509`; `routes/lens_routes.py:175`.

### 1.5 Services

92 modules, 1,157 top-level functions. Sorted by size, with a coverage flag, 
**NONE** means the filename appears nowhere in any of the 31 chapters.

| Module | Lines | Top-level fns | Classes | Guide coverage |
|---|---:|---:|---:|---|
| `sage_service.py` | 2975 | 35 | 0 | ch 09 |
| `history_import_service.py` | 2293 | 39 | 8 | **NONE** |
| `shadow_scorecard_service.py` | 2039 | 45 | 1 | **NONE** |
| `sage_ambient_service.py` | 1988 | 55 | 0 | ch 10 |
| `drug_safety_service.py` | 1580 | 26 | 1 | ch 14 |
| `room_fit_service.py` | 1576 | 33 | 3 | **NONE** |
| `roster_import.py` | 1556 | 34 | 5 | **NONE** |
| `escalation_service.py` | 1324 | 30 | 1 | **NONE** |
| `consent_service.py` | 1314 | 27 | 0 | **NONE** |
| `clinical_knowledge.py` | 1184 | 16 | 0 | ch 09 |
| `ingestion_adapters.py` | 1129 | 6 | 9 | ch 24 |
| `ingestion_service.py` | 1071 | 13 | 1 | ch 24 |
| `order_sets_service.py` | 1066 | 20 | 4 | **NONE** |
| `shadow_service.py` | 1062 | 35 | 0 | **NONE** |
| `ops_health_service.py` | 1029 | 23 | 0 | **NONE** |
| `bot_service.py` | 1023 | 12 | 0 | ch 06 |
| `forecast_service.py` | 1019 | 14 | 0 | ch 13 |
| `scribe_service.py` | 1016 | 23 | 0 | ch 11 |
| `nurse_shift_service.py` | 997 | 16 | 0 | **NONE** |
| `shadow_export_service.py` | 977 | 28 | 0 | **NONE** |
| `sage_provenance_service.py` | 974 | 28 | 0 | **NONE** |
| `heartbeat_service.py` | 962 | 30 | 1 | **NONE** |
| `emar_service.py` | 883 | 12 | 0 | **NONE** |
| `vigil_service.py` | 867 | 9 | 0 | ch 07 |
| `relay_service.py` | 854 | 15 | 1 | ch 08 |
| `corpus_loader.py` | 845 | 26 | 0 | ch 09 |
| `nervous_system_triggers.py` | 817 | 18 | 0 | ch 10 |
| `patient_timeline_service.py` | 806 | 28 | 1 | **NONE** |
| `loop_watchdog.py` | 784 | 8 | 1 | **NONE** |
| `bootstrap_service.py` | 746 | 19 | 0 | **NONE** |
| `downtime_service.py` | 707 | 18 | 0 | ch 28 |
| `site_config_service.py` | 660 | 10 | 3 | **NONE** |
| `pulse_service.py` | 635 | 12 | 0 | ch 18 |
| `thinking_mode.py` | 612 | 11 | 1 | **NONE** |
| `sage_handoff_service.py` | 607 | 12 | 0 | ch 22 |
| `voice_agent_service.py` | 603 | 2 | 2 | ch 09 |
| `allergy_interlock.py` | 567 | 14 | 1 | **NONE** |
| `llm_queue_service.py` | 565 | 0 | 2 | ch 26 |
| `coding_service.py` | 526 | 7 | 0 | ch 20 |
| `event_bus.py` | 511 | 3 | 2 | ch 06 |
| `compliance_service.py` | 509 | 14 | 0 | ch 25 |
| `ipass_service.py` | 505 | 20 | 0 | ch 22 |
| `fhir_export_service.py` | 488 | 15 | 0 | ch 24 |
| `timeline_service.py` | 414 | 13 | 0 | **NONE** |
| `sage_narrator_service.py` | 396 | 1 | 1 | **NONE** |
| `ai_governance.py` | 395 | 7 | 1 | **NONE** |
| `command_center_service.py` | 392 | 13 | 0 | ch 18 |
| `discharge_service.py` | 392 | 6 | 0 | ch 21 |
| `helix_service.py` | 390 | 12 | 0 | ch 14 |
| `sage_relay_context.py` | 381 | 5 | 0 | **NONE** |
| `scribe_providers.py` | 379 | 1 | 4 | ch 11 |
| `surgery_service.py` | 367 | 5 | 0 | ch 17 |
| `surgery_escalation_service.py` | 361 | 8 | 0 | ch 17 |
| `audit_service.py` | 357 | 4 | 1 | ch 05 |
| `flight_recorder_service.py` | 353 | 7 | 0 | ch 05 |
| `sage_proactive_service.py` | 346 | 12 | 0 | ch 10 |
| `impact_service.py` | 329 | 5 | 0 | ch 25 |
| `world_model_service.py` | 328 | 14 | 0 | **NONE** |
| `rxnorm_corrector.py` | 318 | 3 | 1 | ch 14 |
| `capacity_sim_service.py` | 296 | 3 | 0 | ch 19 |
| `gpu_coordinator.py` | 296 | 4 | 1 | ch 26 |
| `discharge_access_service.py` | 280 | 6 | 0 | ch 21 |
| `knowledge_worker.py` | 278 | 13 | 1 | **NONE** |
| `pharmacy_verification_service.py` | 270 | 7 | 0 | ch 15 |
| `orders_service.py` | 267 | 9 | 0 | ch 16 |
| `bridge_service.py` | 245 | 4 | 0 | ch 23 |
| `interaction_db.py` | 241 | 4 | 0 | ch 14 |
| `admission_service.py` | 234 | 1 | 0 | ch 21 |
| `whisper_daemon.py` | 233 | 5 | 0 | ch 11 |
| `retrieval_eval.py` | 231 | 9 | 0 | ch 09 |
| `retrieval_calibration.py` | 222 | 7 | 0 | **NONE** |
| `metrics_service.py` | 219 | 13 | 0 | ch 30 |
| `model_router.py` | 189 | 6 | 1 | **NONE** |
| `crypto_service.py` | 188 | 10 | 0 | ch 05 |
| `terminology_service.py` | 185 | 3 | 0 | ch 16 |
| `lens_service.py` | 181 | 5 | 0 | ch 12 |
| `sepsis_bundle_service.py` | 181 | 10 | 0 | ch 07 |
| `who_checklist.py` | 178 | 4 | 0 | ch 17 |
| `helix_db.py` | 171 | 3 | 0 | ch 14 |
| `batch_coordinator.py` | 169 | 1 | 1 | **NONE** |
| `medication_safety_gate.py` | 169 | 2 | 1 | **NONE** |
| `llm_providers.py` | 164 | 1 | 2 | **NONE** |
| `sage_feedback_service.py` | 159 | 6 | 0 | **NONE** |
| `shadow_verdicts.py` | 153 | 5 | 0 | **NONE** |
| `model_governance_service.py` | 147 | 4 | 0 | ch 25 |
| `pin_security.py` | 131 | 7 | 0 | **NONE** |
| `field_crypto.py` | 118 | 8 | 0 | **NONE** |
| `audit_middleware.py` | 114 | 2 | 0 | ch 05 |
| `mfa_service.py` | 114 | 7 | 0 | ch 03 |
| `clinical_records_service.py` | 111 | 7 | 0 | ch 07 |
| `patient_story_service.py` | 96 | 1 | 0 | **NONE** |
| `rxnorm_reconciler.py` | 76 | 3 | 0 | ch 14 |

**36 of 92 service modules are never named in the guide.** Together they are roughly 25,000
lines of the product, including the entire shadow-mode evidence pipeline, the whole DPDP
compliance stack, escalation, order sets, and history import.

**10 of 42 route modules are never named:** `admin_users_routes.py`, `appointments_routes.py`,
`backup_routes.py`, `emar_routes.py`, `nurse_routes.py`, `ops_routes.py`,
`patient_timeline_routes.py`, `roster_import_routes.py`, `search_routes.py`, `shadow_routes.py`.

### 1.6 Frontend

**Router**, `src/App.tsx:186-195`, seven entries:

| Path | Renders | Gate |
|---|---|---|
| `/` | redirect | to `/bridge` if role is `patient`, else `/dashboard`, else `/login` |
| `/login/:mode` | `Login` (`src/pages/Login.tsx`) | `mode` is `patient` or `staff`; `isPatient` at `src/pages/Login.tsx:16`, PIN mode at `:71` |
| `/dashboard/*` | `ClinicianShell` (`src/pages/ClinicianShell.tsx`) | any non-patient role |
| `/bridge/*` | `PatientShell` (`src/pages/PatientShell.tsx`) | role `patient` |
| `/discharge/:token` | `DischargeAccess` (`src/pages/DischargeAccess.tsx`) | none, the token is the credential |
| `/downtime/:patientId` | `DowntimeMode` (`src/pages/DowntimeMode.tsx`) | any non-patient role |
| `/handoff/:patientId` | `HandoffPage` (`src/pages/HandoffPage.tsx`) | any non-patient role |

**Clinician shell tabs**, `TABS` at `src/pages/ClinicianShell.tsx:25`, 17 entries. Each tab id
maps to a lazily imported panel (`src/pages/ClinicianShell.tsx:275-291`) and to a
`role="tabpanel"` div (`:716-800`). Visibility is decided by `getAccessibleTabs`
(`src/pages/ClinicianShell.tsx:182-205`):

| Role | Tabs, in order (first one is the landing tab) |
|---|---|
| `doctor` (non-surgical) | vigil, round, orders, relay, sage, scribe, lens, forecast, command, capacity, coding, appointments |
| `doctor` (dept `Surgery` / `General Surgery`) | …same but `surgical-suite` replaces `appointments` |
| `surgeon` (raw role safety net) | vigil, round, relay, sage, scribe, lens, forecast, surgical-suite |
| `nurse` | **shift**, vigil, orders, relay, sage, scribe, lens, forecast, coding, appointments |
| `admin` | vigil, relay, sage, forecast, command, capacity, coding, appointments, admin |
| `pharmacist` | helix, pharmacy-verify, sage |
| anything else | vigil, relay |

Tab id → component (all `lazy()`, all wrapped in `PanelErrorBoundary`, `src/pages/ClinicianShell.tsx:236`):

| Tab id | Label the user sees | Component |
|---|---|---|
| `shift` | My Shift | `src/pages/panels/nurse/NursePanel.tsx` |
| `helix` | Pharmacy | `src/pages/panels/HelixPanel.tsx` |
| `pharmacy-verify` | Verify Queue | `src/components/helix/PharmacyVerifyQueue.tsx` |
| `vigil` | Patients | `src/pages/panels/VigilPanel.tsx` |
| `round` | Ward Round | `src/pages/panels/RoundPanel.tsx` |
| `orders` | Orders | `src/components/orders/OrdersPanel.tsx` |
| `relay` | Messages | `src/pages/panels/RelayPanel.tsx` |
| `sage` | Sage | `src/pages/panels/SagePanel.tsx` |
| `scribe` | Scribe | `src/pages/panels/ScribePanel.tsx` |
| `lens` | Lens | `src/pages/panels/LensPanel.tsx` |
| `forecast` | Forecast | `src/pages/panels/ForecastPanel.tsx` |
| `command` | Command | `src/components/command/CommandCenter.tsx` |
| `capacity` | Simulator | `src/components/capacity/CapacitySimulator.tsx` |
| `coding` | Revenue | `src/components/coding/RevenueIntegrityPanel.tsx` |
| `appointments` | Appointments | `src/pages/panels/AppointmentsPanel.tsx` |
| `surgical-suite` | Surgical Suite | `src/pages/panels/SurgicalSuitePanel.tsx` |
| `admin` | Admin | `src/pages/panels/AdminPanel.tsx` |

**Admin sub-tabs**, `ADMIN_TABS` at `src/pages/panels/AdminPanel.tsx:20`, 14 declared:
overview, scorecard, audit, trace, ingestion, security, alerts, models, impact, trust, flight,
users, roster, data.

> ⚠️ **This area is moving under you, verify before documenting.** At 00:01 on 2026-08-09,
> `AdminPanel.tsx` declared the `roster` tab (`:73`) and imported `RosterImport` (`:16`) but had
> **no** `{activeTab === 'roster' && …}` render branch, so the tab rendered nothing. By 00:13 the
> branch existed at `:122` and the screen worked. Same file, twelve minutes apart, both readings
> taken during this audit. `src/api/rosterImport.ts`, `src/pages/panels/admin/RosterImport.tsx`,
> `src/pages/panels/admin/Admin.css` and `src/api/client.ts` all changed on disk in the same
> window. **Re-read `AdminPanel.tsx:105-128` and anything under `src/` before you cite it.**
> Roster Import is chapter 46's subject; treat every line number for it as provisional.

**Modules the guide has never named** (component, hook, or api-client file that appears in zero
chapters). This is the frontend half of the coverage gap:

- `src/pages/panels/RoundPanel.tsx`
- `src/pages/panels/nurse/`, all 6 files: `NursePanel.tsx`, `ObsEntrySheet.tsx`, `MedRoundSheet.tsx`, `HandoverSheet.tsx`, `News2Badge.tsx`, `time.ts`
- `src/components/round/`, all 7 files: `RoundQueueRail.tsx`, `RoundDictation.tsx`, `RoundSummaryView.tsx`, `RoundHandoff.tsx`, `RoundStates.tsx`, `roundQueue.ts`, `roundSummary.ts`
- `src/pages/panels/admin/`, `Scorecard.tsx`, `UsersPanel.tsx`, `DataPanel.tsx`, `RosterImport.tsx`
- `src/components/`, `AiStatusChip.tsx`, `AlertPermissionButton.tsx`, `ClinicalCourseTimeline.tsx`, `CommandPalette.tsx`, `KeyboardShortcuts.tsx`, `MfaEnrollmentModal.tsx`, `OfflineBanner.tsx`, `SessionExpiredModal.tsx`, `SizedChart.tsx`
- `src/components/vigil/`, `AdministerModal.tsx`, `PatientStory.tsx`, `SafetyAlertModal.tsx`
- `src/components/orders/OrderSetsPanel.tsx`, `src/components/relay/RoomFitWarning.tsx`, `src/components/sage/WardBrief.tsx`, `src/components/common/PatientPicker.tsx`
- `src/components/bridge/`, `CareTeamCard.tsx`, `VitalsTrendsCard.tsx`
- `src/components/print/`, `MedListPrintView.tsx`, `OrdersPrintView.tsx`
- `src/components/charts/useChartTheme.ts`
- `src/api/`, `deviceAuth.ts`, `orderSets.ts`, `nurse.ts`, `scorecard.ts`, `thinkingMode.ts`, `rosterImport.ts`, `bridgeClient.ts`
- `src/hooks/`, `useOfflineSnapshot.ts`, `useOfflineSession.ts`, `useOfflineStatus.ts`, `useIdleTimer.ts`, `useVisiblePolling.ts`
- `src/lib/`, **all 10 files**: `a11y.ts`, `datetime.ts`, `demographics.ts`, `humanise.ts`, `messageMerge.ts`, `nurseDrafts.ts`, `orbBus.ts`, `roles.ts`, `shortcuts.ts`, `tablist.ts`
- `src/lib/offline/`, `phiCache.ts`, `writeGuard.ts`
- `src/contexts/`, `ActiveViewContext.tsx`, `SageRuntimeContext.tsx`

### 1.7 Schema

**48 tables created in `server/database.py`** (line = the `CREATE TABLE` statement):

`users` :22 · `patients` :34 · `admissions` :56 · `patient_profiles` :80 · `vitals` :93 ·
`risk_scores` :107 · `channels` :117 · `messages` :126 · `sage_conversations` :139 ·
`clinical_documents` :152 · `scribe_notes` :163 · `discharge_summaries` :177 ·
`discharge_access_tokens` :188 · `lens_analyses` :203 · `medications` :216 ·
`medication_administrations` :236 · `forecast_predictions` :257 · `hospital_forecasts` :269 ·
`admission_history` :281 · `surgeries` :297 · `surgery_team_assignments` :320 ·
`appointments` :330 · `surgery_prep_requests` :344 · `ingestion_events` :363 ·
`observations` :385 · `audit_logs` :409 · `sage_room_memory` :480 · `sage_observations` :500 ·
`sage_actions` :514 · `sage_insights` :532 · `sage_action_events` :551 · `sage_handoffs` :567 ·
`sage_handoff_events` :587 · `event_log` :601 · `event_deliveries` :621 · `world_model_state` :651 ·
`insight_feedback` :669 · `schema_version` :681 · `sage_decision_cache` :688 ·
`patient_allergies` :700 · `problem_list` :716 · `care_bundles` :734 · `patient_snapshots` :758 ·
`model_registry` :771 · `orders` :791 · `order_events` :810 · `terminology` :825 ·
`drug_interactions` :839.

Plus four in-place rebuild scratch tables further down the same file, `messages_new` :1575,
`surgeries_new` :1632, and re-declarations of `sage_actions` :1734 / `sage_action_events` :1750
inside legacy repair paths. Do not document these as product tables; they are repair scaffolding.

**24 tables created in `server/migrations.py`:**

`schema_migrations` :35 · `sage_insights` (rebuild) :90 · `shadow_events` :132 ·
`roster_import_runs` :253 · `medication_allergy_overrides` :303 · `message_room_fit_flags` :346 ·
`channel_members` :426 · `medication_dose_overrides` :566 · `clinical_escalations` :633 ·
`escalation_events` :689 · `enrolled_devices` :772 · `order_set_applications` :843 ·
`consent_records` :956 · `consent_withdrawals` :999 · `retention_policies` :1036 ·
`erasure_requests` :1059 · `erasure_decisions` :1087 · `erasure_executions` :1130 ·
`breach_records` :1164 · `breach_notifications` :1191 · `history_import_patient_xref` :1455 ·
`history_import_runs` :1476 · `history_import_rejects` :1505 · `patient_review_marks` :1589.

**17 migrations**, list at `server/migrations.py:1609`:

| # | Name | Function |
|---:|---|---|
| 1 | hot_path_indexes | `server/migrations.py:50` |
| 2 | messages_idempotency | `:61` |
| 3 | sage_insights_auto_resolved | `:77` |
| 4 | shadow_events | `:112` |
| 5 | roster_import_runs | `:243` |
| 6 | allergy_interlock | `:277` |
| 7 | message_room_fit_flags | `:326` |
| 8 | channel_members | `:383` |
| 9 | recover_stranded_sage_insights | `:465` |
| 10 | dose_guard | `:530` |
| 11 | escalation_chain | `:592` |
| 12 | device_pin_identity | `:713` |
| 13 | order_sets | `:795` |
| 14 | dpdp_act_machinery | `:902` |
| 15 | history_import | `:1345` |
| 16 | sage_conversation_threads | `:1525` |
| 17 | patient_review_marks | `:1556` |

The apply loop is `_apply_pending` (`server/migrations.py:1630`) wrapped by
`run_pending_migrations` (`:1650`), which owns its own `BEGIN IMMEDIATE` when the caller has no
open transaction. `current_version` is at `:1715`. Chapter 04 documents none of migrations 5–17.

**None of the 24 migration-created tables is named anywhere in the guide.** Chapter
04-database-schema documents the `database.py` set only.

---

## 2. Citation audit

### 2.1 How this was measured

Three passes, in increasing strictness.

1. **Resolution pass.** Every `path:line` in every chapter was extracted (4,438 of them),
   resolved against the repo, and checked that the file exists and has that many lines.
2. **Context pass.** For each citation, the identifiers on the same chapter line were searched
   for in a ±12-line window around the cited line. 1,051 citations had no identifier match.
   This is a noisy signal (prose often does not repeat a symbol name), use it for triage only.
3. **Anchor pass, the one that matters.** Markdown headings of the form
   `### Symbol, \`path:line\`` are *definitional*: the citation is claiming "this symbol is
   defined here". For each of those 1,108 anchors, the symbol was looked up in the file's real
   symbol table (`def` / `class` / module constant for Python; `export function|const|class|
   interface|type` for TS/TSX; top-level selector for CSS) and compared with the cited line.

**Precision check:** 13 anchor findings were verified by hand, chosen at random across nine
different chapters, by opening both the cited line and the proposed line. **13 of 13 were
genuinely wrong**, and in every case the proposed line was the real definition. Examples:

| Chapter | Cited | What is actually on the cited line | Real definition |
|---|---|---|---|
| 06-realtime | `server/services/bot_service.py:488` | `worsened = (` | `async def run_reports_bot():` at `:751` |
| 03-auth-rbac | `server/models/user.py:40` | `class DeviceBindBody(BaseModel):` | `class TokenResponse(BaseModel):` at `:77` |
| 02-api-client | `src/api/client.ts:911` | a comment about `querySage` | `export async function analyzeLensImage(` at `:1613` |
| 01-app-shell | `src/pages/ClinicianShell.tsx:177` | `</svg>` | `class PanelErrorBoundary …` at `:236` |
| 25-admin-panel | `server/services/compliance_service.py:374` | `add("dpdp_consent", …)` | `def compliance_report() -> dict:` at `:453` |
| 13-forecast | `server/services/forecast_service.py:309` | *(blank line)* | `def _heuristic_prediction(patient_id: str)` at `:316` |

The founder's own example is already fixed: `28-offline-pwa-downtime.md:253` now cites
`server/services/downtime_service.py:300`, which is the `os.makedirs(d, mode=0o700, …)` call.
That is the one confirmed correction already applied; the other 502 are not.

### 2.2 Scoreboard

| Chapter | Chapter lines | Citations | Symbol anchors | Stale anchors | Stale % | Wider suspects |
|---|---:|---:|---:|---:|---:|---:|
| 01-app-shell.md | 492 | 267 | 36 | 10 | 28% | 93 |
| 02-api-client.md | 859 | 188 | 65 | 35 | 54% | 102 |
| 03-auth-rbac.md | 590 | 185 | 38 | 23 | 61% | 60 |
| 04-database-schema.md | 693 | 270 | 77 | 24 | 31% | 50 |
| 05-audit-chain.md | 570 | 289 | 41 | 6 | 15% | 51 |
| 06-realtime.md | 658 | 222 | 51 | 39 | **76%** | 85 |
| 07-vigil.md | 632 | 226 | 66 | 35 | 53% | 67 |
| 08-relay.md | 609 | 203 | 39 | 26 | 67% | 67 |
| 09-sage.md | 689 | 200 | 64 | 42 | 66% | 86 |
| 10-sage-proactive-ambient.md | 663 | 66 | 36 | 32 | **89%** | 45 |
| 11-scribe.md | 724 | 107 | 57 | 31 | 54% | 51 |
| 12-lens.md | 392 | 218 | 26 | 11 | 42% | 55 |
| 13-forecast.md | 593 | 286 | 46 | 25 | 54% | 61 |
| 14-helix.md | 765 | 250 | 65 | 11 | 17% | 42 |
| 15-pharmacy-verify.md | 487 | 62 | 18 | 15 | **83%** | 33 |
| 16-orders-cpoe.md | 479 | 77 | 22 | 15 | 68% | 34 |
| 17-surgical-suite.md | 521 | 71 | 31 | 21 | 68% | 36 |
| 18-command-center.md | 531 | 100 | 30 | 12 | 40% | 37 |
| 19-capacity-simulator.md | 432 | 55 | 15 | 0 | 0% | 13 |
| 20-revenue-integrity.md | 350 | 48 | 12 | 6 | 50% | 15 |
| 21-admissions-discharge.md | 436 | 88 | 31 | 4 | 13% | 20 |
| 22-handoff-ipass.md | 538 | 85 | 35 | 8 | 23% | 24 |
| 23-bridge-patient-portal.md | 448 | 62 | 22 | 11 | 50% | 33 |
| 24-ingestion-interop.md | 569 | 64 | 36 | 18 | 50% | 23 |
| 25-admin-panel.md | 787 | 82 | 56 | 8 | 14% | 16 |
| 26-ai-infrastructure.md | 489 | 59 | 7 | 1 | 14% | 8 |
| 27-design-system.md | 454 | 96 | 11 | 0 | 0% | 13 |
| 28-offline-pwa-downtime.md | 431 | 147 | 30 | 16 | 53% | 40 |
| 29-i18n.md | 261 | 57 | 11 | 0 | 0% | 2 |
| 30-harness-and-ci.md | 479 | 42 | 19 | 2 | 11% | 7 |
| 31-api-reference.md | 987 | 266 | 15 | 15 | **100%** | 36 |
| **TOTAL** | **17,628** | **4,438** | **1,108** | **502** | **45%** | **1,305** |

Read the columns as: "Stale anchors" is the confirmed work list (§6.2 has every row).
"Wider suspects" is a broader heuristic net, a superset that includes false positives; use it
as a to-check list once the confirmed set is done, not as a fix list.

Fix order, by damage per hour of work:
1. **31-api-reference.md**, 100% of anchors stale *and* 101 live endpoints missing. It is the
   chapter an investor's engineer will open first, and it is the least true.
2. **10-sage-proactive-ambient.md** (89%), **15-pharmacy-verify.md** (83%), **06-realtime.md** (76%).
3. **02-api-client.md** (35 stale, and `src/api/client.ts` has grown to ~1,700 lines, nearly
   every function moved).
4. Everything else in descending stale count.

### 2.3 Citations that cannot be repaired by moving the line number

These point at files or symbols that no longer exist. They need prose changes, not renumbering.

**Files that do not exist:**

| Chapter | Chapter line | Citation | Reality |
|---|---:|---|---|
| 17-surgical-suite.md | 411 | `src/components/surgery/SurgeryActionCard.tsx:11` | no such file; `src/components/surgery/` contains only `EmergencyIntakePopup.tsx` and `EmergencyIntakePopup.module.css` |
| 17-surgical-suite.md | 423 | `src/components/surgery/SurgeryReviewSheet.tsx:11` | same |
| 17-surgical-suite.md | 503 | `SurgeryActionCard.tsx:11` | same |
| 05-audit-chain.md | 474 | `AdminPanel.css:138` | no such file; admin styles live in `src/pages/panels/admin/Admin.css` |
| 01-app-shell.md | 393 | `ClockWidget.css:82` | file is 71 lines long |

**Symbols documented that no longer exist anywhere in the cited file:**

| Chapter | Chapter line | Symbol documented | Cited file | Reality |
|---|---:|---|---|---|
| 02-api-client.md | 189 | `authHeaders` | `src/api/client.ts:101` | string absent from the file, the header helper was renamed or inlined |
| 03-auth-rbac.md | 362 | `get_me` | `server/routes/auth_routes.py:228` | moved out of the router entirely; `get_me` now lives at `server/main.py:854`, decorated at `:853` |
| 08-relay.md | 441 | `refreshMessages` | `src/pages/panels/RelayPanel.tsx:206` | absent from the file |
| 01-app-shell.md | 265 | `SHOW_QUICK_ACCESS` / `QUICK_ACCESS` | `src/pages/Login.tsx:13` | absent; Login was rebuilt around device/PIN modes |
| 06-realtime.md | 445 | `vitals_simulator` | `server/main.py:107` | absent from `main.py` |
| 06-realtime.md | 464 | `warmup_model` | `server/main.py:178` | the function is `warmup_models` (plural) at `server/main.py:374`, spawned at `:423` |
| 04-database-schema.md | 573 | `TABLES_TO_CLEAR` | `server/scripts/reset_db_and_seed.py:13` | the constant is `EXTRA_TABLES_TO_CLEAR` at `:25` |

### 2.4 Factual claims that are now wrong

These are worse than a bad line number: the sentence is false.

1. **`06-realtime.md:651`, "`typing` is dead end-to-end."** It is not. The client emits it
   (`src/pages/panels/RelayPanel.tsx:270`) and subscribes to it (`:181`); the server handler is
   `server/ws/realtime.py:408` and it re-emits at `:424`. Delete the claim.
2. **`31-api-reference.md` says routes are "split across 32 files"** (line 6), there are 42
   route files, and 42 routers mounted at `server/main.py:681-724`.
3. **`31-api-reference.md` is missing 101 live endpoints.** Whole feature areas absent:
   every `/api/admin/shadow/*` (8), every `/api/admin/compliance/*` (10), every
   `/api/admin/history-import/*` (7), every `/api/admin/roster-import/*` (5), every
   `/api/admin/users/*` (8), every `/api/consent/*` (3), every `/api/chart/*` (3), every
   `/api/nurse/*` (3), every `/api/emar/*` (2), every `/api/ops/*` (2), every
   `/api/orders/sets/*` (5), every `/api/vigil/escalations*` (6), every `/api/auth/device*` (7),
   `/api/auth/pin-login`, `/api/auth/refresh`, `/api/sage/provenance*` (3),
   `/api/sage/ward-brief`, `/api/sage/prefetch`, `/api/sage/ai-posture`,
   `/api/sage/world/patient/{id}`, `/api/search/patients`, `/api/appointments*` (4),
   `/api/health/*` (5), `/api/admin/backups`, `/api/admin/patients/{id}/erase`,
   `/api/bridge/care-team`, `/api/bridge/vitals-trends`, `/api/bridge/appointments*`,
   `/api/relay/channels/direct`, `/api/relay/channels/{id}/members`,
   `/api/relay/channels/{id}/room_fit`, `/api/relay/room_fit/{id}/resolve`,
   `/api/vigil/patients/{id}/story`, `/api/vigil/patients/{id}/timeline-projection`.
4. **Chapter 04 documents 48 tables and stops.** 24 more exist, created by migrations 4–17,
   and 13 of the 17 migrations are undocumented.
5. **Screenshots are orphaned.** 463 PNGs exist under `docs/screenshots/`; exactly 2 image tags
   exist across all 31 chapters, both in `12-lens.md`. Every "what the user sees" claim in the
   manual is currently unillustrated while the illustrations sit on disk.

### 2.5 The chain format every chapter must use

To satisfy the founder's requirement, each documented unit needs the full chain in one place.
Use this shape (it is what the good parts of chapters 07 and 14 already do, copy it):

```
#### <symbol>: `<path>:<line>`

**Does:** one or two plain sentences.

**On screen:** what the user sees change, named by the visible label, 
"the amber NEWS2 pill on the Patients board", and the component + line that renders it.

**Chain:** <Component>.tsx:<line> → <apiFn> (src/api/client.ts:<line>)
          → <METHOD /path> (<route file>:<line>, guard `<permission>`)
          → <service fn> (<service file>:<line>)
          → tables: <table>, <table>

**Breaks if:** the specific thing that goes wrong when this changes.
```

The route table in §6.1 gives you the middle three links for all 268 endpoints; the socket table
in §1.4 gives them for the realtime paths.

---

## 3. What is documented nowhere

### 3.1 The nine features that landed 2026-08-07: confirmed, all nine have zero coverage

Verified by searching all 31 chapters for the defining filenames, table names and symbols.
Zero hits for each.

| Feature | Server | Frontend | Schema |
|---|---|---|---|
| **Escalation chain** | `services/escalation_service.py` (1,324 ln, 30 fns); 6 routes `/api/vigil/escalations*` (`routes/vigil_routes.py:1011,1076,1098,1117,1138,1155`) | surfaced on Vigil | migration 11 (`migrations.py:592`) → `clinical_escalations` :633, `escalation_events` :689 |
| **PIN-on-device identity** | `services/pin_security.py`; `/api/auth/device` `:1199`, `/api/auth/device/bind` `:1143`, `/api/auth/pin-login` `:1228`, `/api/auth/devices` `:972/:1025`, `/api/auth/devices/{id}/revoke` `:1042`, `/api/auth/devices/{id}/unlock` `:1092`, `/api/auth/device-pin` `:1453`, `/api/auth/device-pin/{user_id}` `:1494`, `/api/admin/users/{id}/pin` (`admin_users_routes.py:334`) | `src/api/deviceAuth.ts`, `src/pages/Login.tsx` PIN mode (`:71`), shared-device idle lock (`src/App.tsx:43,90`) | migration 12 (`:713`) → `enrolled_devices` :772 |
| **Order sets** | `services/order_sets_service.py` (1,066 ln); `/api/orders/sets*`, 5 routes (`routes/orders_routes.py:94,109,119,151,196`); catalogue file `server/site_config/order_sets.json` | `src/api/orderSets.ts`, `src/components/orders/OrderSetsPanel.tsx` | migration 13 (`:795`) → `order_set_applications` :843 |
| **DPDP consent + data rights** | `services/consent_service.py` (1,314 ln), `services/compliance_service.py`; 16 routes in `routes/compliance_routes.py` | `src/pages/panels/admin/TrustCenter.tsx` (partly) | migration 14 (`:902`) → `consent_records` :956, `consent_withdrawals` :999, `retention_policies` :1036, `erasure_requests` :1059, `erasure_decisions` :1087, `erasure_executions` :1130, `breach_records` :1164, `breach_notifications` :1191 |
| **Bulk history import** | `services/history_import_service.py` (2,293 ln, 39 fns, 8 classes); 7 routes `/api/admin/history-import/*` (`routes/admin_routes.py:517,528,539,570,611,666,676`) | admin Data panel | migration 15 (`:1345`) → `history_import_patient_xref` :1455, `history_import_runs` :1476, `history_import_rejects` :1505 |
| **Sage provenance** | `services/sage_provenance_service.py` (974 ln); `/api/sage/provenance` `:2406`, `/api/sage/provenance/storage` `:2391`, `/api/sage/provenance/{turn_id}` `:2441`, all gated on `audit.read` | replay surface | migration 16 (`:1525`) sage conversation threads |
| **Ward round mode** | reuses vigil/scribe/orders endpoints | `src/pages/panels/RoundPanel.tsx` + 7 files in `src/components/round/` (`roundQueue.ts`, `roundSummary.ts` are pure logic with their own tests) | n/a |
| **Offline read cache** | n/a | `src/lib/offline/phiCache.ts`, `src/lib/offline/writeGuard.ts`, `src/hooks/useOfflineSnapshot.ts`, `useOfflineSession.ts`, `useOfflineStatus.ts`, `src/components/OfflineBanner.tsx` | n/a |
| **Ops health + site config** | `services/ops_health_service.py` (1,029 ln), `services/site_config_service.py` (660 ln); `/api/ops/status` and `/api/ops/status.txt` (`routes/ops_routes.py:80,94`); `server/site_config/site.example.toml` | admin status view | n/a |

### 3.2 Everything *else* with no coverage

Discovered by the same search, these were not on the founder's list of nine and are equally
invisible:

1. **Shadow mode, the entire pilot evidence instrument.** `shadow_service.py` (1,062 ln),
   `shadow_scorecard_service.py` (2,039 ln), `shadow_export_service.py` (977 ln),
   `shadow_verdicts.py`; `routes/shadow_routes.py` (8 routes); `src/api/scorecard.ts`;
   `src/pages/panels/admin/Scorecard.tsx`; migration 4 → `shadow_events`. This is the thing the
   pilot is *for* and it has no chapter.
2. **Nurse mode.** `nurse_shift_service.py` (997 ln), `emar_service.py` (883 ln),
   `routes/nurse_routes.py`, `routes/emar_routes.py`; the whole `src/pages/panels/nurse/`
   directory. Chapter 15 covers pharmacy verification, not the nurse's shift board, obs entry,
   eMAR round or handover sheet.
3. **Allergy and dose interlocks.** `allergy_interlock.py` (567 ln),
   `medication_safety_gate.py`; migrations 6 and 10 → `medication_allergy_overrides`,
   `medication_dose_overrides`. This is the sharpest safety story in the product.
4. **Room-fit / bed placement.** `room_fit_service.py` (1,576 ln), 3 relay routes,
   `src/components/relay/RoomFitWarning.tsx`, migration 7 → `message_room_fit_flags`.
5. **Patient chart timeline + review marks.** `patient_timeline_service.py` (806 ln),
   `patient_story_service.py`, `routes/patient_timeline_routes.py` (3 routes),
   `src/components/vigil/PatientStory.tsx`, `src/components/ClinicalCourseTimeline.tsx`,
   migration 17 → `patient_review_marks`.
6. **Roster import.** `roster_import.py` (1,556 ln), `routes/roster_import_routes.py` (5 routes),
   `src/api/rosterImport.ts`, `src/pages/panels/admin/RosterImport.tsx`, migration 5.
7. **Thinking mode / model routing.** `thinking_mode.py` (612 ln), `model_router.py`,
   `llm_providers.py`, `ai_governance.py`, `src/api/thinkingMode.ts`, `/api/sage/ai-posture`.
   The safety floor (patient + dosing stay on the 27B model) is a load-bearing product decision
   with no written description.
8. **Operational resilience.** `loop_watchdog.py` (784 ln), `heartbeat_service.py` (962 ln),
   `bootstrap_service.py` (746 ln), `batch_coordinator.py`, `routes/backup_routes.py`,
   `server/ops/watchdog/`.
9. **Users and access administration.** `routes/admin_users_routes.py` (8 routes),
   `src/pages/panels/admin/UsersPanel.tsx`, `src/pages/panels/admin/DataPanel.tsx`,
   `server/scripts/access_review.py`.
10. **Appointments + patient search.** `routes/appointments_routes.py` (5 routes),
    `routes/search_routes.py`, `src/pages/panels/AppointmentsPanel.tsx`.
11. **Sage support layer.** `sage_narrator_service.py` (ward brief),
    `sage_relay_context.py`, `sage_feedback_service.py`, `world_model_service.py`,
    `knowledge_worker.py`, `retrieval_calibration.py`; `src/components/sage/WardBrief.tsx`,
    `src/contexts/SageRuntimeContext.tsx`.
12. **Frontend plumbing with no home.** All 10 files in `src/lib/`, the keyboard-shortcut system
    (`src/lib/shortcuts.ts`, `src/components/KeyboardShortcuts.tsx`), the command palette,
    `src/components/AiStatusChip.tsx`, `src/components/SessionExpiredModal.tsx`,
    `src/components/MfaEnrollmentModal.tsx`, `src/components/common/PatientPicker.tsx`,
    `src/components/print/*`.
13. **`field_crypto.py`**, field-level encryption, referenced by nothing in the guide even
    though chapter 05 covers the audit chain and chapter 04 the schema.

---

## 4. Proposed chapters 32+

Numbering continues the existing scheme. Scope lines are deliberately narrow, one chapter per
thing a reader would go looking for.

| # | Title | Scope, in one line |
|---|---|---|
| 32 | Escalation chain | How an unacknowledged deterioration climbs from nurse to registrar to consultant: `escalation_service.py`, the 6 `/api/vigil/escalations*` routes, migration 11's two tables, the sweep job, and where the escalation shows on the Patients board. |
| 33 | PIN-on-device identity and shared devices | Device enrolment and binding, PIN login, the 2-minute shared-device idle lock, admin PIN reset, revoke/unlock, server (`pin_security.py`, 9 auth routes, `enrolled_devices`) through to `Login.tsx` PIN mode and `deviceAuth.ts`. |
| 34 | Order sets | The site-configurable bundle catalogue (`site_config/order_sets.json`), preview vs apply, what gets written to `orders` and `order_set_applications`, and the `OrderSetsPanel` UI including its empty state. |
| 35 | DPDP consent and data rights | Consent register and withdrawal, retention policies, the erasure request → adjudicate → execute pipeline, breach records and notifications: `consent_service.py`, `compliance_service.py`, 16 routes, migration 14's eight tables, and the Trust Center surface. |
| 36 | Bulk history import | Record types, the provenance model, streaming parse, dry-run vs commit, the reject ledger and how a rejected row is shown back to the operator: `history_import_service.py`, 7 admin routes, migration 15's three tables. |
| 37 | Sage provenance and replay | What is captured for every Sage turn, where it is stored, storage accounting, and how an auditor replays a turn: `sage_provenance_service.py`, the 3 `audit.read`-gated routes, migration 16. |
| 38 | Ward round mode | The round queue (`roundQueue.ts`), dictation, the generated summary (`roundSummary.ts`), handoff, and the empty/error states, plus which existing endpoints it composes rather than adding new ones. |
| 39 | Offline read cache and the write refusal | `phiCache.ts` and what it is allowed to hold, `writeGuard.ts` and why writes refuse rather than queue, the three offline hooks, and exactly what `OfflineBanner` says to the user in each state. |
| 40 | Ops health and site configuration | `site.example.toml`, the loader's line-numbered validation errors, `ops_health_service.py`, `/api/ops/status` (JSON) and `/api/ops/status.txt` (the human-readable one), and what a red status means operationally. |
| 41 | Shadow mode and the pilot scorecard | Episode capture, verdicts, adjudication, outcome and no-outcome recording (`outcome.record` permission), the scorecard computation and its export, server through `src/api/scorecard.ts` to the admin Scorecard view. |
| 42 | Nurse mode: shift board, observations, eMAR and handover | `nurse_shift_service.py`, `emar_service.py`, `/api/nurse/*` and `/api/emar/*`, NEWS2 preview, and the four nurse surfaces including the tablet obs sheet. |
| 43 | Medication interlocks: allergy and dose | The pure matcher, `allergy_interlock.py`, `medication_safety_gate.py`, the override tables from migrations 6 and 10, the administer flow's hard stop, and `SafetyAlertModal` / `AdministerModal`. |
| 44 | Room fit and bed placement | `room_fit_service.py`, flag creation and resolution, the three relay routes, `message_room_fit_flags`, and `RoomFitWarning` in the Messages panel. |
| 45 | Patient chart timeline, story and review marks | `patient_timeline_service.py`, `patient_story_service.py`, the 3 chart routes, unseen counts, `patient_review_marks`, and the `PatientStory` / `ClinicalCourseTimeline` components including the ward-board unseen badge. |
| 46 | Roster import | Tolerant CSV parsing, column mapping, dry-run, idempotent apply, the run ledger, and the admin Roster Import screen. |
| 47 | Thinking mode, model routing and the AI safety floor | `thinking_mode.py`, `model_router.py`, `llm_providers.py`, `ai_governance.py`, `/api/sage/ai-posture`, the env gate, and what the user sees in `AiStatusChip` when the toggle is off. |
| 48 | Operational resilience: watchdog, heartbeat, bootstrap, backups | `loop_watchdog.py`, `heartbeat_service.py` and its no-PHI payload proof, `bootstrap_service.py` first-admin flow, `backup_routes.py`, and `server/ops/watchdog/`. |
| 49 | User and access administration | `admin_users_routes.py`'s 8 routes, `UsersPanel`, `DataPanel`, MFA enrolment and reset, account lock/unlock, and `access_review.py`. |
| 50 | Appointments and patient search | `appointments_routes.py`, `search_routes.py`, the Appointments panel, and the Bridge-side appointment surfaces. |
| 51 | Sage support layer: ward brief, relay context, world model, feedback | `sage_narrator_service.py`, `sage_relay_context.py`, `world_model_service.py`, `sage_feedback_service.py`, `knowledge_worker.py`, and `WardBrief` / `SageRuntimeContext`. |
| 52 | Frontend foundations: `src/lib`, shortcuts, palette, shared modals | All 10 `src/lib` modules, the shortcut registry and help sheet, the command palette, `PatientPicker`, `AiStatusChip`, `SessionExpiredModal`, `MfaEnrollmentModal`, and the print views. |

**Also required, and not a new chapter, a rewrite:**

- **31-api-reference.md** must be regenerated from the live app, not edited. All 268 endpoints,
  each with method, path, guard, handler `file:line`, and the frontend caller. §6.1 of this file
  is the raw material; it was produced by walking `app.routes` and is correct as of the
  timestamp at the top.
- **04-database-schema.md** must gain the 24 migration-created tables and migrations 5–17.

---

## 5. Suggested wave split

Three agents follow this file. Suggested division, sized by work not by chapter count:

- **Wave 2, trust repair.** Fix all 502 stale citations (§6.2) and every item in §2.3 and §2.4.
  Regenerate 31-api-reference.md from §6.1. Extend 04-database-schema.md with migrations 5–17
  and their 24 tables. Nothing new is written; the existing manual is made true.
- **Wave 3, the nine missing features.** Chapters 32–40, one per feature, each with the §2.5
  chain format and screenshots wired in from `docs/screenshots/`.
- **Wave 4, the rest of the gap.** Chapters 41–52, plus a pass to wire the remaining
  screenshots into existing chapters and to add the "On screen" block to every existing chapter
  section that lacks one.

---

## 6. Appendices

### 6.1 Every registered route

#### `server/main.py`: 9 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/auth/me` | logged in | `get_me`, `server/main.py:853` |
| GET | `/api/health` | **none** | `health`, `server/main.py:864` |
| GET | `/api/health/access` | `admin.overview` | `health_access`, `server/main.py:891` |
| GET | `/api/health/loop` | **none** | `health_loop`, `server/main.py:869` |
| GET | `/api/health/models` | logged in | `health_models`, `server/main.py:911` |
| POST | `/api/health/models/load` | `admin.overview` | `load_model`, `server/main.py:1003` |
| POST | `/api/health/models/unload` | `admin.overview` | `unload_model`, `server/main.py:1035` |
| GET/POST | `/socket.io/{_path:path}` | **none** | `_socketio_wrong_entrypoint`, `server/main.py:1092` |
| GET | `/uploads/{filename:path}` | logged in | `serve_upload`, `server/main.py:770` |

#### `server/routes/admin_routes.py`: 9 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/admin/history-import/commit` | `admin.overview` | `history_import_commit`, `server/routes/admin_routes.py:611` |
| POST | `/api/admin/history-import/dry-run` | `admin.overview` | `history_import_dry_run`, `server/routes/admin_routes.py:570` |
| GET | `/api/admin/history-import/fields` | `admin.overview` | `history_import_fields`, `server/routes/admin_routes.py:528` |
| POST | `/api/admin/history-import/preview` | `admin.overview` | `history_import_preview`, `server/routes/admin_routes.py:539` |
| GET | `/api/admin/history-import/record-types` | `admin.overview` | `history_import_record_types`, `server/routes/admin_routes.py:517` |
| GET | `/api/admin/history-import/runs` | `admin.overview` | `history_import_runs`, `server/routes/admin_routes.py:666` |
| GET | `/api/admin/history-import/runs/{run_id}/rejects` | `admin.overview` | `history_import_rejects`, `server/routes/admin_routes.py:676` |
| GET | `/api/admin/overview` | `admin.overview` | `admin_overview`, `server/routes/admin_routes.py:28` |
| POST | `/api/admin/patients/{patient_id}/erase` | `admin.overview` | `erase_patient`, `server/routes/admin_routes.py:265` |

#### `server/routes/admin_users_routes.py`: 8 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/admin/users` | `admin.overview` | `list_users`, `server/routes/admin_users_routes.py:130` |
| POST | `/api/admin/users` | `admin.overview` | `create_user`, `server/routes/admin_users_routes.py:200` |
| POST | `/api/admin/users/me/password` | logged in | `change_own_password`, `server/routes/admin_users_routes.py:455` |
| PATCH | `/api/admin/users/{user_id}` | `admin.overview` | `patch_user`, `server/routes/admin_users_routes.py:405` |
| POST | `/api/admin/users/{user_id}/activate` | `admin.overview` | `activate_user`, `server/routes/admin_users_routes.py:284` |
| POST | `/api/admin/users/{user_id}/deactivate` | `admin.overview` | `deactivate_user`, `server/routes/admin_users_routes.py:252` |
| POST | `/api/admin/users/{user_id}/pin` | logged in | `set_user_pin`, `server/routes/admin_users_routes.py:334` |
| POST | `/api/admin/users/{user_id}/reset-password` | `admin.overview` | `reset_password`, `server/routes/admin_users_routes.py:305` |

#### `server/routes/admission_routes.py`: 1 route

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/admissions/admit` | `patient.write` | `process_admission`, `server/routes/admission_routes.py:53` |

#### `server/routes/appointments_routes.py`: 5 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/appointments` | `patient.read_all` | `list_appointments`, `server/routes/appointments_routes.py:252` |
| POST | `/api/appointments` | logged in | `book_appointment`, `server/routes/appointments_routes.py:114` |
| GET | `/api/appointments/mine` | patient-self | `my_appointments`, `server/routes/appointments_routes.py:235` |
| DELETE | `/api/appointments/{appointment_id}` | logged in | `cancel_appointment`, `server/routes/appointments_routes.py:292` |
| POST | `/api/appointments/{appointment_id}/complete` | `patient.write` | `complete_appointment`, `server/routes/appointments_routes.py:364` |

#### `server/routes/audit_routes.py`: 1 route

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/audit/logs` | `audit.read` | `get_audit_logs`, `server/routes/audit_routes.py:11` |

#### `server/routes/auth_routes.py`: 17 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/auth/device` | **none** | `device_status`, `server/routes/auth_routes.py:1199` |
| POST | `/api/auth/device-pin` | logged in | `set_own_device_pin`, `server/routes/auth_routes.py:1453` |
| POST | `/api/auth/device-pin/{user_id}` | `admin.overview` | `admin_set_device_pin`, `server/routes/auth_routes.py:1494` |
| POST | `/api/auth/device/bind` | **none** | `bind_device`, `server/routes/auth_routes.py:1143` |
| GET | `/api/auth/devices` | `admin.overview` | `list_devices`, `server/routes/auth_routes.py:1025` |
| POST | `/api/auth/devices` | `admin.overview` | `enroll_device`, `server/routes/auth_routes.py:972` |
| POST | `/api/auth/devices/{device_id}/revoke` | `admin.overview` | `revoke_device`, `server/routes/auth_routes.py:1042` |
| POST | `/api/auth/devices/{device_id}/unlock` | `admin.overview` | `unlock_device`, `server/routes/auth_routes.py:1092` |
| POST | `/api/auth/login` | **none** | `login`, `server/routes/auth_routes.py:361` |
| POST | `/api/auth/logout` | logged in | `logout`, `server/routes/auth_routes.py:855` |
| POST | `/api/auth/mfa/disable` | logged in | `mfa_disable`, `server/routes/auth_routes.py:696` |
| POST | `/api/auth/mfa/enroll/begin` | `_enrolment_principal` | `mfa_enroll_begin`, `server/routes/auth_routes.py:635` |
| POST | `/api/auth/mfa/enroll/confirm` | `_enrolment_principal` | `mfa_enroll_confirm`, `server/routes/auth_routes.py:663` |
| POST | `/api/auth/mfa/reset/{user_id}` | `admin.overview` | `mfa_admin_reset`, `server/routes/auth_routes.py:735` |
| POST | `/api/auth/pin-login` | **none** | `pin_login`, `server/routes/auth_routes.py:1228` |
| POST | `/api/auth/refresh` | logged in | `refresh`, `server/routes/auth_routes.py:889` |
| POST | `/api/auth/unlock/{user_id}` | `admin.overview` | `admin_unlock_account`, `server/routes/auth_routes.py:779` |

#### `server/routes/backup_routes.py`: 2 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/admin/backups` | `admin.overview` | `list_backups`, `server/routes/backup_routes.py:113` |
| POST | `/api/admin/backups` | `admin.overview` | `create_backup`, `server/routes/backup_routes.py:158` |

#### `server/routes/bridge_routes.py`: 12 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/bridge/appointments` | patient-self | `patient_appointments`, `server/routes/bridge_routes.py:359` |
| POST | `/api/bridge/appointments` | patient-self | `request_appointment`, `server/routes/bridge_routes.py:374` |
| DELETE | `/api/bridge/appointments/{appointment_id}` | patient-self | `cancel_appointment`, `server/routes/bridge_routes.py:425` |
| POST | `/api/bridge/ask` | patient-self | `patient_ask`, `server/routes/bridge_routes.py:108` |
| GET | `/api/bridge/care-team` | patient-self | `patient_care_team`, `server/routes/bridge_routes.py:259` |
| GET | `/api/bridge/discharge` | patient-self | `patient_discharge`, `server/routes/bridge_routes.py:210` |
| GET | `/api/bridge/documents` | patient-self | `patient_documents`, `server/routes/bridge_routes.py:240` |
| GET | `/api/bridge/medications` | patient-self | `patient_medications`, `server/routes/bridge_routes.py:193` |
| GET | `/api/bridge/reports` | patient-self | `patient_reports`, `server/routes/bridge_routes.py:142` |
| GET | `/api/bridge/status` | patient-self | `patient_status`, `server/routes/bridge_routes.py:87` |
| GET | `/api/bridge/vitals` | patient-self | `patient_vitals`, `server/routes/bridge_routes.py:98` |
| GET | `/api/bridge/vitals-trends` | patient-self | `patient_vitals_trends`, `server/routes/bridge_routes.py:315` |

#### `server/routes/bundle_routes.py`: 5 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/bundles/active` | `vitals.read` | `active_bundles`, `server/routes/bundle_routes.py:16` |
| GET | `/api/bundles/patient/{patient_id}` | patient-scoped | `patient_bundles`, `server/routes/bundle_routes.py:21` |
| POST | `/api/bundles/patient/{patient_id}/start` | patient-scoped | `start`, `server/routes/bundle_routes.py:30` |
| POST | `/api/bundles/{bundle_id}/cancel` | `alert.read` | `cancel`, `server/routes/bundle_routes.py:67` |
| POST | `/api/bundles/{bundle_id}/complete/{element}` | `alert.read` | `complete`, `server/routes/bundle_routes.py:49` |

#### `server/routes/capacity_sim_routes.py`: 2 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/capacity-sim` | `forecast.read` | `run_scenario`, `server/routes/capacity_sim_routes.py:38` |
| POST | `/api/capacity-sim/` | `forecast.read` | `run_scenario`, `server/routes/capacity_sim_routes.py:38` |

#### `server/routes/clinical_records_routes.py`: 7 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/patients/{patient_id}/allergies` | patient-scoped | `get_allergies`, `server/routes/clinical_records_routes.py:19` |
| POST | `/api/patients/{patient_id}/allergies` | patient-scoped | `post_allergy`, `server/routes/clinical_records_routes.py:28` |
| DELETE | `/api/patients/{patient_id}/allergies/{allergy_id}` | `patient.write` | `delete_allergy`, `server/routes/clinical_records_routes.py:50` |
| GET | `/api/patients/{patient_id}/medication-safety` | patient-scoped | `medication_safety`, `server/routes/clinical_records_routes.py:105` |
| GET | `/api/patients/{patient_id}/problems` | patient-scoped | `get_problems`, `server/routes/clinical_records_routes.py:66` |
| POST | `/api/patients/{patient_id}/problems` | patient-scoped | `post_problem`, `server/routes/clinical_records_routes.py:75` |
| PATCH | `/api/patients/{patient_id}/problems/{problem_id}/resolve` | patient-scoped | `patch_resolve_problem`, `server/routes/clinical_records_routes.py:96` |

#### `server/routes/coding_routes.py`: 1 route

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/coding/patient/{patient_id}` | patient-scoped | `suggest_patient_codes`, `server/routes/coding_routes.py:23` |

#### `server/routes/command_center_routes.py`: 2 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/command-center` | `forecast.read` | `get_house_snapshot`, `server/routes/command_center_routes.py:25` |
| GET | `/api/command-center/` | `forecast.read` | `get_house_snapshot`, `server/routes/command_center_routes.py:25` |

#### `server/routes/compliance_routes.py`: 16 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/admin/compliance/` | `admin.overview` | `get_compliance_report`, `server/routes/compliance_routes.py:125` |
| GET | `/api/admin/compliance/breaches` | `admin.overview` | `get_breaches`, `server/routes/compliance_routes.py:469` |
| POST | `/api/admin/compliance/breaches` | `admin.overview` | `create_breach`, `server/routes/compliance_routes.py:438` |
| GET | `/api/admin/compliance/breaches/{breach_id}` | `admin.overview` | `get_breach_detail`, `server/routes/compliance_routes.py:474` |
| POST | `/api/admin/compliance/breaches/{breach_id}/notifications` | `admin.overview` | `create_breach_notification`, `server/routes/compliance_routes.py:485` |
| GET | `/api/admin/compliance/data-principal/{patient_id}/export` | patient-scoped | `export_data_principal`, `server/routes/compliance_routes.py:239` |
| GET | `/api/admin/compliance/erasure` | `admin.overview` | `list_erasure`, `server/routes/compliance_routes.py:335` |
| POST | `/api/admin/compliance/erasure` | `admin.overview` | `create_erasure_request`, `server/routes/compliance_routes.py:309` |
| GET | `/api/admin/compliance/erasure/{request_id}` | `admin.overview` | `get_erasure`, `server/routes/compliance_routes.py:350` |
| POST | `/api/admin/compliance/erasure/{request_id}/adjudicate` | `admin.overview` | `adjudicate_erasure`, `server/routes/compliance_routes.py:363` |
| POST | `/api/admin/compliance/erasure/{request_id}/execute` | `admin.overview` | `execute_erasure`, `server/routes/compliance_routes.py:392` |
| GET | `/api/admin/compliance/retention` | `admin.overview` | `get_retention`, `server/routes/compliance_routes.py:290` |
| GET | `/api/consent/vocabulary` | `patient.read_all` | `consent_vocabulary`, `server/routes/compliance_routes.py:137` |
| GET | `/api/consent/{patient_id}` | patient-scoped | `list_patient_consents`, `server/routes/compliance_routes.py:148` |
| POST | `/api/consent/{patient_id}` | patient-scoped | `create_consent`, `server/routes/compliance_routes.py:158` |
| POST | `/api/consent/{patient_id}/{consent_id}/withdraw` | patient-scoped | `withdraw_patient_consent`, `server/routes/compliance_routes.py:202` |

#### `server/routes/discharge_access_routes.py`: 2 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/discharge-access/verify` | **none** | `verify_access`, `server/routes/discharge_access_routes.py:14` |
| GET | `/api/discharge-access/{token}/summary` | **none** | `get_summary`, `server/routes/discharge_access_routes.py:29` |

#### `server/routes/downtime_routes.py`: 4 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/downtime/capture-all` | `admin.overview` | `capture_all`, `server/routes/downtime_routes.py:50` |
| POST | `/api/downtime/capture/{patient_id}` | `vitals.read` | `capture`, `server/routes/downtime_routes.py:26` |
| GET | `/api/downtime/{patient_id}/history` | patient-scoped | `history`, `server/routes/downtime_routes.py:87` |
| GET | `/api/downtime/{patient_id}/latest` | patient-scoped | `latest`, `server/routes/downtime_routes.py:74` |

#### `server/routes/emar_routes.py`: 2 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/emar/medications/{med_id}/administer` | `medication.write` | `administer_medication`, `server/routes/emar_routes.py:51` |
| GET | `/api/emar/patients/{patient_id}/administrations` | patient-scoped | `get_patient_administrations`, `server/routes/emar_routes.py:116` |

#### `server/routes/fhir_export_routes.py`: 3 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/fhir/Patient/{patient_id}` | patient-scoped | `export_patient`, `server/routes/fhir_export_routes.py:83` |
| GET | `/api/fhir/patient/{patient_id}/$everything` | logged in | `export_everything`, `server/routes/fhir_export_routes.py:98` |
| GET | `/api/fhir/patient/{patient_id}/MedicationRequest` | patient-scoped | `export_medications`, `server/routes/fhir_export_routes.py:118` |

#### `server/routes/flight_recorder_routes.py`: 3 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/flight-recorder/event/{source}/{event_id}` | `audit.read` | `event_detail`, `server/routes/flight_recorder_routes.py:43` |
| GET | `/api/flight-recorder/timeline` | `audit.read` | `timeline`, `server/routes/flight_recorder_routes.py:21` |
| GET | `/api/flight-recorder/verify` | `audit.read` | `verify`, `server/routes/flight_recorder_routes.py:34` |

#### `server/routes/forecast_routes.py`: 6 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/forecast/admission-patterns` | `forecast.read` | `admission_patterns`, `server/routes/forecast_routes.py:123` |
| GET | `/api/forecast/appointments` | `forecast.read` | `appointments_schedule`, `server/routes/forecast_routes.py:134` |
| GET | `/api/forecast/capacity` | `forecast.read` | `capacity_forecast`, `server/routes/forecast_routes.py:60` |
| GET | `/api/forecast/discharge-board` | `forecast.read` | `discharge_board`, `server/routes/forecast_routes.py:71` |
| GET | `/api/forecast/hospital` | `forecast.read` | `hospital_forecast`, `server/routes/forecast_routes.py:33` |
| GET | `/api/forecast/patient/{patient_id}` | patient-scoped | `patient_forecast`, `server/routes/forecast_routes.py:45` |

#### `server/routes/helix_routes.py`: 8 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/helix/catalog` | `helix.read` | `get_catalog`, `server/routes/helix_routes.py:15` |
| GET | `/api/helix/inventory` | `helix.read` | `get_inventory`, `server/routes/helix_routes.py:20` |
| GET | `/api/helix/orders` | `helix.read` | `get_all_orders`, `server/routes/helix_routes.py:30` |
| POST | `/api/helix/orders/approve` | `helix.write` | `approve_helix_order`, `server/routes/helix_routes.py:107` |
| GET | `/api/helix/orders/pending` | `helix.read` | `get_pending_orders`, `server/routes/helix_routes.py:25` |
| POST | `/api/helix/orders/{order_id}/transition` | `helix.write` | `transition_order`, `server/routes/helix_routes.py:142` |
| GET | `/api/helix/orders/{order_id}/transitions` | `helix.read` | `get_order_transitions`, `server/routes/helix_routes.py:40` |
| GET | `/api/helix/stock-movements` | `helix.read` | `get_stock_movements`, `server/routes/helix_routes.py:35` |

#### `server/routes/impact_routes.py`: 2 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/admin/impact/` | `admin.overview` | `get_impact`, `server/routes/impact_routes.py:21` |
| GET | `/api/admin/impact/assumptions` | `admin.overview` | `get_assumptions`, `server/routes/impact_routes.py:56` |

#### `server/routes/ingestion_routes.py`: 5 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/ingest/fhir` | **none** | `ingest_fhir`, `server/routes/ingestion_routes.py:166` |
| POST | `/api/ingest/hl7` | **none** | `ingest_hl7`, `server/routes/ingestion_routes.py:108` |
| POST | `/api/ingest/json` | **none** | `ingest_json`, `server/routes/ingestion_routes.py:56` |
| POST | `/api/ingest/replay` | **none** | `replay_fixture`, `server/routes/ingestion_routes.py:264` |
| GET | `/api/ingest/status` | `ingest.monitor` | `ingestion_status`, `server/routes/ingestion_routes.py:220` |

#### `server/routes/ipass_routes.py`: 3 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/ipass/{patient_id}/draft` | patient-scoped | `get_draft`, `server/routes/ipass_routes.py:24` |
| GET | `/api/ipass/{patient_id}/recent` | patient-scoped | `recent_handoffs`, `server/routes/ipass_routes.py:75` |
| POST | `/api/ipass/{patient_id}/save` | patient-scoped | `save_draft`, `server/routes/ipass_routes.py:38` |

#### `server/routes/lens_routes.py`: 3 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/lens/analyses` | `lens.read` | `list_analyses`, `server/routes/lens_routes.py:178` |
| PATCH | `/api/lens/analyses/{analysis_id}/assign` | `lens.analyze` | `assign_to_patient`, `server/routes/lens_routes.py:236` |
| POST | `/api/lens/analyze` | `lens.analyze` | `analyze_image`, `server/routes/lens_routes.py:86` |

#### `server/routes/metrics_routes.py`: 1 route

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/metrics` | **none** | `metrics`, `server/routes/metrics_routes.py:46` |

#### `server/routes/model_governance_routes.py`: 3 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/admin/models` | `admin.overview` | `get_models`, `server/routes/model_governance_routes.py:20` |
| GET | `/api/admin/models/provenance` | logged in | `get_model_provenance`, `server/routes/model_governance_routes.py:26` |
| POST | `/api/admin/models/reseed` | `admin.overview` | `reseed_models`, `server/routes/model_governance_routes.py:38` |

#### `server/routes/nurse_routes.py`: 3 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/nurse/board` | `patient.read_all, vitals.read` | `get_board`, `server/routes/nurse_routes.py:51` |
| POST | `/api/nurse/patients/{patient_id}/news2-preview` | patient-scoped | `preview_news2`, `server/routes/nurse_routes.py:112` |
| POST | `/api/nurse/patients/{patient_id}/observations` | patient-scoped | `record_observations`, `server/routes/nurse_routes.py:147` |

#### `server/routes/ops_routes.py`: 2 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/ops/status` | `admin.overview` | `ops_status`, `server/routes/ops_routes.py:80` |
| GET | `/api/ops/status.txt` | `admin.overview` | `ops_status_text`, `server/routes/ops_routes.py:94` |

#### `server/routes/orders_routes.py`: 13 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/orders` | `order.write` | `create_order`, `server/routes/orders_routes.py:231` |
| POST | `/api/orders/` | `order.write` | `create_order`, `server/routes/orders_routes.py:231` |
| GET | `/api/orders/patient/{patient_id}` | patient-scoped | `list_patient_orders`, `server/routes/orders_routes.py:347` |
| GET | `/api/orders/sets` | `order.read` | `list_order_sets`, `server/routes/orders_routes.py:94` |
| GET | `/api/orders/sets/applications/{patient_id}` | patient-scoped | `list_order_set_applications`, `server/routes/orders_routes.py:196` |
| GET | `/api/orders/sets/{set_id}` | `order.read` | `get_order_set`, `server/routes/orders_routes.py:109` |
| POST | `/api/orders/sets/{set_id}/apply` | `order.write` | `apply_order_set`, `server/routes/orders_routes.py:151` |
| POST | `/api/orders/sets/{set_id}/preview` | `order.write` | `preview_order_set`, `server/routes/orders_routes.py:119` |
| GET | `/api/orders/terminology/validate` | `order.read` | `validate_terminology`, `server/routes/orders_routes.py:211` |
| GET | `/api/orders/{order_id}` | `order.read` | `get_order`, `server/routes/orders_routes.py:359` |
| GET | `/api/orders/{order_id}/events` | `order.read` | `get_order_events`, `server/routes/orders_routes.py:367` |
| POST | `/api/orders/{order_id}/result` | `order.write` | `add_result`, `server/routes/orders_routes.py:389` |
| POST | `/api/orders/{order_id}/transition` | `order.write` | `transition_order`, `server/routes/orders_routes.py:374` |

#### `server/routes/patient_timeline_routes.py`: 3 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/chart/patients/{patient_id}/reviewed` | patient-scoped | `mark_chart_reviewed`, `server/routes/patient_timeline_routes.py:74` |
| GET | `/api/chart/patients/{patient_id}/timeline` | patient-scoped | `patient_timeline`, `server/routes/patient_timeline_routes.py:57` |
| GET | `/api/chart/unseen` | `patient.read_all` | `unseen_counts`, `server/routes/patient_timeline_routes.py:99` |

#### `server/routes/pharmacy_routes.py`: 3 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/pharmacy/pending` | `helix.read` | `get_pending`, `server/routes/pharmacy_routes.py:36` |
| POST | `/api/pharmacy/{med_id}/reject` | `helix.write` | `reject`, `server/routes/pharmacy_routes.py:100` |
| POST | `/api/pharmacy/{med_id}/verify` | `helix.write` | `verify`, `server/routes/pharmacy_routes.py:42` |

#### `server/routes/pulse_routes.py`: 6 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/pulse/aqi` | `forecast.read` | `air_quality`, `server/routes/pulse_routes.py:56` |
| GET | `/api/pulse/audit-log` | `audit.read` | `audit_log`, `server/routes/pulse_routes.py:92` |
| GET | `/api/pulse/dashboard` | `forecast.read` | `pulse_dashboard`, `server/routes/pulse_routes.py:30` |
| GET | `/api/pulse/drug-recalls` | `forecast.read` | `drug_recalls`, `server/routes/pulse_routes.py:80` |
| GET | `/api/pulse/flu` | `forecast.read` | `flu_surveillance`, `server/routes/pulse_routes.py:69` |
| GET | `/api/pulse/weather` | `forecast.read` | `weather`, `server/routes/pulse_routes.py:43` |

#### `server/routes/relay_routes.py`: 17 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/relay/actions/{message_id}/execute` | `relay.execute_action` | `execute_ambient_action`, `server/routes/relay_routes.py:1051` |
| GET | `/api/relay/alerts` | `relay.read` | `list_alerts`, `server/routes/relay_routes.py:84` |
| POST | `/api/relay/alerts/{message_id}/resolve` | `relay.send` | `resolve_alert`, `server/routes/relay_routes.py:1200` |
| GET | `/api/relay/channels` | `relay.read` | `list_channels`, `server/routes/relay_routes.py:78` |
| POST | `/api/relay/channels/direct` | `relay.send` | `open_direct_channel`, `server/routes/relay_routes.py:129` |
| GET | `/api/relay/channels/{channel_id}/members` | `relay.read` | `get_channel_members`, `server/routes/relay_routes.py:146` |
| POST | `/api/relay/channels/{channel_id}/members` | `relay.send` | `add_member`, `server/routes/relay_routes.py:157` |
| GET | `/api/relay/channels/{channel_id}/messages` | `relay.read` | `get_messages`, `server/routes/relay_routes.py:90` |
| POST | `/api/relay/channels/{channel_id}/messages` | `relay.send` | `post_message`, `server/routes/relay_routes.py:383` |
| POST | `/api/relay/channels/{channel_id}/read` | `relay.read` | `mark_read`, `server/routes/relay_routes.py:101` |
| GET | `/api/relay/channels/{channel_id}/room_fit` | `relay.read` | `list_room_fit_flags`, `server/routes/relay_routes.py:651` |
| GET | `/api/relay/channels/{channel_id}/sage_actions` | `relay.read` | `get_sage_actions`, `server/routes/relay_routes.py:210` |
| POST | `/api/relay/channels/{channel_id}/sage_actions/{action_id}/confirm_agentic` | `relay.execute_action` | `confirm_agentic_action`, `server/routes/relay_routes.py:1464` |
| GET | `/api/relay/channels/{channel_id}/sage_actions/{action_id}/pre_flight` | `relay.execute_action` | `pre_flight_sage_action`, `server/routes/relay_routes.py:1251` |
| POST | `/api/relay/channels/{channel_id}/sage_actions/{action_id}/transition` | `relay.execute_action` | `transition_sage_action`, `server/routes/relay_routes.py:350` |
| GET | `/api/relay/channels/{channel_id}/sage_memory` | `relay.read` | `get_sage_memory`, `server/routes/relay_routes.py:176` |
| POST | `/api/relay/room_fit/{flag_id}/resolve` | `relay.send` | `resolve_room_fit_flag`, `server/routes/relay_routes.py:717` |

#### `server/routes/roster_import_routes.py`: 5 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/admin/roster-import/commit` | `admin.overview` | `commit`, `server/routes/roster_import_routes.py:199` |
| POST | `/api/admin/roster-import/dry-run` | `admin.overview` | `dry_run`, `server/routes/roster_import_routes.py:154` |
| GET | `/api/admin/roster-import/fields` | `admin.overview` | `get_fields`, `server/routes/roster_import_routes.py:113` |
| POST | `/api/admin/roster-import/preview` | `admin.overview` | `preview`, `server/routes/roster_import_routes.py:127` |
| GET | `/api/admin/roster-import/runs` | `admin.overview` | `runs`, `server/routes/roster_import_routes.py:260` |

#### `server/routes/sage_proactive_routes.py`: 5 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/sage/insights` | `sage.query` | `list_insights`, `server/routes/sage_proactive_routes.py:14` |
| POST | `/api/sage/insights/evaluate` | `sage.query` | `evaluate`, `server/routes/sage_proactive_routes.py:139` |
| GET | `/api/sage/insights/feedback` | `sage.query` | `insight_feedback`, `server/routes/sage_proactive_routes.py:21` |
| POST | `/api/sage/insights/{insight_id}/act` | `sage.query` | `act_on_insight`, `server/routes/sage_proactive_routes.py:127` |
| POST | `/api/sage/insights/{insight_id}/transition` | `sage.query` | `transition`, `server/routes/sage_proactive_routes.py:36` |

#### `server/routes/sage_routes.py`: 11 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/sage/ai-posture` | logged in | `get_ai_posture`, `server/routes/sage_routes.py:2782` |
| DELETE | `/api/sage/history` | `sage.query` | `clear_history`, `server/routes/sage_routes.py:2491` |
| GET | `/api/sage/history` | `sage.query` | `get_history`, `server/routes/sage_routes.py:2480` |
| POST | `/api/sage/prefetch` | `sage.query` | `sage_prefetch`, `server/routes/sage_routes.py:2791` |
| GET | `/api/sage/provenance` | `audit.read` | `list_provenance`, `server/routes/sage_routes.py:2406` |
| GET | `/api/sage/provenance/storage` | `audit.read` | `get_provenance_storage`, `server/routes/sage_routes.py:2391` |
| GET | `/api/sage/provenance/{turn_id}` | `audit.read` | `replay_provenance`, `server/routes/sage_routes.py:2441` |
| POST | `/api/sage/query` | `sage.query` | `sage_query`, `server/routes/sage_routes.py:1505` |
| POST | `/api/sage/voice` | `sage.query` | `sage_voice_query`, `server/routes/sage_routes.py:2506` |
| GET | `/api/sage/ward-brief` | `sage.query` | `get_ward_brief`, `server/routes/sage_routes.py:2740` |
| GET | `/api/sage/world/patient/{patient_id}` | patient-scoped | `get_patient_world`, `server/routes/sage_routes.py:2708` |

#### `server/routes/scribe_routes.py`: 13 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/scribe/config` | `admin.overview` | `get_scribe_config`, `server/routes/scribe_routes.py:903` |
| GET | `/api/scribe/daemon/status` | `scribe.read_all` | `daemon_status`, `server/routes/scribe_routes.py:934` |
| GET | `/api/scribe/notes` | `scribe.read_all` | `list_notes`, `server/routes/scribe_routes.py:541` |
| GET | `/api/scribe/notes/{note_id}` | `scribe.read_all` | `get_note`, `server/routes/scribe_routes.py:565` |
| PATCH | `/api/scribe/notes/{note_id}` | `scribe.edit_draft` | `edit_note`, `server/routes/scribe_routes.py:672` |
| POST | `/api/scribe/notes/{note_id}/reject` | `scribe.reject` | `reject_note`, `server/routes/scribe_routes.py:820` |
| POST | `/api/scribe/notes/{note_id}/revise` | `scribe.edit_draft` | `revise_note`, `server/routes/scribe_routes.py:861` |
| POST | `/api/scribe/notes/{note_id}/sign` | `scribe.sign` | `sign_note`, `server/routes/scribe_routes.py:734` |
| GET | `/api/scribe/notes/{note_id}/verification` | `scribe.read_all` | `get_note_verification`, `server/routes/scribe_routes.py:571` |
| POST | `/api/scribe/notes/{note_id}/verify` | `scribe.edit_draft` | `retry_verification`, `server/routes/scribe_routes.py:595` |
| POST | `/api/scribe/structure` | `scribe.create_draft` | `structure`, `server/routes/scribe_routes.py:422` |
| POST | `/api/scribe/transcribe` | `scribe.create_draft` | `transcribe`, `server/routes/scribe_routes.py:310` |
| POST | `/api/scribe/transcribe-chunk` | `scribe.create_draft` | `transcribe_chunk`, `server/routes/scribe_routes.py:366` |

#### `server/routes/search_routes.py`: 1 route

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/search/patients` | `patient.read_all` | `search_patients`, `server/routes/search_routes.py:26` |

#### `server/routes/shadow_routes.py`: 8 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| POST | `/api/admin/shadow/adjudication` | `admin.overview` | `post_adjudication`, `server/routes/shadow_routes.py:892` |
| GET | `/api/admin/shadow/episode/{episode_id}` | `admin.overview` | `get_episode`, `server/routes/shadow_routes.py:493` |
| GET | `/api/admin/shadow/episodes` | `admin.overview` | `list_episodes`, `server/routes/shadow_routes.py:387` |
| GET | `/api/admin/shadow/export/scorecard` | `admin.overview` | `export_scorecard`, `server/routes/shadow_routes.py:973` |
| POST | `/api/admin/shadow/outcome` | `outcome.record` | `post_outcome`, `server/routes/shadow_routes.py:520` |
| POST | `/api/admin/shadow/outcome/none` | `outcome.record` | `post_no_outcome`, `server/routes/shadow_routes.py:568` |
| GET | `/api/admin/shadow/outcome/shift-status` | `outcome.record` | `get_shift_status`, `server/routes/shadow_routes.py:756` |
| GET | `/api/admin/shadow/scorecard` | `admin.overview` | `get_scorecard`, `server/routes/shadow_routes.py:304` |

#### `server/routes/surgery_routes.py`: 14 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/surgery/cases` | `surgery.read` | `get_surgery_cases`, `server/routes/surgery_routes.py:131` |
| GET | `/api/surgery/checklist/template` | `surgery.read` | `who_checklist_template`, `server/routes/surgery_routes.py:236` |
| GET | `/api/surgery/emergency-queue` | `surgery.read` | `get_emergency_queue`, `server/routes/surgery_routes.py:117` |
| POST | `/api/surgery/emergency/{surgery_id}/acknowledge` | `surgery.write` | `acknowledge_emergency_surgery`, `server/routes/surgery_routes.py:631` |
| GET | `/api/surgery/schedule` | `surgery.read` | `get_surgery_schedule`, `server/routes/surgery_routes.py:67` |
| GET | `/api/surgery/{surgery_id}` | `surgery.read` | `get_surgery_detail`, `server/routes/surgery_routes.py:161` |
| POST | `/api/surgery/{surgery_id}/alert-team` | `surgery.write` | `alert_surgical_team`, `server/routes/surgery_routes.py:368` |
| GET | `/api/surgery/{surgery_id}/checklist` | `surgery.read` | `get_who_checklist`, `server/routes/surgery_routes.py:243` |
| POST | `/api/surgery/{surgery_id}/checklist` | `surgery.write` | `set_who_checklist_item`, `server/routes/surgery_routes.py:253` |
| POST | `/api/surgery/{surgery_id}/complete` | `surgery.write` | `complete_surgery`, `server/routes/surgery_routes.py:530` |
| POST | `/api/surgery/{surgery_id}/prep` | `surgery.write` | `prep_for_surgery`, `server/routes/surgery_routes.py:605` |
| GET | `/api/surgery/{surgery_id}/risk` | `surgery.read` | `surgery_risk_assessment`, `server/routes/surgery_routes.py:665` |
| POST | `/api/surgery/{surgery_id}/start` | `surgery.write` | `start_surgery`, `server/routes/surgery_routes.py:488` |
| POST | `/api/surgery/{surgery_id}/team-response` | `surgery.read` | `surgical_team_response`, `server/routes/surgery_routes.py:403` |

#### `server/routes/vigil_routes.py`: 22 routes

| Method | Path | Guard | Handler |
|---|---|---|---|
| GET | `/api/vigil/discharged` | `patient.read_all` | `list_discharged`, `server/routes/vigil_routes.py:392` |
| GET | `/api/vigil/escalations` | `alert.read` | `list_escalations_route`, `server/routes/vigil_routes.py:1011` |
| POST | `/api/vigil/escalations/sweep` | `alert.read` | `sweep_escalations_route`, `server/routes/vigil_routes.py:1138` |
| GET | `/api/vigil/escalations/{escalation_id}` | `alert.read` | `get_escalation_route`, `server/routes/vigil_routes.py:1076` |
| POST | `/api/vigil/escalations/{escalation_id}/acknowledge` | `alert.read, vitals.write` | `acknowledge_escalation_route`, `server/routes/vigil_routes.py:1098` |
| POST | `/api/vigil/escalations/{escalation_id}/resolve` | `alert.read, vitals.write` | `resolve_escalation_route`, `server/routes/vigil_routes.py:1117` |
| GET | `/api/vigil/patients` | `patient.read_all` | `list_patients`, `server/routes/vigil_routes.py:262` |
| POST | `/api/vigil/patients` | `patient.write` | `create_patient`, `server/routes/vigil_routes.py:853` |
| GET | `/api/vigil/patients/{patient_id}` | patient-scoped | `get_patient`, `server/routes/vigil_routes.py:499` |
| GET | `/api/vigil/patients/{patient_id}/analysis` | patient-scoped | `get_patient_analysis`, `server/routes/vigil_routes.py:623` |
| POST | `/api/vigil/patients/{patient_id}/discharge` | `discharge.generate` | `discharge_patient_route`, `server/routes/vigil_routes.py:713` |
| GET | `/api/vigil/patients/{patient_id}/discharge-summary` | patient-scoped | `get_discharge_summary`, `server/routes/vigil_routes.py:767` |
| GET | `/api/vigil/patients/{patient_id}/escalations` | patient-scoped | `patient_escalations_route`, `server/routes/vigil_routes.py:1155` |
| GET | `/api/vigil/patients/{patient_id}/medication-reconciliation` | patient-scoped | `medication_reconciliation_route`, `server/routes/vigil_routes.py:696` |
| POST | `/api/vigil/patients/{patient_id}/medications` | patient-scoped | `add_patient_medication`, `server/routes/vigil_routes.py:790` |
| GET | `/api/vigil/patients/{patient_id}/notes` | patient-scoped | `get_patient_notes`, `server/routes/vigil_routes.py:586` |
| POST | `/api/vigil/patients/{patient_id}/notes` | patient-scoped | `add_patient_note`, `server/routes/vigil_routes.py:598` |
| GET | `/api/vigil/patients/{patient_id}/qr` | patient-scoped | `get_qr_code`, `server/routes/vigil_routes.py:831` |
| GET | `/api/vigil/patients/{patient_id}/story` | patient-scoped | `get_patient_story_route`, `server/routes/vigil_routes.py:466` |
| GET | `/api/vigil/patients/{patient_id}/timeline-projection` | patient-scoped | `get_patient_timeline_projection`, `server/routes/vigil_routes.py:925` |
| GET | `/api/vigil/patients/{patient_id}/vitals` | patient-scoped | `get_patient_vitals`, `server/routes/vigil_routes.py:560` |
| GET | `/api/vigil/test-stream` | `patient.read_all` | `test_stream`, `server/routes/vigil_routes.py:239` |

### 6.2 Every confirmed-stale citation

Read as: *at line N of this chapter, the citation `X` claims symbol `S` is defined there; `S` is
actually defined at `Y`.* Verify each one by opening the file before you change it, a symbol
with several definitions shows several lines.

For a decorated route handler, `Y` is the `def` line. §6.1 gives the decorator line for the same
function, one or two lines earlier. Both are correct; prefer §6.1's for a route (it is where the
path and method are declared) and this table's for a plain function.

**01-app-shell.md**, 10 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 193 | `RootErrorBoundary` | `src/main.tsx:26` | `src/main.tsx:44` |
| 219 | `AuthContext` | `src/App.tsx:28` | `src/App.tsx:51` |
| 235 | `App` | `src/App.tsx:45` | `src/App.tsx:68` |
| 271 | `Login` | `src/pages/Login.tsx:29` | `src/pages/Login.tsx:11` |
| 292 | `Tab` | `src/pages/ClinicianShell.tsx:12` | `src/pages/ClinicianShell.tsx:22` |
| 298 | `getAccessibleTabs` | `src/pages/ClinicianShell.tsx:153` | `src/pages/ClinicianShell.tsx:182` |
| 307 | `PanelErrorBoundary` | `src/pages/ClinicianShell.tsx:177` | `src/pages/ClinicianShell.tsx:236` |
| 317 | `ClinicianShell` | `src/pages/ClinicianShell.tsx:213` | `src/pages/ClinicianShell.tsx:305` |
| 366 | `FOCUSABLE_SELECTOR` | `src/hooks/useFocusTrap.ts:19` | `src/hooks/useFocusTrap.ts:45` |
| 369 | `useFocusTrap` | `src/hooks/useFocusTrap.ts:28` | `src/hooks/useFocusTrap.ts:54` |

**02-api-client.md**, 35 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 149 | `authenticateSocket` | `src/api/client.ts:49` | `src/api/client.ts:57` |
| 159 | `deauthenticateSocket` | `src/api/client.ts:60` | `src/api/client.ts:68` |
| 169 | `setToken` | `src/api/client.ts:68` | `src/api/client.ts:76` |
| 179 | `StoredUser` | `src/api/client.ts:81` | `src/api/client.ts:89` |
| 199 | `apiFetch` | `src/api/client.ts:109` | `src/api/client.ts:195` |
| 217 | `handleResponseError` | `src/api/client.ts:139` | `src/api/client.ts:271` |
| 242 | `parseSSEData` | `src/api/client.ts:189` | `src/api/client.ts:319` |
| 262 | `processOfflineQueue` | `src/api/client.ts:229` | `src/api/client.ts:465` |
| 278 | `login` | `src/api/client.ts:278` | `src/api/client.ts:581` |
| 305 | `getPatientForecast` | `src/api/client.ts:327` | `src/api/client.ts:638` |
| 315 | `admitPatient` | `src/api/client.ts:336` | `src/api/client.ts:647` |
| 362 | `querySage` | `src/api/client.ts:506` | `src/api/client.ts:900` |
| 376 | `getSageHistory` | `src/api/client.ts:547` | `src/api/client.ts:1023` |
| 382 | `querySageVoice` | `src/api/client.ts:556` | `src/api/client.ts:1032` |
| 392 | `transcribeAudio` | `src/api/client.ts:605` | `src/api/client.ts:1086` |
| 402 | `transcribeChunk` | `src/api/client.ts:629` | `src/api/client.ts:1110` |
| 412 | `structureNote` | `src/api/client.ts:654` | `src/api/client.ts:1134` |
| 449 | `askBridge` | `src/api/client.ts:766` | `src/api/client.ts:1246` |
| 457 | `getPatientNotes` | `src/api/client.ts:798` | `src/api/client.ts:1315` |
| 463 | `addClinicalNote` | `src/api/client.ts:802` | `src/api/client.ts:1319` |
| 473 | `addMedication` | `src/api/client.ts:818` | `src/api/client.ts:1332` |
| 482 | `getPatientAnalysis` | `src/api/client.ts:833` | `src/api/client.ts:1532` |
| 499 | `getDischargeQrObjectUrl` | `src/api/client.ts:894` | `src/api/client.ts:1593` |
| 509 | `analyzeLensImage` | `src/api/client.ts:911` | `src/api/client.ts:1613` |
| 519 | `getLensAnalyses` | `src/api/client.ts:943` | `src/api/client.ts:1664` |
| 548 | `getSurgeryRiskAssessment` | `src/api/client.ts:978` | `src/api/client.ts:1699` |
| 555 | `createPatient` | `src/api/client.ts:1050` | `src/api/client.ts:1771` |
| 561 | `triggerDeterioration` | `src/api/client.ts:1058` | `src/api/client.ts:1779` |
| 577 | `getAuditLogs` | `src/api/client.ts:1116` | `src/api/client.ts:1837` |
| 587 | `verifyDischargeAccess` | `src/api/client.ts:1139` | `src/api/client.ts:1860` |
| 597 | `getVerifiedDischargeSummary` | `src/api/client.ts:1152` | `src/api/client.ts:1873` |
| 619 | `transitionHelixOrder` | `src/api/client.ts:1174` | `src/api/client.ts:1895` |
| 735 | `getPendingVerifications` | `src/api/pharmacy.ts:66` | `src/api/pharmacy.ts:92` |
| 744 | `verifyMedication` | `src/api/pharmacy.ts:72` | `src/api/pharmacy.ts:106` |
| 754 | `rejectMedication` | `src/api/pharmacy.ts:83` | `src/api/pharmacy.ts:146` |

**03-auth-rbac.md**, 23 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 137 | `hash_password` | `server/auth.py:17` | `server/auth.py:155` |
| 147 | `verify_password` | `server/auth.py:25` | `server/auth.py:163` |
| 157 | `create_token` | `server/auth.py:40` | `server/auth.py:323` |
| 167 | `decode_token` | `server/auth.py:66` | `server/auth.py:371` |
| 177 | `get_current_user` | `server/auth.py:75` | `server/auth.py:416` |
| 187 | `require_role` | `server/auth.py:104` | `server/auth.py:493` |
| 209 | `ROLE_PERMISSIONS` | `server/permissions.py:93` | `server/permissions.py:104` |
| 260 | `has_permission` | `server/permissions.py:144` | `server/permissions.py:172` |
| 270 | `require_permission` | `server/permissions.py:164` | `server/permissions.py:376` |
| 280 | `require_patient_self` | `server/permissions.py:224` | `server/permissions.py:523` |
| 302 | `_prune_login_attempts` | `server/routes/auth_routes.py:35` | `server/routes/auth_routes.py:46` |
| 312 | `is_ip_rate_limited` | `server/routes/auth_routes.py:50` | `server/routes/auth_routes.py:61` |
| 322 | `record_login_attempt` | `server/routes/auth_routes.py:63` | `server/routes/auth_routes.py:74` |
| 332 | `consume_totp` | `server/routes/auth_routes.py:68` | `server/routes/auth_routes.py:275` |
| 342 | `login` | `server/routes/auth_routes.py:96` | `server/routes/auth_routes.py:362` |
| 372 | `mfa_enroll_begin` | `server/routes/auth_routes.py:245` | `server/routes/auth_routes.py:636` |
| 382 | `mfa_enroll_confirm` | `server/routes/auth_routes.py:273` | `server/routes/auth_routes.py:664` |
| 392 | `mfa_disable` | `server/routes/auth_routes.py:306` | `server/routes/auth_routes.py:697` |
| 402 | `logout` | `server/routes/auth_routes.py:341` | `server/routes/auth_routes.py:856` |
| 484 | `MfaEnrollBody` | `server/models/user.py:20` | `server/models/user.py:57` |
| 489 | `UserOut` | `server/models/user.py:31` | `server/models/user.py:68` |
| 495 | `TokenResponse` | `server/models/user.py:40` | `server/models/user.py:77` |
| 517 | `DEPLOYMENT_PROFILE` | `server/config.py:86` | `server/config.py:11` |

**04-database-schema.md**, 24 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 368 | `_open_raw_connection` | `server/database.py:743` | `server/database.py:854` |
| 378 | `_sqlcipher_key_hex` | `server/database.py:775` | `server/database.py:896` |
| 388 | `_warn_db_encryption_unavailable` | `server/database.py:788` | `server/database.py:909` |
| 395 | `get_db` | `server/database.py:800` | `server/database.py:1008` |
| 405 | `db_session` | `server/database.py:817` | `server/database.py:1034` |
| 415 | `_harden_db_file_permissions` | `server/database.py:832` | `server/database.py:1280` |
| 423 | `init_db` | `server/database.py:872` | `server/database.py:1427` |
| 450 | `verify_audit_chain` | `server/database.py:1234` | `server/database.py:1928` |
| 505 | `cleanup` | `server/cleanup_data.py:8` | `server/cleanup_data.py:14` |
| 517 | `_default_db_path` | `server/scripts/backup_db.py:46` | `server/scripts/backup_db.py:51` |
| 523 | `parse_args` | `server/scripts/backup_db.py:63` | `server/scripts/backup_db.py:68` |
| 528 | `_resolve_out_dir` | `server/scripts/backup_db.py:104` | `server/scripts/backup_db.py:109` |
| 533 | `make_backup` | `server/scripts/backup_db.py:120` | `server/scripts/backup_db.py:125` |
| 543 | `_verify_backup` | `server/scripts/backup_db.py:159` | `server/scripts/backup_db.py:169` |
| 549 | `prune_old_backups` | `server/scripts/backup_db.py:174` | `server/scripts/backup_db.py:186` |
| 554 | `main` | `server/scripts/backup_db.py:200` | `server/scripts/backup_db.py:212` |
| 578 | `reset_and_seed` | `server/scripts/reset_db_and_seed.py:41` | `server/scripts/reset_db_and_seed.py:81` |
| 587 | `seed_clinical_history` | `server/scripts/reset_db_and_seed.py:95` | `server/scripts/reset_db_and_seed.py:155` |
| 595 | `link_patient_login` | `server/scripts/reset_db_and_seed.py:170` | `server/scripts/reset_db_and_seed.py:230` |
| 600 | `randomize_dates` | `server/scripts/reset_db_and_seed.py:186` | `server/scripts/reset_db_and_seed.py:261` |
| 613 | `USERS` | `server/scripts/seed_clinical_data.py:15` | `server/scripts/seed_clinical_data.py:20` |
| 619 | `TRIAGE` | `server/scripts/seed_clinical_data.py:32` | `server/scripts/seed_clinical_data.py:37` |
| 625 | `generate_vitals_history` | `server/scripts/seed_clinical_data.py:238` | `server/scripts/seed_clinical_data.py:243` |
| 631 | `seed` | `server/scripts/seed_clinical_data.py:266` | `server/scripts/seed_clinical_data.py:271` |

**05-audit-chain.md**, 6 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 125 | `_HASH_FIELDS` | `server/services/audit_service.py:40` | `server/services/audit_service.py:133` |
| 135 | `compute_audit_row_hash` | `server/services/audit_service.py:48` | `server/services/audit_service.py:141` |
| 147 | `AuditWriteError` | `server/services/audit_service.py:34` | `server/services/audit_service.py:39` |
| 156 | `log_audit_event` | `server/services/audit_service.py:66` | `server/services/audit_service.py:159` |
| 174 | `_actor_from_request` | `server/services/audit_middleware.py:30` | `server/services/audit_middleware.py:39` |
| 186 | `audit_mutations` | `server/services/audit_middleware.py:44` | `server/services/audit_middleware.py:53` |

**06-realtime.md**, 39 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 114 | `sio` | `server/ws/realtime.py:20` | `server/ws/realtime.py:36` |
| 127 | `_authenticate` | `server/ws/realtime.py:28` | `server/ws/realtime.py:61` |
| 142 | `connect` | `server/ws/realtime.py:69` | `server/ws/realtime.py:151` |
| 155 | `disconnect` | `server/ws/realtime.py:92` | `server/ws/realtime.py:351` |
| 165 | `_user_can_access_channel` | `server/ws/realtime.py:97` | `server/ws/realtime.py:363` |
| 177 | `join_channel` | `server/ws/realtime.py:112` | `server/ws/realtime.py:381` |
| 191 | `leave_channel` | `server/ws/realtime.py:131` | `server/ws/realtime.py:400` |
| 201 | `typing` | `server/ws/realtime.py:139` | `server/ws/realtime.py:408` |
| 214 | `new_message` | `server/ws/realtime.py:159` | `server/ws/realtime.py:428` |
| 235 | `StateEvent` | `server/services/event_bus.py:22` | `server/services/event_bus.py:98` |
| 245 | `LocalEventBus` | `server/services/event_bus.py:60` | `server/services/event_bus.py:140` |
| 255 | `LocalEventBus` | `server/services/event_bus.py:71` | `server/services/event_bus.py:140` |
| 268 | `LocalEventBus` | `server/services/event_bus.py:112` | `server/services/event_bus.py:140` |
| 274 | `LocalEventBus` | `server/services/event_bus.py:120` | `server/services/event_bus.py:140` |
| 283 | `event_bus` | `server/services/event_bus.py:135` | `server/services/event_bus.py:510` |
| 290 | `on_critical_vigil_alert` | `server/services/nervous_system_triggers.py:16` | `server/services/nervous_system_triggers.py:21` |
| 303 | `on_scribe_note_signed` | `server/services/nervous_system_triggers.py:75` | `server/services/nervous_system_triggers.py:707` |
| 319 | `on_relay_voice_prewarm` | `server/services/nervous_system_triggers.py:213` | `server/services/nervous_system_triggers.py:798` |
| 329 | `register_triggers` | `server/services/nervous_system_triggers.py:226` | `server/services/nervous_system_triggers.py:811` |
| 344 | `_get_patient_severity` | `server/services/bot_service.py:68` | `server/services/bot_service.py:101` |
| 354 | `run_vitals_bot` | `server/services/bot_service.py:99` | `server/services/bot_service.py:311` |
| 371 | `evaluate_and_dispatch_alerts` | `server/services/bot_service.py:259` | `server/services/bot_service.py:431` |
| 387 | `get_dynamic_finding` | `server/services/bot_service.py:440` | `server/services/bot_service.py:664` |
| 395 | `run_reports_bot` | `server/services/bot_service.py:488` | `server/services/bot_service.py:751` |
| 408 | `LAB_PANELS` | `server/services/bot_service.py:564` | `server/services/bot_service.py:805` |
| 427 | `proactive_sweep` | `server/main.py:62` | `server/main.py:123` |
| 437 | `downtime_snapshot_sweep` | `server/main.py:79` | `server/main.py:140` |
| 455 | `lifespan` | `server/main.py:130` | `server/main.py:210` |
| 472 | `warmup_voice_agent` | `server/main.py:215` | `server/main.py:443` |
| 478 | `app` | `server/main.py:249` | `server/main.py:585` |
| 496 | `_resolve_media_patient` | `server/main.py:354` | `server/main.py:749` |
| 503 | `serve_upload` | `server/main.py:376` | `server/main.py:771` |
| 518 | `get_me` | `server/main.py:444` | `server/main.py:854` |
| 523 | `health` | `server/main.py:455` | `server/main.py:865` |
| 526 | `socket_app` | `server/main.py:461` | `server/main.py:1174` |
| 553 | `authenticateSocket` | `src/api/client.ts:49` | `src/api/client.ts:57` |
| 560 | `deauthenticateSocket` | `src/api/client.ts:60` | `src/api/client.ts:68` |
| 565 | `ClinicianShell` | `src/pages/ClinicianShell.tsx:1` | `src/pages/ClinicianShell.tsx:305` |
| 582 | `test_trigger_scribe_note_signed_syncs_ehr` | `server/tests/test_hospital_nervous_system.py:148` | `server/tests/test_hospital_nervous_system.py:157` |

**07-vigil.md**, 35 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 148 | `OrbVigilCfC` | `server/models/vigil_cfc.py:138` | `server/models/vigil_cfc.py:87` |
| 227 | `_assemble_feature_sequence` | `server/services/vigil_service.py:111` | `server/services/vigil_service.py:120` |
| 235 | `compute_cfc_risk` | `server/services/vigil_service.py:167` | `server/services/vigil_service.py:176` |
| 243 | `compute_news2` | `server/services/vigil_service.py:225` | `server/services/vigil_service.py:234` |
| 268 | `compute_mews` | `server/services/vigil_service.py:343` | `server/services/vigil_service.py:402` |
| 274 | `get_patient_risk` | `server/services/vigil_service.py:348` | `server/services/vigil_service.py:407` |
| 283 | `get_vitals_trend` | `server/services/vigil_service.py:416` | `server/services/vigil_service.py:481` |
| 290 | `generate_patient_analysis` | `server/services/vigil_service.py:470` | `server/services/vigil_service.py:570` |
| 299 | `generate_ai_recommendation` | `server/services/vigil_service.py:654` | `server/services/vigil_service.py:783` |
| 364 | `test_stream` | `server/routes/vigil_routes.py:13` | `server/routes/vigil_routes.py:240` |
| 369 | `list_patients` | `server/routes/vigil_routes.py:36` | `server/routes/vigil_routes.py:265` |
| 377 | `list_discharged` | `server/routes/vigil_routes.py:97` | `server/routes/vigil_routes.py:395` |
| 380 | `get_patient` | `server/routes/vigil_routes.py:111` | `server/routes/vigil_routes.py:500` |
| 387 | `get_patient_vitals` | `server/routes/vigil_routes.py:217` | `server/routes/vigil_routes.py:561` |
| 392 | `get_patient_notes` | `server/routes/vigil_routes.py:245` | `server/routes/vigil_routes.py:587` |
| 397 | `get_patient_analysis` | `server/routes/vigil_routes.py:282` | `server/routes/vigil_routes.py:624` |
| 404 | `medication_reconciliation_route` | `server/routes/vigil_routes.py:350` | `server/routes/vigil_routes.py:697` |
| 408 | `discharge_patient_route` | `server/routes/vigil_routes.py:366` | `server/routes/vigil_routes.py:714` |
| 414 | `get_discharge_summary` | `server/routes/vigil_routes.py:418` | `server/routes/vigil_routes.py:768` |
| 417 | `add_patient_medication` | `server/routes/vigil_routes.py:441` | `server/routes/vigil_routes.py:791` |
| 422 | `get_qr_code` | `server/routes/vigil_routes.py:470` | `server/routes/vigil_routes.py:832` |
| 427 | `create_patient` | `server/routes/vigil_routes.py:495` | `server/routes/vigil_routes.py:856` |
| 445 | `start` | `server/routes/bundle_routes.py:27` | `server/routes/bundle_routes.py:31` |
| 450 | `complete` | `server/routes/bundle_routes.py:46` | `server/routes/bundle_routes.py:50` |
| 453 | `cancel` | `server/routes/bundle_routes.py:64` | `server/routes/bundle_routes.py:68` |
| 475 | `formatDuration` | `src/components/vigil/CareBundleTimer.tsx:52` | `src/components/vigil/CareBundleTimer.tsx:61` |
| 478 | `STATE_META` | `src/components/vigil/CareBundleTimer.tsx:62` | `src/components/vigil/CareBundleTimer.tsx:76` |
| 481 | `ElapsedTimer` | `src/components/vigil/CareBundleTimer.tsx:74` | `src/components/vigil/CareBundleTimer.tsx:88` |
| 487 | `CareBundleTimer` | `src/components/vigil/CareBundleTimer.tsx:108` | `src/components/vigil/CareBundleTimer.tsx:122` |
| 498 | `VigilPanel` | `src/pages/panels/VigilPanel.tsx:20` | `src/pages/panels/VigilPanel.tsx:65` |
| 506 | `isEmergency` | `src/pages/panels/VigilPanel.tsx:158` | `src/pages/panels/VigilPanel.tsx:364` |
| 512 | `PatientDetailView` | `src/pages/panels/VigilPanel.tsx:194` | `src/pages/panels/VigilPanel.tsx:430` |
| 534 | `VitalCard` | `src/pages/panels/VigilPanel.tsx:1638` | `src/pages/panels/VigilPanel.tsx:2064` |
| 538 | `RiskGauge` | `src/pages/panels/VigilPanel.tsx:1651` | `src/pages/panels/VigilPanel.tsx:2169` |
| 543 | `AiNarrative` | `src/pages/panels/VigilPanel.tsx:1682` | `src/pages/panels/VigilPanel.tsx:2203` |

**08-relay.md**, 26 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 129 | `get_user_alerts` | `server/services/relay_service.py:95` | `server/services/relay_service.py:510` |
| 144 | `get_channel_messages` | `server/services/relay_service.py:148` | `server/services/relay_service.py:584` |
| 154 | `mark_channel_read` | `server/services/relay_service.py:181` | `server/services/relay_service.py:688` |
| 164 | `send_message` | `server/services/relay_service.py:212` | `server/services/relay_service.py:778` |
| 188 | `list_channels` | `server/routes/relay_routes.py:22` | `server/routes/relay_routes.py:79` |
| 198 | `list_alerts` | `server/routes/relay_routes.py:28` | `server/routes/relay_routes.py:85` |
| 206 | `get_messages` | `server/routes/relay_routes.py:34` | `server/routes/relay_routes.py:91` |
| 216 | `mark_read` | `server/routes/relay_routes.py:45` | `server/routes/relay_routes.py:102` |
| 225 | `get_sage_memory` | `server/routes/relay_routes.py:57` | `server/routes/relay_routes.py:177` |
| 235 | `get_sage_actions` | `server/routes/relay_routes.py:92` | `server/routes/relay_routes.py:211` |
| 245 | `transition_sage_action` | `server/routes/relay_routes.py:136` | `server/routes/relay_routes.py:351` |
| 262 | `post_message` | `server/routes/relay_routes.py:206` | `server/routes/relay_routes.py:384` |
| 276 | `execute_ambient_action` | `server/routes/relay_routes.py:233` | `server/routes/relay_routes.py:1052` |
| 292 | `AlertActionPayload` | `server/routes/relay_routes.py:401` | `server/routes/relay_routes.py:1107` |
| 298 | `resolve_alert` | `server/routes/relay_routes.py:405` | `server/routes/relay_routes.py:1201` |
| 313 | `ConfirmAgenticRequest` | `server/routes/relay_routes.py:454` | `server/routes/relay_routes.py:1236` |
| 318 | `pre_flight_sage_action` | `server/routes/relay_routes.py:461` | `server/routes/relay_routes.py:1252` |
| 333 | `confirm_agentic_action` | `server/routes/relay_routes.py:521` | `server/routes/relay_routes.py:1465` |
| 367 | `formatMessage` | `src/pages/panels/RelayPanel.tsx:8` | `src/pages/panels/RelayPanel.tsx:14` |
| 387 | `formatTime` | `src/pages/panels/RelayPanel.tsx:28` | `src/pages/panels/RelayPanel.tsx:23` |
| 392 | `RelayPanel` | `src/pages/panels/RelayPanel.tsx:38` | `src/pages/panels/RelayPanel.tsx:33` |
| 408 | `loadChannels` | `src/pages/panels/RelayPanel.tsx:147` | `src/pages/panels/RelayPanel.tsx:205` |
| 417 | `loadMessages` | `src/pages/panels/RelayPanel.tsx:151` | `src/pages/panels/RelayPanel.tsx:217` |
| 426 | `handleSend` | `src/pages/panels/RelayPanel.tsx:166` | `src/pages/panels/RelayPanel.tsx:273` |
| 448 | `caseRooms` | `src/pages/panels/RelayPanel.tsx:213` | `src/pages/panels/RelayPanel.tsx:415` |
| 458 | `deptChannels` | `src/pages/panels/RelayPanel.tsx:242` | `src/pages/panels/RelayPanel.tsx:454` |

**09-sage.md**, 42 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 112 | `SAGE_SYSTEM_PROMPT` | `server/services/sage_service.py:17` | `server/services/sage_service.py:21` |
| 122 | `SAGE_BROAD_SYSTEM_PROMPT` | `server/services/sage_service.py:97` | `server/services/sage_service.py:443` |
| 132 | `HIGH_RISK_PATTERNS` | `server/services/sage_service.py:114` | `server/services/sage_service.py:461` |
| 142 | `MUTATION_PATTERNS` | `server/services/sage_service.py:149` | `server/services/sage_service.py:621` |
| 152 | `SCOPE_CONFIGS` | `server/services/sage_service.py:167` | `server/services/sage_service.py:841` |
| 160 | `query_ollama` | `server/services/sage_service.py:174` | `server/services/sage_service.py:1285` |
| 170 | `_assemble_patient_context` | `server/services/sage_service.py:267` | `server/services/sage_service.py:1758` |
| 180 | `get_relevant_context` | `server/services/sage_service.py:349` | `server/services/sage_service.py:2050` |
| 190 | `_get_guideline_context` | `server/services/sage_service.py:701` | `server/services/sage_service.py:2764` |
| 200 | `save_conversation` | `server/services/sage_service.py:791` | `server/services/sage_service.py:2886` |
| 207 | `get_conversation_history` | `server/services/sage_service.py:800` | `server/services/sage_service.py:2902` |
| 214 | `clear_sage_history` | `server/services/sage_service.py:822` | `server/services/sage_service.py:2971` |
| 221 | `_get_embedder` | `server/services/clinical_knowledge.py:19` | `server/services/clinical_knowledge.py:69` |
| 229 | `_embed_text` | `server/services/clinical_knowledge.py:46` | `server/services/clinical_knowledge.py:116` |
| 236 | `CLINICAL_DOCUMENTS` | `server/services/clinical_knowledge.py:64` | `server/services/clinical_knowledge.py:147` |
| 245 | `MEDICAL_ABBREVIATIONS` | `server/services/clinical_knowledge.py:342` | `server/services/clinical_knowledge.py:425` |
| 253 | `chunk_text` | `server/services/clinical_knowledge.py:421` | `server/services/clinical_knowledge.py:504` |
| 260 | `_corpus_fingerprint` | `server/services/clinical_knowledge.py:438` | `server/services/clinical_knowledge.py:521` |
| 267 | `init_knowledge_base` | `server/services/clinical_knowledge.py:453` | `server/services/clinical_knowledge.py:536` |
| 280 | `_rerank_terms` | `server/services/clinical_knowledge.py:675` | `server/services/clinical_knowledge.py:757` |
| 286 | `_get_cross_encoder` | `server/services/clinical_knowledge.py:684` | `server/services/clinical_knowledge.py:766` |
| 294 | `_lexical_rerank_score` | `server/services/clinical_knowledge.py:733` | `server/services/clinical_knowledge.py:837` |
| 303 | `rerank_results` | `server/services/clinical_knowledge.py:776` | `server/services/clinical_knowledge.py:880` |
| 311 | `_approved_document_ids` | `server/services/clinical_knowledge.py:831` | `server/services/clinical_knowledge.py:956` |
| 319 | `search_guidelines` | `server/services/clinical_knowledge.py:864` | `server/services/clinical_knowledge.py:989` |
| 394 | `load_directory` | `server/services/corpus_loader.py:562` | `server/services/corpus_loader.py:567` |
| 401 | `_table_exists` | `server/services/corpus_loader.py:621` | `server/services/corpus_loader.py:634` |
| 480 | `stripMarkdown` | `src/pages/panels/SagePanel.tsx:19` | `src/pages/panels/SagePanel.tsx:35` |
| 487 | `stripXmlTags` | `src/pages/panels/SagePanel.tsx:38` | `src/pages/panels/SagePanel.tsx:54` |
| 491 | `CLINICAL_CORRECTIONS` | `src/pages/panels/SagePanel.tsx:75` | `src/pages/panels/SagePanel.tsx:92` |
| 499 | `extractSentences` | `src/pages/panels/SagePanel.tsx:126` | `src/pages/panels/SagePanel.tsx:143` |
| 506 | `getBestVoice` | `src/pages/panels/SagePanel.tsx:145` | `src/pages/panels/SagePanel.tsx:162` |
| 511 | `SagePanel` | `src/pages/panels/SagePanel.tsx:169` | `src/pages/panels/SagePanel.tsx:186` |
| 527 | `SageComposer` | `src/components/sage/SageComposer.tsx:17` | `src/components/sage/SageComposer.tsx:22` |
| 564 | `formatForDisplay` | `src/components/sage/SageMessage.tsx:51` | `src/components/sage/SageMessage.tsx:64` |
| 571 | `SageDetailsExpander` | `src/components/sage/SageMessage.tsx:63` | `src/components/sage/SageMessage.tsx:124` |
| 579 | `SageActionCard` | `src/components/sage/SageMessage.tsx:110` | `src/components/sage/SageMessage.tsx:180` |
| 587 | `SageMessage` | `src/components/sage/SageMessage.tsx:190` | `src/components/sage/SageMessage.tsx:260` |
| 611 | `sage_query` | `server/routes/sage_routes.py:48` | `server/routes/sage_routes.py:1506` |
| 621 | `get_history` | `server/routes/sage_routes.py:363` | `server/routes/sage_routes.py:2481` |
| 627 | `clear_history` | `server/routes/sage_routes.py:370` | `server/routes/sage_routes.py:2492` |
| 634 | `sage_voice_query` | `server/routes/sage_routes.py:385` | `server/routes/sage_routes.py:2507` |

**10-sage-proactive-ambient.md**, 32 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 130 | `_now` | `sage_proactive_service.py:22` | `server/services/sage_proactive_service.py:53` |
| 140 | `_level_rank` | `sage_proactive_service.py:26` | `server/services/sage_proactive_service.py:57` |
| 149 | `_num` | `sage_proactive_service.py:30` | `server/services/sage_proactive_service.py:61` |
| 156 | `_parse_drivers` | `sage_proactive_service.py:40` | `server/services/sage_proactive_service.py:71` |
| 164 | `_hours_since` | `sage_proactive_service.py:50` | `server/services/sage_proactive_service.py:81` |
| 171 | `_recent_dedup_exists` | `sage_proactive_service.py:61` | `server/services/sage_proactive_service.py:92` |
| 176 | `_insert_insight` | `sage_proactive_service.py:69` | `server/services/sage_proactive_service.py:100` |
| 190 | `evaluate_patient` | `sage_proactive_service.py:93` | `server/services/sage_proactive_service.py:134` |
| 211 | `evaluate_all_admitted` | `sage_proactive_service.py:200` | `server/services/sage_proactive_service.py:271` |
| 219 | `get_active_insights` | `sage_proactive_service.py:213` | `server/services/sage_proactive_service.py:293` |
| 228 | `transition_insight` | `sage_proactive_service.py:235` | `server/services/sage_proactive_service.py:315` |
| 255 | `transition` | `sage_proactive_routes.py:19` | `server/routes/sage_proactive_routes.py:37` |
| 262 | `act_on_insight` | `sage_proactive_routes.py:36` | `server/routes/sage_proactive_routes.py:128` |
| 279 | `evaluate` | `sage_proactive_routes.py:75` | `server/routes/sage_proactive_routes.py:140` |
| 300 | `_deterministic_extract` | `sage_ambient_service.py:96` | `server/services/sage_ambient_service.py:117` |
| 311 | `_insert_deterministic_action` | `sage_ambient_service.py:113` | `server/services/sage_ambient_service.py:192` |
| 319 | `_normalize` | `sage_ambient_service.py:169` | `server/services/sage_ambient_service.py:1186` |
| 325 | `_is_duplicate` | `sage_ambient_service.py:179` | `server/services/sage_ambient_service.py:1196` |
| 332 | `_record_hash` | `sage_ambient_service.py:198` | `server/services/sage_ambient_service.py:1215` |
| 351 | `_load_room_memory` | `sage_ambient_service.py:260` | `server/services/sage_ambient_service.py:1285` |
| 356 | `_fetch_patient_context` | `sage_ambient_service.py:274` | `server/services/sage_ambient_service.py:1299` |
| 362 | `_fetch_recent_messages` | `sage_ambient_service.py:308` | `server/services/sage_ambient_service.py:1333` |
| 368 | `_fetch_message` | `sage_ambient_service.py:323` | `server/services/sage_ambient_service.py:1348` |
| 374 | `_build_prompt` | `sage_ambient_service.py:339` | `server/services/sage_ambient_service.py:1371` |
| 381 | `_classify_with_llm` | `sage_ambient_service.py:373` | `server/services/sage_ambient_service.py:1448` |
| 390 | `_cap_json_array` | `sage_ambient_service.py:420` | `server/services/sage_ambient_service.py:1492` |
| 396 | `_merge_intent` | `sage_ambient_service.py:431` | `server/services/sage_ambient_service.py:1548` |
| 401 | `_merge_risk` | `sage_ambient_service.py:452` | `server/services/sage_ambient_service.py:1569` |
| 408 | `_update_memory` | `sage_ambient_service.py:476` | `server/services/sage_ambient_service.py:1603` |
| 432 | `_write_observation` | `sage_ambient_service.py:612` | `server/services/sage_ambient_service.py:1825` |
| 439 | `sage_ambient_observe` | `sage_ambient_service.py:650` | `server/services/sage_ambient_service.py:1869` |
| 480 | `SageInsightsFeed` | `SageInsightsFeed.tsx:21` | `src/components/sage/SageInsightsFeed.tsx:51` |

**11-scribe.md**, 31 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 230 | `structure_transcript_stream` | `scribe_service.py:423` | `server/services/scribe_service.py:431` |
| 238 | `VERIFY_PROMPT` | `scribe_service.py:453` | `server/services/scribe_service.py:482` |
| 243 | `verify_note` | `scribe_service.py:509` | `server/services/scribe_service.py:540` |
| 258 | `build_pipeline_meta` | `scribe_service.py:581` | `server/services/scribe_service.py:621` |
| 264 | `save_scribe_note` | `scribe_service.py:602` | `server/services/scribe_service.py:709` |
| 272 | `get_scribe_notes` | `scribe_service.py:632` | `server/services/scribe_service.py:740` |
| 277 | `update_scribe_note_content` | `scribe_service.py:667` | `server/services/scribe_service.py:775` |
| 283 | `sign_scribe_note` | `scribe_service.py:695` | `server/services/scribe_service.py:803` |
| 295 | `reject_scribe_note` | `scribe_service.py:765` | `server/services/scribe_service.py:873` |
| 300 | `create_revision_from_signed` | `scribe_service.py:792` | `server/services/scribe_service.py:900` |
| 306 | `update_scribe_note_verification` | `scribe_service.py:836` | `server/services/scribe_service.py:944` |
| 313 | `get_scribe_note_by_id` | `scribe_service.py:889` | `server/services/scribe_service.py:997` |
| 333 | `MLXProvider` | `scribe_providers.py:187` | `server/services/scribe_providers.py:193` |
| 340 | `_PROVIDERS` | `scribe_providers.py:334` | `server/services/scribe_providers.py:343` |
| 345 | `StageTimer` | `scribe_providers.py:348` | `server/services/scribe_providers.py:359` |
| 389 | `VOICE_SYSTEM_PROMPT` | `voice_agent_service.py:29` | `server/services/voice_agent_service.py:39` |
| 392 | `build_patient_context` | `voice_agent_service.py:47` | `server/services/voice_agent_service.py:57` |
| 398 | `VoiceStreamCleaner` | `voice_agent_service.py:122` | `server/services/voice_agent_service.py:148` |
| 403 | `VoiceAgent` | `voice_agent_service.py:240` | `server/services/voice_agent_service.py:266` |
| 413 | `get_voice_agent` | `voice_agent_service.py:500` | `server/services/voice_agent_service.py:578` |
| 472 | `reconcile_stale_pending_verifications` | `scribe_routes.py:225` | `server/routes/scribe_routes.py:229` |
| 478 | `transcribe` | `scribe_routes.py:306` | `server/routes/scribe_routes.py:311` |
| 487 | `transcribe_chunk` | `scribe_routes.py:362` | `server/routes/scribe_routes.py:367` |
| 493 | `structure` | `scribe_routes.py:418` | `server/routes/scribe_routes.py:423` |
| 516 | `edit_note` | `scribe_routes.py:631` | `server/routes/scribe_routes.py:673` |
| 520 | `_verification_verdict` | `scribe_routes.py:666` | `server/routes/scribe_routes.py:707` |
| 526 | `sign_note` | `scribe_routes.py:693` | `server/routes/scribe_routes.py:735` |
| 534 | `reject_note` | `scribe_routes.py:779` | `server/routes/scribe_routes.py:821` |
| 538 | `get_scribe_config` | `scribe_routes.py:862` | `server/routes/scribe_routes.py:904` |
| 606 | `QuickAdmitModal` | `ScribePanel.tsx:416` | `src/pages/panels/ScribePanel.tsx:441` |
| 611 | `StructuredNoteModal` | `ScribePanel.tsx:554` | `src/pages/panels/ScribePanel.tsx:625` |

**12-lens.md**, 11 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 193 | `_detect_image_ext` | `server/routes/lens_routes.py:33` | `server/routes/lens_routes.py:73` |
| 202 | `analyze_image` | `server/routes/lens_routes.py:47` | `server/routes/lens_routes.py:87` |
| 218 | `stream_analysis` | `server/routes/lens_routes.py:113` | `server/routes/lens_routes.py:151` |
| 227 | `list_analyses` | `server/routes/lens_routes.py:137` | `server/routes/lens_routes.py:181` |
| 236 | `assign_to_patient` | `server/routes/lens_routes.py:146` | `server/routes/lens_routes.py:237` |
| 261 | `handleFile` | `src/pages/panels/LensPanel.tsx:32` | `src/pages/panels/LensPanel.tsx:38` |
| 269 | `handleDrop` | `src/pages/panels/LensPanel.tsx:42` | `src/pages/panels/LensPanel.tsx:60` |
| 276 | `handleAnalyze` | `src/pages/panels/LensPanel.tsx:48` | `src/pages/panels/LensPanel.tsx:66` |
| 285 | `handleAssign` | `src/pages/panels/LensPanel.tsx:64` | `src/pages/panels/LensPanel.tsx:83` |
| 295 | `filteredPatients` | `src/pages/panels/LensPanel.tsx:84` | `src/pages/panels/LensPanel.tsx:108` |
| 343 | `test_single_shared_model_instance` | `server/tests/test_lens.py:112` | `server/tests/test_lens.py:119` |

**13-forecast.md**, 25 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 144 | `OrbForecastLSTM` | `server/models/forecast_lstm.py:102` | `server/models/forecast_lstm.py:51` |
| 158 | `OrbForecastLSTM` | `server/models/forecast_lstm.py:121` | `server/models/forecast_lstm.py:51` |
| 200 | `WARD_CAPACITY` | `server/services/forecast_service.py:48` | `server/services/forecast_service.py:55` |
| 207 | `_get_environmental_multipliers` | `server/services/forecast_service.py:64` | `server/services/forecast_service.py:71` |
| 221 | `extract_patient_features` | `server/services/forecast_service.py:162` | `server/services/forecast_service.py:169` |
| 237 | `predict_patient_los` | `server/services/forecast_service.py:262` | `server/services/forecast_service.py:269` |
| 251 | `_heuristic_prediction` | `server/services/forecast_service.py:309` | `server/services/forecast_service.py:316` |
| 265 | `_cache_prediction` | `server/services/forecast_service.py:351` | `server/services/forecast_service.py:358` |
| 274 | `predict_hospital_flow` | `server/services/forecast_service.py:380` | `server/services/forecast_service.py:387` |
| 291 | `get_bed_capacity_forecast` | `server/services/forecast_service.py:528` | `server/services/forecast_service.py:535` |
| 303 | `get_discharge_board` | `server/services/forecast_service.py:580` | `server/services/forecast_service.py:587` |
| 312 | `get_admission_patterns` | `server/services/forecast_service.py:630` | `server/services/forecast_service.py:637` |
| 320 | `refresh_all_predictions` | `server/services/forecast_service.py:714` | `server/services/forecast_service.py:721` |
| 329 | `_normalize_ward` | `server/services/forecast_service.py:753` | `server/services/forecast_service.py:760` |
| 336 | `_get_cached_or_predict` | `server/services/forecast_service.py:767` | `server/services/forecast_service.py:774` |
| 343 | `get_appointments` | `server/services/forecast_service.py:824` | `server/services/forecast_service.py:831` |
| 359 | `hospital_forecast` | `server/routes/forecast_routes.py:27` | `server/routes/forecast_routes.py:34` |
| 365 | `patient_forecast` | `server/routes/forecast_routes.py:39` | `server/routes/forecast_routes.py:46` |
| 370 | `capacity_forecast` | `server/routes/forecast_routes.py:52` | `server/routes/forecast_routes.py:61` |
| 375 | `discharge_board` | `server/routes/forecast_routes.py:63` | `server/routes/forecast_routes.py:72` |
| 380 | `admission_patterns` | `server/routes/forecast_routes.py:74` | `server/routes/forecast_routes.py:124` |
| 385 | `appointments_schedule` | `server/routes/forecast_routes.py:85` | `server/routes/forecast_routes.py:135` |
| 481 | `MiniBar` | `src/pages/panels/ForecastPanel.tsx:78` | `src/pages/panels/ForecastPanel.tsx:94` |
| 487 | `MetricCard` | `src/pages/panels/ForecastPanel.tsx:92` | `src/pages/panels/ForecastPanel.tsx:110` |
| 492 | `ForecastPanel` | `src/pages/panels/ForecastPanel.tsx:107` | `src/pages/panels/ForecastPanel.tsx:125` |

**14-helix.md**, 11 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 342 | `_DRUG_CLASS_PATTERNS` | `server/services/drug_safety_service.py:28` | `server/services/drug_safety_service.py:66` |
| 349 | `_CROSS_REACTIVITY_PAIRS` | `server/services/drug_safety_service.py:55` | `server/services/drug_safety_service.py:139` |
| 356 | `classify_drug` | `server/services/drug_safety_service.py:75` | `server/services/drug_safety_service.py:159` |
| 363 | `_norm` | `server/services/drug_safety_service.py:87` | `server/services/drug_safety_service.py:240` |
| 366 | `_active_allergies` | `server/services/drug_safety_service.py:91` | `server/services/drug_safety_service.py:283` |
| 371 | `check_allergies` | `server/services/drug_safety_service.py:100` | `server/services/drug_safety_service.py:382` |
| 381 | `check_interactions` | `server/services/drug_safety_service.py:161` | `server/services/drug_safety_service.py:392` |
| 391 | `check_medication_safety` | `server/services/drug_safety_service.py:200` | `server/services/drug_safety_service.py:431` |
| 509 | `approve_helix_order` | `server/routes/helix_routes.py:57` | `server/routes/helix_routes.py:108` |
| 519 | `transition_order` | `server/routes/helix_routes.py:86` | `server/routes/helix_routes.py:143` |
| 636 | `MetricCard` | `src/components/helix/HelixDashboard.tsx:154` | `src/components/helix/HelixDashboard.tsx:175` |

**15-pharmacy-verify.md**, 15 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 167 | `_serialize_med` | `pharmacy_verification_service.py:51` | `server/services/pharmacy_verification_service.py:59` |
| 179 | `list_pending_verification` | `pharmacy_verification_service.py:70` | `server/services/pharmacy_verification_service.py:78` |
| 194 | `_fetch_med` | `pharmacy_verification_service.py:103` | `server/services/pharmacy_verification_service.py:116` |
| 206 | `verify_medication` | `pharmacy_verification_service.py:113` | `server/services/pharmacy_verification_service.py:126` |
| 231 | `reject_medication` | `pharmacy_verification_service.py:157` | `server/services/pharmacy_verification_service.py:224` |
| 263 | `get_pending` | `pharmacy_routes.py:33` | `server/routes/pharmacy_routes.py:37` |
| 275 | `verify` | `pharmacy_routes.py:39` | `server/routes/pharmacy_routes.py:43` |
| 291 | `reject` | `pharmacy_routes.py:79` | `server/routes/pharmacy_routes.py:101` |
| 308 | `severityColor` | `PharmacyVerifyQueue.tsx:59` | `src/components/helix/PharmacyVerifyQueue.tsx:70` |
| 315 | `SafetyAlerts` | `PharmacyVerifyQueue.tsx:65` | `src/components/helix/PharmacyVerifyQueue.tsx:100` |
| 324 | `SourceBadge` | `PharmacyVerifyQueue.tsx:92` | `src/components/helix/PharmacyVerifyQueue.tsx:153` |
| 331 | `PharmacyVerifyQueue` | `PharmacyVerifyQueue.tsx:113` | `src/components/helix/PharmacyVerifyQueue.tsx:174` |
| 364 | `getPendingVerifications` | `pharmacy.ts:66` | `src/api/pharmacy.ts:92` |
| 370 | `verifyMedication` | `pharmacy.ts:72` | `src/api/pharmacy.ts:106` |
| 376 | `rejectMedication` | `pharmacy.ts:83` | `src/api/pharmacy.ts:146` |

**16-orders-cpoe.md**, 15 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 185 | `transition_order` | `orders_service.py:143` | `server/services/orders_service.py:150` |
| 201 | `add_result` | `orders_service.py:191` | `server/services/orders_service.py:198` |
| 215 | `list_orders` | `orders_service.py:230` | `server/services/orders_service.py:237` |
| 224 | `get_order` | `orders_service.py:246` | `server/services/orders_service.py:253` |
| 230 | `get_order_events` | `orders_service.py:252` | `server/services/orders_service.py:259` |
| 249 | `validate_terminology` | `orders_routes.py:46` | `server/routes/orders_routes.py:212` |
| 258 | `create_order` | `orders_routes.py:66` | `server/routes/orders_routes.py:233` |
| 267 | `list_patient_orders` | `orders_routes.py:87` | `server/routes/orders_routes.py:348` |
| 273 | `get_order` | `orders_routes.py:97` | `server/routes/orders_routes.py:360` |
| 278 | `get_order_events` | `orders_routes.py:105` | `server/routes/orders_routes.py:368` |
| 284 | `transition_order` | `orders_routes.py:112` | `server/routes/orders_routes.py:375` |
| 289 | `add_result` | `orders_routes.py:127` | `server/routes/orders_routes.py:390` |
| 305 | `nextStatus` | `OrdersPanel.tsx:45` | `src/components/orders/OrdersPanel.tsx:89` |
| 312 | `OrdersPanel` | `OrdersPanel.tsx:54` | `src/components/orders/OrdersPanel.tsx:98` |
| 328 | `CreateOrderForm` | `OrdersPanel.tsx:210` | `src/components/orders/OrdersPanel.tsx:413` |

**17-surgical-suite.md**, 21 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 139 | `generate_risk_assessment` | `server/services/surgery_service.py:133` | `server/services/surgery_service.py:140` |
| 155 | `trigger_surgery_prep` | `server/services/surgery_service.py:212` | `server/services/surgery_service.py:255` |
| 174 | `create_surgery_prep_request` | `server/services/surgery_escalation_service.py:10` | `server/services/surgery_escalation_service.py:27` |
| 184 | `confirm_surgery_prep` | `server/services/surgery_escalation_service.py:37` | `server/services/surgery_escalation_service.py:106` |
| 199 | `generate_surgical_prep_package_async` | `server/services/surgery_escalation_service.py:104` | `server/services/surgery_escalation_service.py:256` |
| 212 | `get_emergency_queue` | `server/services/surgery_escalation_service.py:232` | `server/services/surgery_escalation_service.py:331` |
| 222 | `acknowledge_emergency` | `server/services/surgery_escalation_service.py:259` | `server/services/surgery_escalation_service.py:358` |
| 275 | `get_surgery_schedule` | `server/routes/surgery_routes.py:19` | `server/routes/surgery_routes.py:68` |
| 283 | `get_emergency_queue` | `server/routes/surgery_routes.py:66` | `server/routes/surgery_routes.py:118` |
| 288 | `get_surgery_cases` | `server/routes/surgery_routes.py:80` | `server/routes/surgery_routes.py:132` |
| 295 | `get_surgery_detail` | `server/routes/surgery_routes.py:110` | `server/routes/surgery_routes.py:162` |
| 308 | `who_checklist_template` | `server/routes/surgery_routes.py:185` | `server/routes/surgery_routes.py:237` |
| 311 | `get_who_checklist` | `server/routes/surgery_routes.py:192` | `server/routes/surgery_routes.py:244` |
| 314 | `set_who_checklist_item` | `server/routes/surgery_routes.py:202` | `server/routes/surgery_routes.py:254` |
| 319 | `alert_surgical_team` | `server/routes/surgery_routes.py:217` | `server/routes/surgery_routes.py:369` |
| 333 | `surgical_team_response` | `server/routes/surgery_routes.py:304` | `server/routes/surgery_routes.py:404` |
| 341 | `start_surgery` | `server/routes/surgery_routes.py:367` | `server/routes/surgery_routes.py:489` |
| 347 | `complete_surgery` | `server/routes/surgery_routes.py:383` | `server/routes/surgery_routes.py:531` |
| 355 | `prep_for_surgery` | `server/routes/surgery_routes.py:425` | `server/routes/surgery_routes.py:606` |
| 358 | `acknowledge_emergency_surgery` | `server/routes/surgery_routes.py:441` | `server/routes/surgery_routes.py:632` |
| 361 | `surgery_risk_assessment` | `server/routes/surgery_routes.py:452` | `server/routes/surgery_routes.py:666` |

**18-command-center.md**, 12 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 224 | `get_house_snapshot` | `server/routes/command_center_routes.py:20` | `server/routes/command_center_routes.py:27` |
| 240 | `_log_ingestion` | `server/services/pulse_service.py:67` | `server/services/pulse_service.py:142` |
| 249 | `_hash_response` | `server/services/pulse_service.py:83` | `server/services/pulse_service.py:158` |
| 255 | `fetch_weather` | `server/services/pulse_service.py:92` | `server/services/pulse_service.py:167` |
| 265 | `_generate_weather_clinical_flags` | `server/services/pulse_service.py:188` | `server/services/pulse_service.py:263` |
| 275 | `fetch_aqi` | `server/services/pulse_service.py:266` | `server/services/pulse_service.py:341` |
| 284 | `fetch_fda_recalls` | `server/services/pulse_service.py:354` | `server/services/pulse_service.py:429` |
| 293 | `fetch_cdc_flu_data` | `server/services/pulse_service.py:407` | `server/services/pulse_service.py:482` |
| 302 | `_classify_ili_level` | `server/services/pulse_service.py:473` | `server/services/pulse_service.py:548` |
| 308 | `_seasonal_flu_estimate` | `server/services/pulse_service.py:486` | `server/services/pulse_service.py:561` |
| 315 | `get_pulse_dashboard` | `server/services/pulse_service.py:511` | `server/services/pulse_service.py:586` |
| 325 | `get_ingestion_audit_log` | `server/services/pulse_service.py:557` | `server/services/pulse_service.py:632` |

**20-revenue-integrity.md**, 6 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 133 | `_term_matches` | `coding_service.py:279` | `server/services/coding_service.py:285` |
| 144 | `_emit_code` | `coding_service.py:288` | `server/services/coding_service.py:294` |
| 158 | `suggest_codes` | `coding_service.py:302` | `server/services/coding_service.py:308` |
| 185 | `_add_suggestion` | `coding_service.py:332` | `server/services/coding_service.py:339` |
| 195 | `_llm_available` | `coding_service.py:482` | `server/services/coding_service.py:498` |
| 204 | `_llm_refine` | `coding_service.py:495` | `server/services/coding_service.py:511` |

**21-admissions-discharge.md**, 4 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 248 | `get_token_record` | `server/services/discharge_access_service.py:58` | `server/services/discharge_access_service.py:68` |
| 251 | `verify_discharge_access` | `server/services/discharge_access_service.py:66` | `server/services/discharge_access_service.py:76` |
| 267 | `get_verified_discharge_summary` | `server/services/discharge_access_service.py:197` | `server/services/discharge_access_service.py:211` |
| 316 | `AdmitPatientModal` | `src/components/AdmitPatientModal.tsx:15` | `src/components/AdmitPatientModal.tsx:56` |

**22-handoff-ipass.md**, 8 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 339 | `Section` | `src/components/handoff/IpassHandoffView.tsx:26` | `src/components/handoff/IpassHandoffView.tsx:36` |
| 348 | `IpassHandoffView` | `src/components/handoff/IpassHandoffView.tsx:65` | `src/components/handoff/IpassHandoffView.tsx:75` |
| 415 | `_dispatch_to_scribe` | `server/services/sage_handoff_service.py:186` | `server/services/sage_handoff_service.py:209` |
| 424 | `_resolve_surgeon_id` | `server/services/sage_handoff_service.py:210` | `server/services/sage_handoff_service.py:233` |
| 433 | `_dispatch_to_transfer` | `server/services/sage_handoff_service.py:241` | `server/services/sage_handoff_service.py:264` |
| 446 | `_alert_assigned_team` | `server/services/sage_handoff_service.py:327` | `server/services/sage_handoff_service.py:373` |
| 455 | `emit_team_alerts_live` | `server/services/sage_handoff_service.py:387` | `server/services/sage_handoff_service.py:457` |
| 464 | `generate_transfer_surgery_package_async` | `server/services/sage_handoff_service.py:433` | `server/services/sage_handoff_service.py:542` |

**23-bridge-patient-portal.md**, 11 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 187 | `_resolve_patient_id` | `bridge_routes.py:12` | `server/routes/bridge_routes.py:70` |
| 198 | `patient_status` | `bridge_routes.py:26` | `server/routes/bridge_routes.py:88` |
| 208 | `patient_vitals` | `bridge_routes.py:37` | `server/routes/bridge_routes.py:99` |
| 216 | `patient_ask` | `bridge_routes.py:47` | `server/routes/bridge_routes.py:109` |
| 231 | `patient_reports` | `bridge_routes.py:80` | `server/routes/bridge_routes.py:143` |
| 245 | `patient_medications` | `bridge_routes.py:131` | `server/routes/bridge_routes.py:194` |
| 253 | `patient_discharge` | `bridge_routes.py:148` | `server/routes/bridge_routes.py:211` |
| 262 | `patient_documents` | `bridge_routes.py:178` | `server/routes/bridge_routes.py:241` |
| 276 | `getWeekDates` | `PatientShell.tsx:20` | `src/pages/PatientShell.tsx:95` |
| 283 | `PatientShell` | `PatientShell.tsx:30` | `src/pages/PatientShell.tsx:105` |
| 315 | `handleBook` | `PatientShell.tsx:84` | `src/pages/PatientShell.tsx:175` |

**24-ingestion-interop.md**, 18 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 191 | `compute_fingerprint` | `ingestion_adapters.py:182` | `server/services/ingestion_adapters.py:197` |
| 199 | `parse_hl7_timestamp` | `ingestion_adapters.py:191` | `server/services/ingestion_adapters.py:206` |
| 206 | `IngestionAdapter` | `ingestion_adapters.py:209` | `server/services/ingestion_adapters.py:231` |
| 213 | `ParseError` | `ingestion_adapters.py:232` | `server/services/ingestion_adapters.py:254` |
| 216 | `HL7Adapter` | `ingestion_adapters.py:241` | `server/services/ingestion_adapters.py:263` |
| 228 | `JSONAdapter` | `ingestion_adapters.py:447` | `server/services/ingestion_adapters.py:461` |
| 238 | `CSVAdapter` | `ingestion_adapters.py:576` | `server/services/ingestion_adapters.py:590` |
| 247 | `FHIRAdapter` | `ingestion_adapters.py:668` | `server/services/ingestion_adapters.py:682` |
| 264 | `ADAPTERS` | `ingestion_adapters.py:1101` | `server/services/ingestion_adapters.py:1115` |
| 282 | `_resolve_patient_id` | `ingestion_service.py:86` | `server/services/ingestion_service.py:96` |
| 290 | `ingest_payload` | `ingestion_service.py:118` | `server/services/ingestion_service.py:353` |
| 300 | `_record_raw_event` | `ingestion_service.py:315` | `server/services/ingestion_service.py:577` |
| 308 | `_update_event_status` | `ingestion_service.py:363` | `server/services/ingestion_service.py:625` |
| 313 | `_route_events` | `ingestion_service.py:389` | `server/services/ingestion_service.py:651` |
| 321 | `_insert_vitals` | `ingestion_service.py:437` | `server/services/ingestion_service.py:710` |
| 331 | `_check_ingested_alert` | `ingestion_service.py:606` | `server/services/ingestion_service.py:843` |
| 339 | `_insert_observation` | `ingestion_service.py:759` | `server/services/ingestion_service.py:998` |
| 442 | `_audit_export` | `fhir_export_routes.py:29` | `server/routes/fhir_export_routes.py:57` |

**25-admin-panel.md**, 8 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 138 | `admin_overview` | `server/routes/admin_routes.py:16` | `server/routes/admin_routes.py:29` |
| 155 | `get_compliance_report` | `server/routes/compliance_routes.py:21` | `server/routes/compliance_routes.py:126` |
| 327 | `_session_revocation_supported` | `server/services/compliance_service.py:213` | `server/services/compliance_service.py:220` |
| 334 | `_build_controls` | `server/services/compliance_service.py:226` | `server/services/compliance_service.py:233` |
| 343 | `_overall_score` | `server/services/compliance_service.py:362` | `server/services/compliance_service.py:441` |
| 351 | `compliance_report` | `server/services/compliance_service.py:374` | `server/services/compliance_service.py:453` |
| 515 | `list_models` | `server/services/model_governance_service.py:113` | `server/services/model_governance_service.py:127` |
| 528 | `AdminPanel` | `src/pages/panels/AdminPanel.tsx:59` | `src/pages/panels/AdminPanel.tsx:82` |

**26-ai-infrastructure.md**, 1 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 233 | `PriorityLLMQueue` | `server/services/llm_queue_service.py:16` | `server/services/llm_queue_service.py:65` |

**28-offline-pwa-downtime.md**, 16 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 131 | `OfflineRequest` | `src/api/client.ts:199` | `src/api/client.ts:329` |
| 136 | `getOfflineQueue` | `src/api/client.ts:207` | `src/api/client.ts:348` |
| 143 | `queueRequest` | `src/api/client.ts:216` | `src/api/client.ts:391` |
| 149 | `processOfflineQueue` | `src/api/client.ts:229` | `src/api/client.ts:465` |
| 161 | `addClinicalNote` | `src/api/client.ts:802` | `src/api/client.ts:1319` |
| 211 | `_now_iso` | `server/services/downtime_service.py:40` | `server/services/downtime_service.py:45` |
| 215 | `_build_summary` | `server/services/downtime_service.py:48` | `server/services/downtime_service.py:53` |
| 222 | `capture_snapshot` | `server/services/downtime_service.py:78` | `server/services/downtime_service.py:83` |
| 243 | `capture_all_admitted` | `server/services/downtime_service.py:244` | `server/services/downtime_service.py:249` |
| 250 | `fhir_self_export_dir` | `server/services/downtime_service.py:273` | `server/services/downtime_service.py:278` |
| 258 | `run_nightly_fhir_self_export` | `server/services/downtime_service.py:290` | `server/services/downtime_service.py:549` |
| 272 | `_envelope` | `server/services/downtime_service.py:348` | `server/services/downtime_service.py:665` |
| 277 | `get_latest_snapshot` | `server/services/downtime_service.py:366` | `server/services/downtime_service.py:683` |
| 283 | `list_snapshots` | `server/services/downtime_service.py:379` | `server/services/downtime_service.py:696` |
| 310 | `history` | `server/routes/downtime_routes.py:83` | `server/routes/downtime_routes.py:88` |
| 363 | `downtime_snapshot_sweep` | `server/main.py:79` | `server/main.py:140` |

**30-harness-and-ci.md**, 2 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 311 | `_reseed_users` | `server/tests/conftest.py:35` | `server/tests/conftest.py:127` |
| 318 | `init_test_db` | `server/tests/conftest.py:61` | `server/tests/conftest.py:153` |

**31-api-reference.md**, 15 stale heading anchors

| Chapter line | Symbol | Cited (wrong) | Correct |
|---|---|---|---|
| 209 | `get_current_user` | `server/auth.py:75` | `server/auth.py:416` |
| 230 | `require_permission` | `server/permissions.py:164` | `server/permissions.py:376` |
| 245 | `require_patient_self` | `server/permissions.py:224` | `server/permissions.py:523` |
| 253 | `require_role` | `server/auth.py:104` | `server/auth.py:493` |
| 261 | `audit_mutations` | `server/services/audit_middleware.py:44` | `server/services/audit_middleware.py:53` |
| 288 | `login` | `auth_routes.py:96` | `server/routes/auth_routes.py:362` |
| 323 | `logout` | `auth_routes.py:340` | `server/routes/auth_routes.py:856` |
| 351 | `get_patient` | `vigil_routes.py:111` | `server/routes/vigil_routes.py:500` |
| 372 | `create_patient` | `vigil_routes.py:495` | `server/routes/vigil_routes.py:856` |
| 404 | `execute_ambient_action` | `relay_routes.py:232` | `server/routes/relay_routes.py:1052` |
| 427 | `confirm_agentic_action` | `relay_routes.py:520` | `server/routes/relay_routes.py:1465` |
| 471 | `sage_query` | `sage_routes.py:48` | `server/routes/sage_routes.py:1506` |
| 523 | `structure` | `scribe_routes.py:418` | `server/routes/scribe_routes.py:423` |
| 541 | `sign_note` | `scribe_routes.py:693` | `server/routes/scribe_routes.py:735` |
| 840 | `serve_upload` | `main.py:375` | `server/main.py:771` |

### 6.3 Reproducing this audit

Save as `/tmp/orb_cite_audit.py` and run with
`/Users/samuelmorningstar/Documents/Projects/Orb/server/.venv/bin/python /tmp/orb_cite_audit.py`.
Takes about 40 seconds. It reads only; it writes nothing into the repo.

```python
import os, re, json, collections
ROOT = "/Users/samuelmorningstar/Documents/Projects/Orb"
GUIDE = os.path.join(ROOT, "docs/guide")

PY_DEF   = re.compile(r'^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)')
PY_CLS   = re.compile(r'^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)')
PY_CONST = re.compile(r'^([A-Za-z_][A-Za-z0-9_]{2,})\s*(?::[^=]+)?=')
TS_DEF   = re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?'
                      r'(?:function|const|let|var|class|interface|type|enum)\s+([A-Za-z_$][A-Za-z0-9_$]*)')
CSS_SEL  = re.compile(r'^\s*([.#][A-Za-z0-9_\-]+)')

_sym = {}
def symbols(path):
    if path in _sym:
        return _sym[path]
    try:
        lines = open(os.path.join(ROOT, path), encoding="utf-8", errors="replace").read().split("\n")
    except Exception:
        _sym[path] = {}
        return {}
    d = {}
    for i, l in enumerate(lines, 1):
        if path.endswith(".py"):
            for rx in (PY_DEF, PY_CLS, PY_CONST):
                m = rx.match(l)
                if m:
                    d.setdefault(m.group(1), []).append(i)
        elif path.endswith((".ts", ".tsx", ".js", ".mjs")):
            m = TS_DEF.match(l)
            if m:
                d.setdefault(m.group(1), []).append(i)
        elif path.endswith(".css"):
            m = CSS_SEL.match(l)
            if m:
                d.setdefault(m.group(1), []).append(i)
    _sym[path] = d
    return d

SKIP = {"node_modules", ".git", ".venv", "__pycache__", "dist", "playwright-report",
        "test-results", "backups", "downtime_exports", "uploads", "_to_delete"}
index = {}
for dp, dn, fn in os.walk(ROOT):
    dn[:] = [d for d in dn if d not in SKIP]
    for f in fn:
        index.setdefault(f, []).append(os.path.join(dp, f))

def resolve(rel):
    for c in (os.path.join(ROOT, rel), os.path.join(ROOT, "server", rel), os.path.join(ROOT, "src", rel)):
        if os.path.isfile(c):
            return os.path.relpath(c, ROOT)
    hits = [h for h in index.get(os.path.basename(rel), []) if h.endswith(rel)]
    return os.path.relpath(hits[0], ROOT) if len(hits) == 1 else None

HEAD = re.compile(r'^(#{2,5})\s+(.+?)\s+[, \-–]\s+`?'
                  r'((?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_.\-]+'
                  r'\.(?:py|ts|tsx|css|json|toml|sh|yml|yaml|js|mjs|html)):(\d+)')
IDENT = re.compile(r'[A-Za-z_$][A-Za-z0-9_$]{2,}')

rows = []
for ch in sorted(os.listdir(GUIDE)):
    if not ch.endswith(".md") or ch.startswith("_"):
        continue
    for i, ln in enumerate(open(os.path.join(GUIDE, ch), encoding="utf-8").read().split("\n"), 1):
        m = HEAD.match(ln.strip())
        if not m:
            continue
        title, rel, lno = m.group(2), m.group(3), int(m.group(4))
        path = resolve(rel)
        if not path:
            rows.append((ch, i, f"{rel}:{lno}", title, None, "FILE_MISSING"))
            continue
        names = IDENT.findall(title)
        name = names[0] if names else None
        locs = symbols(path).get(name or "", [])
        if not name or not locs:
            rows.append((ch, i, f"{rel}:{lno}", name or title, None, "SYMBOL_NOT_FOUND"))
        elif any(abs(L - lno) <= 3 for L in locs):
            rows.append((ch, i, f"{rel}:{lno}", name, locs, "OK"))
        else:
            rows.append((ch, i, f"{rel}:{lno}", name, locs, "STALE"))

print(collections.Counter(r[5] for r in rows))
for ch, i, cite, sym, locs, st in rows:
    if st == "STALE":
        print(f"{ch}:{i}  {cite}  ->  {sym} is at {locs}")
```

To re-derive the route table instead, with `ORB_DEPLOYMENT_PROFILE=dev` set and cwd
`server/`: import `main`, iterate `main.app.routes`, and for each `APIRoute` read
`inspect.getsourcefile(r.endpoint)` / `inspect.getsourcelines(r.endpoint)[1]` for the citation
and `r.dependant.dependencies[*].call.__closure__` for the permission strings that
`require_permission` closed over.
