# Orb visual audit — features driven & checks run

The harness drives **every screen to its real on-screen state, in both themes**
(dark default + `[data-theme="light"]`), across all roles, then audits each
capture with `checks.cjs` and gates the result with `ci-gate.cjs`. The trunk's
third theme (**night / OR**, true-black) is exercised by the `night/*`
scenarios over a representative surface set.

## Files

| File | Role |
|---|---|
| `driver.cjs` | Playwright driver — scenario engine, route mocking, theme switching, screenshots, per-capture audits, console/page-error tracking. Mocked mode (default) + `--live` smoke mode against the real backend. The default mocked run also mirrors findings → `tests/visual/audit-results.json` and screenshots → `tests/visual/shots/` for the CI artifact upload. |
| `checks.cjs` | In-page audit (runs via `page.evaluate`): WCAG contrast with alpha compositing (incl. gradient color-stop compositing), invisible/zero-size focusable controls, horizontal overflow, clipped text, small targets. |
| `fixtures.cjs` | Mock API payloads keyed by URL path, **matching the real backend response shapes** (bare array vs `{key:[…]}` — a mismatch crashes panels). Fixtures deliberately exercise every semantic state (critical/elevated/stable risk, an emergency-surgery patient, safety alerts, overdue bundle elements, out-of-stock inventory, failed ingestion rows, a blocking drug-safety 428…). |
| `ci-gate.cjs` | Fails on non-allowlisted findings; prints capture count + ranked summary; writes `gate-summary.json`. |

## Screens driven (mocked mode)

- **Auth**: staff + patient login (and phone viewport).
- **Doctor** (DOC001): Vigil list → patient detail → medications modal →
  discharge modal → PRINT RECORD (print-media emulation of the discharge print
  portal); Orders (patient query + New Order form); Relay (channel list + open
  case room with Sage ambient sidebar); Sage; Scribe; Lens; Forecast (all 4
  sub-views); Command Center; Capacity Simulator; Revenue Integrity (analysis
  run); Appointments; Admit Patient modal (Identity + Vitals + Review tabs,
  Escape closes); Pulse widget.
- **Doctor — trunk wave-1/2 surfaces**: explainable NEWS2 ("why this score"
  per-parameter attribution panel); unified **Patient Story** timeline modal
  (`#btn-story`, filter chips); **eMAR Administer** modal (five rights,
  type-to-confirm) opened from the med table; the blocking **drug-safety
  acknowledgement modal** (med-add POST answers 428); **MFA enrollment** modal;
  **command palette** (Ctrl/Cmd-K patient search with results).
- **Surgeon** (DOC004, General Surgery): Surgical Suite schedule → day view →
  Active Cases.
- **Nurse** (NUR001): Vigil.
- **Admin** (ADM001): Vigil (bed-occupancy card) and every admin sub-tab:
  Overview, Audit Logs, Trace (with query), Ingestion, Security, Alerts,
  Model Governance, Impact, Trust Center, Flight Recorder, Users,
  Data & Backups.
- **Pharmacist** (PHA001): Helix Overview / Patient Board / Formulary + order
  review modal; Verification Queue; Sage.
- **Patient** (P001): Bridge status/vitals; all four records modals; Ask
  (streamed answer); appointment booking (confirm state, real
  `/api/appointments` fixtures).
- **Public**: `/discharge/:token` gate + verified summary.
- **Staff standalone**: `/downtime/:patientId` and `/handoff/:patientId`
  (I-PASS), each also captured under print-media emulation.
- **Error states**: forced-401 (API returns 401 → token cleared → login) and
  **session-expired** (mid-session 401 → `orb:session-expired` → blocking
  glass re-auth modal that preserves unsaved work). "Session expired"/401
  console noise is allowlisted **inside these two scenarios only**.
- **Night / OR theme** (`night/*`, trunk third theme): login, ward board +
  patient detail, Forecast, Relay, admin overview, pharmacist verify queue —
  validates the token system holds up on true-black in each kind of surface.
- **Responsive**: login + Vigil + Bridge at tablet (834×1194) and phone
  (390×844) widths.

Each capture is taken **after entrance animations settle** (the driver waits
for running finite animations to end; infinite pulses are ignored and frozen
via `animations: 'disabled'` at screenshot time). Do not audit mid-animation —
it produces opacity/contrast false positives.

## Checks per capture

