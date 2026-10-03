## 11. Surgical Suite, Checklists & Care Bundles

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

This chapter covers Orb's perioperative and protocol-safety tools: the **Surgical Suite** module, the operating-theatre schedule, emergency queue, and live surgical cases; the **WHO Surgical Safety Checklist** record kept for every case; the **Sepsis Six care bundle** timer that turns a deterioration alert into a measurable, time-boxed protocol; and the **I-PASS shift handoff**, auto-drafted from the patient's last twelve hours of data. The Surgical Suite has its own sidebar tab; the care-bundle timer and the handoff are reached from a patient's detail view on the Patients board.

### 11.1 Opening the Surgical Suite

The **Surgical Suite** tab appears in the left sidebar for doctors whose department is Surgery (it takes the place of the Appointments tab that other doctors see). Nurses, pharmacists, and administrators do not see the tab; nursing and anesthesia staff participate through the targeted alerts they receive in **Messages** (see 11.6). All surgical actions are permission-checked on the server: viewing requires surgery read access, changing a case requires surgery write access.

At the top of the panel are two view buttons: **Schedule**, the day-by-day operating-theatre schedule, opened by default, and **Active Cases**, every case not yet completed or cancelled, with team-assignment status and the start/complete controls. A red count badge on Active Cases shows how many exist.

### 11.2 The schedule: three tiers of drill-down

The schedule is organized as three progressively deeper views. You move down by clicking and back up with the back button in the top-left corner.

#### Tier 1: Week overview

The panel opens on a row of day cards covering the next fourteen days. Each card shows the day name and date (with a **Today** badge), a large procedure count ("3 Surgeries"), and a compact list of that day's cases: scheduled time, a colored risk dot (green stable, amber elevated, red critical, the patient's most recent risk score from the Patients board), the procedure, and the patient and operating room. A footer gives three statistics: **Total OR Time**, **Elevated Risk** (cases whose patients are at elevated or critical risk), and **Active Rooms**. If nothing is booked, the panel shows "No surgeries scheduled this week."

When emergency cases exist, a red **Emergency banner** appears above the day cards reading "*N* Emergency Case(s) Pending". Each emergency is a card marked **URGENT** with the procedure, diagnosis, patient and bed, and its stage, **Pending**, **Acknowledged**, or **Ready**. Clicking it jumps straight to Tier 3.

#### Tier 2: Day agenda

Clicking a day card expands it into a timeline of that day's procedures ("*N* scheduled procedures"). Each surgery card shows the time and estimated duration, the procedure and operating room, the surgical diagnosis, the patient (name, age, sex), the risk level, and a **success percentage** from the case's prep package (green at 95% and above, amber from 85%, red below). **Week Overview** returns to Tier 1.

#### Tier 3: Case detail

Clicking a surgery opens the full case record. The header holds the back button (labelled with the day you came from, or "Surgical Suite" if you arrived from the emergency banner) and the **Prep for Surgery** button (see 11.5).

Below it, a patient bar shows the patient's name, age, sex, MRN, and bed, followed by pills for the patient's **co-morbidities** and **drug allergies**, each allergy marked with a warning symbol, or a single **NKDA** pill (no known drug allergies) when none are recorded. Allergies are surfaced here deliberately: the operating team must see them before an emergency case. On the right, pills show the operating room and the scheduled time with duration.

The body is a two-column grid of cards:

| Card | Contents |
|---|---|
| **Clinical Handoff Context (Medicine)** | Shown only when the case arrived by escalation from a medical ward: the active medications, recent vitals, recent labs, and Scribe notes imported at the moment of transfer. |
| **AI Risk Assessment** (badged **MedGemma**) | A streamed, structured pre-operative risk assessment (see 11.3). Click the card to expand or collapse it. |
| **Possible Complications** | The procedure-specific complication list from the prep package. |
| **Surgical Team** | The proposed staff roster, each member's name and role. |
| **Surgical Checklist (N)** | The numbered, procedure-specific preparation and operative steps from the prep package, with a circular gauge showing the case's success probability. |
| **Instruments Required** | The instrument and equipment list for the case. |

