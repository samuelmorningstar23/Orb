# Product UI wording sweep and website recapture, 4 October 2026

The public website was rewritten on 3 and 4 October to the wording rules in
`docs/fundraising/SALES_PLAN_2026-10.md` section 9. Two reviewers found that the
product captures embedded on the site still said the old words. This note records
the product strings that changed, what was deliberately left, the test results,
and the recapture.

Backups of every file touched, plus the patch, are in
`docs/archive/pre-ui-wording-2026-10-04/`. No behaviour changed: every edit is a
label, a tooltip, a notification body, an API description or a capture caption.

## Strings changed

| File and line | Before | After |
|---|---|---|
| `src/pages/panels/AdminPanel.tsx:30` | Pilot Scorecard | Evaluation scorecard |
| `src/pages/panels/admin/Scorecard.tsx:601` | Shadow-mode scorecard | Evaluation scorecard |
| `src/pages/panels/admin/Scorecard.tsx:603` | "What Orb saw, what it said, and what the ward actually did, accumulated silently, one appended row per observation." | "What Orb saw, what it said and what the ward did, one appended row per observation." |
| `src/pages/panels/admin/Scorecard.tsx:340` | Orb's pilot evidence log | Orb's shadow evidence log |
| `server/services/shadow_scorecard_service.py:153` | Phase A: silent (no clinician sees Orb output) | Phase A: Orb output not shown to clinicians |
| `src/i18n/locales/en.json:62` (subtitle.vigil) | Patient monitoring & risk scoring | Vitals and NEWS2 |
| `src/i18n/locales/en.json:61` (subtitle.today) | Deteriorating patients, decisions waiting on you & new results | Highest NEWS2 scores, decisions waiting on you & new results |
| `src/i18n/locales/en.json:45` (subtitle.forecast) | Predictive intelligence & patient flow | Census against capacity, discharge board & admission patterns |
| `src/pages/panels/VigilPanel.tsx:408` | Elevated Risk | Elevated |
| `src/pages/panels/VigilPanel.tsx:1781` | Orb Risk Assessment | Why this score |
| `src/pages/panels/VigilPanel.tsx:472` (tooltip) | Sort by Risk Stability | Sort by NEWS2 band |
| `src/pages/panels/VigilPanel.tsx:1621` and `:2838` (tooltips) | RCP early-warning table | RCP NEWS2 chart |
| `src/components/command/CommandCenter.tsx:248` | Active Deteriorations | Highest NEWS2 scores |
| `src/components/command/CommandCenter.tsx:260` | No active deteriorations in {scope}. / No active deteriorations. House is stable. | No elevated or critical NEWS2 scores in {scope}. / No elevated or critical NEWS2 scores. House is stable. |
| `src/pages/panels/DoctorHomePanel.tsx:929` | Deteriorating (section header) | Highest NEWS2 scores |
| `src/pages/panels/DoctorHomePanel.tsx:589` and `:593` | Couldn't load deteriorating patients / Loading deteriorating patients | Couldn't load the NEWS2 board / Loading the highest NEWS2 scores |
| `server/services/model_governance_service.py:105` | NEWS2 early-warning score | NEWS2 score |
| `server/services/model_governance_service.py:107` | Early-warning score (NEWS2, including Scale 2 for COPD) | NEWS2 score (including Scale 2 for COPD) |
| `server/services/impact_service.py:81` | Fraction of early-warning (NEWS2) escalations / Sepsis-Six bundles | Fraction of NEWS2 escalations / Sepsis Six bundles |
| `server/main.py:886` (OpenAPI description) | Local-first hospital operating system API | Local-first hospital software API |
| `src/services/criticalAlerts.ts:321` (notification body) | A new emergency case requires triage in the Surgical Suite. | A new emergency case is waiting in the Surgical Suite. |
| `src/pages/panels/BiomedicalPanel.tsx:68` | The calibration register is the only thing that catches that. | The calibration register is the only thing that shows it. |
| `docs/website-assets/capture.cjs:291, 522, 523, 554, 606, 607` | capture titles and flow captions with "NEWS2 risk ordering", "deteriorating patients", "ordered by risk", "caught", "drives escalation", "Pilot Scorecard: how the pilot will be measured" | "ordered by NEWS2 score", "highest NEWS2 scores by name", "flagged", "the NEWS2 table", "Evaluation scorecard: how a ward would be measured ... The figures are seeded demo data." |

