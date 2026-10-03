## 14. The Frontend (React + TypeScript + Vite)

This section is written for a backend-leaning reader who understands Python and REST but
is new to React. It covers what every file does and, most importantly, how the browser
talks to the FastAPI server, because that is where most cross-stack bugs live.

---

### Key concept glossary (read this first if you are new to React)

**Component**, A TypeScript function that returns JSX (HTML-like markup). React calls it
every time its inputs change and re-paints only the parts of the page that differ. Every
`.tsx` file in `src/` is a component or a collection of components.

**Props**, Arguments passed into a component from its parent, like keyword arguments in
Python. They are read-only inside the component that receives them.

**State**, Data that a component owns and can change itself. Changing state triggers a
re-render. Declared with `useState(initialValue)`, which returns `[value, setter]`.

**Hook**, A function whose name starts with `use`. Hooks let components tap into React
machinery (state, lifecycle, context) without being class-based. You will see three
built-in ones throughout this codebase:
- `useState`, declare a mutable value inside a component.
- `useEffect`, run side-effects (API calls, subscriptions) after render; the return value
  is a cleanup function that runs before the next call or on unmount.
- `useCallback`, cache a function reference so it does not get recreated on every render
  (important when passing callbacks to child components or effect dependency arrays).

**Context**, A React mechanism for sharing data across a component tree without threading
props through every level. Think of it as a module-level singleton that any descendant
component can read. Created with `createContext`, provided by a `<Provider>` wrapper, and
consumed with `useContext`.

**React Router**, A library that maps URL paths to components entirely in the browser
(no server round-trip). The `<Routes>` block in `App.tsx` decides which top-level
component to show for each URL. "Client-side routing" means clicking a nav link does not
reload the page, React swaps out the component instead.

**Shell**, This codebase's term for a top-level layout component that owns the nav
sidebar, header, and the area where panels appear. There are two: `ClinicianShell` for
staff and `PatientShell` for patients.

**Panel**, A module's full-screen content area, rendered inside the shell. Every clinical
module (Vigil, Scribe, Sage, …) maps to exactly one panel file under `src/pages/panels/`.

**Vite**, The build tool and development server. In dev mode (`npm run dev`) it serves
files over `http://localhost:5173` with hot-module replacement (changes appear instantly
without a full reload). In production (`npm run build`) it bundles everything into
`dist/`. The Vite proxy config (see below) forwards `/api`, `/socket.io`, and `/uploads`
to the FastAPI server on port 8000 so the browser sees a single origin.

**CSS Modules**, Some component folders use `*.module.css` files. Importing
`styles from './Helix.module.css'` gives an object where `styles.container` resolves to
a globally-unique class name, preventing CSS collisions between modules. Files without
`.module.css` use plain global CSS.

**WebSocket streaming hook**, `useStreamingTranscription` captures microphone audio,
runs a voice-activity detector (VAD), and sends raw PCM chunks to the server over
Socket.IO, receiving transcript events in return. See the dedicated section below.

---

### 14.1 Entry and routing

```
index.html          <- HTML shell; <div id="root"> is where React mounts
src/main.tsx        <- createRoot() + RootErrorBoundary wrapper
src/App.tsx         <- AuthContext + BrowserRouter + all top-level routes
```

**`src/main.tsx`**

The browser's true entry point. It:
1. Wraps everything in `<StrictMode>` (runs effects twice in dev to catch bugs).
2. Wraps everything in `RootErrorBoundary`, a class component that catches any
   uncaught render-time exception and shows a "Reload" affordance instead of a blank
   screen. This is a patient-safety measure: a malformed vitals payload should never
   leave a clinician staring at nothing.
3. Calls `createRoot(document.getElementById('root')).render(<App />)`.

**`src/App.tsx`**

Defines and exports two things that the rest of the app imports:
- `AuthContext` / `useAuth()`, the global auth state (user object + login/logout
  functions). Any component can call `useAuth()` to get the logged-in user without
  prop-drilling.
- The route table inside `<BrowserRouter>`:

| URL pattern | Condition | Renders | `src/App.tsx` |
|---|---|---|---|
| `/` | logged in as patient | redirect `/bridge` | `:187` |
| `/` | logged in as staff | redirect `/dashboard` | `:187` |
| `/` | not logged in | redirect `/login/staff` | `:187` |
| `/login/:mode` | not logged in | `<Login>` | `:188` |
| `/dashboard/*` | staff logged in | `<ClinicianShell>` | `:189` |
| `/bridge/*` | patient logged in | `<PatientShell>` | `:190` |
| `/discharge/:token` | unauthenticated | `<DischargeAccess>` | `:191` |
| **`/downtime/:patientId`** | **staff logged in** | **`<DowntimeMode>`** | **`:192`** |
| **`/handoff/:patientId`** | **staff logged in** | **`<HandoffPage>`** | **`:193`** |
| `*` | anything else | redirect `/` | `:194` |

> **Correction (2026-08-15).** The last two rows are new to this manual. `/downtime/:patientId` is the paper-fallback surface (`src/pages/DowntimeMode.tsx`) and `/handoff/:patientId` is the I-PASS handover page (`src/pages/HandoffPage.tsx`); both are staff-only and redirect to `/login/staff` otherwise.

The `/*` wildcard on `/dashboard` and `/bridge` lets the shells declare their own
sub-routes in the future without changing `App.tsx`.