This on-screen "Surgical Checklist" card is the case-specific *procedural* checklist drafted with the prep package, distinct from the WHO Surgical Safety Checklist record described in 11.7.

### 11.3 The AI risk assessment

When you open a case at Tier 3, Orb streams a pre-operative risk assessment from the local MedGemma model, word by word, into the AI Risk Assessment card. The model receives the patient's demographics, admission and surgical diagnoses, co-morbidities, current risk level and score, and estimated duration, and is asked for a fixed structure: ASA Physical Status Classification with justification, a surgical risk grade, key pre-operative concerns, anesthetic considerations, intra-operative risk factors, post-operative monitoring priorities for the first 24–48 hours, recommended pre-operative optimizations, and a short risk–benefit summary.

Badges on the card tell you where the content came from: **AI Generated** means the package was produced by the model; **Fallback** means it was not, and the card shows a standard template with the banner "AI-generated surgical package unavailable, showing standard fallback prep summary."

If the AI service is offline when the live assessment streams, the card shows a static assessment stamped "AI-assisted assessment (offline fallback), requires clinical verification," or the message "Risk assessment unavailable, AI service offline." For an emergency case whose package generation failed entirely, opening the case raises a "Feature under development" notice with an **Acknowledge** button, Orb will not present a half-built package as clinical content.

### 11.4 Emergency cases: intake and the prep package

Emergency cases do not come from the booking schedule; they are escalated out of a live clinical conversation. When a discussion in **Messages** turns to the need for emergency surgery, Sage's ambient layer drafts a surgical-prep action for the doctor to review, and a clinician's explicit confirmation of that action creates the case. Orb then:

1. Creates the case as **Pending**, assigned to the emergency operating room ("OR-E (Emergency)") at the current date and time.
2. Moves it to **context generating** while a background task assembles the prep package, risk assessment, possible complications, success probability, surgical checklist, instrument list, and staff roster. This step is time-bounded: the case always reaches **Ready** within 60 seconds, falling back to a clearly labelled standard prep summary if generation fails, so the queue never sits blocked behind the AI.
3. Pushes a live **EMERGENCY INTAKE** popup to the Surgical Suite, marked "Escalated from Relay", showing the patient, procedure, and indication, with two buttons: **Acknowledge** (records that the theatre team has seen the intake) and **Open Case** (acknowledges and jumps straight to Tier 3).

While a package is generating, the Tier 3 view shows "Generating risk assessment via MedGemma..." with a shimmer placeholder and refreshes every few seconds until the case is Ready, no reload needed.

### 11.5 The "Prep for Surgery" alert

From any case's Tier 3 view, the **Prep for Surgery** button broadcasts a structured preparation alert through Relay (the Messages module). While sending it shows a spinner; on success it changes to **Alert Sent** for a few seconds.

The alert goes to the **Surgery department channel** (targeted at the Surgery and Anesthesiology departments) and to the **patient's case room**, addressed to the case's assigned team members where a team exists, or to the staff named in the prep package otherwise. In Messages it renders as a card titled **PREP FOR SURGERY ALERT** carrying the patient's name and MRN, the procedure, the operating room and scheduled time, and the highlighted action line "Navigate to Surgical Suite to review Handoff Context."

Repeat presses are suppressed: if a prep alert for the same case was sent within the last four hours, Orb reports it as already active rather than flooding the channel with duplicates.

### 11.6 Team alerts, responses, and the case timeline

The **Active Cases** view is where a case moves through its lifecycle: prep → team assembly → start → complete. Each card shows a status ribbon in the corner (emergency stages in red, **● In Progress** in green), the patient header (name, age/sex, MRN), the procedure, the diagnosis and operating room, and, once a team has been alerted, a **Team Assignments** section listing each role and person with a colored status chip: **alerted** (amber), **confirmed** (green), or **declined** (red).

#### Alerting the team

For a case with no team yet, the red **Alert Team** button sends a targeted **Surgical Alert** to each proposed member. In Messages, the alert appears as a red-bordered card headed "⚠ Surgical Alert" with the text "SURGICAL ALERT: You have been assigned as *Role* for an emergency procedure" and the role. It is delivered live to the patient's case room and directly to each targeted person, not a department-wide broadcast. Sending the alerts also moves an emergency case to **Ready**.

