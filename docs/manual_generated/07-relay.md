## 7. Relay: Clinical Messaging

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![Relay case rooms, one per patient, sorted by recency or urgency with unread counts and criticality badges.](assets/manual/relay-case-rooms.png)

*Relay case rooms, one per patient, sorted by recency or urgency with unread counts and criticality badges.*

Relay is Orb's clinical messaging hub. Instead of pagers, phone tag, or personal chat apps, every conversation about a patient happens in that patient's own **case room**, a message channel automatically tied to their record, while broader coordination happens in shared **department channels**. Everything posted in Relay is stored alongside the clinical record, delivered live to everyone in the room, and visible to Orb's AI layer, which quietly reads along and surfaces risks and suggested actions in the **Sage Copilot** sidebar.

Relay is also where Orb's automated systems talk to you. The Vigil monitoring engine posts deterioration alerts directly into a patient's case room, surgery preparation produces actionable alert cards with **Confirm** and **Escalate** buttons, and emergency surgical staffing sends targeted assignment alerts that individual team members can **Confirm** or **Decline**, all in the same stream as human conversation, so the context never gets separated from the decision.

### 7.1 Opening Relay

1. Sign in with your employee ID and password.
2. In the left sidebar, select **Messages** (the Relay pillar).

Relay is available to doctors, nurses, and administrators. Doctors and nurses can read and send messages; administrators can read and send in every channel and see every targeted alert. Acting on Sage's suggested action cards (Acknowledge / Dismiss / Confirm) is reserved for doctors. Patients never see Relay, the patient portal (Bridge) is a separate, read-only experience.

### 7.2 The channel list

When you open Relay you land on the channel list. From top to bottom it contains:

| Section | What it shows |
|---|---|
| **Case Rooms** header with sort buttons | The heading plus two sort toggles: **Recency** and **Urgency**. |
| **⚠️ Action Required** | Unresolved alert cards targeted at you (see 7.6). Hidden when there is nothing to act on. |
| Search bar | A search field with the placeholder *"Search patient, ID, or case label..."*. |
| Case room list | One entry per admitted patient you can see. |
| **Department** | Shared channels for your department. |

#### Case room entries

Each case room row is a card showing, at a glance:

- **Patient / room name**, usually the patient's name.
- **Criticality badge**, a colored pill reading **CRITICAL** (red), **ELEVATED** (amber), or **STABLE** (green), driven by the patient's most recent Vigil risk score. Rooms for patients without a risk score show no badge.
- **Alert dot**, a small dot on the left edge when the room received at least one automated alert in the last 24 hours.
- **Ward / bed**, e.g. "ICU / 4", when the patient has an assigned bed.
- **Diagnosis**, the working diagnosis, truncated if long.
- **Unread count**, a numeric badge showing how many messages in the room you have not yet read. Opening the room clears it.

The list shows the ten most relevant rooms by default. If there are more, a **Show N More ↓** button expands the full list, and **Show Less ↑** collapses it again.

Case rooms follow the patient lifecycle: a room is created automatically when a patient is admitted, and it disappears from Relay when the patient is discharged. If a room you were using vanishes, check whether the patient has been discharged, that is expected behavior, not data loss.

#### Sorting: Recency vs. Urgency

Two buttons in the header switch the ordering of the case room list:

| Sort | Ordering |
|---|---|
| **Recency** | Rooms with the most recent message activity first. |
| **Urgency** | Rooms with the most alerts and unread messages first; ties broken by recent activity. |

Use **Urgency** at the start of a shift to surface the rooms demanding attention; use **Recency** to follow live conversations.

#### Searching

Type into the search bar to filter case rooms as you type (the filter applies about 150 ms after you stop typing, to keep the list steady). The search matches against:

- the room / patient name,
- the patient ID,
- the MRN (medical record number),
- the diagnosis.

A small **×** button clears the query. The search filters case rooms only; department channels are unaffected.

#### Department channels

Below the case rooms, the **Department** section lists shared channels. You only see channels matching your department, for example, staff in Surgery and Anesthesiology both see the Surgery channel. Administrators see every department channel. If no channel matches, the section reads *"No department channels found."*

### 7.3 Inside a room

Selecting any channel opens the chat view. A back arrow at the top left returns you to the channel list; the channel name is shown beside it.