Theme initialization (`data-theme` attribute on `<html>`) also runs here, before the
first render, to avoid a flash of the wrong theme.

**`src/index.html`**

Standard Vite HTML template. Nothing custom. The `<script type="module" src="/src/main.tsx">`
tag is Vite's entry point.

---

### 14.2 The API client (`src/api/client.ts`)

**This is the single most important file for a backend developer.** Every REST call and
every Socket.IO connection originates here. If you add a new backend route, you add its
client function here. If auth breaks, look here first.

#### Base URL and the Vite proxy

```typescript
const API_HOST = window.location.origin;
const API_BASE = `${API_HOST}/api`;
```

`window.location.origin` is `http://localhost:5173` in dev and the real hostname in
production. All REST calls go to `{origin}/api/...`.

In **dev mode**, `vite.config.ts` proxies `/api` → `http://localhost:8000/api`, so the
browser never sees a cross-origin request and CORS is not needed. In **production**, Nginx
(or the same server) must provide an equivalent reverse proxy.

⚠️ **If you change the backend port or path prefix:** update the proxy targets in
`vite.config.ts`. Do not hardcode `localhost:8000` anywhere else, if you do, it will
break LAN and ngrok deployments.

#### JWT token storage and auth headers

```typescript
function getToken(): string | null {
  return localStorage.getItem('orb_token');
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}
```

> ### ⚠️ Corrected 2026-08-15, the SPA no longer authenticates with a `localStorage` token.
>
> The paragraphs that followed said the JWT lives in `localStorage` under `orb_token` and that HttpOnly cookies were "tracked as a deferred task because it requires backend CSRF work". **That deferred task shipped.** Authentication is now primarily the **HttpOnly `orb_session` cookie**, sent automatically with `credentials: 'include'` (`src/api/client.ts:17-21`, `:156-157`, and `credentials: 'include'` on every `fetch` in the file, e.g. `:225`, `:244-247`, `:1003-1005`). The backend CSRF work landed with it: double-submit `orb_csrf` / `x-csrf-token`, enforced inside `auth.get_current_user` (`server/auth.py:15-29`; §1).
>
> **The practical consequence:** the token is no longer readable by JavaScript, so an XSS or a compromised npm dependency can no longer exfiltrate a clinician's persistent session. New code **must rely on the cookie plus `credentials: 'include'`, never on a stored token** (`src/api/client.ts:19-21`).
>
> A bearer token remains supported server-side for machine clients and the test suite (§1), which is why the helper above still exists, but it is not how the browser authenticates.

The user object is still stored in `localStorage` under `orb_user` (non-secret UI state). Do not add `localStorage.getItem` calls for the *token* anywhere.

#### The `apiFetch` wrapper

```typescript
export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T>
```

Every call that returns JSON goes through this function. It:
1. Prepends `API_BASE` to `path`.
2. Merges `authHeaders()` with any caller-provided headers.
3. On **401**: clears the token and reloads the page (session expired).
4. On **403**: throws a structured error with `.status = 403` and `.permissions` so the
   UI can show a role-based message rather than a generic error.
5. On other errors: throws with the `detail` field from the FastAPI error response.
6. On success: parses and returns the JSON body.

⚠️ **If you add a new backend endpoint:** add a named async function in the appropriate
section of this file that calls `apiFetch('/your/path', ...)`. Do not call `fetch()`
directly from panel files, two panels already do this for legacy reasons
(`ForecastPanel.tsx`, `AppointmentsPanel.tsx`) and it means they bypass the 401/403
handling.

#### Streaming responses (SSE over fetch)

Several endpoints stream tokens back using Server-Sent Events (`text/event-stream`).
The pattern is the same every time:

```typescript
const response = await fetch(`${API_BASE}/sage/query`, { method: 'POST', ... });
const reader = response.body?.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  const events = buffer.split('\n\n');
  buffer = events.pop() || '';   // keep incomplete event in buffer
  for (const event of events) {
    const data = JSON.parse(event.replace(/^data: /, ''));
    onChunk?.(data.content);     // call the caller's progress callback
  }
}
```

Functions that use streaming: `querySage`, `querySageVoice`, `structureNote`,
`askBridge`, `getPatientAnalysis`, `getSurgeryRiskAssessment`, `analyzeLensImage`.

All streaming functions accept an optional `onChunk: (partialText: string) => void`
callback so the panel can update the UI token-by-token.

#### Socket.IO connection

```typescript
export const socket = io(API_HOST, {
  autoConnect: true,
  auth: (cb) => cb({ token: getToken() }),
});
```

A single Socket.IO client is created at module load and exported as `socket`. Because
`auth` is a callback (not a static object), the current token is read on every connection
attempt, including reconnects after session restoration.

After login, `authenticateSocket()` disconnects and reconnects with the freshly stored
token.

⚠️ **Socket.IO event names are a hard contract with the backend.** If you rename an event
on either side, the other side goes silent without an error. All event names used in the
frontend are:

| Direction | Event name | Used by |
|---|---|---|
| server → client | `vitals:update` | `VigilPanel.tsx` |
| server → client | `scribe:transcript` | `useStreamingTranscription.ts` |
| server → client | `scribe:status` | `useStreamingTranscription.ts` |
| server → client | `scribe:error` | `useStreamingTranscription.ts` |
| server → client | `new_message` | `RelayPanel.tsx` |
| server → client | `surgery:update` | `SurgicalSuitePanel.tsx` |
| client → server | `scribe:start_session` | `useStreamingTranscription.ts` |
| client → server | `scribe:audio_chunk` | `useStreamingTranscription.ts` |
| client → server | `scribe:end_utterance` | `useStreamingTranscription.ts` |
| client → server | `scribe:stop_session` | `useStreamingTranscription.ts` |
| client → server | `join_channel` | `RelayPanel.tsx` |
| client → server | `leave_channel` | `RelayPanel.tsx` |
| client → server | `new_message` | `RelayPanel.tsx` |

#### Offline queue

When `navigator.onLine` is false, `addClinicalNote()` and `addMedication()` enqueue the
request in `localStorage` under `orb_offline_queue` and return an optimistic response.
When the browser comes back online (`window.addEventListener('online', ...)`) the queue is
replayed via `processOfflineQueue()`, which removes each item only after the server
confirms success.

#### Named API functions by module

All functions exported from `client.ts` are organized in comment-delimited sections.
Here is a quick reference:

**Auth:** `login`, `setToken`, `clearToken`, `setUser`, `getUser`, `authenticateSocket`

**Vigil:** `getPatients`, `getPatientDetail`, `getPatientVitals`, `getPatientForecast`,
`admitPatient`, `getPatientNotes`, `addClinicalNote`, `addMedication`,
`getPatientAnalysis`, `dischargePatient`, `getDischargeSummary`, `getDischargedPatients`

**Relay:** `getChannels`, `getAlerts`, `resolveAlert`, `getMessages`, `getSageMemory`,
`getSageActions`, `transitionSageAction`, `sendMessage`

**Sage:** `querySage`, `getSageHistory`, `clearSageHistory`, `querySageVoice`

**Scribe:** `transcribeAudio`, `transcribeChunk`, `structureNote`, `getScribeConfig`,
`getScribeNotes`, `getScribeNoteVerification`, `retryScribeNoteVerification`,
`editScribeNoteDraft`, `signScribeNote`, `rejectScribeNote`, `reviseScribeNote`

**Bridge (patient):** `getBridgeStatus`, `getBridgeReports`, `getBridgeMedications`,
`getBridgeDischarge`, `getBridgeDocuments`, `askBridge`

**Lens:** `analyzeLensImage`, `getLensAnalyses`, `assignLensToPatient`, `getPatientsList`

**Surgery:** `getSurgerySchedule`, `getSurgeryDetail`, `getSurgeryRiskAssessment`,
`prepForSurgery`, `getEmergencyQueue`, `getSurgeryCases`, `alertSurgicalTeam`,
`surgicalTeamResponse`, `startSurgery`, `completeSurgery`

**Helix:** `approveHelixOrder`, `getHelixOrders`, `transitionHelixOrder`,
`getHelixStockMovements`, `getHelixOrderTransitions`, `apiFetch` (also re-exported for
panels that need generic calls, e.g. `/helix/inventory`, `/helix/catalog`)

**Admin/Audit:** `getAdminOverview`, `getAuditLogs`, `getIngestionStatus`

**Discharge access (unauthenticated):** `verifyDischargeAccess`, `getVerifiedDischargeSummary`

**Demo/testing:** `triggerDeterioration`, `getEscalationStatus`, `createPatient`

---

### 14.3 Shells

Shells are the top-level layout components. They own the nav sidebar, header widgets, and
an area where panels appear. A user sees exactly one shell at a time based on their role.

#### `src/pages/ClinicianShell.tsx`

The shell for doctors, nurses, admins, and pharmacists. Key behaviour:

**Tab-based navigation (not URL-based):** The shell maintains an `activeTab` state value.
Switching tabs does not change the URL, it just hides/shows the relevant panel `<div>`.
All panels are rendered in hidden divs from the moment the shell mounts (they all receive
their `useEffect` calls immediately). This means background panels stay up to date with
Socket.IO events even when not visible.

**Role-based tab filtering:** `getAccessibleTabs(role, department)` returns the allowed
tab IDs for each role.

> **Rewritten 2026-08-15, `src/shellConfig.tsx` is now the seam, and the old table below was wrong on every row.** The shell used to answer "what does this role see?" in four different places: a switch returning a tab array, the implicit rule that the landing tab is `accessibleTabs[0]`, an inline `role !== 'pharmacist'` gate on the primary CTA, and the master `TABS` list's ordering. *"Four sources of truth about one question is how the admit-CTA defect was born, the CTA's role list drifted from the server's permission map and every nurse and administrator got a button that existed only to 403"* (`src/shellConfig.tsx:6-16`).

#### `src/shellConfig.tsx`: the shell's one role input

A role's experience is one declarative record: **`home` + ordered, grouped `tabs` + a `primaryAction`** (`ShellRoleConfig`, `:119`; `ROLE_CONFIG`, `:826-881`). `getAccessibleTabs` is **derived** from it rather than written twice (`:984-990`).

> **The review rule that keeps it that way:** `user.role` may not appear in `ClinicianShell`'s JSX outside a lookup through this file (`src/shellConfig.tsx:17-20`).

