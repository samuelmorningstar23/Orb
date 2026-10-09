## 16. The Hospital-Administration Modules

> **New section, written 2026-08-15.** Eleven modules shipped in the three days before this pass and none of them had any coverage in this manual. Everything below was verified by reading the named file at the named line; nothing is inferred from another document. Where a module's own source states a boundary or a refusal, it is quoted rather than paraphrased, because those sentences are the design.

### 16.0 What they are, and the ruling that produced them

**Orb replaces the hospital information system, so it has to carry the parts a hospital cannot run without.** That is the founder ruling of 2026-08-11, and it is recorded in the config file rather than only in a strategy doc (`server/config.py:542-547`):

> *"Orb REPLACES the hospital information system, so it has to carry the parts a hospital cannot run without: billing, payments, payer/insurance, procurement and the front desk. Founder ruling 2026-08-11: build them all NOW and ship them VISIBLE, with one switch each so a site that already has (say) a purchasing system can hide that module without a code change or a rebuild."*

Six more followed on the same pattern: biomedical, housekeeping, workforce, dietary, NABH and ABDM.

**A "module" here means four things wearing the same word:** a server flag, a nav tab, a lazily-loaded panel, and an API prefix. The tab id, the flag key and the module id are deliberately identical *"so there is one vocabulary for 'is the billing module on?' across the whole system"* (`server/config.py:717-719`; `src/shellConfig.tsx:48-53`).

| Module | Env switch | API prefix | Service | Panel |
|---|---|---|---|---|
| Billing | `ORB_BILLING` | `/api/billing` | `billing_service.py` | `BillingPanel.tsx` |
| Payments | `ORB_PAYMENTS` | `/api/payments` | `payments_service.py` | `PaymentsPanel.tsx` |
| Payer / TPA | `ORB_PAYER` | `/api/payer` | `payer_service.py` | `PayerPanel.tsx` |
| Procurement | `ORB_PROCUREMENT` | `/api/procurement` | `procurement_service.py` | `ProcurementPanel.tsx` |
| Front desk | `ORB_FRONTDESK` | `/api/frontdesk` | `frontdesk_service.py` | `FrontDeskPanel.tsx` |
| Biomedical | `ORB_BIOMEDICAL` | `/api/biomedical` | `biomedical_service.py` | `BiomedicalPanel.tsx` |
| Housekeeping | `ORB_HOUSEKEEPING` | `/api/housekeeping` | `housekeeping_service.py` | `HousekeepingPanel.tsx` |
| Workforce | `ORB_WORKFORCE` | `/api/workforce` | `workforce_service.py` | `WorkforcePanel.tsx` |
| Dietary | `ORB_DIETARY` | `/api/dietary` | `dietary_service.py` | `DietaryPanel.tsx` |
| NABH | `ORB_NABH` | `/api/nabh` | `nabh_service.py` | `NabhPanel.tsx` |
| ABDM / ABHA | `ORB_ABDM` | `/api/abdm` | `abdm_service.py` | `AbdmPanel.tsx` |

Router prefixes verified in each `server/routes/<module>_routes.py`; panels in `src/pages/panels/`. Every panel has a co-located `*.test.tsx`.

### 16.1 The switch semantics: read this before answering a customer question

```python
_MODULE_OFF_VALUES = ("0", "false", "off", "no")            # server/config.py:559

def _module_enabled(env_name: str) -> bool:                 # :562-570
    raw = (os.getenv(env_name) or "").strip().lower()
    if not raw:
        return True
    return raw not in _MODULE_OFF_VALUES
```

**All eleven default ON. Unset means ON. Blank means ON.** The reason is written in the file: *"'visible' is the ruling and an operator who fat-fingers `ORB_BILLING=` to empty must land on the documented default rather than silently lose the billing screen"* (`server/config.py:550-554`).

**None of them gates a clinical path.** Stated once for the group at `server/config.py:555-557` and separately for the two whose subject matter is clinical:

- **Dietary** (`:679-697`): *"It gates no clinical write path. Medication ordering, administration, vitals, alerts, shadow capture, the audit chain, auth and the offline queue are untouched by this flag in either position."*
- **NABH** (`:699-715`): *"Reporting an incident does not create, block, delay or annotate any order, administration, vital, alert, escalation or shadow-mode record … With the flag off, a nurse's day is byte-identical."*

