## 3. Vigil: Patient Monitoring & Risk

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![The Vigil patient board. The metric tiles count your own department; the Sage brief beneath them is hospital-wide.](assets/manual/vigil-ward-board.png)

*The Vigil patient board. The metric tiles count your own department; the Sage brief beneath them is hospital-wide.*

Vigil is Orb's live patient board, the screen most clinicians keep open all shift. It shows every admitted patient in your department, continuously scores each one with the NEWS2 early-warning system, streams live vitals into each chart, and layers Sage's hospital-wide situational brief and proactive alerts on top. From Vigil you can open any patient's full chart, order and administer medications, review notes and reports, admit a new patient, and run a complete discharge.

To open Vigil, sign in with your employee ID and password and select **Patients** in the left sidebar. Vigil is the default landing tab for clinical roles, so in most cases it is simply the first screen you see.

### 3.1 The patient board at a glance

The board is arranged top to bottom as: metric tiles, the **Sage, State of the Hospital** brief, the **Sage, needs your attention** feed, and the **Active Patients** list.

#### Metric tiles

| Tile | What it counts |
|---|---|
| **Admitted · My Dept** | Patients currently admitted **under your department only**. Hover the label to see the reminder: "Patients admitted under your department." The number reflects the full server-side count for your department, even if not all rows are loaded yet. |
| **Elevated Risk** | Patients on the loaded board whose current risk band is *elevated*. |
| **Critical** | Patients on the loaded board whose current risk band is *critical*. |
| **Bed Occupancy** | Administrators only, occupancy as a percentage of the department bed baseline. |

The department scoping matters: a doctor in Cardiology sees Cardiology's admissions here, not the whole hospital. The one deliberate exception is the Sage brief directly below, which is always hospital-wide, the two scopes are labelled so they cannot be confused.

Because the Elevated Risk and Critical tiles are tallied over the rows currently loaded in the list, on very large boards (more than 100 patients) load the remaining pages with **Load more** if you want those two tiles to reflect every patient.

#### Sage: State of the Hospital

This collapsible card is the shift brief. Its header shows a live census, for example "42 admitted hospital-wide", and hovering that count explains the scope: "All admitted patients across the hospital, the Vigil tiles above count only your department."

The card contains:

- **A narrative paragraph.** A 3–5 sentence situational brief written by the local AI model, leading with who needs eyes first. It is regenerated in the background when ward events occur (a deterioration alert, a signed note, an incoming emergency) and on a slow periodic refresh, but never more often than every 90 seconds, an alert storm produces one new brief, not thirty. If the AI engine is unavailable, the card falls back to a plain deterministic line (for example "3 patients need attention first." or "All admitted patients are currently stable."), it degrades to facts, never to silence.
- **A watchlist.** Up to eight patients currently at *critical* or *elevated* risk, hospital-wide, worst first, each showing name, bed, and risk band. Click any watchlist row to jump straight into that patient's chart.
- **An active-alert badge.** When present, "N active" counts Sage insights of *critical* or *warning* severity that are currently open. This count is self-cleaning: an insight whose triggering condition has been clear for about ten minutes resolves itself automatically, and insights belonging to discharged patients are retired on the next sweep. So the badge reflects present ward truth, not an ever-growing backlog. Insights you dismiss or act on are removed immediately.
- **A provenance footer.** "Narrated locally · …" names the local model that wrote the paragraph; "Deterministic snapshot (local)" means you are looking at the AI-free fallback. Nothing on this card ever leaves the machine.

Click the card's header to collapse or expand it.

#### Sage: needs your attention

Below the brief, this feed lists proactive insight cards produced by Sage's continuous surveillance of every admitted patient. The rules are deterministic and grounded in real charted numbers, Sage cannot invent a patient or a value here. Current rules include:

- **Deterioration**, a patient is critical on their latest vitals, or their NEWS2 band has risen since a reading at least 15 minutes earlier. The band is the one on their Vigil board row, and the card names the parameters that scored. When a complete set of vitals puts them below that band, the card changes to "was critical" (or "was deteriorating") and clears itself 10 minutes later. An incomplete set does not count as recovery: the card stays and says what was not measured.
- **Trajectory**, SpO2 falling by 3+ points, or respiratory rate rising by 4+, across the last three readings.
- **Possible sepsis**, when the latest vitals meet SIRS: any two of fever (above 38.0 °C) or hypothermia (below 36.0 °C), heart rate above 90, and respiratory rate above 20. SIRS also counts the white cell count, which Orb does not record, so the card says it was not checked. It suggests a sepsis screen and cultures.
- **Antibiotic stewardship**, a broad-spectrum antibiotic running for more than 72 hours, prompting a de-escalation review.