What this file is deliberately **not**: a panel registry. Panels stay mounted by name in the shell because they take different props, `VigilPanel` needs the role, `SagePanel` the user name, and hiding that behind a generic renderer buys nothing but indirection (`:21-26`). The one exception is the doctor home, which has to be resolvable before its file exists (`:145-152`).

**Roles, homes and tabs** (verified against `src/shellConfig.tsx:826-887`):

| Role | Home | Tabs | Primary action |
|---|---|---|---|
| `doctor` | **`today`** | `DOCTOR_TABS` (today, vigil, round, orders, relay, sage, scribe, …) + appointments + dietary | `admit` |
| `clinician` | **`today`** | same as `doctor`, aliased, `:838-842` | `admit` |
| `surgeon` | `vigil` | vigil, round, relay, sage, scribe, lens, forecast, surgical-suite | none |
| `nurse` | **`shift`** | shift, vigil, orders, relay, sage, scribe, lens, forecast, coding, appointments, dietary | `admit` |
| `admin` | `vigil` | vigil, relay, sage, forecast, command, capacity, coding, **all eleven hospital modules**, dietary, appointments, admin | `admit` |
| `pharmacist` | `helix` | helix, pharmacy-verify, sage | none |
| unknown/absent | `vigil` | vigil, relay, *"the smallest honest surface, never a blank shell"* (`:884-890`) | `admit` |

**The two new home screens.** `today` (**"Today"**, `:356`) is the doctor home, `src/pages/panels/DoctorHomePanel.tsx`; `shift` (**"My Shift"**, `:366`) is the nurse home, `src/pages/panels/nurse/`. *"'shift' leads for nurses and is their home: a nurse coming on shift needs the task list, not the patient roster"* (`:856-857`).

`surgeon` is a safety net for a raw backend role that `Login` normally normalizes to *doctor + department Surgery*; a direct/refresh path can still surface it. It keeps the pre-existing reduced set, **no `today`**, because the home is registered for the normalized doctor role, and **no admit CTA**, because the server has no `surgeon` key and would refuse the write (`:845-848`).

#### Two filters every tab passes

`getRoleConfig` (`:954-975`):

1. **`today` survives only if `DoctorHomePanel.tsx` is in the build**, resolved through `import.meta.glob` so the tab can be declared before the file exists (`:145-152`, `:967`). Absent the panel, doctors land on `vigil`.
2. **A hospital-module tab survives only if its flag is on AND its panel is in the build** (`:970-972`). Either half missing means the tab is not there at all, *never a tab that opens onto nothing, and never a tab for a module this deployment switched off.*

`home` then falls back to the first surviving tab, never to a hard-coded id (`:974`).

#### `CLINICAL_MODULE_IDS`: the one hospital module a nurse can write to

`src/shellConfig.tsx:812-824`. **Dietary alone** is clinical: `POST /api/dietary/orders` requires `order.write`, which only doctors and nurses hold. Billing, payments, payer, procurement, front desk, housekeeping and workforce do not and must not, they are `MONEY_MODULE_IDS`, admin-only. *"A nurse who cannot see who is nil by mouth is a nurse who hands somebody breakfast before theatre"* (`:69-75`).

#### Per-deployment overrides

`ShellOverrides` / `readDeploymentOverrides()` (`:892-935`). A site may move a role's landing tab, two ways:

```js
window.__ORB_SHELL__ = { home: { doctor: 'vigil' } }   // index.html / ops
VITE_ORB_HOME_DOCTOR=vigil                             // build-time
```

The reason this seam exists is worth quoting, because it is a schedule constraint rather than a technical one: the doctor home defaults ON during the shadow phase because clinicians are not in yet and the feedback is free, but *"training materials are written at Weeks 9–10 and **training must never describe a screen that moved last week**"*, so a site that has already trained its doctors on the roster must be able to put them back without a code change (`:893-908`).

**An override naming a tab the role cannot reach is ignored, not obeyed** (`getLandingTab`, `:997-1006`), a config typo must not land a doctor on a tab they have no permission for, or on a blank screen. `window.__ORB_MODULES__` (`:916-919`) does the same for module switches, patched over the server's answer from `/api/health`.

#### `getPrimaryAction`: permission, never a role list

`:1017-1027`. Two conditions, and **the permission is the one that matters**: the experience has to place the action, *and* the account has to hold the permission the request requires (`PRIMARY_ACTION_PERMISSION`). Never a role list, that is the defect this file was built to end.

**Lazy loading with `React.lazy`:** Each panel is loaded via `lazy(() => import(...))`.
The panel's JS is only downloaded from the server the first time it is needed. All panels
are wrapped in `<Suspense fallback={...}>` for the loading state, and each gets its own
`PanelErrorBoundary` so a panel crash does not bring down the whole shell.

**`ActivePatientProvider`:** The entire shell is wrapped in this context provider so any
panel (or the floating Sage assistant) can read/set the currently-selected patient without
passing props.

**Offline banner:** Monitors `navigator.onLine` and shows an "Offline Mode" badge when the
network is unavailable.

**`SageFloatingAssistant`:** Rendered outside the panel area, always visible regardless of
active tab. Listens for a hotword and triggers a voice query to the Sage API.

**`AdmitPatientModal`:** The "Admit Patient" button in the sidebar opens this modal; on
success it reloads the page to refresh all panels.

#### `src/pages/PatientShell.tsx`