**How the frontend finds out.** `GET /api/health` returns `config.enabled_modules()` (`server/main.py:985-1004`, `server/config.py:735-758`), the only unauthenticated endpoint the shell can read before a session exists, which it must, because it has to know which tabs to build at first paint. **A tab survives only if the flag is on AND the panel is in the build** (`src/shellConfig.tsx:970-972`), never a tab that opens onto nothing.

### 16.2 Who can see them: the split that matters

`src/shellConfig.tsx:812-824`:

- **`CLINICAL_MODULE_IDS = ['dietary']`**, dietary alone, because `POST /api/dietary/orders` requires `order.write`, which only doctors and nurses hold. *"A nurse who cannot see who is nil by mouth is a nurse who hands somebody breakfast before theatre"* (`:69-75`).
- **`MONEY_MODULE_IDS`**, the other ten, admin-only.

The test that keeps this honest injects both the module flags and the "is the panel installed" map, so *"a switched-off module has no tab"* and *"no clinical role gets a money screen"* can be asserted for real (`src/shellConfig.tsx:940-953`).

### 16.3 `services/money.py`: the single source of truth for money arithmetic

Every rupee Orb stores, adds, taxes or prints goes through this module. Billing, payments, payer/claims, procurement and the front desk all import from it; **none re-implements a rounding rule, a GST split or an Indian number format.** *"The failure mode this module exists to prevent is five modules that each round 'about the same way' and a bill that is two paise off the sum of its own lines"* (`server/services/money.py:1-9`).

**Rule 1, money is an `int` of paise.** Never a float, never a string, never a `Decimal` at rest. `assert_paise()` **rejects `float` outright**, including `2.0`, which is indistinguishable from a rounding artefact once it is in the column, and rejects `bool`, which is an `int` subclass and would otherwise store `True` as one paise. Parsing happens **once**, at the edge, via `to_paise()`; formatting **once**, at the other edge, via `format_inr()`. In between there is only integer arithmetic (`:11-24`).

**Rule 2, one rounding rule: half-up, away from zero, at the paise boundary.** `_div_round_half_up()` is the only division in the file. Half-up rather than banker's rounding because that is what Indian invoicing, every hospital's existing accounting package and every clerk checking by hand expects. Away from zero rather than toward negative infinity so a refund of −₹1.005 rounds to −₹1.01, the mirror of the charge that created it. *"A refund that is not the exact mirror of its charge is a reconciliation ticket"* (`:26-33`).

**Rule 3, rounding happens early and once per line, never again.** An invoice is the **integer sum of already-rounded lines**, sum-of-rounded, not round-of-sum. This is the order Indian tax invoices are checked in: a line-item total that does not reproduce from its own printed inputs is what an auditor flags. The residue costs at most a paise per line at the invoice foot, absorbed by an optional, explicit, separately recorded round-off line (`:35-42`).

> **If you are changing anything that touches money, read `money.py` in full first.** It is short, and every rule in it is there because of a specific way bills go wrong.

`services/billing_masters.py` is its companion: what an item costs, who is paying, and what number a document gets. *"Nothing in [billing_service] invents a rate, a rounding rule or a tax split"* (`server/services/billing_service.py:1-11`).

### 16.4 Billing

`server/services/billing_service.py`, turns things that happened to a patient into a bill, built on `money.py` and `billing_masters.py` and duplicating neither.

**The discharge advisory** is the part with a policy attached: see `ORB_DISCHARGE_FINANCIAL_ENFORCE` (§15.2.9). Default **off**, and it must stay off for the pilot. A discharge may compute and display the patient's financial position and warn about it; **it may not stop the discharge.** The discharge write path does not consult this module for permission, it records what the money side saw (`discharge_financial_checks`) and proceeds (`server/config.py:764-771`).

Two reasons are recorded, and the first alone is sufficient (`server/config.py:773-784`):

1. **Clinical.** A discharge decision is a clinical decision. A patient who is medically ready to leave and is held on a ward by a software rule is occupying a bed somebody sicker needs, and is accumulating the risks of being in a hospital. **In India, holding a patient over a bill is also unlawful.**
2. **Pilot scope.** The shadow pilot does not bill through Orb, the incumbent keeps running the money, so Orb's idea of an outstanding balance is definitionally incomplete during the pilot. Enforcing on an incomplete figure would block discharges over bills already paid somewhere Orb cannot see.