#### Reading messages

Messages appear in chronological order. Each message header shows the **sender's name**, their **role**, and the **time**. Automated messages additionally carry a small **AUTO** badge, hovering over it explains: *"Automated / AI-generated message, not a clinician."* This provenance badge is how you distinguish, at a glance, a colleague's note from a bot posting.

The main kinds of message you will see:

| Message | Sender | Appearance |
|---|---|---|
| Clinician message | A doctor or nurse | Plain chat bubble with name, role, time. Supports **bold** and *italic* text and bullet lists. |
| Bot / AI message | "Orb Vigil", lab bots, "Sage Ambient", "Orb System" | Same layout plus the **AUTO** badge. |
| System notice | "Orb System" | Muted styling; records events such as a team member confirming a surgical assignment. |
| Vigil alert | "Orb Vigil" | Highlighted alert styling; see 7.5. |
| Actionable alert card | Surgery prep and similar workflows | Structured card with **Confirm** / **Escalate** buttons; see 7.6. |
| Surgical assignment alert | "Orb System" or "Sage Ambient" | Red-bordered card with **✓ Confirm** / **✕ Decline** for the targeted user; see 7.7. |

#### Typing indicators

While another member of the room is typing, a small line appears above the composer, *"Priya Reddy is typing…"*, and disappears a few seconds after they stop. When several people type at once, up to two names are listed, followed by "and others". Your own typing is never shown to you.

#### Sending a message

1. Click the input at the bottom of the room (*"Type a message…"*).
2. Type your message. You can use `**bold**`, `*italic*`, and `-` bullet points; they render as formatted text for everyone.
3. Press **Enter** or click the paper-plane send button.

Your message appears instantly (optimistically) and is delivered live to every clinician who has the room open, no refresh needed. If the send fails (for example, a network drop), the message is removed from the stream and your text is restored to the input box so you can retry. Messages composed while offline are queued and replayed safely: the same message is never posted twice.

Every message you send in a patient-linked case room is also passed, in the background, never delaying your message, to Sage's ambient observation pipeline (see 7.8).

### 7.4 Automated Vigil alerts

Vigil, Orb's continuous monitoring engine, bridges directly into Relay. Whenever a patient's NEWS2-based risk level is **elevated** or **critical**, Vigil posts an alert into that patient's case room as the bot user **Orb Vigil** (with the AUTO badge). A typical alert reads:

> ⚠️ VIGIL ALERT: Moderate hypoxia (SpO2 88%); Tachypnea (RR 24); Hypotension (SBP 92) (+2 more)
> Current vitals: HR 118 · BP 92/60 · SpO2 88% · Temp 38.4°C · RR 24
> NEWS2 10 (critical). Immediate review recommended.

Each alert contains:

- **The reason(s)**, the NEWS2 parameters that scored points, in the same words as the NEWS2 drivers, worst first. The first three are named and any others are counted, for example "(+2 more)". A reading that scored nothing, such as a blood pressure of 170, is never listed as a reason; it is still in the vitals snapshot.
- **A vitals snapshot**, heart rate, blood pressure, SpO2, temperature, respiratory rate at the moment of the alert.
- **The NEWS2 aggregate and band**, reported as the raw score plus its clinical band (e.g. "NEWS2 9 (critical)"). Orb deliberately reports the ordinal score, not a percentage: NEWS2 is an escalation trigger, not a calibrated probability.

#### MedGemma AI recommendation

For **critical** alerts only, Vigil appends a single-sentence recommendation generated by the local MedGemma model, labeled:

> **✦ MedGemma AI**: Consider immediate supplemental oxygen and urgent senior review for suspected respiratory deterioration.

This is decision support, not an order. It is one concise sentence based on the vital-sign deviations, produced entirely on-device. If the AI model is temporarily unreachable, Orb pauses recommendation calls rather than delaying alerts, the alert is still delivered, just without the MedGemma line.

#### Alert fatigue protection

To avoid flooding a room, Vigil enforces a two-minute cooldown per patient between alerts, with two deliberate exceptions that always break through:

- **Escalation always re-alerts.** A patient moving from elevated to critical is never muted by the cooldown.
- **Critical patients are exempt entirely.** A further deterioration in an already-critical patient (e.g. SpO2 88 → 80) pages the team immediately.

