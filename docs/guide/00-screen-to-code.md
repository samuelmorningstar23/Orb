# 00: Screen to code

**Two questions this file answers.**

1. *"I am looking at this thing on screen. Which file do I open?"* → **[Part one, by screen](#part-one--by-screen)**.
2. *"I am about to change this function. What breaks on screen?"* → **[Part two, by file](#part-two--by-file)**.

Part one is a router: fifteen screen files, each one a complete inventory of everything
visible on one screen, plus an inline quick-answer table for the elements people actually
ask about. Part two is the same body of knowledge turned inside out, every file and
function that puts pixels on screen, with the complete list of what it puts there, across
**all** screens at once. That second half does not exist in any single screen file and
cannot be reconstructed from one, which is the whole reason it is here.

**Scale of the index.** 1,092 contribution statements, covering 895 distinct
`path::function` pairs across 237 files. 116 of those functions drive more than one
screen, see [the dangerous ones](#the-dangerous-ones--functions-that-drive-more-than-one-screen).
(Counts are from the last full sweep. The 4 October 2026 offline pass removed the floating orb's
voice loop and the hotword hook, and the entries it touched were edited by hand.)

**Repo root for every path below:** `/Users/samuelmorningstar/Documents/Projects/Orb`.

---

# Part one: by screen

## The fifteen screen files

Find the screen you are looking at, open that file, search it for the words printed on
the element. Each file lists *every* visible thing on its screen with the exact
`file:line` that renders it.

| # | Screen file | The screen | What is on it |
|---|---|---|---|
| 01 | [screen-map/01-app-shell.md](screen-map/01-app-shell.md) | **The application shell**, present on every clinician screen | Left nav rail, top bar, panel region, and the four global overlays (Sage orb, command palette, shortcut sheet, shell modals) |
| 02 | [screen-map/02-login.md](screen-map/02-login.md) | **Login** | One card in four faces, staff password, patient, ward-tablet PIN pad, tablet setup, plus the authenticator-code step and the MFA enrolment modal |
| 03 | [screen-map/03-sage.md](screen-map/03-sage.md) | **Sage** (the AI assistant tab) | History sidebar, message list, provenance banners, the "Show details" evidence trail, chart-action cards, the composer and voice mode |
| 04 | [screen-map/04-relay-list.md](screen-map/04-relay-list.md) | **Messages**, before you open a room | Sort header, the "Action Required" alerts rail, search, the case-room list and the Department list |
| 05 | [screen-map/05-relay-thread.md](screen-map/05-relay-thread.md) | **Messages**, after you open a room | Message bubbles, structured and surgical alert cards, the wrong-room warning strip, typing line, composer, and the Sage ambient sidebar |
| 06 | [screen-map/06-vigil-board.md](screen-map/06-vigil-board.md) | **Patients**, the ward board | Metric tiles, the patient list, risk dots and percentages, EMERGENCY rows, "N NEW" badges, paging and the staleness banner |
| 07 | [screen-map/07-vigil-chart.md](screen-map/07-vigil-chart.md) | **Patients**, one patient's chart | Command bar, five vital cells and their sparklines, risk gauge and NEWS2 attribution, AI assessment, Sepsis Six, Clinical Course, lab trends, and every modal the chart opens |
| 08 | [screen-map/08-nurse-board.md](screen-map/08-nurse-board.md) | **My Shift**, the nurse shift board | Five count pills, the three-tab strip, the "Due now" task list, the patient-card grid |
| 09 | [screen-map/09-nurse-vitals-emar.md](screen-map/09-nurse-vitals-emar.md) | **My Shift**, the two bedside sheets | Observation entry with live NEWS2 scoring, the medication round sheet, and the five-rights administration modal |
| 10 | [screen-map/10-nurse-handover.md](screen-map/10-nurse-handover.md) | **My Shift**, I-PASS handover | The Handover tab (whole-list) and the full-page per-patient sheet at `/handoff/:patientId` |
| 11 | [screen-map/11-round-mode.md](screen-map/11-round-mode.md) | **Ward Round** | The queue rail, the one-screen bedside summary, dictation, the two round decisions, and the hand-on screen |
| 12 | [screen-map/12-orders.md](screen-map/12-orders.md) | **Orders** | Individual order entry (create form, order cards, status chips, print) and Order sets (protocol library, preview, apply) |
| 13 | [screen-map/13-admin.md](screen-map/13-admin.md) | **Admin** | Fourteen tabs, overview, pilot scorecard, audit logs, correlation trace, security, users, roster import, and the rest |
| 14 | [screen-map/14-surgical-appointments.md](screen-map/14-surgical-appointments.md) | **Surgical Suite** and **Appointments** | The three-tier surgical drill-down (week strip → day agenda → case detail), Active Cases, the emergency banner; and the appointments day-column board |
| 15 | [screen-map/15-analytics-pharmacy-scribe-lens-bridge.md](screen-map/15-analytics-pharmacy-scribe-lens-bridge.md) | **Seven smaller screens** | Forecast, Command Center, Pulse, Helix/Pharmacy, the Pharmacist Verify Queue, Scribe, Lens, and the patient-facing Bridge |

**Not sure which screen you are on?** The shell prints the pillar name in the top bar
(`src/pages/ClinicianShell.tsx::ClinicianShell`), and every panel is mounted in a
`<div id="panel-{tab}" role="tabpanel">`, read the `id` in devtools and match it to the
table above.

---

## Quick-answer table

The ~80 elements people ask about most. Element → which screen file has the full entry →
the component that renders it → the server function that decides what it says. A dash
means the element is decided entirely on the client.

For anything not here, open the screen file: these tables are the shortlist, the screen
files are the complete inventory.

### The shell (visible on every clinician screen)

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| Left nav item; which tabs your role sees | 01 | `src/pages/ClinicianShell.tsx::TABS`, `::getAccessibleTabs` | `server/permissions.py::ROLE_PERMISSIONS` |
| Page title and subtitle (top bar) | 01 | `src/pages/ClinicianShell.tsx::ClinicianShell` | n/a |
| AI status chip and the model popover | 01 | `src/components/AiStatusChip.tsx::AiStatusChip` | `server/main.py::health_models` |
| Offline / Syncing / Synced / Sync-failed badge | 01 | `src/pages/ClinicianShell.tsx::ClinicianShell` |, (client queue, `src/api/client.ts::processOfflineQueue`) |
| Command palette (⌘K) result rows | 01 | `src/components/CommandPalette.tsx::CommandPalette` | `server/routes/search_routes.py::search_patients` |
| Sage floating orb (text-only, opens the Sage tab) | 01 | `src/components/sage/SageFloatingAssistant.tsx::SageFloatingAssistant` | n/a |
| Pulse panel (season and conditions: This season, Coming up, review status) | 01, 15 | `src/components/PulseWidget.tsx::PulseWidget` | `server/services/seasonal_calendar.py::season_report` |
| Admit Patient modal and the one-time Bridge credential | 01 | `src/components/AdmitPatientModal.tsx::AdmitPatientModal` | `server/services/admission_service.py::admit_patient` |
| Staleness banner, "Offline, not live" | 06, 07, 08, 09, 10 | `src/components/OfflineBanner.tsx::OfflineBanner` | n/a |

### Login

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| Error banner, "Invalid credentials", lockouts | 02 | `src/pages/Login.tsx::Login` | `server/routes/auth_routes.py::login` |
| PIN pad on a bound ward tablet | 02 | `src/pages/Login.tsx::Login` | `server/routes/auth_routes.py::pin_login` |
| MFA enrolment modal (setup key, provisioning URI) | 01, 02 | `src/components/MfaEnrollmentModal.tsx::MfaEnrollmentModal` | `server/routes/auth_routes.py::mfa_enroll_begin` |

### Sage

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| The streaming answer text | 03 | `src/components/sage/SageMessage.tsx::SageMessage` | `server/routes/sage_routes.py::sage_query` |
| "Show details", citations, sources, evidence trail | 03 | `src/components/sage/SageMessage.tsx::SageDetailsExpander`, `::SageSources` | `server/services/sage_service.py::get_relevant_context` |
| ⚡ fast-answer / 🛡️ safety-floor banner | 03 | `src/api/thinkingMode.ts::describeAnswerProvenance` | `server/services/thinking_mode.py::decide` |
| Thinking / fast toggle pill | 03 | `src/components/sage/SageComposer.tsx::SageComposer` | `server/services/thinking_mode.py::fast_mode_available` |
| Chart-action card, "Approve & Send to Helix" | 03 | `src/components/sage/SageMessage.tsx::SageActionCard` | `server/routes/helix_routes.py::approve_helix_order` |

### Messages (Relay)

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| Case-room row, name, risk chip, unread badge | 04 | `src/components/relay/CaseList.tsx::CaseList` | `server/services/relay_service.py::get_channels_for_user` |
| "⚠️ ACTION REQUIRED" alerts rail | 04 | `src/components/relay/AlertsSection.tsx::AlertsSection` | `server/services/relay_service.py::get_user_alerts` |
| Alert Confirm / Escalate buttons | 04, 05 | `src/pages/panels/RelayPanel.tsx::handleResolveAlert` | `server/routes/relay_routes.py::_apply_alert_resolution` |
| Message bubble, sender, role chip, AUTO badge | 05 | `src/pages/panels/RelayPanel.tsx::RelayPanel` | `server/services/relay_service.py::_message_row_to_dict` |
| Wrong-room amber warning strip | 05 | `src/components/relay/RoomFitWarning.tsx::RoomFitWarning` | `server/services/room_fit_service.py::check_message` |
| Sage ambient sidebar, risk flags, suggested actions | 05 | `src/components/relay/SageAmbientSidebar.tsx::SageAmbientSidebar` | `server/routes/relay_routes.py::get_sage_actions` |

### Patients (Vigil): the board

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| Patient row, risk dot and risk percentage | 06 | `src/pages/panels/VigilPanel.tsx::PatientRow` | `server/services/vigil_service.py::compute_news2` |
| Solid red EMERGENCY row and badge | 06 | `src/pages/panels/VigilPanel.tsx::isEmergency` | `server/services/vigil_service.py::compute_news2` |
| "N NEW" badge on a row | 06 | `src/pages/panels/VigilPanel.tsx::loadUnseen` | `server/services/patient_timeline_service.py::unseen_counts_for_scope` |
| The four metric tiles above the list | 06 | `src/pages/panels/VigilPanel.tsx::VigilPanel` | `server/routes/vigil_routes.py::list_patients` |
| Which patients appear at all | 06 | n/a | `server/permissions.py::department_scope` |

### Patients (Vigil): one patient's chart

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| Five vital cells and their hover sparklines | 07 | `src/pages/panels/VigilPanel.tsx::PatientDetailView`, `::renderSparkline` | `server/routes/vigil_routes.py::get_patient` |
| Live vital movement without a reload | 07 | n/a | `server/ws/realtime.py::emit_patient_vitals` |
| Risk gauge ring and the NEWS2 number | 07 | `src/pages/panels/VigilPanel.tsx::RiskGauge` | `server/services/vigil_service.py::get_patient_risk` |
| "Why NEWS2 n?" per-parameter table | 07 | `src/pages/panels/VigilPanel.tsx::News2Attribution` | `server/services/vigil_service.py::compute_news2` |
| MedGemma AI Assessment and the Glass Window | 07 | `src/pages/panels/VigilPanel.tsx::AiNarrative`, `::GlassWindow` | `server/services/vigil_service.py::generate_patient_analysis` |
| Sepsis Six care-bundle card and its timer | 07 | `src/components/vigil/CareBundleTimer.tsx::CareBundleTimer` | `server/services/sepsis_bundle_service.py::_decorate` |
| Clinical Course timeline | 07 | `src/components/ClinicalCourseTimeline.tsx::ClinicalCourseTimeline` | `server/services/timeline_service.py::get_timeline_projection` |
| Lab Trends tiles and the ESTIMATED badge | 07 | `src/pages/panels/VigilPanel.tsx::syntheticLabTrendData` | `server/routes/vigil_routes.py::get_patient` |
| Discharge summary, timeline and QR | 07 | `src/components/DischargePrintView.tsx::DischargePrintView` | `server/services/discharge_service.py::generate_qr_code` |
| "Patient not found" on a cross-ward chart | 07 | n/a | `server/permissions.py::require_patient_access` |

### My Shift (nurse)

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| Task row, OBS/MED chip, "40m late" | 08 | `src/pages/panels/nurse/NursePanel.tsx::buildTasks`, `::renderTaskRow` | `server/services/nurse_shift_service.py::_build_med_task` |
| The five count pills | 08 | `src/pages/panels/nurse/NursePanel.tsx::NursePanel` | `server/services/nurse_shift_service.py::build_board` |
| NEWS2 badge (task rows, patient cards, handover rows) | 08, 09, 10 | `src/pages/panels/nurse/News2Badge.tsx::News2Badge` | `server/services/vigil_service.py::compute_news2` |
| When an observation turns overdue | 08 | n/a | `server/services/nurse_shift_service.py::obs_schedule` |
| Live NEWS2 block inside the obs sheet | 09 | `src/pages/panels/nurse/ObsEntrySheet.tsx::ObsEntrySheet` | `server/routes/nurse_routes.py::preview_news2` |
| "+N" score chip on each vital field | 09 | `src/pages/panels/nurse/ObsEntrySheet.tsx::pointsFor` | `server/services/vigil_service.py::compute_news2` |
| Which med-round group a dose lands in | 09 | `src/pages/panels/nurse/MedRoundSheet.tsx::groups` | `server/services/nurse_shift_service.py::parse_frequency` |
| Five-rights Administer modal | 07, 09 | `src/components/vigil/AdministerModal.tsx::AdministerModal` | `server/routes/emar_routes.py::administer_medication` |
| Allergy block, "allergy interlock: X conflicts with…" | 09 | `src/components/vigil/AdministerModal.tsx::submit` | `server/services/allergy_interlock.py::screen_for_administration` |
| Dose-guard refusal, "no adult indication reaches this…" | 09, 12 | `src/components/vigil/AdministerModal.tsx::submit` | `server/services/drug_safety_service.py::evaluate_dose` |
| The five I-PASS rows | 10 | `src/pages/panels/nurse/HandoverSheet.tsx::HandoverSheet` | `server/services/ipass_service.py::build_ipass` |
| The full-page I-PASS sheet, `/handoff/:patientId` | 10 | `src/components/handoff/IpassHandoffView.tsx::IpassHandoffView` | `server/routes/ipass_routes.py::get_draft` |

### Ward Round

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| Queue rail and "N of M seen" | 11 | `src/components/round/RoundQueueRail.tsx::RoundQueueRail`, `roundQueue.ts::progressOf` | n/a |
| The one-screen bedside summary (three columns) | 11 | `src/components/round/RoundSummaryView.tsx::RoundSummaryView` | `server/services/nurse_shift_service.py::build_board` |
| "Since HH:00" overnight event rows | 11 | `src/components/round/roundSummary.ts::changesSince` | `server/services/patient_story_service.py::get_patient_story` |
| Dictate the plan, mic, transcript | 11 | `src/components/round/RoundDictation.tsx::RoundDictation` | `server/routes/scribe_routes.py::transcribe` |
| "Save plan to chart" | 11 | `src/pages/panels/RoundPanel.tsx::savePlan` | `server/routes/vigil_routes.py::add_patient_note` |

### Orders

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| Order card and its status chip | 12 | `src/components/orders/OrdersPanel.tsx::OrdersPanel`, `::CHIP_CLASS` | `server/services/orders_service.py::list_orders` |
| "→ active / in progress / completed" button | 12 | `src/components/orders/OrdersPanel.tsx::nextStatus` | `server/services/orders_service.py::LEGAL_TRANSITIONS` |
| Code validation ✓ / ✗ hint | 12 | `src/components/orders/OrdersPanel.tsx::validateCode` | `server/services/terminology_service.py::validate_code` |
| Drug-safety blocking modal (the 428) | 07, 12 | `src/components/vigil/SafetyAlertModal.tsx::SafetyAlertModal` | `server/services/medication_safety_gate.py::enforce_medication_safety` |
| Order-set preview, Cleared / STOPPED chips | 12 | `src/components/orders/OrderSetsPanel.tsx::OrderSetsPanel` | `server/services/order_sets_service.py::screen_items` |
| "Nothing was ordered." blocked callout | 12 | `src/components/orders/OrderSetsPanel.tsx::runApply` | `server/services/order_sets_service.py::OrderSetBlocked` |
| Patient picker (used by both order views) | 12 | `src/components/common/PatientPicker.tsx::PatientPicker` | `src/api/client.ts::getPatients` |

### Admin

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| The fourteen tab pills | 13 | `src/pages/panels/AdminPanel.tsx::AdminPanel` | `server/permissions.py::ROLE_PERMISSIONS` |
| Overview stat cards | 13 | `src/pages/panels/admin/AdminOverview.tsx::AdminOverview` | `server/routes/admin_routes.py::admin_overview` |
| Pilot Scorecard metric bands | 13 | `src/pages/panels/admin/Scorecard.tsx::MetricBand`, `::Figure` | `server/services/shadow_scorecard_service.py::scorecard` |
| Audit Logs table and the correlation trace | 13 | `src/pages/panels/admin/AuditLogTable.tsx::AuditLogTable`, `CorrelationTrace.tsx::CorrelationTrace` | `server/routes/audit_routes.py::get_audit_logs` |
| Users table, Role / Status / MFA chips | 13 | `src/pages/panels/admin/UsersPanel.tsx::UsersPanel` | `server/routes/admin_users_routes.py::list_users` |
| Roster import, the three-step wizard | 13 | `src/pages/panels/admin/RosterImport.tsx::RosterImport` | `server/services/roster_import.py::plan` |

### Surgical Suite and Appointments

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| Week strip day cards | 14 | `src/pages/panels/SurgicalSuitePanel.tsx::SurgicalSuitePanel` | `server/routes/surgery_routes.py::get_surgery_schedule` |
| Team assignment chips and their four colours | 14 | `src/pages/panels/SurgicalSuitePanel.tsx::SurgicalSuitePanel` | `server/routes/surgery_routes.py::surgical_team_response` |
| Streamed AI Risk Assessment | 14 | `src/pages/panels/SurgicalSuitePanel.tsx::SurgicalSuitePanel` | `server/services/surgery_service.py::generate_risk_assessment` |
| Appointments day columns and the TODAY chip | 14 | `src/pages/panels/AppointmentsPanel.tsx::AppointmentsPanel`, `src/lib/datetime.ts::localDateKey` | `server/services/forecast_service.py::get_appointments` |

### Forecast, Command Center, Pharmacy, Scribe, Lens, Bridge

| Element on screen | Screen file | Component file :: function | Server function |
|---|---|---|---|
| Forecast tiles and the 7-day occupancy chart | 15 | `src/pages/panels/ForecastPanel.tsx::ForecastPanel`, `::MetricCard` | `server/services/forecast_service.py::predict_hospital_flow` |
| Command Center tiles and the acuity heatmap | 15 | `src/components/command/CommandCenter.tsx::CommandCenter` | `server/services/command_center_service.py::house_snapshot` |
| Pharmacist Work Queue and order status badges | 15 | `src/components/helix/HelixDashboard.tsx::HelixDashboard`, `HelixOrderReviewModal.tsx::getStatusBadge` | `server/services/helix_service.py::get_all_orders` |
| Pharmacist Verification Queue, Verify / Reject | 15 | `src/components/helix/PharmacyVerifyQueue.tsx::PharmacyVerifyQueue` | `server/services/pharmacy_verification_service.py::verify_medication` |
| Structured AI Note modal and the Sign gate | 15 | `src/pages/panels/ScribePanel.tsx::StructuredNoteModal` | `server/routes/scribe_routes.py::sign_note` |
| Lens AI Reference Read and Confirm & File | 15 | `src/pages/panels/LensPanel.tsx::LensPanel` | `server/services/lens_service.py::analyze_image_stream` |
| Bridge, "Your Status" card | 15 | `src/pages/PatientShell.tsx::PatientShell` | `server/services/bridge_service.py::get_patient_status_simple` |
| Bridge, the Ask bar answer | 15 | `src/pages/PatientShell.tsx::PatientShell` | `server/services/bridge_service.py::answer_patient_question` |

---

# Part two: by file

This is the inversion. Every file and function named anywhere in the fifteen screen maps,
with the complete list of visible elements it affects, **across all screens at once**.

**How to read an entry.** A file gets a heading. Under it, each function is a bullet, and
under that, one line per screen it touches, listing what it puts on that screen. A ⚠ and a
screen count on a function means it drives more than one screen; if you are editing it,
you are editing all of them.

**What "affects" means here.** It is deliberately broad: renders it, decides its text,
decides its colour, decides whether it appears at all, or decides the error shown in its
place. A CSS file with no `::` is listed as *(whole file / stylesheet)*.

---

## The dangerous ones: functions that drive more than one screen

116 of the 895 functions in this index put pixels on
**more than one screen**. Nothing in a single screen file tells you that. These are the ones where
a "small fix for the nurse board" silently changes the ward round, the handover sheet and the
doctor's chart at the same time.

Read this table before you edit anything in it.

| Screens | `path::function` | What it drives |
|---|---|---|
| **12** | `src/api/client.ts::apiFetch` | Every server-error sentence in the product. Change its error shape and twelve screens change wording at once. |
| **8** | `server/permissions.py::ROLE_PERMISSIONS` | Which screens, tabs and buttons exist for a role at all. |
| **8** | `server/permissions.py::require_permission` | The 403, and the "Insufficient permissions" text that replaces a whole panel. |
| **8** | `src/pages/ClinicianShell.tsx::ClinicianShell` | The frame: nav rail, top bar, sync badges, and the tabpanel every screen mounts into. |
| **8** | `src/pages/ClinicianShell.tsx::getAccessibleTabs` | Whether a role sees a tab at all, and which screen it lands on at sign-in. |
| **6** | `server/permissions.py::require_patient_access` | The 404-not-403 an out-of-ward patient produces, and the PHI-read audit row behind every chart open. |
| **6** | `server/services/vigil_service.py::compute_news2` | Every NEWS2 number, band colour and driver phrase in the product, board, chart, nurse badges, handover, round. |
| **6** | `src/pages/ClinicianShell.tsx::TABS` | Every left-nav label, icon and pillar grouping. |
| **5** | `src/components/OfflineBanner.tsx::OfflineBanner` | The "Offline, not live" staleness banner on all five cached surfaces. |
| **5** | `src/hooks/useOfflineSnapshot.ts::useOfflineSnapshot` | Whether a screen shows live data or a labelled cached copy. |
| **5** | `src/lib/offline/writeGuard.ts::assertOnlineForWrite` | Every refusal to write while offline. |
| **4** | `server/permissions.py::department_scope` | Which patients appear on any list, anywhere. |
| **4** | `src/api/client.ts::getPatients` | Every patient picker, dropdown and round queue. |
| **4** | `src/components/OfflineBanner.tsx::ageInWords` | The "N minutes old" phrase inside each staleness banner. |
| **3** | `server/routes/nurse_routes.py::get_board` | One endpoint feeds the shift board, both bedside sheets, and the ward-round summary. |
| **3** | `server/routes/scribe_routes.py::transcribe` | Every dictated transcript: Sage voice, the ward-round plan, Scribe. |
| **3** | `server/services/nurse_shift_service.py::_build_med_task` | Whether a dose reads as due, late, or greyed out as unverified. |
| **3** | `server/services/nurse_shift_service.py::build_board` | The count pills, patient cards, drug rows, and the ward-round summary band. |
| **3** | `server/services/nurse_shift_service.py::obs_schedule` | The RCP observation interval, and therefore when observations read as overdue. |
| **3** | `src/api/client.ts::buildHeaders` | Whether an authenticated fetch succeeds on the screens that bypass the shared client. |
| **3** | `src/api/client.ts::transcribeAudio` | The client half of every dictation path. |
| **3** | `src/api/nurse.ts::getShiftBoard` | The single call behind the nurse board, both bedside sheets, and the ward round. |
| **3** | `src/contexts/SageRuntimeContext.tsx::SageRuntimeProvider` | Microphone arbitration, and the local speech engine check that refuses voice up front. |
| **3** | `src/hooks/useFocusTrap.ts::useFocusTrap` | Escape-to-close, Tab containment and scroll lock in every modal and sheet. |
| **3** | `src/index.css` *(whole file)* | `.glass-card`, the fade/slide animations, and every status colour token. |
| **3** | `src/lib/datetime.ts::formatUTCDateTime` | Every rendered UTC timestamp. |
| **3** | `src/lib/offline/writeGuard.ts::offlineRefusalMessage` | The exact wording of every offline refusal. |
| **3** | `src/pages/Vigil.css` *(whole file)* | Board rows, the chart command bar, and the numerals on the Appointments cards. |
| **3** | `src/pages/panels/nurse/News2Badge.tsx::News2Badge` | The NEWS2 tile on task rows, patient cards, the obs sheet and handover rows. |
| **3** | `src/pages/panels/nurse/NursePanel.css` *(whole file)* | Every nurse surface: the board, both sheets, the handover tab. |
| **3** | `src/pages/panels/nurse/NursePanel.tsx::NursePanel` | The board itself, the buttons that open both bedside sheets, and the handover tab wrapper. |
| **3** | `src/pages/panels/nurse/time.ts::clockTime` | Every "HH:MM" a nurse reads. |

<details>
<summary>The remaining 81 two-screen functions</summary>

| Screens | `path::function` |
|---|---|
| App shell, Login | `server/routes/auth_routes.py::mfa_enroll_begin` |
| App shell, Login | `server/routes/auth_routes.py::mfa_enroll_confirm` |
| Nurse board, Nurse handover | `server/routes/ipass_routes.py::get_draft` |
| Nurse board, Nurse handover | `server/routes/ipass_routes.py::save_draft` |
| Nurse board, Nurse vitals/eMAR | `server/routes/nurse_routes.py::preview_news2` |
| Nurse board, Nurse vitals/eMAR | `server/routes/nurse_routes.py::record_observations` |
| App shell, Analytics/Pharmacy/Scribe/Lens/Bridge | `server/routes/pulse_routes.py::pulse_season` |
| Relay list, Relay thread | `server/routes/relay_routes.py::_apply_alert_resolution` |
| Relay list, Relay thread | `server/routes/relay_routes.py::list_channels` |
| Relay list, Relay thread | `server/routes/relay_routes.py::mark_read` |
| Relay list, Relay thread | `server/routes/relay_routes.py::resolve_alert` |
| Sage, Round mode | `server/routes/sage_routes.py::get_ai_posture` |
| Relay thread, Surgical/Appointments | `server/routes/surgery_routes.py::surgical_team_response` |
| Vigil chart, Round mode | `server/routes/vigil_routes.py::_insert_patient_note` |
| Vigil chart, Round mode | `server/routes/vigil_routes.py::add_patient_note` |
| Vigil chart, Round mode | `server/routes/vigil_routes.py::get_patient_story_route` |
| Vigil board, Round mode | `server/routes/vigil_routes.py::list_patients` |
| Sage, Round mode | `server/services/ai_governance.py::ai_posture` |
| Login, Nurse handover | `server/services/audit_service.py::log_audit_event` |
| Nurse vitals/eMAR, Orders | `server/services/drug_safety_service.py::evaluate_dose` |
| Nurse board, Nurse handover | `server/services/ipass_service.py::_build_action_list` |
| Nurse board, Nurse handover | `server/services/ipass_service.py::_build_illness_severity` |
| Nurse board, Nurse handover | `server/services/ipass_service.py::_build_patient_summary` |
| Nurse board, Nurse handover | `server/services/ipass_service.py::_build_situation_awareness` |
| Nurse board, Nurse handover | `server/services/ipass_service.py::_build_synthesis` |
| Nurse board, Nurse handover | `server/services/ipass_service.py::build_ipass` |
| Nurse board, Nurse handover | `server/services/ipass_service.py::save_ipass_handoff` |
| Vigil chart, Orders | `server/services/medication_safety_gate.py::enforce_medication_safety` |
| Nurse board, Round mode | `server/services/nurse_shift_service.py::_load_board_rows` |
| Nurse board, Nurse vitals/eMAR | `server/services/nurse_shift_service.py::parse_frequency` |
| Nurse board, Nurse vitals/eMAR | `server/services/nurse_shift_service.py::record_observation` |
| App shell, Analytics/Pharmacy/Scribe/Lens/Bridge | `server/services/seasonal_calendar.py::season_report` |
| Relay list, Relay thread | `server/services/relay_service.py::can_access_channel` |
| Relay list, Relay thread | `server/services/relay_service.py::get_channels_for_user` |
| Relay list, Relay thread | `server/services/relay_service.py::mark_channel_read` |
| Vigil chart, Nurse handover | `server/services/sepsis_bundle_service.py::_decorate` |
| Vigil chart, Nurse handover | `server/services/sepsis_bundle_service.py::get_bundles_for_patient` |
| Vigil chart, Nurse handover | `server/services/vigil_service.py::get_patient_risk` |
| Vigil chart, Nurse handover | `server/services/vigil_service.py::get_vitals_trend` |
| Relay list, Relay thread | `server/ws/realtime.py::_user_can_access_channel` |
| App shell, Login | `src/App.tsx::App` |
| Vigil chart, Round mode | `src/api/client.ts::addClinicalNote` |
| Relay list, Relay thread | `src/api/client.ts::getChannels` |
| Sage, Round mode | `src/api/client.ts::getSageAiPosture` |
| Relay list, Relay thread | `src/api/client.ts::markChannelRead` |
| Relay list, Relay thread | `src/api/client.ts::resolveAlert` |
| Relay thread, Surgical/Appointments | `src/api/client.ts::socket` |
| Nurse board, Nurse handover | `src/api/ipass.ts::getIpassDraft` |
| Nurse board, Nurse handover | `src/api/ipass.ts::saveIpass` |
| Nurse board, Nurse handover | `src/api/nurse.ts::currentUserName` |
| App shell, Login | `src/components/MfaEnrollmentModal.tsx::MfaEnrollmentModal` |
| App shell, Login | `src/components/MfaEnrollmentModal.tsx::groupSecret` |
| App shell, Login | `src/components/OrbLogo.css` *(whole file)* |
| App shell, Login | `src/components/OrbLogo.tsx::OrbLogo` |
| App shell, Analytics/Pharmacy/Scribe/Lens/Bridge | `src/components/PulseWidget.tsx::PulseWidget` |
| Vigil chart, Nurse vitals/eMAR | `src/components/vigil/AdministerModal.tsx::AdministerModal` |
| Vigil chart, Orders | `src/components/vigil/SafetyAlertModal.tsx::SafetyAlertModal` |
| Vigil board, Nurse board | `src/hooks/useOfflineStatus.ts::useOfflineStatus` |
| Relay list, Vigil board | `src/hooks/useVisiblePolling.ts::useVisiblePolling` |
| App shell, Login | `src/i18n/locales/en.json` *(whole file)* |
| App shell, Login | `src/i18n/locales/hi.json` *(whole file)* |
| Vigil chart, Round mode | `src/lib/datetime.ts::formatUTCTime` |
| Relay thread, Round mode | `src/lib/datetime.ts::parseUTC` |
| App shell, Nurse board | `src/lib/tablist.ts::rovingTabKeyDown` |
| App shell, Vigil board | `src/pages/ClinicianShell.css` *(whole file)* |
| App shell, Surgical/Appointments | `src/pages/ClinicianShell.tsx::PanelErrorBoundary` |
| Relay list, Relay thread | `src/pages/panels/RelayPanel.tsx::RelayPanel` |
| Relay list, Relay thread | `src/pages/panels/RelayPanel.tsx::handleResolveAlert` |
| Relay list, Relay thread | `src/pages/panels/RelayPanel.tsx::loadChannels` |
| Relay list, Relay thread | `src/pages/panels/RelayPanel.tsx::loadMessages` |
| Nurse board, Nurse handover | `src/pages/panels/nurse/HandoverSheet.tsx::HandoverSheet` |
| Nurse board, Nurse handover | `src/pages/panels/nurse/HandoverSheet.tsx::loadDraft` |
| Nurse board, Nurse handover | `src/pages/panels/nurse/HandoverSheet.tsx::save` |
| Nurse board, Nurse handover | `src/pages/panels/nurse/HandoverSheet.tsx::setNote` |
| Nurse board, Nurse vitals/eMAR | `src/pages/panels/nurse/MedRoundSheet.tsx::MedRoundSheet` |
| Nurse board, Nurse vitals/eMAR | `src/pages/panels/nurse/MedRoundSheet.tsx::groups` |
| Nurse board, Nurse vitals/eMAR | `src/pages/panels/nurse/ObsEntrySheet.tsx::ObsEntrySheet` |
| Nurse board, Nurse vitals/eMAR | `src/pages/panels/nurse/time.ts::dueLabel` |
| Nurse board, Nurse vitals/eMAR | `src/pages/panels/nurse/time.ts::relativeMinutes` |
| Nurse handover, Orders | `src/print.css` *(whole file)* |

</details>

### The top of that list, in detail

#### `src/api/client.ts::apiFetch`: 12 screens

- **App shell**, Command palette result rows; Pulse panel error state; MFA modal error strip; Admit Patient modal error strip
- **Login**, error banner text for every server-side failure; MFA enrolment modal trigger (lifts `X-MFA-Enrollment-Ticket`)
- **Relay list**, "Alerts could not be loaded." message; alert inline failure line; silent-empty channel lists on failure
- **Relay thread**, the error text inside the thread-load error box, alert inline error, warning inline error and surgical inline error
- **Nurse board**, the text of the red board-error notice; the "Insufficient permissions" wording on a 403
- **Nurse vitals/eMAR**, the sentence that appears in the red error strip for every 4xx/428 guard
- **Nurse handover**, the wording of every draft/save failure message; the 403 permission error; the 401 re-auth modal
- **Round mode**, the error text inside both degraded strips and the fatal error card; "Session expired"
- **Orders**, the typed 428 that raises the blocking safety modal; the structured `order_set_blocked` detail behind the per-item blocked list; the "Insufficient permissions" text; the message shown in the form's red error line; session-expiry handling
- **Admin**, every Admin panel's error message text; 403 permission message; 401 session-expired dispatch
- **Surgical/Appointments**, the "Insufficient permissions" text behind every Surgical Suite failure
- **Analytics/Pharmacy/Scribe/Lens/Bridge**, data behind Command Center, Helix, Bridge status/reports and appointment calls

#### `server/permissions.py::ROLE_PERMISSIONS`: 8 screens

- **App shell**, Command palette result rows; Pulse panel; AI configuration popover Load/Unload buttons; Admit Patient modal submit
- **Relay list**, whether the channel lists and the alerts rail load at all; "Alerts could not be loaded." message for a role without relay.read
- **Relay thread**, whether the thread opens at all; whether Acknowledge/Dismiss/Confirm succeed or raise a 403 alert
- **Nurse vitals/eMAR**, whether the save and the Given button work at all for a given role
- **Nurse handover**, which roles can open the document (`vitals.read`) and which can save it (`scribe.create_draft`)
- **Orders**, whether a role can read the order list, create orders, or preview/apply a set
- **Admin**, whether the Admin sidebar item exists at all; which Admin tabs return data
- **Surgical/Appointments**, whether a role can open either screen, and whether the four Surgical Suite mutation buttons succeed

#### `server/permissions.py::require_permission`: 8 screens

- **App shell**, Command palette empty state for pharmacists; Pulse panel error for pharmacists; AI configuration popover Load/Unload for non-admins; Admit Patient modal error strip
- **Relay list**, 403 behind the "Alerts could not be loaded." message and the silently empty channel lists
- **Vigil board**, 403 board-failure block for a role lacking patient.read_all
- **Nurse board**, the red "Insufficient permissions" notice in place of a board
- **Nurse handover**, the "Insufficient permissions" message; the admin's ability to read but not save a handover; the `permission_denied` audit row
- **Orders**, the "Insufficient permissions" message on create, preview and apply
- **Admin**, the 403 message shown in every Admin error state; the `permission_denied` rows listed on the Security tab
- **Surgical/Appointments**, the "Insufficient permissions" text behind the Surgical Suite error banner; the generic "Failed to load appointments" sentence for a role without forecast.read

#### `src/pages/ClinicianShell.tsx::ClinicianShell`: 8 screens

- **App shell**, Pillar navigation rail; Nav item; Active nav item indicator; Nav item hover tooltip; Admit Patient button; User avatar disc; User name; Role · department line; Security (padlock) button; Sign Out button; Page title; Page subtitle; Offline badge, saved copy read-only; Offline badge, N pending; Syncing badge; Synced badge; Sync-failed badge; Sync-failed dismiss ✕; Keyboard shortcuts button; Language toggle; Theme toggle; Panel region (tabpanel wrappers); "Loading panel..." fallback
- **Sage**, Sage nav tab; Sage tab panel mount; userName prop feeding the greeting
- **Relay list**, Messages nav item and icon; the `panel-relay` tabpanel wrapper that keeps this screen mounted
- **Vigil board**, Vigil tab panel mount; `role` prop that gates the Bed Occupancy tile
- **Nurse board**, shift tab panel mount; mount-on-first-view so the board does not fetch until visited
- **Round mode**, Ward Round panel mount; Ward Round panel error boundary
- **Admin**, Admin panel mount; admin panel error boundary
- **Analytics/Pharmacy/Scribe/Lens/Bridge**, panel mount points for Forecast/Command/Helix/Verify Queue/Scribe/Lens; Pulse trigger button in the top bar; "Loading panel..." Suspense fallback

#### `src/pages/ClinicianShell.tsx::getAccessibleTabs`: 8 screens

- **App shell**, Pillar navigation rail; Nav item; Keyboard shortcuts sheet (Go to list); Landing tab on sign-in
- **Relay list**, whether the Messages tab (and therefore this whole screen) exists for the signed-in role
- **Vigil board**, presence or absence of the Patients tab per role; tab ordering
- **Nurse board**, presence of the My Shift tab (nurse only); the shift board being the nurse's landing screen
- **Round mode**, Ward Round nav item visibility (doctor/surgeon only)
- **Orders**, whether the Orders tab exists at all for the signed-in role (doctor and nurse only)
- **Admin**, Admin sidebar item (admin role only)
- **Surgical/Appointments**, whether a user sees Surgical Suite, Appointments, or neither

#### `server/permissions.py::require_patient_access`: 6 screens

- **Vigil chart**, the "Patient not found" screen shown for a cross-ward chart; the audited `patient_chart_viewed` row on every open
- **Nurse board**, the handover draft error notice for an out-of-ward patient; the observation sheet's save refusal for one
- **Nurse vitals/eMAR**, 404 (not 403) in the red note when a nurse charts against a patient outside her ward
- **Nurse handover**, the 404 for an out-of-ward patient (never a 403); the `ipass_draft_viewed` PHI-read row; the `patient_scope_denied` row
- **Round mode**, "Overnight events unavailable" (404 for an off-ward patient); inline save error on an off-ward plan save
- **Orders**, the 404 behind the "Orders could not be loaded" card and the empty applications list for an out-of-ward patient

#### `server/services/vigil_service.py::compute_news2`: 6 screens

- **Vigil board**, risk dot colour; risk percentage; percentage text colour; solid red emergency row; EMERGENCY badge; critical float to top of board; "Elevated Risk" tile count; "Critical" tile count
- **Vigil chart**, NEWS2 aggregate; risk band; driver sentences; per-parameter attribution table; single-parameter escalation warning; the 0-1 index the gauge ring fills to
- **Nurse board**, the number on every NEWS2 badge; critical/elevated/stable colours; the single-parameter-3 corner flag; the "critical" and "elevated" count pills; card top-border colour; sickest-first ordering of tasks and cards
- **Nurse vitals/eMAR**, the NEWS2 number; badge colour; the single-parameter "3" flag; per-field "+N" chips; driver pills; "All vitals within normal range"; "Within range on what was measured (N/5 parameters)"; the SpO₂ Scale-2 tooltip clause
- **Nurse handover**, the severity banding; the driver phrases ("Tachypnea (RR 23)", "Severe hypotension (SBP 84)", …); "All vitals within normal range"; "Within range on what was measured (n/5 parameters)"; the badge colour on each handover row
- **Round mode**, "NEWS2 N" label; its severity colour and ordering; the driver list; "SpO₂ Scale 2"

#### `src/pages/ClinicianShell.tsx::TABS`: 6 screens

- **Vigil board**, "Patients" nav tab label; heartbeat-trace tab icon; Vigil as the landing tab for doctor/surgeon/admin
- **Nurse board**, "My Shift" nav tab label; clipboard-with-tick tab icon; "Nurse Mode" pillar grouping
- **Nurse vitals/eMAR**, "My Shift" nav entry that leads to both surfaces
- **Round mode**, Ward Round nav item (icon, label, tooltip)
- **Orders**, the left-nav "Orders" entry and its document icon
- **Analytics/Pharmacy/Scribe/Lens/Bridge**, Forecast sidebar tab; Command sidebar tab; Pharmacy sidebar tab; Verify Queue sidebar tab; Scribe sidebar tab; Lens sidebar tab

#### `src/components/OfflineBanner.tsx::OfflineBanner`: 5 screens

- **Vigil board**, "Offline, not live" heading; "Can't reach Orb, not live" heading; "Showing the patient board saved at HH:MM" line; "unknown save time" variant; "no saved copy" variant; "Nothing can be recorded until you reconnect" line; "Try again" button; the banner's total absence when live
- **Vigil chart**, staleness banner heading; "saved at HH:MM" line; age-in-words; "nothing can be recorded" line; Try again button
- **Nurse board**, "Offline, not live" heading; "Can't reach Orb, not live" heading; "Showing the shift board saved at HH:MM" line; the unknown-save-time variant; the "no saved copy" variant; the "nothing can be recorded until you reconnect" paragraph; the banner's total absence when live
- **Nurse vitals/eMAR**, "Offline, not live" heading; "Can't reach Orb, not live" heading; "Showing the patient details saved at HH:MM" line; "Showing the drug chart saved at HH:MM" line; "no saved copy" variant; "Nothing can be recorded until you reconnect" line; the banner's complete absence when live
- **Nurse handover**, "Offline, not live" / "Can't reach Orb, not live" above the handover list; "Showing the shift board saved at HH:MM"; "no saved copy" variant; the "handovers are refused outright" write line

#### `src/hooks/useOfflineSnapshot.ts::useOfflineSnapshot`: 5 screens

- **Vigil board**, whether the board renders live or labelled-cached rows; which staleness heading appears; cached board on a cold offline start
- **Vigil chart**, decides whether the chart renders live or cached, and therefore whether the staleness banner appears
- **Nurse board**, whether the board renders live or labelled-cached; which staleness heading appears; the cached board on a cold offline start; refetch on reconnect
- **Nurse vitals/eMAR**, whether either sheet renders live or labelled-cached data; which staleness heading appears
- **Nurse handover**, whether the handover list is built from live or cached patients; whether the tab body renders at all

#### `src/lib/offline/writeGuard.ts::assertOnlineForWrite`: 5 screens

- **Relay thread**, offline-refusal band; suppression of the optimistic bubble when offline
- **Vigil chart**, the offline refusal alert when saving a clinical note
- **Nurse vitals/eMAR**, "Cannot record these observations, this device is offline…" red note; "Cannot chart this dose, this device is offline…" red strip
- **Nurse handover**, "Cannot save this handover, this device is offline. Your entry has not been saved. Reconnect and enter it again."
- **Round mode**, the offline refusal text in the inline save error

#### `server/permissions.py::department_scope`: 4 screens

- **App shell**, Command palette result rows (which patients appear)
- **Vigil board**, which patients appear on the board at all; the "My Dept" scope of the Admitted tile
- **Nurse board**, which patients appear on the board; the department (or "All departments") in the subtitle
- **Round mode**, which patients appear as queue rows; the ward name in the subtitle

#### `src/api/client.ts::getPatients`: 4 screens

- **Sage**, Whisper patient-name hint (dictation accuracy only)
- **Round mode**, queue rows; "Day N"; "Patient board unavailable" strip
- **Orders**, every row in both patient pickers
- **Analytics/Pharmacy/Scribe/Lens/Bridge**, Scribe patient-link dropdown

#### `src/components/OfflineBanner.tsx::ageInWords`: 4 screens

- **Vigil board**, the "N minutes old" age reading in the staleness banner
- **Vigil chart**, the age phrase inside the staleness banner
- **Nurse board**, the "N minutes old" reading in the staleness banner
- **Nurse vitals/eMAR**, the "9 minutes old" age reading in both banners
---

## Two functions that are not the function you think they are

Not a disagreement between screen files, a trap the inversion makes visible.

`src/pages/panels/ForecastPanel.tsx::apiFetch` (`ForecastPanel.tsx:24`) and
`src/pages/panels/AppointmentsPanel.tsx::apiFetch` (`AppointmentsPanel.tsx:8`) are
**private, local copies**, not the shared `src/api/client.ts::apiFetch`. Each is eight
lines: `fetch` with `credentials: 'include'` and `buildHeaders('GET')`, then
`if (!res.ok) throw new Error(\`API ${res.status}\`)`. That is why those two screens show
a bare `API 403` / `API 500` where every other screen shows a sentence, and why neither
gets the shared client's 401 session-expiry handling, offline queueing, or typed error
detail. If you harden `client.ts::apiFetch`, these two screens do **not** get the fix.

---

## Corrections resolved from the code

Where per-screen files disagreed about the name of a symbol, the code decided. The index
below uses the corrected name. **The screen files have since been corrected too**, as of
the 2026-08-09 sweep every `Reverse map contributions` line uses the real symbol name, so
the "It said" column is history, not a live discrepancy. It is kept because a reader
holding an older printout of a screen file still needs the translation.

| Screen file | It said | The code says | Evidence |
|---|---|---|---|
| `06-vigil-board.md` | `src/pages/ClinicianShell.tsx::navItems` | **`TABS`** | `ClinicianShell.tsx:31`, `const TABS: { id: Tab; label: string; pillar: string; icon: ReactNode }[] = [` |
| `12-orders.md` | `src/pages/ClinicianShell.tsx::NAV_ITEMS` | **`TABS`** | same line; there is no `NAV_ITEMS` anywhere in the file |
| `12-orders.md` | `src/pages/ClinicianShell.tsx::accessibleTabsFor` | **`getAccessibleTabs`** | `ClinicianShell.tsx:375`, `function getAccessibleTabs(role, department): Tab[]`, called at `:474` |
| `15-…-bridge.md` | `server/ws/scribe_stream.py::start_session` | **`handle_start_session`** | `scribe_stream.py:168-169`, `@sio.on("scribe:start_session")` then `async def handle_start_session(sid, data=None)`. The screen file named the socket **event**, not the handler |
| `15-…-bridge.md` | `server/ws/scribe_stream.py::audio_chunk` | **`handle_audio_chunk`** | `scribe_stream.py:195-196`, event `scribe:audio_chunk` |
| `15-…-bridge.md` | `server/ws/scribe_stream.py::end_utterance` | **`handle_end_utterance`** | `scribe_stream.py:209-210`, event `scribe:end_utterance` |

Three names, one pattern: `TABS`/`getAccessibleTabs` were mis-remembered in two later
screen files after four earlier ones had them right, and the Scribe socket handlers were
cited by their wire event rather than their Python name. Nothing else disagreed, where
two screen files described the same function differently, they were describing what it does
*on their own screen*, and both were right.

**Confirmed dead-on-arrival citations** (each checked: defined exactly once, called
nowhere), these are correctly documented as dead, not mis-cited:

- `src/pages/panels/VigilPanel.tsx::VitalCard`, defined at `VigilPanel.tsx:2091`, zero usages in `src/`.
- `server/services/surgery_service.py::get_weekly_schedule`, defined at `:10`, zero callers in `server/`. It is the only producer of `is_today` in the tree, which is why the Surgical Suite's Today badge never renders.
- `src/api/ipass.ts::getRecentHandoffs`, defined at `:114`, zero callers in `src/`. Its route (`server/routes/ipass_routes.py::recent_handoffs`, `:75-83`) is live and unreached.

---

## Files in this index

237 files. Jump to the directory you are working in.

**`server/`** (6), `auth.py`, `config.py`, `database.py`, `main.py`, `migrations.py`, `permissions.py`

**`server/models/`** (1), `user.py`

**`server/routes/`** (34), `admin_routes.py`, `admin_users_routes.py`, `admission_routes.py`, `appointments_routes.py`, `audit_routes.py`, `auth_routes.py`, `backup_routes.py`, `bridge_routes.py`, `bundle_routes.py`, `command_center_routes.py`, `compliance_routes.py`, `emar_routes.py`, `flight_recorder_routes.py`, `forecast_routes.py`, `helix_routes.py`, `impact_routes.py`, `ingestion_routes.py`, `ipass_routes.py`, `lens_routes.py`, `model_governance_routes.py`, `nurse_routes.py`, `orders_routes.py`, `patient_timeline_routes.py`, `pharmacy_routes.py`, `pulse_routes.py`, `relay_routes.py`, `roster_import_routes.py`, `sage_proactive_routes.py`, `sage_routes.py`, `scribe_routes.py`, `search_routes.py`, `shadow_routes.py`, `surgery_routes.py`, `vigil_routes.py`

**`server/services/`** (44), `admission_service.py`, `ai_governance.py`, `allergy_interlock.py`, `audit_service.py`, `bot_service.py`, `bridge_service.py`, `command_center_service.py`, `compliance_service.py`, `discharge_service.py`, `drug_safety_service.py`, `emar_service.py`, `forecast_service.py`, `helix_service.py`, `impact_service.py`, `ingestion_service.py`, `ipass_service.py`, `lens_service.py`, `medication_safety_gate.py`, `mfa_service.py`, `model_governance_service.py`, `nurse_shift_service.py`, `order_sets_service.py`, `orders_service.py`, `patient_story_service.py`, `patient_timeline_service.py`, `pharmacy_verification_service.py`, `relay_service.py`, `room_fit_service.py`, `roster_import.py`, `sage_ambient_service.py`, `sage_handoff_service.py`, `sage_proactive_service.py`, `sage_service.py`, `scribe_service.py`, `seasonal_calendar.py`, `sepsis_bundle_service.py`, `shadow_export_service.py`, `shadow_scorecard_service.py`, `surgery_escalation_service.py`, `surgery_service.py`, `terminology_service.py`, `thinking_mode.py`, `timeline_service.py`, `vigil_service.py`

**`server/services/vitals_source/`** (1), `simulator_source.py`

**`server/site_config/`** (1), `order_sets.json`

**`server/ws/`** (2), `realtime.py`, `scribe_stream.py`

**`src/`** (3), `App.tsx`, `index.css`, `print.css`

**`src/api/`** (12), `bridgeClient.ts`, `bundles.ts`, `client.ts`, `deviceAuth.ts`, `ipass.ts`, `nurse.ts`, `orderSets.ts`, `pharmacy.ts`, `pulse.ts`, `rosterImport.ts`, `scorecard.ts`, `thinkingMode.ts`

**`src/components/`** (19), `AdmitPatientModal.tsx`, `AiStatusChip.tsx`, `AlertPermissionButton.tsx`, `ClinicalCourseTimeline.tsx`, `ClockWidget.tsx`, `CommandPalette.tsx`, `DischargePrintView.tsx`, `FlightRecorder.tsx`, `HandoffPrintView.tsx`, `KeyboardShortcuts.tsx`, `MfaEnrollmentModal.css`, `MfaEnrollmentModal.tsx`, `OfflineBanner.tsx`, `OrbLogo.css`, `OrbLogo.tsx`, `ProvenanceChip.tsx`, `PulseWidget.css`, `PulseWidget.tsx`, `SizedChart.tsx`

**`src/components/bridge/`** (2), `CareTeamCard.tsx`, `VitalsTrendsCard.tsx`

**`src/components/charts/`** (1), `useChartTheme.ts`

**`src/components/command/`** (1), `CommandCenter.tsx`

**`src/components/common/`** (1), `PatientPicker.tsx`

**`src/components/handoff/`** (1), `IpassHandoffView.tsx`

**`src/components/helix/`** (5), `HelixDashboard.tsx`, `HelixFormulary.tsx`, `HelixOrderReviewModal.tsx`, `HelixPatientBoard.tsx`, `PharmacyVerifyQueue.tsx`

**`src/components/orders/`** (4), `OrderSets.css`, `OrderSetsPanel.tsx`, `Orders.module.css`, `OrdersPanel.tsx`

**`src/components/print/`** (2), `MedListPrintView.tsx`, `OrdersPrintView.tsx`

**`src/components/relay/`** (7), `AlertsSection.tsx`, `CaseList.tsx`, `RelayComponents.css`, `RelaySearch.tsx`, `RoomFitWarning.tsx`, `SageAmbientSidebar.module.css`, `SageAmbientSidebar.tsx`

**`src/components/round/`** (8), `Round.css`, `RoundDictation.tsx`, `RoundHandoff.tsx`, `RoundQueueRail.tsx`, `RoundStates.tsx`, `RoundSummaryView.tsx`, `roundQueue.ts`, `roundSummary.ts`

**`src/components/sage/`** (9), `Sage.css`, `SageComposer.tsx`, `SageEmptyState.tsx`, `SageFloatingAssistant.css`, `SageFloatingAssistant.tsx`, `SageHistorySidebar.tsx`, `SageInsightsFeed.tsx`, `SageMessage.tsx`, `WardBrief.tsx`

**`src/components/surgery/`** (1), `EmergencyIntakePopup.tsx`

**`src/components/vigil/`** (5), `AdministerModal.css`, `AdministerModal.tsx`, `CareBundleTimer.tsx`, `PatientStory.tsx`, `SafetyAlertModal.tsx`

**`src/contexts/`** (3), `ActivePatientContext.tsx`, `ActiveViewContext.tsx`, `SageRuntimeContext.tsx`

**`src/hooks/`** (5), `useFocusTrap.ts`, `useOfflineSnapshot.ts`, `useOfflineStatus.ts`, `useStreamingTranscription.ts`, `useVisiblePolling.ts`

**`src/i18n/`** (1), `index.ts`

**`src/i18n/locales/`** (2), `en.json`, `hi.json`

**`src/lib/`** (10), `a11y.ts`, `datetime.ts`, `demographics.ts`, `humanise.ts`, `messageMerge.ts`, `nurseDrafts.ts`, `orbBus.ts`, `roles.ts`, `shortcuts.ts`, `tablist.ts`

**`src/lib/offline/`** (1), `writeGuard.ts`

**`src/pages/`** (8), `ClinicianShell.css`, `ClinicianShell.tsx`, `HandoffPage.tsx`, `Login.css`, `Login.tsx`, `PatientShell.tsx`, `Relay.css`, `Vigil.css`

**`src/pages/panels/`** (12), `AdminPanel.tsx`, `AppointmentsPanel.tsx`, `ForecastPanel.tsx`, `HelixPanel.tsx`, `LensPanel.tsx`, `RelayPanel.tsx`, `RoundPanel.tsx`, `SagePanel.tsx`, `ScribePanel.tsx`, `SurgicalSuitePanel.css`, `SurgicalSuitePanel.tsx`, `VigilPanel.tsx`

**`src/pages/panels/admin/`** (16), `Admin.css`, `AdminBadges.tsx`, `AdminEmptyState.tsx`, `AdminOverview.tsx`, `AlertOperations.tsx`, `AuditLogTable.tsx`, `CorrelationTrace.tsx`, `DataPanel.tsx`, `ImpactDashboard.tsx`, `IngestionMonitor.tsx`, `ModelGovernance.tsx`, `RosterImport.tsx`, `Scorecard.tsx`, `SecurityActivity.tsx`, `TrustCenter.tsx`, `UsersPanel.tsx`

**`src/pages/panels/nurse/`** (7), `HandoverSheet.tsx`, `MedRoundSheet.tsx`, `News2Badge.tsx`, `NursePanel.css`, `NursePanel.tsx`, `ObsEntrySheet.tsx`, `time.ts`

**`src/services/`** (1), `criticalAlerts.ts`

---

## Server files


### `server/auth.py`

- `::clear_device_cookie`
  - *Login*, tablet name badge disappearing after revocation
- `::create_token`
  - *Login*, `authMethod`-driven short tablet session
- `::DEVICE_PIN_SESSION_MINUTES`
  - *Login*, PIN session length (returned to the screen as `session_minutes`, currently unrendered)
- `::extract_device_token`
  - *Login*, tablet name badge; PIN sign-in succeeding on this browser
- `::generate_device_secret`
  - *Login*, the 43-character length the setup paste box is sized for
- `::load_active_device`
  - *Login*, tablet name badge; PIN pad availability; "This device credential is not valid." banner
- `::set_device_cookie`
  - *Login*, PIN pad persisting across reloads after tablet setup
- `::set_session_cookies`
  - *Login*, session persistence after sign-in; PIN session cookie lifetime
- `::verify_device_pin_ct`
  - *Login*, "Invalid staff ID or PIN" banner and its constant response time
- `::verify_password_ct`
  - *Login*, "Invalid credentials" banner and its constant response time


### `server/config.py`

- `::MFA_ISSUER`
  - *Login*, issuer name in the modal's hint line and in the provisioning URI
- `::MFA_REQUIRED_ROLES`
  - *Login*, whether the MFA enrolment modal ever appears; "PIN sign-in is not available for this account" banner


### `server/database.py`

- *(whole file / stylesheet)*
  - *Surgical/Appointments*, the status values the ribbon prints; the absence of a bed column behind the blank Bed in the Tier-3 patient bar
  - *Surgical/Appointments*, the four team-chip statuses and the `pending` default that shows as a neutral chip
  - *Surgical/Appointments*, the 24-hour time and the type/reason on every BOOKED row; the `status` value that makes a booking appear or disappear


### `server/main.py`

- `::_invalidate_router_cache`
  - *App shell*, AI status chip warm/cold dots after a load or unload
- `::_resolve_configured_model`
  - *App shell*, AI configuration popover error strip
- `::health_models`
  - *App shell*, AI status chip; AI status chip OFFLINE pill; AI configuration popover model rows
- `::load_model`
  - *App shell*, AI configuration popover Load button
- `::MODEL_IMPACTS`
  - *App shell*, AI configuration popover unload-confirmation impact list
- `::unload_model`
  - *App shell*, AI configuration popover Unload button; Unload confirmation


### `server/migrations.py`

- `::_ORDER_SET_LOCAL_CODES`
  - *Orders*, whether the amber "codes not loaded in this deployment's terminology table" note appears for the shipped protocols


### `server/models/user.py`

- `::DeviceBindBody`
  - *Login*, setup-code paste box minimum length
- `::PinLogin`
  - *Login*, PIN-variant staff ID field; PIN field
- `::UserLogin`
  - *Login*, employee ID field; password field; authenticator code field
- `::UserOut`
  - *Login*, the name, role and department carried into the shell after sign-in


### `server/permissions.py`

- ⚠ `::department_scope`, **4 screens**
  - *App shell*, Command palette result rows (which patients appear)
  - *Vigil board*, which patients appear on the board at all; the "My Dept" scope of the Admitted tile
  - *Nurse board*, which patients appear on the board; the department (or "All departments") in the subtitle
  - *Round mode*, which patients appear as queue rows; the ward name in the subtitle
- `::enforce_patient_scope`
  - *Orders*, the 404-shaped "Preview failed" for an out-of-ward patient on preview and apply
- `::log_phi_read`
  - *App shell*, Command palette result rows (audited disclosure)
- ⚠ `::require_patient_access`, **6 screens**
  - *Vigil chart*, the "Patient not found" screen shown for a cross-ward chart; the audited `patient_chart_viewed` row on every open
  - *Nurse board*, the handover draft error notice for an out-of-ward patient; the observation sheet's save refusal for one
  - *Nurse vitals/eMAR*, 404 (not 403) in the red note when a nurse charts against a patient outside her ward
  - *Nurse handover*, the 404 for an out-of-ward patient (never a 403); the `ipass_draft_viewed` PHI-read row; the `patient_scope_denied` row
  - *Round mode*, "Overnight events unavailable" (404 for an off-ward patient); inline save error on an off-ward plan save
  - *Orders*, the 404 behind the "Orders could not be loaded" card and the empty applications list for an out-of-ward patient
- ⚠ `::require_permission`, **8 screens**
  - *App shell*, Command palette empty state for pharmacists; Pulse panel error for pharmacists; AI configuration popover Load/Unload for non-admins; Admit Patient modal error strip
  - *Relay list*, 403 behind the "Alerts could not be loaded." message and the silently empty channel lists
  - *Vigil board*, 403 board-failure block for a role lacking patient.read_all
  - *Nurse board*, the red "Insufficient permissions" notice in place of a board
  - *Nurse handover*, the "Insufficient permissions" message; the admin's ability to read but not save a handover; the `permission_denied` audit row
  - *Orders*, the "Insufficient permissions" message on create, preview and apply
  - *Admin*, the 403 message shown in every Admin error state; the `permission_denied` rows listed on the Security tab
  - *Surgical/Appointments*, the "Insufficient permissions" text behind the Surgical Suite error banner; the generic "Failed to load appointments" sentence for a role without forecast.read
- ⚠ `::ROLE_PERMISSIONS`, **8 screens**
  - *App shell*, Command palette result rows; Pulse panel; AI configuration popover Load/Unload buttons; Admit Patient modal submit
  - *Relay list*, whether the channel lists and the alerts rail load at all; "Alerts could not be loaded." message for a role without relay.read
  - *Relay thread*, whether the thread opens at all; whether Acknowledge/Dismiss/Confirm succeed or raise a 403 alert
  - *Nurse vitals/eMAR*, whether the save and the Given button work at all for a given role
  - *Nurse handover*, which roles can open the document (`vitals.read`) and which can save it (`scribe.create_draft`)
  - *Orders*, whether a role can read the order list, create orders, or preview/apply a set
  - *Admin*, whether the Admin sidebar item exists at all; which Admin tabs return data
  - *Surgical/Appointments*, whether a role can open either screen, and whether the four Surgical Suite mutation buttons succeed


### `server/routes/admin_routes.py`

- `::_build_erase_plan`
  - *Admin*, number of clinical records reported as removed
- `::admin_overview`
  - *Admin*, Ingestion Mode card; Audit simulation line; Audit Events (24h) card and category chips; Ingested Payloads card and chips; Source Distribution card; Alert Activity card and Vigil/Relay chips; Records Written card; Recent Parse Failures table; Auth Summary chips
- `::erase_patient`
  - *Admin*, erase result block on Data & Backups


### `server/routes/admin_users_routes.py`

- `::activate_user`
  - *Admin*, Activate confirmation outcome; Status chip flip to Active
- `::create_user`
  - *Admin*, Create User dialog outcome, duplicate-id error, "User created" toast
- `::deactivate_user`
  - *Admin*, Deactivate confirmation outcome; Status chip flip to Disabled; "cannot deactivate your own account" toast
- `::list_users`
  - *Admin*, user count; every user table row; Role chip; MFA chip; Status chip; deactivated-row tint
- `::patch_user`
  - *Admin*, Change role & department outcome; Role chip colour; Department cell; row reordering
- `::reset_password`
  - *Admin*, Reset password dialog outcome; "Password reset" toast


### `server/routes/admission_routes.py`

- `::process_admission`
  - *App shell*, Admit Patient modal submit; One-time Bridge credential card; Admit Patient modal error strip


### `server/routes/appointments_routes.py`

- `::book_appointment`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Confirmed view date/time/type; duplicate-slot and idempotent replay behaviour
- `::cancel_appointment`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, appointment row removal; cancel error line
- `::my_appointments`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, "Your upcoming appointments" list


### `server/routes/audit_routes.py`

- `::get_audit_logs`
  - *Admin*, Audit Logs table rows and total; Security table rows and total; Trace timeline events; Alerts table rows


### `server/routes/auth_routes.py`

- `::_account_is_locked`
  - *Login*, "Account temporarily locked after too many failed attempts" banner
- `::_clear_pin_failures`
  - *Login*, a correct PIN resetting the lockout counters
- `::_enrolment_principal`
  - *Login*, whether the enrolment modal can load at all before a session exists
- `::_locked_until`
  - *Login*, both PIN lockout banners
- `::_record_account_failure`
  - *Login*, how many wrong passwords or codes it takes before the lockout banner appears
- `::_record_pin_failure`
  - *Login*, how many wrong PINs it takes before each PIN lockout banner appears
- `::bind_device`
  - *Login*, "This device credential is not valid." banner; "Too many attempts…" banner; tablet name badge appearing after setup
- `::consume_totp`
  - *Login*, "Invalid authenticator code…" banner on a replayed code; the "wait for the next code" warning's reason for existing
- `::device_status`
  - *Login*, tablet name badge; PIN pad vs password form on first load
- `::is_ip_rate_limited`
  - *Login*, "Too many login attempts…" and "Too many attempts…" banners
- `::issue_mfa_enroll_ticket`
  - *Login*, MFA enrolment modal availability (the `X-MFA-Enrollment-Ticket` header)
- `::login`
  - *Login*, "Invalid credentials" banner; "Account temporarily locked…" banner; "Too many login attempts…" banner; "Account deactivated…" banner; authenticator code step (401 `mfa_required`); "Invalid authenticator code…" banner; MFA enrolment modal (403 `mfa_enrollment_required` + ticket header); successful sign-in and redirect
- ⚠ `::mfa_enroll_begin`, **2 screens**
  - *App shell*, MFA setup key; MFA provisioning URI; MFA disable view (via "MFA already enabled")
  - *Login*, "Preparing your authenticator setup…" loading text; grouped setup secret; provisioning URI; issuer hint line
- ⚠ `::mfa_enroll_confirm`, **2 screens**
  - *App shell*, MFA enrolled confirmation; MFA error strip
  - *Login*, "Enable MFA" outcome; enrolled success view; modal error strip on a bad code
- `::pin_login`
  - *Login*, "Invalid staff ID or PIN" banner; "This tablet is not set up for PIN sign-in" banner; "This tablet is temporarily locked after too many failed PINs" banner; "Your PIN is temporarily locked…" banner; "PIN sign-in is not available for this account" banner; "Account deactivated…" banner on the PIN path; 15-minute PIN session


### `server/routes/backup_routes.py`

- `::create_backup`
  - *Admin*, "Backup created" toast and the new backup row
- `::list_backups`
  - *Admin*, backup table rows and count


### `server/routes/bridge_routes.py`

- `::_resolve_patient_id`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, the 404 that produces the Bridge cards' error/empty text for an unlinked account
- `::patient_ask`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Bridge answer card streaming text
- `::patient_care_team`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Your Care Team members and care unit line
- `::patient_reports`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Medications / Report History / Lab History / Discharge modal contents; FHIR button enablement
- `::patient_status`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Your Status card text and badge; Bridge header patient name
- `::patient_vitals_trends`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Your Vitals & Trends values, status words and sparklines


### `server/routes/bundle_routes.py`

- `::cancel`
  - *Vigil chart*, Cancel bundle
- `::complete`
  - *Vigil chart*, Mark done
- `::patient_bundles`
  - *Vigil chart*, every value in the Care Bundle card
- `::start`
  - *Vigil chart*, Start Sepsis Six


### `server/routes/command_center_routes.py`

- `::get_house_snapshot`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, entire Command Center page; deteriorations department scoping; command-center PHI read audit entry


### `server/routes/compliance_routes.py`

- `::get_compliance_report`
  - *Admin*, trust score ring; control checklist; audit-chain badge; MFA adoption bars


### `server/routes/emar_routes.py`

- `::administer_medication`
  - *Nurse vitals/eMAR*, the green "Given" chip; the amber "Not given" chip; every 422 and 428 sentence in the modal's red strip
- `::get_patient_administrations`
  - *Vigil chart*, the same chips


### `server/routes/flight_recorder_routes.py`

- `::timeline`
  - *Admin*, Flight Recorder event stream
- `::verify`
  - *Admin*, Flight Recorder chain-integrity result


### `server/routes/forecast_routes.py`

- `::admission_patterns`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, 30-Day Admissions tile; Avg Length of Stay tile; Discharge Rate tile; Admissions by Day of Week chart; Distribution Analysis panel
- `::appointments_schedule`
  - *Surgical/Appointments*, whether the Appointments board loads; the 14-day clamp on how many columns can appear
- `::capacity_forecast`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Ward Capacity cards; ward status badge; bed grid; ward Now/48h stats; capacity totals strip
- `::discharge_board`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Discharge Readiness board rows (department-scoped); discharge-board PHI read audit entry
- `::hospital_forecast`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Current Census tile; Beds Available in 48h tile; 7-Day Discharges tile; Peak Day tile; 7-day occupancy chart; daily breakdown table


### `server/routes/helix_routes.py`

- `::approve_helix_order`
  - *Sage*, "Sent to Pharmacy" badge; "Failed" badge
- `::get_all_orders`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Helix metric tiles; Pharmacist Work Queue; Fulfillment Pipeline; Patient Medication Board
- `::get_catalog`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Formulary rows; substitute chooser options
- `::get_inventory`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Available Stock cell; Active Lots cell; patient-board stock pills
- `::transition_order`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, order status badge changes after a modal action


### `server/routes/impact_routes.py`

- `::get_impact`
  - *Admin*, Impact hero figures; metric cards; assumption values


### `server/routes/ingestion_routes.py`

- `::ingestion_status`
  - *Admin*, Ingestion table rows and the Mode label


### `server/routes/ipass_routes.py`

- ⚠ `::get_draft`, **2 screens**
  - *Nurse board*, the five I-PASS rows
  - *Nurse handover*, every drafted value on both surfaces; the 404 for an unknown or out-of-ward patient; the `ipass_draft_viewed` audit row
- `::recent_handoffs`
  - *Nurse handover*, nothing visible today (no frontend caller)
- ⚠ `::save_draft`, **2 screens**
  - *Nurse board*, handover save success and its "Handed over HH:MM" stamp
  - *Nurse handover*, "Handed over HH:MM"; "Handoff saved as draft note #N."; the 400 "content is required"; the 403 for a role without `scribe.create_draft`; the `ipass_handoff_saved` audit row


### `server/routes/lens_routes.py`

- `::_assign_analysis`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, patient name in the success strip
- `::analyze_image`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, AI Reference Read text; analysis id; 413/415/403 messages surfaced as "Analysis failed: …"
- `::assign_to_patient`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, assignment result strip; Relay case-room imaging message
- `::list_analyses`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, fallback analysis lookup when the streamed id is missing


### `server/routes/model_governance_routes.py`

- `::get_models`
  - *Admin*, Model Governance table rows


### `server/routes/nurse_routes.py`

- ⚠ `::get_board`, **3 screens**
  - *Nurse board*, every patient card, task row and count pill; the department shown in the subtitle; the board failing closed when its phi_access audit row cannot be written
  - *Nurse vitals/eMAR*, every value on both sheets' identity lines; allergy strip; drug rows
  - *Round mode*, ward name subtitle; "ward data as of HH:MM"; queue rows; allergy strip; NEWS2 row; "Observations overdue"; "N medications overdue"; unverified-medication decision rows; "Ward board unavailable" strip
- ⚠ `::preview_news2`, **2 screens**
  - *Nurse board*, the live score inside the observation sheet (no element on the board itself)
  - *Nurse vitals/eMAR*, the live NEWS2 badge; "+N" chips; band and response; driver pills; ward-scope 404 on a patient the nurse cannot open
- ⚠ `::record_observations`, **2 screens**
  - *Nurse board*, the recorded NEWS2 and escalation sentence in the green flash; the board changes after saving obs
  - *Nurse vitals/eMAR*, charted observation set; the green flash text; the 422/404 messages in the red error note


### `server/routes/orders_routes.py`

- `::_order_set_status`
  - *Orders*, whether a bad set/patient shows as a 404-shaped or 400-shaped preview failure
- `::_shadow_clinical_action`
  - *Orders*, nothing visible on this screen (it feeds the shadow scorecard)
- `::add_result`
  - *Orders*, the "Result: …" line; the gold RESULTED chip
- `::apply_order_set`
  - *Orders*, the apply success callout; the "Nothing was ordered." callout and its message; the generic apply error
- `::create_order`
  - *Orders*, every new order card; the 428 that opens the blocking modal; the `safety` payload behind the info modal; the red form error line
- `::list_order_set_applications`
  - *Orders*, the "Already applied to this patient" rows
- `::list_order_sets`
  - *Orders*, the protocol cards; the provenance line's "editable by …" text
- `::list_patient_orders`
  - *Orders*, the order list; the empty state; the "Orders could not be loaded" card
- `::preview_order_set`
  - *Orders*, the item rows and their verdicts; "Preview failed: …"; the 404 for an out-of-ward patient
- `::transition_order`
  - *Orders*, the status chip after an advance; the cancelled chip; the failure alert()
- `::validate_terminology`
  - *Orders*, the "✓ <display>" / "✗ unknown code" hint; the auto-filled Description


### `server/routes/patient_timeline_routes.py`

- `::unseen_counts`
  - *Vigil board*, "N NEW" badge; absence of the badge for never-reviewed charts


### `server/routes/pharmacy_routes.py`

- `::get_pending`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Pharmacist Verification Queue rows
- `::reject`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Reject outcome
- `::verify`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Verify outcome; 428 acknowledgement row heading and message


### `server/routes/pulse_routes.py`

- ⚠ `::pulse_season`, **2 screens**
  - *App shell*, Pulse panel
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, every Pulse panel section; the 503 error sentence


### `server/routes/relay_routes.py`

- ⚠ `::_apply_alert_resolution`, **2 screens**
  - *Relay list*, alert resolved line; "Alert already processed" inline failure line
  - *Relay thread*, green resolved strip; "Alert already processed" inline error; "Not authorized to act on this channel" inline error
- `::_room_fit_adjudicate_later`
  - *Relay thread*, a wrong-room strip appearing seconds late; a strip disappearing (auto-cleared)
- `::_room_fit_publish`
  - *Relay thread*, the amber wrong-room strip; its headline, body and suggested destination
- `::_room_fit_redirect_txn`
  - *Relay thread*, the copied message that appears in the destination room
- `::_run_room_fit`
  - *Relay thread*, whether a wrong-room strip appears at all; silence on a `review` band
- `::_transition_action`
  - *Relay thread*, action state badge; "Action was modified concurrently" alert; "Cannot transition from a resolved state" alert
- `::get_messages`
  - *Relay thread*, every message bubble; thread-load error box on a 403
- `::get_sage_actions`
  - *Relay thread*, suggested action cards; handled action cards; handoff badge (and its absence)
- `::get_sage_memory`
  - *Relay thread*, current-concern line; discussion-summary paragraph; risk cards
- `::list_alerts`
  - *Relay list*, alert row; alert title; alert description; urgent dot; "→" arrow
- ⚠ `::list_channels`, **2 screens**
  - *Relay list*, channel row; channel name; risk chip; ward/bed line; diagnosis line; unread badge; red alert dot on a channel row
  - *Relay thread*, room title; Sage sidebar presence
- `::list_room_fit_flags`
  - *Relay thread*, wrong-room strips loaded with the thread; per-reader redaction of the suspected patient name and destination
- ⚠ `::mark_read`, **2 screens**
  - *Relay list*, unread badge (clearing)
  - *Relay thread*, unread badge clearing when a thread is opened
- `::post_message`
  - *Relay thread*, sent message bubble; wrong-room strip attached to your own send; live bubbles for everyone else
- ⚠ `::resolve_alert`, **2 screens**
  - *Relay list*, Confirm button; Escalate button; alert resolved line; removal of an alert row from the rail
  - *Relay thread*, green "✓ Confirmed/Escalated by …" strip
- `::resolve_room_fit_flag`
  - *Relay thread*, grey resolved strip; "Moved to …" label; the copied message appearing in the destination room; "already resolved" and "not authorized to post" errors
- `::transition_sage_action`
  - *Relay thread*, Acknowledge/Dismiss/Confirm outcomes; handoff badge; live sidebar refresh for other readers; surgical alert cards created by a confirmation


### `server/routes/roster_import_routes.py`

- `::commit`
  - *Admin*, commit result block; 409 "NOTHING was written" error; history table refresh
- `::dry_run`
  - *Admin*, Step 3 count chips; file-issues list; plan table; absent-from-roster block
- `::get_fields`
  - *Admin*, Orb-field options; required-field warning; row/byte limits line; date-order options
- `::preview`
  - *Admin*, delimiter and row-count line; header-candidate buttons; mapping table and samples
- `::runs`
  - *Admin*, Applied imports history table


### `server/routes/sage_proactive_routes.py`

- `::list_insights`
  - *Round mode*, Sage insight issue rows; "Sage suggests: …" decision rows; "Sage surveillance flags could not be loaded" strip


### `server/routes/sage_routes.py`

- `::_match_medication`
  - *Sage*, Drafted vs Not Available in Pharmacy badge
- `::_safety_refusal`
  - *Sage*, harm-intent refusal text; prompt-injection refusal text; High-Risk Query banner on a refusal
- `::_strip_contract_scaffolding`
  - *Sage*, readable answer text when the model ignores the output contract
- `::_strip_forbidden`
  - *Sage*, absence of fabricated confirmations in the final answer and clinical assessment
- ⚠ `::get_ai_posture`, **2 screens**
  - *Sage*, voice mode's up-front refusal when the local speech engine is not installed
  - *Round mode*, "Local speech recognition is not installed…"
- `::get_history`
  - *Sage*, Recent Conversations list
- `::MarkerAnswerStreamer`
  - *Sage*, when the typing dots give way to text; fully-buffered (dots-only) mutation turns
- `::parse_fast_response`
  - *Sage*, answer text and detail sections on the fast contract
- `::sage_query`
  - *Sage*, streaming answer text; stage indicator; all six banners; details expander; source chips; action card; refusal texts
- `::sage_voice_query`
  - *Sage*, spoken answer tokens; voice-lane refusal text
- `::SentenceStreamSanitizer`
  - *Sage*, absence of fabricated "order placed"/"Done." confirmations in streamed text


### `server/routes/scribe_routes.py`

- `::_note_in_scope_or_404`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, 404 that hides out-of-ward notes from the verification badge
- `::_verification_verdict`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, whether Sign & Finalize is blocked
- `::edit_note`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, saved note body after Save
- `::get_note_verification`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, verification badge during polling
- `::reject_note`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Rejected badge
- `::retry_verification`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, verification badge after ↻ Retry
- `::revise_note`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, new draft after Create Revision
- `::sign_note`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Signed badge; signed banner; 428 sign-gate on unverified notes
- `::structure`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Structured AI Note streaming body; note id; Draft status; initial Verifying badge; pipeline timing footer
- ⚠ `::transcribe`, **3 screens**
  - *Sage*, dictated question text; "Transcription failed." status
  - *Round mode*, transcript text in the plan textarea; "Transcription failed: …"
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, refined transcript text; "Recording too short" and refinement failure statuses


### `server/routes/search_routes.py`

- `::search_patients`
  - *App shell*, Command palette result rows; Command palette empty state


### `server/routes/shadow_routes.py`

- `::_attach_stats`
  - *Admin*, dropped/error event counts behind the "No shadow writes dropped" prerequisite
- `::export_scorecard`
  - *Admin*, the file delivered by the Download de-identified JSON/CSV buttons
- `::get_scorecard`
  - *Admin*, every Pilot Scorecard figure; export descriptor; instrument health block


### `server/routes/surgery_routes.py`

- `::_audit_surgery`
  - *Surgical/Appointments*, nothing visible on this screen; every state change here lands on the audit trail
- `::_load_risk_detail`
  - *Surgical/Appointments*, the patient facts the risk assessment is written about
- `::_persist_alerts`
  - *Surgical/Appointments*, the amber team chips after Alert Team; the 422 that silently does nothing when DOC004 or anesthetist_1 is missing
- `::acknowledge_emergency_surgery`
  - *Surgical/Appointments*, the Emergency Intake popup's Acknowledge; the case leaving the emergency banner
- `::alert_surgical_team`
  - *Surgical/Appointments*, the Alert Team button's effect; the Relay message the alerted clinician receives
- `::complete_surgery`
  - *Surgical/Appointments*, the Complete Surgery button; the case leaving Active Cases; the post-op note that appears in Scribe
- `::get_emergency_queue`
  - *Surgical/Appointments*, the emergency banner and its count; each emergency row; why the "Acknowledged" chip is unreachable
- `::get_surgery_cases`
  - *Surgical/Appointments*, every Active Cases card; the red count bubble; the team chips
- `::get_surgery_detail`
  - *Surgical/Appointments*, the whole Tier-3 screen; co-morbidity pills; allergy pills and the "Allergies not recorded" fallback; the blank Bed in the patient sub-line; the never-rendering Clinical Handoff Context card
- `::get_surgery_schedule`
  - *Surgical/Appointments*, the week strip's 14 day cards; day labels and dates; the surgeries inside each day; the risk dot colour; the UTC-based start of the window; the absent `is_today` that kills the Today badge
- `::prep_for_surgery`
  - *Surgical/Appointments*, the Prep for Surgery button's success and its silent failure
- `::start_surgery`
  - *Surgical/Appointments*, whether Start Surgery succeeds; the native alert() text on refusal; the "● In Progress" ribbon
- `::surgery_risk_assessment`
  - *Surgical/Appointments*, the streamed AI Risk Assessment body
- ⚠ `::surgical_team_response`, **2 screens**
  - *Relay thread*, SYSTEM confirmation/decline message in the thread; replacement surgical alert card; "User not assigned to this surgery" inline error
  - *Surgical/Appointments*, whether a team chip turns green or red; the Sage re-route chip that appears after a decline


### `server/routes/vigil_routes.py`

- `::_insert_medication`
  - *Vigil chart*, the new row in the medications table
- ⚠ `::_insert_patient_note`, **2 screens**
  - *Vigil chart*, the note row that then appears in Notes History
  - *Round mode*, "Saved to the chart" (and the absence of a duplicate note on replay)
- `::_load_latest_risk`
  - *Vigil chart*, which Clinical Course phase is marked "Now"
- `::_load_patient_bundle`
  - *Vigil chart*, the SQL behind every field above
- `::add_patient_medication`
  - *Vigil chart*, Add Medication; the 428 that opens the blocking drug-safety dialog
- ⚠ `::add_patient_note`, **2 screens**
  - *Vigil chart*, Save Note
  - *Round mode*, "Saved to the chart"; inline save error
- `::discharge_patient_route`
  - *Vigil chart*, discharge success bar; discharge summary preview; discharge timeline; QR block
- `::get_patient`
  - *Vigil chart*, patient name; MRN/age/sex; bed badge; diagnosis; comorbidity tags; vital cells; sparklines; risk gauge; drivers; trend line; NEWS2 attribution; medications table; notes history; report lists; lab tiles
- `::get_patient_analysis`
  - *Vigil chart*, MedGemma AI Assessment stream and its cross-tab dedup replay
- ⚠ `::get_patient_story_route`, **2 screens**
  - *Vigil chart*, Patient Story feed
  - *Round mode*, overnight event rows; "N more events overnight"; "Overnight events unavailable"
- `::get_patient_timeline_projection`
  - *Vigil chart*, Clinical Course card contents
- `::get_patient_vitals`
  - *Vigil chart*, (available; this screen uses the bundled vitals and the socket instead)
- `::get_qr_code`
  - *Vigil chart*, the discharge QR image
- ⚠ `::list_patients`, **2 screens**
  - *Vigil board*, every patient row and every field in it; "Admitted · My Dept" count via X-Total-Count; "Load more" totals; department scoping of the whole board; board-failure block when the audited read cannot be written
  - *Round mode*, queue rows for Vigil-only patients; "Day N"; "Patient board unavailable" strip


### `server/services/admission_service.py`

- `::admit_patient`
  - *App shell*, One-time Bridge credential card (Login ID, one-time password)


### `server/services/ai_governance.py`

- ⚠ `::ai_posture`, **2 screens**
  - *Sage*, voice mode's up-front refusal when the local speech engine is not installed
  - *Round mode*, "Local speech recognition is not installed…"
- `::local_asr_available`
  - *Round mode*, "Local speech recognition is not installed…"; the mic button's refusal message


### `server/services/allergy_interlock.py`

- `::decide`
  - *Nurse vitals/eMAR*, whether an allergy stops the dose (red strip) or passes silently
- `::effective_severity`
  - *Nurse vitals/eMAR*, which allergies count as blocking; the fail-safe treatment of unknown severity
- `::evaluate`
  - *Nurse board*, nothing visible on this screen (the verdict is computed and shipped but no element renders it)
- `::screen_for_administration`
  - *Nurse vitals/eMAR*, "allergy interlock: X conflicts with a documented allergy (Y)"; "allergy screening is unavailable"; silent pass on a carried-forward override
- `::validate_override_reason`
  - *Nurse vitals/eMAR*, "an allergy override requires a clinical reason (e.g. …)" if an override is ever wired up


### `server/services/audit_service.py`

- ⚠ `::log_audit_event`, **2 screens**
  - *Login*, no visible element on this screen; every outcome above is recorded
  - *Nurse handover*, the `ipass_handoff_saved` trail entry behind every save (no visible element, but it is what makes the save defensible)


### `server/services/bot_service.py`

- `::_next_reading`
  - *Vigil chart*, the simulated vital values and the risk object that arrive on `vitals:update`
- `::run_vitals_bot`
  - *Vigil board*, live recolouring of dots and percentages without a reload; rows jumping to the top mid-shift


### `server/services/bridge_service.py`

- `::answer_patient_question`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Bridge answer text
- `::get_patient_status_simple`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Your Status message wording; status level driving the badge colour
- `::screen_for_red_flags`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, fixed emergency escalation answer shown instead of a model reply


### `server/services/command_center_service.py`

- `::_bundles`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Sepsis Bundle Compliance tile
- `::_census_and_acuity`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Census/Occupancy tile; Critical Patients tile; Ward Acuity Heatmap cells
- `::_deteriorations`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Active Deteriorations rows; NEWS2 chip; critical-first ordering
- `::_downtime_readiness`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Downtime Readiness tile; snapshot counts; oldest-snapshot age
- `::_forecast`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Capacity Forecast 24h strip
- `::_or_today`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Operating Room Today table rows and count badge
- `::house_snapshot`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, all Command Center sections and their zeroed fallbacks


### `server/services/compliance_service.py`

- `::compliance_report`
  - *Admin*, every Trust Center figure and control status


### `server/services/discharge_service.py`

- `::build_patient_timeline`
  - *Vigil chart*, the Timeline list in the discharge success body and the printed timeline
- `::discharge_patient`
  - *Vigil chart*, the "✓ Patient discharged successfully" bar
- `::generate_discharge_summary`
  - *Vigil chart*, the discharge summary text
- `::generate_qr_code`
  - *Vigil chart*, the discharge QR image


### `server/services/drug_safety_service.py`

- `::check_medication_safety`
  - *Orders*, the alert lists in both modals; the max-severity that decides blocking vs info; `interaction_screened` behind the "Not screened" strip and its coverage note
- ⚠ `::evaluate_dose`, **2 screens**
  - *Nurse vitals/eMAR*, "is at or above N, no adult indication reaches this as a single dose…"; the never-correct-route sentences; the silent high-dose warning
  - *Orders*, the dose-guard STOPPED reasons on order-set items; the startup refusal of a lethal protocol item
- `::evaluate_duplicate`
  - *Nurse vitals/eMAR*, the "already given N minutes ago" warning that is recorded but never displayed
- `::ROUTE_RULES`
  - *Nurse vitals/eMAR*, "Concentrated potassium chloride must NEVER be given by IV push…"; "Vinca alkaloids given intrathecally are universally fatal."
- `::validate_dose_override_reason`
  - *Nurse vitals/eMAR*, "a dose override requires a clinical reason (e.g. …)" if an override is ever wired up


### `server/services/emar_service.py`

- `::_screen_dose`
  - *Nurse vitals/eMAR*, the dose-guard block sentence in the red strip; the invisible duplicate/high-dose warnings; the "not fully checked" unavailable flag
- `::list_administrations`
  - *Vigil chart*, "Given HH:MM" chips and their recorded-by tooltip
- `::record_administration`
  - *Nurse vitals/eMAR*, "not an active order"; "unverified medication, pharmacist verification required"; "patient … is not admitted"; the charted dose behind the green chip; silent success on a carried-forward override


### `server/services/forecast_service.py`

- `::_APPT_TIMES`
  - *Surgical/Appointments*, the time printed in each row's left gutter on synthesized rows
- `::_APPT_TYPES`
  - *Surgical/Appointments*, the bold type line on synthesized rows
- `::_get_cached_or_predict`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, per-patient predicted remaining days; discharge probability; acuity trajectory
- `::_normalize_ward`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, ward grouping in capacity cards and ward distribution
- `::_VISIT_REASONS`
  - *Surgical/Appointments*, the faint reason line on synthesized rows
- `::get_admission_patterns`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Admission Patterns tiles; day-of-week bars; severity and ward distribution bars
- `::get_appointments`
  - *Surgical/Appointments*, every appointment row; every day column and its label; the UTC date that the TODAY chip is compared against; the Sunday skip; the BOOKED badge; acuity chips; appointment types and reasons; the Today's Appointments, This Week Total, Critical Patients and Post-Discharge counts
- `::get_bed_capacity_forecast`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Ward Capacity cards; 48h projection column; capacity totals strip
- `::get_discharge_board`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Discharge Readiness board rows and their sort order
- `::predict_hospital_flow`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Current Census; Beds Available in 48h; 7-Day Discharges; Peak Day; occupancy chart series; daily breakdown rows; Command Center Capacity Forecast 24h strip


### `server/services/helix_service.py`

- `::_atomic_dispense`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, inventory decrement visible as reduced Available Stock after dispense
- `::get_all_orders`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, order rows with patient name, ward, bed, prescriber and substitute name
- `::get_inventory`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, lot quantities behind stock pills
- `::get_medication_catalog`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Formulary medication and generic name cells
- `::transition_order_status`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, legal-transition enforcement behind the modal buttons; transition error text


### `server/services/impact_service.py`

- `::compute_impact`
  - *Admin*, monthly/annual savings, events prevented, per-metric amounts and evidence counts


### `server/services/ingestion_service.py`

- `::_persist_risk_score`
  - *Vigil board*, the persisted risk row the board reads; live risk updates from a real monitor feed
- *(whole file / stylesheet)*
  - *Vigil chart*, the same `vitals:update` event from real HL7 monitor feeds


### `server/services/ipass_service.py`

- ⚠ `::_build_action_list`, **2 screens**
  - *Nurse board*, the "A, Action list" bullets; "OVERDUE (…)"/"Pending (…)" prefixes; "No outstanding actions recorded." when empty
  - *Nurse handover*, every "A, Action list" bullet; "OVERDUE (bundle): element"; "Pending (bundle): element"; "Repeat a full set of observations now…"; "Recheck observations within the hour…"; "Continue routine observations; no outstanding actions handed over."; the "Active medications" and "Open care bundles" lists on the full page
- ⚠ `::_build_illness_severity`, **2 screens**
  - *Nurse board*, the "I, Illness severity" text
  - *Nurse handover*, the "I, Illness severity" sentence; the Unstable/Watcher/Stable word; "NEWS2 n (level)"; "Key drivers: …"; "NEWS2 unavailable"
- ⚠ `::_build_patient_summary`, **2 screens**
  - *Nurse board*, the "P, Patient summary" text; the "NKDA" fallback in it
  - *Nurse handover*, the "P, Patient summary" paragraph; the Diagnosis / Active problems / Allergies fields; "not documented"; "none recorded"; "NKDA"; "?y" for an unknown age
- ⚠ `::_build_situation_awareness`, **2 screens**
  - *Nurse board*, the "S, Situation awareness" text
  - *Nurse handover*, the "S, Situation awareness" sentence; "Latest vitals: HR …"; "Trend over the shift: …"; "ACTIVE DETERIORATION, watch closely."; "Readings analysed: N"; the red "· active deterioration" marker on the full page
- ⚠ `::_build_synthesis`, **2 screens**
  - *Nurse board*, the "S, Synthesis" text
  - *Nurse handover*, the "S, Synthesis" read-back sentence and its blank underscores
- `::_compute_age`
  - *Nurse handover*, the age in the patient summary
- `::_contingencies`
  - *Nurse handover*, every "Contingency planning" bullet on the full page and in the saved note
- `::_get_active_allergies`
  - *Nurse handover*, "Allergies: …" and the NKDA fallback
- `::_get_active_meds`
  - *Nurse handover*, the "Active medications" bullets
- `::_get_active_problems`
  - *Nurse handover*, "Active problems: …"
- `::_get_vitals_window`
  - *Nurse handover*, "Readings analysed: N"; the "Latest vitals" values (including the days-old fallback reading)
- `::_num`
  - *Nurse handover*, the "--" placeholders inside the situation-awareness sentence
- `::_render_plain_text`
  - *Nurse handover*, the "Plain-text handoff" `<pre>`; the exact text stored as `structured_note`; the medication, bundle and contingency blocks that appear only in the saved note
- `::_SEVERITY_MAP`
  - *Nurse handover*, which of Unstable / Watcher / Stable is shown, and therefore which colour the severity chip takes
- ⚠ `::build_ipass`, **2 screens**
  - *Nurse board*, all five I-PASS rows
  - *Nurse handover*, the whole document on both surfaces; `generated_at` in the page sub-line; the 12-hour window; the 404 "Patient not found"
- ⚠ `::save_ipass_handoff`, **2 screens**
  - *Nurse board*, the persisted handover behind "Handed over HH:MM"
  - *Nurse handover*, the saved note's contents; the note id printed on the full page


### `server/services/lens_service.py`

- `::analyze_image_stream`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, streamed findings text in the editable textarea
- `::get_lens_analyses`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Bridge "Lab & Imaging History" rows
- `::save_lens_analysis`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, the analysis row that Confirm & File updates


### `server/services/medication_safety_gate.py`

- ⚠ `::enforce_medication_safety`, **2 screens**
  - *Vigil chart*, the drug-safety dialog contents and the 428 that blocks the order
  - *Orders*, the blocking modal; the info modal; the "Not screened" strip; the 503 "drug-safety screen could not run" line; the STOPPED reason on a medication order-set item


### `server/services/mfa_service.py`

- `::generate_secret`
  - *Login*, grouped setup secret
- `::provisioning_uri`
  - *Login*, provisioning URI value
- `::verify_code_step`
  - *Login*, authenticator code acceptance or rejection


### `server/services/model_governance_service.py`

- `::list_models`
  - *Admin*, model key, version, task, training data, checkpoint/validated/active flags


### `server/services/nurse_shift_service.py`

- ⚠ `::_build_med_task`, **3 screens**
  - *Nurse board*, med task existence; the 15-minute overdue grace; the due-soon window; "N not charted"; the AI-extracted/unverified greyed Administer button; "last given" timestamp
  - *Nurse vitals/eMAR*, drug row name/dose/route/frequency; due time and "40m late"; "N earlier doses not charted"; red overdue left edge; the unverified amber strip and disabled Administer button
  - *Round mode*, "N medications overdue"; unverified-medication decision rows
- `::_clean_obs`
  - *Nurse vitals/eMAR*, "must be between X and Y" and "an observation set must contain at least one reading" in the red error note
- ⚠ `::_load_board_rows`, **2 screens**
  - *Nurse board*, which patients appear at all; name/bed/MRN/age/sex/diagnosis; the latest-vitals row behind every NEWS2 badge; the allergy strip; the medication list on every round
  - *Round mode*, queue rows; bed number; patient name; diagnosis; allergy chips; medication rows
- `::_preview_allergy`
  - *Nurse vitals/eMAR*, the per-med allergy screen carried on the board payload (computed, currently not rendered)
- `::_score_from_vitals`
  - *Round mode*, "NEWS2 N" row; "No observations recorded"
- ⚠ `::build_board`, **3 screens**
  - *Nurse board*, all five count pills; patient card order; every card field; every task row
  - *Nurse vitals/eMAR*, the allergy strip contents; the Scale-2 note; every drug row on the round
  - *Round mode*, ward name subtitle; "ward data as of HH:MM"; queue rows; allergy chips; NEWS2 row; "Observations overdue"; "N medications overdue"
- `::dispatch_observation_alert`
  - *Nurse vitals/eMAR*, the "⚠️ VIGIL ALERT [NURSE OBS]" message that appears in Relay after a non-stable set is charted
- ⚠ `::obs_schedule`, **3 screens**
  - *Nurse board*, the obs task detail line ("NEWS2 5-6 · minimum 1-hourly"); the red escalation response on a patient card; when an obs task turns overdue at all
  - *Nurse vitals/eMAR*, the band label; the RCP response sentence; the single-parameter-3 wording; the interval that later makes obs read as overdue
  - *Round mode*, "due every N min" inside "Observations overdue"; the RCP response text appended to the NEWS2 driver line
- ⚠ `::parse_frequency`, **2 screens**
  - *Nurse board*, which med round group a dose lands in; "As required" / "Continuous" / "Stat" / "No fixed schedule" wording; the exclusion of untimeable meds from the task list
  - *Nurse vitals/eMAR*, which group a drug lands in; "As required"; "Continuous"; "Stat, give now"; "No fixed schedule"
- ⚠ `::record_observation`, **2 screens**
  - *Nurse board*, the NEWS2 and level in the green confirmation flash; the new obs fact line and badge after saving
  - *Nurse vitals/eMAR*, the charted reading; "replayed" behaviour on a double-tap; the correction path when the same time carries different values
- `::score_observation`
  - *Nurse vitals/eMAR*, the previewed NEWS2 and its schedule


### `server/services/order_sets_service.py`

- `::_dose_screen`
  - *Orders*, dose-guard STOPPED reasons; the silent WARN when the guard itself could not run
- `::_validate_item`
  - *Orders*, the startup refusal of a protocol item the dose guard stops; the fields available on every preview row
- `::apply_set`
  - *Orders*, "Applied, N draft order(s) created and audited."; the draft orders that then appear on the Individual-orders list with source "order_set"; the unresolved-code apply refusal
- `::get_set`
  - *Orders*, the detail heading; the citation; the "Unknown order set" preview failure
- `::list_applications`
  - *Orders*, the "Already applied to this patient" rows and their set name, time, actor and order count
- `::list_sets`
  - *Orders*, protocol card name, indication, item count and guideline name
- `::load_config`
  - *Orders*, whether the screen has any protocols at all; the "Protocols v1" version string; the mandatory citation on every card
- `::OrderSetBlocked`
  - *Orders*, the blocked-apply callout; whether it arrives as 428 or 422
- `::preview_set`
  - *Orders*, the whole item list; `would_apply`, which enables or disables Apply
- `::require_orderable_patient`
  - *Orders*, the "not admitted"/"not found" preview failure
- `::resolve_items`
  - *Orders*, which items appear in the preview list and with what dose/route/frequency
- `::screen_items`
  - *Orders*, every Cleared and STOPPED chip; every block reason sentence
- `::unknown_codes`
  - *Orders*, the amber unresolved-codes note; the apply refusal that names them


### `server/services/orders_service.py`

- `::add_result`
  - *Orders*, the "Result: …" line; the RESULTED chip; the "Cannot result order_type" refusal
- `::create_order`
  - *Orders*, the new order card and its DRAFT chip; the "Unknown <system> code" and "requires a coded concept" error lines
- `::LEGAL_TRANSITIONS`
  - *Orders*, which action buttons a card shows; when they all disappear
- `::list_orders`
  - *Orders*, the order list and its newest-first order
- `::transition_order`
  - *Orders*, legal advance buttons; the "Illegal transition" alert; idempotent no-ops


### `server/services/patient_story_service.py`

- `::get_patient_story`
  - *Round mode*, overnight event rows; "No plan documented today yet"


### `server/services/patient_timeline_service.py`

- `::_r_note`
  - *Round mode*, the "Manual Entry" row a saved round plan produces in the overnight column
- `::build_chart_timeline`
  - *Round mode*, overnight event rows (time, title, detail)
- `::unseen_counts_for_scope`
  - *Vigil board*, "N NEW" badge counts


### `server/services/pharmacy_verification_service.py`

- `::list_pending_verification`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, queue row fields including source, screen_failed, interaction_screened, coverage_note and safety alerts
- `::reject_medication`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, row removal after Reject
- `::verify_medication`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, severe-acknowledgement gate behind the 428 row


### `server/services/seasonal_calendar.py`

(Replaced `server/services/pulse_service.py`, deleted 6 October 2026 with Pulse's internet feeds.)

- `::CalendarError.public_message`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, the sentence under "The seasonal calendar could not be loaded."
- ⚠ `::season_report`, **2 screens**
  - *App shell*, Pulse panel place line, review status, This season, Coming up
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, place and date line; review status; This season cards and their order; Coming up lines; state-not-set message


### `server/services/relay_service.py`

- `::_message_row_to_dict`
  - *Relay thread*, sender name; role chip; timestamp; AUTO badge
- ⚠ `::can_access_channel`, **2 screens**
  - *Relay list*, whether an alert row or channel row can be opened at all
  - *Relay thread*, thread-load error box on a 403
- `::get_channel_messages`
  - *Relay thread*, every message bubble; sender name; role chip; timestamp; AUTO badge; message body text
- ⚠ `::get_channels_for_user`, **2 screens**
  - *Relay list*, which channel rows exist at all; channel name; risk chip; ward/bed line; diagnosis line; unread badge; red alert dot on a channel row; case-room empty state; department empty state; Department list ordering under Urgency
  - *Relay thread*, room title; Sage sidebar presence; socket room membership; whether a wrong-room destination is offered
- `::get_user_alerts`
  - *Relay list*, alert row; alert title; alert description; alerts empty state
- ⚠ `::mark_channel_read`, **2 screens**
  - *Relay list*, unread badge (clearing)
  - *Relay thread*, unread badge clearing when the thread is opened
- `::send_message`
  - *Relay thread*, the sent message bubble; idempotent replay behaviour on reconnect


### `server/services/room_fit_service.py`

- `::_original_casing`
  - *Relay thread*, the author's own spelling inside the "The message names …" phrase
- `::auto_clear_flag`
  - *Relay thread*, a wrong-room strip disappearing entirely
- `::check_message`
  - *Relay thread*, whether a wrong-room strip appears; the headline's named-vs-generic form; the matched-surface phrase; the ambiguity sentence; whether "Move to {room}" is offered
- `::flag_row_to_dict`
  - *Relay thread*, resolved-strip labels; resolver's name; suggested room name
- `::get_flags_for_channel`
  - *Relay thread*, wrong-room strips loaded with the thread
- `::link_redirected_message`
  - *Relay thread*, the resolved strip's link back to the copied message
- `::persist_flag`
  - *Relay thread*, the wrong-room strip surviving a page reload
- `::resolve_flag`
  - *Relay thread*, grey resolved strip; "already resolved" error on the loser of a race


### `server/services/roster_import.py`

- `::apply_import`
  - *Admin*, run id; created/updated/unchanged/skipped summary; admissions summary
- `::field_catalogue`
  - *Admin*, Orb-field select labels and their hover notes
- `::list_runs`
  - *Admin*, Applied imports history rows
- `::plan`
  - *Admin*, planned Line/MRN/Name/Action/Why/Admission rows; counts; absent-from-roster list
- `::sniff`
  - *Admin*, delimiter, header line, header candidates, suggested mapping, sample rows


### `server/services/sage_ambient_service.py`

- `::_merge_risk`
  - *Relay thread*, risk card detail, rationale and confidence
- `::_retire_retracted_risk_flags`
  - *Relay thread*, a retracted risk card ceasing to render as "Risk Detected / Active"
- `::_retraction_subjects`
  - *Relay thread*, which risk cards a retraction removes and which it leaves standing
- `::_risk_already_charted`
  - *Relay thread*, suppression of duplicate risk cards; a retracted card staying gone when its message is replayed
- `::sage_ambient_observe`
  - *Relay thread*, live sidebar refresh after a new message


### `server/services/sage_handoff_service.py`

- `::_persist_package`
  - *Surgical/Appointments*, the "AI Generated" badge on a transfer-created case
- `::generate_transfer_surgery_package_async`
  - *Surgical/Appointments*, the Emergency Intake popup appearing (twice per case); the emergency banner refreshing live; the transfer case's risk text, complications, checklist, instruments and team


### `server/services/sage_proactive_service.py`

- `::get_active_insights`
  - *Round mode*, Sage insight issue rows; "Sage suggests: …" decision rows


### `server/services/sage_service.py`

- `::_assemble_patient_context`
  - *Sage*, "Partial data" banner; patient chip; Patient Facts section; action-card Patient row
- `::_audit_out_of_scope`
  - *Sage*, out-of-department refusal (fail-closed audit precondition)
- `::_get_guideline_context`
  - *Sage*, source rows; trust-tier labels; "Limited evidence" banner
- `::detect_mutation_intent`
  - *Sage*, action card on a chart-change request; dots-only (fully buffered) turn
- `::get_conversation_history`
  - *Sage*, New Chat genuinely starting a new thread
- `::get_relevant_context`
  - *Sage*, out-of-department refusal text; source chips; "Limited evidence" banner; "Partial data" banner; patient chip
- `::list_conversation_turns`
  - *Sage*, Recent Conversations row titles; "General Medical" preview line
- `::OUT_OF_SCOPE_NOTICE`
  - *Sage*, out-of-department refusal text
- `::query_ollama`
  - *Sage*, streaming answer text; "Answered by the smaller model" banner


### `server/services/scribe_service.py`

- `::save_scribe_note`
  - *Nurse handover*, the persisted handoff row that makes "Handed over HH:MM" true; one extra row per "Save again" press
- `::transcribe_audio`
  - *Round mode*, transcript text in the plan textarea


### `server/services/sepsis_bundle_service.py`

- ⚠ `::_decorate`, **2 screens**
  - *Vigil chart*, per-element state pills; "N of 6 done"; on-track vs past-target wording; "NN% complete"
  - *Nurse handover*, whether an element reads OVERDUE or Pending; the "N% compliant" figure on the full page; "all elements complete"
- ⚠ `::get_bundles_for_patient`, **2 screens**
  - *Vigil chart*, Care Bundle card data
  - *Nurse handover*, which bundles reach the action list
- `::maybe_autostart`
  - *Vigil chart*, the Trigger line on an auto-opened bundle


### `server/services/shadow_export_service.py`

- `::deidentification_manifest`
  - *Admin*, "What the export removes" list
- `::small_cell_k`
  - *Admin*, the small-cell suppression note under the export


### `server/services/shadow_scorecard_service.py`

- `::_readiness`
  - *Admin*, the seven Prerequisites rows
- `::instrument_health`
  - *Admin*, provenance grid values; instrument lag
- `::scorecard`
  - *Admin*, the four metric bands; period; wards; phase label; coverage; bed-days


### `server/services/surgery_escalation_service.py`

- `::_finalize_surgery`
  - *Surgical/Appointments*, the AI Generated / Fallback badge; the risk text, complications, checklist, instruments and team on a generated emergency case
- `::acknowledge_emergency`
  - *Surgical/Appointments*, the case leaving the emergency banner after Acknowledge
- `::FALLBACK_DATA`
  - *Surgical/Appointments*, the generic checklist, instruments and "Assigned on arrival" team shown after a failed enrichment
- `::generate_surgical_prep_package_async`
  - *Surgical/Appointments*, the "Generating risk assessment via MedGemma…" state and its exit; the 60-second ceiling that turns it into the Feature-under-development dialog


### `server/services/surgery_service.py`

- `::_staff_display_names`
  - *Surgical/Appointments*, the team names inside the prep alert payload
- `::generate_risk_assessment`
  - *Surgical/Appointments*, the AI Risk Assessment text; the "⚠️ AI-assisted assessment (offline fallback)" tail when Ollama is unreachable
- `::get_surgery_detail`
  - *Surgical/Appointments*, the risk prompt's patient facts; the real `imported_context` that the detail route does not use
- `::get_weekly_schedule`
  - *Surgical/Appointments*, nothing (dead code; the only `is_today` producer in the tree, called by no route)
- `::trigger_surgery_prep`
  - *Surgical/Appointments*, who receives the prep alert; the silent "duplicate" that still shows "Alert Sent"


### `server/services/terminology_service.py`

- `::get_display`
  - *Orders*, the text after "✓" and the auto-filled Description
- `::validate_code`
  - *Orders*, the ✗ / ✓ validation hint; the rejection of an unknown code on create


### `server/services/thinking_mode.py`

- `::classify_floor`
  - *Sage*, floor-banner reason clauses ("it is about a specific patient", "it involves a medication or a dose", "it is about a score Orb computes itself")
- `::decide`
  - *Sage*, ⚡ fast banner; 🛡️ floor banner
- `::fast_mode_available`
  - *Sage*, thinking/fast toggle having no effect on an appliance with one model
- `::select_provider`
  - *Sage*, which model answered; downgrade banner
- `::stamp`
  - *Sage*, all provenance banners; "Answered by the smaller model" banner fields


### `server/services/timeline_service.py`

- `::_current_phase_index`
  - *Vigil chart*, which phase card shows the "Now" badge
- `::_heuristic_projection`
  - *Vigil chart*, the "Estimated" four-phase course shown when Ollama is unavailable
- `::get_timeline_projection`
  - *Vigil chart*, Clinical Course phases, expected stay, rationale, discharge criteria, provenance chip


### `server/services/vigil_service.py`

- ⚠ `::compute_news2`, **6 screens**
  - *Vigil board*, risk dot colour; risk percentage; percentage text colour; solid red emergency row; EMERGENCY badge; critical float to top of board; "Elevated Risk" tile count; "Critical" tile count
  - *Vigil chart*, NEWS2 aggregate; risk band; driver sentences; per-parameter attribution table; single-parameter escalation warning; the 0-1 index the gauge ring fills to
  - *Nurse board*, the number on every NEWS2 badge; critical/elevated/stable colours; the single-parameter-3 corner flag; the "critical" and "elevated" count pills; card top-border colour; sickest-first ordering of tasks and cards
  - *Nurse vitals/eMAR*, the NEWS2 number; badge colour; the single-parameter "3" flag; per-field "+N" chips; driver pills; "All vitals within normal range"; "Within range on what was measured (N/5 parameters)"; the SpO₂ Scale-2 tooltip clause
  - *Nurse handover*, the severity banding; the driver phrases ("Tachypnea (RR 23)", "Severe hypotension (SBP 84)", …); "All vitals within normal range"; "Within range on what was measured (n/5 parameters)"; the badge colour on each handover row
  - *Round mode*, "NEWS2 N" label; its severity colour and ordering; the driver list; "SpO₂ Scale 2"
- `::generate_patient_analysis`
  - *Vigil chart*, every sentence of the MedGemma AI Assessment
- ⚠ `::get_patient_risk`, **2 screens**
  - *Vigil chart*, bed badge level and NEWS2 number; risk gauge colour, ring fill and number; drivers list; CfC escalation of the level
  - *Nurse handover*, the NEWS2 number and level behind the severity word; the driver phrases; the CfC escalation that can raise a quiet patient to Unstable
- ⚠ `::get_vitals_trend`, **2 screens**
  - *Vigil chart*, Trend: Deteriorating / Improving / Stable line
  - *Nurse handover*, "Trend over the shift: deteriorating (HR +12.0, SBP -14.0)"; "insufficient_data"; "insufficient recent data"


### `server/services/vitals_source/simulator_source.py`

- `::SimulatorSource`
  - *Vigil board*, 5-second cadence of live risk changes on the board


### `server/site_config/order_sets.json`

- *(whole file / stylesheet)*
  - *Orders*, every protocol name, indication, citation, item description, dose/route/frequency and note; the "SITE TO COMPLETE" sign-off warning on all five shipped sets


### `server/ws/realtime.py`

- `::_broadcast_presence`
  - *Vigil chart*, the presence roster the chips render
- `::_roster`
  - *Vigil chart*, dedup of one clinician with two tabs into a single chip
- ⚠ `::_user_can_access_channel`, **2 screens**
  - *Relay list*, whether opening a channel row yields live messages (socket room join)
  - *Relay thread*, whether any live update reaches this screen
- `::emit_patient_vitals`
  - *Vigil chart*, live movement of the five vital cells and the sparklines
- `::enter_patient`
  - *Vigil chart*, presence chips appearing for other viewers
- `::join_channel`
  - *Relay thread*, live bubbles, typing line and wrong-room pushes arriving at all
- `::typing`
  - *Relay thread*, the "… is typing…" line and the name shown in it


### `server/ws/scribe_stream.py`

- `::handle_audio_chunk`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, audio level bar movement
- `::handle_end_utterance`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Processing state and utterance count increment
- `::handle_start_session`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, live streaming indicator entering Listening


---

## Frontend files


### `src/App.tsx`

- ⚠ `::App`, **2 screens**
  - *App shell*, Sign Out button; User name; Role · department line; User avatar disc; whole shell mount/redirect
  - *Login*, login route rendering and the redirect that hides this screen from a signed-in user; idle timeout length driven by `authMethod`
- *(whole file / stylesheet)*
  - *Nurse handover*, the `/handoff/:patientId` route and its redirect to the staff login for patients


### `src/api/bridgeClient.ts`

- `::getCareTeam`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Your Care Team card contents
- `::getVitalsTrends`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Your Vitals & Trends card contents


### `src/api/bundles.ts`

- `::cancelBundle`
  - *Vigil chart*, Cancel bundle button
- `::completeBundleElement`
  - *Vigil chart*, Mark done buttons
- `::getPatientBundles`
  - *Vigil chart*, every value in the Care Bundle card
- `::startBundle`
  - *Vigil chart*, Start Sepsis Six button


### `src/api/client.ts`

- `::acknowledgeEmergency`
  - *Surgical/Appointments*, the Emergency Intake popup's Acknowledge and Open Case buttons; the case leaving the emergency banner
- ⚠ `::addClinicalNote`, **2 screens**
  - *Vigil chart*, saves the Add Clinical Note modal; refreshes Notes History
  - *Round mode*, "Saved to the chart"; the new "Manual Entry" note in the overnight column
- `::addMedication`
  - *Vigil chart*, saves the Add Medication modal; triggers the drug-safety dialog
- `::administerMedication`
  - *Nurse vitals/eMAR*, charting a dose; the green "Given"/amber "Not given" chip; stable idempotency key across retries
- `::AdministerPayload`
  - *Nurse vitals/eMAR*, what the Given/Not-given press sends, and, by omission of the two override fields, why a blocked dose cannot be cleared from this screen
- `::admitPatient`
  - *App shell*, Admit Patient modal submit; One-time Bridge credential card
- `::alertSurgicalTeam`
  - *Surgical/Appointments*, what the Alert Team button does; the amber team chips that appear after it
- `::analyzeLensImage`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, AI Reference Read text; analysis id used by Confirm & File
- ⚠ `::apiFetch`, **12 screens**
  - *App shell*, Command palette result rows; Pulse panel error state; MFA modal error strip; Admit Patient modal error strip
  - *Login*, error banner text for every server-side failure; MFA enrolment modal trigger (lifts `X-MFA-Enrollment-Ticket`)
  - *Relay list*, "Alerts could not be loaded." message; alert inline failure line; silent-empty channel lists on failure
  - *Relay thread*, the error text inside the thread-load error box, alert inline error, warning inline error and surgical inline error
  - *Nurse board*, the text of the red board-error notice; the "Insufficient permissions" wording on a 403
  - *Nurse vitals/eMAR*, the sentence that appears in the red error strip for every 4xx/428 guard
  - *Nurse handover*, the wording of every draft/save failure message; the 403 permission error; the 401 re-auth modal
  - *Round mode*, the error text inside both degraded strips and the fatal error card; "Session expired"
  - *Orders*, the typed 428 that raises the blocking safety modal; the structured `order_set_blocked` detail behind the per-item blocked list; the "Insufficient permissions" text; the message shown in the form's red error line; session-expiry handling
  - *Admin*, every Admin panel's error message text; 403 permission message; 401 session-expired dispatch
  - *Surgical/Appointments*, the "Insufficient permissions" text behind every Surgical Suite failure
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, data behind Command Center, Helix, Bridge status/reports and appointment calls
- `::approveHelixOrder`
  - *Sage*, "Approve & Send to Helix" button; Sent to Pharmacy badge; Failed badge
- `::askBridge`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Bridge answer card streaming text
- `::assignLensToPatient`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, assignment result strip
- `::authenticateSocket`
  - *Login*, realtime socket handshake after sign-in
- ⚠ `::buildHeaders`, **3 screens**
  - *Vigil board*, authenticated board fetch (rows appear at all)
  - *Vigil chart*, auth on the AI assessment stream and the QR blob fetch
  - *Surgical/Appointments*, whether either screen authenticates at all (the 401/403 error states)
- `::clearForeignOfflineQueue`
  - *App shell*, Sync-failed badge
- `::clearOfflineQueue`
  - *App shell*, Offline badge, N pending (destroyed on sign-out)
- `::completeSurgery`
  - *Surgical/Appointments*, the Complete Surgery button; the case's disappearance from Active Cases
- `::createOrder`
  - *Orders*, the created order; the `safety` payload behind the info modal; the acknowledged re-submit
- `::createPatient`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Quick Admission result and one-time credential screen
- `::dischargePatient`
  - *Vigil chart*, the discharge success body, timeline and QR block
- `::editScribeNoteDraft`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, saved note body after Save
- `::getAdministrations`
  - *Vigil chart*, "Given HH:MM" chips in the medications table
- `::getAdminOverview`
  - *Admin*, all six Overview stat cards; Recent Parse Failures; Auth Summary
- `::getAlerts`
  - *Relay list*, alert row; alert title; alert description; urgent dot; "→" arrow
- `::getAuditLogs`
  - *Admin*, Audit Logs table and pager; Security table; Trace timeline; Alerts table
- `::getBridgeReports`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Medications / Report History / Lab History / Discharge modal contents; FHIR button enablement
- `::getBridgeStatus`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Your Status card text and badge; Bridge header patient name
- ⚠ `::getChannels`, **2 screens**
  - *Relay list*, channel row; channel name; risk chip; ward/bed line; diagnosis line; unread badge; red alert dot on a channel row
  - *Relay thread*, room title; Sage sidebar presence
- `::getDischargeQrObjectUrl`
  - *Vigil chart*, the discharge QR image (and its "Loading QR…" placeholder)
- `::getEmergencyQueue`
  - *Surgical/Appointments*, the emergency banner; its case count; its case rows; the Emergency Intake popup's patient/procedure/indication text
- `::getIngestionStatus`
  - *Admin*, Ingestion table and Mode label
- `::getMessages`
  - *Relay thread*, every message bubble; post-send thread refresh
- `::getModelRegistry`
  - *Admin*, Model Governance table
- `::getPatientAnalysis`
  - *Vigil chart*, MedGemma AI Assessment text; Glass Window text; AI sections of the discharge and handoff printouts
- `::getPatientDetail`
  - *Vigil chart*, patient name; MRN/age/sex; bed badge; diagnosis; comorbidity tags; all five vital cells; sparklines; risk gauge; drivers; trend line; NEWS2 attribution; lab tiles; medications table; notes history; report lists
- `::getPatientForecast`
  - *Vigil chart*, (fetched, renders nothing)
- ⚠ `::getPatients`, **4 screens**
  - *Sage*, Whisper patient-name hint (dictation accuracy only)
  - *Round mode*, queue rows; "Day N"; "Patient board unavailable" strip
  - *Orders*, every row in both patient pickers
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Scribe patient-link dropdown
- `::getPatientsList`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Lens patient search results
- `::getPatientStory`
  - *Round mode*, overnight event rows; "N more events overnight"; "No plan documented today yet"
- `::getRoomFitFlags`
  - *Relay thread*, wrong-room strips that load with the thread
- `::getSageActions`
  - *Relay thread*, suggested action cards; handled action cards; handoff badge
- ⚠ `::getSageAiPosture`, **2 screens**
  - *Sage*, voice mode's up-front refusal when the local speech engine is not installed
  - *Round mode*, "Local speech recognition is not installed…"
- `::getSageHistory`
  - *Sage*, Recent Conversations list; sidebar empty state
- `::getSageInsights`
  - *Round mode*, Sage insight issue rows; "Sage suggests: …" decision rows; "Sage surveillance flags could not be loaded" strip
- `::getSageMemory`
  - *Relay thread*, current-concern line; discussion-summary paragraph; risk cards
- `::getScribeNoteVerification`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, verification badge state during polling
- `::getSurgeryCases`
  - *Surgical/Appointments*, the Active Cases list; the red count bubble; every team chip
- `::getSurgeryDetail`
  - *Surgical/Appointments*, the entire Tier-3 detail screen; the Tier-3 progressive-hydration refresh; whether the Feature-under-development dialog opens instead
- `::getSurgeryRiskAssessment`
  - *Surgical/Appointments*, the streamed AI Risk Assessment body; the streaming cursor; "Risk assessment unavailable, AI service offline."
- `::getSurgerySchedule`
  - *Surgical/Appointments*, the whole week strip; day cards; day counts; day list rows; day summary footers
- `::getTimelineProjection`
  - *Vigil chart*, Clinical Course phases, expected stay, provenance chip, rationale, discharge criteria
- `::getUnseenCounts`
  - *Vigil board*, "N NEW" badge
- `::getUser`
  - *Round mode*, which stored round is restored (the resume banner)
- `::handleResponseError`
  - *Sage*, "Connection failed" answer text (403/other errors)
- ⚠ `::markChannelRead`, **2 screens**
  - *Relay list*, unread badge (persisted clear)
  - *Relay thread*, unread badge clearing when a thread is opened
- `::offlineMutate`
  - *Relay thread*, Idempotency-Key on every send; negative-id "queued" message
- `::prefetchSageContext`
  - *Vigil board*, faster first Sage answer after opening a row (no visible board element)
- `::prepForSurgery`
  - *Surgical/Appointments*, the Prep for Surgery button and its "Alert Sent" state
- `::processOfflineQueue`
  - *App shell*, Syncing badge; Synced badge; Sync-failed badge
- `::querySage`
  - *Sage*, streaming answer text; stage indicator; banner metadata; details expander data; source chips
- `::querySageVoice`
  - *Sage*, spoken answer text in voice mode
- `::rejectScribeNote`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Rejected badge
- ⚠ `::resolveAlert`, **2 screens**
  - *Relay list*, Confirm button; Escalate button; alert resolved line
  - *Relay thread*, structured-alert Confirm/Escalate outcomes
- `::resolveRoomFit`
  - *Relay thread*, "It's the right patient" and "Move to {room}" outcomes
- `::reviseScribeNote`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, new draft after Create Revision
- `::sendMessage`
  - *Relay thread*, optimistic bubble; "queued" marker; wrong-room strip on your own send
- `::setUser`
  - *Login*, post-login user cache consumed by the shell
- `::signScribeNote`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Signed badge; signed banner
- ⚠ `::socket`, **2 screens**
  - *Relay thread*, live bubbles; typing line; wrong-room strip arriving after the message; resolved-alert strip swap
  - *Surgical/Appointments*, the live emergency banner refresh; the Emergency Intake popup appearing unprompted
- `::startSurgery`
  - *Surgical/Appointments*, the Start Surgery button; the "● In Progress" ribbon; the green card border
- `::structureNote`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Structured AI Note streaming body; note id; initial verification badge; pipeline timing footer
- `::surgicalTeamResponse`
  - *Relay thread*, surgical "✓ Confirm"/"✕ Decline" outcomes
- ⚠ `::transcribeAudio`, **3 screens**
  - *Sage*, dictated question text
  - *Round mode*, transcript text appearing in the plan textarea; "Transcription failed: …"
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, refined transcript text after recording stops
- `::transitionHelixOrder`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Helix order state changes and badge updates
- `::transitionSageAction`
  - *Relay thread*, Acknowledge/Dismiss/Confirm outcomes
- `::UNAUTHENTICATED_AUTH_PATHS`
  - *Login*, error banner showing the real reason instead of "Session expired"; suppression of the session-expired modal after a mistyped PIN


### `src/api/deviceAuth.ts`

- `::bindDevice`
  - *Login*, setup-code paste box submit outcome; tablet name badge after enrolment
- `::getDeviceStatus`
  - *Login*, tablet name badge; PIN pad visibility; "Set up this tablet for PIN sign-in" link visibility
- `::pinLogin`
  - *Login*, PIN input submit outcome; "Invalid staff ID or PIN" banner; PIN lockout banners; "This tablet is not set up for PIN sign-in" banner


### `src/api/ipass.ts`

- ⚠ `::getIpassDraft`, **2 screens**
  - *Nurse board*, the five I-PASS rows; "Drafting handover…"; the draft error notice
  - *Nurse handover*, every value in the five I-PASS rows on the nurse tab and every field on the full-page sheet
- `::getRecentHandoffs`
  - *Nurse handover*, nothing today (no caller); it is the endpoint that would let "N of M handed over" survive a refresh
- ⚠ `::saveIpass`, **2 screens**
  - *Nurse board*, "Handed over HH:MM"; progress counter; "Save again"
  - *Nurse handover*, the saved handover; "Handed over HH:MM"; "Handoff saved as draft note #N."


### `src/api/nurse.ts`

- ⚠ `::currentUserName`, **2 screens**
  - *Nurse board*, the author name stamped into a saved handover note; the "this shift" fallback
  - *Nurse handover*, the name in the saved note's `HANDOVER NOTE, <name>` heading; the literal "this shift" when no session user is stored
- ⚠ `::getShiftBoard`, **3 screens**
  - *Nurse board*, every element on this screen that is not a literal
  - *Nurse vitals/eMAR*, patient identity lines on both sheets; allergy strip; every drug row and every field in it; Scale-2 note
  - *Round mode*, ward name subtitle; "ward data as of HH:MM"; queue rows; allergy strip; NEWS2 row; "Observations overdue"; "N medications overdue"; unverified-medication decision rows
- `::MedTask`
  - *Nurse vitals/eMAR*, the fields available to a drug row, and, by omission of `allergy`, the absence of any pre-emptive allergy warning on the round
- `::previewNews2`
  - *Nurse vitals/eMAR*, the entire live NEWS2 block; per-field "+N" chips; band and response lines; driver pills
- `::recordObservations`
  - *Nurse vitals/eMAR*, charted observation set; the green board flash; the red error line on failure


### `src/api/orderSets.ts`

- `::applyOrderSet`
  - *Orders*, "Applied, N draft order(s) created and audited."; the blocked callout
- `::listOrderSetApplications`
  - *Orders*, the "Already applied to this patient" rows
- `::listOrderSets`
  - *Orders*, the protocol cards; the "Protocols v1 · editable by …" line
- `::previewOrderSet`
  - *Orders*, the item rows; the Cleared and STOPPED chips; the unknown-codes note; whether Apply is enabled


### `src/api/pharmacy.ts`

- `::getPendingVerifications`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Pharmacist Verification Queue rows
- `::rejectMedication`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Reject button outcome
- `::verifyMedication`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Verify button outcome; 428 acknowledgement row


### `src/api/pulse.ts`

- ⚠ `::fetchPulseSeason`, **2 screens**
  - *App shell*, Pulse panel (the one Pulse request)
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, every Pulse panel section


### `src/api/rosterImport.ts`

- `::commitRoster`
  - *Admin*, commit result chips and run id
- `::dryRunRoster`
  - *Admin*, Step 3 count chips; file-issues list; plan table; absent-from-roster block
- `::getRosterFields`
  - *Admin*, Orb-field select options; required-field warning; limits line
- `::listRosterRuns`
  - *Admin*, Applied imports history table
- `::previewRoster`
  - *Admin*, delimiter/row-count line; header-candidate buttons; mapping table rows and samples


### `src/api/scorecard.ts`

- `::emptyScorecard`
  - *Admin*, "No shadow data on this appliance" banner; four definition-only metric bands
- `::getScorecard`
  - *Admin*, the whole Pilot Scorecard tab
- `::METRIC_SPEC`
  - *Admin*, fallback metric labels, definitions, caveats and denominator labels
- `::normalizeMetric`
  - *Admin*, metric state pill (including the defensive downgrades to Insufficient data / Not computable)
- `::normalizeScorecard`
  - *Admin*, provenance grid values; wards/phase/computed scope line; readiness list; export descriptor
- `::scorecardExportUrl`
  - *Admin*, Download de-identified JSON/CSV link targets


### `src/api/thinkingMode.ts`

- `::describeAnswerProvenance`
  - *Sage*, ⚡ fast-answer banner; 🛡️ safety-floor banner; ⚠️ floor-unsatisfied banner
- `::getThinkingMode`
  - *Sage*, thinking/fast toggle state at send time
- `::readStoredThinkingMode`
  - *Sage*, thinking/fast pill state after reload
- `::toggleThinkingMode`
  - *Sage*, thinking/fast pill label and tone; fast-mode standing notice


### `src/components/AdmitPatientModal.tsx`

- `::AdmitPatientModal`
  - *App shell*, Admit Patient modal; Admit modal seven-tab strip; Admit modal tab completeness indicators; Admit modal chip inputs; Admit modal error strip; One-time Bridge credential card; Copy credentials button
- `::ChipInput`
  - *App shell*, Admit modal chip inputs


### `src/components/AiStatusChip.tsx`

- `::AiStatusChip`
  - *App shell*, AI status chip; AI status chip OFFLINE pill; AI configuration popover; Engine running / Engine unreachable pill; Load and Unload buttons; Ready / Not loaded state words; Unload confirmation with impact list; AI config error strip; AI config footer
- `::gb`
  - *App shell*, AI configuration popover (loaded size, memory freed)


### `src/components/AlertPermissionButton.tsx`

- `::AlertPermissionButton`
  - *App shell*, Critical-alert bell
- `::readPermission`
  - *App shell*, Critical-alert bell (disappears when unsupported; disabled when denied)


### `src/components/ClinicalCourseTimeline.tsx`

- `::ClinicalCourseTimeline`
  - *Vigil chart*, Clinical Course card; expected-stay chip; Sage-projected / Estimated provenance chip; phase rail markers; phase names; "Now" badge; typical-days line; phase focus text; rationale paragraph; "Discharge when" criteria chips; "Sage is charting the expected course…" loading line; "Clinical course unavailable" failure line; Clinical Course Retry button


### `src/components/ClockWidget.tsx`

- `::ClockWidget`
  - *App shell*, Clock, date; Clock, time seconds and AM/PM
- `::formatDate`
  - *App shell*, Clock, date
- `::formatTime`
  - *App shell*, Clock, time seconds and AM/PM


### `src/components/CommandPalette.tsx`

- `::CommandPalette`
  - *App shell*, Command palette; Command palette search input; Esc keycap chip; Command palette "Searching…" row; Command palette empty state; Command palette result rows
- `::riskColor`
  - *App shell*, Command palette result rows (risk dot)
- `::selectPatient`
  - *App shell*, Command palette result rows (jump to Patients tab)


### `src/components/DischargePrintView.tsx`

- `::DischargePrintView`
  - *Vigil chart*, discharge preview inside the discharge modal; printed discharge summary; printed discharge timeline


### `src/components/FlightRecorder.tsx`

- `::FlightRecorder`
  - *Admin*, Flight Recorder title and subtitle; timeline and chain-verification surface


### `src/components/HandoffPrintView.tsx`

- `::HandoffPrintView`
  - *Vigil chart*, printed handoff sheet (unreachable from this screen's buttons)


### `src/components/KeyboardShortcuts.tsx`

- `::KeyboardShortcuts`
  - *App shell*, Keyboard shortcuts sheet; Go to shortcut rows; Shortcut sheet empty state; Anywhere shortcut rows; Shortcut sheet close button


### `src/components/MfaEnrollmentModal.css`

- *(whole file / stylesheet)*
  - *Login*, modal backdrop dimming and centring


### `src/components/MfaEnrollmentModal.tsx`

- ⚠ `::groupSecret`, **2 screens**
  - *App shell*, MFA setup key (4-character groups)
  - *Login*, grouped setup secret
- ⚠ `::MfaEnrollmentModal`, **2 screens**
  - *App shell*, MFA enrolment modal; MFA loading view; MFA error view; MFA setup key and Copy; MFA provisioning URI and Copy; MFA verification code input; MFA enrolled confirmation; MFA disable form; MFA disabled confirmation
  - *Login*, modal backdrop; modal title; modal close button; modal error strip; "Preparing your authenticator setup…" loading text; error-view Close button; enrolment steps list; "Setup key" label; grouped setup secret; setup-key Copy button; "Provisioning URI" label; provisioning URI value; URI Copy button; issuer/algorithm hint line; "Verification code" label; verification code input; modal Cancel button; "Enable MFA" button and its "Verifying…" pending label; enrolled success line; enrolled explanation; "wait for the next code" warning; "Continue to sign in" button
- *(whole file / stylesheet)*
  - *Login*, modal focus trap and Escape-to-close


### `src/components/OfflineBanner.tsx`

- ⚠ `::ageInWords`, **4 screens**
  - *Vigil board*, the "N minutes old" age reading in the staleness banner
  - *Vigil chart*, the age phrase inside the staleness banner
  - *Nurse board*, the "N minutes old" reading in the staleness banner
  - *Nurse vitals/eMAR*, the "9 minutes old" age reading in both banners
- ⚠ `::OfflineBanner`, **5 screens**
  - *Vigil board*, "Offline, not live" heading; "Can't reach Orb, not live" heading; "Showing the patient board saved at HH:MM" line; "unknown save time" variant; "no saved copy" variant; "Nothing can be recorded until you reconnect" line; "Try again" button; the banner's total absence when live
  - *Vigil chart*, staleness banner heading; "saved at HH:MM" line; age-in-words; "nothing can be recorded" line; Try again button
  - *Nurse board*, "Offline, not live" heading; "Can't reach Orb, not live" heading; "Showing the shift board saved at HH:MM" line; the unknown-save-time variant; the "no saved copy" variant; the "nothing can be recorded until you reconnect" paragraph; the banner's total absence when live
  - *Nurse vitals/eMAR*, "Offline, not live" heading; "Can't reach Orb, not live" heading; "Showing the patient details saved at HH:MM" line; "Showing the drug chart saved at HH:MM" line; "no saved copy" variant; "Nothing can be recorded until you reconnect" line; the banner's complete absence when live
  - *Nurse handover*, "Offline, not live" / "Can't reach Orb, not live" above the handover list; "Showing the shift board saved at HH:MM"; "no saved copy" variant; the "handovers are refused outright" write line


### `src/components/OrbLogo.css`

- ⚠ *(whole file / stylesheet)*, **2 screens**
  - *App shell*, Orb logo mark (spinning ring, pulsing core)
  - *Login*, Orb logo mark ring rotation; Orb logo mark core pulse


### `src/components/OrbLogo.tsx`

- ⚠ `::OrbLogo`, **2 screens**
  - *App shell*, Orb logo mark
  - *Login*, Orb logo mark


### `src/components/ProvenanceChip.tsx`

- `::loadProvenance`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, provenance chip label (Validated/Unvalidated/Untrained)
- `::ProvenanceChip`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Forecast hero provenance chip; Lens AI Reference Read provenance chip


### `src/components/PulseWidget.css`

- *(whole file / stylesheet)*
  - *App shell*, Pulse trigger button; Pulse panel


### `src/components/PulseWidget.tsx`

- `::CalendarStatus`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, "Draft: not yet reviewed by a clinician" / "Reviewed by" line
- ⚠ `::PulsePanel`, **2 screens**
  - *App shell*, Pulse panel; Pulse loading state; Pulse error state; Pulse footer
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Pulse panel header and close; place and date line; footer; loading state; error state and Try again
- ⚠ `::PulseWidget`, **2 screens**
  - *App shell*, Pulse trigger button
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Pulse trigger button
- `::SeasonBody`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, This season section; Coming up section; empty state; state-not-set message
- `::SeasonCard`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, season card title, Peak now chip, period, summary, Typically rises, Sources
- `::UpcomingList`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Coming up lines


### `src/components/SizedChart.tsx`

- `::SizedChart`
  - *Vigil chart*, defers the full lab trend chart until its container has width


### `src/components/bridge/CareTeamCard.tsx`

- `::CareTeamCard`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Your Care Team card; member initials avatar; care unit line; care-team loading/error/empty states


### `src/components/bridge/VitalsTrendsCard.tsx`

- `::Sparkline`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, per-vital sparkline (and its em-dash fallback)
- `::VitalsTrendsCard`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Your Vitals & Trends card; per-vital value and unit; plain-language status word; vitals disclaimer


### `src/components/charts/useChartTheme.ts`

- `::useChartTheme`
  - *Vigil chart*, axis, grid, tick and tooltip colours of the full lab trend chart; the status colour of its line


### `src/components/command/CommandCenter.tsx`

- `::ageLabel`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, "oldest 3.2h" snapshot age text
- `::CommandCenter`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Command Center title block; Updated timestamp and LIVE pill; reconnecting chip; Census/Occupancy tile; Critical Patients tile; Sepsis Bundle Compliance tile; Downtime Readiness tile and Capture all button; Ward Acuity Heatmap; Active Deteriorations list; Capacity Forecast 24h strip; Operating Room Today table; Command Center loading state; Command Center error state and Retry
- `::complianceTone`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Sepsis Bundle Compliance tile colour
- `::heatIntensity`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, heatmap cell background intensity
- `::pctTone`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Census/Occupancy tile colour; projected utilization colour
- `::readinessTone`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Downtime Readiness tile colour


### `src/components/common/PatientPicker.tsx`

- `::PatientPicker`
  - *Orders*, "Patient" and "Apply to" labels; the search input and its placeholder; the results dropdown; option name/bed/ward/MRN/age lines; option diagnosis line; "Clear" button; "Loading patients…"; "No patient matches …"; the selected-patient footer line; "No patient with that id is on your ward list."; the patient-list failure line with Retry and "enter an id instead"; the "Enter an id"/"Search by name" toggle and the raw-id input


### `src/components/handoff/IpassHandoffView.tsx`

- `::handlePrint`
  - *Nurse handover*, the browser print dialog (and, today, a blank printed page)
- `::IpassHandoffView`
  - *Nurse handover*, "I-PASS Shift Handoff" page title; the identity/provenance sub-line; "Save handoff" / "Saving…"; the **Print** button; "Handoff saved as draft note #N."; the coloured Unstable/Watcher/Stable severity chip; "NEWS2 n (level)"; "Drivers: …"; the patient identity, Diagnosis, Active problems and Allergies fields; "Active medications" list and "None charted."; "Open care bundles" list; "Outstanding items" list; the trend line and its red "· active deterioration"; the "Latest: HR/BP/SpO₂/RR/Temp" line; "Contingency planning" bullets; the Synthesis paragraph; the "Plain-text handoff" `<details>` and its `<pre>`; "Drafting I-PASS handoff…"; "Could not draft handoff: …" and Retry; "No handoff data available."
- `::Section`
  - *Nurse handover*, the bordered card, the small letter tag (I/P/A/S/S) and the section titles
- `::SEVERITY_COLOR`
  - *Nurse handover*, the red/amber/green fill of the severity chip


### `src/components/helix/HelixDashboard.tsx`

- `::HelixDashboard`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Pending Review tile; Verified tile; Scheduled tile; In Transit tile; Delivered tile; Rejected tile; Pharmacist Work Queue table; queue-clear empty state; Fulfillment Pipeline table
- `::MetricCard`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Helix Overview bento metric tiles


### `src/components/helix/HelixFormulary.tsx`

- `::HelixFormulary`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Medication Formulary & Inventory table; Available Stock cell; Active Lots cell; formulary status pills


### `src/components/helix/HelixOrderReviewModal.tsx`

- `::getNextActions`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Begin Review / Verify / Reject / Suggest Substitute / Return to Review / Schedule / Dispense buttons (delivery is the nurse's Deliveries tab, not a pharmacy button)
- `::getStatusBadge`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, order status badges across the work queue, pipeline, patient board and modal
- `::HelixOrderReviewModal`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Order Review modal; Current Status badge; order detail rows; substitute chooser; pharmacist notes textarea; transition action buttons; transition error text


### `src/components/helix/HelixPatientBoard.tsx`

- `::HelixPatientBoard`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Patient Medication Board table; fulfillment count cell; stock pills (Out of Stock / Low / units); patient-board empty state


### `src/components/helix/PharmacyVerifyQueue.tsx`

- `::PharmacyVerifyQueue`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Pharmacist Verification Queue header and Refresh; queue table; editable dose/route/frequency inputs; Verify button; Reject button; acknowledgement row; verify-queue loading state; "Queue clear" empty state; verify-queue error box; success flash line
- `::SafetyAlerts`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, per-alert severity lines
- `::SafetyCell`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Safety alerts cell; "Screening unavailable" pill; "Not screened" pill; coverage note
- `::SourceBadge`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, "AI extracted" / "Agentic" source pill


### `src/components/orders/OrderSets.css`

- *(whole file / stylesheet)*
  - *Orders*, blocked-row red wash and left rule; solid STOPPED chip; green Cleared chip; the disabled Apply button's neutral pair; the warn/error/ok callout recipe; the two-column layout and its 900px collapse


### `src/components/orders/OrderSetsPanel.tsx`

- `::OrderSetsPanel`
  - *Orders*, "Order Sets" title; the safety-promise subtitle; "Apply to" picker; "Protocols v1 · editable by …" line; "Order sets could not be loaded: …" banner; "Loading protocols…"; "No protocols are configured for this site."; protocol cards (name, indication, "N items · guideline"); selected-card accent rule; protocol detail heading; guideline citation; "not yet been signed off by a named consultant" warning; "Running the safety gates…"; "Preview failed: …" and its Retry; the unknown-codes amber note; item rows; item description; item metadata line; item note; "Cleared" chip; "STOPPED" chip and reason; blocked-row red wash; "Apply N order(s)" button and its disabled state; the "N item(s) are stopped by a safety check…" note; "Applied, N draft order(s) created and audited."; "Nothing was ordered." blocked callout and its per-item list; the generic apply-error line; "Already applied to this patient" section and its rows
- `::Placeholder`
  - *Orders*, "No protocol selected" card; "No patient selected" card; the clipboard glyph in both
- `::runApply`
  - *Orders*, "Applying…" label; the success callout; the blocked callout; the generic error line; the refreshed history and re-run preview
- `::runPreview`
  - *Orders*, the item list after a Retry or an apply; "Running the safety gates…"


### `src/components/orders/Orders.module.css`

- *(whole file / stylesheet)*
  - *Orders*, status-chip colours; stat/urgent priority colours; the dashed empty/loading frame vs the red error frame; the disabled Create Order colours; the view-switch tab styling


### `src/components/orders/OrdersPanel.tsx`

- `::addResult`
  - *Orders*, the native result prompt; the "Result: …" line; the gold RESULTED chip
- `::CANCEL_BTN_STYLE`
  - *Orders*, the red-tinted Cancel border; the solid-danger "Confirm cancel" fill
- `::CHIP_CLASS`
  - *Orders*, the six status-chip looks (draft grey, active blue, in progress amber, completed green, resulted gold, cancelled red)
- `::CODELESS`
  - *Orders*, whether the Code System + Code row is shown; whether Create Order requires a validated code
- `::CreateOrderForm`
  - *Orders*, the create-order card; "Order Type" select; "Priority" select; "Code System" select; "Code" input; "Validate" button; "…" / "✓ display" / "✗ unknown code" hints; "Description" input; red form error line; "Cancel" button; "Create Order"/"Creating…" button and its disabled look; the drug-safety modal mount
- `::loadOrders`
  - *Orders*, the order list contents and order; "Loading orders…"; both failure surfaces; the empty state
- `::nextStatus`
  - *Orders*, whether the advance button exists on a card; its label ("→ active", "→ in progress", "→ completed"); its disappearance on terminal orders
- `::OrdersPanel`
  - *Orders*, "Orders" title; "Provider order entry with terminology validation" subtitle; Print button and its "Preparing…" label; "+ New Order"/"Close" button; "Loading protocols…" lazy fallback; the Patient picker row; "Could not refresh orders, showing the last successful load." banner and its Retry; "Loading orders…" block; "Orders could not be loaded" failure card, its body copy and Retry; "No orders yet"/"No patient selected" empty card and clipboard glyph; the order-card list; order description; order type; code chip; priority text; source tag; "Result: …" line; status chip; "→ active"/"→ in progress"/"→ completed" button; "+ Result" button and its window.prompt; Cancel button; "Cancel order?" confirmation row; reason input; "Confirm cancel"; "Keep"; the print portal; transition-failure alert() dialogs
- `::OrdersViewSwitch`
  - *Orders*, "Individual orders" tab; "Order sets" tab; the segmented switch tray; selected-tab accent fill
- `::PRIORITIES`
  - *Orders*, the option lists in the three form dropdowns; the absence of LOCAL from the code-system list
- `::RESULTABLE`
  - *Orders*, whether "+ Result" appears on a card
- `::submit`
  - *Orders*, "Creating…" label; the red form error line; the blocking modal; the info modal; the form closing and the list reloading
- `::transition`
  - *Orders*, chip colour changes after an advance or a cancel; the alert() on failure
- `::validateCode`
  - *Orders*, the ✓/✗/… validation hint; the auto-filled Description


### `src/components/print/MedListPrintView.tsx`

- `::MedListPrintView`
  - *Vigil chart*, printed medication administration record


### `src/components/print/OrdersPrintView.tsx`

- `::OrdersPrintView`
  - *Orders*, "Orb Hospital System"; "Active Orders List"; "Generated: …"; "CONFIDENTIAL MEDICAL RECORD"; the "Patient"/patient-id band; the Type/Description/Priority/Status/Result/Created table; the ", " for a missing result; "No orders recorded for this patient."; "Reviewed by:"/"Date / Time:" signature lines; the automatic print dialog


### `src/components/relay/AlertsSection.tsx`

- `::AlertsSection`
  - *Relay list*, "⚠️ ACTION REQUIRED" heading; alert row; urgent pulsing dot; alert title; alert description; "→" arrow; alerts loading state (renders nothing); "Alerts could not be loaded." message; alerts Retry link; alerts empty state (renders nothing); alert-row navigation target
- `::loadAlerts`
  - *Relay list*, alert row; alert title; alert description; "Alerts could not be loaded." message


### `src/components/relay/CaseList.tsx`

- `::CaseList`
  - *Relay list*, channel row; red alert dot on a channel row; channel name; risk chip (CRITICAL/ELEVATED/STABLE); ward/bed line; diagnosis line; unread badge; "Show N More ↓" / "Show Less ↑" button; "No case rooms found." empty state; "No department channels found." empty state; "DEPARTMENT" section heading


### `src/components/relay/RelayComponents.css`

- selector `.relay-alert-arrow`
  - *Relay list*, "→" arrow on an alert row
- selector `.relay-alert-desc`
  - *Relay list*, alert description
- selector `.relay-alert-dot--urgent`
  - *Relay list*, urgent pulsing dot on an alert row
- selector `.relay-alert-item`
  - *Relay list*, alert row (red tint and hover)
- selector `.relay-alert-title`
  - *Relay list*, alert title
- selector `.relay-empty-state`
  - *Relay list*, "No case rooms found." empty state; "No department channels found." empty state
- selector `.relay-expand-btn`
  - *Relay list*, "Show N More ↓" / "Show Less ↑" button
- selector `.relay-search-clear`
  - *Relay list*, clear (×) button
- selector `.relay-search-icon`
  - *Relay list*, magnifier icon
- selector `.relay-search-input`
  - *Relay list*, search field text; search placeholder
- selector `.relay-search-input-wrapper`
  - *Relay list*, search field; search focus ring


### `src/components/relay/RelaySearch.tsx`

- `::RelaySearch`
  - *Relay list*, search field; search placeholder; magnifier icon; clear (×) button; search focus ring


### `src/components/relay/RoomFitWarning.tsx`

- `::act`
  - *Relay thread*, button busy/disabled state; warning inline error; transition to the resolved strip
- `::RoomFitWarning`
  - *Relay thread*, amber warning strip; ⚠ icon; headline (named and generic variants); body paragraph; matched-surface phrase; ambiguity sentence; "It has already been sent" clause; "It's the right patient" button; "Recording…" label; "Move to {room}" button; "Moving…" label; move tooltip; absent move button on an ambiguous match; warning inline error; grey resolved strip; "Checked, confirmed as the right patient by …"; "Moved to … by …"; "Warning withdrawn automatically" (currently unreachable)


### `src/components/relay/SageAmbientSidebar.module.css`

- *(whole file / stylesheet)*
  - *Relay thread*, sidebar column and mobile drawer; loading pulse; "Sage Copilot" header; summary block; concern line; summary text; section titles; card surface and hover; card title; badges; risk card amber rule; risk badge; suggestion badge; stale badge and desaturation; action button row; ghost buttons; filled Confirm button and its disabled state; handled card; handled badge; handoff accepted/pending/failed/unsupported badges; failure state; failure Retry button


### `src/components/relay/SageAmbientSidebar.tsx`

- `::fetchMemory`
  - *Relay thread*, "Connecting to Sage..."; load-failure state; all sidebar content
- `::handleTransition`
  - *Relay thread*, Acknowledge/Dismiss/Confirm busy state; native alert() on failure; card moving between Suggested and Handled Actions
- `::isStale`
  - *Relay thread*, "Stale" badge on risk cards and action cards; desaturated card
- `::SageAmbientSidebar`
  - *Relay thread*, sidebar column; "Connecting to Sage..."; load-failure state; failure paragraph; failure Retry button; hidden (null) state; "Sage Copilot" header; current-concern line; discussion-summary paragraph; "Risk Flags" section title; risk card; "Risk Detected" title; Active/Stale badge; risk detail text; risk "Signal:" line; disappearance of retracted risk cards; "Suggested Actions" section title; action card; action-type title; action state badge; "Target:" line; action "Signal:" line; Acknowledge button; Dismiss button; Confirm button; "..." processing labels; "Handled Actions" section title; handled card; handoff badge (Sent to/pending/failed/Unsupported route); handled state badge


### `src/components/round/Round.css`

- *(whole file / stylesheet)*
  - *Round mode*, two-column body grid; queue rail; current-bed highlight; gold progress bar; three-column summary band and its sticky headings and scroll shadows; allergy chips; loading / empty / failed shapes; degraded strip; mic button and its live red state; blinking record dot; plan textarea; blocked-seen amber panel; oversized action buttons and key caps; hand-on rows and tags; tablet breakpoints; reduced-motion behaviour


### `src/components/round/RoundDictation.tsx`

- `::mmss`
  - *Round mode*, the elapsed timer inside "Stop · M:SS"
- `::RoundDictation`
  - *Round mode*, "Dictate the plan" mic button; "Stop · M:SS"; "Transcribing…"; blinking "Recording" indicator; "Transcribing on this appliance…"; "Press to dictate, or type below."; "Local speech recognition is not installed…"; microphone/transcription error box; "Retry transcription"; "Plan for today" label and textarea; "Save plan to chart" / "Saving…"; "Saved to the chart"; inline save error
- `::runTranscription`
  - *Round mode*, transcript appended to the plan textarea; "Nothing was transcribed from that recording."; "Transcription failed: …"
- `::startRecording`
  - *Round mode*, "The microphone is in use by Sage."; insecure-context message; "Microphone access was denied."; recorder-start failure message; ASR-not-installed message
- `::stopRecording`
  - *Round mode*, "That recording was too short to transcribe."


### `src/components/round/RoundHandoff.tsx`

- `::RoundHandoff`
  - *Round mode*, "Round complete" / "Round so far" title; "N of M seen · K skipped · P not reached"; "Dictated but not saved" block and its warning; "Unsaved plan" rows and tags; "Still to be seen" heading; skipped / not-reached rows and tags; "Every patient on this ward was seen."; "Back to the round"


### `src/components/round/RoundQueueRail.tsx`

- `::RoundQueueRail`
  - *Round mode*, "N of M seen" count; gold progress bar; "N left · K skipped" / "Every bed seen"; queue rows; bed number; patient name; current-bed highlight; queue-row screen-reader label
- `::STATUS_WORD`
  - *Round mode*, queue-row status word ("Seen" / "Skipped, still owed" / "Not yet seen")
- `::StatusMark`
  - *Round mode*, queue-row status icon (tick / arrow-to-bar / open circle)


### `src/components/round/RoundStates.tsx`

- `::RoundDegraded`
  - *Round mode*, "Ward board unavailable" strip; "Patient board unavailable" strip; "Sage surveillance flags could not be loaded" strip; strip "Retry" chip
- `::RoundEmpty`
  - *Round mode*, "No patients to round on"; "Nothing recorded overnight"; "No active issues"; "Nothing outstanding"; "Round complete"
- `::RoundError`
  - *Round mode*, "The ward round could not be loaded"; "Overnight events unavailable"; "display failure, not an all-clear" warning; "Retry" button
- `::RoundLoading`
  - *Round mode*, "Loading the ward round…"; "Loading overnight events…"; "Opening the next bed…"


### `src/components/round/RoundSummaryView.tsx`

- `::AllergyStrip`
  - *Round mode*, "Allergies" label; "None recorded"; "Unknown, ward data unavailable"; allergy chips with reaction sub-text
- `::RoundSummaryView`
  - *Round mode*, big bed chip; patient name; age/sex/"Day N" meta line; MRN; diagnosis line; "Since HH:00 today/yesterday" heading; overnight event rows; "N more events overnight"; "Current issues" heading; issue rows (dot, label, severity word, detail); "Decide today" heading; decision rows


### `src/components/round/roundQueue.ts`

- `::buildQueue`
  - *Round mode*, queue row order
- `::compareBeds`
  - *Round mode*, queue row order; which bed the round opens on
- `::handoffList`
  - *Round mode*, "Still to be seen" rows
- `::loadRoundState`
  - *Round mode*, "Resumed your round from earlier today"; restored queue-row statuses; restored current-bed highlight
- `::mergeRoster`
  - *Round mode*, queue rows; "No patients to round on"; patient identity block fields
- `::nextIncomplete`
  - *Round mode*, which bed "Seen · next bed", "Skip for now" and N land on; hand-on screen appearing at the end
- `::previousInQueue`
  - *Round mode*, which bed P lands on
- `::progressOf`
  - *Round mode*, "N of M seen"; progress bar; "N left · K skipped"; hand-on subtitle; "Round complete"
- `::purgeForeignRoundState`
  - *Round mode*, absence of another clinician's round on a shared tablet
- `::reconcileStatuses`
  - *Round mode*, "N of M seen" denominator after a discharge; queue rows after a mid-round admission
- `::saveRoundState`
  - *Round mode*, survival of queue-row statuses and current bed across a reload
- `::statusOf`
  - *Round mode*, queue-row status word and icon
- `::todayKey`
  - *Round mode*, whether yesterday's round is resumed or erased


### `src/components/round/roundSummary.ts`

- `::admissionDay`
  - *Round mode*, "Day N" in the patient meta line
- `::changesSince`
  - *Round mode*, overnight event rows; "N more events overnight"
- `::currentIssues`
  - *Round mode*, "NEWS2 N" row; "No observations recorded"; "No ward data for this patient"; "Observations overdue"; "N medications overdue"; Sage insight rows; issue severity word and ordering; "No active issues"
- `::decisionsToday`
  - *Round mode*, unverified-medication rows; "Sage suggests: …" rows; "No plan documented today yet"; "Nothing outstanding"
- `::documentedToday`
  - *Round mode*, "No plan documented today yet"
- `::overnightWindow`
  - *Round mode*, "Since HH:00 today/yesterday" heading; "Nothing recorded overnight" detail


### `src/components/sage/Sage.css`

- *(whole file / stylesheet)*
  - *Sage*, sidebar width and hairline; reading-column width; message gap and enter animation; user bubble surface; assistant avatar ring; banner tones (danger/warning/stopped/floor); stage-status pulse; composer surface and focus ring; think-toggle on/off tones; fast-notice strip; quick-action cards; details panel; source rows and cited badge; action-card badges; markdown typography


### `src/components/sage/SageComposer.tsx`

- `::SageComposer`
  - *Sage*, composer text box and placeholder; auto-growing textarea; Enter-to-send; thinking/fast toggle pill; toggle tooltips; fast-mode standing notice; voice-mode toggle; send button; stop button; voice record/stop button


### `src/components/sage/SageEmptyState.tsx`

- `::SageEmptyState`
  - *Sage*, empty-state Orb logo; "Orb Sage · Clinical Intelligence" eyebrow; time-of-day greeting; "How can I assist…" subtitle; Patient Summary card; Lab Insights card; Discharge Planning card; Drug Interactions card


### `src/components/sage/SageFloatingAssistant.css`

- *(whole file / stylesheet)*
  - *App shell*, Sage floating orb position; Sage raised position on the Sage tab


### `src/components/sage/SageFloatingAssistant.tsx`

- `::SageFloatingAssistant`
  - *App shell*, Sage floating orb (text-only); "Voice is not available here" card; "Open Sage to type" button


### `src/components/sage/SageHistorySidebar.tsx`

- `::SageHistorySidebar`
  - *Sage*, Orb mark + "Sage" wordmark; "Clinical AI" sub-label; "New Chat" button; "Recent Conversations" group label; recent-conversation row title; recent-conversation row preview line; sidebar empty state


### `src/components/sage/SageInsightsFeed.tsx`

- `::SageInsightsFeed`
  - *Vigil board*, Sage insights strip above the list; its complete absence when there are no insights


### `src/components/sage/SageMessage.tsx`

- `::formatForDisplay`
  - *Sage*, detail-section body text (bold/italic/bullets/line breaks)
- `::SageActionCard`
  - *Sage*, action card; "Chart Action: …" title; Drafted badge; Not Available in Pharmacy badge; Sent to Pharmacy badge; Not Executed badge; Failed badge; Patient row; Medication row; footer detail message; "Approve & Send to Helix" button; "Sending..." label; green card border on success
- `::SageDetailsExpander`
  - *Sage*, "Show details"/"Hide details" button; chevron rotation; patient chip; "(discharged)" suffix; "N cited"/"N retrieved" chip; Patient Facts / Local Policy / External Guidance / Clinical Assessment / Caveats / Escalation sections
- `::SageMessage`
  - *Sage*, assistant avatar; user bubble; assistant answer text; typing dots; banner stack container; details expander mount; action card mount; de-emphasised bubble under an action card
- `::SageSources`
  - *Sage*, "Sources Sage cited" title; "Documents Sage read" title; retrieved-only caveat paragraph; source row title; source row meta (trust tier · source · synthetic); "cited" badge; cited-first ordering
- `::sanitizeAssistantContent`
  - *Sage*, visible answer text during streaming; blank bubble before safe blocks arrive


### `src/components/sage/WardBrief.tsx`

- `::WardBriefCard`
  - *Vigil board*, ward brief card above the list; "N admitted hospital-wide" count; deterioration watchlist; failed-brief retry strip


### `src/components/surgery/EmergencyIntakePopup.tsx`

- `::EmergencyIntakePopup`
  - *Surgical/Appointments*, EMERGENCY INTAKE badge and pulse; "Escalated from Relay" caption; patient name; Procedure row; Indication row; Acknowledge button and its "Acknowledging..." state; Open Case button


### `src/components/vigil/AdministerModal.css`

- *(whole file / stylesheet)*
  - *Nurse vitals/eMAR*, administer scrim and card; error strip red wash; five-rights list hairlines and label/value typography; emphasised drug name; confirm input; green confirmed border; amber "not given" block; button styles including the dark-red "Record not given"


### `src/components/vigil/AdministerModal.tsx`

- ⚠ `::AdministerModal`, **2 screens**
  - *Vigil chart*, bedside administration dialog; five-rights check; type-to-confirm field
  - *Nurse vitals/eMAR*, administer scrim; administer card; "Administer medication" title; modal × close; red error strip; five-rights list; Patient row; Drug row; Dose row; Route row; Time "(now)" row; "Confirm drug name" label; the type-to-confirm input and its placeholder; green border on match; barcode-style hint; "Reason not given" block; reason select and its five options; detail input and its switching placeholder; Cancel; "Not given"; "Record not given"/"Recording…"; "Given"/"Recording…" and its tooltip
- `::keyFor`
  - *Nurse vitals/eMAR*, absence of a duplicate dose after a retry (no visible element, but it is why re-pressing Given is safe)
- `::NOT_GIVEN_REASONS`
  - *Nurse vitals/eMAR*, Refused / Held, clinical decision / NPO / Unavailable / Other
- `::submit`
  - *Nurse vitals/eMAR*, "Recording…" state; the red error strip contents; modal closing and the green "Given" chip on success


### `src/components/vigil/CareBundleTimer.tsx`

- `::CareBundleTimer`
  - *Vigil chart*, Care Bundle card; Sepsis Six title; "N/6 done" header count; compliance pill; trigger line; six-element checklist; per-element state pills; Mark done buttons; "NN% complete" footer; Cancel bundle button; "Loading care bundle…"; "No active care bundle"; Start Sepsis Six button; bundle Retry button
- `::ElapsedTimer`
  - *Vigil chart*, live elapsed stopwatch; red breach tint past 60 minutes
- `::formatDuration`
  - *Vigil chart*, stopwatch MM:SS / H:MM:SS / "9d 4h" formats


### `src/components/vigil/PatientStory.tsx`

- `::PatientStory`
  - *Vigil chart*, Story dialog; chronological event feed; type filter chips including Allergies; "since you last looked" watermark; Mark reviewed control


### `src/components/vigil/SafetyAlertModal.tsx`

- `::AlertGroup`
  - *Orders*, "Allergy alerts" heading; "Interaction alerts" heading; one row per alert
- ⚠ `::SafetyAlertModal`, **2 screens**
  - *Vigil chart*, drug-safety dialog; allergy alert group; interaction alert group; "No allergy or interaction alerts were raised."; acknowledgement reason field
  - *Orders*, "Severe drug-safety alerts" title; "Drug-safety review" title; the ✕ close; "Screening result for <drug>"; the red blocked lede; "No allergy or interaction alerts were raised."; the "Not screened" coverage strip; "Clinical justification" label and textarea; "Cancel"; "Acknowledge & Order"/"Submitting…"; the info-mode "Close"
- `::severityClass`
  - *Orders*, red chips for severe/contraindicated; green for mild/minor/low; amber for moderate/unknown


### `src/contexts/ActivePatientContext.tsx`

- `::ActivePatientProvider`
  - *App shell*, Sage patient chip (on the floating orb's answer card, which went with the orb's voice loop on 2026-10-04)


### `src/contexts/ActiveViewContext.tsx`

- `::ActiveViewProvider`
  - *App shell*, Sage floating orb raised position
- `::buildWireViewContext`
  - *Sage*, patient chip on the details button (via server-side patient resolution); patient-scoped answers without naming the patient


### `src/contexts/SageRuntimeContext.tsx`

- `::claimMic`
  - *Round mode*, "The microphone is in use by Sage."
- ⚠ `::SageRuntimeProvider`, **3 screens**
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Scribe's "Microphone is in use by Sage, stop it before dictating."
  - *Sage*, "Microphone is in use by another Sage surface." status; local-transcription listening status; voice mode's refusal when the local speech engine is not installed
  - *Round mode*, "Local speech recognition is not installed…"


### `src/hooks/useFocusTrap.ts`

- ⚠ `::useFocusTrap`, **3 screens**
  - *App shell*, Keyboard shortcuts sheet; MFA enrolment modal; Admit Patient modal
  - *Vigil chart*, Escape-to-close, Tab containment and scroll lock for every chart dialog and the Glass Window
  - *Nurse vitals/eMAR*, Escape closing each sheet/modal; Tab staying inside; focus returning to the button that opened it; the page behind not scrolling


### `src/hooks/useOfflineSnapshot.ts`

- ⚠ `::useOfflineSnapshot`, **5 screens**
  - *Vigil board*, whether the board renders live or labelled-cached rows; which staleness heading appears; cached board on a cold offline start
  - *Vigil chart*, decides whether the chart renders live or cached, and therefore whether the staleness banner appears
  - *Nurse board*, whether the board renders live or labelled-cached; which staleness heading appears; the cached board on a cold offline start; refetch on reconnect
  - *Nurse vitals/eMAR*, whether either sheet renders live or labelled-cached data; which staleness heading appears
  - *Nurse handover*, whether the handover list is built from live or cached patients; whether the tab body renders at all


### `src/hooks/useOfflineStatus.ts`

- ⚠ `::useOfflineStatus`, **2 screens**
  - *Vigil board*, offline vs unreachable wording in the staleness banner
  - *Nurse board*, offline vs unreachable wording; suppression of the red error notice while offline


### `src/hooks/useStreamingTranscription.ts`

- `::useStreamingTranscription`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Speaking/Processing/Listening indicator; audio level bar; utterance count


### `src/hooks/useVisiblePolling.ts`

- ⚠ `::useVisiblePolling`, **2 screens**
  - *Relay list*, unread badge refresh; red alert dot refresh; risk chip refresh; channel row ordering refresh
  - *Vigil board*, 30-second silent board refresh; poll pause while the tab is hidden; immediate refresh on tab return


### `src/i18n/index.ts`

- `::detectInitialLanguage`
  - *Login*, which language every translated label on this screen renders in


### `src/i18n/locales/en.json`

- `::nav.round`
  - *Round mode*, Ward Round nav label; Ward Round nav tooltip
- ⚠ *(whole file / stylesheet)*, **2 screens**
  - *App shell*, Nav item labels; Page subtitle; Role · department line; Admit Patient button; Sign Out button; Security button; Offline/sync badge text; Theme toggle tooltip; Language toggle tooltip
  - *Login*, card title; card subtitle; employee ID label; patient ID label; password label; password placeholder; submit "Sign In"; submit "Signing in…"


### `src/i18n/locales/hi.json`

- ⚠ *(whole file / stylesheet)*, **2 screens**
  - *App shell*, Nav item labels; Page subtitle; Role · department line; Admit Patient button; Sign Out button; Security button; Offline/sync badge text
  - *Login*, Hindi variants of the same eight strings


### `src/index.css`

- selector `.animate-fade-in`
  - *Relay list*, Relay panel entry fade
- selector `.glass-card`
  - *Relay list*, channel row surface; alert row surface
- selector `@keyframes pulse-soft`
  - *Relay list*, pulsing of the channel alert dot and the urgent alert dot
- ⚠ *(whole file / stylesheet)*, **3 screens**
  - *Login*, login card surface (`.glass-card`); login card entrance animation (`.animate-slide-up`)
  - *Vigil board*, `.glass-card` flat opaque surface and hairline; `.animate-fade-in` panel entry; status colour tokens behind every dot, badge and percentage in all three themes
  - *Surgical/Appointments*, `.glass-card` surfaces used by every card on both screens; `.animate-fade-in` on the Appointments board and Active Cases list; the `scale-up` keyframe the Feature-under-development dialog enters on; every `--status-*`, `--accent-*` and `--btn-*` token both screens read


### `src/lib/a11y.ts`

- `::activatable`
  - *App shell*, no mapped element since the 6 October 2026 Pulse rebuild (its only mapped users were the old Pulse rows)


### `src/lib/datetime.ts`

- `::formatUTCDate`
  - *Vigil chart*, report and note dates in the history modals; Admitted date in the discharge modal
- ⚠ `::formatUTCDateTime`, **3 screens**
  - *Vigil chart*, note detail timestamp; report detail timestamp; discharge timeline event dates; care bundle "since" timestamp
  - *Round mode*, "Last set at …" inside "Observations overdue"
  - *Orders*, the timestamp in each application-history row
- ⚠ `::formatUTCTime`, **2 screens**
  - *Vigil chart*, note and report times in the history and report-list modals
  - *Round mode*, the time column on each overnight event row
- `::localDateKey`
  - *Surgical/Appointments*, which day column gets the amber left edge and the TODAY chip; the Today's Appointments count
- ⚠ `::parseUTC`, **2 screens**
  - *Relay thread*, message timestamps; chronological ordering of bubbles
  - *Round mode*, overnight event filtering and ordering; "Day N"; "No plan documented today yet"


### `src/lib/demographics.ts`

- `::sexLabel`
  - *Vigil chart*, the MALE / FEMALE / OTHER / SEX NOT RECORDED word in the MRN line
- `::sexShort`
  - *Surgical/Appointments*, the "M"/"F"/"O" suffix after the age on every appointment row


### `src/lib/humanise.ts`

- `::humaniseEnum`
  - *Admin*, humanised category chip text in the Audit Logs table


### `src/lib/messageMerge.ts`

- `::claimOptimistic`
  - *Relay thread*, optimistic bubble replaced in place by the socket echo, keeping its scroll position
- `::mergeById`
  - *Relay thread*, one bubble per message (no duplicate sends)


### `src/lib/nurseDrafts.ts`

- `::isOwnFreshDraft`
  - *Nurse vitals/eMAR*, refusal (and deletion) of another nurse's restored draft on a shared tablet
- `::obsDraftKey`
  - *Nurse vitals/eMAR*, which patient's draft is restored into the obs sheet


### `src/lib/offline/writeGuard.ts`

- ⚠ `::assertOnlineForWrite`, **5 screens**
  - *Relay thread*, offline-refusal band; suppression of the optimistic bubble when offline
  - *Vigil chart*, the offline refusal alert when saving a clinical note
  - *Nurse vitals/eMAR*, "Cannot record these observations, this device is offline…" red note; "Cannot chart this dose, this device is offline…" red strip
  - *Nurse handover*, "Cannot save this handover, this device is offline. Your entry has not been saved. Reconnect and enter it again."
  - *Round mode*, the offline refusal text in the inline save error
- ⚠ `::offlineRefusalMessage`, **3 screens**
  - *Relay thread*, the exact wording of the offline-refusal band
  - *Nurse board*, the "Cannot save this handover, this device is offline…" text in the handover save-error notice
  - *Round mode*, "Cannot save this plan, this device is offline…"


### `src/lib/orbBus.ts`

- `::gotoTab`
  - *App shell*, Active nav item indicator; Panel region
- `::onGotoTab`
  - *App shell*, Active nav item indicator; Panel region
- `::onShowShortcuts`
  - *App shell*, Keyboard shortcuts sheet
- `::openPatient`
  - *App shell*, Command palette result rows
- `::showShortcuts`
  - *App shell*, Keyboard shortcuts button; Keyboard shortcuts sheet


### `src/lib/roles.ts`

- `::canSeeWardList`
  - *Sage*, absence of pharmacist 403 console errors; Whisper patient-name hint


### `src/lib/shortcuts.ts`

- `::isModalOpen`
  - *App shell*, chord suppression under Admit Patient modal, MFA modal, shortcut sheet
- `::isTypingTarget`
  - *App shell*, Keyboard shortcuts sheet (subtitle promise); chord suppression
- `::modifierLabel`
  - *App shell*, Keyboard shortcuts sheet (⌘/Ctrl keycap)
- `::NAV_SHORTCUTS`
  - *App shell*, Keyboard shortcuts sheet (Go to rows)
- `::resolveNavKey`
  - *App shell*, Active nav item indicator (chord navigation)
- `::visibleNavShortcuts`
  - *App shell*, Keyboard shortcuts sheet (Go to rows); Shortcut sheet empty state


### `src/lib/tablist.ts`

- ⚠ `::rovingTabKeyDown`, **2 screens**
  - *App shell*, Nav item; Active nav item indicator
  - *Nurse board*, arrow/Home/End navigation of the three-tab strip; the strip holding one Tab stop


### `src/pages/ClinicianShell.css`

- selector `.orb-section-title`
  - *Relay list*, "CASE ROOMS" heading; "DEPARTMENT" heading; "⚠️ ACTION REQUIRED" heading
- selector `.relay-chat__sort-controls`
  - *Relay list*, sort chip pair layout
- selector `.sort-btn`
  - *Relay list*, Recency sort chip; Urgency sort chip
- selector `.sort-btn--active`
  - *Relay list*, active state of the Recency/Urgency chips
- ⚠ *(whole file / stylesheet)*, **2 screens**
  - *App shell*, Sidebar rail geometry; Active nav item indicator; Admit Patient button fill; User card; Sign Out button; Top bar row; Page title; Page subtitle; Offline/sync badges; Header icon buttons; Language toggle; Theme toggle; Panel region gutter; AI status chip; AI configuration popover; Responsive icon-rail (≤1024px); Responsive top-bar (≤768px)
  - *Vigil board*, metric tile grid and card padding; metric value and label typography; warn/danger metric colours; "ACTIVE PATIENTS" uppercase section title; section header layout; sort chip base, hover and active tint


### `src/pages/ClinicianShell.tsx`

- `::chunkReloadAllowed`
  - *App shell*, "Updating to the latest version..."; Panel error state
- ⚠ `::ClinicianShell`, **8 screens**
  - *App shell*, Pillar navigation rail; Nav item; Active nav item indicator; Nav item hover tooltip; Admit Patient button; User avatar disc; User name; Role · department line; Security (padlock) button; Sign Out button; Page title; Page subtitle; Offline badge, saved copy read-only; Offline badge, N pending; Syncing badge; Synced badge; Sync-failed badge; Sync-failed dismiss ✕; Keyboard shortcuts button; Language toggle; Theme toggle; Panel region (tabpanel wrappers); "Loading panel..." fallback
  - *Sage*, Sage nav tab; Sage tab panel mount; userName prop feeding the greeting
  - *Relay list*, Messages nav item and icon; the `panel-relay` tabpanel wrapper that keeps this screen mounted
  - *Vigil board*, Vigil tab panel mount; `role` prop that gates the Bed Occupancy tile
  - *Nurse board*, shift tab panel mount; mount-on-first-view so the board does not fetch until visited
  - *Round mode*, Ward Round panel mount; Ward Round panel error boundary
  - *Admin*, Admin panel mount; admin panel error boundary
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, panel mount points for Forecast/Command/Helix/Verify Queue/Scribe/Lens; Pulse trigger button in the top bar; "Loading panel..." Suspense fallback
- ⚠ `::getAccessibleTabs`, **8 screens**
  - *App shell*, Pillar navigation rail; Nav item; Keyboard shortcuts sheet (Go to list); Landing tab on sign-in
  - *Relay list*, whether the Messages tab (and therefore this whole screen) exists for the signed-in role
  - *Vigil board*, presence or absence of the Patients tab per role; tab ordering
  - *Nurse board*, presence of the My Shift tab (nurse only); the shift board being the nurse's landing screen
  - *Round mode*, Ward Round nav item visibility (doctor/surgeon only)
  - *Orders*, whether the Orders tab exists at all for the signed-in role (doctor and nurse only)
  - *Admin*, Admin sidebar item (admin role only)
  - *Surgical/Appointments*, whether a user sees Surgical Suite, Appointments, or neither
- `::isStaleChunkError`
  - *App shell*, "Updating to the latest version..."
- ⚠ `::PanelErrorBoundary`, **2 screens**
  - *App shell*, Panel error state; "Reload panel" button; "Updating to the latest version..."
  - *Surgical/Appointments*, "This panel hit an error and couldn't be shown."; "Updating to the latest version..."
- `::shouldRenderPanel`
  - *Surgical/Appointments*, that both panels always first paint from their loading state; that Vigil.css may not yet be loaded when Appointments renders
- ⚠ `::TABS`, **6 screens**
  - *Vigil board*, "Patients" nav tab label; heartbeat-trace tab icon; Vigil as the landing tab for doctor/surgeon/admin
  - *Nurse board*, "My Shift" nav tab label; clipboard-with-tick tab icon; "Nurse Mode" pillar grouping
  - *Nurse vitals/eMAR*, "My Shift" nav entry that leads to both surfaces
  - *Round mode*, Ward Round nav item (icon, label, tooltip)
  - *Orders*, the left-nav "Orders" entry and its document icon
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Forecast sidebar tab; Command sidebar tab; Pharmacy sidebar tab; Verify Queue sidebar tab; Scribe sidebar tab; Lens sidebar tab
- `::ViewTabSync`
  - *App shell*, Sage floating orb


### `src/pages/HandoffPage.tsx`

- `::HandoffPage`
  - *Nurse handover*, whether the full-page sheet renders; "No patient specified."


### `src/pages/Login.css`

- *(whole file / stylesheet)*
  - *Login*, page canvas; login card; card title; card subtitle; tablet name badge; error banner; all field labels; all text inputs and their focus ring; PIN input digit styling; authenticator code input digit styling; setup-code paste box; PIN hint; setup hint; MFA hint; submit button; both link-style toggle buttons; unused back button style


### `src/pages/Login.tsx`

- `::Login`
  - *Login*, page canvas; login card; Orb logo mark; card title; card subtitle; tablet name badge; error banner; employee ID label; employee ID input; password label; password input; patient ID label; patient ID input; patient password input; PIN-variant employee ID label; PIN-variant staff ID input; PIN label; PIN input; PIN hint; tablet setup code label; setup-code paste box; setup hint; authenticator code label; authenticator code input; MFA hint; "Use a different account" link; submit button; submit pending label "Signing in…"; submit label "Set up this tablet"; submit label "Verify code"; submit label "Sign In"; "Use my password instead" / "Use my PIN" toggle; "Set up this tablet for PIN sign-in" / "Back to sign in" toggle; all client-side validation messages


### `src/pages/PatientShell.tsx`

- `::PatientShell`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Bridge header and patient name; Sign out button; welcome heading; Your Status card; Schedule Follow-Up card; upcoming appointments list; week strip calendar; time slots; department buttons; note textarea; Confirm Appointment button; booking confirmation view; booking error line; My Records panel; Medications/Report History/Lab History/Discharge buttons; Download my record (FHIR) button; records modals; report detail modal; Ask bar; Bridge answer card
- `::PortalModal`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, focus trap, Escape handling and scrim for every Bridge modal


### `src/pages/Relay.css`

- selector `.relay-channel-item`
  - *Relay list*, channel row; alert row (base layout and hover fill)
- selector `.relay-channel-item__alert-dot`
  - *Relay list*, red pulsing dot on a channel row; urgent dot on an alert row (base)
- selector `.relay-channel-item__badge`
  - *Relay list*, unread badge
- selector `.relay-channel-item__name`
  - *Relay list*, channel name
- selector `.relay-channel-list`
  - *Relay list*, channel row stacking; alert row stacking
- selector `.relay-chat__sort-header`
  - *Relay list*, sort header strip
- selector `.relay-panel`
  - *Relay list*, Relay panel root
- selector `.relay-panel__channel-scroll`
  - *Relay list*, channel scroll container (28px region gaps)
- *(whole file / stylesheet)*
  - *Relay thread*, thread column surface; header hairline; back-arrow hover; room title type; message list padding and gaps; thread-error card; Retry chip; message bubble surface and slide-up animation; alert bubble danger tint; sender name; role chip; AUTO badge; timestamp; message body wrapping rule; offline-refusal band; composer row; text input and its focus state; send button fill and disabled state; wrong-room strip amber frame; warning icon; warning headline; warning body; warning buttons and the filled override; warning disabled state; warning focus ring; warning inline error; resolved strip neutral surface; tablet and phone reflow


### `src/pages/Vigil.css`

- ⚠ *(whole file / stylesheet)*, **3 screens**
  - *Vigil board*, patient list spacing; patient row grid and hover; risk dot colours (critical/elevated/stable) and the unstyled null case; name/bed/diagnosis typography; diagnosis ellipsis truncation; risk percentage colours and the null-risk neutral; emergency red fill and forced white text; EMERGENCY badge; "N NEW" badge fill and on-fill label; tablet and phone row layouts; "Load more" button base/hover/disabled
  - *Vigil chart*, command bar stickiness and layout; bed badge tint by risk level; amber vital-value warn state; vital hover tooltip visibility; action button colour families; two-column risk/labs row; risk gauge sizing; trend line tints; lab tile and lab tooltip styling (including :focus-visible); course timeline rail; modal scrim, buttons and disabled button treatment
  - *Surgical/Appointments*, the mono/tabular numerals on the Appointments summary cards and times; the uppercase "Scheduled Appointments" title treatment


### `src/pages/panels/AdminPanel.tsx`

- `::AdminPanel`
  - *Admin*, Admin sub-navigation pill row; the 14 tab pills; active-tab highlight; sub-panel body; Audit-Logs-to-Trace tab jump


### `src/pages/panels/AppointmentsPanel.tsx`

- `::apiFetch`
  - *Surgical/Appointments*, whether the Appointments board loads at all; the "Failed to load appointments" sentence
- `::AppointmentsPanel`
  - *Surgical/Appointments*, "Loading appointments..."; error card and Retry; "Scheduled Appointments" title; subtitle; day-column grid; day column cards and the amber today edge; day-number chip; day label; TODAY chip; "N appts" count; appointment rows and their hover tint; appointment time; acuity chip; patient name; age+sex; BOOKED badge; appointment type; reason line
- `::MetricCard`
  - *Surgical/Appointments*, Today's Appointments card; This Week Total card; Critical Patients card; Post-Discharge card; all four labels, values, and subtitles


### `src/pages/panels/ForecastPanel.tsx`

- `::apiFetch`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Forecast error message text ("API 403"/"API 500")
- `::ForecastPanel`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Orb Forecast Engine hero banner; section navigation pills; Current Census tile; Beds Available in 48h tile; 7-Day Discharges tile; Peak Day tile; 7-day occupancy chart; chart legend; daily breakdown table; Ward Capacity cards; ward status badge; bed grid; ward Now/48h stats; capacity totals strip; Discharge Readiness board; Admission Patterns summary cards; Admissions by Day of Week chart; Distribution Analysis panel; Forecast loading state; Forecast error state and Retry; footer disclaimer
- `::MetricCard`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Current Census tile; Beds Available in 48h tile; 7-Day Discharges tile; Peak Day tile; 30-Day Admissions tile; Avg Length of Stay tile; Discharge Rate tile
- `::MiniBar`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, daily-table occupancy bar; ward Now/48h bars; discharge-probability bar; severity distribution bars; ward distribution bars


### `src/pages/panels/HelixPanel.tsx`

- `::HelixPanel`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Pharmacy Operations header; Overview/Patient Board/Formulary tabs; Helix loading state; "Pharmacy queue did not load" error block and Retry; Order Review modal mount


### `src/pages/panels/LensPanel.tsx`

- `::LensPanel`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Lens dropzone; Browse files button; image preview and Clear; image type select; clinical context input; Analyze Image button; AI Reference Read card; DRAFT review-required pill; Lens disclaimer paragraph; editable findings textarea; Assign to Patient form; patient search results; clinician-responsibility checkbox; Confirm & File button; Skip button; assignment result strip


### `src/pages/panels/RelayPanel.tsx`

- `::alertsKey`
  - *Relay list*, alerts rail refresh/remount behaviour
- `::caseRooms`
  - *Relay list*, case-room list ordering; case-room search filtering; case-room empty state
- `::deptChannels`
  - *Relay list*, Department list ordering
- `::emitTyping`
  - *Relay thread*, typing indicator seen by other members
- `::formatMessage`
  - *Relay thread*, bullet conversion; bold and italic in message body text
- `::formatTime`
  - *Relay thread*, message timestamp; surgical-alert timestamp
- `::handleMessagesScroll`
  - *Relay thread*, pin-to-bottom scroll behaviour
- ⚠ `::handleResolveAlert`, **2 screens**
  - *Relay list*, Confirm button; Escalate button; alert inline failure line
  - *Relay thread*, Confirm/Escalate busy state; alert inline error; thread reload showing the green resolved strip
- `::handleSend`
  - *Relay thread*, offline-refusal band; optimistic bubble; input clearing and restoration; wrong-room strip on your own send
- `::handleSurgicalResponse`
  - *Relay thread*, "✓ Confirm"/"✕ Decline" busy state; surgical inline error; SYSTEM response message appearing in the thread
- `::isNearBottom`
  - *Relay thread*, pin-to-bottom scroll behaviour
- ⚠ `::loadChannels`, **2 screens**
  - *Relay list*, channel row; channel name; risk chip; ward/bed line; diagnosis line; unread badge; red alert dot on a channel row; case-room empty state; department empty state
  - *Relay thread*, room title; Sage sidebar presence
- ⚠ `::loadMessages`, **2 screens**
  - *Relay list*, unread badge (optimistic clear on open)
  - *Relay thread*, "Loading messages…"; thread-load error box; blank list on a failed load; unread-badge clearing; wrong-room strips loaded with the thread
- ⚠ `::RelayPanel`, **2 screens**
  - *Relay list*, Relay panel root (fade-in container); "CASE ROOMS" sort-header heading; Recency sort chip; Urgency sort chip; channel scroll container; alerts rail mount point and remount key; search mount point; case-room list mount point; Department list mount point; Confirm button on a structured alert; Escalate button on a structured alert; alert resolved line ("✓ Confirmed/Escalated by …"); alert inline failure line
  - *Relay thread*, thread/room-list fork; room container split; thread column; back arrow; room title; message list scroll container; "Loading messages…"; thread-load error box; Retry button; absent empty state; message bubble; sender name; AUTO bot badge; role chip; timestamp; "queued" marker; message body text; "(alert could not be fully displayed)" caveat; structured alert card; alert title; Patient/MRN line; Procedure line; OR/scheduled-time line; action-required box; "✓ Confirmed/Escalated by" strip; Confirm button; Escalate button; alert inline error; surgical alert card frame; "⚠ Surgical Alert" label; surgical sender chip; surgical timestamp; surgical alert text; "Role:" line; "✓ Confirm" button; "✕ Decline" button; surgical inline error; hidden legacy ambient_action messages; wrong-room strip mount; auto-cleared flag suppression; typing indicator line; offline-refusal band; composer text input; send button; Sage sidebar mount and its absence on department channels
- `::scrollToBottom`
  - *Relay thread*, pin-to-bottom scroll behaviour; landing at the live end when opening a room


### `src/pages/panels/RoundPanel.tsx`

- `::goTo`
  - *Round mode*, current-bed highlight; blocked-seen warning clearing
- `::hasPlanToday`
  - *Round mode*, "No plan documented today yet"; "Nothing outstanding"
- `::loadRoster`
  - *Round mode*, ward name subtitle; "ward data as of HH:MM"; both degraded strips; "The ward round could not be loaded" message; Sage degraded strip in Current issues
- `::loadStory`
  - *Round mode*, "Since HH:00" event rows; "Loading overnight events…"; "Overnight events unavailable"; "Nothing recorded overnight"; "No plan documented today yet"
- `::markAndAdvance`
  - *Round mode*, queue-row status word and icon; "N of M seen" count; progress bar; "N left · K skipped"; blocked-seen warning; hand-on screen appearance at the end of a round
- `::onKeyDown`
  - *Round mode*, S / K / N / P keyboard actions behind the hint line
- `::refresh`
  - *Round mode*, "ward data as of HH:MM"; every summary value; overnight column reload; both degraded strips clearing
- `::RoundPanel`
  - *Round mode*, "Loading the ward round…" skeleton; "The ward round could not be loaded" error card; "Retry" on the fatal error card; "No patients to round on" empty ward; "Check again" button; "Ward round" page title; ward name + "ward data as of HH:MM" subtitle; "Refresh ward data" button; "Finish / hand on" button; "Resumed your round from earlier today" banner; "Start over" button; "Ward board unavailable" degraded strip; "Patient board unavailable" degraded strip; "There is a dictated plan…" blocked-seen warning; "Save plan and mark seen"; "Discard plan and mark seen"; "Skip for now" button + K key cap; "Seen · next bed" button + S key cap; keyboard hint line; "Round complete" main column; "Opening the next bed…"; plan textarea contents (draft state); "Plan for today" save outcome ("Saved to the chart" / inline save error)
- `::savePlan`
  - *Round mode*, "Saving…" / "Saved to the chart"; inline save error; offline refusal message; "No plan documented today yet" disappearance; new note row in the overnight column


### `src/pages/panels/SagePanel.tsx`

- `::correctClinicalSpelling`
  - *Sage*, corrected drug spellings in the sent question
- `::handleMessagesScroll`
  - *Sage*, follow-the-stream scroll behaviour
- `::handleQuickAction`
  - *Sage*, quick-action card click behaviour
- `::handleStop`
  - *Sage*, Stop button behaviour; "Stopped by user" banner
- `::isNearBottom`
  - *Sage*, follow-the-stream scroll behaviour
- `::loadConversation`
  - *Sage*, recent-conversation row click behaviour; scroll-to-end after load
- `::newConversationId`
  - *Sage*, New Chat thread reset (server-side history forgetting)
- `::refreshHistory`
  - *Sage*, Recent Conversations list; sidebar empty state
- `::SagePanel`
  - *Sage*, Sage workspace root; message list; message list scroll container; user bubble mount; assistant turn mount; stage indicator (retrieving/generating); voice-mode landing screen; floating voice HUD; "Listening... m:ss" timer; "Connection failed" answer text; "Stopped by user" metadata flag; empty-assistant-turn suppression; composer mount; history sidebar mount
- `::startVoiceRecording`
  - *Sage*, "Listening…" status; mic-contention status; mic error statuses; voice HUD timer; refusal when the local speech engine is not installed
- `::stopVoiceRecording`
  - *Sage*, "Processing…" status; "Transcribing with Whisper AI…" status; "No speech detected."; "Transcription failed."
- `::submitQuery`
  - *Sage*, typing dots; streaming answer text; stage indicator; "Stopped by user" banner; "Connection failed" answer text; disappearance of question+placeholder on early Stop; Recent Conversations refresh
- `::toggleVoiceMode`
  - *Sage*, voice-mode toggle active state; "Voice Mode" landing screen; "Starting voice mode…" status


### `src/pages/panels/ScribePanel.tsx`

- `::QuickAdmitModal`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Quick Admission modal; auto-fill chip; admission form fields; admission error message; one-time Bridge credential hand-off screen; Copy credentials button
- `::ScribePanel`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Scribe empty state; Transcript section header; recording timer; Refining chip; live streaming indicator; language selector; transcript box; patient select; format select; Structure Note button; Admit to Vigil button; View Note button; text input; record button; live-stream button; status line
- `::StructuredNoteModal`
  - *Analytics/Pharmacy/Scribe/Lens/Bridge*, Structured AI Note modal; note status badge; verification badge and Retry; Verification Findings panel; signed banner; note body/edit textarea; reject reason input; action error banner; Edit/Save/Cancel/Sign & Finalize/Reject/Create Revision/Close/Admit buttons; pipeline timing footer


### `src/pages/panels/SurgicalSuitePanel.css`

- *(whole file / stylesheet)*
  - *Surgical/Appointments*, skeleton card pulse; error banner and Retry chip; empty-state card; horizontal week strip; 360×480 day cards and their enter/dismiss/expand animations; today card treatment and gold top rule (unreachable); Today badge pill; day count numeral; day list rows; day footer stats; emergency banner radius; day-view enter/fade; back chip; day-view heading sizes; timeline; case cards and hover; time block; timeline dot and stem; procedure/OR/diagnosis/meta typography; risk and success colours; detail enter/exit; detail top nav; Prep button (danger→ok, 140px min-width, spinner); patient bar; avatar disc; meta pills and the clay OR pill; co-morbidity pills; allergy pill; "Allergies not recorded" amber pill; two-column detail grid; section titles; MEDGEMMA badge; enrichment badges; fallback label; risk card 160px/1200px clamp; 44px chevron button; risk spinner; fade mask; risk shimmer; risk body typography; blinking cursor; success gauge; checklist rows; instrument chips; complication dots; loading overlay and spinner; staff cards; handoff blocks


### `src/pages/panels/SurgicalSuitePanel.tsx`

- `::SurgicalSuitePanel`
  - *Surgical/Appointments*, seven-card loading skeleton; failed-to-load banner and Retry; Schedule tab; Active Cases tab; red count bubble; "Loading surgical cases…"; "No active surgical cases" empty state; case-card frame and its emergency/in-progress border; status ribbon; patient initial avatar; patient name and "42y/M · MRN"; procedure name; diagnosis strip with gold rule; "OR:" fragment; "TEAM ASSIGNMENTS" caption; team member chips and their four colour states; Alert Team button; Start Surgery / Awaiting Confirmations button; Complete Surgery button; Refresh Status button; emergency banner and pulsing dot; "N Emergency Cases Pending"; emergency case rows (URGENT, duration, procedure, Pending/Ready chip, diagnosis, patient·bed, EMERGENCY tag); "No surgeries scheduled this week"; day cards; Today badge (never renders); day name and date; day count and Surgery/Surgeries label; day list rows with risk dot; day summary footer (Total OR Time, Elevated Risk, Active Rooms); Week Overview back chip; Tier-2 day heading and "N scheduled procedures"; timeline case cards; case-card time and duration; timeline dot and connector stem; procedure heading and OR tag; diagnosis line; patient line; risk glyph and word; "% success"; Tier-3 back chip; Prep for Surgery / spinner / Alert Sent button; patient bar avatar, name and demographic sub-line (with the blank Bed); co-morbidity pills; allergy pills; "Allergies not recorded" pill; OR pill; time+duration pill; Clinical Handoff Context card (never renders); AI Risk Assessment heading; MEDGEMMA badge; AI Generated / Fallback badge; fallback warning banner; risk header spinner; expand/collapse chevron; "Generating risk assessment via MedGemma…"; risk shimmer bars; risk assessment body formatting (headings, numbered items, bullets, paragraphs); streaming cursor; collapsed-card fade mask; Possible Complications list; Surgical Team cards; Surgical Checklist heading and count; success donut gauge; checklist rows; Instruments Required chips; full-panel loading overlay; Feature-under-development dialog


### `src/pages/panels/VigilPanel.tsx`

- `::AiNarrative`
  - *Vigil chart*, MedGemma AI Assessment heading; "Generating…" label; shimmer placeholder bars; AI narrative text block; "AI analysis unavailable" line; Expand to Glass Window button
- `::fetchPatientsPage`
  - *Vigil board*, every patient row; "Admitted · My Dept" number; "Load more (N of M)" counts; board-failure block; session-expired redirect on 401
- `::formatAdministeredTime`
  - *Vigil chart*, "Given HH:MM" chip time
- `::GlassWindow`
  - *Vigil chart*, Glass Window dialog; Orb AI Clinical Assessment title; Glass Window close button; full-size narrative text
- `::handleSelectPatient`
  - *Vigil board*, row click opening the patient chart; Sage context prefetch on open
- `::isEmergency`
  - *Vigil board*, solid red emergency row; EMERGENCY badge; critical float to top of board
- `::loadMore`
  - *Vigil board*, "Load more (N of M)" button; "Loading…" busy label; additional patient rows; row deduplication
- `::loadPatients`
  - *Vigil board*, patient rows; "Admitted · My Dept" number; board-failure block; Retry button behaviour; silent 30s refresh (no loading flash); preservation of extra loaded pages
- `::loadUnseen`
  - *Vigil board*, "N NEW" badge; badge refresh after closing a chart; no-badge degraded state
- `::ModalOverlay`
  - *Vigil chart*, the scrim and focus trap behind every chart modal
- `::News2Attribution`
  - *Vigil chart*, "Why NEWS2 n?" disclosure toggle; per-parameter contribution table; single-parameter escalation warning; RCP scoring disclaimer
- `::PatientDetailView`
  - *Vigil chart*, Chart container; Staleness banner mount; Back arrow; Patient name; MRN · age · sex line; Bed + risk badge; Diagnosis label; Diagnosis value; Comorbidity tags; HR vital cell; BP vital cell; SPO2 vital cell; TEMP vital cell; RR vital cell; Diag. Reports button; Lab. Reports button; Medications button; Notes button; Story button; HANDOFF button; DOWNTIME button; PRINT RECORD button; PRINT MEDS button; DISCHARGE button; Orb Risk Assessment title; Risk drivers list; Trend line; Lab Trends title; ESTIMATED badge; Lab tiles; Lab tile hover sparkline; Full lab trend modal; Discharge modal; Note detail modal; Report detail modal; Clinical Notes History modal; Diagnostic Reports History modal; Diagnostic Reports modal; Lab Reports modal; Medications modal; Given HH:MM chip; Administer button; Add Clinical Note modal; Add Medication modal; Loading screen; Patient not found screen; Chart unavailable screen
- `::PatientRow`
  - *Vigil board*, patient row tap target; risk dot; patient name; EMERGENCY badge; "N NEW" badge and its tooltip; bed label; diagnosis line; risk percentage
- `::PresenceChips`
  - *Vigil chart*, Presence chips; viewer initials circles; "N others viewing" sentence
- `::realLabTrendData`
  - *Vigil chart*, Lab tile values, units, reference ranges and status colours when real lab documents exist
- `::renderSparkline`
  - *Vigil chart*, HR/BP/SPO2/TEMP/RR hover sparkline tooltips; "TREND (6H)" tooltip header; sparkline min/max y labels; -6h / now x labels
- `::RiskGauge`
  - *Vigil chart*, Risk gauge ring; NEWS2 number in the gauge; gauge level sub-label
- `::sortedPatients`
  - *Vigil board*, row order under the Stability chip; row order under the Recent chip; critical-patient float to top of board
- `::syntheticLabTrendData`
  - *Vigil chart*, Lab tile values when no lab documents exist; ESTIMATED badge; dashed sparkline strokes
- `::VIGIL_PAGE_SIZE`
  - *Vigil board*, number of rows on first load; "Load more" page size
- `::VigilPanel`
  - *Vigil board*, panel root; metric tile strip; ", " unknown-metric fallback; ward brief mount; Sage insights mount; "ACTIVE PATIENTS" heading; sort chip pair; staleness banner mount; patient list container; "Loading patients…"; board-failure block; empty-board blank state; "Load more" button
- `::VitalCard`
  - *Vigil chart*, (defined, renders nothing on this screen)
- *(whole file / stylesheet)*
  - *Nurse handover*, the `HANDOFF` command-bar button that opens the full-page sheet in a new tab


### `src/pages/panels/admin/Admin.css`

- *(whole file / stylesheet)*
  - *Admin*, tab pill shape and active fill; stat-card grid; table wrapper and scroll; failure-row red wash; modal overlay


### `src/pages/panels/admin/AdminBadges.tsx`

- `::OutcomeBadge`
  - *Admin*, outcome badge in Audit Logs, Security, Trace, Alerts and Auth Summary
- `::ParseStatusBadge`
  - *Admin*, Status badge in the Ingestion table
- `::SourceBadge`
  - *Admin*, source badge in Source Distribution, Recent Parse Failures, Ingestion table


### `src/pages/panels/admin/AdminEmptyState.tsx`

- `::AdminEmptyState`
  - *Admin*, shared empty state (icon, title, message)
- `::AdminErrorState`
  - *Admin*, shared error icon, "Something went wrong", message, Retry button
- `::AdminLoadingState`
  - *Admin*, shared spinner + loading message


### `src/pages/panels/admin/AdminOverview.tsx`

- `::AdminOverview`
  - *Admin*, Ingestion Mode card; Audit simulation sub-line; Audit Events (24h) card; audit category chips; Ingested Payloads (24h) card; parsed/failed/dupes chips; Source Distribution card; Alert Activity (24h) card; Vigil/Relay chips; Records Written (24h) card; vitals/observations chips; Recent Parse Failures table; Auth Summary (24h) chips; overview loading and error states


### `src/pages/panels/admin/AlertOperations.tsx`

- `::AlertOperations`
  - *Admin*, alert-type select; alert count and pager; 6 alert table columns; Vigil/Relay type badge; risk-level badge; alerts empty state


### `src/pages/panels/admin/AuditLogTable.tsx`

- `::AuditLogTable`
  - *Admin*, Correlation ID box; Patient ID box; All actions/categories/outcomes selects; apply-filters magnifier; "N events" count; audit pager; 8 audit table columns; correlation trace link; copy-correlation button; humanised category chip; audit empty/loading/error states


### `src/pages/panels/admin/CorrelationTrace.tsx`

- `::CorrelationTrace`
  - *Admin*, correlation search bar; Trace button; trace timeline nodes (dot, connector, action, outcome badge, ms time, summary, meta row); failure-tinted node; both trace empty states


### `src/pages/panels/admin/DataPanel.tsx`

- `::DataPanel`
  - *Admin*, Database Backups heading and count; Create backup button; 3 backup table columns; restores-are-out-of-band note; Data Erasure heading; patient-id input; Erase patient button; erase result block; erase error line; backups empty/loading/error states


### `src/pages/panels/admin/ImpactDashboard.tsx`

- `::ImpactDashboard`
  - *Admin*, impact intro; hero monthly/annual/events-prevented figures; no-data block; metric cards (label, money, value, evidence pill, assumption); tunable assumptions expander and inputs


### `src/pages/panels/admin/IngestionMonitor.tsx`

- `::IngestionMonitor`
  - *Admin*, Mode label; status select; refresh button; "N events" count; 8 ingestion table columns; red parse-error cell; ingestion empty state


### `src/pages/panels/admin/ModelGovernance.tsx`

- `::ModelGovernance`
  - *Admin*, model governance intro; 6 model table columns; checkpoint/validated/active pill badges; bare loading, error and empty lines


### `src/pages/panels/admin/RosterImport.tsx`

- `::RosterImport`
  - *Admin*, roster heading and hint; Step 1 file input, file chip and limits line; Step 2 delimiter/rows line; header-candidate buttons; "Still unmapped and required" warning; column-mapping table and Orb-field selects; Date order / Default department / Update-existing controls; Dry run button; Step 3 count chips; file-issues list; plan table; "first 100 of N" line; absent-from-roster block; audit-note input; "mapping changed" warning; Apply this import button; commit result chips; Applied imports history table; roster loading and error states


### `src/pages/panels/admin/Scorecard.tsx`

- `::ExportPanel`
  - *Admin*, de-identification manifest list; Download de-identified JSON/CSV buttons; "Export unavailable" disabled button and its explanation
- `::Figure`
  - *Admin*, metric value and unit; denominator line; scope line under each figure; state pill; insufficient/unadjudicated sub-lines; blockers list
- `::Ledger`
  - *Admin*, episode ledger "Recorded" and "Materialised negatives" columns; "No events recorded in this window."
- `::MetricBand`
  - *Admin*, "Metric N of 4" eyebrow; Proxy chip; spread line; metric title and definition; alternative-denominators list; adjudication meter and note; unadjudicated warning; disagreements line; assumptions list; "What this cannot show" caveat
- `::Provenance`
  - *Admin*, provenance grid (Source, Record, Schema, Events, Episodes, Corrections, First/Last event, Instrument lag); lanes list; simulator-lane warning
- `::Readiness`
  - *Admin*, "Prerequisites, N of M met" checklist rows
- `::ScorecardView`
  - *Admin*, scorecard title and standfirst; 7/14/30/90 window buttons; Refresh button; scope line (Window/Wards/Phase/Computed); "No shadow data" banner; scorecard footnote


### `src/pages/panels/admin/SecurityActivity.tsx`

- `::SecurityActivity`
  - *Admin*, "N security events" bar; security pager; 8 security table columns (incl. Role, IP Address, Path); failed-row tint; security empty state


### `src/pages/panels/admin/TrustCenter.tsx`

- `::StatusChip`
  - *Admin*, control status chip (pass/warn/fail/unknown)
- `::TrustCenter`
  - *Admin*, trust score ring; Passing/Warnings/Failing/Models-validated stats; tamper-evident audit badge and sealed-row count; audit coverage pills; control checklist rows (rail, name, evidence, remediation); MFA adoption bars and header line


### `src/pages/panels/admin/UsersPanel.tsx`

- `::ChangeRoleModal`
  - *Admin*, Change role & department dialog
- `::ConfirmDialog`
  - *Admin*, Deactivate/Activate confirmation dialog and its red confirm button
- `::CreateUserModal`
  - *Admin*, Create User dialog (User ID, Full name, Role, Department, Temporary password, inline error, Create user button)
- `::MfaChip`
  - *Admin*, MFA On / Off chip
- `::ResetPasswordModal`
  - *Admin*, Reset password dialog; Generate button; Copy/Copied button; session-revocation warning
- `::RoleChip`
  - *Admin*, role chip colours per role
- `::StatusChip`
  - *Admin*, Active / Disabled chip
- `::UsersPanel`
  - *Admin*, "User Management" heading; user count; Create user button; 7 user table columns; Name/ID/Department cells; deactivated-row tint; Role / Reset password / Deactivate / Activate row buttons; toast; users empty/loading/error states


### `src/pages/panels/nurse/HandoverSheet.tsx`

- ⚠ `::HandoverSheet`, **2 screens**
  - *Nurse board*, "Shift handover" heading and hint; "N of M handed over" counter; "No patients to hand over."; per-patient accordion rows; name and bed/diagnosis meta; "Not handed over" / "Handed over HH:MM"; +/− chevron; the five I-PASS rows; "No outstanding actions recorded."; "What are you worried about?" label and textarea; save-error notice; auto-draft caveat; "Save handover" / "Saving…" / "Save again"
  - *Nurse handover*, "Shift handover" heading; the I-PASS hint sentence; "N of M handed over" progress pill; "No patients to hand over." empty state; the handover list; per-patient row card; the full-row toggle button; the NEWS2 badge slot on each row; patient name; "Bed 7 · diagnosis" meta line; "Not handed over" / "Handed over HH:MM" state text; the +/− chevron; the expanded body; "Drafting handover…" loading line; the red draft-failure note; the Retry button; the I-PASS definition list; "I, Illness severity" label and sentence; "P, Patient summary" label and paragraph; "A, Action list" label; the outstanding-items bullet list; "No outstanding actions recorded." fallback; "S, Situation awareness" label and trend sentence; "S, Synthesis" label and read-back sentence; "What are you worried about?" label; the worry textarea and its placeholder; the red save-error line; the auto-draft caveat footnote; "Save handover" / "Saving…" / "Save again" button
- ⚠ `::loadDraft`, **2 screens**
  - *Nurse board*, "Drafting handover…"; the draft error notice and Retry button; the I-PASS content itself
  - *Nurse handover*, "Drafting handover…"; the red failure note's message; the document appearing; the fact that a re-opened row shows a stale draft
- `::noteKey`
  - *Nurse handover*, which typed worry is restored into which row; the shared-tablet leak of one nurse's note to the next signed-in user
- ⚠ `::save`, **2 screens**
  - *Nurse board*, green done border; "Handed over HH:MM"; progress counter increment; "Save again" relabel; the "HANDOVER NOTE, {author}" block appended to the saved note
  - *Nurse handover*, "Saving…" label; the green "Handed over HH:MM"; the green left edge; the progress-pill increment; the "Save again" label; the offline refusal message; the red save-error line; the `HANDOVER NOTE, <name>, <timestamp>` block appended to the saved note; deletion of the local worry copy on success
- ⚠ `::setNote`, **2 screens**
  - *Nurse board*, handover note persistence across an interruption; its removal when the box is cleared
  - *Nurse handover*, the typed worry text; its survival across a refresh; its deletion when the box is cleared
- `::toggle`
  - *Nurse handover*, which row is expanded; the previous row collapsing; the +/− flip; the one-and-only-one fetch per patient


### `src/pages/panels/nurse/MedRoundSheet.tsx`

- ⚠ `::groups`, **2 screens**
  - *Nurse board*, which group a dose appears in; empty groups vanishing; within-group ordering by due time
  - *Nurse vitals/eMAR*, the five group titles; group order; group hints; which rows appear in which group; absence of empty groups; within-group ordering by due time
- ⚠ `::MedRoundSheet`, **2 screens**
  - *Nurse board*, "Medication round" title and subject line; close × and Done; allergy danger/warn banner; NKDA notice; five group headings, hints and count chips; med rows; drug name, detail and timing lines; "N earlier doses not charted"; AI-extracted/unverified block; Administer button and its disabled tooltip; "Given"/"Not given" chip; "No active medications charted for this patient."; derived-times footnote
  - *Nurse vitals/eMAR*, round sheet scrim; wide round card; "Medication round" title; patient identity line; round × close button; staleness banner slot; allergy strip; NKDA strip; group headings and count chips; group hint lines; drug rows; drug name; dose·route·frequency line; timing line; "last given" clause; "N earlier doses not charted" clause; unverified-order amber strip; Administer button and its disabled tooltip; "Given"/"Not given" result chip; "No active medications charted" empty state; derived-schedule footnote; "Done" button


### `src/pages/panels/nurse/News2Badge.tsx`

- ⚠ `::News2Badge`, **3 screens**
  - *Nurse board*, NEWS2 tile on task rows, patient cards and handover rows; "NEWS2" caption; the integer score; critical/elevated/stable fill colours; the ", " no-score badge; the red single-parameter-3 corner flag; the badge tooltip including the SpO₂ Scale 2 mention
  - *Nurse vitals/eMAR*, NEWS2 badge on the obs sheet; "NEWS2" caption; the integer aggregate; red/amber/green badge fill by level; the small red single-parameter "3" flag; the badge tooltip; the em-dash "no observations" variant
  - *Nurse handover*, the NEWS2 badge on every handover row; the bare integer with no "NEWS2" caption; red/amber/green fill by level; the em-dash no-observations variant; the small red single-parameter "3" flag; the badge tooltip


### `src/pages/panels/nurse/NursePanel.css`

- ⚠ *(whole file / stylesheet)*, **3 screens**
  - *Nurse board*, totals pill shape and its danger/warn tints; tab strip underline and active gold rule; section title danger colour and count chip; dashed empty-state boxes; note danger/warn/ok/info colours; button primary/secondary/ghost/disabled states; 44px tap minimum; NEWS2 tile fills and the on-fill text rule; the "3" corner flag; task row left-border colours; OBS blue / MED warm chip fills; lateness text colours; "not charted" amber; patient card grid and top-border colours; allergy strip wash; is-late bold red facts; sheet backdrop and panel; med group and med row styling; handover accordion, done border and hidden-on-phone state text
  - *Nurse vitals/eMAR*, sheet scrim and card; sheet header, body and footer layout; note colours (danger/warn/ok/info/muted); button styles (primary/secondary/ghost/danger/disabled); obs grid and field boxes; red error field border and wash; "+N" chip colours at 0/1-2/3+; chip and segmented-control styling; active chip gold fill; score block; amber driver pills; NEWS2 badge fills per level and the single-3 flag; discard-confirmation overlay and box; drug group headings and count chips; drug row layout; red overdue left edge; green done left edge; amber unverified strip; green/amber result chips; "N not charted" amber text; the full-screen sheet behaviour on a phone
  - *Nurse handover*, handover header layout; the progress pill's rounded outline and tabular figures; row card, its 3px left edge and the green done edge; 44px row target and hover; name and truncated meta typography; state text colour and its disappearance below 560px; the chevron square; the expanded body's top border; I-PASS `dt` accent caps and `dd` body text; the action bullets; the worry label and textarea, its placeholder and gold focus glow; the action row and its stacking on a phone; `nurse-empty` dashed panel; `nurse-note--danger` red wash; primary/ghost/disabled button styles; focus rings


### `src/pages/panels/nurse/NursePanel.tsx`

- `::buildTasks`
  - *Nurse board*, every task row's existence; OBS vs MED kind chip; task title; task detail line; exclusion of PRN/continuous/unscheduled meds; exclusion of not-yet-due meds; sickest-first row ordering
- `::load`
  - *Nurse board*, whether any board renders at all; "Refreshing…" label; red error notice text; 60-second silent refresh; refresh-on-reconnect; refresh after a sheet closes
- ⚠ `::NursePanel`, **3 screens**
  - *Nurse board*, panel root; "My shift" title; department + "updated HH:MM" subtitle; Refresh/Refreshing… button; all five count pills and their danger/warn tints; three-tab strip and its counts; staleness banner mount; green confirmation flash; red error notice; "Loading your shift…"; "Nothing overdue or due in the next 30 minutes."; "Overdue" heading and count chip; "Due within 30 minutes" heading and count chip; RCP/derived-times footnote; "No admitted patients in this scope."; patient card grid; card top colour bar; patient name; identity meta line; diagnosis line; allergies strip; escalation response line; obs fact line; meds fact line; "Record obs" button; "Med round" button and its disabled state; handover tab panel; observation sheet mount; med round sheet mount
  - *Nurse vitals/eMAR*, the "Record obs" and "Med round" buttons that open these sheets; the green success flash after a save; the disabled "Med round" button for a patient with no meds; the quiet board reload after either sheet closes
  - *Nurse handover*, the "Handover" tab button; the handover tab panel wrapper; whether the tab renders at all (the `board &&` guard); "Loading your shift…"; the Offline banner above the list; the 60-second board refresh behind the row headers
- `::openTask`
  - *Nurse board*, obs sheet opening focused on a patient; med round opening focused on one drug
- `::renderTaskRow`
  - *Nurse board*, task row tap target; overdue/due-soon left bar; unlabelled NEWS2 badge on rows; "Bed · Name" caption; OBS/MED chip; task title; task detail; lateness reading and "Not recorded" fallback; "N not charted" marker
- `::showFlash`
  - *Nurse board*, the green "Observations recorded for … NEWS2 N (level). {response}" flash; the "… charted as given/not given." flash; their 6-second lifetime


### `src/pages/panels/nurse/ObsEntrySheet.tsx`

- `::ACVPU`
  - *Nurse vitals/eMAR*, the five consciousness chips and their A/C/V/P/U key badges
- `::FIELDS`
  - *Nurse vitals/eMAR*, field order and tab order; field labels; units; accepted ranges in the error text; which fields get a score chip
- ⚠ `::ObsEntrySheet`, **2 screens**
  - *Nurse board*, "Record observations" sheet title and subject line (entry point from this board); the board refresh and green flash on save
  - *Nurse vitals/eMAR*, observation sheet scrim; observation sheet card; "Record observations" title; patient identity line (name · bed · MRN); observation sheet × close button; offline banner slot; "unsaved observations restored" note; "Severe allergy" red note; "SpO₂ scored on Scale 2" amber note; red save-error line; the six vitals fields; field labels and units; numeric inputs; per-field "+N" score chips; inline field range errors; "Supplemental oxygen" legend; "On air"/"On oxygen" chips; "Consciousness (ACVPU)" legend; ACVPU legend "+3" pill; five ACVPU chips with letter keys; live NEWS2 score block; band label; RCP response line; driver pills; "not yet recorded" caveat; "Scoring…" placeholder; "Fix the highlighted readings" placeholder; "Enter observations to see the NEWS2" placeholder; Cancel button; "Record observations"/"Recording…" button and its tooltips; discard confirmation overlay; "Discard these observations?" title and body; "Keep entering"; "Keep draft & close"; "Discard"
- `::pointsFor`
  - *Nurse vitals/eMAR*, per-field "+N" chips (SpO₂ on either scale); ACVPU "+3" pill
- `::readDraft`
  - *Nurse vitals/eMAR*, restored draft values; "unsaved observations restored" note; silent deletion of another nurse's draft
- `::save`
  - *Nurse vitals/eMAR*, "Recording…" label; red error line; sheet closing on success; the green board flash
- `::setField`
  - *Nurse vitals/eMAR*, rejection of non-numeric keystrokes; dismissal of the restored-draft note


### `src/pages/panels/nurse/time.ts`

- ⚠ `::clockTime`, **3 screens**
  - *Nurse board*, "updated HH:MM" in the subtitle; "Last HH:MM" on the card; "Handed over HH:MM" in the handover list
  - *Nurse vitals/eMAR*, the "Due HH:MM" clock in the timing line
  - *Nurse handover*, the "HH:MM" inside "Handed over HH:MM"
- ⚠ `::dueLabel`, **2 screens**
  - *Nurse board*, med row timing text; "As required"; "Continuous"; "Stat, give now"; "Stat dose given"; "No fixed schedule"; "Due HH:MM · 40m late"
  - *Nurse vitals/eMAR*, "As required"; "Continuous"; "Stat, give now"; "Stat dose given"; "No fixed schedule"; "Due 14:00 · 40m late"
- `::obsLabel`
  - *Nurse board*, the observations fact line on every patient card; "No observations recorded"; the "next obs" vs "obs" wording switch
- ⚠ `::relativeMinutes`, **2 screens**
  - *Nurse board*, "40m late" / "in 2h 10m" / "now" on every task row; the empty string that becomes "Not recorded"
  - *Nurse vitals/eMAR*, "40m late"; "in 2h 10m"; "now"


### `src/print.css`

- ⚠ *(whole file / stylesheet)*, **2 screens**
  - *Nurse handover*, what actually reaches paper when Print is pressed; the `body * { visibility: hidden }` rule and the four-class allow-list that currently excludes the I-PASS sheet
  - *Orders*, the printed orders sheet's black-on-white reset, table rules and signature lines


### `src/services/criticalAlerts.ts`

- `::flashTitle`
  - *App shell*, Tab title "⚠ N critical alerts, Orb"
- `::initCriticalAlerts`
  - *App shell*, Critical-alert bell (what it governs); browser alert toast; alert chime; tab-title flash
- `::isAlertSoundMuted`
  - *App shell*, Critical-alert bell (muted state)
- `::playChime`
  - *App shell*, Critical-alert chime
- `::toggleAlertSoundMuted`
  - *App shell*, Critical-alert bell (click to mute/unmute)
---

## How this index was verified

**Every citation was existence-checked.** All 898 distinct `path::function` pairs *as
written* in the fifteen reverse maps were resolved against the working tree. (898 as
written; 895 after the three mis-named symbols below collapsed into names already in the
index, that is the number Part two indexes.)

- **0 cited files were missing.** Every one of the 237 paths exists.
- **888 of 898 symbols** matched a definition automatically (`def` / `async def` /
  `function` / `class` / `const` / `interface` / CSS selector / JSON key).
- **The 10 that did not match were opened by hand.** Four were real and the matcher was
  wrong, `src/api/client.ts::AdministerPayload` (`interface`, `:1393`),
  `src/api/nurse.ts::MedTask` (`interface`, `:43`), `src/i18n/locales/en.json::nav.round`
  (nested key, `:7`), `src/index.css::@keyframes pulse-soft` (`:476`). **Six were genuine
  name errors** and are corrected above.

**More than 100 symbols were then read in the source**, across roughly 30 files, to check
that the function does what the screen file claims, `server/permissions.py` (all seven
cited symbols), `server/services/vigil_service.py`, `nurse_shift_service.py` (11),
`ipass_service.py` (21), `order_sets_service.py` (13), `room_fit_service.py` (8),
`allergy_interlock.py` (5), `forecast_service.py` (10), `command_center_service.py`,
`sage_service.py` (9), `shadow_scorecard_service.py`, `roster_import.py`,
`orders_routes.py`, `admin_users_routes.py`, `scribe_stream.py`,
`src/pages/ClinicianShell.tsx`, `src/api/client.ts`, `src/api/rosterImport.ts`,
`src/api/scorecard.ts`, `src/components/round/roundQueue.ts` (15 exports),
`roundSummary.ts` (6), `src/components/OfflineBanner.tsx`,
`src/lib/offline/writeGuard.ts`, `src/hooks/useOfflineSnapshot.ts`.

**What is not verified.** Line numbers inside the per-screen files were not re-checked
here, each screen file states its own citations were opened and read at that line. This
index verifies that the *symbols* named in the reverse maps exist and are named correctly;
for a line-level claim, open the screen file entry it came from.

**Keeping it true.** This file is generated by inverting the `## Reverse map
contributions` section at the end of each `screen-map/*.md`. If you add a screen file or
extend an existing one, extend its reverse map in the same `path::function` → elements
format and re-invert; do not hand-edit Part two.
