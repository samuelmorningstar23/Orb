## 8. Orders (CPOE), eMAR & Drug Safety

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![Provider order entry. Codes are validated against LOINC, SNOMED, or ICD-10 before the order can be created.](assets/manual/orders-cpoe.png)

*Provider order entry. Codes are validated against LOINC, SNOMED, or ICD-10 before the order can be created.*

This chapter covers the ordering half of Orb's medication and diagnostics loop: the **Orders** panel (computerized provider order entry with terminology validation), the order-time **drug safety screen** that checks every medication order against the patient's allergies and current medications, and the **eMAR** (electronic Medication Administration Record) that nurses use at the bedside to record that a dose was actually given, or deliberately withheld.

Together, these three pieces enforce one principle: a medication cannot reach a patient's chart without a safety screen, and a dose cannot be charted as given without a bedside confirmation. Every screen, override, and administration is written to Orb's tamper-evident audit chain.

### 8.1 The Orders panel

#### What it is for

The Orders panel is Orb's provider order entry surface. Doctors use it to place lab, imaging, medication, procedure, and nursing orders for a patient; the panel then tracks each order through its lifecycle from draft to completion, and (for labs and imaging) captures the result. The panel's subtitle describes it precisely: *"Provider order entry with terminology validation"*, coded orders are checked against real LOINC, SNOMED, and ICD-10 terminology before they are accepted.

#### Opening the panel

1. Sign in with a clinician account (for example the demo doctor **DOC001**).
2. Click **Orders** in the left sidebar of the clinician shell.
3. In the **Patient ID** field at the top of the panel, type the patient's identifier (for example `P001`). The panel loads that patient's orders immediately.

If no patient ID has been entered, the panel shows *"Enter a patient ID to view orders."* Once a patient is selected, an empty list reads *"No orders for this patient yet."*

#### Reading the order list

Each order appears as a card showing:

| Element | Meaning |
|---|---|
| **Description** | The order text (e.g. "Lactate [Moles/volume] in Serum or Plasma"). |
| **Order type** | One of `lab`, `imaging`, `medication`, `procedure`, `nursing`, `other`. |
| **Code badge** | The terminology system and code (e.g. "LOINC 2524-7"), shown only for coded orders. |
| **Priority** | `routine`, `urgent`, or `stat`. Urgent and stat priorities are visually highlighted so they stand out in the list. |
| **Source** | Shown only when the order did not come from a clinician directly (for example, orders raised by other Orb modules). |
| **Status chip** | The current lifecycle state: draft, active, in progress, completed, resulted, or cancelled, each with its own color. |
| **Result line** | For resulted labs/imaging, the recorded result text appears beneath the description. |

To the right of each card are the action buttons available for that order's current state (see 8.3).

### 8.2 Creating an order

Click **+ New Order** in the panel header. The button label changes to **Close** while the form is open, so you can collapse it again without creating anything.

#### The order form, field by field

1. **Order Type**, choose `lab`, `imaging`, `medication`, `procedure`, `nursing`, or `other`. This choice determines whether a terminology code is required: `nursing` and `other` orders may be free-text; all other types **must** carry a valid code.
2. **Priority**, `routine` (default), `urgent`, or `stat`.
3. **Code System**, shown for coded order types. Choose **LOINC** (laboratory tests), **SNOMED** (procedures and clinical findings), or **ICD10** (diagnoses).
4. **Code**, type the code itself (the field hints with an example, `e.g. 2524-7`).
5. **Description**, the human-readable order text. If you validate a code first, this fills itself in automatically (see below).

#### The Validate button

The **Validate** button next to the Code field checks your code against Orb's local terminology tables. Validation also runs automatically when you click out of the Code field.

- While checking, an ellipsis (…) appears.
- A **✓** with the code's official display name means the code is real and active, for example, validating LOINC `2524-7` shows *"✓ Lactate [Moles/volume] in Serum or Plasma"*. If the Description field is still empty, the display name is copied into it automatically, which is the fastest way to write a correctly-worded order.
- A **✗ unknown code** means the code is not in the terminology tables. You cannot submit a coded order with an unvalidated or invalid code, the **Create Order** button stays disabled until the code shows ✓ (or, for nursing/other orders, until a description is entered).

Changing the code, or switching the code system or order type, resets the validation state, so a stale ✓ can never carry over to a different code.

Validation is not only a front-end nicety: the server independently re-checks the code when the order is submitted and rejects unknown codes outright, so an invalid code can never be saved even by an integration that bypasses the form.