The flag exists at all so a post-pilot site that has moved billing into Orb can enable a soft-stop with an override *"rather than someone adding the enforcement later as an un-flagged `if`"*.

### 16.5 Payments: the four rules

`server/services/payments_service.py:1-9`. **Billing decides what a patient owes; this module records what was paid**, what was held on account, what was given back, who authorised a reduction, and whether the drawer balanced. *"It is the half of the money system a clerk touches all day, and the half an auditor reads first."*

1. **A receipt is immutable.** Once a `payments` row is written it is never edited. A mistake is corrected the way this codebase corrects an audit entry or a signed note: with a new, opposing, linked entry, `reverse_payment()`, leaving both halves visible. **`amend_payment()` exists only to refuse, loudly**, so a caller reaching for an edit gets an exception instead of finding no function and writing their own `UPDATE` (`:12-19`). A reversal writes only *lifecycle* on the original row (`status='reversed'`, who, when, why); amount, method, reference, patient, invoice, purpose, direction and receipt number are never touched after the INSERT (`:21-25`).
   **A reversal needs a second person (2026-10-03).** `reverse_payment()` refuses the person who took the payment, compared ignoring case and surrounding spaces, exactly as `decide_refund()` refuses the requester. The `payments.approve` permission alone never stopped this for an admin, who holds record and approve both, so voiding your own receipt was open to every admin account.
   **Both legs of a reversal count in a drawer or day-end sum; neither counts in a patient's figures.** A reversal moves the original to `reversed` and writes a live `out` row, so summing live rows took the money off twice: a ₹500 receipt, reversed, left the drawer expecting ₹500 less than its float, and the honest count read as short. Drawer and day-end sums use `LEDGER_PAYMENT_STATUSES` (live plus `reversed`); a patient's paid, refunded and refundable figures leave out both legs, because a correction is not a refund.
2. **An over-payment is an advance, never a negative balance.** A receipt is allocated to an invoice up to its remaining due and **not one paise further**; the excess lands in the `patient_deposits` ledger. An invoice's paid total can therefore never exceed its payable total, *"which is what stops a 'balance' column going negative and being read as a credit nobody can explain."* The over-payment path is explicit and reported back as `advance_paise` so the counter can tell the patient what happened (`:27-34`).
3. **Money is integer paise and all of it goes through `money.py`.** No arithmetic in the file rounds; percentages only via `apply_basis_points`; splits via `split_shares`, whose parts sum exactly to the whole (`:36-39`).
4. **A variance is recorded, never absorbed.** `close_counter_session` stores what the system expected, what the cashier counted, and the difference, **even when it is zero, and especially when it is not.** A session that does not balance closes as `disputed`, not `closed`. *"Silently reconciling a ₹200 short is how a till loss becomes invisible"* (`:41-46`).
   **Its corollary:** cash taken with no counter session open belongs to no drawer, so no shift count can explain it. It is recorded anyway, *"refusing it after the notes are on the counter only pushes the money off-system"*, marked `unattributed_cash` on the receipt, and reported separately at day-end, where it holds the whole-day sheet **open** (`:48-56`).
   **The day-end sheet covers every drawer the day touched (2026-10-03):** opened in the day, still open and opened before the day ended, or holding a receipt taken in the day. It used to choose drawers by `opened_at` alone, so a till opened yesterday and never closed was not on today's sheet at all, and the sheet read final with "Counted ₹0.00" over the cash that drawer had taken. Any open drawer now holds the sheet open, and one opened before the day began is named in `finality_blockers` with who opened it and when. The counter screen shows the same facts on the till card (opened by, opened at, and a warning when that was an earlier day).

### 16.6 Payer / TPA

`server/services/payer_service.py:1-9`. Payers, patient policies, pre-authorisation, claims and the aging work queue.

> *"In an Indian hospital this desk is where the money actually lives. A ward earns the revenue; the TPA desk collects it, and the gap between those two facts is weeks of paperwork, a pre-authorisation that has not come back, a claim that was short-settled by ₹4,300 with no reason given, and a denial nobody appealed because nobody noticed it."*