#### Confirming or declining

Only the person the alert targets sees its response buttons: **✓ Confirm** and **✕ Decline**. Either choice posts a system message to the case room, "*Name* (*Role*) has confirmed/declined the surgical assignment", and updates the chip on the case card. If a member declines, Sage attempts to re-route the assignment: where an alternate for that role is available, a new targeted alert is posted ("SAGE SUGGESTION: Re-routing assignment to available *Role*.") and the replacement joins the team in the alerted state. When declines are present, a **↻ Refresh Status** button re-fetches the latest states.

#### Starting the case

The **Start Surgery** button only becomes active when every alerted member has responded and at least one has confirmed; until then it is greyed out and reads **Awaiting Confirmations**. This is enforced on the server, not just in the interface, an early attempt is refused with "Cannot start surgery until all assigned team members have confirmed." Starting moves the case to **In Progress**.

#### Completing the case

For an in-progress case, the green **Complete Surgery** button closes it. Completion marks the case **completed** (removing it from Active Cases and the emergency queue) and files a **post-operative note** into the patient's record as a Scribe note. On demonstration installations this is an illustrative worked example; on production installations Orb deliberately refuses to fabricate operative details and files a factual stub, procedure, diagnosis, surgeon, and the line "Operative note pending, to be authored by the operating surgeon." Completion is idempotent: pressing the button twice never writes a second note. The note appears afterwards in the patient's Notes on the Patients board.

### 11.7 The WHO Surgical Safety Checklist

Alongside the procedure-specific checklist in the prep package, Orb maintains a **WHO Surgical Safety Checklist** record for every case, following the 2009 WHO standard, the globally adopted three-phase verbal team check. The record is version-stamped, so a completed checklist shows which template it was filled against.

| Phase | When | Items |
|---|---|---|
| **Sign In** | Before induction of anaesthesia | Patient has confirmed identity, site, procedure, and consent · Surgical site marked / not applicable · Anaesthesia safety check completed · Pulse oximeter on patient and functioning · Known allergy reviewed · Difficult airway / aspiration risk assessed and equipment/assistance available · Risk of >500 ml blood loss (7 ml/kg in children), IV access/fluids planned |
| **Time Out** | Before skin incision | All team members have introduced themselves by name and role · Surgeon, anaesthetist and nurse confirm patient, site and procedure · Antibiotic prophylaxis given within the last 60 minutes / not indicated · Surgeon reviews critical/unexpected steps, duration, blood loss · Anaesthetist reviews patient-specific concerns · Nursing team confirms sterility, equipment issues · Essential imaging displayed / not applicable |
| **Sign Out** | Before patient leaves operating room | Nurse verbally confirms the name of the procedure recorded · Instrument, sponge and needle counts are correct · Specimen labelled (incl. patient name) / not applicable · Any equipment problems identified and addressed · Key concerns for recovery and management reviewed by whole team |

The electronic record enforces several rules worth knowing:

- **Attribution.** Every item, when checked, is stamped with who checked it and when. Unchecking an item removes that record.
- **Progress and completion.** Progress is tracked as checked-out-of-19, and the checklist is only complete when all nineteen items across the three phases are checked, there is no partial "complete."
- **Simultaneous checking is safe.** Two team members ticking different items at the same moment cannot overwrite each other's entries.
- **Nothing is destroyed.** Older checklist data from a previous format is preserved alongside the WHO record, never overwritten.

The electronic checklist documents the check; it does not replace it. The WHO checklist is a spoken, whole-team ritual, run it aloud at each pause point and record completion as you go.

### 11.8 Sepsis Six care bundles

A care bundle turns "we alerted you" into "the protocol was completed on time." Orb's built-in bundle is the **Sepsis Six**, the six actions of the 1-hour sepsis response, each with a 60-minute target from the moment the bundle starts:

| Element | Target |
|---|---|
| Give high-flow oxygen | within 60 minutes |
| Take blood cultures | within 60 minutes |
| Give IV antibiotics | within 60 minutes |
| Give IV fluids | within 60 minutes |
| Measure serum lactate | within 60 minutes |
| Monitor urine output | within 60 minutes |

#### Starting a bundle

Open a patient on the **Patients** board; the **Care Bundle** card sits directly beneath the patient's action bar. With no active bundle, it offers a single **Start Sepsis Six** button. Bundles start two ways:

- **Manually**, any clinician presses Start Sepsis Six. The bundle records who started it and why.
- **Automatically**, when a patient triggers a critical deterioration alert (a NEWS2 aggregate of 7 or more, the threshold for urgent clinical review), Orb opens a bundle on its own, recorded with the triggering score (for example "NEWS2 8 (>=7)"). A patient can only have one active Sepsis Six bundle, so an automatic trigger on top of a manual start joins the existing bundle rather than stacking a second one.

Every bundle start and element completion is written to the audit log.

#### Reading the timer card

Once active, the card is titled **Sepsis Six** and shows:

- A large elapsed clock ticking every second from the trigger time, captioned "elapsed · 60m target." The clock turns red once the hour is breached.
- A completion count ("3/6 done") and a compliance badge, "*N*% on track" in green while no element has missed its target, "*N*% at risk" in amber once one has.
- The **Trigger:** line recording why the bundle was opened.
- The six elements, each with its target ("within 60m"), a state chip, and a **Mark done** button. The four states are **Pending** (not yet done, still inside the hour), **Overdue** (not done and past the hour), **On time** (done inside the target), and **Late** (done after the target). Completed elements are struck through.
- A footer with the overall "*N*% complete" figure and a **Cancel bundle** link (which asks for confirmation).

Press **Mark done** as each action is performed, the timestamp is recorded at the moment you press it. When all six elements are marked, the bundle completes automatically.

Two percentages are deliberately kept separate: **completion** (how much is done, regardless of timing) and **compliance** (how much was done *within* the hour). A bundle finished late can reach 100% completion but will never report 100% compliance, the headline metric does not forgive a breached target. The card refreshes roughly every fifteen seconds, so a colleague's updates appear without reloading.

### 11.9 I-PASS shift handoff

Orb auto-drafts a structured shift handover in the **I-PASS** format from the last ~12 hours of a patient's record. From a patient's detail view on the Patients board, press the **HANDOFF** button in the action bar; the handoff opens in a new browser tab ("Drafting I-PASS handoff…" while it assembles).

A crucial design point: the draft is assembled **deterministically from the patient's actual database records, no AI model is involved**. It cannot fabricate a medication or a vital sign, and it works identically when the AI service is down.

The page is headed **I-PASS Shift Handoff**, with the patient, drafting window, and generation time, plus the **Save handoff** and **Print** buttons. The five sections are:

- **I, Illness Severity.** A colored severity badge, **Unstable** (red), **Watcher** (amber), or **Stable** (green), mapped directly from the patient's current NEWS2 band, with the raw score and the risk drivers behind it.
- **P, Patient Summary.** Demographics, ward and bed, primary diagnosis, active problems, and allergies (or NKDA).
- **A, Action List.** Active medications with dose, route, and frequency; any **open care bundles**, with compliance percentage and outstanding elements explicitly flagged ("OVERDUE (sepsis6): Give IV antibiotics"); and outstanding items, including a severity-driven monitoring action so a sick patient's list is never empty (for a critical patient: "Repeat a full set of observations now and keep continuous monitoring.").
- **S, Situation Awareness & Contingency.** The vitals trend over the shift (with heart-rate, blood-pressure, and oxygen-saturation deltas), the latest full set of observations, an "active deterioration" flag where applicable, and **contingency planning**, concrete if-then lines, one for each vital that scored on NEWS2 and for the side it is abnormal on, such as "If systolic BP is 90 mmHg or below (NEWS2 3 points), give a fluid bolus per protocol and escalate to the registrar." The number in each line is the RCP NEWS2 chart's own 3-point threshold, so the sheet and the Vigil command bar call the same reading red. Where Orb has no vital-specific action (a systolic BP of 220 or above, a heart rate of 40 or below, a respiratory rate of 8 or below, a temperature of 35.0°C or below) the line gives the RCP red-score response: inform the covering doctor for urgent review.
- **S, Synthesis by Receiver.** A read-back block with blanks for outstanding questions, the confirmer's name, and the time, the receiver's verbal read-back is a required part of I-PASS, and Orb leaves it to a human.