### 7.5 The Action Required section

At the top of the channel list, the **⚠️ Action Required** section gathers every unresolved actionable alert that targets *you*, either because you were explicitly assigned, or because your role and department match the alert's audience (administrators see all of them). Alerts stay listed for up to 48 hours or until someone resolves them, and duplicates of the same surgical case are collapsed into one entry.

Each entry shows the alert title and required action, with an arrow on the right. Clicking it jumps you straight into the relevant patient's case room, where the full alert card awaits.

### 7.6 Actionable alert cards: Confirm and Escalate

Certain workflows, most prominently surgery preparation, post **structured alert cards** into both the department channel and the patient's case room. A surgery-prep card is titled **PREP FOR SURGERY ALERT** and displays:

- **Patient**, name and MRN,
- **Procedure**, the planned procedure name,
- **OR**, the operating room and scheduled time,
- an amber highlighted strip with the required action, e.g. *"Navigate to Surgical Suite to review Handoff Context."*

Below the details are two buttons:

| Button | Meaning | Result |
|---|---|---|
| **Confirm** | You acknowledge the alert and are acting on it. | The card is marked resolved; everyone in the room sees *"✓ Confirmed by \<your name\>"*. |
| **Escalate** | The alert needs attention beyond you. | The card is marked escalated; everyone sees *"✓ Escalated by \<your name\>"*. |

Each alert can be resolved exactly once, hospital-wide. If a colleague beats you to it, your click returns the inline error *"Alert already processed"* and the card refreshes to show who resolved it. Buttons are disabled while a resolution is in flight, and any failure (permission, network) is shown inline next to the card so nothing fails silently. Once resolved, the alert also disappears from everyone's Action Required list.

### 7.7 Surgical team alerts: Confirm and Decline

When an emergency procedure is staffed, whether triggered from the Surgical Suite or by a confirmed Sage transfer-to-surgery action, each assigned team member receives a personal **Surgical Alert** in the patient's case room, delivered live to them even if they don't have the room open. The card has a red border and reads, for example:

> ⚠ Surgical Alert
> SURGICAL ALERT: You have been assigned as Anesthetist for an emergency procedure.
> Role: **Anesthetist**

Everyone in the room can *see* the card, but only the person it targets sees the response buttons:

- **✓ Confirm**, accepts the assignment. A system message is posted for the whole team: *"SYSTEM: \<name\> (\<role\>) has confirmed the surgical assignment."*
- **✕ Decline**, declines it. The decline is posted the same way, and Sage immediately suggests a re-route: a new targeted alert is issued to an available colleague in the same role (*"SAGE SUGGESTION: Re-routing assignment to available Anesthetist."*).

The surgery cannot be started in the Surgical Suite until every alerted member has responded and at least one has confirmed, the alerts in Relay are the gate.

### 7.8 The Sage Copilot sidebar

Open any case room linked to a patient and, once Sage has something worth showing, a sidebar appears to the right of the conversation headed **Sage Copilot**. This is Orb's *ambient intelligence*: Sage passively reads the room's clinical conversation and maintains a live picture of the case, without ever posting into the chat or taking any action on its own.

While the sidebar first loads you may briefly see *"Connecting to Sage..."*. If Sage has observed nothing noteworthy yet, the sidebar stays hidden entirely; it is normal for quiet rooms to have no sidebar.

#### How ambient observation works

Every human message in a patient-linked room passes through a series of cheap filters before any AI runs:

1. Bot and system messages are ignored, Sage never analyzes its own output.
2. Only patient-linked case rooms are observed; department channels are not.
3. Short acknowledgments ("ok", "thanks", "noted", a thumbs-up) are skipped.
4. Repeated or duplicated messages are skipped.
5. Only messages containing clinical signal, deterioration language, medication verbs, imaging, escalation or transfer talk, doses, are passed to the AI at all.

Messages that pass the gates are classified a few seconds later by the smaller, always-warm ambient model (MedGemma 4B in the standard setup), which updates the room's structured memory: a running discussion summary, the current concern, risk flags, and possible action intents. Unambiguous clinical directives, "start IV ceftriaxone 1 g", "she needs an emergency laparotomy", are additionally caught by a deterministic rule, so a clear order surfaces an action card even if the small model misses it. All of this runs in the background on your local machine; the chat itself is never delayed.