> **About the terminology subset.** Orb ships with a curated starter set of real codes: common LOINC laboratory panels and analytes (CBC, BMP/CMP, troponins, coagulation studies, HbA1c and more), SNOMED CT findings and procedures (chest pain, pneumonia, transfusion of blood product, CABG…), and frequent ICD-10 diagnoses (E11.9, I10, J18.9, A41.9…). It is deliberately small; a site administrator can bulk-load the full LOINC, ICD-10-CM, and licensed SNOMED CT releases into the same tables, after which validation transparently covers the complete terminologies. If a legitimate code shows "unknown", it simply has not been loaded yet, see Troubleshooting.

#### Submitting

Click **Create Order**. The button shows *"Creating…"* while the order is submitted, then the form closes and the new order appears in the list with a **draft** status chip. If the order is a **medication**, the drug-safety screen runs before anything is saved, see 8.5.

### 8.3 Order lifecycle: draft → active → completed

Orders move through a guarded state machine. Illegal jumps (for example draft straight to completed) are rejected by the server; every legal transition writes an order-event record, so the full history of who moved an order and when is preserved.

| From | Allowed next states |
|---|---|
| draft | active, cancelled |
| active | in progress, resulted*, cancelled |
| in progress | completed, resulted*, cancelled |
| completed / resulted / cancelled |, (terminal) |

\* *resulted* applies only to lab and imaging orders, the two types that produce a result.

On each order card, the panel offers exactly the buttons that make sense for its state:

- **→ active**, **→ in progress**, **→ completed**, advances the order one step. The arrow button always shows the next legal forward state.
- **+ Result**, shown for lab and imaging orders that are active or in progress. Clicking it opens a prompt (*"Result for ‹order description›:"*); entering text attaches the result and moves the order directly to **resulted**, its terminal state. The result text then appears on the card as *"Result: …"*.
- **Cancel**, available on any order that has not yet reached a terminal state. Cancelled is terminal: a cancelled order cannot be reactivated (place a new order instead).

Repeating a transition an order has already made is treated as a harmless no-op rather than an error, which keeps double-clicks and retried requests safe.

Orders may only ever be *created* in draft (or active) state. There is no way to create an order that is already completed or resulted, the state machine is the only path to later states, which keeps the ordering audit trail honest.

### 8.4 Printing the orders list

When a patient has at least one order, a **Print** button appears in the panel header. Clicking it (the label changes to *"Preparing…"*) renders a print-formatted **Active Orders List**, headed "Orb Hospital System", stamped with the generation time and marked **CONFIDENTIAL MEDICAL RECORD**, and opens your browser's print dialog automatically. The printout lists each order's type, description, priority, status, result (if any), and creation time. Printing or cancelling the dialog returns you to the normal panel view.

### 8.5 Order-time drug safety

Every **medication** order passes through Orb's drug-safety gate before it is written to the chart. The same single gate guards every other route by which a medication can enter Orb, adding a med directly in a patient's Vigil chart, ambient actions from Relay, agentic suggestions from Sage, medications auto-extracted from signed notes, and Helix approvals, so there is no back door around the screen.

#### What the screen checks

1. **Allergy screening**, against the patient's documented active allergies:
   - *Direct matches*, the ordered drug's name overlaps a documented allergen (e.g. ordering amoxicillin for a patient allergic to amoxicillin).
   - *Drug-class matches*, the drug belongs to the same class as a documented allergen (e.g. ordering flucloxacillin, a penicillin, for a penicillin-allergic patient).
   - *Cross-reactivity*, known cross-reactive classes (e.g. a cephalosporin ordered for a penicillin-allergic patient, and vice versa). The alert is graded by the documented reaction, exactly as the bedside check grades it: a penicillin anaphylaxis, or a penicillin allergy whose severity was never recorded, makes a cephalosporin order a severe alert that stops until acknowledged; a moderate rash history warns.
2. **Drug–drug interaction screening**, comparing the new drug against every medication the patient currently has active. The interaction knowledge base is data-backed and covers high-consequence pairs such as warfarin + NSAIDs/aspirin (severe bleeding risk), duplicate anticoagulation, potassium + potassium-sparing diuretics (severe hyperkalaemia), SSRIs + MAOIs, statin combinations, and more.

Each alert carries a severity, **mild**, **moderate**, or **severe**, and the screen reports the single worst severity found.

#### What you will see

**No alerts, drug recognised.** The order is created normally with no interruption.

**Sub-severe alerts (mild/moderate), or the drug was not screened.** The order **is** created, but a review-only dialog titled **"Drug-safety review"** appears first, listing the *Allergy alerts* and *Interaction alerts* that were raised, each tagged with a severity chip. Read it, then click **Close** to finish. Nothing further is required, this mode is informational, so useful alerts are seen without adding friction to routine ordering.