Tests updated to the new wording: `CommandCenter.test.tsx`, `DoctorHomePanel.test.tsx`,
`VigilPanel.commandbar.test.tsx`, `admin/ModelGovernance.test.tsx`.

### Already fixed before this sweep (nothing to change)

- Sage proactive "Risk level CRITICAL": gone. The card label now comes from
  `news2_text.news2_description` ("NEWS2 9 (critical) ...") in
  `sage_proactive_service._label`. The old captures predate that change.
- I-PASS "Allergies: NKDA" from an empty list: fixed in the 3 October baseline.
  `ipass_service.py:948-957` prints "No allergies recorded. Confirm with the
  patient." when the structured list is empty, and `admission_service.py:353-360`
  now charts admission allergy chips into `patient_allergies`. The seed also
  writes the ceftriaxone anaphylaxis row, so the recaptured sheet names it.

## Never-say words found and left in place

Code identifiers, comments and docstrings are not listed. These are user-facing or
model-facing strings judged not to be Orb's own claim about itself, or where a
change would alter behaviour. Samuel can overrule any of them.

| Where | Text | Why left |
|---|---|---|
| `src/components/AdmitPatientModal.tsx:545, 692` | "Triage Severity (1-5)", "Triage" | A clinical field the admitting clinician fills (the hospital's own triage category), not Orb claiming to triage. Renaming the field changes clinical vocabulary. |
| `server/services/admission_service.py:472` | "High acuity admission (Triage n)" | Same field, echoed as a Sage risk flag. |
| `server/services/pulse_service.py:585` | "Increase ED triage screening for ILI symptoms" | A Pulse recommendation about the ED's own triage desk. |
| `server/services/bot_service.py:1101-1117` | "Orb Vision detects ...", "Orb Lens detects ...", "Orb AI detects ..." | Simulated imaging-report bot text in the simulation lane (`_create_report`). Lens is marked switched off on the site. Candidates for a rewrite or removal under the no-demo-scaffolding rule; not touched here because the lane's tests may pin them. |
| `server/services/sage_assessment_service.py:1995, 2035` | "hospital operating system" inside two Sage system prompts | Model prompt text. Changing it changes model behaviour. |
| `src/pages/panels/BiomedicalPanel.tsx:373` | "Orb is not the system of record for it here." | Shown only when the equipment module is switched off, and says Orb is NOT the record. |
| `src/i18n/locales/en.json` (subtitle.coding) | "AI medical coding & revenue-integrity CDI" | Near "AI-powered" but not on the list. |
| `server/main.py`, `server/database.py`, `server/services/audit_middleware.py` | HIPAA section citations | Code comments and docstrings. |
| `server/services/vigil_service.py:1-4, 237, 345` | "Risk Scoring Service", "Modified Early Warning Score", "early warning score" | Docstrings and comments. |

## Sepsis Six: a question for Samuel, not decided here

`server/services/sepsis_bundle_service.py:243` `maybe_autostart` opens the Sepsis
Six bundle by itself when NEWS2 >= 7 (`triggered_by="vigil_auto"`, reason "NEWS2 n
(>=7)"). It is called from `nervous_system_triggers.py:42` on every critical
Vigil alert and from `nurse_shift_service.py:1254` after a nurse charts
observations. There is no env gate. The chart shows it in
`src/components/vigil/CareBundleTimer.tsx:371` as "Trigger: NEWS2 7 (>=7)".

The website (branch wording-sweep) says: "The clinician opens Sepsis Six on
the chart: a checklist of six items with a 60-minute target" (SafetySuite.tsx:22
and modulePages.ts:40). One of the two has to move:

- (a) add an env switch so the auto-start is off in the capture and founding
  programme profiles (the clinician then opens the bundle from the chart), or
- (b) change the site back to say Orb opens the bundle at NEWS2 7 and the
  clinician works it.

Neither was done. The recaptured chart still shows the auto-started bundle.

## Tests

- Backend, main venv, bare environment (no `.env`): `test_model_governance.py`,
  `test_scorecard_plain_text.py`, `test_shadow_routes.py`, `test_shadow_scorecard.py`,
  `test_shadow_producers.py`, `test_impact.py`, `test_assessment_tiers.py`,
  `test_hospital_time_surfaces.py`, `test_patients_registered_status_migration.py`,
  `test_wedge_end_to_end.py`, `test_wedge_loop.py`: 344 passed, 2 xfailed.
- Front end (vitest): `CommandCenter.test.tsx`, `DoctorHomePanel.test.tsx`,
  `VigilPanel.commandbar.test.tsx`, `admin/ModelGovernance.test.tsx`,
  `VigilPanel.assessment.test.tsx`: 57 passed.
- `npm run build` in main succeeded; the bundle carries every new string and none
  of the old ones.

## Recapture

Run on 4 October 2026, 16:49 to 17:00 IST, detached under caffeinate on battery
(52% at start, 48% when the gallery began). Log and scripts in the session
scratchpad (`recapture_run.sh`, `recapture.log`, `capture.log`).

- The capture backend ran from a frozen APFS clone of main, not from main: five
  peer sessions were editing `server/*.py` and the dev-profile backend reloads
  on every edit (`reload=is_dev`, no switch). The shared `:8000` backend was
  stopped, its `orb.db` copied into the clone while quiescent, the CLONE's DB
  reseeded (`reset_db_and_seed.py`, 26 s, 3 allergy rows, 2 AI medications in
  the verify queue), and `main.py` started from the clone with
  `ORB_MFA_REQUIRED_ROLES=`. The shared dev DB was not reseeded. After the run
  the plain `.env` backend was restarted from main and answered `/api/health`.
- `capture.cjs --gallery`: exit 0, zero warnings, 84 screens x 2 themes, all 168
  PNGs rewritten, in 7 minutes. `build_assets.py`: exit 0.
- `--flows` was not run. The site does not use the flow frames, the seed now
  fills the pharmacist queue on its own, and the Scribe flow would have driven
  the 27B model on battery. (A `--only scribe-note` attempt was started and
  stopped; the truncated frames it left were restored from HEAD and
  `build_assets.py` re-run, so `frames/`, `gifs/` and `widgets/frames/` are as
  before.)
- Website: worktree `~/Documents/Projects/Orb-website-captures`, branch
  `product-captures-2026-10-04` off `60aeaa3` (wording-sweep, which
  carries the import script's SCREENS titles and FLOW_TEXT overrides). `scripts/import_orb_assets.py`
  imported 61 screens x 2 themes (7.7 MB); FLOW_TEXT still applies (3 override
  captions present, 0 old words in `orbCaptures.ts`). One commit, NOT pushed.
- Captures whose text changed (checked on the dark PNGs): `today-doctor-home`
  (subtitle and section header), `vigil-patient-board` (subtitle, Elevated, Sage
  card labels), `vigil-patient-chart` and `vigil-news2-explained` (Why this
  score), `command-center` (Highest NEWS2 scores), every `admin-*` screen (tab
  label), `admin-scorecard` (title, intro, phase label, source), `admin-models`
  (NEWS2 row text, below the fold in the viewport shot), `handoff-ipass` and
  `nurse-handover-ipass` (allergy line), `forecast-*` (subtitle), `sage-panel`
  and `relay-case-room` (insight and alert wording from the 3 October code).
  Every other screen changed only in clock, vitals and seed times.

Seen in the new pictures and left as is: the I-PASS trend line says
"deteriorating" and the chart's trend chip says "Deteriorating" (clinical trend
statements, not product claims), and the I-PASS active-medication line for the
seeded AI-extracted paracetamol prints "REVIEW REQUIRED Review REVIEW REQUIRED"
(seed placeholders in dose, route and frequency).