#### What the sidebar shows

From top to bottom:

**Current concern and summary.** A short statement of the room's active clinical concern, followed by Sage's running summary of the discussion.

**Risk Flags.** Cards titled **Risk Detected**, each with:

- a badge reading **Active**, or **Stale** if the flag is more than 30 minutes old (stale cards are visually dimmed, treat them as historical context, not a live concern);
- the risk description;
- a **Signal:** line quoting the evidence in the conversation that triggered the flag.

Sage applies a confidence filter here: risk flags below 80% model confidence are never shown. You see fewer flags, but the ones you see are meant to be worth your time.

**Suggested Actions.** When the conversation implies a concrete next step, Sage drafts an action card, for example **START MEDICATION** or **TRANSFER TO SURGERY**, showing the **Target** (the drug, procedure, or destination), the **Signal** evidence, a state badge, and three buttons:

| Button | What it does |
|---|---|
| **Acknowledge** | Marks the suggestion as seen and under consideration. The card stays active with an "acknowledged" badge. Available only while the card is newly suggested. |
| **Dismiss** | Rejects the suggestion. The card moves to Handled Actions and can never be re-actioned. |
| **Confirm** | Accepts the suggestion and hands it off to the right module (see below). This is the only path by which a Sage suggestion ever touches the chart. |

Cards older than 30 minutes are badged **Stale**, and only doctors can press these buttons. Each card carries a version number behind the scenes: if two clinicians act on the same card simultaneously, the second receives *"Action was modified concurrently. Please refresh."* and the list reloads with the true state. Once a card is confirmed or dismissed it is final, resolved cards cannot be transitioned again.

**Handled Actions.** Dismissed and confirmed cards collect here, each showing its final state plus, for confirmed cards, a handoff badge reporting where the work went and whether it landed:

| Badge | Meaning |
|---|---|
| **Sent to Helix** | A medication action was handed to the pharmacy system. Any drug-safety alerts that did not stop it are listed under the card. |
| **Charted only** | The medication is on the chart for review, but no pharmacy order was created (usually because the drug as written is not in the Helix catalog). The reason is shown under the card; place the order in Helix. |
| **Sent to Scribe** | A documentation action created a draft clinical note. |
| **Sent to Transfer** | A surgical/transfer action staged the transfer and surgery. |
| **… handoff pending** | Dispatch is still in progress. |
| **… handoff failed** | The downstream module rejected the handoff; the action did not complete. |
| **Blocked** | The drug-safety screen stopped the medication order: a severe allergy or interaction match, or the screen could not run. Nothing was charted, and the reason is shown under the card. Order it from the chart if it is still wanted. |
| **Unsupported route** | Sage could not map the action to a module; nothing was executed. |

#### What Confirm actually does: and the guardrails around it

Confirming a card routes it by intent:

- **Medication actions** go to Helix (pharmacy). Crucially, the drug is charted as an *unverified*, AI-extracted entry with the dose explicitly marked **REVIEW REQUIRED**, Sage never invents a dose. Before anything is written, the drug is screened against the patient's allergies and active medications; a severe allergy match blocks the entry outright. The unverified entry then appears in the Pharmacy Verify queue, where a pharmacist must review it before it becomes an active order.
- **Documentation actions** create an AI-generated draft note in Scribe, attributed to Sage's observation, for a clinician to review and sign.
- **Transfer / surgery actions** stage the surgery, move the patient's admission to the surgical service, and fire the targeted surgical alerts of section 7.7 to the lead surgeon, a scrub nurse, and an anesthetist, live, into this same case room.

For medication dispatch, Orb additionally provides a hardened **agentic confirmation** pathway with pre-flight checks. Before an order is committed, the platform:

1. **Reconciles** the free-text drug name against the Helix master catalog (phonetic matching with a confidence score), refusing to proceed if no match is found.
2. **Checks stock**, and if the drug is out of stock, either substitutes a pre-approved therapeutic alternative or requires a documented override reason.
3. **Detects high-alert medications** (epinephrine, heparin, insulin, potassium, tPA, and opioids such as morphine, fentanyl, oxycodone, hydromorphone).
4. **Requires the confirming clinician's signing PIN**, provisioned per user by an administrator, rate-limited, and locked for 15 minutes after repeated failures.
5. **Requires an explicit dose and frequency** from the clinician. An order with an unresolved ("TBD") dose is never dispatched, under any circumstance.
6. **Runs the drug-safety gate**, allergy and interaction screening, where severe findings, and *all* high-alert medications, demand an explicit acknowledgment with a written reason before the order proceeds.
7. **Writes a cryptographic audit record**: a delegation hash chaining the source conversation, the AI suggestion, and the signing clinician onto Orb's tamper-evident audit log, alongside the safety-screen results.

