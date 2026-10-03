## 9. Helix: Pharmacy & Verification

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![The Helix pharmacy workspace: catalogue, inventory lots, and the pending verification queue.](assets/manual/helix-pharmacy.png)

*The Helix pharmacy workspace: catalogue, inventory lots, and the pending verification queue.*

Helix is Orb's pharmacy pillar: the medication catalog and physical stock, a strict audited lifecycle for every medication order from prescription to delivery, and the pharmacist's role as the final human checkpoint on every drug the hospital's AI has touched. It appears as two sidebar entries:

- **Pharmacy** (the Helix workspace), the day-to-day operations board: pending orders, verification and fulfillment, the patient medication board, and the formulary with live stock levels.
- **Verify Queue** (Pharmacy Verification), a safety queue holding every medication charted by an AI pathway, awaiting pharmacist review before it can become an active, administrable order.

This chapter covers both surfaces, the order lifecycle, stock deduction, and how Helix interlocks with order entry (the Orders tab) and bedside administration (the eMAR in the Patients tab).

### 9.1 Who can use Helix, and how to open it

Helix is governed by dedicated pharmacy permissions: a read permission (viewing the catalog, inventory, and queues) and a write permission (approving, verifying, rejecting, substituting, dispensing). The **pharmacist** role holds both, plus Sage for clinical context, a pharmacist's sidebar shows exactly three tabs: **Pharmacy**, **Verify Queue**, and **Sage**. Administrators hold the underlying permissions too, but the tabs belong to the pharmacist's sidebar; doctors and nurses do not see them.

To follow along on a demo system:

1. Sign in with the seeded pharmacist account, employee ID **PHA001** (Deepa Joshi), password **password**.
2. Click **Pharmacy** in the sidebar. The panel briefly shows *"Connecting to Helix..."* while it loads orders, inventory, and the catalog, then opens on the Overview tab.

The workspace header reads **Pharmacy Operations** with the subtitle *"Medication orders, verification, and fulfillment"*, and offers three view tabs on the right: **Overview**, **Patient Board**, and **Formulary**.

### 9.2 The medication order lifecycle

Every medication order in Helix moves through a fixed set of statuses. Only specific, legal steps are permitted, the system will refuse any transition that is not in this table, so an order can never skip verification or be dispensed twice.

| Status | Badge | Meaning | What can happen next |
|---|---|---|---|
| Pending | **Pending** | Newly created; awaiting a pharmacist | Begin Review |
| Reviewing | **Reviewing** | A pharmacist has picked it up | Verify, Reject, or Suggest Substitute |
| Verified | **Verified** | Clinically approved by pharmacy | Schedule |
| Rejected | **Rejected** | Declined by pharmacy, terminal | Nothing (final state) |
| Substitute suggested | **Substitute** | An alternative product was proposed | Return to Review |
| Scheduled | **Scheduled** | Queued for dispensing at a set time | Dispense |
| Dispensed | **Dispensed** | Stock deducted; en route to the ward | Nothing for pharmacy. The **ward** records receipt |
| Delivered | **Delivered** | A named nurse signed for the drug on the ward, terminal | Nothing (final state) |

Two properties matter in daily use:

- **Every step is recorded.** Each transition writes an immutable history entry, who acted, when, from which status to which, with any notes or substitution, so the full journey of any order can be reconstructed.
- **Every step is idempotent.** A button pressed twice, or a request replayed over a flaky network, is not applied twice; the system returns the already-recorded result. You cannot double-dispense by double-clicking.

### 9.3 The Overview tab: your work queue

The Overview tab is the pharmacist's home screen. It has three zones.

#### Metric cards

A row of cards summarizes the state of the whole pharmacy at a glance:

| Card | Counts | Subtitle |
|---|---|---|
| **Pending Review** | Orders in Pending, Reviewing, or Substitute status | "Orders awaiting action" |
| **Verified** | Verified orders | "Ready for scheduling" |
| **Scheduled** | Scheduled orders | "Awaiting dispense" |
| **In Transit** | Dispensed orders | "Dispensed, awaiting delivery" |
| **Delivered** | Delivered orders | "Completed today" |
| **Rejected** | Rejected orders | "Requires attention", this card only appears when at least one order has been rejected, and is highlighted as critical |

#### Pharmacist Work Queue