A simpler, self-contained layout for the `/bridge` route. It does not use tabs or lazy
panels. Instead it renders everything in one scrollable page:
- A welcome header with the patient's name and status badge (from `getBridgeStatus()`).
- A vitals card.
- An appointment scheduler (fully client-side state; does not POST to the backend yet).
- A "My Records" section with four modal drawers (Medications, Report History, Labs,
  Discharge Summary) populated from `getBridgeReports()`.
- A freetext Q&A input that calls `askBridge()` and streams the answer.

Status is polled every 30 seconds via `setInterval`.

#### `src/pages/DischargeAccess.tsx`

A standalone page at `/discharge/:token` that requires **no login**. It verifies the
patient's date of birth against the token (`verifyDischargeAccess`), then fetches and
renders the discharge summary (`getVerifiedDischargeSummary`). Used for post-discharge
access by the patient from a link in an SMS or email.

---

### 14.4 Panels and module map

Each row in this table lets you jump from a frontend panel to the backend Python router.

> **Corrected 2026-08-15.** Every backend filename in this table was wrong, the table said `server/routes/vigil.py`, `sage.py`, `helix.py` and so on, when the files have always been `*_routes.py`. The old caption ("confirm with the actual file tree") was the manual admitting it had not verified its own claim, which is exactly what this document is not allowed to do. The paths below are the real ones.
>
> **The table is also incomplete.** It covers eleven panels; `src/pages/panels/` holds twenty-odd, including the eleven hospital-module panels (§16), `DoctorHomePanel.tsx`, `RoundPanel.tsx` and the `nurse/` and `admin/` sub-trees. See §14.9 and §16.

| Module | Tab label | Panel file | Backend route file | Key API calls |
|---|---|---|---|---|
| Vigil | Patients | `src/pages/panels/VigilPanel.tsx` | `server/routes/vigil_routes.py` | `getPatients`, `getPatientDetail`, `getPatientVitals`, `getPatientAnalysis`, `addClinicalNote`, `addMedication`, `dischargePatient` |
| Relay | Messages | `src/pages/panels/RelayPanel.tsx` | `server/routes/relay_routes.py` | `getChannels`, `getMessages`, `sendMessage`, `getAlerts`, `resolveAlert`, Socket.IO `new_message` |
| Sage | Sage | `src/pages/panels/SagePanel.tsx` | `server/routes/sage_routes.py` | `querySage` (streaming SSE), `querySageVoice` (streaming SSE), `getSageHistory`, `clearSageHistory` |
| Scribe | Scribe | `src/pages/panels/ScribePanel.tsx` | `server/routes/scribe_routes.py` | `transcribeAudio`, `transcribeChunk`, `structureNote` (streaming), `signScribeNote`, `rejectScribeNote`, Socket.IO scribe events |
| Lens | Lens | `src/pages/panels/LensPanel.tsx` | `server/routes/lens_routes.py` | `analyzeLensImage` (streaming), `getLensAnalyses`, `assignLensToPatient` |
| Forecast | Forecast | `src/pages/panels/ForecastPanel.tsx` | `server/routes/forecast_routes.py` | `/forecast/hospital`, `/forecast/weekly` (direct `fetch`, bypasses `apiFetch`) |
| Appointments | Appointments | `src/pages/panels/AppointmentsPanel.tsx` | `server/routes/appointments_routes.py` | `/appointments/upcoming` (direct `fetch`, bypasses `apiFetch`) |
| Surgical Suite | Surgical Suite | `src/pages/panels/SurgicalSuitePanel.tsx` | `server/routes/surgery_routes.py` | `getSurgerySchedule`, `getSurgeryCases`, `getSurgeryRiskAssessment` (streaming), `alertSurgicalTeam`, `startSurgery`, `completeSurgery`, Socket.IO `surgery:update` |
| Admin | Admin | `src/pages/panels/AdminPanel.tsx` | `server/routes/admin_routes.py`, `server/routes/audit_routes.py`, `server/routes/ingestion_routes.py` | `getAdminOverview`, `getAuditLogs`, `getIngestionStatus` |
| Helix | Pharmacy | `src/pages/panels/HelixPanel.tsx` | `server/routes/helix_routes.py` | `getHelixOrders`, `transitionHelixOrder`, `/helix/inventory`, `/helix/catalog` |
| Bridge | (PatientShell) | `src/pages/PatientShell.tsx` | `server/routes/bridge_routes.py` | `getBridgeStatus`, `getBridgeReports`, `askBridge` (streaming) |

⚠️ `ForecastPanel.tsx` and `AppointmentsPanel.tsx` contain their own local `apiFetch`
helper that reads `orb_token` directly from localStorage. They bypass the global 401/403
handler. If those panels need RBAC error handling, migrate them to use the exported
`apiFetch` from `src/api/client.ts`.

#### AdminPanel sub-tabs

`AdminPanel.tsx` is a router component that delegates to six sub-components based on an
internal `activeTab` state:

| Sub-tab | Component | Backend call |
|---|---|---|
| Overview | `admin/AdminOverview.tsx` | `getAdminOverview()` → `GET /admin/overview` |
| Audit Logs | `admin/AuditLogTable.tsx` | `getAuditLogs()` → `GET /audit/logs` |
| Trace | `admin/CorrelationTrace.tsx` | `getAuditLogs({ correlation_id })` |
| Ingestion | `admin/IngestionMonitor.tsx` | `getIngestionStatus()` → `GET /ingest/status` |
| Security | `admin/SecurityActivity.tsx` | `getAuditLogs({ event_category: 'security' })` |
| Alerts | `admin/AlertOperations.tsx` | `getAlerts()`, `resolveAlert()` |

---

### 14.5 Hooks and contexts

#### `src/hooks/useStreamingTranscription.ts`

The most complex custom hook in the codebase. Call it from any component that needs
live microphone transcription. It:

1. On `startStreaming()`:
   - Requests microphone access (`getUserMedia` at 16 kHz mono).
   - Creates an `AudioContext` and a `ScriptProcessorNode` (deprecated but not yet
     replaced, see TODO comment in the file for the AudioWorklet migration plan).
   - Emits `scribe:start_session` to the Socket.IO server with optional `patient_id` and
     `hints`.
   - In the audio processing callback, calculates RMS energy for a simple energy-based
     VAD (voice activity detector):
     - Above `vadThreshold` for `minSpeechMs` → mark as speaking, buffer audio.
     - Below threshold for `silenceMs` → end utterance, emit `scribe:end_utterance`.
   - Converts `Float32` PCM to `Int16` and emits raw bytes as `scribe:audio_chunk`.

2. Listens for Socket.IO events from the server:
   - `scribe:transcript` → appends to `fullTranscript`, calls `onFinalTranscript` callback.
   - `scribe:status` → updates `isProcessing` state.
   - `scribe:error` → calls `onError` callback.

3. On `stopStreaming()`: disconnects audio nodes, stops mic tracks, emits
   `scribe:stop_session`.

Returns an object with: `{ isStreaming, isProcessing, isSpeaking, currentTranscript,
fullTranscript, utteranceCount, audioLevel, startStreaming, stopStreaming }`.

Used by: `ScribePanel.tsx` (streaming mode toggle), `SageFloatingAssistant.tsx` (implicit
via the hotword hook pathway).

⚠️ **If you rename Socket.IO events on the server:** update the `socket.on(...)` and
`socket.emit(...)` calls in this file too. There is no type-safety across the wire.

#### `src/hooks/useSageHotword.ts`

A lightweight hook that persists a boolean `isEnabled` flag to `localStorage` under
`orb_sage_hotword`. Returns `{ isEnabled, toggleEnabled }`. Used by
`SageFloatingAssistant.tsx` to gate the hotword listener.

#### `src/contexts/ActivePatientContext.tsx`

Provides `{ activePatientId, activePatientName, setActivePatient }` to the entire
`ClinicianShell` tree. When a clinician selects a patient in `VigilPanel`, it calls
`setActivePatient(id, name)`. `SageFloatingAssistant` reads the active patient and
automatically scopes voice queries to that patient.

Pattern for consuming it in a panel:
```typescript
import { useActivePatient } from '../../contexts/ActivePatientContext'
const { activePatientId, setActivePatient } = useActivePatient()
```

#### `src/App.tsx`: `AuthContext` / `useAuth`

Provides `{ user, login, logout }` to the entire app tree. `user` is the logged-in
`User` object (id, name, role, department) or `null`. Calling `logout()` clears the token
and sets `user` to null, which causes the router to redirect to `/login/staff`.

---

### 14.6 Shared components

These live under `src/components/` and are reused across shells and panels.

#### Top-level (`src/components/`)

| File | Role |
|---|---|
| `Aurora.tsx` | Animated canvas background shown on the Login page. |
| `OrbLogo.tsx` + `OrbLogo.css` | The Orb animated logo SVG. Accepts a `size` prop. Used in the sidebar, login card, and patient shell header. |
| `PulseWidget.tsx` + `PulseWidget.css` | Real-time "system pulse" status chip in the clinician header. Polls backend health. |
| `PulseDetail.tsx` | Expanded detail panel for PulseWidget. |
| `ClockWidget.tsx` + `ClockWidget.css` | Live digital clock displayed in the clinician header. |
| `AdmitPatientModal.tsx` + `AdmitPatientModal.css` | Modal form for admitting a new patient. Calls `admitPatient()`. Opened by the "Admit Patient" button in ClinicianShell. |
| `HandoffPrintView.tsx` | Print-optimized layout for a handoff report. Rendered inside VigilPanel and opened via `createPortal`. |
| `DischargePrintView.tsx` | Print-optimized layout for a discharge summary. Used by VigilPanel and DischargeAccess. |

#### `src/components/sage/`

| File | Role |
|---|---|
| `SageFloatingAssistant.tsx` + `SageFloatingAssistant.css` | Always-visible floating orb that listens for a hotword and runs a voice query. Uses `useActivePatient` to scope queries. |
| `SageComposer.tsx` | The text input + submit button at the bottom of SagePanel. |
| `SageMessage.tsx` | Renders a single Sage chat message, handling markdown display. |
| `SageHistorySidebar.tsx` | Left sidebar in SagePanel listing past conversations. |
| `SageEmptyState.tsx` | Placeholder shown when no conversation is active. |
| `Sage.css` | Shared CSS for all Sage components. |

#### `src/components/helix/`