**Severe alerts.** The order is **blocked** before anything is saved, and a dialog titled **"Severe drug-safety alerts"** opens with the warning: *"This order is blocked until the alerts below are reviewed. Acknowledging records your identity and justification on the audit chain."* To proceed anyway you must:

1. Read the listed allergy and interaction alerts.
2. Type a **Clinical justification** of at least 10 characters into the text box, a genuine reason why proceeding is clinically appropriate despite the alerts.
3. Click **Acknowledge & Order**.

The order is then re-submitted with your acknowledgement attached and is created. Your identity, the justification you typed, and the full list of alerts you overrode are written permanently to the tamper-evident audit chain. Click **Cancel** instead to abandon the override; the order form stays open so you can change the drug.

**"Not screened" notice.** If Orb cannot map the ordered drug to one of its twelve known drug classes, the safety dialog shows a **Not screened** chip with the note that *drug–drug interactions were NOT screened, verify manually*. Allergy matching by name still runs regardless. **The limit:** the chip does **not** fire on a drug Orb classifies successfully but holds no curated interaction pair for, that returns "No alerts", indistinguishable from a genuinely clean screen. Orb ships 55 curated pairs across 12 classes; a licensed maintained source must be bulk-loaded before go-live.

**Screening unavailable.** If the safety screen itself fails to run, Orb fails **closed**: the order is refused with a clear message that screening is unavailable, rather than quietly proceeding unchecked. Resolve the fault or route the medication through pharmacist verification.

#### Automation never overrides

Only an interactive clinician, present at the screen, can acknowledge severe alerts. Non-interactive paths, AI-driven or automated flows, can never acknowledge a severe alert away; a severe hit on those paths blocks the medication and routes it to human review instead. Every screen (clean, alerting, blocked, or overridden) is audited, whether or not you ever see a dialog.

The same acknowledgement flow appears when adding a medication from a patient's chart in Vigil (the **Add Medication** dialog with its Drug Name, Dosage, Route, and Frequency fields), the modal, the 10-character justification, and the audit trail behave identically there.

### 8.6 The eMAR: recording bedside administration

CPOE and pharmacist verification put a medication *on the chart*; the eMAR records what actually happened *at the bedside*. Every administration attempt, given or withheld, becomes one permanent record in the patient's administration history.

#### Where to find it

1. Open **Patients (Vigil)** from the sidebar and select your patient.
2. In the patient detail command bar, click **Medications**.
3. The Medications dialog lists every prescribed medication in a table: **Drug**, **Dose**, **Route**, **Frequency**, **Status** (● Active, ◌ PRN, or ○ D/C), and **Administration**.

The **Administration** column is the working MAR for the row:

- A green **"Given ‹time›"** chip shows when the most recent dose was recorded; hovering over it shows who recorded it. Together with the **Frequency** column (e.g. QID), this is how you judge whether the next dose is due.
- An **Administer** button appears on every *active* medication. It is disabled, greyed out with the tooltip *"Pharmacist verification required"*, for any AI-sourced medication that a pharmacist has not yet verified. Such medications cannot be administered under any circumstances until verification happens (see the Pharmacy chapter).

#### The Administer modal

Clicking **Administer** opens the **"Administer medication"** dialog, which is built around the five rights of medication administration:

1. **Five-rights summary.** The top of the dialog lists **Patient** (name and bed), **Drug**, **Dose**, **Route**, and **Time** (the current time, marked "now"). Check each line against the physical medication in your hand and the wristband on the patient.
2. **Confirm drug name.** The dialog's central safety control is a barcode-style type-to-confirm field: you must type the drug name *exactly as charted* (case-insensitive) into the **Confirm drug name** box. The **Given** button stays disabled until the typed name matches, which forces a deliberate comparison between the medication in hand and the chart row before a dose can be recorded. The dialog states this explicitly: *"Barcode-style check: type the drug name exactly as charted (case-insensitive) to enable 'Given'."*
3. **Given.** Once confirmed, click **Given** (it shows *"Recording…"* while saving). The dose and route recorded default to exactly what was ordered, the ordered dose is the administered dose unless stated otherwise.
4. **Not given.** If the dose is being withheld, click **Not given** instead. A **Reason not given** section appears with a structured dropdown: **Refused**, **Held, clinical decision**, **NPO**, **Unavailable**, or **Other**, plus a free-text detail line (optional for the named reasons, *required* when the reason is Other). The **Record not given** button stays disabled until a valid reason is in place, a withheld dose can never be recorded without one.
5. **Cancel** closes the dialog without recording anything.

After a successful recording the dialog closes, the history refreshes, and the row's "Given" chip updates.

#### Guarantees behind the modal