**Permissions.** This is the one admin module with its own permission pair rather than `admin.overview`: `payer.read` / `payer.write`, granted to `admin` (`server/permissions.py:98-99`, `:167-176`). See §1 for why the split runs the direction it does.

### 16.7 Procurement & stores: and the boundary with Helix

`server/services/procurement_service.py:8-45`. Vendors, a non-pharmacy item master, indent → purchase order → goods receipt, stock per store, reorder points, consumption by department, expiry.

**Orb has two stock systems and that is deliberate.** *"Two stock systems that DISAGREE about the same physical thing is a far worse outcome than one incomplete system, so the line between them is drawn structurally rather than by convention."*

- **Helix owns drugs** (`services/helix_db.py`, `helix_medication_master`, `helix_inventory_lots`, `helix_stock_movements`). Dispense-oriented: a lot moves because a pharmacist verified a prescription for a named patient. Its batch/expiry answers *"which lot did this patient's dose come from"*, a recall and medication-safety question.
- **Procurement owns everything else the hospital buys.** Surgical consumables, sutures, gloves, linen, lab reagents, CSSD, housekeeping, stationery, PPE, implants, biomedical spares. Purchase-oriented: stock moves because a vendor delivered against a PO or a ward drew from a store. Its expiry answers *"which reagent lot goes off next"*, a stores question.

**One question, no judgement call:** *"Is it a medicine that a pharmacist dispenses against a prescription?"* Yes → Helix. No → Procurement.

**And it is enforced by the database, not by the docstring.** Migration 20 declares `inventory_items.is_pharmacy INTEGER NOT NULL DEFAULT 0 CHECK (is_pharmacy = 0)`. **A row for a drug cannot be inserted into this module's item master at all, SQLite refuses it.** `upsert_inventory_item` refuses earlier and with a sentence a storekeeper can act on, but the CHECK is the actual guarantee and it survives someone writing raw SQL.

**The known seam:** IV fluids and disposables, which some sites class as pharmacy and some as stores. Whichever a site picks, it must pick **one**, and the choice is visible as the item's presence or absence in this module's master.

**The pharmacy store shares the documents, not the stock (corrected 10 October 2026, grn-18).** The old wording in `procurement_service.py` and `ProcurementPanel.tsx` said Helix tracks "schedule-H"; it did not. A drug's schedule and NDPS class are now recorded in Helix's drug master through the **Pharmacy store** module (`ORB_PHARMACY_STORE`, opt-in and off by default; §8, "The pharmacy store"), which also buys and receives drugs. It reuses this module's indent, purchase order and goods receipt *tables* in a separate `pharmacy` domain (`procurement_service.py`, the Domains note): every core that loads a document by id refuses the other desk's document as not found, a receipt takes its domain from its order, the store master is partitioned by `pharmacy_kind`, and the pharmacy branch of a receipt never writes `stock_movements` or `inventory_items`. Every drug unit it receives lands in Helix's ledger through `pharmacy_stock_service`, so the two-ledgers rule above still holds. The stores desk's screens and lists show stores documents only; a ward's issue indent for drugs (grn-21) never appears on them.

**Retention.** Purchase orders and goods receipts of both desks, and the pharmacy's returns to vendor, are mapped to the `gst_tax_record` retention category (CGST Act 2017 s.36, 72 months from the annual return's due date) in `consent_service._RETENTION_SOURCES`; the category's policy row comes with the W168 retention migration.

### 16.8 Front desk: OPD **and transfers**

`server/services/frontdesk_service.py:1-42`. Two jobs, because one desk does both.

1. **OPD.** A walk-in or booked outpatient is registered against a consulting doctor, gets a token, waits, is called, is seen, and the consultation is **charged through the billing spine** (`encounter_charges`), this module does not invent an invoice, a rounding rule or a price.
2. **Transfer.** *"Orb had `admit` and `discharge` and nothing in between, so a patient moved from Cardiology to the ICU today leaves NO record of the move at all: somebody edits a ward label and the previous location is gone."* `transfer_patient` makes the move a first-class, audited event with from/to, timestamp, reason and named actor.

> ### ⚠️ A transfer is a permission change wearing a logistics change's clothes.
>
> Ward/department is not decoration in Orb, **it is the access boundary** (§1). Vigil's patient board, the nurse shift board, the Sage lane, Relay's case rooms, the search surface and every `/{patient_id}` route hang off `permissions.PATIENT_DEPARTMENT_SQL` vs `permissions.department_scope(user)`.