| File | Role |
|---|---|
| `HelixDashboard.tsx` | Summary view: order queue stats, stock alerts. |
| `HelixPatientBoard.tsx` | Per-patient medication list with order status. |
| `HelixFormulary.tsx` | Formulary/catalog browser. |
| `HelixOrderReviewModal.tsx` | Modal for pharmacist to approve/reject/substitute an order. Calls `transitionHelixOrder`. |
| `Helix.module.css` | CSS Module for Helix layout. |

#### `src/components/relay/`

| File | Role |
|---|---|
| `AlertsSection.tsx` | Renders the unresolved alert list at the top of RelayPanel. |
| `CaseList.tsx` | The left-column channel/case list in RelayPanel. |
| `RelaySearch.tsx` | Search bar for filtering channels. |
| `SageAmbientSidebar.tsx` + `SageAmbientSidebar.module.css` | Right sidebar in an open channel showing Sage's ambient summary, active intents, and risk flags derived from the conversation. Calls `getSageMemory` and `getSageActions`. |
| `RelayComponents.css` | Shared CSS for relay sub-components. |

#### `src/components/surgery/`

| File | Role |
|---|---|
| `EmergencyIntakePopup.tsx` + `.module.css` | Modal that appears when a new emergency surgery case arrives via Socket.IO. |
| `SurgeryActionCard.tsx` + `.module.css` | Card component for a single surgery case showing status and action buttons. |
| `SurgeryReviewSheet.tsx` + `.module.css` | Detail sheet for a selected surgery, including the streaming AI risk assessment. |

---

### 14.7 Styling approach

The app uses a hybrid of three techniques:

**1. Global CSS variables + theme tokens** (`src/index.css`, `src/App.css`)

CSS custom properties (`--var-name`) define the color palette, spacing, and typography.
The `data-theme` attribute on `<html>` switches between dark and light token sets. All
component styles should reference these variables rather than hardcoding hex values, so
theme switching works automatically.

**2. Plain CSS files per component** (`*.css` colocated with `.tsx`)

Most files have a sibling `.css` that is imported directly. Class names are global, so
naming follows a loose BEM convention: `.panel__header`, `.shell__nav-item`, etc. to
avoid collisions.

**3. CSS Modules** (`*.module.css`)

Used in the `helix/`, `relay/` (SageAmbientSidebar), and `surgery/` component folders.
Importing a module file gives a locally-scoped class name object:
```typescript
import styles from './Helix.module.css'
<div className={styles.container}>
```
CSS Modules are the more collision-proof approach; new components should prefer them.

---

### 14.8 Vite configuration (`vite.config.ts`)

```typescript
server: {
  host: '0.0.0.0',       // LAN accessible
  allowedHosts: true,
  proxy: {
    '/api':      { target: 'http://localhost:8000', changeOrigin: true },
    '/uploads':  { target: 'http://localhost:8000', changeOrigin: true },
    '/socket.io':{ target: 'http://localhost:8000', ws: true, changeOrigin: true },
  },
}
```

The proxy is **dev-only**. In production, requests go directly to whatever origin serves
the built `dist/` folder. If that origin differs from where FastAPI runs, you need a
reverse proxy (Nginx, Caddy, etc.) replicating these routes.

The `VitePWA` plugin generates a service worker (`dev-dist/sw.js` in dev, `dist/sw.js`
in prod) that caches static assets for offline use. The `registerType: 'autoUpdate'`
mode silently updates the cache when a new build is deployed.

---

### 14.9 File inventory

A complete structured listing of every file under `src/`, with a one-line role.