- **Active orders only.** A dose can only be recorded against a medication whose order is active. Attempting anything else is refused with an inline error message in the dialog.
- **Unverified AI medications are blocked** server-side as well as in the UI, even a crafted request cannot chart a dose of an unverified, AI-extracted medication.
- **Append-only history.** Administration records are never edited or deleted; corrections are new rows. The history stays faithful to what was recorded, when.
- **Safe against double-charting.** Each recording carries a one-time key, so if a flaky network causes the same recording to be sent twice (for example an offline replay), the second attempt returns the original record instead of charting the dose twice.
- **Audited atomically.** The administration row and its audit entry are committed together on the tamper-evident chain, an administration can never exist without its audit record. The audit captures the drug, dose and route given (or the reason not given), who recorded it, and how it was confirmed.

The patient's full administration history (newest first, with the administering user's name) is retrievable per patient and feeds the "Given" chips throughout Vigil.

### 8.7 Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| **Create Order** stays disabled | Coded order types require a validated code *and* a description. | Click **Validate** until you see ✓; check the description field is not empty. For free-text orders, switch the type to `nursing` or `other`. |
| "✗ unknown code" for a code you know is real | The full terminology release has not been loaded; only the curated starter subset is present. | Double-check the code and system match (a LOINC code under ICD10 will fail). If correct, ask your administrator to bulk-load the full terminology release. |
| Order rejected with "Unknown … code" on submit | The server re-validates codes independently of the form. | Re-validate the code; do not attempt to bypass the ✓ check. |
| A transition button seems missing | The order is in a terminal state (completed, resulted, cancelled), or the transition is not legal from its current state. | Check the status chip against the lifecycle table in 8.3. Terminal orders cannot move; place a new order. |
| **+ Result** not offered | Only lab and imaging orders in active/in progress state accept results. | Advance the order to active first; other order types never take results. |
| Medication order blocked by a 428/"Severe drug-safety alerts" dialog | The screen found a severe allergy or interaction hit. | Review the alerts. Either change the drug, or complete the **Clinical justification** (minimum 10 characters) and click **Acknowledge & Order**, knowing the override is permanently audited. |
| "The drug-safety screen could not run" | The screening service errored; Orb fails closed rather than ordering unchecked. | Retry; if it persists, report the fault and use the pharmacist verification path. |
| **Administer** button greyed out | The medication is AI-sourced and awaiting pharmacist verification, or is not an active order. | Hover the button to see the reason. Ask pharmacy to verify the medication; it becomes administrable immediately after. |
| **Given** button disabled in the Administer modal | The type-to-confirm field does not match the charted drug name. | Type the name exactly as displayed in the **Drug** line (capitalisation does not matter). |
| **Record not given** disabled | No reason selected, or "Other" chosen without a description. | Pick a reason from the dropdown; if Other, fill in the required detail line. |
| Error message inside the Administer modal | A server-side guard fired (inactive order, unverified medication, missing reason). | Read the inline message, it states the exact guard, and resolve it; nothing was recorded. |

### 8.8 Safety & limitations

- **The starter knowledge bases are deliberately small.** Both the terminology subset and the drug-interaction table ship as curated, real-data starter sets sized for demonstration and pilot use. They catch the common, high-consequence cases (penicillin/cephalosporin cross-reactivity, warfarin + NSAIDs, duplicate anticoagulation, hyperkalaemia combinations), but they are **not** comprehensive. For production use, load the full terminology releases and back the interaction table with a licensed, maintained source.
- **"No alerts" is not a clinical clearance.** A clean screen means nothing in the local knowledge base matched, it does not replace pharmacist review or clinical judgement. Watch specifically for the **Not screened** chip: a drug outside the knowledge base was not interaction-checked at all, and the system says so rather than implying safety.
- **Overrides are audited, not erased.** Acknowledging severe alerts records your identity, your written justification, and the alerts overridden on a tamper-evident chain. Overrides are for genuine clinical judgement calls, and the record is permanent.
- **The screen fails closed.** If safety screening cannot run, medication orders are refused rather than silently unscreened. This is intentional; do not interpret a screening outage as clearance.
- **Automated paths cannot self-approve.** No AI or background process in Orb can acknowledge a severe alert or administer an unverified medication. Human review is structurally required, not just recommended.
- **The eMAR type-to-confirm is a check, not a barcode scanner.** It enforces a deliberate name comparison but cannot physically verify the vial in your hand. Perform the full five-rights check at the bedside every time.
- **Dose timing is by clinical judgement.** The Medications table shows the ordered frequency and the time of the last recorded dose; Orb does not currently generate a scheduled due-time worklist, so due-ness is judged from those two facts by the nurse.