Three rules follow, all enforced and tested (`frontdesk_service.py:17-42`):

- **A. The derivation is not duplicated.** A transfer **UPDATEs** `admissions.department/ward/bed` on the patient's existing active admission. No second location column, no `current_ward` cache, no movement-derived scope. *"The movement row is a LEDGER, never a scope source."*
- **B. The admission is preserved.** Same admission id, `status` stays `'active'`, `patients.status` stays `'admitted'`. **This is the trap:** both columns have a `'transferred'` value in their CHECK constraint, and setting either one means *"this patient has LEFT"* to every board in the system.
- **C.** (see the file, the third rule continues at `:42`.)

### 16.9 Housekeeping: and the capacity number it deliberately does not touch

`server/services/housekeeping_service.py:1-10`. *"A discharged bed is not an available bed. It is a dirty bed."* The gap between a patient leaving and the next arriving is routinely measured in hours, was invisible to Orb, and gates capacity far more often than bed count does. The module makes it a first-class, timed, attributable object:

```
occupied → vacant_dirty → cleaning_in_progress → vacant_ready → occupied
```

> ### The most instructive refusal in the whole admin spine
>
> `housekeeping_service.py:16-41`. Orb already has a bed-availability number that a bed manager acts on: `forecast_service.get_bed_capacity_forecast` computes, per ward, `available = WARD_CAPACITY[ward] − count(patients with status 'admitted')`. `WARD_CAPACITY` is a per-ward **integer site setting** (`ORB_WARD_BEDS`), **there is no bed registry anywhere in Orb**; `patients.bed` and `admissions.bed` are free-text labels nothing validates.
>
> **This module does not touch that number, and the choice was deliberate.** The tempting move is to subtract dirty beds from `available` so the capacity screen tells the truth. *"It would make the capacity screen tell a NEW kind of lie."* `housekeeping_units` is a register a site fills in by hand, one bed at a time; on the day the module ships it contains **zero rows**. A ward with twelve beds and three registered would report "3 clean of 12", and subtracting `registered − ready` from a capacity computed on an entirely different basis produces a number that is neither the old one nor a correct one, silently, on a screen somebody uses to decide whether to accept a transfer.
>
> So the two live side by side and this module's job is to **make the relationship visible rather than hide it.** Turning `ORB_HOUSEKEEPING` off removes one tab and its routes and *"does NOT change any capacity or forecast number, because this module never fed one"* (`server/config.py:616-619`).

**The turnaround targets are site settings, not measurements.** `ORB_HK_STANDARD_CLEAN_MINUTES` 45 and `ORB_HK_TERMINAL_CLEAN_MINUTES` 120 (`server/config.py:635-636`). Two of them because *"a terminal clean after an isolation patient is a materially longer job than wiping down a bed space, contact time for the disinfectant, curtain and linen change, PPE on and off, and a board that expects both to take 45 minutes will report every isolation room as late."* Until a site has run the module for a few weeks and set its own, the panel labels them **"site setting"** (`:626-634`).

### 16.10 Workforce: roster, attendance, and the payroll boundary

`server/services/workforce_service.py:1-11`.

> **ORB OWNS THE ROSTER AND THE ATTENDANCE. THE PAYROLL PROVIDER OWNS THE MONEY.**
>
> *"There is no salary in this module. No PF, no ESI, no Professional Tax, no TDS."*

The same boundary is restated in the config file, *"because a config file is where somebody looks for a missing feature"*: those rules change with each Union and State budget and getting them wrong is a **statutory liability, not a bug**. Orb owns the facts only it can see, the roster and the attendance, and exports them (`server/config.py:645-656`).

Three thresholds, all site-settable, all documented as defaults rather than findings (`server/config.py:658-677`):