### 7.9 Common workflows

#### Start-of-shift triage

1. Open **Messages** and set the sort to **Urgency**.
2. Work through the **⚠️ Action Required** list first, each click lands you in the relevant case room.
3. Then review rooms with **CRITICAL** badges and unread counts, oldest alerts first.

#### Responding to a Vigil deterioration alert

1. Open the case room (from Action Required, an unread badge, or a notification).
2. Read the Vigil alert: reasons, vitals snapshot, NEWS2 band, and, for critical alerts, the MedGemma suggestion.
3. Discuss in the room; the whole team sees the conversation in context.
4. Check the **Sage Copilot** sidebar for risk flags and drafted actions arising from your discussion, and Acknowledge, Dismiss, or Confirm as clinically appropriate.

#### Finding a patient fast

1. From the channel list, type any fragment of the name, patient ID, MRN, or diagnosis into the search bar.
2. Select the room; the unread badge clears as it opens.

### 7.10 Troubleshooting

| Symptom | Explanation / fix |
|---|---|
| A case room disappeared | The patient was discharged; rooms close automatically at discharge. |
| A department channel is missing | You only see channels matching your department. Ask an administrator to check your department assignment. |
| *"Alerts could not be loaded."* above the channel list | A transient fetch failure; click **Retry**. |
| *"(alert could not be fully displayed)"* under an alert | The alert payload was malformed; the raw content is shown as text. Report persistent cases to your administrator. |
| *"Alert already processed"* when clicking Confirm/Escalate | A colleague resolved the alert first. The card refreshes to show who. |
| *"Action was modified concurrently. Please refresh."* | Another clinician actioned the same Sage card at the same moment; the list reloads with the authoritative state. |
| A sent message vanished and returned to the input box | The send failed (usually network); your text is preserved, press Enter to retry. |
| Live messages stopped after a network blip | Relay rejoins the room automatically on reconnection; if messages still seem stale, go back and reopen the room. |
| Unread badge won't clear | Badges clear when the room is opened; reopen the room. |
| No Sage Copilot sidebar | The channel isn't patient-linked (department channels have no sidebar), or Sage simply has nothing to show yet. |
| Acknowledge / Confirm / Dismiss rejected | Only doctors can action Sage suggestion cards; nurses and administrators see them read-only. |
| Agentic confirmation says no signing PIN is provisioned | Ask an administrator to provision a PIN for your account; without one, PIN-signed dispatch is unavailable by design. |

### 7.11 Safety & limitations

- **Provenance is always visible.** Every automated post carries the **AUTO** badge. If a message has no AUTO badge, a human wrote it; if it does, treat it as machine output requiring clinical judgment.
- **AI content is advisory.** Vigil alerts, MedGemma recommendations, Sage summaries, risk flags, and drafted actions are decision support, not diagnoses or orders. Nothing Sage observes ever changes the chart without an explicit clinician **Confirm**.
- **Confidence and staleness filters are deliberate.** Risk flags under 80% confidence are hidden, and anything older than 30 minutes is badged **Stale**, Sage prefers silence to noise, which also means it will not surface every conceivable concern.
- **Doses are never invented.** AI-drafted medication actions are charted dose-less ("REVIEW REQUIRED") and held for pharmacist verification; PIN-signed dispatch demands clinician-entered dose and frequency. Severe allergy conflicts block the order outright.
- **Alert cooldowns trade noise for vigilance.** Identical elevated-level alerts are suppressed for two minutes per patient; escalations and critical patients always alert immediately. Do not rely on Relay alone for continuous observation of an unstable patient.
- **Everything is local and audited.** All AI runs on the machine in front of you, no patient data leaves the building, and alert resolutions, action transitions, and agentic dispatches are written to Orb's tamper-evident audit chain with the acting clinician's identity.