Each card shows a severity dot (red critical, amber warning, blue info), a title naming the patient and location, a one-line detail with the actual numbers, and up to two actions:

- **⚡ Draft …**, where a suggested action exists (for example a starter antibiotic order), one click drafts it into the patient's case room in Relay. A confirmation toast appears: the draft is *not* an order until you open Relay and confirm it. Nothing is prescribed from this button alone.
- **Dismiss**, removes the card. Dismissals are recorded, and Sage learns from them: nudge patterns that clinicians consistently reject are quietened over time, but critical-severity insights are never suppressed by this feedback loop.

Duplicate suppression means the same insight will not be re-raised within 30 minutes, and only the first four cards show by default, use **Show N more** to expand.

#### Active Patients list

Every row is one patient: a coloured risk bar on the left edge, the patient's name, bed, diagnosis, and a risk percentage on the right. Patients whose band is *critical* carry a red **EMERGENCY** badge and always float to the top of the list regardless of sort order.

Two sort buttons sit in the section header:

- **Stability**, highest risk first (the default).
- **Recent**, most recently admitted first.

The board loads 100 patients per page; if your department has more, a **Load more (X of Y)** button appears. The list refreshes itself every 30 seconds while the tab is visible (refreshing pauses when the browser tab is in the background), and live vitals updates adjust each row's risk in near-real time.

Click any row to open the patient's detail view.

### 3.2 How Vigil scores risk: NEWS2

Vigil's risk engine is the UK Royal College of Physicians **National Early Warning Score 2 (NEWS2)**, a deterministic, auditable scoring table, not a black box. Each of seven parameters contributes 0–3 points from the most recent vitals:

| Parameter | Scores points when… |
|---|---|
| Respiratory rate | Below 12 or above 20 breaths/min (3 points at ≤8 or ≥25) |
| SpO2 | Below target (scale-dependent, see below) |
| Supplemental oxygen | +2 points whenever the patient is on oxygen |
| Systolic BP | ≤110 (up to 3 points at ≤90) or ≥220 |
| Heart rate | ≤50 or ≥91 (3 points at ≤40 or ≥131) |
| Temperature | ≤36.0 °C or ≥38.1 °C |
| Consciousness (ACVPU) | 3 points for anything other than Alert |

The aggregate maps to Vigil's three risk bands:

- **Critical**, NEWS2 of 7 or more.
- **Elevated**, NEWS2 of 5–6, **or** any single parameter scoring 3 (the NEWS2 single-parameter escalation rule: one severely deranged vital elevates the patient regardless of the total).
- **Stable**, everything else.

#### Scale 2 SpO2 for COPD

NEWS2 defines two oxygen-saturation scales, and Vigil implements both. **Scale 1** (the default) treats ≥96% as normal. **Scale 2** is for patients with COPD or chronic type-2 respiratory failure, whose target is 88–92%: saturations in that range score zero, and, critically, saturations *above* target score points **when the patient is on supplemental oxygen**, because over-oxygenation is dangerous in this group. On air, a high saturation scores nothing. Scale 2 is selected per patient as a flag on the patient record; when active, the score breakdown labels the parameter "SpO2 (Scale 2)" so it is always visible which scale produced the number.

#### The number you see, and the number that decides

Vigil displays two related figures and it is worth knowing which is which:

- On the **board rows**, the right-hand percentage is a normalized 0–100% index of the NEWS2 aggregate. It is a *ranking convenience*, it lets the board sort the sickest to the top, and it is **not** a probability that the patient will deteriorate.
- In the **patient detail view**, the risk badge and gauge show the **raw NEWS2 integer** (for example "NEWS2 7 · CRITICAL"). All escalation decisions are taken from this raw score and its clinical bands, never from the percentage.

#### The learned overlay (when installed)

Sites that have trained Vigil's optional neural-network models (one each for deterioration, sepsis, and mortality, predicting risk at 1, 3, 6, 12, and 24 hours ahead) get a learned overlay on top of NEWS2. The overlay is deliberately one-directional: it can **escalate** a patient's band when the model sees high near-term risk, including lifting a clinically-quiet, NEWS2-stable patient, but it can **never lower** the NEWS2 baseline. When an escalation happens, the risk drivers say so explicitly (for example "CfC: high near-term deterioration risk"). If no trained models are installed, Vigil runs on NEWS2 alone and behaves identically otherwise.