| Constant | Default | Note from the source |
|---|---|---|
| `WORKFORCE_OT_DAILY_MINUTES` | 480 (8 h) | *"a DEFAULT A SITE MUST REVIEW, not a legal finding."* Indian working-hours law for a hospital is **state** law, most hospitals sit under a State Shops & Establishments Act, a few under the Factories Act. Orb only **counts** minutes past a threshold the site sets; **it never prices them, and it never enforces a cap.** |
| `WORKFORCE_LATE_GRACE_MINUTES` | 10 | *"Purely a reporting courtesy, nothing in Orb acts on a late mark."* |
| `WORKFORCE_MAX_SPAN_MINUTES` | 960 (16 h) | Covers a double shift and a badly-timed handover. *"Anything longer is almost always a punch-out that was never recorded, and silently accepting it would put a fictional 30 hours of overtime into a payroll file."* |

### 16.11 Dietary: a hospital module by machinery, a clinical surface by content

`server/services/dietary_service.py:1-9`. Diet orders per admitted patient, ward meal plans, kitchen counts and indent, NPO handling.

> *"A hospital feeds people, and what it feeds them is part of their treatment. A diabetic's carbohydrate load, a renal patient's potassium and salt, a stroke patient's swallow-safe texture and a pre-operative patient's NIL BY MOUTH are clinical decisions that arrive at the bedside on a tray."*

**Four things it exists to get right** (`dietary_service.py:10-40`); two of them are safety controls a reader should know verbatim:

1. **The allergy on the tray card is the live one, or it says it could not be read.** `patient_allergies` is the coded source of truth this codebase already uses for order-time drug screening (`services/allergy_interlock.read_active_allergies`). This module **reads it and never copies it**, there is no allergy column on `diet_orders`, migration 25 does not create one, and `tests/test_dietary.py` **asserts its absence**, because a copy taken at ordering time is wrong the moment a nurse records a new reaction, and the tray card printed from it is confidently wrong.
   **The read is fail-closed, exactly as the drug interlock is:** if the allergy table cannot be read the row carries `allergies_unavailable: True` and `ALLERGY_UNAVAILABLE_MESSAGE`. **It never degrades to an empty list**, because an empty allergy list and an unreadable one render identically as *"no known allergies"* and mean opposite things.
2. **NPO is time-bounded, attributed, and never silently lifted.** *"A patient who is nil by mouth for a theatre list that then gets cancelled is the commonest harm in this domain, they go on not eating because the paper said NPO and nobody owned the sentence."* So `npo_until` is **required, enforced by a database CHECK** (migration 25) rather than by a validation the service could forget; the ordering clinician is on the row and surfaced by name on the ward view, so *"who said this patient cannot eat"* has an answer; and a reason is required, *"because an NPO nobody can review is an NPO nobody lifts."*

**What the switch means here, and it is different from the money modules** (`server/config.py:686-696`): turning dietary off does not make Orb hold diet orders it will not show, **it means Orb holds no diet orders at all** and the hospital's own kitchen system owns the whole question, which is the honest posture for a site that already runs one. *"What the switch can never do is hide an NPO order from a ward while the order still exists."*

### 16.12 Biomedical: the register that says whether a number can be believed

`server/services/biomedical_service.py:1-10`. Asset register, preventive-maintenance and **calibration** schedules, breakdown/downtime log, AMC/CMC contracts.

**Why it exists, in one paragraph** (`:10-20`):

> Orb ingests numbers off devices. An infusion pump that has drifted 12% delivers 88 ml where the chart says 100 and **Orb records 100**. An uncalibrated SpO2 monitor reads 94% where the truth is 88%, **Vigil sees 94%, and no alert fires.** The device is not broken, it is working perfectly and it is wrong, *"which is the failure mode no amount of software validation catches, because every layer above the sensor is doing its job correctly on a bad input."* The only defence is a maintained record of when each measuring device was last calibrated and when it is next due.

**The one definition everything turns on** (`:22-40`):

```
next_due_date = last_done_date + interval_days
A schedule is OVERDUE when   today >= next_due_date + grace_days
A CONTRACT  is EXPIRED when  today >  end_date
```

*"Read that second line twice, because the `>=` is the whole module."* A calibration done on 1 Jan with a 90-day interval is next due 1 April; the certificate covers 1 Jan–31 March. **On 1 April the cover has run out, not on 2 April.** `today > next_due_date` would call the device compliant for one more day, *"and 'one more day' is a whole day of readings from a device whose certificate has expired, on the exact day somebody might have caught it."*