A **Plain-text handoff** expander at the bottom shows the complete rendered handoff as text. **Save handoff** files the draft into the patient's record as a handoff-format note in draft status (confirmed on screen as "Handoff saved as draft note #N."); the save is audit-logged. **Print** uses the browser's print dialog, so the handoff can go to paper for the huddle.

Because the Action List folds in open care bundles, the tools in this chapter reinforce each other: a Sepsis Six bundle left incomplete at shift change arrives in the incoming clinician's handoff, flagged overdue, with its contingency lines attached.

### 11.10 Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| No Surgical Suite tab in the sidebar | The tab is shown to doctors in the Surgery department. Other roles work surgical cases through Messages alerts; ask an administrator if your department assignment is wrong. |
| "No surgeries scheduled this week" | Nothing is booked in the 14-day window. Emergency cases appear in the red banner, not on day cards. |
| Emergency case stuck at "Generating risk assessment via MedGemma..." | Generation is bounded at 60 seconds; the case reaches Ready with a labelled fallback package if the AI cannot respond. The view polls automatically. |
| Opening a case shows "Feature under development" | The AI package for that emergency case failed or timed out. Press Acknowledge; the case remains workable from Active Cases. |
| **Start Surgery** greyed out ("Awaiting Confirmations") | Every alerted member must respond and at least one must confirm. Chase the amber chips; use ↻ Refresh Status after declines. |
| No Confirm/Decline buttons on a Surgical Alert | The buttons appear only for the person the alert targets; others see it read-only. |
| **Prep for Surgery** does nothing on a second press | Duplicate prep alerts are suppressed for four hours; the original is still active in the Surgery channel. |
| Risk assessment reads "offline fallback, requires clinical verification" | The local AI service is not responding. The fallback is a template, not a patient-specific assessment; retry once the AI recovers. |
| Care Bundle card shows "No active care bundle" for a septic patient | Auto-start only fires on a critical (NEWS2 ≥ 7) alert. Start the bundle manually with Start Sepsis Six. |
| Care bundle action fails or the card shows an error | Press Retry; the card also refreshes itself every ~15 seconds. |
| Handoff tab shows "Could not draft handoff" | Use Retry; if the patient is reported as not found, reopen them from the Patients board and press HANDOFF again. |

### 11.11 Safety & limitations

- **The AI risk assessment is decision support, not a decision.** It is generated by the local MedGemma model and streams with an explicit model badge. Verify every clinically material claim, ASA class, risk grade, anesthetic concerns, against your own assessment. Fallback content is always labelled.
- **Success probability is an estimate from the prep package**, shown to aid prioritization and consent discussions, not a validated prognostic instrument, and never a guarantee to quote to patients.
- **Emergency prep packages are drafts.** The checklist, instrument list, and staff roster generated at intake are starting points for the theatre coordinator, not orders.
- **Post-operative documentation is the surgeon's.** On production systems Orb files only a factual stub on completion; the operating surgeon must author the operative note.
- **The WHO checklist must be performed aloud.** Orb records completion with per-item attribution, but the safety benefit comes from the spoken team check. Do not tick items that were not verbally confirmed.
- **Sepsis Six targets are a configuration, not a law of nature.** The shipped 60-minute targets reflect standard teaching; confirm them against your hospital's sepsis policy. Compliance figures feed quality reporting, so mark elements done when the action actually happened.
- **Sage's team re-routing is a suggestion.** A declined assignment alerts a replacement automatically, but the surgeon or coordinator remains responsible for confirming the assembled team before starting.
- **The I-PASS draft is only as complete as the chart.** Anything not documented, a verbal order, an unrecorded observation, will be missing. Review every section before handing over, and complete the receiver's read-back in person; the Synthesis section is deliberately left blank for that reason.