### 3.3 The patient detail view

Opening a patient replaces the board with a full chart. The top strip, the command bar, packs identity, risk, live vitals, and every action into one row.

- **Back arrow** (top left) returns to the board.
- **Identity**, name, MRN, age, and sex. When another clinician is viewing the same chart at the same moment, small presence chips appear here ("Priya Reddy also viewing") so two people never chart the same patient unknowingly.
- **Bed & risk badge**, bed number plus "NEWS2 n · LEVEL", coloured by band.
- **Diagnosis & comorbidities**, the primary diagnosis, with comorbidity tags shown *only* when genuinely recorded on the patient's problem list. Vigil never invents history: if nothing is recorded, nothing is shown.
- **Vital cells**, HR, BP, SPO2, TEMP, and RR, updating live. A cell highlights when its value is outside normal limits, and hovering any cell reveals a six-hour mini trend chart for that vital.

#### Action buttons

| Button | Opens |
|---|---|
| **Diag. Reports** | Imaging reports (X-ray, CT, MRI, ultrasound, ECG, echo) with the scan image, AI findings, and search by keyword or date (DDMMYYYY). |
| **Lab. Reports** | Laboratory reports, searchable the same way; results containing critical values carry a red **⚠️ CRITICAL** badge. |
| **Medications** | The medication list and eMAR (section 3.4). |
| **Notes** | The Clinical Notes History, every clinical document and Scribe note, searchable by word, date, or type. Click a note to read it in full. |
| **Story** | One scrollable, filterable clinical narrative merging admission, alerts, notes, imaging, medications, orders, administrations, and discharge onto a single timeline. |
| **HANDOFF** | The auto-drafted I-PASS shift handoff for this patient (opens in a new tab). |
| **DOWNTIME** | A read-only offline snapshot of the chart for downtime use. |
| **PRINT RECORD** | Prints the patient record. |
| **PRINT MEDS** | Prints the Medication Administration Record. |
| **DISCHARGE** | Starts the discharge workflow (section 3.6). |

#### Below the command bar

- **Care bundle timer.** When a Sepsis Six bundle is active for the patient, a live timer shows elapsed time since trigger, an overall compliance badge, and the six bundle elements with per-element **Mark done** actions.
- **Clinical Course.** Sage's deduced expected inpatient course: ordered care phases with the current phase highlighted, plus an expected length of stay, reasoned from the diagnosis, comorbidities, age, and sex. While it computes, the card reads "Sage is charting the expected course…".
- **Orb Risk Assessment.** The risk gauge (raw NEWS2 in the centre, band underneath), the top risk drivers in plain language, and a trend line, "↗ Improving", "→ Stable", or "↘ Deteriorating", computed by comparing the last six hours of vitals against the six hours before that. Expand **"Why NEWS2 n?, per-parameter contribution"** for a transparent table of exactly which vital contributed how many points, worst first, with a warning line when the single-parameter rule applied. The panel states its own limits: this is the deterministic RCP scoring table, a decision-support aid, *not* a clinically-validated deterioration probability.
- **MedGemma AI Assessment.** Directly under the risk panel, the local AI model streams a short clinical narrative, who the patient is, day of admission, current vitals with notable findings, risk context, trend, and active medications. Click the panel (or **Expand to Glass Window**) to read it full-screen. Concurrent opens of the same patient share one generation rather than each triggering the model. If the AI engine is not running, the panel simply reports that analysis is unavailable; nothing else on the chart depends on it.
- **Lab Trends.** A grid of per-analyte tiles (value, unit, reference range, trend arrow, status colour). Hovering a tile shows its trend chart with the reference range shaded; clicking opens a full-size graph modal (**Close Tracker** to dismiss). When real lab results are on file, trends are built from them. When *no* labs are on file, Vigil shows model-estimated values instead, and says so loudly: an amber **Estimated** badge appears in the panel header, and estimated trend lines render dashed. Estimated values are derived from the diagnosis, not measured; treat them as illustrative only and never as a substitute for ordering real labs.

### 3.4 Medications and administration