**Why its switch is not like the other five** (`server/config.py:601-612`): billing, payments and procurement are things another system can do just as well. *"The calibration register is the record that says whether the number on a monitor can be believed, and Orb READS those numbers."* A site that switches this off is choosing to keep that record somewhere Orb cannot see, legitimate (most hospitals keep it in a file cabinet today), *"but it should be a decision somebody makes out loud rather than a default."*

Consequently the **overdue view is ward-visible** (`vitals.read`) while the register itself is administrative (`src/shellConfig.tsx:76-83`).

### 16.13 NABH: and the rule that an indicator with no data is not zero

`server/services/nabh_service.py:1-11`. The incident / adverse-event register, a quality-indicator dashboard computed from data Orb already holds, and a date-ranged evidence export.

**NABH accredits the hospital, not the software. Nothing in this file makes a hospital compliant.** What it does is remove the three weeks of file-pulling between *"show me your medication-error data for last quarter"* and an answer, *"and make the answer one nobody had to assemble by hand, which is the only kind that survives a second question."*

> ### **An indicator with insufficient data is not zero and it is not 100%.**
>
> `nabh_service.py:14-31`. *"A pack that flatters the hospital is worse than no pack. It is worse in the ordinary way (an assessor who catches one made-up number stops believing the other eight) and worse in the way that matters (a quality committee that reads '0 medication errors' stops looking for medication errors)."*

Every indicator carries a `state`, and **only `reported` may carry a number**:

| State | Meaning |
|---|---|
| `reported` | The number is real and the denominator supports it. |
| `insufficient_data` | The source exists but there is not enough of it. The computed value is retained under `detail.withheld_value` **for the audit trail** and is **not rendered**. |
| `not_collected` | Orb holds no source data for this indicator at this site. Distinct from `insufficient_data` because **the fix is different**. |

### 16.14 ABDM / ABHA: the module whose first line is what it does not do

> ### ⚠️ ORB HAS NO CONNECTION TO ANY ABDM GATEWAY.
>
> `server/services/abdm_service.py:5-11`, in a box in the source: *"There is no ABDM API client in Orb. No HIP callback URL, no HIU registration, no consent-manager subscription, no gateway token, no outbox, no retry queue, no sandbox credential. Orb has not been onboarded to the National Health Authority in any capacity, and nothing in this module changes that."*

**What it is:** the hospital's own record of ABDM work that happened somewhere else. A registration clerk keys an ABHA number off a card. Someone verifies it on the ABDM portal, in the ABHA app, or against Aadhaar OTP on whatever software *is* onboarded, and then records here that they did, **who, when, by which method.** A care-context link made on a HIP console is recorded here. A consent artefact issued by a consent manager has its terms recorded here (`:13-20`).

**Why that is worth building.** *"Because the record is the compliance artefact."* An NHA or NABH assessor asks *"show me that the ABHA numbers you hold were verified, by whom, and when"*, and a hospital with an `abha_number` column and no verification trail cannot answer. The rails also mean that the day Orb *is* onboarded, the schema, vocabulary and screens already exist full of real data; **what gets added is a transport, not a module** (`:22-28`).

**Why it is dangerous to get wrong** (`:30-40`), the failure mode is silent and lands on the patient:

- A desk that believes Orb linked the care context does not link it, and the patient's discharge summary never appears in their PHR, *"a fact nobody discovers for months, because there is no error and no empty screen."*
- A desk that believes an ABHA number is verified because Orb rendered it in confident black type attaches that number to the wrong human being, and *"a national health id pointed at the wrong patient is a durable, portable clinical error."*

So, **without exception**, `NO_GATEWAY_NOTICE` rides on every payload this module produces. The same statement is repeated in the config file at `server/config.py:594-597`: *"Orb transmits nothing to any ABDM gateway, these are records of steps performed elsewhere, and the module says so."*

> **For a CIO questionnaire:** the honest answer to "is Orb ABDM-integrated?" is **no**. It is ABDM-*ready* in the record-keeping sense described above. Do not let this module be cited as integration.

### 16.15 Where the schema lives

Migrations **20–26** and **28** create these modules' tables (§2.3.2): `hospital_admin_spine` (20), `biomedical_assets` (21), `housekeeping` (22), `abdm_abha_rails` (23), `workforce_attendance` (24), `dietary` (25), `nabh_evidence` (26), `patient_appointment_requests_bridge` (28). The individual tables are **not yet documented column-by-column** in §2.4, see §17.
