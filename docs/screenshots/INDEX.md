# Screenshot index

Every PNG in `docs/screenshots/` (top level), what it actually shows, which theme it is
actually in, and which `docs/guide/screen-map/` file embeds or references it.

**Every image in this index was opened and looked at.** Filenames from the automated
walkthrough are not reliable, at least three of them describe a state the image does not
contain (see [Filenames that lie](#filenames-that-lie)). Where this index and a filename
disagree, believe this index.

- **223 PNGs** at the top level. 71 of them are byte-different, pixel-identical copies
  (the `-full` suffix, see below), leaving **152 distinct captures** in **136 distinct
  screens/states**.
- Two subdirectories, `wavec/` (73 files) and `wavee/` (167 files), hold earlier
  before/after and wave-by-wave captures. They are **not indexed here** and nothing
  references them; see [Subdirectories](#subdirectories).

---

## Read this before you trust any screenshot

**1. The `-full` suffix is meaningless.** 71 files end in `-full.png` and were meant to be
full-page captures. All 71 are **1512 × 950**, the same viewport size as their siblings, 
and every one is pixel-identical to the file without the suffix (max per-pixel difference
across the whole set: 44/255, i.e. anti-aliasing noise from a re-render). The full-page
capture mode in the walkthrough script did not work. Anything below the fold on these
screens was never captured. Use the plain filename; ignore the `-full` twin.

**2. Nothing here shows below-the-fold content** except `bridge-01-home-dark.png`
(1512 × 1706), `misc-01-downtime-dark.png` (1512 × 1242) and `misc-02-handoff-dark.png`
(1512 × 1216), which are standalone print/portal pages rather than shell panels.

**3. The data is seeded and the clock is wrong.** The capture run is dated **8 Aug 2026**
but patient records were admitted in **May–Aug 2026** and last charted **1–2 Aug 2026**.
That is why observations read *"144h 47m late"* and a stat dose reads *"227h 42m late"*.
Those are not UI bugs; they are a stale fixture rendered honestly. Two consequences of it
*are* bugs and are listed under [Shows a known defect](#shows-a-known-defect).

**4. The role prefix tells you who was signed in:**

| Prefix | User | Role | Department |
|---|---|---|---|
| `adm001-` | Rajesh Kumar | admin | Administration |
| `doc001-` | Dr Meera Sharma | doctor | General Medicine |
| `doc004-` | Dr Vikram Rathore | surgeon | General Surgery |
| `nur001-` | Priya Reddy | nurse | General Medicine |
| `pha001-` | Deepa Joshi | pharmacist | Pharmacy |
| `admin-`, `nurse-`, `sage-`, `relay-`, `orders-`, `round-`, `scribe-`, `chart-`, `surgical-`, `bridge-`, `misc-` | targeted single-workflow runs | n/a | n/a |

**5. Theme.** Measured, not read off the filename. One file is mislabelled:
`misc-01-downtime-dark.png` is a **light** page (the downtime snapshot renders on white in
both themes, by design, it is meant to be printed).

---

## By screen-map file

Column key: **Use** = `body` (embedded in the chapter), `dup` (pixel-duplicate of a
sibling), `alt` (other theme, mentioned but not embedded), `defect` (listed but deliberately
not embedded as normal behaviour), `ref` (referenced in the index only).

### `01-app-shell.md`: the shell frame (sidebar, top bar, orb)

The landing screen after sign-in is whatever `accessibleTabs[0]` is for the role, so these
"after-login" captures double as the RBAC evidence: count the sidebar items.

| File | Theme | What it shows | Use |
|---|---|---|---|
| `doc001-01-after-login-light.png` | light | Doctor shell, 12 sidebar items, Patients first, "Admit Patient" button + user card pinned to the sidebar foot | body |
| `doc001-01-after-login-dark.png` | dark | same, dark | alt |
| `pha001-01-after-login-light.png` | light | Pharmacist shell, **only three** sidebar items (Pharmacy, Verify Queue, Sage), no Admit Patient shortcut | body |
| `pha001-01-after-login-dark.png` | dark | same, dark | alt |
| `doc004-01-after-login-light.png` | light | Surgeon shell, Surgical Suite appears at the foot of the nav; no Admin, no My Shift | body |
| `doc004-01-after-login-dark.png` | dark | same, dark | dup of `doc004-02-vigil-dark.png` |
| `adm001-01-after-login-light.png`, `adm001-01-after-login-dark.png` | light / dark | Admin shell, 10 items ending in Admin; differs from `adm001-02-vigil-*` only by the clock | alt |
| `nur001-01-after-login-light.png`, `nur001-01-after-login-dark.png` | light / dark | Nurse shell, **My Shift** is first and is the landing tab | alt |

### `02-login.md`: the login card

| File | Theme | What it shows | Use |
|---|---|---|---|
| `adm001-00-login-light.png` | light | Staff face: "Clinician Login / Orb Clinical Platform", Employee ID placeholder `DOC001`, Password, Sign In, and the "Set up this tablet for PIN sign-in" link | body |
| `adm001-00-login-dark.png` | dark | same, dark | alt |
| `bridge-00-login-dark.png` | dark | Patient face: "Patient Access / Orb Bridge", **Patient ID** placeholder `P001`, and **no** tablet-setup link | body |
| `doc001-00-login-*.png`, `doc004-00-login-*.png`, `nur001-00-login-*.png`, `pha001-00-login-*.png` | light/dark | identical to `adm001-00-login-*`, the login card cannot know who is about to sign in | dup (8 files) |

No capture exists for the PIN-pad face, the tablet-setup paste box, the authenticator-code
step, or the MFA-enrolment modal. Four of the six documented login faces are unphotographed.

### `03-sage.md`: the Sage panel

| File | Theme | What it shows | Use |
|---|---|---|---|
| `doc001-06-sage-light.png` | light | Empty state: 264px history rail with five recent conversations, greeting, the four suggestion cards, and the composer with the **Thinking** toggle to the left of the mic | body |
| `doc001-06-sage-dark.png` | dark | same, dark | alt |
| `sage-01-general-thinkingON-dark.png` | dark | Careful (27B) lane answer with **both** guardrail chips, "Limited evidence, Verify independently" and "Partial data: No signed Scribe notes, No recent imaging", over the retrieval disclosure "Rajesh Iyer · 3 retrieved" | body |
| `sage-02-general-thinking-dark.png` | dark | The same careful lane one turn earlier; the retrieved-documents drawer is open showing three external guidelines | body |
| `sage-02-general-thinkingOFF-dark.png` | dark | Fast (4B) lane. The amber "Fast answer from the small model (medgemma:4b), lower accuracy" strip and the footer explaining that patient/medication/dose questions still route to the careful model | **defect**, the raw XML contract leaks into the answer |
| `sage-01-general-dark.png` | dark | Mid-stream: the three-dot pulse and the stage line "Searching clinical guidelines…", composer button switched to stop | body |
| `sage-10-patientQ-dark.png` | dark | Patient question answered with a per-parameter NEWS2 breakdown that sums to 14 and states the score came from Orb's deterministic engine, not the model | body |
| `sage-11-followup-dark.png` | dark | Follow-up ("And what should I do about it?") keeps patient context and cites a local policy by id, `LOC-SAFE-007` | body |
| `sage-12-leaktest-dark.png` | dark | Leak test, a pure pharmacology question returns pharmacology only, no patient data | body |
| `sage-13-crossdept-dark.png` | dark | Cross-department refusal: "That patient is outside your department", explicitly distinguished from "no such patient", and the request logged to audit | body |
| `adm001-04-sage-light.png` / `-dark.png` | light / dark | Sage empty state as **admin** ("Good morning, Dr. Kumar") | ref |
| `nur001-06-sage-light.png` / `-dark.png` | light / dark | Sage empty state as **nurse** | ref |
| `pha001-04-sage-light.png` / `-dark.png` | light / dark | Sage empty state as **pharmacist**, the history rail reads "No recent conversations" | ref |
| `doc001-06-sage-*-full.png`, `adm001-04-sage-*-full.png`, `nur001-06-sage-*-full.png`, `pha001-04-sage-*-full.png` | n/a | pixel-identical twins | dup (8 files) |

### `04-relay-list.md`: the Relay room list

| File | Theme | What it shows | Use |
|---|---|---|---|
| `doc001-05-relay-light.png` | light | Doctor's five case rooms + one Department room, each with an acuity chip (ELEVATED / CRITICAL / STABLE) and a right-hand count badge | body |
| `doc001-05-relay-dark.png` | dark | same, dark | alt |
| `adm001-03-relay-light.png` | light | Admin's hospital-wide view: ten rooms across five wards plus a "Show 20 More ↓" button, the same component with a wider scope | body |
| `adm001-03-relay-dark.png` | dark | same, dark | alt |
| `nur001-05-relay-light.png` / `-dark.png` | light / dark | Nurse's view, identical scope to the doctor's | ref |
| `*-relay-*-full.png` (6 files) | n/a | pixel-identical twins | dup |

### `05-relay-thread.md`: an open Relay thread

| File | Theme | What it shows | Use |
|---|---|---|---|
| `relay-01-room-dark.png` | dark | Case: Priya Nair. Message bubbles carry a role chip (DOCTOR / NURSE) beside the name; the right rail is **SAGE COPILOT**, a standing case summary that already contains the retracted-potassium correction | body |
| `relay-03-wrongroom-dark.png` | dark | The wrong-patient interlock: an amber panel under the message reading "This looks like it belongs to Ananya Kapoor", with **It's the right patient** / **Move to Case: Ananya Kapoor**, and the crucial sentence "It has already been sent; nothing is blocked" | body |
| `relay-02-sent-dark.png` | dark | Same thread just after sending | **defect**, the sent message renders twice |

### `06-vigil-board.md`: the Vigil ward board

| File | Theme | What it shows | Use |
|---|---|---|---|
| `doc001-02-vigil-light.png` | light | Three metric tiles (5 Admitted · My Dept / 1 Elevated Risk / 2 Critical), the "Sage, State of the Hospital" card ending in the provenance line **"Deterministic snapshot (local)"**, and the NEEDS YOUR ATTENTION feed with a Dismiss button per row | body |
| `doc001-02-vigil-dark.png` | dark | same, dark | alt |
| `chart-00-vigil-list-dark.png` | dark | the same screen, captured by the chart run | dup of `doc001-02-vigil-dark.png` |
| `nur001-03-vigil-light.png` | light | Nurse's board scrolled to ACTIVE PATIENTS: two solid-red EMERGENCY rows above three outline rows, with a right-hand risk percentage. The provenance line here reads **"Narrated locally · local/medgemma:4b"**, the same card, a different generator | body |
| `nur001-03-vigil-dark.png` | dark | same, dark | alt |
| `adm001-02-vigil-light.png` / `-dark.png` | light / dark | Admin's board | **defect**, tiles read 0 while the feed lists 13 alerts |
| `doc004-02-vigil-dark.png` | dark | Surgeon's board, ward SW-1 | dup of `doc004-01-after-login-dark.png` |
| `nur001-03-vigil-*-full.png`, `doc001-02-vigil-*-full.png`, `adm001-02-vigil-*-full.png`, `doc004-02-vigil-dark-full.png` | n/a | pixel-identical twins | dup (7 files) |

### `07-vigil-chart.md`: the individual patient chart

| File | Theme | What it shows | Use |
|---|---|---|---|
| `chart-01-patient-full-dark.png` | dark | The only chart capture there is. Identity strip → bed / NEWS2 13 CRITICAL / diagnosis / five vitals; the ten action buttons top-right (Diag. Reports … PRINT MEDS, DISCHARGE); the **SEPSIS SIX** bundle card with six items and Mark done; the four-stage CLINICAL COURSE with a SAGE-PROJECTED chip and a DISCHARGE WHEN criteria row | **defect**, bundle timer and patient sex are both wrong |
| `misc-01-downtime-dark.png` | **light** | The DOWNTIME button's output: a read-only, print-shaped local snapshot, header "EHR Downtime Mode · Local read-only snapshot · works offline", footer warning that it may not reflect changes after the capture time | body |

No light-theme chart capture exists. No capture exists of Notes, Story, Timeline, Medications
or any of the eight other chart windows.

### `08-nurse-board.md`: the nurse shift board

| File | Theme | What it shows | Use |
|---|---|---|---|
| `nur001-02-shift-light.png` | light | **Due now** tab: five count pills (5 patients / 2 critical / 1 elevated / 4 obs overdue / 6 doses overdue), then task rows each carrying a NEWS2 badge, an OBS or MED type chip, the order detail, and a right-aligned lateness | body |
| `nur001-02-shift-dark.png` | dark | same, dark | alt |
| `nurse-01-mypatients-dark.png` | dark | **My patients** tab: one card per bed with the NEWS2 badge, the escalation sentence NEWS2 mandates at that score, the last-obs line and two buttons, Record obs / Med round | body |
| `nur001-02-shift-*-full.png` | n/a | pixel-identical twins | dup (2 files) |

### `09-nurse-vitals-emar.md`: vitals entry and the eMAR round

| File | Theme | What it shows | Use |
|---|---|---|---|
| `nurse-03-recordobs-dark.png` | dark | Empty obs sheet: six numeric fields, the On air / On oxygen pair, the five ACVPU buttons, and the placeholder "Enter observations to see the NEWS2 they produce" where the score will appear | body |
| `nurse-04-obs-filled-dark.png` | dark | The same sheet filled: a **per-field point chip** (+3, +3, +2, +2, +1) beside each abnormal value, and the live NEWS2 13 panel naming its three drivers plus the footnote "NEWS2 (RCP 2017), computed from the values above, not yet recorded" | body |
| `nurse-05-obs-saved-dark.png` | dark | After save: a green flash strip above the cards, and Rajesh Iyer's card has flipped from "obs 144h 33m late" to "next obs in 30m" | body |
| `nurse-07-emar-due-dark.png` | dark | Medication round with work to do: an NKDA allergy strip, an **OVERDUE (2)** group defined as "Past the derived due time by more than 15 minutes", a CONTINUOUS group, and the footnote that due times are derived from frequency, not from a prescriber-authored schedule | body |
| `nurse-06-emar-dark.png` | dark | Medication round for a patient with continuous infusions only, no overdue group, three Administer buttons, "Running infusions and feeds, check, do not chart on a clock" | body |
| `nurse-08-emar-administered-dark.png`, `nurse-09-administer-modal-dark.png` | dark | filenames promise an administered state and the five-rights modal; both are **pixel-identical to `nurse-07-emar-due-dark.png`** | dup, see [Filenames that lie](#filenames-that-lie) |

The five-rights `AdministerModal`, the safety-critical surface of this chapter, has **no
working capture**.

### `10-nurse-handover.md`: the I-PASS handover

| File | Theme | What it shows | Use |
|---|---|---|---|
| `nurse-02-handover-dark.png` | dark | Handover tab collapsed: "0 of 5 handed over" counter, one row per bed with its NEWS2 badge and a "Not handed over" state plus a **+** expander | body |
| `nurse-10-ipass-dark.png` | dark | One row expanded: the five I-PASS blocks (I / P / A / S / S) rendered as headed sections, the action list carrying six OVERDUE sepsis-six items, and the read-back line with blank fields to be filled by the receiver | body |
| `misc-02-handoff-dark.png` | dark | The standalone `/handoff/:patientId` page, same document, different frame: **Save handoff** and **Print** in the header, a "Contingency planning" block the tab version does not show, and a "Plain-text handoff" disclosure at the foot | body |

### `11-round-mode.md`: ward round mode

| File | Theme | What it shows | Use |
|---|---|---|---|
| `doc001-03-round-light.png` | light | The whole round screen: the "0 of 5 seen" queue rail, the bed header, and the three summary columns, SINCE 20:00 YESTERDAY / CURRENT ISSUES / DECIDE TODAY, over the dictation block and the two-button footer (Skip for now **K** / Seen · next bed **S**) with the keyboard legend underneath | body, with a defect noted |
| `doc001-03-round-dark.png` | dark | same, dark | alt |
| `round-01-dictate-dark.png` | dark | identical to `doc001-03-round-dark.png` | dup |
| `round-02-saved-dark.png` | dark | After **Save plan to chart**: the confirmation "Saved to the chart" next to the button, and the note now listed in the left column as "01:44 AM WARD ROUND note", the empty-overnight card is gone | body, with a defect noted |
| `doc001-03-round-*-full.png` | n/a | pixel-identical twins | dup (2 files) |

Notice the **negative-evidence card** in `doc001-03-round-light.png`: "Nothing recorded
overnight … Nothing failed to load, there is genuinely nothing recorded here." That
distinction is the point of the column and is the thing to read first.

### `12-orders.md`: orders

| File | Theme | What it shows | Use |
|---|---|---|---|
| `doc001-04-orders-light.png` | light | Orders landing: title, **+ New Order**, a single Patient ID field with placeholder `e.g. PAT001`, and the instruction "Enter a patient ID to view orders", nothing loads until a patient is chosen | body |
| `doc001-04-orders-dark.png` | dark | same, dark | alt |
| `orders-02-loaded-dark.png` | dark | A valid MRN entered (`PAT-A73E5E98`), field ring turns accent, result is the empty list "No orders for this patient yet", an empty patient, not a failure | body |
| `orders-03-neworder-dark.png` | dark | The create form: Order Type / Priority selects, a **Code System + Code + Validate** trio (LOINC, placeholder `e.g. 2524-7`), Description, Cancel / Create Order, terminology validation is a separate step before creation | body |
| `orders-01-badid-dark.png` | dark | An unknown ID (`PAT001`) | **defect-adjacent**, deliberately triggered error state, but the message is wrong |
| `nur001-04-orders-light.png` / `-dark.png` | light / dark | Nurse's Orders tab, same screen, same empty prompt | ref |
| `doc001-04-orders-*-full.png`, `nur001-04-orders-*-full.png` | n/a | pixel-identical twins | dup (4 files) |

The **Order sets** sub-view, half of this chapter, has no top-level capture at all. The
only order-set images in the repo are in `wavee/` (`w4-01-ordersets-list-*`,
`w4-02-ordersets-sepsis-*`) and `wavec/`.

### `13-admin.md`: Admin

Thirteen tabs, all captured in both themes. The tab strip is identical in every one, so the
row below tells you only what is *under* it. Both files are named in each row; the light one
is the one embedded in the chapter.

| Files | What it shows | Use |
|---|---|---|
| `admin-00-overview-light.png`<br>`admin-00-overview-dark.png` | Ingestion mode **Simulation**, 789 audit events broken out by category, 0 ingested payloads, 0 sources, and an Auth Summary (24h) with Login Failure 4 / Login Success 35 / Patient Scope Denied 1 / Permission Denied 2 | body (light) |
| `admin-01-pilot-scorecard-light.png`<br>`admin-01-pilot-scorecard-dark.png` | The shadow-mode scorecard: window / wards / **PHASE Phase A, silent (no clinician sees Orb output)**, a "WHERE THESE NUMBERS COME FROM" provenance block (source table, schema v1, 113,376 events, instrument lag p50 21 ms), the three lanes with `simulator 84,649` marked synthetic, and PREREQUISITES **4 of 7 met** | body (light) |
| `admin-02-audit-logs-light.png`<br>`admin-02-audit-logs-dark.png` | 63,143 events, 2,526 pages; columns Timestamp / Correlation / Category / Action / Outcome / Actor / Patient / Summary, each correlation id with a copy affordance | body (light) |
| `admin-03-trace-light.png`<br>`admin-03-trace-dark.png` | Empty state, a correlation-id box and the instruction to paste one from Audit Logs | body (light) |
| `admin-04-ingestion-light.png`<br>`admin-04-ingestion-dark.png` | `Mode: simulation`, a status filter, "0 events", empty state "No ingestion events recorded yet" | body (light) |
| `admin-05-security-light.png`<br>`admin-05-security-dark.png` | Empty state "No security events" | **defect**, contradicts Overview |
| `admin-06-alerts-light.png`<br>`admin-06-alerts-dark.png` | Empty state "No alert events / No ingestion-driven alerts have been generated yet", correct, ingestion is off | body (light) |
| `admin-07-model-governance-light.png`<br>`admin-07-model-governance-dark.png` | Five models with Version / Training data / Checkpoint / Validated / Active. `forecast_lstm` is **untrained**, `vigil_cfc` is **UNTRAINED**, both inactive; only `vigil_news2` is validated. The header states Orb never presents an untrained model as validated | body (light) |
| `admin-08-impact-light.png`<br>`admin-08-impact-dark.png` | $25,814/month saved, $310K/year, 6.5 events prevented, broken into four cards where two read **$0** with "0 recorded events", over a tunable "Assumptions, cited (9)" drawer | body (light) |
| `admin-09-trust-center-light.png`<br>`admin-09-trust-center-dark.png` | Trust score 85 with 9 passing / 4 warnings / 0 failing / **1 of 5 models validated** (STALE: captured 8 Aug 2026, before the `sage_llm_hosted` row was removed on 2026-08-09; the live screen now reads **1 of 4**); a tamper-evident audit log marked "Verified, chain intact" over 63,272 sealed rows; a control checklist where each row carries a verdict **and** its remediation (TLS in transit = WARN, encryption at rest = PASS) | body (light) |
| `admin-10-flight-recorder-light.png`<br>`admin-10-flight-recorder-dark.png` | A 500-event scrubber with transport controls, a chain-verified badge, an event list and a detail pane | **defect**, overlapping text, and the detail pane never loads |
| `admin-11-users-light.png`<br>`admin-11-users-dark.png` | 19 users; columns Name / ID / Role / Department / MFA / Status / Actions, MFA **Off** on every row | **defect**, placeholder user names |
| `admin-12-data-backups-light.png`<br>`admin-12-data-backups-dark.png` | Five backups with size and timestamp, a Create backup button, the notice "Restores are intentionally not available here, they must be performed out-of-band by an operator with the server stopped", and a separate Data Erasure block | body (light) |
| `adm001-10-admin-light.png` | the Overview tab reached through the ordinary sidebar | dup of `admin-00-overview-light.png` |
| `adm001-10-admin-dark.png`, `adm001-10-admin-*-full.png` | same, dark / twins | dup (3 files) |

### `14-surgical-appointments.md`: Surgical Suite and Appointments

| File | Theme | What it shows | Use |
|---|---|---|---|
| `surgical-02-activecases-dark.png` | dark | **Active Cases** tab, badge 5: one card per case with initial avatar, age/sex, MRN, procedure, a Diagnosis + OR strip, a SCHEDULED status chip and an **Alert Team** button | body |
| `doc004-02-surgical-suite-light.png` | light | **Schedule** tab: the week strip, one card per day | **defect**, every day reads 0 Surgeries while Active Cases holds 5 |
| `doc004-03-surgical-suite-dark.png` | dark | same, dark | alt |
| `adm001-09-appointments-light.png` | light | Appointments: a four-card summary strip (Today's / This Week Total 21 / Critical Patients 3 / Post-Discharge 0) over one column per day, each row carrying a time, an acuity chip, a patient name, an age badge and a reason | body |
| `adm001-09-appointments-dark.png` | dark | same, dark | alt |
| `doc001-13-appointments-light.png` / `-dark.png`, `nur001-11-appointments-light.png` / `-dark.png` | light / dark | the same board seen by doctor and nurse | ref |
| `doc004-02-surgical-suite-light-full.png`, `doc004-03-surgical-suite-dark-full.png`, `*-appointments-*-full.png` | n/a | pixel-identical twins | dup (8 files) |

### `15-analytics-pharmacy-scribe-lens-bridge.md`: the remaining panels

| File | Theme | What it shows | Use |
|---|---|---|---|
| `adm001-05-forecast-light.png` | light | Forecast: a four-tab strip, four headline numbers, the 7-day occupancy projection with a dashed capacity ceiling, and a per-day table underneath | body, but see the badge defect |
| `adm001-05-forecast-dark.png` | dark | same, dark | alt |
| `doc001-10-command-light.png` | light | Command Center: four KPI tiles each with its own sparkline/meter, a **Ward Acuity Heatmap** where only W1 is populated (30/8), a 24h Capacity Forecast, and an **Active Deteriorations** rail listing three named patients with their NEWS2 | body |
| `doc001-10-command-dark.png` | dark | same, dark | alt |
| `adm001-06-command-light.png` / `-dark.png` | light / dark | the same screen as admin | **defect**, "No active deteriorations. House is stable." beside a tile reading 3 critical |
| `adm001-07-capacity-light.png` | light | What-If Simulator: five scenario presets, a horizon selector, three sliders, and a live "Projected occupancy vs capacity" chart with a **Model assumptions & citations** disclosure at the foot | body |
| `adm001-07-capacity-dark.png` | dark | same, dark | alt |
| `adm001-08-coding-light.png` | light | Revenue Integrity: an empty prompt, "Enter a patient ID to analyze coding & CDI opportunities", with the Patient ID + Analyze pair top-right | body |
| `adm001-08-coding-dark.png` | dark | same, dark | alt |
| `pha001-02-helix-light.png` | light | Pharmacy Operations: an Overview / Patient Board / Formulary segmented control over five stage counters (Pending Review, Verified, Scheduled, In Transit, Delivered, all 0) and a Pharmacist Work Queue reading "Queue clear, no pending orders" | body |
| `pha001-02-helix-dark.png` | dark | same, dark | alt |
| `pha001-03-pharmacy-verify-light.png` | light | Pharmacist Verification Queue: the scope sentence "AI / agentic medications awaiting pharmacist review **before they become active orders**", then "Queue clear" | body |
| `pha001-03-pharmacy-verify-dark.png` | dark | same, dark | alt |
| `doc001-07-scribe-light.png` | light | Scribe empty state: the mark, the sentence describing what it does, and a composer with **two** mic buttons (record / live) | body |
| `doc001-07-scribe-dark.png` | dark | same, dark | alt |
| `scribe-01-structured-dark.png` | dark | A transcript loaded: Language select, the raw dictation, then the three-control row, patient link ("No patient linked"), note type (SOAP Note) and **Structure Note** | body |
| `scribe-02-soap-dark.png` | dark | After structuring: a "Structured AI Note" card carrying a **Draft** chip, with DIALOGUE / SUBJECTIVE / OBJECTIVE sections where un-dictated fields are honestly filled with `[Not explicitly stated in dictation]` | **defect**, an empty black panel occupies the right half |
| `doc001-08-lens-light.png` | light | Lens: a single dashed drop target, "Upload Medical Image / Drag & drop an X-ray, ECG, CT scan, or clinical photo" plus a Browse files button. No result state was captured | body |
| `doc001-08-lens-dark.png` | dark | same, dark | alt |
| `bridge-01-home-dark.png` | dark | Patient Bridge (1512 × 1706, the one genuinely full-page capture): "Welcome, Geeta" over Your Status / Your Care Team / Your Vitals & Trends, each vital with a sparkline and an "In normal range" label, then Schedule Follow-Up, a My Records block ending in **Download my record (FHIR)**, and a plain-language ask box | body |
| `nur001-07-scribe-*`, `nur001-08-lens-*`, `nur001-09-forecast-*`, `nur001-10-coding-*`, `doc001-09-forecast-*`, `doc001-11-capacity-*`, `doc001-12-coding-*`, `adm001-*-full`, `pha001-*-full` | light/dark | the same panels under other roles, and the `-full` twins | ref / dup (46 files) |

---

## Shows a known defect

These captures record something wrong. **Do not present them as normal behaviour.** Each
entry says what is wrong and how you can tell from the image alone.

| File(s) | What is wrong |
|---|---|
| `sage-02-general-thinkingOFF-dark.png` | **The Sage XML contract leaks into the visible answer in the fast lane.** Below the prose, the message body literally renders `Details: <display_mode> concise <patient_facts_summary> None <local_policy_guidance> None <external_guidance> None <clinical_assessment> … <caveats> … <escalation> … <cited_sources> ["DOC-ID"]`. The careful-lane captures (`sage-01-general-thinkingON-dark.png`, `sage-02-general-thinking-dark.png`) render the same contract correctly, which is what makes this a lane-parity failure rather than a formatting slip. Note also that the leaked `<cited_sources>` value is the literal placeholder `"DOC-ID"`. |
| `relay-02-sent-dark.png`, `relay-03-wrongroom-dark.png` | **Messages render twice.** In `relay-02` the just-sent "Repeat U&E in the morning please, and chase the platelet trend." appears as two separate bubbles, both stamped 01:37 AM. In `relay-03` the duplication is worse: the insulin-rate message appears twice, and each copy carries its **own** wrong-patient warning panel, so the interlock is offered twice for one message. |
| `chart-01-patient-full-dark.png` | Two faults on one screen. (a) **The Sepsis Six timer is runaway**: it reads `226:42:54 elapsed · 60m target`, a 226-hour stopwatch against a one-hour bundle, with all six items Overdue and a "0% at risk" chip beside a NEWS2 13 CRITICAL patient. (b) **The identity strip says `67Y · FEMALE` for Rajesh Iyer**, who is `67y/Male` in `surgical-02-activecases-dark.png` and `67y Male` in `misc-01-downtime-dark.png` and `misc-02-handoff-dark.png`. The chart header is the odd one out. |
| `doc001-03-round-light.png`, `doc001-03-round-dark.png`, `round-01-dictate-dark.png`, `round-02-saved-dark.png` | **The dictation block overlaps the summary columns.** All three columns are cut mid-sentence, CURRENT ISSUES stops at "Ananya Kapoor (GM-1-GM-101) is", and a stray fragment of the clipped text ("Temp 38.2C, HR 140, RR 35, meets …") bleeds out from behind the footer button bar. The three columns and the dictation panel are stacked in the same space instead of sharing it. Present in every round capture, both themes. |
| `admin-10-flight-recorder-light.png`, `-dark.png` | Two faults. (a) **The timestamp column overprints the action name** in every row of the event stream, you get `8/8/2026 1:15:13 AMward_brief_viewed` rendered on top of itself. (b) **The RAW RECORD pane never resolves**: it is stuck on "Loading…" in both themes, in captures taken 10 minutes apart. |
| `admin-05-security-light.png`, `-dark.png` | **The Security tab says there are no security events while the Overview tab counts them.** Security reads "No security events / No authentication or permission events recorded yet"; `admin-00-overview-*.png`, captured minutes earlier, reports `authentication: 39`, `authorization: 3`, and an Auth Summary of 4 login failures, 35 successes, 1 patient-scope denial and 2 permission denials. One of the two panels is reading the wrong source. |
| `adm001-02-vigil-light.png`, `-dark.png` (and their `-full` twins) | **The admin ward board's metric tiles read 0 / 0 / 0 / 0% while the feed underneath lists 13 alerts**, four of which are named CRITICAL patients. The Sage card above them also claims "No patients on the deterioration watchlist." "Admitted · My Dept = 0" is defensible for a user with no department; "Critical 0" and "Bed Occupancy 0%" are not, on the same screen as four critical patients. |
| `adm001-06-command-light.png`, `-dark.png` | **The Active Deteriorations rail says "No active deteriorations. House is stable."** next to a KPI tile reading "CRITICAL PATIENTS 3 · 4 elevated · 23 stable". The doctor's capture of the same screen (`doc001-10-command-*.png`) populates the rail with three named patients, so the panel works, the admin scope does not reach it. |
| `admin-11-users-light.png`, `-dark.png` | **Placeholder rows are in the live user table.** Three of the 19 users are named `Dr U-A-5313F0`, `Dr U-B-5313F0`, `Dr U-C-5313F0` with matching ids, generated fixtures sitting in General Medicine alongside real-looking staff. Also worth reading off this screen: **MFA is Off for all 19 accounts**, including both admins. |
| `doc004-02-surgical-suite-light.png`, `doc004-03-surgical-suite-dark.png` (and twins) | **The Schedule tab reports 0 Surgeries on every day** while the Active Cases tab beside it carries a badge of 5 and lists five SCHEDULED cases with assigned ORs. Secondary: the week strip overflows its container, the fourth day column is cut off mid-word at the right edge with no scroll affordance. |
| `scribe-02-soap-dark.png` | **A large empty black panel occupies the right half of the structured-note area**, and the note card is squeezed into the left half. The card also renders a bare `✕` on its own line directly under the title. |
| `adm001-05-forecast-light.png`, `-dark.png` (and twins) | **The model-status badge prints its label twice**, it reads `• AI untrained Untrained`. Cosmetic, but it is on the one badge whose whole job is to tell a buyer the forecast model is not trained. |
| `orders-01-badid-dark.png` | Not a crash, a deliberately triggered bad-ID lookup, but **the error message is wrong for the cause**. An unknown patient id produces "Orders could not be loaded." with a **Retry** button, which tells the clinician the server failed and invites them to hammer it, when the actual problem is that no such patient exists. Compare `orders-02-loaded-dark.png`, where a real patient with no orders gets the correct "No orders for this patient yet." |
| `doc001-05-relay-light.png`, `adm001-03-relay-light.png` and every Relay list capture | **The room count badges read in the thousands**, 5428, 5147, 5093, 5236, 4734, 4804, 4543, on a five-bed ward whose threads are ten to fifty messages long. Two rooms in the same list carry single-digit badges (4, 5, 6), so the badge is not simply a total. Either the badge is counting something other than unread, or it is counting bulk soak-test rows. Unverified from the image alone; do not quote these numbers, and check the badge's source before documenting it. |
| `nurse-08-emar-administered-dark.png`, `nurse-09-administer-modal-dark.png` | **Capture failure, not a UI fault**, both are pixel-identical to `nurse-07-emar-due-dark.png`. The walkthrough never reached the administered state or the five-rights modal. Listed here because the filenames will otherwise be trusted. |
| `nur001-02-shift-light.png` and every nurse task list | **Not a defect, but do not quote the numbers.** "144h 47m late", "227h 42m late" and "55 not charted" are the honest arithmetic of a fixture last charted on 1 Aug against a clock reading 8 Aug. Anything derived from elapsed time in these captures is meaningless. |

## Correct empty states: do not mistake these for defects

`admin-03-trace-*`, `admin-04-ingestion-*`, `admin-06-alerts-*`, `pha001-02-helix-*`,
`pha001-03-pharmacy-verify-*`, `adm001-08-coding-*`, `doc001-04-orders-*`,
`doc001-08-lens-*`, `orders-02-loaded-dark.png`, `doc001-07-scribe-*`,
`doc004-02-surgical-suite-light.png`'s *empty day cards*. Each of these is a panel waiting
for an input it has not been given (a correlation id, a patient id, an uploaded file) or a
queue that is genuinely clear because ingestion is in simulation mode. They render an
explanatory sentence rather than a zero, which is the intended behaviour.

## Filenames that lie

| Filename says | Image actually is |
|---|---|
| `misc-01-downtime-**dark**.png` | a **light** page, the downtime snapshot is print-shaped and renders on white in either theme |
| `nurse-08-emar-**administered**-dark.png` | the *due* state, unchanged, identical to `nurse-07-emar-due-dark.png` |
| `nurse-09-**administer-modal**-dark.png` | no modal, identical to `nurse-07-emar-due-dark.png` |
| `chart-00-vigil-list-dark.png` | not a chart, the ward **list**, identical to `doc001-02-vigil-dark.png` (the filename is honest, the `chart-` prefix is not) |
| any `*-full.png` | not full-page, a 1512 × 950 viewport, identical to the file without the suffix |

## Duplicates

Pixel-identical groups. Keep the first of each; the rest carry no information.

| Keep | Identical to |
|---|---|
| `adm001-00-login-dark.png` | `doc001-00-login-dark.png`, `doc004-00-login-dark.png`, `nur001-00-login-dark.png`, `pha001-00-login-dark.png` |
| `adm001-00-login-light.png` | `doc001-00-login-light.png`, `doc004-00-login-light.png`, `nur001-00-login-light.png`, `pha001-00-login-light.png` |
| `admin-00-overview-light.png` | `adm001-10-admin-light.png` |
| `doc001-02-vigil-dark.png` | `chart-00-vigil-list-dark.png` |
| `doc001-03-round-dark.png` | `round-01-dictate-dark.png` |
| `doc004-01-after-login-dark.png` | `doc004-02-vigil-dark.png` |
| `nurse-07-emar-due-dark.png` | `nurse-08-emar-administered-dark.png`, `nurse-09-administer-modal-dark.png` |
| `pha001-01-after-login-dark.png` | `pha001-02-helix-dark.png` |
| `pha001-01-after-login-light.png` | `pha001-02-helix-light.png` |
| every `X.png` | its `X-full.png` twin, **71 files**, listed below |

<details>
<summary>The 71 redundant <code>-full</code> files</summary>

`adm001-02-vigil-dark-full` · `adm001-02-vigil-light-full` · `adm001-03-relay-dark-full` ·
`adm001-03-relay-light-full` · `adm001-04-sage-dark-full` · `adm001-04-sage-light-full` ·
`adm001-05-forecast-dark-full` · `adm001-05-forecast-light-full` ·
`adm001-06-command-dark-full` · `adm001-06-command-light-full` ·
`adm001-07-capacity-dark-full` · `adm001-07-capacity-light-full` ·
`adm001-08-coding-dark-full` · `adm001-08-coding-light-full` ·
`adm001-09-appointments-dark-full` · `adm001-09-appointments-light-full` ·
`adm001-10-admin-dark-full` · `adm001-10-admin-light-full` · `doc001-02-vigil-dark-full` ·
`doc001-02-vigil-light-full` · `doc001-03-round-dark-full` · `doc001-03-round-light-full` ·
`doc001-04-orders-dark-full` · `doc001-04-orders-light-full` · `doc001-05-relay-dark-full` ·
`doc001-05-relay-light-full` · `doc001-06-sage-dark-full` · `doc001-06-sage-light-full` ·
`doc001-07-scribe-dark-full` · `doc001-07-scribe-light-full` · `doc001-08-lens-dark-full` ·
`doc001-08-lens-light-full` · `doc001-09-forecast-dark-full` ·
`doc001-09-forecast-light-full` · `doc001-10-command-dark-full` ·
`doc001-10-command-light-full` · `doc001-11-capacity-dark-full` ·
`doc001-11-capacity-light-full` · `doc001-12-coding-dark-full` ·
`doc001-12-coding-light-full` · `doc001-13-appointments-dark-full` ·
`doc001-13-appointments-light-full` · `doc004-02-surgical-suite-light-full` ·
`doc004-02-vigil-dark-full` · `doc004-03-surgical-suite-dark-full` ·
`nur001-02-shift-dark-full` · `nur001-02-shift-light-full` · `nur001-03-vigil-dark-full` ·
`nur001-03-vigil-light-full` · `nur001-04-orders-dark-full` ·
`nur001-04-orders-light-full` · `nur001-05-relay-dark-full` · `nur001-05-relay-light-full` ·
`nur001-06-sage-dark-full` · `nur001-06-sage-light-full` · `nur001-07-scribe-dark-full` ·
`nur001-07-scribe-light-full` · `nur001-08-lens-dark-full` · `nur001-08-lens-light-full` ·
`nur001-09-forecast-dark-full` · `nur001-09-forecast-light-full` ·
`nur001-10-coding-dark-full` · `nur001-10-coding-light-full` ·
`nur001-11-appointments-dark-full` · `nur001-11-appointments-light-full` ·
`pha001-02-helix-dark-full` · `pha001-02-helix-light-full` ·
`pha001-03-pharmacy-verify-dark-full` · `pha001-03-pharmacy-verify-light-full` ·
`pha001-04-sage-dark-full` · `pha001-04-sage-light-full`

</details>

## Gaps: screens with no usable capture

Worth knowing before someone assumes the set is complete.

- **The five-rights administration modal** (`AdministerModal`), the single most
  safety-critical surface in `09-nurse-vitals-emar.md`. Its two captures are duplicates of
  the sheet behind it.
- **Order sets**, half of `12-orders.md`. Nothing at the top level.
- **The patient chart beyond the landing view**, no Notes, Story, Timeline, Medications,
  Lab/Diag reports, and no light theme at all.
- **Login: PIN pad, tablet setup, authenticator step, MFA enrolment**, four of the six
  documented faces in `02-login.md`.
- **Lens with a result**, **Revenue Integrity with a result**, **Forecast's other three
  tabs** (Ward Capacity, Discharge Board, Admission Patterns), **Helix's Patient Board and
  Formulary tabs**, **the Surgical Suite day agenda and case detail**.
- **The command palette, the shortcut sheet and the two shell modals**, four of the global
  overlays documented in `01-app-shell.md`.

## Subdirectories

Not indexed, not referenced by any chapter, kept for history:

- **`wavec/`**, 73 files, 8 Aug 2026, a before/after pair for a design change
  (`before-NN-*` / `after-NN-*`) across 12 screens in **three** themes: `light`, `dark` and
  a third `night` theme that the top-level set does not capture at all. If you need to see
  the night theme, it is only here.
- **`wavee/`**, 167 files, the wave-by-wave capture runs (`w1-` … `w9-`) plus an earlier
  role sweep. Includes the only images of order sets, the keyboard-shortcut sheet
  (`w3-09-shortcuts-*`, `w4-04-shortcuts-*`), the sepsis-six blocked state
  (`w5-01-sepsis-six-blocked-dark`), and a cold-start first load (`w9-01-coldstart-*`).

If any of those turn out to be needed, promote the file to the top level and add a row
here rather than linking into the subdirectory.