```
src/
├── main.tsx                          App entry point; mounts React; root error boundary
├── App.tsx                           AuthContext; BrowserRouter; top-level route table
├── index.css                         Global CSS variables, resets, theme tokens
├── App.css                           Root-level animation helpers and glass-card utilities
├── constants.ts                      Static lists (DOCTORS array)
│
├── api/
│   └── client.ts                     ALL backend calls + JWT handling + Socket.IO instance
│
├── assets/
│   ├── hero.png                      Hero image (used in marketing references, unused in app)
│   ├── react.svg                     Default Vite asset (unused)
│   └── vite.svg                      Default Vite asset (unused)
│
├── contexts/
│   └── ActivePatientContext.tsx      Cross-panel "which patient is selected" state
│
├── hooks/
│   ├── useSageHotword.ts             Persists hotword-enabled flag to localStorage
│   └── useStreamingTranscription.ts  Mic capture → VAD → PCM → Socket.IO → transcript
│
├── pages/
│   ├── Login.tsx                     Clinician + patient login form; calls api/client login()
│   ├── Login.css
│   ├── ClinicianShell.tsx            Staff layout: sidebar nav, header widgets, lazy panels
│   ├── ClinicianShell.css
│   ├── PatientShell.tsx              Patient layout: status, records, ask-bridge Q&A
│   ├── PatientShell.css
│   ├── DischargeAccess.tsx           Token-based unauthenticated discharge summary page
│   ├── DischargeAccess.css
│   ├── Relay.css                     CSS for RelayPanel (imported by RelayPanel.tsx)
│   ├── Vigil.css                     CSS for VigilPanel
│   ├── SurgicalSuite.css             CSS for SurgicalSuitePanel
│   │
│   └── panels/
│       ├── VigilPanel.tsx            Patient monitoring list + detail drill-down
│       ├── RelayPanel.tsx            Channel messaging + alert inbox + Sage ambient sidebar
│       ├── SagePanel.tsx             Chat interface for the Sage AI assistant
│       ├── ScribePanel.tsx           Voice recording + AI note structuring + note lifecycle
│       ├── ScribePanel.css
│       ├── LensPanel.tsx             Image upload + AI analysis + patient assignment
│       ├── LensPanel.css
│       ├── ForecastPanel.tsx         Hospital occupancy + 7-day flow forecast charts
│       ├── AppointmentsPanel.tsx     Upcoming scheduled follow-up list
│       ├── SurgicalSuitePanel.tsx    Surgery schedule drill-down + emergency queue
│       ├── SurgicalSuitePanel.css
│       ├── HelixPanel.tsx            Pharmacy order queue + inventory + formulary
│       ├── AdminPanel.tsx            Admin router: delegates to admin/ sub-components
│       │
│       └── admin/
│           ├── AdminOverview.tsx     System health summary cards
│           ├── AdminBadges.tsx       Status badge components reused in AdminOverview
│           ├── AdminEmptyState.tsx   Empty-state placeholder for admin sub-tabs
│           ├── AuditLogTable.tsx     Paginated, filterable audit log table
│           ├── CorrelationTrace.tsx  Trace view: all events for a single correlation ID
│           ├── IngestionMonitor.tsx  Data ingestion job status list
│           ├── SecurityActivity.tsx  Security-category audit events filtered view
│           └── AlertOperations.tsx   Unresolved alert list with confirm/escalate actions
│
└── components/
    ├── Aurora.tsx                    Animated login background canvas
    ├── OrbLogo.tsx + OrbLogo.css     Orb logo SVG with size prop
    ├── PulseWidget.tsx + .css        System health pulse chip in clinician header
    ├── PulseDetail.tsx               Expanded pulse detail view
    ├── ClockWidget.tsx + .css        Live clock in clinician header
    ├── AdmitPatientModal.tsx + .css  Modal form: admit a new patient
    ├── HandoffPrintView.tsx          Print layout for handoff report (portal-rendered)
    ├── DischargePrintView.tsx        Print layout for discharge summary
    │
    ├── sage/
    │   ├── SageFloatingAssistant.tsx + .css  Always-visible hotword voice assistant
    │   ├── SageComposer.tsx          Text input bar in SagePanel
    │   ├── SageMessage.tsx           Single chat message bubble with markdown
    │   ├── SageHistorySidebar.tsx    Past-conversation list sidebar
    │   ├── SageEmptyState.tsx        "Ask me anything" placeholder
    │   └── Sage.css                  Shared Sage component styles
    │
    ├── helix/
    │   ├── HelixDashboard.tsx        Order queue stats + stock alert summary
    │   ├── HelixPatientBoard.tsx     Per-patient medication order list
    │   ├── HelixFormulary.tsx        Drug catalog browser
    │   ├── HelixOrderReviewModal.tsx Approve/reject/substitute order modal
    │   └── Helix.module.css          CSS Module for Helix layout
    │
    ├── relay/
    │   ├── AlertsSection.tsx         Unresolved alert chips above channel list
    │   ├── CaseList.tsx              Channel/case list column
    │   ├── RelaySearch.tsx           Search bar for channel filtering
    │   ├── SageAmbientSidebar.tsx + .module.css  Sage ambient memory + action panel
    │   └── RelayComponents.css       Shared relay component CSS
    │
    └── surgery/
        ├── EmergencyIntakePopup.tsx + .module.css  Emergency case arrival modal
        ├── SurgeryActionCard.tsx + .module.css     Single surgery case card
        └── SurgeryReviewSheet.tsx + .module.css    Surgery detail + streaming risk report
```

---

### 14.10 Quick-start for a backend developer making a frontend change

**Scenario: you added a new FastAPI endpoint and need to wire it to the UI.**

1. Open `src/api/client.ts`. Find the section matching your module (e.g. `// ─── Vigil ───`).
   Add a new exported async function that calls `apiFetch('/your/new/path', options)`.

2. Open the relevant panel under `src/pages/panels/`. Import your new function. Call it
   inside a `useEffect` (for data fetched on mount) or inside an event handler (for user
   actions). Store the result with `useState`.

3. Render the result in the JSX return value of the panel function.

4. Run `npm run dev` from the project root. Vite's proxy will forward your API calls to
   the already-running FastAPI server on port 8000. The page hot-reloads on save.

**Scenario: you changed a Socket.IO event name on the server.**

Search the frontend for the old name: `grep -r 'old_event_name' src/`. Update every
`socket.on('old_event_name', ...)` and `socket.emit('old_event_name', ...)` you find.
The most likely files are `VigilPanel.tsx`, `RelayPanel.tsx`, `SurgicalSuitePanel.tsx`,
and `useStreamingTranscription.ts`.

**Scenario: you need to add a new user role and restrict which tabs it sees.**

Edit the `getAccessibleTabs` function in `src/pages/ClinicianShell.tsx`. Add a new `case`
for the role string and return the array of tab IDs it should see.

**Scenario: you want to understand why a 403 error is not showing up properly.**

Check that the call goes through `apiFetch` (not a local `fetch`). `apiFetch` throws an
error with `.status = 403` and `.permissions`. The panel must catch that error and check
`err.status === 403` to show a role-specific message.