The **Medications** button opens the medication table: **Drug, Dose, Route, Frequency, Status** ("● Active", "◌ PRN", "○ D/C"), and **Administration**. The **+** icon in the header opens **Add Medication**.

#### Ordering a medication

1. Click **+** in the Medications modal.
2. Complete **Drug Name**, **Dose**, **Route** (Oral, IV, IV Infusion, IM, SC, Nebulizer), **Frequency**, and **Status** (Active, PRN, Discontinued). Drug name, dose, and frequency are required.
3. Click **Add Medication**.

Every order passes through the **drug-safety gate**, an allergy and interaction screen that runs *before* anything is written to the chart:

- **Severe alerts block the order.** A safety modal lists each alert with its severity. To proceed anyway you must type a clinical justification of at least 10 characters and click **Acknowledge & Order**. The override, who, what, and why, is written to the tamper-evident audit chain.
- **Sub-severe alerts do not block.** The order succeeds, and the same modal appears once, informationally, so the alerts still get eyes.
- **Unrecognised drugs**, ones Orb cannot map to one of its twelve known drug classes, are flagged as unscreened rather than silently passed as safe. The converse does not hold: a *recognised* drug with no curated interaction pair reports "No alerts" with no flag. Orb ships 55 curated pairs; a licensed source must be bulk-loaded before go-live.

#### Recording an administration (eMAR)

Each active medication has an **Administer** button in the Administration column. It opens the bedside administration modal, which enforces the five rights:

1. Verify the summary, patient, drug, dose, route, and time (now).
2. Type the drug name exactly as charted into the confirmation field. The **Given** button stays disabled until the typed name matches, a deliberate check of the physical medication in your hand against the chart row.
3. Click **Given** to chart the dose. A green "Given HH:MM" chip then appears beside the medication (times display in your local clock), with the recording clinician on hover.
4. If the dose was not given, choose **Not given** and select a structured reason: Refused, Held, clinical decision, NPO, Unavailable, or Other (with detail).

One important guard: medications that were AI-extracted or drafted by the agentic loop and **not yet verified by a pharmacist** appear with their Administer button disabled and the tooltip "Pharmacist verification required". An unreviewed AI suggestion can never be administered as if it were a confirmed order.

### 3.5 Admitting a patient

Click **Admit Patient** in the footer of the app shell. The modal ("Create clinical admission record across Orb modules") is organised into tabs, with a live summary panel on the right and completion dots on each tab. Required fields are starred.

1. **Identity**, Full Name*, Age*, Sex*, plus phone, address, and emergency contact.
2. **Admission**, Admission Source* (Emergency Room, Outpatient Clinic, Hospital Transfer, Direct Admission), Admission Type* (Inpatient, Observation, ICU, Emergency), Department*, Attending Doctor ID* (pre-filled with you), Ward*, Bed, and Referring Clinician. Department also pre-fills from your profile, this is what scopes the patient onto your board.
3. **Clinical**, Chief Complaint* (free text), Initial Diagnosis, and Triage Severity (1 Resuscitation through 5 Non Urgent).
4. **History**, Allergies, Comorbidities, and Current Medications as chip inputs (type and press Enter for each entry), plus free-text past medical, surgical, family, and social history. Allergy and comorbidity chips echo into the summary panel as red and amber flags.
5. **Vitals**, Heart Rate, SpO2, Systolic/Diastolic BP, Temperature, Respiratory Rate, Consciousness (ACVPU: Alert, New confusion, Responds to voice, Responds to pain, Unresponsive), and On supplemental O₂. As the tab itself notes, entering initial vitals immediately computes a NEWS2 risk score, so the patient lands on the board with a real risk band rather than a blank.
6. **Notes**, Code Status (Full Code, DNR, DNI, Comfort Care Only), Isolation Precautions (chips, e.g. Contact, Droplet), and Admitting Notes.
7. **Review**, a read-back of identity, admission context, and clinical overview. Click **Review Admission** at any point to jump here; then click **Confirm Admission**.

A single confirmed admission creates the patient record, the admission record, the clinical profile, an initial risk score, and a dedicated case room in Relay with an admission message, the patient is immediately live across every Orb module.

**The one-time credential screen.** After a successful admission you see "Patient admitted ✓" with the patient's **Login ID** and **One-time password** for the Bridge patient portal. Hand these to the patient now, use **Copy credentials** if helpful, because the password is stored only as a hash and **cannot be shown again**. Each patient receives a unique generated password; there is no shared default. Click **Done** to finish.