1. **Text contrast** — foreground color (× accumulated ancestor opacity)
   composited over the ancestor background stack down to the first opaque
   layer. Gradients are decomposed into their color stops and the **worst**
   stop is reported, so gradient-filled gold buttons compute honestly instead
   of false-positive-ing (don't "fix" those — verify visually in the gallery).
   `< 3:1` gates (`contrast`), `< 1.6:1` is `invisible-text` (critical),
   `< 4.5:1` (normal) / `< 3:1` (large) is informational `aa-contrast`.
2. **Icon contrast** — SVG fill/stroke (incl. `currentColor`) vs composited
   background, `< 3:1` gates.
3. **Invisible/zero-size controls** — focusable elements with ~0 opacity or
   `< 2px` boxes gate; `< 24px` targets are informational (`small-target`,
   WCAG 2.2).
4. **Horizontal overflow** — `scrollWidth > viewport` (html clips overflow, so
   overflowing content is lost); offenders listed, scrollable containers exempt.
5. **Clipped text** — `nowrap` text truncated without `text-overflow: ellipsis`.
6. **Semantic-surface assertions** — scenario-level: the emergency patient row
   must be visually distinct from a normal row **in both themes** (guards the
   `[data-theme="light"] .glass-card` specificity clobber).
7. **Console/page errors** — any `console.error` or uncaught exception during
   a capture gates (allowlist: forced-401/session-expired scenarios, Vite HMR
   noise).

## Known-intentional allowlist (ci-gate.cjs)

1. DischargeAccess `verify-btn` disabled state (DOB gate / brute-force lockout).
2. `.sage-fab__toggle` hover-reveal hotword control (opacity 0 by design).
3. "Session expired"/401 console errors inside `errors/forced-401` and
   `errors/session-expired` only.
4. Vite dev HMR websocket noise.
5. Live mode: LLM-backed streaming endpoints absent in this environment.

## Extending

When you add or change a surface: add a **scenario** (or step) in `driver.cjs`
and matching **fixtures** in `fixtures.cjs` shaped exactly like the real
backend response (check `server/routes/*.py` — bare array vs keyed wrapper).
Run the gate before and after.

---

# Appendix — trunk feature walkthrough (manual checks)

The trunk branch added the feature waves below; each entry says how to reach
the surface and what to verify **by hand** in the browser (the automated
coverage for these surfaces is the scenario list above). Demo logins (dev
build only): `DOC001`/`password` (doctor), `ADM001`/`password` (admin),
`PHA001`/`password` (pharmacist), `P001`/`patient123` (patient).

## 1. Appointment booking (real, was fake)
**Reach:** Log in as `P001` → Bridge portal → "Schedule Follow-Up" card.
**Check:** Pick a future date/time, add a reason, Book. The slot appears under
"Your Upcoming Appointments" with a Cancel button and **survives a refresh**
(it's persisted, not a timer). Cancel → row goes away. As a clinician, the
Appointments panel shows a "Booked" chip on real bookings vs predicted ones.
**Regression watch:** booking a past date is rejected; double-submitting the
same booking (offline replay) does not create duplicates (Idempotency-Key).

## 2. MFA login step-up
**Reach:** Log in as a user with MFA enabled (enroll first via #3, or any TOTP
user). Enter ID + password → submit.
**Check:** An **"Authenticator Code"** 6-digit field appears; entering the
current TOTP finishes login. Wrong code → inline error, no crash. "Use a
different account" resets the form.

## 3. MFA enrollment
**Reach:** Any clinician → sidebar user card → the **lock icon** (Security).
**Check:** Modal shows a setup key (grouped), the `otpauth://` provisioning URI,
Copy buttons, a 6-digit verify field, "Enable MFA". Escape closes. Already-
enrolled users get the Disable path (password + code).

## 4. User management (admin)
**Reach:** `ADM001` → Admin → **Users** tab.
**Check:** Table of users with role chips, MFA on/off, Active/Disabled status.
"Create user" adds one. Row actions: change **Role**, **Reset password**,
**Deactivate/Activate** (deactivating revokes their sessions; you can't
deactivate yourself). Also: any user can change their own password (relogin
required after).
**Security watch:** disabled users can't log in; hashes/secrets never returned.

## 5. Critical-alert notifications
**Reach:** Any clinician shell → the **bell icon** in the header (beside the
theme toggle).
**Check:** Click → browser asks notification permission. With the tab
**backgrounded**, a critical realtime event (deterioration / emergency / alert)
fires a browser notification + a chime + a flashing tab title; clicking the
notification refocuses Orb. The bell also toggles sound mute.
**Note:** notifications only fire while the tab is hidden (by design).

## 6. Global patient search / jump-to-patient
**Reach:** Anywhere in the clinician shell → press **Cmd/Ctrl-K**.
**Check:** Frosted palette opens; type a name/MRN/bed → results with risk dots.
↑/↓ + Enter (or tap) jumps to that patient in Vigil. Esc closes.

## 7. Patient-list pagination (scale)
**Reach:** Vigil (Patients) with a large census.
**Check:** Board loads the first page; a **"Load more (N of TOTAL)"** button
appends the next page. Search/sort still work over loaded rows. (API sends
`X-Total-Count`; body stays an array so nothing else broke.)

## 8. Idle auto-lock
**Reach:** Log in as staff, leave the tab idle ~15 min (or trigger it).
**Check:** A blocking **"Screen locked"** overlay appears; re-enter password to
resume with all work intact, or Sign out. Patients (Bridge) are exempt.

## 9. FHIR record export
**Reach (patient):** `P001` → Bridge → My Records → "Download my record (FHIR)".
**Reach (clinician):** any `$everything` export.
**Check:** Downloads a FHIR R4 Bundle JSON. A patient can export **only their
own** record (403 otherwise); clinicians with `patient.read_all` can export any.

## 10. Backups & GDPR erasure (admin)
**Reach:** `ADM001` → Admin → **Data & Backups**.
**Check:** Lists DB backups with size/date; "Create backup" makes one. "Data
Erasure" anonymizes a patient + deletes their clinical records (must not be
currently admitted; audit trail preserved; irreversible — confirm dialog).

## 11. Orders & Med-list (MAR) print
**Reach:** Vigil → open a patient → "Print Meds"; Orders panel → print.
**Check:** A clean print view opens the browser print dialog with the med
list / orders formatted for paper. Print fires **after** the view renders (no
blank-page race).

## 12. Command Center — capture-all downtime
**Reach:** `DOC001`/`ADM001` → Command tab → downtime readiness area.
**Check:** "Capture all admitted" takes a one-click ward-wide snapshot for an
outage (was per-patient only). Success note renders in a readable status color.

## 13. i18n on new panels (scaffold)
**Reach:** shell language toggle (EN/हि).
**Note:** shell + login + appointments are translated; the newest investor
panels remain English pending string extraction (tracked, not a regression).

## 14. Docs & deploy
**Reach:** repo root `.env.example` + README deploy section.
**Check:** every `ORB_*` env var is documented with a safe default.

## 15. Drug-safety gate on every medication path (slice 1)
**Reach:** Vigil → open a patient → Medications → "+" → order `Amoxicillin`
for a patient with a documented penicillin allergy (seed one via the Admit
modal allergies field, or any patient with active allergies).
**Check:** Submission is challenged with a blocking **Drug Safety** modal
listing the allergy/interaction alerts; "Acknowledge & Order" stays disabled
until a clinical justification (≥10 chars) is entered; on acknowledge the
order proceeds and the override lands in Admin → Audit (action
`drug_safety_override`, hash-chained). A drug outside the interaction KB
(e.g. `Obeticholic acid`) yields an explicit **"interactions NOT screened"**
note — never a silent pass. The same gate runs on CPOE medication orders,
Relay ambient actions, Helix approve, Sage handoff, and signed-note
auto-extraction (non-interactive paths can never ack; they route to the
pharmacist queue instead).
**Regression watch:** note-extracted meds must appear in the pharmacist
queue with `AI extracted` provenance (no more clinician-provenance
laundering); Helix approve requires an exact patient id (no name-substring
matches); agentic dispatch refuses `TBD` doses and the universal `1234` PIN
is gone (PINs are provisioned per user, bcrypt, lockout after 5 failures).

## 16. eMAR — bedside administration (slice 1)
**Reach:** Vigil → patient detail → Medications table → **Administer** on an
active med.
**Check:** The modal shows the five rights (patient/bed, drug, dose, route,
time) and requires typing the drug name to confirm (barcode-style). "Given"
records with the charted dose/route; "Not given" requires a reason. A
**"Given HH:MM"** chip appears on the med row and survives refresh
(append-only `medication_administrations` table, audit-chained). Unverified
AI meds show a disabled button ("Pharmacist verification required"); a
replayed request with the same Idempotency-Key never double-charts a dose.

## 17. Pharmacist verify: fail-closed screening + severe ack (slice 1)
**Reach:** `PHA001` → Verify Queue.
**Check:** Rows carry screening badges — severity chips for real alerts,
**NOT SCREENED** for drugs outside the interaction KB, **SCREENING
UNAVAILABLE** if the safety check itself failed (never "no alerts" on a
failure). Verifying a severe-alert med demands a reason (inline acknowledge
flow, 428 under the hood) and the override is audit-chained.

## 18. Truthful Vigil + explainable NEWS2 (slice 2)
**Reach:** Vigil → open any patient.
**Check:** The risk badge and gauge read **"NEWS2 {n} · {band}"** (e.g.
"NEWS2 7 · CRITICAL") — the raw RCP aggregate and clinical band, never a
"%" (the old 0-1 index shown as a percentage implied a deterioration
probability it isn't). Expand **"Why NEWS2 {n}? — per-parameter
contribution"** to see each vital's point contribution (SpO2 +3, RR +2…),
worst first, straight from the scoring table, with an explicit *"not a
clinically-validated probability"* disclaimer and the single-parameter-3
rule flagged when it fires.
**Regression watch:** the **DIAGNOSIS** header shows only the real diagnosis
— the previously fabricated, MRN-hashed comorbidity tags ("Former Smoker",
"CAD"…) are gone; comorbidity tags render only from genuinely recorded data.
Critical rows float to the top of the board by their real NEWS2 band, not a
hardcoded demo patient id. Estimated lab trends render **dashed** (distinct
from measured data) in addition to the "Estimated" badge.

## 19. Hotword is opt-in and never cloud ASR by default (slice 2)
**Reach:** any clinician shell (the floating Sage mic).
**Check:** The hotword is **OFF by default** — no microphone capture starts
at login. Even when a user enables the hotword, continuous browser speech
recognition (which streams audio to cloud ASR) stays disabled unless
`localStorage.orb_allow_cloud_asr === 'true'` is explicitly set — a second,
deliberate consent. Out of the box, no ward-room audio (PHI) ever leaves the
device, consistent with the on-prem posture.

## 20. Live presence on every patient (slice 3)
**Reach:** open the same patient (Vigil → patient detail) as two different
clinicians in two browsers.
**Check:** Each sees the other as a **presence chip** under the patient name
("{name} also viewing" / "N others viewing", avatar initials, live green
dot). Closing one view removes the chip on the other within a moment. One
clinician with two tabs counts once (deduped by user). The chip is silent
when you're the only viewer — it surfaces only genuine concurrent access, so
two people don't chart the same patient unaware.

## 21. Typing indicators + reconnect-safe Relay (slice 3)
**Reach:** Relay → open a case room as two clinicians.
**Check:** Typing in one shows "{name} is typing…" above the other's
composer, clearing ~4s after they stop (throttled to ≤1 ping/1.5s). Pull the
network briefly and restore it: live messages keep arriving — the client
**re-joins its channel room on reconnect** (previously channel messages went
silent after any blip until you switched channels). Dead one-directional
`ambient:*` listeners were removed.

## 22. Socket sessions honour revocation (slice 3)
**Reach:** N/A (security posture; covered by `tests/test_ws_realtime.py`).
**Check:** The Socket.IO handshake now re-checks `token_version` and
`is_active` like the REST layer — a logged-out, password-reset, or
deactivated account can no longer keep streaming PHI (vitals/relay/scribe)
over an already-open socket until the JWT expires. A bad/stale token is
rejected at connect.

## 23. Auth hardening: deactivation, lockout, bcrypt seed (slice 4)
**Reach:** `ADM001` → Admin → Users → **Deactivate** a user; then try to log
in as that user.
**Check:** A deactivated account is refused at login (**403**, "Account
deactivated") AND on every existing token (`get_current_user` rejects on
`is_active` alone, not only via token_version). Repeated wrong-password
attempts against one account lock it (**429**) even from a fresh IP — a
persistent per-account counter (`users.failed_login_attempts` /
`login_locked_until`) complements the per-IP limiter to throttle distributed
credential stuffing; a correct login clears it. The demo seeder now writes
**salted bcrypt** hashes (`$2b$…`), not the legacy unsalted SHA-256 (the
login flow still accepts and auto-upgrades any legacy hash). Every `ORB_*`
env var the server reads is now documented in `.env.example`.
**Covered by:** `tests/test_auth_lockout_active.py`.

> **Deferred (own slice):** moving the JWT out of `localStorage` into an
> HttpOnly+Secure+SameSite cookie with a refresh flow + CSRF is a larger
> architectural change to the auth/socket handshake and is intentionally not
> in this slice; the localStorage exposure remains mitigated by DOMPurify on
> the HTML sinks. The higher-severity server-side gaps (revocation, deactivation,
> lockout, weak-hash seeding) are closed here.

## 24. Third theme + a11y focus + accent contrast (slice 5)
**Reach:** click the theme control in the shell header repeatedly.
**Check:** It **cycles dark → light → night → dark**. The **Night Shift / OR**
theme is true-black with high-contrast white text, a warm amber (low-blue)
accent, and crisper, more-saturated semantic colours — built for dim wards and
the OR (see the gallery: any `night/*` capture). **Tab** through the shell,
Orders, and med forms: every focusable control now shows a consistent keyboard
focus ring (`:focus-visible`, previously near-absent). In **light** mode,
accent-coloured text uses an AA-contrast token (`--accent-text`) instead of the
washed-out Sierra blue. The full design system is documented in
[`DESIGN.md`](../../DESIGN.md).
**Gate:** the night theme is captured across representative surfaces (login,
ward board, patient detail, Forecast, Relay, Admin, pharmacist queue) by the
`night/*` scenarios.

> **Deferred (own slice):** migrating VigilPanel's ~153 inline styles and the
> remaining hardcoded-colour recharts sites onto tokens, and a single unified
> chart kit, are large refactors tracked for a follow-up; the token layer,
> night theme, focus convention, and accent-text fix land here.

## 25. Performance hygiene + single-origin deploy (slice 6)
**Reach:** N/A (behavioural / deployment).
**Check:** (a) **Polling pauses when the tab is backgrounded** — Vigil (30s)
and Sage insights (20s) use `useVisiblePolling`, which stops the interval on
`document.hidden` and fires once on return, instead of hammering the backend
for data nobody's looking at. (b) The **Vigil board no longer re-renders all
~100 rows every 2s** — `PatientRow`'s `memo` now actually bails (the parent
passes the stable `setSelectedPatientId`, not a fresh closure per row). (c)
**A single `uvicorn main:socket_app` now serves the whole product**: when a
`../dist` build exists, FastAPI serves the SPA with history fallback
(`/dashboard`, `/bridge` → index.html) while `/api`, `/uploads`, and
`/socket.io` keep precedence and unmatched `/api/*` still 404s JSON. Previously
no shipped path served the frontend at all (dev-server only).

## 26. Unified Patient Story (slice 7)
**Reach:** Vigil → open a patient → **🧬 Story** in the command bar.
**Check:** One scrollable clinical narrative merges **every** recorded event —
admission, critical NEWS2 alerts, clinician + scribe notes, imaging, meds
(started/stopped), CPOE orders, bedside administrations (eMAR) and discharge —
onto a single timeline, newest first, each with a colour-coded type rail and a
timestamp. **Filter chips** (only the types that occur are shown) narrow the
feed; "All" resets. It refreshes live on the `patient:timeline_updated` socket
event, and is focus-trapped + Escape-closable. AI-extracted meds pending
pharmacist verification are labelled as such. Backed by
`/api/vigil/patients/{id}/story` (`tests/test_patient_story.py`).

---

## Running the whole gate yourself

```bash
npm run dev &                 # vite on :5173 (proxies /api → backend :8000)
node tests/visual/driver.cjs  # every screen, both themes + night pass → tests/visual/out/
node tests/visual/ci-gate.cjs # fails on any non-allowlisted finding
```

Portable via `ORB_BASE_URL` / `ORB_CHROMIUM` / `ORB_CHROMIUM_PATH`. CI runs
this on every PR (`.github/workflows/ci.yml` → `visual-audit` job) and uploads
`tests/visual/shots/` + `tests/visual/audit-results.json` (written by the
default mocked run alongside `out/`).