The **Pharmacist Work Queue** table lists every order needing pharmacist action, Pending, Reviewing, and Substitute-suggested, with columns **Patient** (name plus ward/bed), **Medication** (name plus dosage, frequency, route), **Prescriber**, **Time**, and **Status**. Orders from an automated pathway with no named prescriber show **System** as prescriber. When there is nothing to do, the queue shows *"Queue clear, no pending orders."* Click any row to open the **Order Review** dialog (section 9.4).

#### Fulfillment Pipeline

Below the work queue, the **Fulfillment Pipeline** table appears whenever orders are in flight, Verified, Scheduled, or Dispensed, showing **Patient**, **Medication** (the substitute's name, if one was approved), **Ward / Bed**, and **Status**. These rows are clickable too, so you can push an order through scheduling, dispensing, and delivery from the same dialog.

### 9.4 Reviewing an order: the Order Review dialog

Clicking any order opens the **Order Review** dialog, which always shows:

- **Current Status**, the order's badge.
- **Order Details**, Patient, Ward / Bed, Medication, Dosage, Frequency, Route, and Prescriber, plus a **Substitute** line if one was proposed and a **Notes** line if a pharmacist has written any.

The action buttons at the bottom change with the order's status, you are only ever offered the legal next steps.

#### Step-by-step: verifying a routine order

1. On the Overview tab, click a **Pending** order in the Pharmacist Work Queue.
2. Click **Begin Review**. The order moves to Reviewing and the dialog closes; click the order again to continue (the queue refreshes automatically).
3. In Reviewing, the dialog offers **Verify**, **Reject**, and **Suggest Substitute**, plus two extra sections:
   - **Substitute Medication (optional)**, a dropdown of every other catalog product, shown as name and strength.
   - **Pharmacist Notes**, a free-text box with the placeholder *"Add verification notes, rejection reason, or substitution rationale..."*; anything typed here is stored on the order and shown to subsequent reviewers.
4. Check the drug, dose, frequency, and route against the patient and your clinical judgment, add any notes, and click **Verify**.
5. The order becomes **Verified**, and Helix posts a system message into the patient's care-team chat room in Relay, *"Pharmacy has verified the order for [medication]. Dispensing will commence."*, so the ordering clinician and nurses see, unprompted, that pharmacy has signed off.

#### Rejecting an order

From Reviewing, click **Reject** instead. Enter your reason in **Pharmacist Notes** first, Rejected is terminal, so the note is the record the care team will see. Nothing is deleted: the order keeps its Rejected badge, and the Rejected metric card surfaces it until dealt with clinically (typically a new order from the prescriber).

#### Suggesting a substitute

When the prescribed product is out of stock or a therapeutic alternative is preferable:

1. From Reviewing, choose the alternative in the **Substitute Medication (optional)** dropdown. **Suggest Substitute** stays disabled until you have selected one.
2. Add your rationale in **Pharmacist Notes**, then click **Suggest Substitute**. The order moves to **Substitute** status, showing both the original and the proposed substitute.
3. To proceed, open the order and click **Return to Review**, then **Verify** as usual. From then on the substitute is what is dispensed and what appears in the Fulfillment Pipeline, the eventual stock deduction comes from the substitute's inventory, not the original's.

#### Scheduling and dispensing

1. Open a **Verified** order and click **Schedule**. The order is stamped with the scheduling time and moves to Scheduled.
2. Open a **Scheduled** order and click **Dispense**, the step that touches inventory (section 9.5). The order moves to Dispensed and counts on the In Transit card.

That is where the pharmacist's work ends. **Delivery is a nurse action.** A Dispensed order offers no further pharmacy buttons; the ward signs for the drug on the nurse's **Deliveries** tab, under the `helix.receive` permission that pharmacists do not hold, and the server refuses the write when the account signing is the account that dispensed. See `docs/USER_MANUAL.md` §9.4a for the full description.

While a transition is being applied the buttons show **Processing...** and are disabled. If a transition fails, for example, insufficient stock at dispense time, the error appears in red inside the dialog and the order is left exactly as it was.

### 9.5 Inventory, lots, and stock deduction

Helix tracks stock at the **lot** level: each catalog medication can have one or more inventory lots, each with a lot number, quantity, and expiration date.

When you click **Dispense**, Helix:

1. Finds the lot of the medication being dispensed (the approved substitute, if there is one) with stock remaining and the **earliest expiration date**, first-expiring, first-out, so short-dated stock is used before fresh stock.
2. Deducts one unit from that lot, atomically with the status change, the deduction and the transition both happen or neither does.
3. Writes a **stock movement** record tying together the lot, medication, order, and quantity change, the permanent audit trail of every unit that left the shelf and why.

If no lot has stock, the dispense is refused with an insufficient-stock error and the order stays Scheduled. The remedy is a restock or a substitution (via the substitute path above).

### 9.6 The Patient Board tab

The **Patient Board** answers a different question from the work queue: not "what do I do next?" but "how is each patient's medication picture going?" The **Patient Medication Board** table shows one row per patient with active orders:

- **Patient**, name and ward/bed.
- **Medications**, every order for that patient with its dose and status badge (the substitute's name where one was approved).
- **Fulfillment**, a progress summary such as "2/3 delivered", with a second line ("1 in transit") when doses are between dispense and delivery. Rejected orders are excluded from the denominator.
- **Stock**, per-order stock for the prescribed medication: total units on hand, a **Low** badge with the remaining count below 100 units, or a red **Out of Stock** badge when nothing is left.

Use this board before ward rounds or when a nurse calls asking where a dose is: you can see instantly whether a patient's medications are stuck in review, in transit, or blocked by a stockout.

### 9.7 The Formulary tab

The **Formulary** tab shows the **Medication Formulary & Inventory**, the master catalog joined with live stock:

| Column | Contents |
|---|---|
| **Medication** | Brand/product name, with the generic name underneath |
| **Formulation** | Form (tablet, injection, etc.) and strength |
| **Available Stock** | Total units across all lots |
| **Active Lots** | The lot numbers currently held ("None" if empty) |
| **Status** | **In Stock**, **Low Stock** (fewer than 30 units remaining), or **Out of Stock** |

The Formulary's **Low Stock** threshold (below 30 units, tuned to the demo inventory scale) differs from the Patient Board's **Low** badge (below 100 units); the Patient Board is deliberately more sensitive because it warns about a specific patient's continuing supply.

The formulary also powers ordering elsewhere in Orb: a medication not in this catalog cannot become a Helix order at all, an AI-drafted request for an unlisted drug is flagged **Not Available in Pharmacy** rather than silently accepted.

### 9.8 Where orders come from: Sage drafts and the safety gate

Helix orders arrive from human prescribers and from Orb's AI assistant, Sage. Sage never writes an order itself. When asked to order a medication it produces a **Chart Action** card in the conversation showing the patient, the medication, and a status badge, **Drafted** at first. The clinician reviews the card and clicks **Approve & Send to Helix** (it reads **Sending...** while it works). Only that human click creates the order, which lands in Helix as **Pending**, at the very start of the pharmacist lifecycle. The card then shows **Sent to Pharmacy**; failures show **Failed**, and a drug not in the formulary shows **Not Available in Pharmacy**.

Two safeguards run at the moment of approval:

- **Patient identity must be unambiguous.** A name matching zero patients, or more than one, is refused outright rather than risk charting a medication to the wrong person.
- **The drug-safety gate runs before anything is written.** Orb screens the drug against the patient's documented allergies (including cross-reactive drug classes) and current medications for interactions. Alerts below severe severity are attached for display and audit. A **severe** alert blocks the approval: the clinician sees the full alert detail and must re-submit with an explicit acknowledgement reason; that override, who, why, and which alerts, goes to the tamper-evident audit chain. If screening itself is unavailable, the gate refuses rather than assuming safety.

The same gate protects every route by which a medication can reach a chart, order entry in the Orders tab, medication additions in the Patients tab, ambient AI actions in Relay, note auto-extraction, and Helix approvals, one screening standard hospital-wide. Fully automated pathways can never acknowledge a severe alert away; they must hand the medication to a human.

Sage's agentic ordering also runs a pre-flight stock check against Helix and can propose an in-stock alternative (same generic, or a standard therapeutic-class substitute) when the requested drug is out of stock, so stockouts surface at drafting time, not at the dispensing bench.

### 9.9 The Verify Queue: verifying AI-charted medications

The **Verify Queue** sidebar tab opens the **Pharmacist Verification Queue**, subtitled *"AI / agentic medications awaiting pharmacist review before they become active orders."* It is a different queue from the Pharmacy workspace, with a different job.

#### Why this queue exists

Some Orb pathways chart medications directly onto a patient's medication list, an AI extraction from an uploaded document, or an autonomous agent acting on a conversation. Orb deliberately charts these in a **quarantined** state: tagged with their machine provenance, dose often the placeholder *REVIEW REQUIRED*, and no verifier recorded. A quarantined medication is visible on the chart but **cannot be administered**: the bedside eMAR refuses it with *"unverified medication, pharmacist verification required."* The Verify Queue is where a pharmacist lifts that quarantine, or refuses to.

#### Reading the queue

Each row is one unverified medication, with columns:

| Column | Contents |
|---|---|
| **Patient** | The patient the medication is charted to |
| **Drug** | The drug name as charted |
| **Dose / Route / Frequency** | Editable fields (see below) |
| **Source** | A provenance badge, **AI extracted** or **Agentic** |
| **Safety alerts** | The freshly-run drug-safety screen for this drug and patient |
| **Actions** | **Verify** and **Reject** buttons |

The **Safety alerts** cell is designed so a screening gap can never masquerade as a clean result:

- A clean, fully-screened drug shows a green **No alerts**.
- Alerts are listed with type and severity (for example *ALLERGY · SEVERE*) and a plain-language message; severe alerts are bold red, moderate amber.
- A drug Orb cannot map to a known drug class shows an amber **Not screened** badge with a coverage note, any allergy alerts it *did* get are still listed, but no green "No alerts" is shown. A classified drug with no curated pair does not get this badge.
- If screening itself failed, a red **Screening unavailable** badge appears and no alert list is rendered, absence of alerts here means "unknown," never "safe."

A **Refresh** button reloads the queue on demand. When nothing is waiting, the panel shows **Queue clear**, *"No AI/agentic medications are awaiting verification."*

#### Step-by-step: verifying a medication

This is the pharmacist's rights-check on the order itself, right patient, right drug, right dose, right route, right frequency, before the medication may become administrable. (The nurse checks the rights once more at the bedside, see section 9.10.)

1. Read the **Patient** and **Drug** columns against the clinical context. Sage is in your sidebar if you need the patient's story.
2. Fill in **Dose**, **Route**, and **Frequency** (placeholders suggest the format: *e.g. 500mg*, *e.g. Oral*, *e.g. TDS*). Where the AI charted a *REVIEW REQUIRED* placeholder these fields start empty, so you type a real value rather than editing boilerplate; where the AI supplied values they are pre-filled for you to correct. What you leave here becomes the authoritative order.
3. Review the **Safety alerts** cell.
4. Click **Verify**. On success the row disappears and a green confirmation appears ("Verified [drug] for [patient]"). The medication is now an **active** order, stamped with your identity and verification time, and the eMAR will accept it for administration.

#### Severe alerts: the acknowledgement flow

The safety screen is re-run at the moment you click **Verify**, not just when the queue loaded, so the decision is always made against current data. If it finds a **severe** alert, or cannot run at all, verification is blocked and an inline panel opens beneath the row, titled **"Severe safety alerts, acknowledgement required"** (or **"Drug-safety screening unavailable, acknowledgement required"**). The panel repeats the alerts and any coverage note, with a text box: *"Clinical reason for verifying despite the alerts (required, audited)."*

- To override, enter your clinical justification and click **Acknowledge & verify** (disabled until a reason is written). The override, your identity, the reason, and the full alert list, is written to the tamper-evident audit chain, and the medication is activated.
- To back out, click **Cancel**. The medication stays quarantined in the queue.

#### Rejecting a medication

Click **Reject**. You are prompted, *"Reason for rejecting [drug]?"*, and an empty reason cancels; a reason is mandatory. On rejection the medication is **discontinued**, your identity and timestamp are recorded as the deciding pharmacist, and the reason is appended permanently to its notes. Nothing is deleted: the discontinued entry remains part of the patient's record.

Every verify and reject, with the alert severity and screening coverage at decision time, is audited. A medication can only be decided once, if a colleague verified it while you were reading, your attempt is refused rather than double-applied.

### 9.10 How Helix connects to CPOE and the eMAR

Helix sits between ordering and administration:

- **Upstream (CPOE).** Doctors and nurses place orders, including medication orders, from the **Orders** tab. Medication orders there pass through the same drug-safety gate as section 9.8, with the same severe-alert acknowledgement rules. Helix's formulary and stock also feed Sage's order drafting.
- **Downstream (eMAR).** Bedside administration happens in the **Patients** (Vigil) tab, where the nurse's Administer dialog presents a five-rights summary, patient, drug, dose, route, time, with type-to-confirm checks before a dose is recorded as given. Two Helix decisions gate that moment: an AI-charted medication not yet verified in the Verify Queue cannot be administered at all, and the dose and route the nurse confirms are the ones the pharmacist fixed at verification.
- **Sideways (Relay).** When a Helix order is verified, the care team's case room receives an automatic system message, closing the loop without phone calls.

### Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Pharmacy or Verify Queue tab missing from the sidebar | Not signed in as a pharmacist | Sign in with a pharmacist account (demo: PHA001) |
| Stuck on *"Connecting to Helix..."*, or the Verify Queue shows a load error | Backend unreachable or session expired | Refresh and sign in again; if it persists, contact your administrator |
| **Dispense** shows an insufficient-stock error | Every lot of that medication is empty | Check the Formulary tab; restock or use **Suggest Substitute** |
| **Suggest Substitute** greyed out | No substitute selected | Pick a product in the **Substitute Medication (optional)** dropdown first |
| An "Illegal transition" error appears | The order changed state since your screen loaded (a colleague acted) | Close the dialog; the queue refreshes with the current status |
| **Verify** in the Verify Queue opens a red acknowledgement panel | The re-run screen found severe alerts, or screening is unavailable | Read the alerts; enter a clinical reason and **Acknowledge & verify**, or **Cancel** and reject/escalate |
| **Acknowledge & verify** disabled | Reason box empty | A written clinical reason is mandatory for any override |
| A Verify Queue row shows **Not screened** | Orb could not map the drug to a known drug class | Perform your interaction check manually; listed allergy alerts are still valid |
| A verified medication is refused by the nurse's eMAR | The nurse is administering a different, still-unverified entry | Check the Verify Queue for remaining quarantined entries for that patient |
| Nurse reports a dose "missing" | The order is mid-pipeline | Check the Fulfillment Pipeline / Patient Board for its status |

### Safety & limitations

- **AI never activates a medication.** Sage can only draft; a clinician must click **Approve & Send to Helix**, and even then the order starts at Pending in the pharmacist's queue. AI-charted medications stay quarantined until a pharmacist verifies them, and the eMAR refuses to record administration of an unverified AI medication.
- **The safety screen advises; it does not decide.** Allergy and interaction screening runs from a locally curated knowledge base, 55 interaction pairs across 12 drug classes as shipped, and is not exhaustive. Drugs Orb cannot classify are flagged **Not screened**, and a failed screen is flagged **Screening unavailable**. But a classified drug with no curated pair reports "No alerts" with no flag, so treat a green "No alerts" as "nothing on Orb's shortlist matched" and apply your own clinical judgment always.
- **Severe alerts fail closed.** A severe alert, or an unavailable screen, blocks activation until a named human records a reason; automated pathways can never supply one. Every override is written to the tamper-evident audit chain with the full alert detail.
- **There is no dispensing robot or tube integration.** **Dispense** is a pharmacist's click standing in for the physical act of picking and sending the drug, and **Confirm receipt** is a nurse's click standing in for taking it off the tray. Everything the software records is real: the lot decrement, the movement ledger, the two named actors, the timestamps, and the audit history. What Orb cannot see is whether the physical drug matched the record.
- **Single-unit deduction.** Each dispense deducts one unit from the earliest-expiring lot; quantities are not dose-calculated from the prescription. Read inventory figures as demo-scale counts, not a validated perpetual-inventory system.
- **Terminal states are permanent.** Rejected and Delivered orders cannot be reopened; corrections require a new order. A rejected Verify Queue medication is discontinued, not deleted, the record and reason persist.
- **Helix is not a dosing reference.** Nothing here checks dose appropriateness for weight, renal function, or age. The dose the pharmacist confirms is the dose that will be administered; verifying it is a professional judgment, not a system output.