### 3.6 Discharging a patient

1. Open the patient's chart and click **DISCHARGE**.
2. The confirmation modal recaps the diagnosis, ward and bed, admission date, and current NEWS2 band, and explains what will happen: a comprehensive discharge summary with timeline is generated, the patient's status changes to discharged, and a QR code is created for records access.
3. Click **Confirm Discharge** (the button reads "Generating Summary…" while it works; **Cancel** or Escape aborts before this point).
4. On success, "✓ Patient discharged successfully", the modal shows the discharge summary preview, the hospital-stay timeline, and the **Discharge QR Code** ("Scan to access discharge summary").
5. Finish with **Print Summary Only**, **Print with Timeline**, or **Done**.

Two things about the summary are worth knowing. First, the narrative hospital course is AI-assisted, but the **medication reconciliation is deterministic**: it is computed directly from the real charted medication record, splitting medications into continued and stopped, carrying PRN ("as needed") medications onto the discharge list rather than letting them silently vanish, and explicitly flagging any AI-extracted medication a pharmacist has not verified so it is never carried onto a discharge script unreviewed. Second, if the AI engine is unavailable, discharge still completes with a clearly-labelled offline summary, discharge never blocks on the model.

Discharged patients leave the Active Patients board on its next refresh, their open Sage insights auto-resolve, and their chart remains reachable through the discharge summary and QR-code access path.

### Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| A patient you expect is not on the board | The board shows only your department's admissions. Check the patient's admitting department; on boards over 100 patients, click **Load more**. Discharged patients do not appear. |
| "Admitted · My Dept" disagrees with the hospital-wide count in the Sage brief | Working as designed, the tile is department-scoped, the brief is hospital-wide. Hover either number for its scope note. |
| Risk shows "No vitals recorded" | The patient has no vitals yet. Record a first set (or include vitals at admission) and NEWS2 computes immediately. |
| MedGemma AI Assessment says analysis is unavailable | The local AI engine (Ollama) is not running. Everything else on the chart, NEWS2, trends, labs, medications, is unaffected. Start the engine and reopen the chart. |
| The Sage brief shows no narrative, only a one-line status | The AI narrative layer is unavailable or still pending; you are seeing the deterministic fallback, which is still trustworthy ward truth. The footer confirms which engine produced what you see. |
| Lab Trends shows an amber "Estimated" badge and dashed lines | No real lab results are on file; values are model-estimated from the diagnosis. Order and file real labs and the panel switches to measured trends automatically. |
| The board or insight feed seems frozen | Background refresh pauses while the browser tab is not visible. Refocus the tab; polling resumes automatically. |
| **Administer** is greyed out with "Pharmacist verification required" | The medication was AI-extracted or agent-drafted and awaits pharmacist verification. It cannot be administered until verified. |
| A "Given" time looks wrong | Administration chips display in your local time zone; hover the chip to see who recorded the dose. |

### Safety & limitations

- **NEWS2 is an escalation aid, not a prediction.** The score is a deterministic lookup from the RCP table. The percentage on board rows is a normalized ranking index, it is not, and is never presented in the detail view as, a validated probability of deterioration. Clinical judgement always governs.
- **The learned overlay can only escalate.** Where neural-network risk models are installed, they may raise a patient's band but can never lower the NEWS2 baseline, the auditable rule-based floor always stands.
- **AI narratives are drafts, not findings.** The MedGemma assessment, the Sage ward narrative, and the discharge hospital course are generated locally from the chart and must be verified against source data before being relied on. The deterministic layers beneath them (snapshot, NEWS2, medication reconciliation) are the trusted signals.
- **Sage insight cards suggest; they never act.** The **⚡ Draft** action creates a draft in the patient's Relay case room that a clinician must explicitly confirm. The feedback loop that quietens frequently-dismissed nudges never suppresses critical-severity insights.
- **Nothing is invented.** Comorbidity tags appear only when genuinely recorded; estimated lab trends are visibly badged and dashed; unscreened drugs are flagged rather than passed as safe.
- **Overrides are accountable.** Ordering through a severe drug-safety alert requires a written clinical justification and is recorded on the tamper-evident audit chain.
- **Everything runs locally.** Risk scoring, AI narration, and the ward brief all execute on the hospital's own hardware; no patient data leaves the machine to produce anything described in this chapter.
