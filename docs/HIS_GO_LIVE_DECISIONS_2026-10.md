# Go-live safety decisions: Orb whole-hospital build

21 October 2026. For Samuel. Every item below waits for your yes, or for a looser choice, before go-live.

## What this is

The whole-hospital build (waves 1 to 15, all committed) put every new function behind a switch that is off by default. Each time an architect raised a safety ruling, the build chose the strictest option and wrote it down for you to confirm. This document collects every one of those items in one place.

The items come from the "Safety defaults to confirm" lists in `docs/his_design/build/contracts.md`, one list per wave, plus a few items the wave text leaves for you elsewhere. They were pulled out as 531 records and checked by a second reviewer against the source. Where the same decision repeats across waves (the same ruling and rule, sometimes applied at several doors behind different switches), the records are merged into one entry, and its Switch line lists each door's switch. That leaves **480 decisions**. **283 of them guard a patient directly.**

## How the switches work

- Every new module ships switched off. With a switch off, Orb behaves the way it did before the build, except for the Always on rules below.
- A decision here matters only when its module is switched on for a real hospital. A hospital that never turns on the lab module never meets the lab decisions.
- Some rules are always on, whatever the switches say: 44 entries say "Always on" on their Switch line. Most of them apply to any go-live, even with every switch off.
- Most multi-site rules have no switch of their own. They wake up only once a second facility is on record, which the facility create gate prevents today. While two facilities exist, every patient screen is shut anyway (entry 220). A first, single-site hospital can leave the Multi-site sections for later, except entries 403, 408, 415 and 417, which act on a single site too, and entry 221, which decides whether that two-site shutdown can fail open.

## How to use this document

1. Start with **Decide first**. Those are the decisions that guard a patient: wrong patient, wrong drug, allergy, dose, critical results, and who can open or act on a chart.
2. Each entry has the same lines. **What is built** is the strict default as it stands. **Looser option** is what you could choose instead, when the source names one. **If relaxed** is what the source says could go wrong. "Not stated in the source" means the build notes give no consequence; where there is a plain-words reading of what the rule guards, it follows. **Switch** and **Ruling** let an engineer find the code. **Source** gives the wave and the record ids.
3. Work module by module. Before a module goes live, say yes to each of its entries or pick the looser option.
4. About 30 entries run the other way: the build sits at the looser end and names a stricter option (entries 1, 5, 26, 33, 40, 70, 73, 76, 111, 112, 179, 210, 211, 221, 233, 238, 254, 256, 260, 261, 292, 298, 305, 340, 400, 402, 417, 423, 434 and 443). The Looser option line says so. They need a yes too.
5. "To confirm" inside an entry marks a specific question the builder left for you.
6. The appendix lists every record id and the entry it went into, so nothing is lost.

The "guards a patient" flag was a judgement made record by record. The checkers questioned some of them. After a second look at the source, and at the code where an entry says so, these were moved up into Decide first: entry 7 (registration at a hospital with inpatients), entry 35 (no allergy list on the consultation print), entry 48 (reopening a stay never restarts care bundles), entries 65 and 66 (who may record leave, which pauses a patient's clocks), entry 70 (the readmission date rule), entries 104 and 105 (no screen asks the pharmacy for a signed medicine), entry 112 (outpatient duplicates are only a notice in record mode), entry 113 (no allergy list on the printed prescription), entries 144 and 145 (the NDPS register's mode and opening), entry 149 (NDPS drugs are never ward stock), entry 153 (ward-returns screens name patients), entries 155 and 156 (crash-cart checks), entry 170 (sample collection time), entry 242 (roster import refusing a cross-site move), entry 266 (a restore erases every site's recent entries) and entry 281 (the practice tool refusing a live database). The checkers also questioned three that stay where they are: entry 419 (a booking handled only at its own site, marked not guarding), entry 428 (a dispense drawing only on the patient's own site's stock, marked not guarding, while entry 256 is marked guarding) and entry 249 (the discharged list, marked guarding). Entry 477 (the practice tool writing no vitals) was flagged by the extractor, not a checker, and stays marked as not guarding a patient because the tool only runs on copies. Move any of them if you disagree. A merged entry counts as guarding a patient if any of its records does.

## Before you switch anything on

The build notes name switches that must stay off, or rulings that hold go-live, whatever you decide below.

- `ORB_OPD`: wave 3 recommended keeping it off until the identity check runs on every visit. That check was built in wave 5 (entry 10, note on entry 2).
- `ORB_REGISTRATION` at a hospital with inpatients: wave 6 recommended keeping it off until conversion and readmission onto the same record were built. Both are built, but readmission is held off (next line). While it is off, a returning inpatient whom the duplicate check finds cannot be admitted onto their own record, and in block mode cannot be admitted at all. Keep `ORB_REGISTRATION` off at a hospital with inpatients until `ORB_READMISSION` can go on (entries 6 and 7).
- `ORB_READMISSION`: must stay off until 21 other lanes (nurse board, I-PASS, portal, Ward Record chart, vitals carry-forward and 16 more) read through the stay boundary. Until then, on a quick readmission, an earlier stay's data could show as current (entry 69). Some gaps from the last stay remain even after that: open escalations keep climbing, old orders stay on the Orders screen, and an old lab critical can post into the reopened case room (entry 69).
- `ORB_MED_ORDER_VERIFY_BEFORE_DOSE`: must stay off until `medorders-5a` lands. With it on today, ward-stock and patient's-own medicines could never be given (they have no way to be verified), and every adopted, unverified live order waits for a pharmacist (entry 119). With it off, as it ships, a nurse can give a dose of any order not charted by AI before a pharmacist has checked it.
- `ORB_MED_ORDER_LIFECYCLE`: until `medorders-6a` lands, a doctor cannot stop, hold or cancel a signed CPOE or order-set medicine in Orb (wave 7 deferred; entry 90). Once an order is linked, turning the switch off does not give the old Orders screen back for it (entry 99). There is also no screen yet to ask the pharmacy for a signed medicine (wave 7 deferred; entries 104 and 105). Keep the lifecycle off until 6a lands, or rule on how a doctor stops a drug and how the pharmacy hears of a new one.
- Laboratory: the lab setup screens and order intake with sample numbers (`lis-4`, `lis-6`) wait on `LIS-R1`, `LIS-R2` and `LIS-R18` (entries 160, 161 and 369). Sample numbering waits on `LIS-R13` (entry 162). Result entry with high and low flags (`lis-10`) waits on `LIS-R2` and `LIS-R7` (entries 161 and 173). The unmatched results queue waits on `LIS-R17` (entry 165). Critical posting waits on `LIS-R20` (entry 199). The ward's critical card with in-app read-back (`lis-16`) and critical alerts from outside labs (`lis-22`) wait on `LIS-R20`, `LIS-R3`, `LIS-R1` and `LIS-R22`.
- Laboratory set-up: with `ORB_LAB` on, nothing is flagged critical, high, low or as a big change, and no sample is checked for stability, until the lab approves its critical limits, reference ranges, stability times and delta thresholds (entries 160, 161, 181 and 189). Until a ladder is written, every critical is due at once and the screen says phone the ward (entry 197).
- `ORB_LAB_CRITICAL_INTERFACED`: three gaps guard a patient (`W8-G`, see Also open). An outside lab's critical that arrives for an unmatched patient never raises an alert, even after lab staff attach it to the right patient. A correction of an open critical to a normal value is not posted to the ward at once. A raise that fails after the result is saved is never tried again.
- `ORB_PHARMACY_NDPS_REGISTER`: it stays off unless `ORB_PHARMACY_NDPS_ON_FAILURE` is also set, and that ships with no mode, so you must choose 'block' or 'record' (entries 144 and 347). Before switching it on, create the stores, adopt the old stock and record a witnessed opening for every controlled drug already on the shelf (entry 145, wave 7 deferred). In 'block' mode, until then, the pharmacy cannot dispense that stock, so a patient's controlled painkiller can be refused.
- `ORB_PHARMACY_MAX_DISPENSE_UNITS`: keep it at 1 until billing charges units times MRP and a dispense of several units can be traced to each dose given (entry 354). Wave 10 built billing at MRP (entries 400 and 401); check it before raising.
- Multi-site: once a second facility is on record, 19 shut lanes would show another site's patients if the fence were lifted today (entry 229). In the 20 October rehearsal the Sage insights list showed facility 1 patients to the facility 2 administrator; the fence keeps it shut (entry 283). Do not lift the fence until these are fixed.
- Multi-site: once a second facility exists, the bedside monitor feed files no vitals until `site.toml` lists its `ORB_MONITOR_SOURCE_ID` (entry 259). Do it as part of go-live.

## Words used

- **ADT**: admission, discharge and transfer (beds, moves, end of stay).
- **UHID** and **MRN**: the hospital's patient number. **ABHA**: the national health ID; **ABDM**: the national digital health mission that issues it.
- **LAMA** and **DAMA**: left, or discharged, against medical advice.
- **NDPS**: narcotic and psychotropic (controlled) drugs. **NSQ**: not of standard quality. **MRP**: the printed maximum retail price. **GRN**: goods receipt.
- **eMAR** or **MAR**: the bedside medication record. **CPOE**: doctors' order entry. **PRN**: as needed. **STAT**: give now.
- **HH**, **LL**: critically high, critically low. **H**, **L**: above or below the reference range. **Delta check**: a big change from the patient's last result.
- **NABH**: the Indian hospital accreditation body; **QI-** codes are its quality indicators.
- **Helix**: Orb's pharmacy desk. **Sage**: the AI assistant. **Scribe**: dictation to chart. **Relay**: case-room chat. **Vigil**: the ward board. **Bridge**: the patient portal. **Lens**: document reading.
- **Record mode** and **lifecycle**: the medication order store can only record orders (record mode), or also run sign, hold and stop (`ORB_MED_ORDER_LIFECYCLE`).
- **Facility**: one hospital site on a shared Orb box. **Group-wide**: an account that reaches every facility.
- **Audited** or **logged**: a row is written to the audit log. **Fail-closed**: if that row cannot be written, the action is refused. **Fail-soft**: the action goes ahead even if its audit row cannot be written.
- **401, 403, 404, 409, 422, 428, 429, 503**: the numbers Orb's server gives when it refuses. 401: sign in again. 403: not allowed. 404: answers 'not found', as if the record did not exist. 409: refused because of the record's current state. 422: the request has a missing or wrong field. 428: needs an acknowledgement first. 429: too many tries, locked for a while. 503: this screen is shut, or the audit log could not be written.
- **Rule C**: a patient can only be moved into a department that has clinical cover (at least one active doctor or nurse account in it); otherwise the move is refused.
- **Terminal clean**: the deep clean a bed needs after an isolation patient leaves it, or in an isolation room, before the next patient.
- **CfC**: Orb's learned risk model, which can raise a patient's Vigil risk level. **I-PASS**: the structured handover sheet between shifts.
- **Par**: the stock level a ward keeps of a drug. **Imprest**: the ward's standing stock, topped up to par.
- **PAN**: the company's tax number. **GSTIN**: its GST registration number. **GST** and **IGST**: goods and services tax, and the interstate form of it.
- **FHIR**: the standard format for exporting a health record. **HFR**: the national registry of health facilities.

## Summary

| Module | Decisions | Guard a patient | Records |
|---|---:|---:|---:|
| OPD and registration | 61 | 39 | 75 |
| Beds and ADT | 59 | 40 | 72 |
| Medication orders | 57 | 50 | 65 |
| Pharmacy store | 67 | 30 | 73 |
| Laboratory | 73 | 55 | 80 |
| Patient portal | 9 | 4 | 9 |
| Billing and money | 10 | 0 | 10 |
| Platform and audit | 1 | 1 | 1 |
| Multi-site: set-up, fence and staff accounts | 26 | 10 | 27 |
| Multi-site: patient access, registration and OPD | 14 | 9 | 14 |
| Multi-site: beds, admissions and imports | 7 | 5 | 8 |
| Multi-site: Sage, Relay, surgery, paging and live screens | 13 | 11 | 13 |
| Multi-site: medication orders, pharmacy and stores | 12 | 4 | 12 |
| Multi-site: laboratory and device feeds | 8 | 6 | 8 |
| Multi-site: billing and money | 14 | 0 | 14 |
| Multi-site: audit, backups, downtime and exports | 19 | 7 | 19 |
| Multi-site: NABH, Command Center, admin and support services | 15 | 2 | 16 |
| Multi-site: visiting consultants and sign-in | 7 | 7 | 7 |
| Multi-site: practice facility tool and two-site rehearsal | 6 | 3 | 6 |
| Post-build fixes (wave 15) | 2 | 0 | 2 |
| **Total** | **480** | **283** | **531** |

## Decide first: decisions that guard a patient

Entries 1 to 283. Grouped by module.

### OPD and registration

**1. A doctor who registers a visit opens the record to their clinic**

- **What is built:** When a doctor or clinician (anyone holding `opd.consult`) registers an outpatient visit themselves, the patient's record opens to that doctor's clinic. The visit is marked self-registered (`scope_self_registered=1`) and the opening is written to the audit log (`opd_scope_opened`). The usual visit window still applies (see entry 3), so a visit dated in the future opens nothing until its day.
- **Looser option:** The source names one other choice, (y): a visit opens the record only after someone else checks the patient in. The source does not say which is stricter. Our reading: (y) is the stricter of the two, so this entry runs the other way. As built, a doctor opens a patient's record just by registering a visit for them.
- **If relaxed:** Not stated in the source. In plain words: as built, a doctor can open a record by registering a visit for that patient; the flag and audit row are the check.
- **Switch:** `ORB_OPD`
- **Ruling:** `OPD-N2`
- **Source:** Wave 3; record W3-1.

**2. OPD visits attached by internal id only, before the identity check**

- **What is built:** Until the two-identifier check (`opd-4` / `opd-6a`) is built, the desk attaches a visit using only the internal patient id, so any patient who is not currently admitted can be registered into the caller's clinic. To limit the harm, the OPD record never shows date of birth, phone, address or next of kin. The build recommends keeping `ORB_OPD` off until `opd-4` / `opd-6a` land.
- **Looser option:** None named. Switching `ORB_OPD` on before `opd-4` / `opd-6a` would go against the recommendation.
- **If relaxed:** Any patient with no active admission can be registered into the caller's clinic, which opens that patient's record to the clinic.
- **Switch:** `ORB_OPD`
- **Ruling:** `OPD-S1`
- **Note:** The check this waits for has since been built: the identity check itself in wave 4, and its use on every visit (`opd-6a`) in wave 5. Both are in entry 10. Confirm that before switching `ORB_OPD` on.
- **Source:** Wave 3; record W3-2.

**3. Limits on a clinic's view of an outpatient's record**

- **What is built:** A clinic's view of an outpatient's record through an OPD visit: any active admission anywhere closes it; a visit booked for a future date opens nothing until that day; the window runs from 7 days before today to today; other clinics' chief complaints are hidden; an empty allergy list shows as 'none recorded', never as 'no known allergies'; if the access audit row cannot be written, the visit is undone and the desk gets an error.
- **Looser option:** None named. The 7-day window is a setting, `ORB_OPD_SCOPE_DAYS`.
- **If relaxed:** Not stated in the source. In plain words: the rules guard against an empty allergy list being read as 'no known allergies' and against record access that is unaudited or stays open.
- **Switch:** `ORB_OPD`, `ORB_OPD_SCOPE_DAYS`
- **Ruling:** None (epic `opd-7`)
- **Source:** Wave 3; record W3-3.

**4. A reused request key from another patient's visit is refused**

- **What is built:** If the desk resends a registration with a repeat key that belongs to a visit for a different patient or clinic, it is refused with an error that reveals nothing about the other visit, undone and audited.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: the rule stops one patient's or one clinic's visit being handed back to the wrong desk.
- **Switch:** `ORB_OPD`
- **Source:** Wave 3; record W3-4.

**5. With OPD off, a reused request key can return another clinic's visit**

- **What is built:** Not made strict: left unchanged because behaviour with the switch off must not change. With OPD off, the front desk looks up repeat keys across the whole hospital, so a key reused in another clinic returns that clinic's visit, and the audit is written under the requesting patient's id.
- **Looser option:** This is the looser state today. The stricter fix you could choose is the `on_replay` mechanism, which the source says would take a few lines.
- **If relaxed:** Stated for the current state: a key reused across clinics returns the other clinic's visit and audits under the wrong patient's id.
- **Switch:** Applies while `ORB_OPD` is off (the flag-off front desk)
- **Source:** Wave 3; record W3-31.

**6. The duplicate check blocks a probable match, at the desk and at admission**

- **What is built:** When staff register or admit someone who looks like an existing patient, a probable match (score 70 or more) is refused with no way past it, and a possible match (50 to 69) goes ahead only with a reviewed, written reason. At registration, a UHID already on file (exactly, or once capitals and separators are ignored) or an active ABHA is always refused, in either mode. At the admission doors the same hospital number is refused in every mode with no override. There is no ABHA check at the doors: neither door has an ABHA field yet (wave 6 deferred, `W6-B`). On the registration screen (wave 5) a possible duplicate needs a reason of at least the minimum length; a probable match, the same UHID or ABHA, or an incomplete check offers only 'This is the patient' (which leads to the identity check), with no override. At the admission doors (wave 6) the desk's full rule applies: in block mode a probable match, or a cut-short search that could hide one, is refused with no override; a possible match, warn-mode probables and a cut-short search that could hide only a possible match need a duplicate review naming every candidate with a reason of at least 8 characters (audited, each one goes on a worklist). Consequence stated in wave 6: until `opd-17a` and `opd-17c` land, a patient the index finds cannot be admitted onto their own record, and in block mode a probable match cannot be admitted at all.
- **Looser option:** `ORB_DUPLICATE_MODE` 'warn': probable and possible matches both go ahead after a review (the UHID refusal stays everywhere; the ABHA refusal stays at registration). The wave 6 notes also record that the design's narrower door rule was replaced by this full rule as the strictest option.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_DUPLICATE_MODE` (ships 'block'; works only with `ORB_REGISTRATION` on)
- **Ruling:** `OPD-S2`
- **Note:** `opd-17a` (wave 7 conversion, `W7-B`) and `opd-17c` (wave 8 readmission, `W8-A`) are built, but readmission sits behind `ORB_READMISSION`, which must stay off (entry 69). While it is off, the wave 6 consequence still holds for anyone who was an inpatient before: the duplicate check finds them, they cannot be admitted onto their own record, and in block mode a probable match cannot be admitted at all. With `ORB_OPD` also off, no patient the check finds can be admitted onto their own record. Entry 7 is the recommendation from the same bullet.
- **Source:** Waves 4, 5 and 6; records W4-1, W5-8 and W6-12.

**7. Keep registration off at inpatient hospitals until readmission onto the same record**

- **What is built:** Recommendation in the same bullet: keep the registration switch off at hospitals with inpatients until `W7-B` and `W8-A` land (admitting an existing patient onto their own record).
- **Looser option:** Switch `ORB_REGISTRATION` on at an inpatient site before `W7-B` and `W8-A`.
- **If relaxed:** Stated: with it on, a patient the index finds cannot be admitted onto their own record, and in block mode a probable match cannot be admitted at all.
- **Switch:** `ORB_REGISTRATION`
- **Ruling:** `OPD-S2` (recommendation)
- **Note:** Both are built, but `ORB_READMISSION` must stay off (entry 69), and while it is off a returning inpatient still meets this refusal. So in practice the recommendation still stands: keep `ORB_REGISTRATION` off at a hospital with inpatients until `ORB_READMISSION` can go on.
- **Source:** Wave 6; record W6-13.

**8. Duplicate search stops at 500 records and refuses if it may have missed one**

- **What is built:** The duplicate search looks at no more than 500 records. In block mode, if a record it had to leave out could be a probable duplicate, registration is refused (409 `duplicate_check_incomplete`) whatever review is given; a possible one needs a reviewed override. In warn mode a review is always needed, even when no candidate is shown. A cut that could not reach any band goes ahead and is audited. Cost to confirm: in block mode, a desk that types a helpline number shared by more than 500 records, for a patient whose name shares a distinctive word with them, cannot register that patient until it leaves the helpline out.
- **Looser option:** 'warn' mode: a review is always needed but the incomplete check never blocks outright.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_DUPLICATE_MODE`; `patient_matching.MAX_CANDIDATE_ROWS` (500)
- **Ruling:** `OPD-S2`
- **Source:** Wave 4; record W4-2.

**9. Department-limited desks: a typed date of birth can block registration in big name groups**

- **What is built:** For a desk limited to one department, in block mode, the scan's upper estimate for every record found by phone or exact name adds a flat 30 points for a date of birth the desk cannot see. Once more than 500 records share the patient's exact name (or the helpline), any registration that types a date of birth is refused (409 `duplicate_check_incomplete`) and no review can override it, while the same patient entered with a stated age registers after a review. The source calls this fail-closed, not unsafe, but it pushes the desk to record an age instead of a known date of birth.
- **Looser option:** Add 30 only where the index card's age could equal the age typed, else 10.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_DUPLICATE_MODE` ('block'), department-limited desk accounts
- **Ruling:** `OPD-S2`
- **Source:** Wave 4; record W4-3.

**10. Stated identity check before every visit, check-in, conversion and readmission**

- **What is built:** Staff must state the patient's name, the date of birth or age, and the last 4 phone digits or the UHID, and Orb checks them against the record. The check only ever answers 'match' or 'no match', never shows the stored date of birth or phone, and is audited (no audit row, no check). 5 failures per user per patient per hour locks it (429). A user limited to one ward cannot check another department's inpatient (404, audited). Wave 5 applies it to every OPD visit opened at the front desk, for everyone, including the administrator, the doctor's walk-in and a patient registered a moment before: no check is 409 `identity_check_required` and a mismatch is 409 `identity_not_confirmed`. Wave 7 adds every appointment check-in and every outpatient-to-inpatient conversion. Wave 8 adds readmission through either door. A missing or failed check is refused, audited, and nothing is saved.
- **Looser option:** The spec's version for OPD visits (wave 5): check only patients found by search. No looser option is named for check-in, conversion or readmission.
- **If relaxed:** Not stated in these records. Entry 2 gives the risk of having no check: any patient with no active admission can be registered into the caller's clinic, which opens that patient's record to the clinic.
- **Switch:** `ORB_REGISTRATION` (`ORB_OPD` needs it) for the check and OPD visits; `ORB_OPD` for check-in and conversion; `ORB_READMISSION` for readmission.
- **Ruling:** `OPD-S1` (a)
- **Source:** Waves 4, 5, 7 and 8; records W4-4, W5-1, W7-1, W7-8 and W8-1.

**11. The front desk never records allergies**

- **What is built:** Registration never writes allergy entries, and the front desk has no allergy or 'no known drug allergies' field. Every registration form refuses fields it does not expect, so sending allergies, 'no known drug allergies' or a full identity document number is refused (422). Allergies are recorded only by someone holding `opd.consult`; the receptionist and the administrator get 403.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION`
- **Ruling:** `OPD-S5` (a)
- **Source:** Waves 4 and 5; records W4-5 and W5-2.

**12. OPD-only access hides contact and identity details**

- **What is built:** Someone who can see a patient only because of an OPD visit sees no date of birth, full phone number, address, emergency contact, guardian, email, identity document or ABHA, and cannot correct them (403 `needs_full_scope`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION` (OPD-visit access arm, with `ORB_OPD`)
- **Source:** Wave 4; record W4-6.

**13. Who may register patients and correct their details**

- **What is built:** Registering patients, correcting their details and resolving duplicates need the `registration.write` permission. Correcting a UHID needs `admin.overview`: in the patient details drawer only the administrator can correct a UHID, and the administrator corrects nothing else and cannot register patients. An identity document is kept as its type plus last 4 characters only. There is no allergy field. Every change needs a reason of at least 8 characters. If someone else changed the record meanwhile, the screen shows the conflict and reloads. Staff who reach the patient only through an outpatient visit cannot see or correct date of birth, contact, address, next of kin or identity document. The server enforces all of it.
- **Looser option:** The front-desk write rule (`require_registration_desk('write')`), which would also let the administrator in through `frontdesk.write` (wave 4 code notes).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION`
- **Ruling:** `OPD-N3` (wave 4); `OPD-N3`, `OPD-N10` and `OPD-S5` (wave 6)
- **Source:** Waves 4 and 6; records W4-7 and W6-11.

**14. UHID rules: the hospital's own number or Orb's series, never guessed**

- **What is built:** Hospital mode: an admission must carry the hospital's own typed UHID, and Orb never makes one up. Orb mode: a typed number is refused, and Orb issues the number as part of the registration or admission. Until the number series is set up, every registration and every admission is refused. An invalid `ORB_UHID_PATTERN` refuses every registration and stops every admission. A hospital number that differs only in capitals or separators counts as the same number.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_UHID_SOURCE` with `ORB_UHID_PATTERN`, under `ORB_REGISTRATION`
- **Ruling:** `OPD-N1`
- **Source:** Waves 4 and 6; records W4-8 and W6-16.

**15. Duplicate refusals are audited, and an ABHA is always marked unverified**

- **What is built:** A refusal that shows the desk other patients' records is audited, and if that audit row cannot be written the desk gets 503 instead. An ABHA sent while ABDM is off is refused (422). An ABHA recorded at registration is always marked unverified.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION` (ABHA part also needs ABDM on)
- **Source:** Wave 4; record W4-9.

**16. A hidden date of birth scores as a full match for a same-age candidate**

- **What is built:** When a candidate record belongs to another department and its date of birth is hidden from this desk, a matching age scores as a full date-of-birth match (+30 points, reason '`dob_hidden`').
- **Looser option:** Score it +10 instead of +30.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION` (department-limited desk)
- **Source:** Wave 4; record W4-10.

**17. Department-limited desks are not shown candidates found only by date of birth**

- **What is built:** A desk limited to one department is not shown candidates found only by date of birth; the administrator's hospital-wide duplicate sweep still pairs them.
- **Looser option:** None named.
- **If relaxed:** The source says the prompt would reveal the other patient's date of birth to that desk.
- **Switch:** `ORB_REGISTRATION` (department-limited desk)
- **Source:** Wave 4; record W4-11.

**18. An OPD note signs only after an allergy review**

- **What is built:** An OPD note signs only after an allergy review on this visit (any answer), or an earlier review that said 'allergies recorded' or 'none known'. An earlier 'unable to obtain' never counts; 'none known' stops counting once an allergy becomes active; an allergy on the chart with no review no longer lets the note sign. Otherwise the answer is 409 `allergy_review_required`, and the visit can still be completed with a written `OPD-N8` reason. This replaced wave 4's interim rule. In the consultation editor (wave 6) the Sign button stays off until the review allows it, and the server refuses a signature without it.
- **Looser option:** Wave 4's interim rule: any active allergy entry on the chart lets the note sign.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Ruling:** `OPD-S7` (a), `OPD-N8`; the editor item also cites `OPD-S3` and `OPD-S6`
- **Source:** Waves 4, 5 and 6; records W4-43, W5-3 and W6-9.

**19. Outpatient vital signs never reach ward vitals or risk scores**

- **What is built:** Vital signs taken in an OPD consultation stay inside that consultation. They are never written to the ward vitals or risk scores, never scored, coloured or put on the timeline, and never prompt anyone (held by an automated code check and a behaviour test). When an outpatient is admitted (wave 7), nothing from outpatients goes into the inpatient vitals or risk scores, and the admission door takes no observations. Outpatient values are shown on the form with their time, for reference only. In the consultation editor (wave 6) they show as plain values with their time: no colour, score band or prompt (record W6-9, filed under entry 18).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Ruling:** `OPD-S3` (a), `OPD-S6` (a)
- **Source:** Waves 4 and 7; records W4-44 and W7-9.

**20. OPD writes stop once the patient is admitted**

- **What is built:** Once the patient has any active admission, OPD writes on a visit are refused (409 `patient_admitted`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 4; record W4-45.

**21. Only doctors run the OPD queue; the administrator is refused**

- **What is built:** Calling, starting, completing, skipping, recalling and taking a patient need `opd.consult`; the administrator gets a refusal (403, audited) while OPD is on (`opd-8`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 4; record W4-48.

**22. A clinic can read its own visits' consultations and tests, whatever the date**

- **What is built:** A consultation opened through a visit is limited by that visit's clinic, not the 7-day OPD window, and stays readable after the patient is admitted elsewhere; reads by patient use the 7-day window. The clinic can also read and print the tests of any visit of its own clinic, whatever the date. Every test read writes an access record first; if that record cannot be written, nothing is shown. The source asks you to decide these two together.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Ruling:** None (wave 6 says decide with the wave 4 visit-read item)
- **Source:** Waves 4 and 6; records W4-50 and W6-7.

**23. OPD queue order puts emergencies first**

- **What is built:** The OPD queue puts emergencies first, then other non-normal categories (no invented order among them), then normal patients; by token number within each rank; skipped patients go after the waiting line; any non-normal priority needs a reason.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Ruling:** `OPD-N9`
- **Source:** Wave 4; record W4-51.

**24. Only a consulting doctor of the clinic can take a patient**

- **What is built:** Only an active consulting doctor of the visit's clinic can take a patient ('assign to me'); the patient keeps their token number unless the doctor's own series already holds it, in which case a new token is issued and audited.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 4; record W4-53.

**25. A taken token number is never reused that day**

- **What is built:** After a doctor takes a patient, that patient's number is never given out again that day in the unassigned line, and a take never lands on a number the doctor's line already issued that day (cancelled and left-without-being-seen included); a take with no audit row is refused (503). The unassigned line can skip a number.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 4; record W4-54.

**26. No follow-up prompt when a desk overrides an incomplete duplicate check**

- **What is built:** Not built. When the desk overrides a duplicate check that could not finish (the scan stops at 500 records), the override is audited, marked `check_incomplete`, but no worklist item is opened, so nobody is prompted to check later whether the new record duplicates one the scan never scored. The duplicate worklist (`patient_duplicate_candidates`) holds pairs of records, and the administrator's sweep skips any group of more than 25 records sharing one key (`SWEEP_MAX_GROUP`), so a crowd sharing a helpline number is never paired. The source says this needs a schema change and a product decision. Listed under Wave 4 deferred (`W4-A`), not in the safety list; Wave 5 deferred lists the same item again for the desk screen (Wave 5 deferred, `W5-A`: the `OPD-S2` (a) worklist item for a duplicate overridden at the desk).
- **Looser option:** The built state is already the loose one; the stricter option, a worklist item that prompts someone to follow up each override of an incomplete check, is not built.
- **If relaxed:** Stated gap: an override of an incomplete duplicate check is audited, but nobody is prompted to follow it up.
- **Switch:** `ORB_REGISTRATION` and `ORB_DUPLICATE_MODE`; no switch for the follow-up item, which is not built
- **Ruling:** `OPD-S2`
- **Source:** Wave 4; record W4-56.

**27. Allergy review answers must agree with the chart**

- **What is built:** 'No known allergies' is refused while any allergy is active (409 `allergies_on_chart`); 'Unable to obtain' needs a reason and records no allergy; an allergen named again that is inactive on the chart is switched back on in the same audited step; 'nkda' or 'nil' is never recorded as an allergen; the review and the allergies save together with their audit row, or not at all.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Ruling:** `OPD-S7`
- **Source:** Wave 5; record W5-4.

**28. An allergy review can be recorded only on a current visit**

- **What is built:** An allergy review can be recorded only on a visit that itself falls inside the OPD window; another recent visit at the same clinic is not enough; a future-dated or undated visit is refused (409 `visit_not_current`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`; `ORB_OPD_SCOPE_DAYS`
- **Source:** Wave 5; record W5-5.

**29. Visit-addressed answers show only the review status**

- **What is built:** An answer addressed to a visit says only the review's status, its label and when it was done: no allergen, note, reason, clinic, visit id or author.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 5; record W5-6.

**30. Outpatient doctors can order only lab tests and imaging**

- **What is built:** From the outpatient consultation a doctor can order only laboratory tests and imaging. Medicines (kept for `opd-12` behind the allergy review), procedures, nursing orders and bedside tests (ABG, VBG, ketones and any test done at the ward bedside) are refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Ruling:** None (builder's default; cites `OPD-S7` and `LIS-R18`)
- **Source:** Wave 6; record W6-1.

**31. The server, not the screen, decides what an outpatient order is**

- **What is built:** The screen sends only a test code and a priority. The server fills in what kind of order it is and its wording from the terminology or the lab catalogue, so no screen can slip in a medicine, free text or a dose.
- **Looser option:** None named.
- **If relaxed:** The source states the purpose: without it a screen could send a medicine, free text or a dose as an outpatient investigation.
- **Switch:** `ORB_OPD`
- **Source:** Wave 6; record W6-3.

**32. Which visits can take an outpatient test order**

- **What is built:** A test can be ordered only on a visit of the user's own clinic, inside the outpatient time window, not cancelled, not finished, for a patient not admitted anywhere (checked again at the moment of writing), and only if the same test is not already live on that visit.
- **Looser option:** Allow same-day orders after the visit is completed.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 6; record W6-4.

**33. Scribe quick-admit can send the same duplicate review**

- **What is built:** Scribe quick-admit follows exactly the admission form's duplicate rule and may send the same duplicate review (through the API only).
- **Looser option:** None named. The named alternative is stricter: Scribe never overrides.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION` (with `ORB_DUPLICATE_MODE`)
- **Ruling:** `OPD-S2` (by reference: the form's rule)
- **Source:** Wave 6; record W6-14.

**34. Duplicate cards show minimal details, only to registration staff**

- **What is built:** When a possible duplicate is found, only minimal cards are shown (UHID, name, sex, age, last 4 of phone, last visit, inpatient now; never a date of birth), and only to staff with registration read rights; anyone else sees a count. A department-limited user's typed date of birth is never compared with a record outside their scope. Every refusal is audited in its own write; if that fails, the answer is 503 and nobody is shown.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION`
- **Ruling:** `OPD-N4`, `OPD-S1`
- **Source:** Wave 6; record W6-15.

**35. What OPD prints show and never show**

- **What is built:** A consultation prints only once signed; a test slip needs at least one live order. Prints carry name, UHID, age, sex, the record's own content and the doctor's name with registration number ('Registration number not on file' when missing), and never an allergy list, date of birth, phone, address, user id, score or band. Every print writes an access record or is refused. The token slip reprints what is on screen and is not audited separately.
- **Looser option:** None named in the bullet. Your choice, from wave 6 deferred and again in wave 8 deferred for the printed prescription: put the patient's allergies on the consultation print. It needs a print addressed to the patient behind the OPD access rule.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Note:** This is an allergy decision. As built, a patient who takes this print to an outside chemist or clinic carries no allergy list on it. Whether allergens should print is your call (see also entry 113).
- **Source:** Wave 6; record W6-8.

**36. Desk bookings follow the outpatient scope rule**

- **What is built:** When desk staff book for a named patient, the outpatient scope rule applies: a clinic desk books only a patient on its wards or one with no active admission seen in its clinic within `ORB_OPD_SCOPE_DAYS` (otherwise 'not found'; the central desk books the rest); a departmental desk books only into its own clinic.
- **Looser option:** The front desk's `opd_registration_allowed` rule.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD` (window `ORB_OPD_SCOPE_DAYS`)
- **Ruling:** `OPD-N2`
- **Source:** Wave 7; record W7-2.

**37. A reused booking key must match the stored booking**

- **What is built:** In OPD mode a repeated booking with the same request key is answered only if it matches the stored booking's patient, doctor, day, time, clinic, origin and predecessor; anything else is refused without describing the other booking, nothing is saved, and it is audited. Type and reason are not checked. With the switch off, the old staff and patient booking paths still return whatever booking a key names; left as is because switch-off behaviour must not change; the Bridge sends no key.
- **Looser option:** None named.
- **If relaxed:** Stated for the switch-off path: a reused key returns the named booking's patient id, date, time, type, reason and department.
- **Switch:** `ORB_OPD` (flag-off path left unchanged)
- **Ruling:** None (a follow-up ruling is asked for the flag-off path)
- **Source:** Wave 7; record W7-6.

**38. A refused conversion preview shows only the visit reference**

- **What is built:** When an outpatient visit cannot be converted to an admission, the preview shows only the visit reference (id, date, clinic, doctor, token, status) and why, with no patient id, identity, allergies, medicines, observations or note, even for hospital-wide staff. It is still audited as an access to that patient.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 7; record W7-7.

**39. Only today's open visit in the user's clinic can be converted**

- **What is built:** Only today's open visit (waiting or with the doctor) in the user's own clinic can be converted. A patient who was an inpatient before is refused (`readmission_not_available`) while `ORB_READMISSION` is off. Readmission is built (`opd-17c`, `W8-A`) but held off; see entry 69. Allergies can only be added at this door, never removed.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 7; record W7-12.

Also behind `ORB_OPD`, filed under Medication orders: entry 94 (outpatient prescriptions never become inpatient medicines on their own). Entries 107 to 113 apply once `ORB_MED_ORDER_STORE` is also on.

### Beds and ADT

**40. No shared beds**

- **What is built:** A bed another admitted patient holds is refused (409 `bed_occupied`), whether picked or typed, at admission, on a move, and at every moment of a bed swap. The database refuses it underneath too, and nothing is written. For overflow, 'Add an extra bed' creates a temporary labelled bed (12A to 12Z) in the same room and class, already occupied, with a bed record entry and a facilities audit, in one step. No shared-bed reason field was built. Open point: anyone who may place a patient can add an extra bed, without the `bed.master` permission (to confirm).
- **Looser option:** A shared bed with a recorded reason (`bed_shared_reason`, not built, wave 5 deferred). On the open point, the stricter choice would limit 'Add an extra bed' to `bed.master` holders.
- **If relaxed:** Not stated in the source. In plain words: the rule stops two patients being recorded in one bed, so a lookup by bed finds one patient.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S1` (a)
- **Source:** Waves 3, 5, 6 and 7; records W3-5, W5-9, W6-21 and W7-15.

**41. Death, LAMA, DAMA and absconded ends are doctor-only, with the MRN typed back**

- **What is built:** Only a doctor allowed to write discharges (`discharge.generate`), and only for patients in their department, can end a stay as death, LAMA, DAMA or absconded. The End of stay screen, its bed-board action and 'Recorded in error' appear only for those doctors. Every end type, every reopen and a DAMA on the discharge screen need the MRN typed from the wristband; with no MRN on file it cannot be done there. LAMA, DAMA and absconded need a typed reason. An end time more than 72 hours ago, in the future, or before the admission is refused. LAMA, absconded and death cannot go through the doctor's ordinary discharge button. The server checks everything again.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: the rules guard against ending the wrong patient's stay, and against deaths or absconding recorded without a reason, by the wrong person, or at an impossible time.
- **Switch:** `ORB_ADT`; the 72-hour limit is `ORB_ADT_BACKTIME_HOURS` (default 72)
- **Ruling:** `ADT-S3`
- **Source:** Waves 3 and 5; records W3-6 and W5-18.

**42. Placing a boarder needs an explicit acknowledgement**

- **What is built:** A boarder is a patient in a bed on another department's ward. Placing a boarder, moving a patient onto another department's ward, or changing department while keeping a bed on another department's ward needs an explicit acknowledgement (`boarder_acknowledged`). Without it the answer is 409, with words naming who will not be able to open the chart. The acknowledgement writes a '`boarder_placed`' access audit and one line in the patient's case room. Scribe cannot acknowledge. The front desk's route needs the same. Who can see the patient's record does not change.
- **Looser option:** None named. Applying `ADT-S6` (host-ward nurse access for boarders) is held, per wave 6 deferred.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S6` (not applied)
- **Source:** Waves 3, 5 and 6; records W3-7, W5-15 and W6-20.

**43. Cleaning and out-of-service checks before a bed is used**

- **What is built:** At admission: putting a patient into a bed marked out of service, or linking a stay to one, needs a typed reason, which is recorded. Retired or switched-off beds and wards are refused. On a ward where cleaning is tracked, a bed that needs or is under a standard clean needs a typed reason. A bed due a terminal clean (an open terminal task, an isolation bed or room, or a patient on isolation precautions has left it since its last clean) needs an authoriser who acts, plus a reason. An out-of-service bed needs its own reason and never hides a terminal clean. A reserved bed is refused. Wards without cleaning tracking raise nothing, and recording where a patient already lies is not refused. On a bed correction: the same rule applies when the correction names a different bed, and a correction when no bed was recorded is refused (409 `no_bed_to_correct`). On a move or transfer: the same rule applies. The terminal-clean authoriser rule reads the new treating department, and keeping an out-of-service bed during a transfer needs a typed reason. On a swap: each patient goes into the bed the other has just left with no clean in between, and on a tracked ward a typed reason is recorded on both. A bed whose next clean is terminal needs an authoriser who acts plus a reason, by the same rule as entry 51: the sender in their own session, or a named person typing their own signing PIN. A bed with a live hold is refused.
- **Looser option:** None named. Wave 3 deferred `ADT-S2` option (c), a terminal-clean block at admit or move, to `W5-B` and `W6-C`; waves 5 to 7 then built it.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S2` (out-of-service part), `ADT-S2` (c)
- **Source:** Waves 3, 5, 6 and 7; records W3-8, W5-11, W5-13, W6-22 and W7-14.

**44. The bed board names patients only inside the user's department**

- **What is built:** The bed board names the patient only for beds inside the user's own department; other beds just say 'Occupied' or 'Boarder' with no patient details; other wards' boards answer 404; nurses do not hold `adt.read`, so they cannot open the board (403). Host-ward nurses get no access to their boarders (`ADT-S6`, not applied), and what nurses may do in ADT has only an interim answer (`ADT-S7`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S6`, `ADT-S7`
- **Source:** Wave 4; record W4-14.

**45. How the bed board ranks bed states**

- **What is built:** On the board an occupied bed shows as occupied above every other state, even 'retired'; a typed ward and bed that names a set-up bed holds that bed; a departure and a clean recorded in the same millisecond read 'Needs cleaning'.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `C2`
- **Source:** Wave 4; record W4-16.

**46. Untracked cleaning shows as such, and beds awaiting a clean are never offered**

- **What is built:** On a ward where cleaning is not tracked, a free bed reads 'Vacant, cleaning not tracked'; the list of free beds never offers a bed that is waiting for or under a clean. Interim answer to `ADT-S2`.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT` with `ORB_HOUSEKEEPING`
- **Ruling:** `ADT-S2`
- **Source:** Wave 4; record W4-17.

**47. Only a doctor can reopen an ended stay**

- **What is built:** Reopening an ended stay is for doctors only (`discharge.generate`), limited to the department of the ended admission, and needs the MRN read back and a reason. Only a stay that ended without a discharge summary, with a live ADT discharge record, can be reopened; it is refused if the patient has another active or later stay. There is no time limit yet (an open question).
- **Looser option:** None named; a time limit on reopening is an open question.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S3`
- **Source:** Wave 4; record W4-18.

**48. What reopening a stay clears and restores**

- **What is built:** Reopening clears the discharge time, discharge type and who discharged (all kept in the movement record and the audit), voids but never deletes the stay's NABH dispositions, restores only the patient portal login that the end switched off, and never restarts the care bundles the end cancelled (for example a running Sepsis Six timer). In plain words: after a stay ended in error is reopened, staff must restart any bundle by hand.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-P8`
- **Source:** Wave 4; record W4-20.

**49. On reopen the old bed comes back only if it is free and usable**

- **What is built:** On reopen the patient gets the old bed back only if it is free and usable; otherwise they are listed as awaiting a bed. A bed marked out of service after the stay ended is still taken back, with a `bed_out_of_service` flag.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S1` (a), `ADT-S2`
- **Source:** Wave 4; record W4-19.

**50. Typed ward and bed text that clashes with another patient is refused**

- **What is built:** A typed ward and bed that are not in bed setup (or an unlisted bed on a set-up ward) are refused when another active admission or admitted patient is already recorded at the same text (capitals and spaces ignored), on the admission form, in Scribe and on the bed route. With the module off this is only a warning.
- **Looser option:** Module-off behaviour: a warning only.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S1` (a)
- **Source:** Wave 5; record W5-10.

**51. The terminal-clean authoriser must act in person**

- **What is built:** The terminal-clean authoriser must act personally: either the sender in their own session, if a doctor, clinician, nurse or admin, or the named person typing their own signing PIN (5 wrong in 15 minutes locks, 429; every wrong or locked try audited, fail-closed). Just naming someone is refused (409 `terminal_clean_pin_required`). The authoriser must work in the ward's department, the treating department, or have no department. The PIN is never stored. Role list and department rule to confirm.
- **Looser option:** None named; the role list and the department rule are open for you.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`; `services.adt.core.TERMINAL_CLEAN_AUTHORISER_ROLES`
- **Ruling:** `ADT-S2` (c)
- **Source:** Wave 5; record W5-12.

**52. A recorded bed is never overwritten**

- **What is built:** A bed already recorded is never overwritten (409 `bed_already_recorded`), except by a correction that names the expected bed and gives a reason (if things changed meanwhile, 409 `state_changed`); a request key (Idempotency-Key) is required; the treating department never changes (`ADT-S8` held); nurses get 403 (`ADT-S7` held); bed text with no ward and no recorded ward is refused (422 `ward_required`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S7`, `ADT-S8`
- **Source:** Wave 5; record W5-16.

**53. A resent admission returns only for the same user and patient**

- **What is built:** If an admission request is resent with the same request key, the first admission comes back only for the same user and a patient of the same name; any other reuse is refused (409 `idempotency_key_reused`) and nothing is written.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 5; record W5-17.

**54. Ending a stay never closes an open escalation**

- **What is built:** At end of stay, open and acknowledged escalation chains are listed and can be closed one by one with a reason. Ending the stay never closes them, and they never block the end. If the list cannot be read the screen says so rather than showing it empty. If a stay ends while a chain is paused because the patient is away, the chain is resumed with the reason '`stay_ended`'. It then behaves as any open chain on a discharged patient does today: it keeps climbing its tiers until someone closes it by hand (wave 8 deferred: `escalation_service.advance_due` ignores whether the patient is still in).
- **Looser option:** None named (`R009` is held).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `R009` (held)
- **Source:** Waves 5 and 9; records W5-19 and W9-10.

**55. One active admission per patient, enforced by the database**

- **What is built:** A duplicate MRN on the admission form is refused without naming the other patient (exact match when registration is off). One active admission per patient is enforced by the database itself, whatever the code does; while any patient holds two active admissions, the migration stops the server from starting and names the patients.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (migration 65)
- **Source:** Wave 6; record W6-17.

**56. Who can change a treating department**

- **What is built:** Changing a patient's treating department needs doctor or administrator rights. The ward clerk can only change beds (a transfer is refused). Nurses are refused on every move screen, the move options and the location read.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S8` (held)
- **Source:** Wave 6; record W6-19.

**57. Roster import cannot skip bed and cover checks**

- **What is built:** A roster import line that would need a cleaning reason, an authoriser or a boarder acknowledgement, or names a department with no clinical cover, is refused and nothing moves. With ADT on, the roster never writes the ward or bed of an open admission.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 6; record W6-23.

**58. Strict handover: the sending department loses the chart at once**

- **What is built:** Strict handover: the sending department loses the chart the moment the transfer is saved. The sender's own retry of the same request gets back only a bare receipt (no patient details); any other reuse of the request key is refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 6; record W6-24.

**59. Moves need a request key, the expected bed and a reason**

- **What is built:** A transfer or bed change needs a request key, the bed the screen expected (if it changed meanwhile, refused) and a reason. The old front desk request without an expected bed is still accepted on its delegated route, with the bed unchecked and a key made up when none is sent. While ADT is on, the front desk's old transfer writer refuses to write.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 6; record W6-25.

**60. Honest bed cleaning record**

- **What is built:** Honest bed record: if a patient was put in a bed before it was recorded clean, housekeeping records 'occupied without clean' naming the move and cancels its open cleans; a skipped terminal clean stays owed. Three edge cases keep the record honest: the next patient's arrival in that bed is recorded in the same sync or was already recorded, or a clean is recorded after the patient left. In each case no bed a patient lies in is left waiting for a clean, a bed is never marked occupied after the patient left, and a bed recorded clean afterwards is never marked dirty (a tie counts as not cleaned). With both switches off nothing is read or written.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT` and housekeeping
- **Ruling:** None (`adt-7`, strictest reading)
- **Source:** Wave 6; record W6-26.

**61. Bed swaps never change a treating department**

- **What is built:** A swap never changes either patient's treating department. A move onto another department's ward needs the boarder acknowledgement and writes an access record plus one case-room line. The ward clerk may swap; nurses are refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S6` (not applied), `ADT-S8` (held)
- **Source:** Wave 7; record W7-16.

**62. Undoing a move recorded in error**

- **What is built:** Only a bed assignment, bed change or transfer can be undone as recorded in error, and only the stay's latest move, once, while the patient is still there. A swap is undone by swapping back. Going back to a bed not recorded clean meets the full cleaning rule. Undoing a transfer needs doctor or administrator rights, clinical cover for the department it returns to, and an audit record that must be written; the receiving department loses the chart when it is saved.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S2` (c); rule C
- **Source:** Wave 7; record W7-17.

**63. Relay transfer cards follow the cover rule and never redirect**

- **What is built:** A transfer confirmed from a Relay card is held to the department it names (if Orb knows it) and refused with rule C's words if that department has no clinical cover, never redirected. A card naming no department goes to General Surgery, which must be covered. The confirmation counts as the boarder acknowledgement ('Confirmed in Relay'). A kept bed that is out of service fails the dispatch (Relay cannot type a reason). Any refusal or audit failure leaves everything unchanged and the handoff is stored as failed.
- **Looser option:** None named.
- **If relaxed:** Wave 6 deferred states the earlier risk: the old Relay lane changed the patient's department directly with no cover check and no movement record, so `ORB_ADT` was not to be switched on at a site with Sage until this landed.
- **Switch:** `ORB_ADT`
- **Ruling:** Rule C (`adt-6c`)
- **Source:** Wave 7; record W7-18.

**64. Patients on leave or absconded: clocks pause, nothing closes**

- **What is built:** Wave 7 only showed 'Not on ward since HH:MM' and left every clinical clock unchanged. Wave 9 applied the architect's recommendation: a patient on leave or absconded stays on every board with a 'Not on ward' chip. The nurse board pauses the observation-due and dose-due clocks with a stated reason and lists the patient after those on the ward. The escalation ladder is paused, never closed, and resumes with exactly the minutes it had left. Sepsis auto-start opens nothing, the vitals simulator writes nothing, and the ward downtime pack prints NOT ON WARD and keeps the page.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S4` (a)
- **Note:** Who can record leave and absconding, and the rules for those records, are entries 65 and 66, and the 24 and 48 hour markers are entries 316 and 317. Since wave 9 a leave record pauses this patient's observation and dose clocks, so decide them with this entry.
- **Source:** Waves 7 and 9; records W7-21 and W9-6.

**65. Only doctors and the administrator record leave and absconding**

- **What is built:** Leave and absconding can be recorded only by the administrator (`adt.presence`) or doctors (`patient.write`). Nurses and ward clerks are refused, and nurses get no bed board. The `W7-D` notes say nurses hold no ADT permission in code, so this runs at its strictest. In plain words: recording leave or absconding pauses the patient's observation and dose clocks and the escalation ladder (entry 64), so this decides who can pause a patient's alerts.
- **Looser option:** None named in the bullet. Wave 7 deferred lists `ADT-S7` nurse and ward clerk grants as held.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S7` (held)
- **Note:** The `W9-B` deferred list adds a cost of the strict side: because only doctors and the administrator record presence, a paused escalation resumes only when a doctor records the return or a new vitals set is charted. Nurse and ward clerk grants (`ADT-S7`) are held.
- **Source:** Wave 7; record W7-20.

**66. Rules for leave and absconding records**

- **What is built:** Presence rules: one open leave at a time; no leave while absconded; leave ends only from on leave, return only from absconded; on leave may become absconded; a typed reason for leave and absconding; times up to `ORB_ADT_BACKTIME_HOURS` back and at most 5 minutes ahead, never before the admission or the last change; the bed is always held; a request key is required; every change is audited (fail-closed).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT` (times bounded by `ORB_ADT_BACKTIME_HOURS`)
- **Source:** Wave 7; record W7-22.

**67. Who may hold a bed, and limits on holds**

- **What is built:** Reservations need ward clerk, doctor or administrator rights; nurses are refused. Every patient a hold names is checked against the user's department, again at the moment of writing (refused as not found, audited). A hold for nobody only on the user's own department's wards. One live hold per patient. Occupied, out-of-service, retired and switched-off beds cannot be held. Transfer and post-op holds must name an admission. Release needs a reason. The board and list name the held patient only inside scope, and a read that names anyone is audited (fail-closed). A department-limited account cannot hold a bed for a patient with no admission.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 7; record W7-27.

**68. A hold's note is hidden outside the department**

- **What is built:** A reservation's note is hidden whenever the hold names a patient outside the user's department. On a hold naming nobody, it is shown only to its maker, an account with no department, and the ward's own department.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 7; record W7-28.

**69. The previous stay's vitals never count as latest after readmission**

- **What is built:** On a readmitted patient, vitals or risk scores from the earlier stay never count as the latest reading on the Vigil board, ward brief, chart, the CfC risk model's input, the expected-course band or Sage. They are shown only with their date and the label 'Previous stay', never scored (on the Vigil board itself the 'Previous stay' line is not drawn yet, so the patient shows 'no observations' until it is); the earlier stay's stored risk band is sent only while risk scores are switched on. Sage's memory of the room is archived and restarted, its open suggestions are superseded, its prefetched context is cleared, and the case room is reused with a READMISSION divider line. The readmission itself records no observations (sending vitals with it is refused).
- **Looser option:** None named.
- **If relaxed:** Not stated for this item. The wave's own notes say other lanes not yet covered (for example the vitals carry-forward of 4 to 12 hours) could cross the stay boundary on a quick readmission, which is why `ORB_READMISSION` must stay off until they read through the same rule.
- **Switch:** Always on for the boundary reads (only a patient with an ended stay plus an active one); the Sage and case-room steps run with `ORB_READMISSION`
- **Ruling:** `ADT-S9` (a)
- **Note:** The wave 8 notes are explicit: `ORB_READMISSION` must stay off until the `ADT-S9` lanes in the `W8-A` deferred list (nurse board, I-PASS, portal, Ward Record chart, vitals carry-forward and 16 others, 21 lanes in all) read through the stay boundary. The same deferred list names gaps outside that gate that are still open: on a readmission, open escalation chains from the earlier stay keep climbing; earlier-stay CPOE orders stay listed on the Orders screen (the source calls cancelling them a clinical decision); an open lab critical from the earlier stay would be posted into the reopened case room; and portal medicines cover all stays. Decide these before lifting the hold.
- **Source:** Wave 8; record W8-3.

**70. A readmission cannot be dated before the last stay ended**

- **What is built:** A readmission dated earlier than the latest end of any earlier stay is refused (`before_previous_stay_end`). The end is the discharge time, else the discharge summary. An end up to 5 minutes in the future is respected, so a readmission at 'now' just after a slightly forward-dated discharge has to wait. An end further in the future counts as no end, so a mistyped imported date cannot block the patient forever.
- **Looser option:** Your choice between the built choice (ignore far-future, mistyped ends) and the stricter alternative (block the readmission until the date is corrected). This entry runs the other way: the build takes the looser of the two choices. It belongs to the same stay-boundary rule as entry 69.
- **If relaxed:** Not stated for relaxing. The source says the stricter alternative would let a mistyped imported date block the patient's readmission until it is corrected.
- **Switch:** `ORB_READMISSION` with `ORB_ADT` on
- **Ruling:** `ADT-S9` (a) extended
- **Source:** Wave 8; record W8-4.

**71. Only discharged patients can be readmitted**

- **What is built:** Builder's strict default: only a patient marked discharged, with an ended admission, can be readmitted. Patients marked transferred, never admitted, or only registered are refused (`readmission_not_applicable`). A record showing a death (a discharge type of death, or a death disposition in the NABH record) is refused (`patient_deceased`) until the earlier stay is corrected.
- **Looser option:** Also allow readmitting patients marked 'transferred'.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_READMISSION`
- **Source:** Wave 8; record W8-5.

**72. The ward clerk is held at its current limits**

- **What is built:** Unchanged while `ADT-S7` and `ADT-S8` are held: the ward clerk keeps its current limits, and automated tests hold it there. It is refused on leave, absconding and any department change; it reads only its own department's board, each read logged (refused if logging fails); no chart, admission, end of stay, bed setup change or Relay.
- **Looser option:** None named. Rulings held.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S7`; `ADT-S8` (held)
- **Source:** Wave 8; record W8-25.

**73. A ward clerk can admit, with every clinical field left unassessed**

- **What is built:** A non-clinical ward clerk can admit a patient: only the `ward_clerk` role with bed rights and a department; the administrator is refused (logged). Own department only, with an attending doctor of that department; the hospital's own number is required unless registration issues it. Every clinical field is stored as not yet assessed (complaint 'Not yet assessed', code status 'Not recorded', no diagnosis, triage, history, medicines, observations, notes or isolation). No allergy entry and no allergy review are written, and sending any clinical field is refused. The allergy interlock sees no allergies on file, as for any patient not yet asked. The bed board shows 'Admission assessment due', only to staff inside the patient's department, until a doctor records the assessment. Each clerk admission is logged (refused if logging fails). The clerk is shown the patient's one-time portal password, as any admitting user is (to confirm).
- **Looser option:** None named as looser. The stricter option is `ADT-S5` (b), doctors only, which is what `ORB_ADT` off gives today. Also to confirm: whether the clerk should see the one-time portal password.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S5` (a)
- **Source:** Wave 9; record W9-1.

**74. Only a doctor completes a clerk admission assessment**

- **What is built:** Builder's strict default for completing a clerk admission: only `patient.write` holders record the doctor's admission assessment, and only for patients inside their department (a patient outside it looks not found); nurses, the clerk and the administrator are refused. Allergy rules as in OPD: 'none known' is refused while an allergy is active or named; 'unable to obtain' needs a reason; 'allergies recorded' needs an allergy named or on the chart. Presenting complaint and code status are required. Recorded once per clerk admission, logged (refused if logging fails), with a case-room line.
- **Looser option:** Open question: let nurses also take the allergy history (they lack `patient.write` today).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S5` (a)
- **Source:** Wave 9; record W9-2.

**75. A ward clerk can record 'left the ward' on a Ward Record computer**

- **What is built:** The ward clerk can record that a patient left the ward: own department only, and only on an Orb Ward Record computer (refused elsewhere, where a doctor's discharge ends the stay). It goes through the end-of-stay steps: the discharge precondition check (which checks nothing today, because `R009` is held; see Also open), typing the MRN back (a mismatch writes nothing), the back-time window, the discharge bed movement, care bundles closed, and a log entry (refused if logging fails). No end type is guessed (it is left as 'not stated'), so no NABH outcome is recorded and the records desk has to enter it by hand. There is no discharge summary and no patient link. Refused for a patient on leave or absconded. Death, LAMA, DAMA and absconded stay doctor-only.
- **Looser option:** Record the end type as 'routine' instead of 'type not stated'.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT` + `ORB_WARD_RECORD`
- **Ruling:** `ADT-S3`
- **Source:** Wave 9; record W9-3.

**76. A ward clerk can change department only on a named doctor's order**

- **What is built:** The ward clerk changes the treating department only on a doctor's order, own department only. The ordering doctor must be active and either the patient's attending or of the patient's current department (a doctor with no department counts only as the attending). Naming the doctor is enough: the record says the clerk named them (not confirmed by the doctor), the move's reason names the doctor, a log entry names both (refused if logging fails), and a case-room line tells the treating team. All transfer rules apply (rule C cover, bed lock, cleaning, boarder acknowledgement, transfer log). A move within the same department is refused (that is a bed change).
- **Looser option:** None named as looser. A stricter option to confirm: the ordering doctor confirms with their own signing PIN.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S8` (b)
- **Source:** Wave 9; record W9-4.

**77. Absconding with an open escalation posts a case-room line**

- **What is built:** When a patient absconds with an unresolved escalation (open or acknowledged), each chain gets one case-room line per absconding naming the treating doctor (the admission's attending, else the first ward doctor, else the role). Orb pages nobody; the line tells the treating doctor to review and anyone else to phone them. The event and log entry record who was named.
- **Looser option:** None named. To confirm the rule for who is named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S4` (a)
- **Source:** Wave 9; record W9-7.

**78. Observations taken while 'away' still count and alert**

- **What is built:** Builder's choices, each keeping the alert: observations taken after the patient left count as the patient being seen and are live (they raise or extend the escalation, which then runs and is not paused again; a bedside set still opens a sepsis bundle). A missing leaving time counts as live. Presence that cannot be read counts as the module off (nothing paused, paused chains resume). If an escalation step's waiting time ran out before the patient left, the escalation moves up a step first. If the stored minutes left cannot be read, the next step is due at once.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S4` (a)
- **Source:** Wave 9; record W9-8.

**79. Clocks restart from the last real care on return**

- **What is built:** When the patient comes back, clocks restart from the last observation set a person took and the last dose given, so a patient back from a long leave shows observations overdue at once and missed doses show as not charted until care is recorded. Nothing is reset to the return time.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S4` (a)
- **Source:** Wave 9; record W9-9.

Also behind `ORB_READMISSION`, filed elsewhere: entry 106 (readmission stops every medicine left running from the last stay) and entry 387 (the portal login gets a fresh password).

### Medication orders

**80. 'Given' is refused on a held, unsigned or ended order**

- **What is built:** The check that decides whether a dose may be charted refuses 'given' when the order is on hold, still an unsigned draft, ended or unreadable, and always allows 'not given'. The eMAR uses it for every ward medicine row linked to an order, even after the switches are turned off; a database that never ran the store has no links. At the bedside the Administer screen says 'This medicine is on hold' with the server's sentence, keeps Given disabled and offers only 'Record as not given (held)'. There is no override for a hold.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on, keyed on the order link (`medications.med_order_id`), not on a switch
- **Ruling:** `MO-R9` (a)
- **Source:** Waves 4, 5 and 9; records W4-36, W5-63 and W9-18.

**81. Adopting old medicine rows never guesses**

- **What is built:** Bringing old rows in never guesses: rows with an odd status, a blank drug name, a one-sided link, or a pharmacy (Helix) row with no link stay unlinked and are listed in the parity report (the list of old rows the order store could not match).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE`
- **Source:** Wave 4; record W4-39.

**82. Hold, resume, stop, cancel and reject need a reason**

- **What is built:** Holding, resuming, stopping, cancelling or rejecting a medication order needs a non-blank reason (422 `reason_required`); signing and verifying refuse a request with no named user.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE` / `ORB_MED_ORDER_LIFECYCLE`
- **Source:** Wave 4; record W4-40.

**83. Who verified a medicine is copied to the ward row and never cleared**

- **What is built:** Who verified a medicine, and when, is copied onto the ward's medicine row and never cleared; linking an order never changes what the ward sees.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE`
- **Ruling:** `I1`
- **Source:** Wave 4; record W4-42.

**84. A broken order link blocks 'given' until repaired**

- **What is built:** A ward medicine row that points to a missing order refuses 'given' (`order_state_unknown`); the parity report (the list of old rows the order store could not match) lists it; an operator must repair the link before that drug can be given from the eMAR.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (keyed on `medications.med_order_id`)
- **Source:** Wave 5; record W5-64.

**85. Pharmacist decisions on linked rows are undone together**

- **What is built:** On the old Verify Queue, if the order store refuses the pharmacist's decision on a linked row, the old write is undone too (400 with a sentence); with a broken link the pharmacist cannot verify or reject until it is repaired.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE` (linked rows)
- **Source:** Wave 5; record W5-65.

**86. Verifying a held order keeps the hold**

- **What is built:** Verifying a held order keeps it on hold; rejecting a held order ends it and clears the hold marker on the ward row.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE`
- **Source:** Wave 5; record W5-66.

**87. Pharmacist corrections are copied onto the order**

- **What is built:** A pharmacist's correction on the Verify Queue is copied onto the order as an 'amended' entry (before and after) plus an access audit row, before it is marked verified.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE`
- **Source:** Wave 5; record W5-67.

**88. Signing needs dose, route and frequency; the route is never defaulted**

- **What is built:** Signing an order, or creating it as prescribed, needs dose, route and frequency (422 `dose_route_and_frequency_required`); the route is never filled in as 'Oral' by default.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`
- **Source:** Wave 5; record W5-68.

**89. Unsigned drafts are never cancelled automatically**

- **What is built:** The system never cancels unsigned draft orders. The to-sign queue ('Awaiting my signature') lists unsigned medicines oldest first and marks any waiting more than 24 hours.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE` (module constant `DRAFT_WAIT_HOURS`, 24)
- **Ruling:** `MO-R10` (a)
- **Source:** Waves 5 and 7; records W5-69 and W7-41.

**90. Only unsigned drafts can be cancelled; a signed order is never cancelled from the chart**

- **What is built:** Until `medorders-6a` lands, a doctor cannot stop, hold or cancel a signed CPOE or order-set medicine in Orb (wave 7 deferred). Only draft orders can be cancelled, with a reason, by their creator or by an `order.write` holder in the patient's department (on the Orders screen, anyone in scope with order rights). For a signed order, a non-prescriber is refused (403 `prescriber_required`, audited) and a prescriber is told cancel is not offered yet (409 `cancel_not_offered`, until `medorders-6a`). A repeat cancel answers OK and writes nothing. On the order screens (wave 9) no step is offered in record mode; a draft is cancelled, with a reason, only by whoever entered it or a prescriber, and a signed order is never cancelled from the chart; Sign, Hold, Resume and Stop are offered only to `medication.prescribe` holders, each except Sign needing a reason; Hold, Resume and Stop stay hidden until the server serves them. This is an interim answer.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE` (with `ORB_MED_ORDER_STORE`); screen constant `MEDORDER_HOLD_STOP_ROUTES` (false)
- **Ruling:** `MO-R2` (a), interim
- **Note:** Our reading of the code, not stated in the source: the ward chart has no step to stop a medicine already charted either. Its only medicine step adds one, and the Hold, Resume and Stop router (`routes/medorders/lifecycle.py`) is still empty. An engineer should confirm how a doctor stops a drug today.
- **Source:** Waves 5, 7 and 9; records W5-70, W7-40 and W9-17.

**91. A blank drug name is refused**

- **What is built:** With the store on, adding a medicine on the chart with a drug name of only spaces is refused (422 `drug_name_required`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE`
- **Source:** Wave 5; record W5-71.

**92. A second live order for the same ingredient is refused outright**

- **What is built:** Until wave 9 a same-ingredient duplicate was only a notice, so a prescriber could sign a duplicate of a drug already on the chart. Wave 9 built the strictest option (c): a second live order for the same ingredient is refused and no reason clears it (also on the Sage, Scribe and Sage assessment lanes). It applies only at the moment of ordering, never at pharmacy verification or pharmacy (Helix) steps; resending the same request is never its own duplicate. On the Scribe lane a blocked duplicate is still charted, marked unverified with a `[SAFETY]` note (`W111` held).
- **Looser option:** 'acknowledge' (the plan's option (b)): the warning can be cleared only by a clinical reason of at least 10 characters that is not a reflex like 'ok', and the override is logged. The source says (b) looks right: with `medorders-6a` unbuilt, 'block' leaves no lifecycle-mode way to order a legitimate same-ingredient route change or stat top-up. Also to confirm: the Scribe lane still charts a duplicate unverified.
- **If relaxed:** Stated in wave 5 for the old notice-only state: a prescriber can sign a duplicate of a drug already charted.
- **Switch:** `ORB_MED_ORDER_DUPLICATE_RULE` (default 'block'; read only while `ORB_MED_ORDER_LIFECYCLE` is on)
- **Ruling:** `MO-R6` (c)
- **Source:** Waves 5 and 9; records W5-72 and W9-11.

**93. Only pharmacists verify medication orders**

- **What is built:** Only pharmacists hold the `medication.verify` permission; tests pin the administrator as not holding it. With the lifecycle on, a pharmacy (Helix) verify or reject counts as the order's pharmacist check only when a pharmacist does it. An administrator with Helix rights is refused (audited) and the pharmacy step is undone. In record mode the step is only a supply event.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on for the role permission `medication.verify`; `ORB_MED_ORDER_LIFECYCLE` for the Helix step
- **Ruling:** `MO-R4` (a)
- **Source:** Waves 5 and 7; records W5-73 and W7-30.

**94. Outpatient prescriptions never become inpatient medicines on their own**

- **What is built:** When an outpatient is admitted, no outpatient prescription becomes an inpatient medicine, and nothing is written to the inpatient medicine chart, the medication order store or the bedside medication record. The admission preview lists the live outpatient medicines so the prescriber can reconcile them one by one.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`; `ORB_OPD` + `ORB_MED_ORDER_STORE` for prescription lines
- **Ruling:** `OPD-S4` (a)
- **Note:** Known gap (wave 8 notes): the preview lists every outpatient line still marked prescribed, even one whose course has ended, because outpatient lines never close on their own.
- **Source:** Waves 7 and 8; records W7-10 and W8-17.

**95. A pharmacy substitute never counts as verification**

- **What is built:** Substitution is unchanged: a pharmacy substitute never counts as the order's verification (`verified_by` stays empty, and a drug sourced from AI stays not givable). The substitute is screened against every drug on the chart, including the order's own row. Only the dose check is added.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `W7-E` switches `ORB_MED_ORDER_STORE` / `ORB_MED_ORDER_LIFECYCLE` (the bullet names none)
- **Ruling:** `R002` (held)
- **Source:** Wave 7; record W7-31.

**96. Pharmacy steps on linked orders refuse mismatches and ended orders**

- **What is built:** On a pharmacy supply row linked to a medication order: verify is refused while the dose or route is blank or 'REVIEW REQUIRED', or the supply no longer matches the order's dose, route or frequency (reject and request again; nothing is corrected in place). Substitute, verify, schedule and dispense are refused on an unsigned or ended order. Any committing step is refused if the order changed or a new allergy or drug was charted since screening. Each refusal undoes the pharmacy step, its stock movement and its case-room message.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on for supply rows linked to a medication order
- **Ruling:** None (`F-A` closure)
- **Source:** Wave 7; record W7-32.

**97. The dose check runs at pharmacy steps and only the prescriber clears it**

- **What is built:** The dose check runs at the pharmacy's substitute, verify and dispense steps using the supply's dose and route. A pharmacist's reason cannot clear a dose block; it goes back to the prescriber. Unlinked rows are screened as before.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on for supply rows linked to a medication order
- **Source:** Wave 7; record W7-33.

**98. How held medicines are labelled across Orb**

- **What is built:** Held medicines: the portal shows 'On hold' only (no time, reason, verdict or colour). Discharge reconciliation keeps a held drug under 'continued' with a WARNING for the prescriber to decide. I-PASS (sheet and screen), FHIR, Sage and ambient label it. A held drug stays in the safety-screening sets. No screen can put an order on hold until `medorders-6a`, so this is dormant for now.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on, keyed on `medications.held_at` (dormant until `medorders-6a`)
- **Ruling:** None (`MO-R2` stays with `medorders-6a`)
- **Source:** Wave 7; record W7-35.

**99. CPOE orders linked to the store stay under the store**

- **What is built:** A CPOE or order-set medicine linked to the medication store stays under the store even after the switches are turned off. The old Orders change route refuses every change to it, a retry included, checked both in the route and at the moment of writing. Cancel is handed to the store.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE` (holds even after the switches go off)
- **Ruling:** `MO-R9` (a)
- **Source:** Wave 7; record W7-36.

**100. CPOE medicines must carry a structured dose**

- **What is built:** With the lifecycle on, a CPOE medicine must carry a structured drug name, dose, route and frequency, and its description must name the drug, so the dose check always runs at entry. Consequence stated: a few existing tests get a different refusal code with the lifecycle on; with it off they pass unchanged.
- **Looser option:** None named.
- **If relaxed:** The source states the purpose: without it a CPOE medicine could be entered without the dose check running at entry.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`
- **Ruling:** None (`medorders-7` strict reading)
- **Source:** Wave 7; record W7-37.

**101. The safety check runs at creation and again at signing**

- **What is built:** The order-time safety check runs when a CPOE medicine is created or an order set applied (with the creator's role), and again at signing with the signer's role, refused if the chart changed meanwhile. So a severe allergy acknowledged at creation must be acknowledged again by the prescriber.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`
- **Source:** Wave 7; record W7-38.

**102. Steps that put a drug on the ward are refused if out of step**

- **What is built:** A step that puts a drug on the ward (sign, resume) is refused if the matching Orders row is not in the state expected. A step that takes a drug off is never blocked (it is logged and recorded).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`
- **Source:** Wave 7; record W7-39.

**103. Free-text CPOE medicines are marked as not on the formulary**

- **What is built:** A CPOE medicine may be free text with no code (a code that is sent is still checked). The Orders list says 'Not supplied by pharmacy: this medicine is not on the formulary.' unless the drug name exactly equals an active pharmacy master name or generic name (ignoring case and spacing, never a partial match). No product is bound.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`
- **Ruling:** `MO-R11` (a)
- **Source:** Wave 7; record W7-42.

**104. Orb never raises a pharmacy supply request by itself**

- **What is built:** Orb never creates a pharmacy supply request by itself. Pharmacy supply happens only when a prescriber or pharmacist asks for it, which sets the source to pharmacy. Only the prescriber changes the source. Ward stock or the patient's own supply means the pharmacy is never asked. Moving away from pharmacy is refused while a pharmacy supply is open.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: nobody is prompted to ask the pharmacy, so a newly signed drug may not reach the ward.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`
- **Ruling:** `MO-R12` (a)
- **Note:** Our check of the code, not stated in the source: no screen calls the 'request supply' step yet. Wave 7 deferred lists 'Request from pharmacy' and the supply-source picker as server-only, and no later wave adds them. So in lifecycle mode nobody can ask the pharmacy for a signed medicine from a screen. An engineer should confirm.
- **Source:** Wave 7; record W7-29.

**105. Signed CPOE medicines raise no pharmacy request**

- **What is built:** A signed CPOE or order-set medicine raises no pharmacy supply request; every linked line on Orders says 'Not sent to the pharmacy'.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: as built, a signed medicine is not sent to the pharmacy. The ward sees 'Not sent to the pharmacy' on the Orders line, and unless someone asks for supply (entry 104, which has no screen yet) the patient may miss doses.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`
- **Ruling:** `MO-R12` (not ruled into code for CPOE)
- **Source:** Wave 7; record W7-44.

**106. Readmission stops every medicine left running from the last stay**

- **What is built:** When a discharged patient is readmitted, every medicine still running on the earlier stay's chart is stopped in the same step, including linked orders in the order store (reason 'Stopped at readmission: prescribed in the previous stay') and live inpatient orders that have no chart entry. Unsigned drafts are left alone. One log entry records what was closed. The readmission answer lists the stopped medicines so the doctor can re-prescribe them one by one: by name only to someone allowed to read this chart, otherwise as a count; on an Orb Ward Record computer only as a count. The treating team sees the names on the case-room line. The bedside medication record refuses to mark any of them 'given'.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_READMISSION` (a linked live order is stopped through the order store whatever the other switches say)
- **Ruling:** `OPD-S8` (a); `MO-R10`
- **Note:** Orders entered through CPOE on the earlier stay still show on the Orders screen (see entry 69).
- **Source:** Wave 8; record W8-2.

**107. No outpatient prescription line before an allergy review**

- **What is built:** No outpatient prescription line is saved until the patient's allergies have been reviewed: a review on this visit, or an earlier 'allergies recorded' or 'none known' that the chart does not contradict. Allergies on the chart with no review do not count, and an earlier 'unable to obtain' never carries over. The check runs before the safety check and again inside the save. The review never changes how the allergy interlock screens.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD` + `ORB_MED_ORDER_STORE`
- **Ruling:** `OPD-S7` (a)
- **Note:** The printed prescription and the consultation print never list the patient's allergens. Whether they should is your call (entries 35 and 113).
- **Source:** Wave 8; record W8-12.

**108. Outpatient prescriptions are screened against other clinics' medicines**

- **What is built:** Builder's addition: each new outpatient line is checked for interactions against the patient's other outpatient medicines from any clinic whose course has not ended (an unreadable end date counts as still running). A severe pair is blocked until a prescriber gives a written reason, and the block or override is logged. If the check cannot run, nothing is saved.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD` + `ORB_MED_ORDER_STORE`
- **Source:** Wave 8; record W8-13.

**109. Which visits can take an outpatient prescription**

- **What is built:** A prescription can be written only on a visit in the doctor's own clinic (others look not found), inside the outpatient window, not cancelled, not finished, and only when the patient has no active admission anywhere. All of this is checked again inside the save.
- **Looser option:** Allow same-day lines after the visit is completed.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD` + `ORB_MED_ORDER_STORE` (visit window `ORB_OPD_SCOPE_DAYS`)
- **Source:** Wave 8; record W8-14.

**110. What an outpatient prescription line must carry**

- **What is built:** Dose, route and frequency are required and the route is never filled in by default. 'When needed' must say what for. Duration (1 to 365 whole days) and quantity (1 to 9999 whole units) are optional and never made up. Unknown fields are refused. At most 25 live lines per visit.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD` + `ORB_MED_ORDER_STORE`
- **Source:** Wave 8; record W8-15.

**111. A printed prescription line can still be cancelled**

- **What is built:** Builder's default: a prescriber may cancel a line, with a reason of at least 3 characters, while the visit is open, even after it was printed. The answer and the log say it was printed before cancelling, and the screen asks for the paper copy back.
- **Looser option:** None named. A stricter option is named: refuse cancelling once printed.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD` + `ORB_MED_ORDER_STORE`
- **Source:** Wave 8; record W8-16.

**112. Outpatient prescribing works with the order store in record mode**

- **What is built:** The outpatient prescription lane works with only the medication order store on, in record mode. Our check of the code, not stated in the source: the same-ingredient duplicate rule is read only while `ORB_MED_ORDER_LIFECYCLE` is on, so in record mode a second outpatient line for the same ingredient is only a notice, not a block (entries 92 and 125).
- **Looser option:** None named. A stricter option is named: also require `ORB_MED_ORDER_LIFECYCLE`, as the inpatient medication order write routes do.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE` (the lane does not need `ORB_MED_ORDER_LIFECYCLE`)
- **Source:** Wave 8; record W8-18.

**113. What the prescription list and print show**

- **What is built:** The prescription list never returns the stored safety check, only whether a warning was overridden. The printed prescription shows the allergy review as status, label and time only; diagnoses only from a signed note; live lines only; the doctor's name and registration number; no date of birth, phone or address. Viewing the list and printing each write a log entry; if that fails nothing is shown.
- **Looser option:** None named here. Allergens on the printed prescription are a separate question deferred to you since `W6-A` (`W8-C` deferred list).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD` + `ORB_MED_ORDER_STORE`
- **Note:** This is an allergy decision. As built, a patient who takes this prescription to an outside chemist carries no allergy list on it. Whether allergens should print is your call (wave 6 and wave 8 deferred; see entry 35).
- **Source:** Wave 8; record W8-20.

**114. A confirmed Sage medicine suggestion is all-or-nothing**

- **What is built:** When a clinician confirms a Sage medicine suggestion, nothing is best-effort: if the order, the pharmacy supply request, the link or any log entry cannot be written, the chart entry is undone and the Relay card reads 'failed'. A missing patient or confirming clinician fails the card. With the store off it still charts and answers 'charted-only' if the supply fails.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE`
- **Source:** Wave 8; record W8-29.

**115. Sage's name match links only the supply request, not the order**

- **What is built:** Sage's name match links only the pharmacy supply request to a product; the order itself stays unlinked to the formulary. Wave 9 changed this. With the lifecycle on, a strong match now links the order to the product too (entry 127); a weak match is charted only, with no pharmacy supply. In record mode there is no 'Request from pharmacy' step at all (entry 116), and no screen offers that step in either mode yet (entry 104). To confirm: whether anything more is needed here.
- **Looser option:** None named. To confirm the point above.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE`
- **Source:** Wave 8; record W8-30.

**116. Sage supply requests need review before verification**

- **What is built:** Every Sage supply request is linked as 'REVIEW REQUIRED' and cannot be verified in the pharmacy (Helix) with its placeholder dose; the dose is corrected and verified at the Verify Queue. In record mode there is no 'request supply' step, so after rejecting the placeholder supply the pharmacy has no record-mode way to raise a corrected one.
- **Looser option:** None named.
- **If relaxed:** With the store off, as today, the pharmacy can verify and dispense Sage's placeholder dose.
- **Switch:** `ORB_MED_ORDER_STORE` (record mode)
- **Ruling:** `F-A` (closure)
- **Source:** Wave 8; record W8-31.

**117. Scribe medicine extraction is all-or-nothing**

- **What is built:** For each medicine start Scribe pulls from a note, one prescribed order that needs a pharmacist is created in the same step. If any order or its log entry cannot be written, the whole extraction (chart entries, vitals, documents) is undone and logged, leaving the signed note with nothing charted from it. Extracted medicines are never saved as drafts.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE`
- **Ruling:** `W111` (held)
- **Source:** Wave 8; record W8-32.

**118. The pharmacy order timeline never shows bedside events**

- **What is built:** The pharmacy order timeline never shows bedside events (given, not given), so pharmacy read access does not open up the bedside medication record. A log entry is written before any data is returned (refused if logging fails); a patient outside the user's scope looks not found. Unlinked orders read nothing from the store and are not logged.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on, for orders linked to the order store only
- **Source:** Wave 8; record W8-33.

**119. No dose before pharmacist verification, stat included**

- **What is built:** Strictest option (c): no dose of any linked order, stat included, can be given until a pharmacist has verified the order on the ward record. What the hospital sees at go-live: this switch must stay off until `medorders-5a` lands, so until then a nurse can give a dose of any order not charted by AI before a pharmacist has checked it (AI-charted orders already wait for a pharmacist).
- **Looser option:** `ORB_MED_ORDER_FIRST_DOSE_BEFORE_CHECK` '`stat_urgent`' (option (a)): only the first dose of an order whose recorded priority is stat or urgent may be given before the check (a frequency typed 'STAT' with no priority does not count); that dose is marked on the event, log, record and MAR, and every later dose waits. Strict readings also to confirm: the pharmacist's verification on the ward record is required; orders not linked to the store are exempt; AI-charted orders get no exception; the rule follows the switch, not the link.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_VERIFY_BEFORE_DOSE` (choice set by `ORB_MED_ORDER_FIRST_DOSE_BEFORE_CHECK`, default 'none')
- **Ruling:** `MO-R1` (c)
- **Note:** Release gate from the wave 8 deferred list: `ORB_MED_ORDER_VERIFY_BEFORE_DOSE` must stay off until `medorders-5a` lands. Today an order not charted by AI is verified only when the pharmacy (Helix) verifies a linked supply. Ward-stock and patient's-own medicine orders have no way to be verified until `medorders-5a`, so with the switch on the ward could never give them. Turning it on also stops every adopted, unverified live order from being given until a pharmacist checks it.
- **Source:** Wave 8; record W8-59.

**120. No standard dose times ship**

- **What is built:** Option (a) with no built-in default: no standard dose times ship. The template is refused if copied unsigned; pharmacy and physician-champion signatures are required; a malformed file stops the server at start. Times apply only to linked orders while the lifecycle is on. Frequencies match by exact words, never by interval (TDS is not Q8H). The number of times must equal the frequency's doses a day. A new order's first due time is the first set time at or after it started. Only `medication.prescribe` holders may set or clear an order's times.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_DOSE_TIMES_FILE` or `server/site_config/dose_times.json`, plus `ORB_MED_ORDER_LIFECYCLE`
- **Ruling:** `MO-R8` (a) (`W117`)
- **Source:** Wave 8; record W8-60.

**121. Set dose times at least 2 hours apart**

- **What is built:** Set dose times must be at least 2 hours apart (twice the 60-minute early window, enforced at start). Any frequency more often than every 2 hours (Q1H, Q30min, Q90min) cannot have set times and keeps the worked-out interval schedule.
- **Looser option:** None named. To confirm that hourly set times are not needed.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE` (module constant `services.medorders.schedule.MIN_GAP_MINUTES` 120)
- **Ruling:** `MO-R8`
- **Source:** Wave 8; record W8-61.

**122. Which set time a given dose counts for**

- **What is built:** One given dose counts for exactly one set time: the one at or before it, or the next one if the dose came up to 60 minutes early and was strictly nearer. A tie counts for the earlier time, so the later dose stays due. A dose on time or up to an hour late never hides the next set time.
- **Looser option:** None named. To confirm the tie direction.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`
- **Ruling:** `MO-R8`
- **Source:** Wave 8; record W8-62.

**123. Changing the frequency wording cancels set dose times**

- **What is built:** Any change of an order's frequency to different words (case and spacing ignored), even with the same doses a day (TDS to Q8H), cancels the prescriber's set times in the same step, with an 'amended' event and a log entry (refused if logging fails). The board falls back to the hospital's signed times for the new frequency, or to the worked-out schedule. A respelling ('tds' to 'TDS') keeps them.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (not flag-gated; set times exist only once the lifecycle has been on)
- **Ruling:** `MO-R8`
- **Source:** Wave 8; record W8-63.

**124. How 'same ingredient' is decided**

- **What is built:** How 'same ingredient' is decided: medicines that only share a class word, salt or anion are never one ingredient; a bare class word (vitamin, insulin) matches only itself; a name running into a number does not match (B1 and B12); combinations match their parts; salt forms of one drug match (metoprolol succinate and tartrate); a longer catalogue name counts only when it adds a strength, unit, form or anion.
- **Looser option:** None named.
- **If relaxed:** Not stated for relaxing. Stated costs of the current matching, to confirm (missed warnings): a drug named by its anion in two salts (mycophenolate mofetil and sodium) is not joined; a brand charted bare whose only catalogue row adds a variant word ('Augmentin' against 'Augmentin 625 DUO Tablet') is not matched to its generic by the duplicate rule (the allergy interlock and interaction screen match brands their own way).
- **Switch:** `ORB_MED_ORDER_LIFECYCLE` + `ORB_MED_ORDER_DUPLICATE_RULE`
- **Ruling:** `MO-R6`
- **Source:** Wave 9; record W9-12.

**125. Outpatient duplicates from other visits can be cleared with a reason**

- **What is built:** In the outpatient clinic, a duplicate from another visit (earlier, another clinic's, or unreadable) can be cleared with a written reason (logged) even under 'block', because nobody on this visit can cancel it; a duplicate on the visit being prescribed keeps the hard block. Inpatient orders of a patient with no active admission are left out of the outpatient check (they come from a finished stay while `MO-R7` is held). For a caller that names no visit, a line on any open visit dated today or later keeps the configured rule.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_DUPLICATE_RULE` with `ORB_MED_ORDER_LIFECYCLE`
- **Ruling:** `MO-R6` (`MO-R7` held)
- **Source:** Wave 9; record W9-13.

**126. PRN medicines go through every bedside check**

- **What is built:** An as-needed (PRN) medicine can be given only through the full set of bedside checks (order state, pharmacist checks including `MO-R1`, inpatient guard, allergy interlock, dose guard with its repeat window). The order-time interaction check compares live orders (prescribed, verified and held, PRN included) and unlinked live ward rows, including rows linked to a missing order; drafts, ended orders and outpatient lines are left out.
- **Looser option:** None named. To confirm: held drugs stay in the comparison set, and a ward row left 'active' behind an ended order is not screened (the store is the authority).
- **If relaxed:** Stated for today's behaviour: a PRN dose is given off the record, past the allergy interlock.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`
- **Ruling:** `MO-R3` (a)
- **Source:** Wave 9; record W9-14.

**127. Sage medicine suggestions bind to a product only on a strong match**

- **What is built:** A Sage medicine suggestion is linked to a catalogue product only when the reconciler matches it at 0.8 or above; a match boosted by a partial name binds only when the card names the product whole. Anything else is charted only, flagged on the order, with no pharmacy supply. Safety checks run on the product name, its generic name and the card's words, and any block stops the order. The product is re-read inside the save and must still be confirmed. A check made outside Sage's own pre-check, or an unreadable catalogue, binds nothing.
- **Looser option:** None named.
- **If relaxed:** Not stated for relaxing. Stated gap to confirm: a misspelt name the reconciler matches by spelling at 0.8 or above, with no substring hit, still binds.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`
- **Source:** Wave 9; record W9-15.

**128. Discharge reconciliation reads the order store**

- **What is built:** Display only: the discharge medicine reconciliation reads the order store. For a linked row the order's status wins: an ended order is listed stopped, with how it ended and the reason; a held one is flagged; one still waiting for a pharmacist check it needs is listed unverified (every order while `ORB_MED_ORDER_VERIFY_BEFORE_DOSE` is on); a broken link reads 'ORDER RECORD MISSING'. This stay's unsigned drafts are listed 'NOT SIGNED ... not discharge medications'. Nothing blocks discharge and no order is closed at discharge.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Not named in the bullet (applies to rows linked to the medication order store)
- **Ruling:** None (`R009` and `MO-R7` untouched)
- **Source:** Wave 9; record W9-16.

**129. Doses awaiting a pharmacist are grouped on the round sheet**

- **What is built:** On the medicine round sheet, every dose the bedside will refuse until a pharmacist checks the order is grouped under 'Awaiting pharmacist' with Administer disabled (AI-charted rows and those waiting for the pharmacist check). A first stat or urgent dose allowed before the check stays in its due group. No bedside rule changed.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE` (grouping only)
- **Ruling:** `MO-R1`
- **Source:** Wave 9; record W9-19.

### Pharmacy store

**130. Quarantined, recalled and substandard batches never come back into use**

- **What is built:** No one can release a medicine batch from quarantine back into use. Recalled and NSQ (not of standard quality) batches can never come back.
- **Looser option:** Option (b): release after checks. The checks are written behind `QUARANTINE_RELEASE_ALLOWED`, but nothing in Orb can call them yet (held, per the Deferred list).
- **If relaxed:** Not stated in the source. In plain words: the rule stops a quarantined, recalled or substandard batch being given to a patient.
- **Switch:** `QUARANTINE_RELEASE_ALLOWED` = False (module constant)
- **Ruling:** `GRN-RS2` (option (c))
- **Source:** Wave 3; record W3-11.

**131. Ward returns never go back on the shelf**

- **What is built:** Medicines returned from a ward wait for inspection and never go back into usable stock (option (c), never). Every restock is refused and the screens say the site does not restock.
- **Looser option:** Option (b), written behind the constant, but nothing in Orb can call it yet: restock only when the inspecting pharmacist confirms the seal is intact, the original batch is in date with an expiry recorded, it is not cold chain and not NDPS, the original lot is still 'available' on the dispensing shelf, and the inspector is not the receiver. Units go back into the original lot only.
- **If relaxed:** Not stated in the source. In plain words: the rule stops a returned medicine of unknown condition being dispensed again.
- **Switch:** `RETURN_RESTOCK_ALLOWED` = False (module constant in `pharmacy_stock_service`)
- **Ruling:** `GRN-RS3` (c) (held)
- **Source:** Waves 3 and 9; records W3-12 and W9-20.

**132. A drug's identity cannot be edited once it is in use**

- **What is built:** The pharmacy masters screen refuses any change to a drug's identity (error '`identity_locked`'). The formulary loader refuses to rewrite the identity of a drug that already has orders or stock batches.
- **Looser option:** None named. Identity governance (`grn-6`) was deferred in wave 3 and built in wave 5 (entry 135).
- **If relaxed:** Not stated in the source. In plain words: the rule stops a drug's identity changing under existing orders or stock, so a patient gets the drug that was ordered.
- **Switch:** Always on
- **Ruling:** `GRN-RS1`
- **Source:** Wave 3; record W3-13.

**133. Unclassified, unconfirmed or incomplete drugs cannot be bought, received or moved in**

- **What is built:** A drug cannot go on a purchase order or a goods receipt if it is a draft, retired, inactive or unclassified (blank schedule or NDPS class), its identity is not confirmed, or it has no pack size or GST. This is checked again at every step, including posting. On transfers an unclassified drug does not move at all, and an inactive drug, or one whose identity is not confirmed, never moves into the dispensing store (checked at draft, dispatch and receipt; if it changes in transit the receipt is refused and the units stay in transit). Moving such a drug off the shelf is allowed (`grn-6`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Ruling:** `GRN-R18`
- **Source:** Waves 4 and 5; records W4-24 and W5-30.

**134. Stock screens show no patient details**

- **What is built:** Stock screens show no patient details: a ledger line tied to a patient's order says only "A patient's medicine order".
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 4; record W4-27.

**135. A new drug stays a draft until a second pharmacist confirms it**

- **What is built:** A new drug stays a draft until a different pharmacist confirms its exact identity; the administrator cannot add or confirm drugs; the ruling's 'medical lead' is filled by a second pharmacist (to confirm). An identity correction is allowed only for a confirmed drug never used (no orders, stock, purchase or receipt lines, or medication orders), needs a second pharmacist and is checked again at approval; a drug in use is retired and replaced instead; refused and withdrawn drafts are retired. Ordering screens show only confirmed drugs; that filter is always on, with no effect while every drug is 'active'.
- **Looser option:** None looser named; option (c) is not built because of the offline rule; who fills the ruling's 'medical lead' role is open (to confirm).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`; the identity filter on ordering paths is always on
- **Ruling:** `GRN-RS1` (b)
- **Source:** Wave 5; record W5-23.

**136. Duplicate drug names are refused**

- **What is built:** A drug with a duplicate name is refused; a new drug with the same generic name, strength and form is refused unless 'another brand of the same medicine' is ticked, which the confirming pharmacist then sees.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Ruling:** `GRN-RS1`
- **Source:** Wave 5; record W5-24.

**137. Formulary file loads arrive as drafts**

- **What is built:** While the module is not off, new rows loaded from a formulary file arrive as drafts; a file never rewrites the generic name, form or strength of a confirmed or retired drug.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE` (module not 'off')
- **Ruling:** `GRN-RS1`
- **Source:** Wave 5; record W5-25.

**138. Expiry date typed twice at goods receipt**

- **What is built:** At goods receipt the expiry date is typed twice, both required and matching (422 `expiry_mismatch`); the second box refuses paste and drag-and-drop.
- **Looser option:** None looser named; option (a), a GS1 barcode scan, is deferred until the site has scanners.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Ruling:** `GRN-RS6` (b)
- **Source:** Wave 5; record W5-26.

**139. Expired batches refused; short shelf life needs a second person**

- **What is built:** An expired batch is refused with no override; a batch with under 90 days of shelf life posts only with `pharmacy_store.approve` from someone other than the receiver, with a reason; judged on the site's date and checked again at posting.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_MIN_SHELF_LIFE_DAYS` (90)
- **Ruling:** `GRN-R16`
- **Source:** Wave 5; record W5-27.

**140. A batch must keep one expiry and one MRP**

- **What is built:** A batch already on file with a different expiry is refused (409); the same batch in the same store at a different MRP is refused (409); one batch on two lines must agree.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 5; record W5-29.

**141. Transfers use only available, in-date batches**

- **What is built:** Transfers plan only from batches that are available, in date and have stock (by the site's date); a receipt never merges into a batch with a different status (409 `lot_status_conflict`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Ruling:** `GRN-RS2`
- **Source:** Wave 5; record W5-35.

**142. Units from a batch that changed in transit arrive held**

- **What is built:** If a source batch changed status while in transit, its units arrive held (recalled stays recalled, NSQ stays NSQ, otherwise quarantined), never available; units from a closed source batch also arrive quarantined.
- **Looser option:** Your choice: let units from a closed source batch arrive available.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 5; record W5-38.

**143. A recall or NSQ holds the whole batch everywhere**

- **What is built:** A recall or NSQ (not of standard quality) holds the whole batch (same drug and batch number, ignoring capitals and spaces, any expiry, any store): it is not planned or offered, a dispatch naming it is refused as out of date (`plan_stale`), and a receipt lands it held.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 5; record W5-39.

**144. What happens when an NDPS register entry fails**

- **What is built:** When an NDPS register entry fails (it cannot be written, the register is not opened yet, or the balance would go negative): 'block' refuses the dispense; 'record' lets the dispense through with a variance plus a security audit record in the same step, and refuses it if either cannot be written. 'record' covers dispenses only; every other movement blocks in both modes. You need to choose the mode before go-live.
- **Looser option:** 'record' (the dispense goes ahead and the failure is logged as a variance with a security audit record).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_NDPS_ON_FAILURE` ('block' or 'record'; no mode ships)
- **Ruling:** `GRN-RS7` (held)
- **Note:** In 'block' mode a dispense of a controlled drug whose register entry fails is refused, so a patient's controlled painkiller can wait until the register is fixed (see entry 145).
- **Source:** Wave 7; record W7-46.

**145. Opening the NDPS register for stock already on the shelf**

- **What is built:** Only the first movement into a store holding none of the drug opens the register at zero by itself. Stock already on the shelf needs a witnessed opening or a reclassification opening; until then a movement is refused ('block') or recorded as a variance ('record', dispenses only).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_NDPS_REGISTER` + `ORB_PHARMACY_NDPS_ON_FAILURE`
- **Ruling:** None (`grn-13`)
- **Note:** Wave 7 deferred says to create the stores and adopt the old stock before switching the register on. Until each controlled drug already on the shelf has its witnessed opening, 'block' mode refuses to dispense it.
- **Source:** Wave 7; record W7-47.

**146. NDPS screens are for pharmacists only**

- **What is built:** Every NDPS screen needs the NDPS permission (pharmacists only). A patient outside the user's department shows as 'A patient outside your department'. A read naming patients writes an access record first (fail-closed). The CSV export writes a security record before the file is sent.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_NDPS_REGISTER` + `ORB_PHARMACY_NDPS_ON_FAILURE`
- **Source:** Wave 7; record W7-50.

**147. A dispensed batch must be the right drug and usable**

- **What is built:** A batch the pharmacist picks must be of the drug being dispensed (the substitute, else the prescribed drug), checked under the write lock. Expired, undated, quarantined, recalled, not-of-standard-quality, awaiting-inspection, ward stock, sub-store and returns-quarantine batches, and an ordinary drug's batch in the narcotics vault, can never be dispensed. Too few units is refused, never split across batches; with no batch named, a quantity no single batch holds is refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 8; record W8-28.

**148. High-alert drugs and concentrated electrolytes are never ward stock; none of these, nor NDPS drugs, are cart stock**

- **What is built:** No ward ever keeps as stock a drug that matches a high-alert dose or route rule citing the ISMP high-alert list (the drugs most likely to cause serious harm if misused: insulin, heparin and low molecular weight heparins such as enoxaparin, warfarin, digoxin, morphine, fentanyl, hydromorphone, oxycodone, potassium chloride, midazolam, propofol, methotrexate, vinca alkaloids) or a concentrated electrolyte (potassium chloride, phosphate and acetate, magnesium sulphate, hypertonic saline, sodium chloride above 0.9 per cent). There is no 'unless named' exception. This is checked when an indent is raised, decided, planned and dispatched; a drug barred while on its way lands quarantined. Crash carts follow the same list (wave 9): a cart's stock list never takes a drug no ward store may hold (unclassified, NDPS, ISMP high-alert, concentrated electrolyte, inactive, not confirmed). So magnesium sulphate, potassium chloride and NDPS drugs (diazepam, midazolam, morphine) cannot be cart stock and are dispensed per patient. Adrenaline, atropine, amiodarone, adenosine, calcium, sodium bicarbonate and dextrose are not barred. A cart's list cannot change at all while ward stock is off. The ban on NDPS drugs as ward stock is its own rule (`GRN-R14`, entry 149). The 'unless named' option below would not lift it.
- **Looser option:** An 'unless named' exception, so a barred drug could be ward or cart stock when the store's list names it (deferred in `W8-F`, with medical-lead sign-off of the per-store list; not built).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` (under `ORB_PHARMACY_STORE`)
- **Ruling:** `GRN-RS5` (b)
- **Note:** Known gap (`W9-F`, see Also open): when a drug is reclassified as NDPS or becomes barred, lots already on a crash cart are not held, so a barred drug can stay on a cart until someone removes it.
- **Source:** Waves 8 and 9; records W8-35 and W9-25.

**149. NDPS drugs are never ward stock**

- **What is built:** Narcotic and psychotropic (NDPS) drugs are never ward stock. Drugs not yet classified, inactive or not confirmed are refused too.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` (under `ORB_PHARMACY_STORE`)
- **Ruling:** `GRN-R14`
- **Source:** Wave 8; record W8-38.

**150. Ward stock changes no bedside rule**

- **What is built:** Ward stock changes no bedside rule: a dose still needs an active order on the bedside medication record. Ward batches can never be dispensed by the pharmacy system (Helix).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` (under `ORB_PHARMACY_STORE`)
- **Ruling:** `GRN-RS5` (b)
- **Source:** Wave 8; record W8-37.

**151. Mixed-status batches on a ward take the stronger hold**

- **What is built:** When a ward receives a batch it already holds under another status (same drug, batch and expiry), both lots end up under the stronger hold (recalled, then not of standard quality, then quarantined, then available), set by the receiving nurse with a note naming the transfer, or the arriving units land held. Nothing held is made available. Between pharmacy stores the existing refusal stays.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` (under `ORB_PHARMACY_STORE`)
- **Ruling:** `GRN-RS5`; `GRN-RS2`
- **Source:** Wave 8; record W8-44.

**152. Limits on ward returns**

- **What is built:** Only a 'delivered' order can be returned. The most that can be returned is what that order's dispense took from that batch, minus every unit already returned in any state, checked under the write lock. Returned units wait in returns quarantine as 'pending inspection' and never count as dispensable. The return, its stock movement and a log entry are saved together (refused if logging fails). Another ward's order or return looks not found, after a refusal log entry.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 9; record W9-23.

**153. Ward returns screens need nurse or pharmacy rights**

- **What is built:** Every ward-returns screen needs `ward_return.record` (the nurse, own department) or `pharmacy_store.write`. The administrator, with `pharmacy_store.read` alone, is refused because every answer names a patient. Every read naming patients writes a log entry (refused if logging fails).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 9; record W9-24.

**154. Crash carts follow the ward stock rules**

- **What is built:** Crash carts follow the ward stock rules unchanged: filled only by an indent decided by someone who did not raise it, received by a `ward_stock.receive` holder of the cart's department who did not send it, never by a free transfer, never dispensed by the pharmacy system (Helix). A dose from a cart still needs an active order on the bedside medication record.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE` + `ORB_PHARMACY_WARD_STOCK`
- **Ruling:** `GRN-RS5` (b); `GRN-R14`
- **Source:** Wave 9; record W9-26.

**155. A full cart check counts every batch**

- **What is built:** A full cart check must count every batch the cart holds, including expired and held ones (refused otherwise), ends with a new seal number, and needs a note when the count differs. Only one check can wait per cart.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: a check that skips expired or held batches could pass a cart that holds drugs no one should give.
- **Switch:** `ORB_PHARMACY_STORE` + `ORB_PHARMACY_WARD_STOCK`
- **Source:** Wave 9; record W9-29.

**156. When cart seal and full checks fall due**

- **What is built:** A seal check is due when no check of any kind was done in 24 hours. A full check is due when none is on record, the last is older than 30 days, a later seal check found the seal broken, missing or a different number, the pharmacy asked for a recount, or stock was received at or after the last full check.
- **Looser option:** None named. Making the intervals site settings is deferred.
- **If relaxed:** Not stated in the source. In plain words: longer gaps between checks mean an expired or missing emergency drug is found later.
- **Switch:** Module constants `pharmacy_crash_cart_service.SEAL_CHECK_DUE_HOURS` (24) and `FULL_CHECK_DUE_DAYS` (30)
- **Ruling:** None (product defaults)
- **Note:** Known gaps (wave 9 deferred): nurses have no screen to record a cart check yet, and expired or held units cannot be taken off a cart's books.
- **Source:** Wave 9; record W9-31.

**157. Ward stock is used only when the order says ward stock**

- **What is built:** A dose given on the ward is taken off ward stock only when its medication order says the supply is ward stock, which only the prescriber can set. Orders with no supply source, pharmacy supply or the patient's own medicines are never taken off ward stock; those wards keep topping up by count (`GRN-R15`). `GRN-RS5` and `MO-R12` are the nearest rulings.
- **Looser option:** Also take off ward stock any dose of a formulary drug the ward stocks that did not come with a pharmacy supply unit.
- **If relaxed:** The source gives the reason for the strict reading: a patient should never be wrongly tied to a ward batch in a drug recall.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` with `ORB_PHARMACY_STORE`
- **Ruling:** `GRN-RS5`, `MO-R12`, `GRN-R15`
- **Source:** Wave 10; record W10-15.

**158. Ward stock can go negative, flagged, never blocking a dose**

- **What is built:** Design rule: flag, never refuse. If the ward has no in-date unit on the books, the dose is still booked against the ward's own batch of that drug that is already at or below zero (in-date first, else latest expiry), taking it negative. The negative count shows on the ward stock screen and the nurse's ward stock section and in the reconcile report, and a recall trace marks that use as batch not certain. Held batches, and expired batches that still hold units, are never drawn down. If there is no such batch nothing is booked and the medication record logs it. The next delivery of that batch makes up the gap.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` with `ORB_PHARMACY_STORE`
- **Source:** Wave 10; record W10-16.

**159. Ward stock booking never blocks or delays the dose**

- **What is built:** Booking ward stock never blocks or delays the dose. It runs after the dose is saved, in the same request, in its own separate save. Any failure (stock error, audit outage, a nested session where one save is opened inside another, locked database) undoes only the stock booking, is logged without patient details, and leaves the dose, its audit row and what the nurse sees unchanged. The stock movement and its audit row save together or not at all. The one wait left is the database's busy timeout if another save holds the lock.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` with `ORB_PHARMACY_STORE`
- **Source:** Wave 10; record W10-18.

### Laboratory

**160. No critical limits ship; a critical needs an approved limit**

- **What is built:** No critical (panic) limits come preloaded. Approving one needs a lab master user, a note, the named clinicians and a confirmation tick, and a limit is never edited in place. A result is flagged critical (HH or LL) only against a pathologist-approved limit, decided at entry and at each edit (the limit approved now, age at collection), at reporting precision. Equal to the limit is not critical; a bound wholly beyond the limit is critical; HH and LL outrank H and L. No critical alert is raised without an approved limit, re-checked when raised. So nothing raises a critical alert until the laboratory approves its limits. In wave 6, before notifications existed (`lis-15`), the Validate screen told the laboratory to phone the ward and, before authorisation, to say the value is preliminary.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: the rule stops a critical result being judged against a limit nobody approved. Go-live of `lis-4` and `lis-6` is pending `LIS-R1`.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R1` (a)
- **Source:** Waves 3, 6 and 7; records W3-18, W6-30 and W7-56.

**161. No reference ranges ship; H and L only against an approved range**

- **What is built:** No reference (normal) ranges come preloaded. Approving one needs a lab master user, a note and a tick. A test's unit or number of decimals cannot change while a range is current. A result is flagged High or Low only against a range a pathologist approved; otherwise it shows 'Reference range not configured' with no flag.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: the rule stops results being flagged against unapproved ranges, or against a range that no longer fits the unit. Go-live of `lis-4` and `lis-6` is pending `LIS-R2`.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R2` (a)
- **Source:** Waves 3 and 5; records W3-19 and W5-40.

**162. 12-digit lab sample numbers with a check digit**

- **What is built:** Lab sample numbers are 12 digits: the hospital date, a 5-digit count and a check digit that catches mistyped numbers. When a day's count passes 99999 the system refuses loudly instead of starting again from 1.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: the rule stops two samples sharing a number or a mistyped number matching another sample. Go-live is blocked on `LIS-R13`.
- **Switch:** `ORB_LAB` (go-live blocked on this ruling)
- **Ruling:** `LIS-R13`
- **Source:** Wave 3; record W3-21.

**163. Lab intake takes only new orders and never mixes patients in one tube**

- **What is built:** Only orders activated after the lab was last switched on are taken in. Orders share one sample tube only when they are for the same patient, specimen, container and priority, the tube is still planned and unlabelled, the same test is never added twice, and a STAT (urgent) test is never put with a routine one.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: the rules stop one tube carrying another patient's tests, a doubled test, or an urgent test waiting behind a routine one.
- **Switch:** `ORB_LAB`
- **Source:** Wave 3; record W3-23.

**164. Department staff see only their own patients' lab exceptions**

- **What is built:** Department staff see only the lab exceptions (orders the lab could not take in) for patients in their own department. Rows whose department cannot be worked out are hidden from them.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 3; record W3-24.

**165. Only lab staff can match or discard an unmatched result**

- **What is built:** Only lab technicians and pathologists (the `ingest.reconcile` permission) can attach an unmatched incoming result to a patient or discard it. Admin sees counts only.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: the rule keeps the matching of stray results with lab staff. Go-live is blocked on `LIS-R17`.
- **Switch:** `ORB_RESULT_QUARANTINE` (go-live blocked on this ruling)
- **Ruling:** `LIS-R17`
- **Source:** Wave 3; record W3-25.

**166. Two identifiers must match to attach an unmatched result**

- **What is built:** To attach an unmatched result, two of hospital number, name and date of birth must match both the incoming message and the patient's record. Any contradiction refuses. A message hospital number that now belongs to another patient refuses outright. A name plus date of birth shared with another record refuses.
- **Looser option:** The design's narrower rule (name plus a hospital number found anywhere in the patient section of the incoming lab message) is not built; your choice. The source does not say whether it is looser or stricter.
- **If relaxed:** Not stated in the source. In plain words: the rule stops a lab result being attached to the wrong patient.
- **Switch:** `ORB_RESULT_QUARANTINE`
- **Source:** Wave 3; record W3-26.

**167. A decision on an unmatched result is final**

- **What is built:** Once a queued result is attached or discarded, that decision never changes. Switching the feature off keeps the queue reachable while rows remain in it.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: the rules stop a decided match being changed later and stop stray results being stranded when the switch goes off.
- **Switch:** `ORB_RESULT_QUARANTINE`
- **Source:** Wave 3; record W3-28.

**168. A corrected result replaces only one clear earlier result**

- **What is built:** A corrected result ('C') replaces only one clear earlier final result that is still live, with the same patient, test and sub-test, the same sending lab and feed, and the same order, and never one from its own message. Unclear cases are stored alongside, with an audit row (`observation_correction_not_applied`) that must be written. Corrections that arrive through the JSON or FHIR feeds, not as HL7 lab messages, never replace anything.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: the rule stops a correction overwriting the wrong result or the wrong patient's result.
- **Switch:** `ORB_RESULT_CORRECTIONS`
- **Ruling:** None (epic `lis-20`)
- **Source:** Wave 3; record W3-29.

**169. Attaching a result after its correction is already on the chart**

- **What is built:** If a stray result is attached after its correction is already on the chart, it is stored already marked as replaced only in the clear-cut case. Every other case is refused and audited.
- **Looser option:** None named. Cases beyond the clear-cut rule are listed as deferred.
- **If relaxed:** Not stated in the source. In plain words: the rule stops an out-of-date result showing as current.
- **Switch:** `ORB_RESULT_QUARANTINE` and `ORB_RESULT_CORRECTIONS`
- **Source:** Wave 3; record W3-30.

**170. No back-dated sample collection times**

- **What is built:** Staff cannot enter an earlier sample collection time: any typed collection time is refused. If the site sets a limit, it is capped at 120 minutes, needs a time zone, may be at most 60 seconds in the future, never earlier than when the lab order was activated (placed), and needs a reason when earlier than now.
- **Looser option:** A site back-dating limit of up to 120 minutes through `ORB_LAB_COLLECTION_BACKDATE_MINUTES`.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_COLLECTION_BACKDATE_MINUTES` (ships 0)
- **Ruling:** `LIS-R6`
- **Note:** Moved up on the checker's advice. The collection time decides the patient's age for the reference range (entries 173 and 208), the stability check (entry 181) and the delta window (entry 189), so a wrong time can give a wrong flag.
- **Source:** Wave 4; record W4-29.

**171. Sample labels print only after UHID and name both match**

- **What is built:** Sample labels print only after the typed UHID and name both match the patient; a patient with no UHID can never be confirmed; one patient per print; collection checks both again and needs a printed label; refusals are audited and never show the stored value.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R6`
- **Source:** Wave 4; record W4-30.

**172. Lab-managed orders cannot be moved or typed into by hand**

- **What is built:** Once the lab manages an order, nobody can move it by hand (except back to 'active') or type result text into it (409). Each order move locks the record while it saves, so if two people move the same order at once, only one succeeds (`lis-7` order guard).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (keyed on `lab_order_items`, not on `ORB_LAB`)
- **Source:** Wave 4; record W4-32.

**173. Which reference range applies with no date of birth or sex**

- **What is built:** With no date of birth only an all-ages range is used; with no or unrecognised sex only a 'sex any' range; the stored age field is never used; age is worked out at the moment of collection.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R7` (a)
- **Source:** Wave 5; record W5-41.

**174. The range used is the one approved at result entry**

- **What is built:** The range applied is the one approved when the result is entered, decided again when the result is edited (the builder's choice). To confirm.
- **Looser option:** Named alternative: use the range in force when the sample was collected. The source does not say whether this is looser or stricter.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-42.

**175. How results are compared with range limits**

- **What is built:** A value and its range limits are compared at the test's reporting precision, rounding half up; a value equal to a limit is in range; results with a comparator (such as 'less than 0.01'), coded and text results are never flagged.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-43.

**176. Too many decimals is refused, never rounded**

- **What is built:** A value with more decimals than the test reports is refused, never rounded; the same for comparator bounds.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-44.

**177. Results like 'less than 0.01' are never plotted or flagged**

- **What is built:** A result with a comparator (such as 'less than 0.01') stores no number, so nothing plots it, delta-checks it or flags it.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-45.

**178. Coded results must be on the test's list**

- **What is built:** A coded result must be one of the test's listed values and is stored in the catalogue's spelling; if the list cannot be read, every value is refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-46.

**179. Edits before validation do not need a reason**

- **What is built:** A result can be edited before validation only while it is 'entered'; a reason is optional; the before and after are kept.
- **Looser option:** None looser; the stricter option named is to require a reason.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-47.

**180. A result edit is refused if the result changed underneath**

- **What is built:** Every result edit must state what the editor saw; any difference from the stored result refuses the edit (409 `result_changed`), even when the new value equals the current one; the current result is shown only after an access audit row is written (fail-closed). To confirm.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-48.

**181. A sample past its stability time needs 'Process it anyway'**

- **What is built:** A sample past its stability time is refused unless someone picks 'Process it anyway' with a reason; rejecting it as delayed is offered first. No stability times ship.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-49.

**182. Sample receipt checks the barcode and that it was collected**

- **What is built:** A misread barcode check digit is refused before any lookup; a sample not yet collected cannot be received; unknown and other-department samples get the same 'not found' answer.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-50.

**183. Rejecting a sample rejects every unreported test on it**

- **What is built:** Rejecting a sample needs an active reason code ('other' needs a note); every unreported test on the tube is rejected and unreleased results withdrawn; refused once any result is released; the order stays in progress with an open 'rejected, no decision' exception.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-51.

**184. The ward hears of a rejection in a case room, else 'Phone the ward'**

- **What is built:** The ward is told about a rejection only after it is saved, and only in a case room a clinician can see; with no room, a closed room (patient discharged) or a failed post, Orb records '`ward_notice_failed`' and the screen says 'Phone the ward'; the rejection is never undone.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R19`
- **Source:** Wave 5; record W5-52.

**185. A recollection always gets a new sample number**

- **What is built:** A recollection always gets a new sample number linked to the old one; recollecting is refused while the module drains; cancelling needs a reason and still works while draining.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-54.

**186. Department accounts see only their own patients' lab lists**

- **What is built:** Lab lists are hospital-wide only for accounts with no department; a department account sees only its own patients, filtered before the list limit; each list read is audited with the patient ids returned.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-55.

**187. Strict analyser code mapping**

- **What is built:** An analyser code maps only to an active laboratory test (never a panel, ward point-of-care or switched-off test); no characters that would break an HL7 lab message (such as `|`, `^`, `~`, `\` and `&`); the conversion factor is exact to thousandths and above zero (finer refused); text and coded tests take factor 1 and no unit; every mapping needs a note, retiring needs a reason, and mappings are never deleted.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R18`
- **Source:** Wave 5; record W5-61.

**188. A different signatory must release or amend a result**

- **What is built:** The pathologist who releases a result must not have entered, edited, given the delta reason for, or technically validated it, at any time, even before a send-back; otherwise it is refused and nothing is released. Entry and technical validation may be the same person. An amendment is proposed by one signatory and applied only when a different signatory confirms. Withdrawal is one step by any signatory, whatever the setting. With the setting off, a technician still never releases (pathologists only), and every release where the signatory took part is recorded and counted as one-person.
- **Looser option:** Set `ORB_LAB_REQUIRE_DISTINCT_AUTHORISER` off: a signatory may release a result they worked on, and one signatory amends in one step. Each such action is recorded as one-person.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_REQUIRE_DISTINCT_AUTHORISER` (default on: anything but an explicit False is on)
- **Ruling:** `LIS-R5` (held)
- **Note:** From the checker, reading wave 6 deferred: the one-person count is recorded per result but not reported anywhere yet, so with the setting off nobody would see the total.
- **Source:** Waves 6 and 7; records W6-27 and W7-62.

**189. A big change from the last result needs a reason before validation**

- **What is built:** When a result changes a lot from the patient's earlier one (a delta flag), the technologist must give a reason before technical validation. It is flagged at or beyond the threshold; a change from an earlier 0 is beyond any percent; a threshold with no time window compares any earlier result; simulated earlier rows are left out. The check is rerun at the reason and at validation, and a changed value clears the reason. An amendment that trips the delta check needs the delta reason too, and a confirm that newly flags must be cancelled and proposed again. No thresholds ship, so nothing is flagged until the laboratory sets them.
- **Looser option:** Flag only when the change is more than the threshold, not when it equals it. The no-window rule is also marked to confirm; no alternative to it is named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R9` (a)
- **Source:** Waves 6 and 7; records W6-28 and W7-64.

**190. No delta check when the earlier result cannot be compared**

- **What is built:** When the newest earlier result cannot be compared (it is a bound, text, another unit or outside the window), or the current value is a bound, no delta comparison is made and the result says why.
- **Looser option:** Named alternative: look past it to an older numeric result. The source does not say whether this is looser or stricter.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R9` (a)
- **Source:** Wave 6; record W6-29.

**191. No automatic release of lab results**

- **What is built:** No automatic release: every result needs a person's technical validation and a signatory's authorisation.
- **Looser option:** None named beyond autoverification itself, which this ruling rules out.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R8` (a)
- **Source:** Wave 6; record W6-31.

**192. A release is never blocked by the order's state**

- **What is built:** A release is never blocked by the order's state: an order moved outside the laboratory still receives the result, and an open 'order state conflict' exception is written for staff to sort out, in the same step.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 6; record W6-33.

**193. A release names one sample and goes through whole or not at all**

- **What is built:** An authorisation names one sample, and every named result must be on it, technically validated and unchanged since the screen loaded, or nothing is released. No readable collection time means no release. Validation batches go through whole or not at all. Refusals for an out-of-date screen do not show the current value.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 6; record W6-34.

**194. Only signatories can send a result back**

- **What is built:** Only signatories (`lab.authorise`) can send a result back, with a reason. It returns to 'entered' with its validation cleared and goes back on the worklist with who and why. The earlier validator still counts as having taken part, so cannot later release it.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 6; record W6-35.

**195. Critical values go to the ward at technical validation, marked preliminary**

- **What is built:** A critical value is raised and posted to the ward at technical validation, marked 'Preliminary, not yet authorised'. The chart still shows released values only. Release raises one only for a result never raised before.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB` (go-live held on `LIS-R20`)
- **Ruling:** `LIS-R4` (a)
- **Source:** Wave 7; record W7-55.

**196. A lab callback closes a critical; once the ward card is live, so does the ward's in-app read-back**

- **What is built:** Only a laboratory callback closes a critical: recipient's name, role from a fixed list, method phone or in person, and a read-back. A Relay post or reply never closes one. No auto-close and no 'closed, unreachable'. An unanswered call is logged as an attempt and keeps it open.
- **Looser option:** None named; the bullet rules out auto-close, closing on a Relay reply and '`closed_unreachable`'.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R3`
- **Note:** Wave 8 adds a second way to close a critical: the ward's in-app read-back (entry 211, `LIS-R3` (ii)), live only with `lis-16`.
- **Source:** Wave 7; record W7-57.

**197. No escalation ladder ships for critical results**

- **What is built:** No escalation ladder ships: a critical is due at once and the screen says phone the ward. A written `[lab.critical]` setting with no tiers, a blank or unknown key, or a non-last tier without a timeout stops the server starting. Undelivered makes the tier due at once; each tier posts once; the top tier gets exactly one retry; the last tier is due from the moment it is reached; a failed post never pushes a due time later.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB` (site config `[lab.critical]` tiers)
- **Ruling:** `LIS-R3`
- **Source:** Wave 7; record W7-58.

**198. Discharge and transfer never close a critical**

- **What is built:** Discharge and transfer never close a critical notification. A discharged patient's critical is not posted into the closed case room; it is recorded as case room closed and due at once.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R19` (c)
- **Source:** Wave 7; record W7-59.

**199. Critical posting go-live is blocked on `LIS-R20`**

- **What is built:** Orb can post a critical lab result to the ward, presented as the laboratory's own message, behind the lab switch. That post and the Critical results screen cannot go live until `LIS-R20` is ruled. `LIS-R20` asks whether the lab's critical post and the ward's critical card count as Orb making a clinical judgement under the held `R023` (the intended-use statement that decides Orb's medical device class), or only pass on the lab's own flag, like a phone call. So at go-live, until it is ruled, the ward gets no critical-result post or Critical results screen from Orb.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R20` (held), `R023`
- **Note:** The wave 8 notes add that `lis-16` and `lis-22` are held for go-live on `LIS-R20`, `LIS-R3`, `LIS-R1` and `LIS-R22`.
- **Source:** Wave 7; record W7-60.

**200. Every amendment and withdrawal posts a case-room notice**

- **What is built:** Every applied amendment and withdrawal posts a case-room notice; an amended value crossing a limit goes through the critical flow; a wrong-patient withdrawal prompts a NABH patient-identification report while NABH is on. A failed notice never undoes the change.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB` (NABH prompt only while `ORB_NABH` is on)
- **Ruling:** `LIS-R10` (a)
- **Source:** Wave 7; record W7-61.

**201. A withdrawn result is marked in error and never closes a critical**

- **What is built:** A withdrawn result is marked 'entered in error' on the chart and the test is cancelled with the reason; a resulted order is never reopened (the laboratory gets an exception instead). Withdrawing never closes a critical: open criticals record 'withdrawn', or the latest closed one is reopened at tier 1, closing only on a callback that names the withdrawal.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 7; record W7-63.

**202. Criticals keep working while the lab module drains**

- **What is built:** Criticals, callbacks, the overdue sweep, amend and withdraw keep working while the laboratory module is being switched off (draining), and open criticals keep it draining.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R21`
- **Source:** Wave 7; record W7-65.

**203. A changed critical value is never silent**

- **What is built:** A confirmed critical whose result is re-validated or amended to non-critical is reopened at tier 1 (ladder restarted, acknowledgement cleared) and posted. An open critical whose value changes keeps its tier and due time, posts the change at once, and its 'value told' becomes the new value. A confirmed critical re-validated to another critical value raises a new notification naming the earlier value. Every result is compared with the value told on its latest notification, and a difference is never silent.
- **Looser option:** None named.
- **If relaxed:** The source states the aim that a difference is never silent: relaxing it risks the ward not hearing about a changed critical value.
- **Switch:** `ORB_LAB`
- **Note:** This covers Orb's own lab. For outside labs, a correction to a normal value is not posted at once (see Also open).
- **Source:** Wave 7; record W7-66.

**204. A callback is refused if the value changed since the screen loaded**

- **What is built:** Stale-screen guard: the value told to the ward is required on every callback and attempt; if the value changed since the screen loaded, the callback is refused with nothing written (value only: changed and changed back matches). On screen, the read-back tick belongs to one value and one change revision, and any change clears the whole callback form.
- **Looser option:** To confirm: clearing the typed name and role is wanted, not just the read-back tick. The looser option is to clear only the tick.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 7; record W7-67.

**205. Unauthorised tests print 'Pending' with no value**

- **What is built:** On the lab report, tests not yet authorised print 'Pending' with no value anywhere in the answer or the print. If nothing is released, there is no report.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R4` (a)
- **Source:** Wave 7; record W7-68.

**206. What amended and withdrawn results print**

- **What is built:** An amended result prints its current and every earlier released version with who, when and why, marked 'Amended report'. A withdrawn result never prints a value again; once any version is withdrawn, no value, unit, flag, range or amendment reason prints for any version of that test.
- **Looser option:** None named. Two points to confirm: reprinting superseded values (ISO 15189, the lab accreditation standard), and dropping amendment reasons on a withdrawn chain.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R10`
- **Note:** Known gap (wave 7 notes): the lab desk's reported results screen still shows a withdrawn value and the earlier values in that chain. Only the report and print hide them.
- **Source:** Wave 7; record W7-69.

**207. Interim report wording**

- **What is built:** The report says 'Interim report: N tests not yet reported' while any test is pending or rejected; a rejected test prints 'Not reported: sample rejected'.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R16` (a)
- **Source:** Wave 7; record W7-70.

**208. Age on the lab report is age at collection; date of birth never printed**

- **What is built:** Age on the report is age at collection, from the date of birth on the hospital's local day (years from age 2, months below that, days below a month); the stored age field is never used; 'Not recorded' when missing; the date of birth is never printed or returned. Flags are spelled out in words only for H, L, HH, LL or A.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R7`, `LIS-R2`
- **Source:** Wave 7; record W7-71.

**209. Who can read lab reports and turnaround lists**

- **What is built:** A lab report needs lab or order read rights; department accounts see only patients in scope (otherwise 'not found', identical to an unknown sample, and audited). The lab list and turnaround need lab read rights. Pharmacist, receptionist and cashier are refused.
- **Looser option:** None named. Three points to confirm: the phlebotomist reads full reports; the administrator reads reports and the turnaround overdue list hospital-wide; an exact UHID lookup matching more than one record returns nothing. Wave 7 deferred also leaves to you whether an outpatient clinic may read its outpatient's lab report through the clinic's visit-based access.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 7; record W7-72.

**210. Outside lab criticals raise only on the sending lab's own flag**

- **What is built:** For results from an interfaced outside lab, only the sending lab's own critical flag (HH, LL or AA) raises a critical alert; Orb never works out a flag itself. Only final or corrected results raise; a preliminary HH raises nothing. Bedside monitor readings, simulated rows, back-filled history and results for unmatched patients never raise.
- **Looser option:** None named. A stricter option is named: also raise on preliminary results.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_CRITICAL_INTERFACED` (works without `ORB_LAB`)
- **Ruling:** `LIS-R22` (a)
- **Note:** Known gap: a result attached later from the unmatched results queue raises no critical alert (see Also open).
- **Source:** Wave 8; record W8-46.

**211. The ward can close a critical in the app by read-back**

- **What is built:** The ward can close a critical result in the app by reading it back, in both lanes. It needs the recipient's name, a read-back confirmation and the value the card showed; if the value changed since, nothing is written. Method is in-app or phone (the outside lab's own call, logged by the ward). The read-back is a ticked confirmation naming the value. A Relay reply never closes one.
- **Looser option:** None named. A stricter option is named: the nurse types the value back.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB` or `ORB_LAB_CRITICAL_INTERFACED`
- **Ruling:** `LIS-R3` (ii)
- **Note:** This loosens entry 196: a lab phone call is no longer the only way to close a critical.
- **Source:** Wave 8; record W8-47.

**212. Open interfaced criticals hold the lab from switching off**

- **What is built:** If the interfaced-criticals switch is turned off while interfaced criticals are open, they count as open lab work: the lab module winds down, the card, acknowledgement and reminder sweep stay reachable, and the compliance check fails until they are closed. While the switch is on they never hold up the lab winding down.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_CRITICAL_INTERFACED`
- **Ruling:** `LIS-R21`
- **Source:** Wave 8; record W8-48.

**213. No repeat critical alert within 24 hours for the same value**

- **What is built:** Product default: no second alert for one observation, or for the same patient, test, sending lab, order and value within 24 hours. A different value on the same order always raises a new alert naming the earlier value.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_CRITICAL_INTERFACED` (`lab_critical_service.INTERFACED_DEDUPE_HOURS` 24)
- **Source:** Wave 8; record W8-49.

**214. What the ward's critical result card shows**

- **What is built:** The ward's critical result card shows only the lab's value, the lab's own flag, the agreed limit (or the sending lab's reference range), whether it is preliminary, and words about change. No timer, due time, score or severity. The acknowledgement is the only action on the otherwise read-only doctor home screen and appears only with a switch on.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB` or `ORB_LAB_CRITICAL_INTERFACED`
- **Ruling:** `LIS-R20` (framing)
- **Source:** Wave 8; record W8-51.

### Patient portal

**215. Critical results reach the portal only by a doctor's release**

- **What is built:** Builder's strict default: a critical result (marked critical, HH or LL) never reaches the portal by the delay; only a doctor's release shows it, printed 'Critically high' or 'Critically low' in plain words. The release log records which critical results were released.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_PORTAL_REPORTS` (with `ORB_LAB_PORTAL_DELAY_HOURS`)
- **Ruling:** `LIS-R11` (c)
- **Source:** Wave 8; record W8-53.

**216. Only doctors in the department can release a report to the portal**

- **What is built:** Only `discharge.publish` holders (doctors and clinicians) can release a report to the portal, and only for patients inside their department (others look not found; nurses, pharmacists and lab staff are refused). The doctor must name exactly the report's currently released results; anything else is refused with nothing written. A release covers only the versions it names.
- **Looser option:** None named here (the reuse of `discharge.publish` is a separate item for you, W8-70 (entry 391)).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_PORTAL_REPORTS`
- **Ruling:** `LIS-R11` (c)
- **Source:** Wave 8; record W8-54.

**217. The portal never shows a replaced value as current**

- **What is built:** The portal never shows a replaced value as current. An amended test shows no value until its new version is out. Earlier values are listed only if the patient already saw them, and none at all if any version in the chain was withdrawn. A withdrawn test reads 'Withdrawn by the laboratory' with no value. Reasons for amending or withdrawing never reach the patient.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_PORTAL_REPORTS`
- **Ruling:** `LIS-R10`
- **Source:** Wave 8; record W8-56.

**218. Patients see only their own released results**

- **What is built:** Patients see their own record only, worked out from their login; another patient's sample looks the same as an unknown one and the attempt is logged. Pending, rejected and held tests all read 'Not available here yet'. Hidden: the patient details block, ward and bed, who handled the tube, critical-call logs and anything not released. Shown: laboratory comments and signatories with registration numbers.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_PORTAL_REPORTS`
- **Ruling:** `LIS-R11`
- **Source:** Wave 8; record W8-57.

### Platform and audit

**219. Front desk, ward clerk and lab accounts are locked out of Relay**

- **What is built:** Every hospital role listed in `roles.ROLE_FEATURE` that lacks `relay.read` is shut out of Relay. Today that is receptionist, ward clerk, lab technician, pathologist and phlebotomist. They cannot list, read, join, type in or send to any Relay channel, on screen or over the live socket. Any new role added to that list gets the same lock. Later waves must not give them Relay without a ruling.
- **Looser option:** Grant `relay.*` to one of those roles (needs a ruling). Relay then opens for that role automatically, with the usual department and case-room limits.
- **If relaxed:** Not stated in the source. In plain words: the lock keeps front desk, ward clerk and lab accounts out of clinical case-room chat.
- **Switch:** Always on (enforced in `relay_service.get_channels_for_user`; keyed on the `relay.read` permission)
- **Source:** Wave 1; record W1-1.

### Multi-site: set-up, fence and staff accounts

**220. A second facility shuts every patient screen**

- **What is built:** Once a second facility is on record (active or not), every patient screen, upload and live connection answers 'This screen is not available yet when more than one facility is on record.' Only sign-in, health, facilities and ops stay open. This includes bedside monitor intake, the Bridge and the discharge link. Backup restore from the screen is fenced too; the ops check gives the keyboard command instead.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (`FACILITY_BLIND_SURFACES` `'*'`; `ORB_MULTI_SITE` does not switch it)
- **Ruling:** `M2` (a), `MS-R14` (a)
- **Source:** Wave 7; record W7-76.

**221. An unreadable facilities table reads as the last known answer**

- **What is built:** If the facilities table cannot be read, the last known answer is kept; with none yet it reads as one facility; it is retried on the next request. Our reading, not stated in the source: before Orb has read the table once, an unreadable table counts as one facility. On a box with two sites, the patient screens that entry 220 shuts would then be open until the next good read. The stricter option (refuse every screen while the table is unreadable) closes that gap.
- **Looser option:** None named. The named alternative is stricter: fail-closed (503 on every screen while the table is unreadable).
- **If relaxed:** Not stated in the source.
- **Switch:** Always on
- **Source:** Wave 7; record W7-78.

**222. Only the administrator manages facilities**

- **What is built:** Reading and managing facilities is for the administrator only. Group-wide reach across facilities is a named, audited grant (none exist yet), never a permission.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` (permissions `facility.read`, `facility.manage`)
- **Ruling:** `MS-R6` (a)
- **Source:** Wave 7; record W7-79.

**223. Only the admin role can hold a group-wide access grant**

- **What is built:** A live group-wide access grant widens an account's reach to the whole group only for the admin role; on any other role it is ignored. Only the admin role can hold one (enforced by the server route, the command-line tool and the access checks). Giving, listing and revoking are only for an admin who holds a live grant, checked fresh each time, never from a remembered copy; each refusal is logged. The first grant needs `grant_enterprise_access.py` at the console (multi-site on, an interactive terminal, the account id typed back). Grant and revoke each write a security log entry in the same step, and are refused if it fails. A reason of at least 3 characters and an active account are required; service and bot accounts are refused. A revoke works with the switch off.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** The admin-only rule is always on once more than one facility is on record, whatever `ORB_MULTI_SITE` says; giving a grant needs `ORB_MULTI_SITE`
- **Ruling:** `MS-R6` (a)
- **Source:** Waves 8 and 9; records W8-65 and W9-42.

**224. An account with an unlisted department sees no patient**

- **What is built:** An account whose department is not in the departments list sees no patient at all, and each attempt writes a refusal entry saying the department belongs to no facility.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on, keyed on data (more than one facilities row), whatever `ORB_MULTI_SITE` says
- **Source:** Wave 8; record W8-67.

**225. Unreadable facility maps fail rather than guess**

- **What is built:** With more than one facility on record and the facility maps never read, the request fails rather than guessing; a stale copy is kept if a later read fails. The wave 7 multi-site check still reads 'not multi-facility' when the facilities count has never been readable.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on, keyed on data (more than one facilities row), whatever `ORB_MULTI_SITE` says
- **Source:** Wave 8; record W8-68.

**226. Unclassified screens are shut while two sites exist**

- **What is built:** Any group of screens the build's safety list (its record of which screens are safe for two sites) has not yet sorted is shut (answers 503) while more than one site is on record, whatever its address. An unclassified screen is treated as unfinished.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record and a non-empty blind list); `server/facility_fence.py` `FACILITY_BLIND_SURFACES`
- **Source:** Wave 11; record W11-7.

**227. Only the seasonal calendar is cleared for two sites**

- **What is built:** Only the seasonal calendar (Pulse, which holds no patient data) is cleared to work with two sites. Every screen group with patient data stays shut, including admissions, uploads, live updates and data feeds, so the per-site binding wave 10 built for feeds that post over the network stays off while two sites exist.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record); `server/facility_fence.py` `FACILITY_CLEARED_SURFACES`
- **Source:** Wave 11; record W11-8.

**228. When a query counts as safely keyed to one patient**

- **What is built:** This is a check the build runs on itself, not something staff see. It decides which database reads count as safe for two sites because they only ever fetch one patient the screen has already checked. A read counts only when every screen that reaches it first runs a patient access check, no live screen update reaches it, and it fetches by that one patient. An access check that does not also check the site does not count. Known gap (wave 11 deferred): the check does not prove the access check was for the same patient, so 41 of these reads (rows in the build's safety list) rest on a person's judgement.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Test-time rule (`server/tests/facility_sql_sites` registry)
- **Source:** Wave 11; record W11-9.

**229. Cross-site leak test: 19 shut lanes would leak if opened**

- **What is built:** The cross-site leak test fails hard on any screen group that is open, cleared or outside the fence. For shut groups it records expected failures without failing the build: 19 shut lanes (single requests, for example GET `/api/vigil/patients`) would show another site's data today if the fence were lifted; they are shut (503) today, so nothing leaks now.
- **Looser option:** None named.
- **If relaxed:** The source states it: 19 lanes would show another site's patient data if the fence were opened today.
- **Switch:** Test-time rule (`tests/test_cross_facility_leak_sweep.py`)
- **Source:** Wave 11; record W11-10.

### Multi-site: patient access, registration and OPD

**230. Facility-bound accounts open a patient only by the latest encounter**

- **What is built:** A staff account tied to a facility but with no department may open a patient only when the patient's latest encounter is at its facility (latest admission first, else the registered facility). Any encounter there at some point is never enough.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on, keyed on data (more than one facilities row), whatever `ORB_MULTI_SITE` says
- **Ruling:** `MS-R5` (a)
- **Source:** Wave 8; record W8-64.

**231. A clinic cannot open an outpatient registered at another facility**

- **What is built:** The facility check runs before the outpatient route. Until `multisite-4` adds outpatient visits to the facility rule, a clinic at one facility cannot open an outpatient registered at another, even with a recent visit at its own.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on, keyed on data (more than one facilities row), whatever `ORB_MULTI_SITE` says
- **Note:** `multisite-4` was built in wave 11. Entry 233 now decides a patient's site by the newest admission, outpatient visit or booking, so a recent visit at the clinic's own site now counts. Confirm entry 233; this interim rule is replaced.
- **Source:** Wave 8; record W8-69.

**232. One UHID series for the whole group**

- **What is built:** The patient's hospital ID number (UHID) is one series for the whole hospital group, held at the main site. The series editor refuses to create a UHID series at any other site (`group_wide_series`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` (number series editor)
- **Ruling:** `MS-R11` (a)
- **Source:** Wave 10; record W10-9.

**233. Which site a patient's chart belongs to**

- **What is built:** This decides which site a patient's chart belongs to for staff access. An active admission always decides, so nothing an outpatient desk records takes an inpatient out of their ward's reach. Otherwise the newest admission, outpatient visit or appointment (not cancelled) decides, by when it was recorded, not when it is scheduled. To confirm: with this rule, a booking a site 1 clinician makes into a site 2 clinic for a discharged patient moves the chart to site 2 as soon as it is booked.
- **Looser option:** No looser option named. Named alternatives: (stricter) count only visits, so a booking counts once the patient is checked in; or count a booking only once its scheduled time has passed.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record); rule in `permissions.FACILITY_PATIENT_SQL`
- **Ruling:** `MS-R5`
- **Source:** Wave 11; record W11-4.

**234. The front desk cannot reach another facility's patient without a match ticket**

- **What is built:** A clinic at another facility is refused (`facility_scope`) and audited. A patient whose latest visit or stay is at another facility answers 'Patient not found' at every desk, department desks included, and is audited, unless the desk sends its own group-match ticket (valid 15 minutes, tied to that account and patient). A matched patient who is admitted at another facility right now is refused (409). A patient registered at a satellite desk before the registering facility was recorded counts as facility 1's and needs the group match.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `frontdesk_routes` `_require_clinic_at_facility` and `_patient_facility_gate`; group-match ticket `GROUP_MATCH_TICKET_SECONDS` 900
- **Ruling:** `MS-R3` (c) and `MS-R5` (a) (nearest)
- **Source:** Wave 12; record W12-36.

**235. Rules for the group-wide identity search**

- **What is built:** A desk can search the whole group for a patient registered at another facility only when `ORB_MULTI_SITE` is on, more than one facility exists and the account has no department (otherwise refused as `desk_only`, audited). All four facts must be given and match: phone (exactly 10 digits, placeholder numbers refused), sex exactly, every name word given found in the record's name, and date of birth exactly or age within 2 years. At most 10 results, identity only (no ward, bed, diagnosis or phone). A reason of at least 8 characters is required. Every search, including one that finds nobody, writes an audit row, and nothing is shown if it cannot be written. 20 searches per account per hour, counted from the audit log. A visit registered through a match ticket writes an audit row together with the visit itself, and the visit is not registered if that row cannot be written. To confirm: the four-fact rule, the 2-year age tolerance, the 20 per hour limit, the desk-only restriction and the 15-minute ticket, and that the group-wide search (`find_group_matches_on`) is filed as an ordinary single-site read in the build's safety list, which has no slot yet for an audited, approved group-wide search.
- **Looser option:** None named; the line lists the values to confirm instead.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` on and more than one facility on record; limits are `frontdesk_service` constants `GROUP_MATCH_LIMIT` 20, `GROUP_MATCH_WINDOW` 1 h, `GROUP_MATCH_MAX_RESULTS` 10, `GROUP_MATCH_TICKET_SECONDS` 900, `GROUP_MATCH_REASON_MIN` 8
- **Ruling:** `MS-R11` (a)
- **Source:** Wave 12; record W12-37.

**236. Facility-bound accounts see only their own facility's registration, OPD and beds**

- **What is built:** An account bound to a facility sees only what is at its own facility: registration (index search, duplicate worklist and counts, duplicate sweep), OPD (anything addressed by visit, the doctor's unassigned queue), ADT (bed board, awaiting-bed list, available beds, reservations, census with hospital totals meaning that facility's totals, bed setup, move options) and clinic timetables. An account whose department no facility lists sees nothing and can register nobody (`facility_scope`). A visit, admission, ward, room, bed, reservation or worklist pair at another facility answers 'not found', as if it did not exist; visit and admission refusals are audited. To confirm: whether clinic timetables (setup data, no patient data) should stay group-wide instead.
- **Looser option:** Keep clinic timetables group-wide (the open question on the line).
- **If relaxed:** Not stated in the source.
- **Switch:** Keyed on data; the lanes sit behind `ORB_ADT`, `ORB_OPD` and `ORB_REGISTRATION`
- **Ruling:** `MS-R5` (a)
- **Source:** Wave 12; record W12-43.

**237. Duplicates at another facility count but are not shown**

- **What is built:** Possible duplicate records at another facility are never shown to the desk but still count. A hard match on UHID or ABHA number refuses registration (`existing_record`), saying the record is at another facility. A probable match blocks when the duplicate mode is 'block'; otherwise a reviewed reason is required. An override opens worklist pairs with the hidden records, and the refusal and override audit rows list the hidden record ids.
- **Looser option:** Ignore other facilities' records.
- **If relaxed:** Stated: duplicate charts and split allergy lists.
- **Switch:** Keyed on data; registration sits behind `ORB_REGISTRATION`; the probable-match block follows `ORB_DUPLICATE_MODE` ('block')
- **Ruling:** `MS-R11` (a)
- **Source:** Wave 12; record W12-44.

**238. The identity check can be run from another facility**

- **What is built:** An inpatient at another facility answers 'not found' to the identity check, like an unknown id, and is audited. A patient with no active admission may still be checked from another facility (match or no match only, rate-limited and audited), which keeps the group-match registration flow working.
- **Looser option:** None named; this is the looser side. Stricter option named: refuse every patient outside the caller's scope.
- **If relaxed:** Not stated. As built, another facility can learn whether identity details match a patient with no active admission.
- **Switch:** Keyed on data; the lanes sit behind `ORB_ADT`, `ORB_OPD` and `ORB_REGISTRATION`
- **Ruling:** `MS-R5` (a) and `OPD-S1` (nearest)
- **Source:** Wave 12; record W12-45.

### Multi-site: beds, admissions and imports

**239. No moves or admissions across facilities until `MS-R3` is ruled**

- **What is built:** Until `MS-R3` and `R009` are ruled, Orb refuses to split one patient's stay across facilities. A front desk transfer into another site's department is refused and audited, and nothing is saved. An admission, OPD-to-inpatient conversion or readmission of a patient who already has an admission at another site is refused, and wave 10 says it is audited. Every bed lane refuses a move that names a ward or treating department at another facility than the admission's own: bed assignment, bed change, transfer (the front desk's and the clerk's ordered transfer included), swap, correction or bed reconciliation link. It answers `cross_facility_move_refused` before anything is written and writes one audit row. An account that sees both facilities is refused too. A ward in use never moves to another facility's department, and a bed is never reserved at one facility for a patient of another. Builder's additions: a staff member whose site access does not cover the department's site cannot admit there (`facility_scope`, audited), and a department the master list does not have, or has as inactive, is left off the list and an admission into it is refused. To confirm: the wave 12 deferred list says the admission form, readmission, clerk admit, Scribe quick admit, roster import and the Relay transfer lane refuse a cross-site move but write no audit row yet. That conflicts with wave 10's 'audited' for admissions and readmissions. Our reading of the code, not stated in the source: these may be two different refusals. The admission service does write an audit row when it refuses a patient admitted at another site; the wave 12 gap may be the refusal of a ward or department at another site in those lanes. An engineer should confirm which refusals are logged before go-live. Their bed pickers also still list every facility's wards.
- **Looser option:** None named. The ruling's options are in `docs/his_design/multisite.json`, `MS-R3`. To confirm: a group-wide admin can admit into any site today; is that intended?
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies once a second facility is on record; ADT lanes sit behind `ORB_ADT`; refusal code `CROSS_FACILITY_CODE` '`cross_facility_move_refused`'
- **Ruling:** `MS-R3` (c), refuse, until `MS-R3` and `R009` are ruled
- **Source:** Waves 10 and 12; records W10-13 and W12-41.

**240. Roster import refuses lines outside the account's sites**

- **What is built:** Builder's additions to roster import (nearest rulings `MS-R4` (a), `MS-R5` (a)). A line admitting or moving a patient into a department the master list does not have as active, or naming no department, is refused. A line that creates, moves or closes an admission, or changes a patient last seen at a site outside the importing account's access, is refused; an account with no site access imports nothing. A refused line shows no patient id, current values or allergies. The 'not on the roster' list covers only the account's own sites. A dry run or apply with no account is refused. A readmission overtaken by another site's admission between plan and save is refused and audited. A new patient is registered at the admitting department's site, or the run's site when no admission opens. Known gap from the wave's deferred list: a refused line can still keep earlier messages that quote stored ward, bed, admission id or allergy values.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record)
- **Ruling:** `MS-R4`, `MS-R5`
- **Note:** The checker asks that both halves be read together: a refused line shows no patient id, current values or allergies, yet earlier messages on that line can still quote stored values.
- **Source:** Wave 11; record W11-2.

**241. History import runs at one site and refuses split stays**

- **What is built:** A history import run belongs to the importing account's site (a group-wide or service account runs at its home site, or the main site), and new patients are registered there. A past admission goes to its department's site, else the run's. A line for a patient last seen outside the account's access fails (`facility_scope`); a past admission that would put a patient's admissions at two sites fails (`cross_facility_move_refused`); both go to the rejects list with no patient id. The dry run needs an account. No audit row is written per line.
- **Looser option:** None named; to confirm: whether an audit row per line is wanted, and whether a group-wide account should have to name the site it imports for.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record)
- **Ruling:** `MS-R3`, `MS-R5`
- **Source:** Wave 11; record W11-3.

**242. Roster import refuses cross-site readmissions and moves**

- **What is built:** `MS-R3` (refuse until ruled) applied to roster import. With more than one site, a roster line that would readmit a patient whose admissions are at another site, or move an open admission into another site's department, is refused. The dry run shows it as an error and skips the line, and Apply writes one audit row (`cross_facility_move_refused`, lane `roster_import`). The whole line saves nothing, not even the patient's other details.
- **Looser option:** Save the patient's other details and skip only the admission.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record)
- **Ruling:** `MS-R3`
- **Note:** Moved up on the checker's advice: the same kind of refusal guards a patient in entries 239, 240 and 241. Entry 239's wave 12 gap lists roster import among lanes that write no audit row, but the code has a roster import audit step (`roster_import._audit_facility_refusals`). Check that against the audit row described here.
- **Source:** Wave 11; record W11-1.

**243. A ward belongs to its department's facility**

- **What is built:** A ward belongs to its department's facility. Wards take their facility from the department's active master record. With more than one facility on record, setting up a ward for a department no facility lists, or for another facility's department, is refused with a message in words. A site-configuration import skips other facilities' wards and gives the reason.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Keyed on data; ADT sits behind `ORB_ADT`; `bed_master_service` codes `department_not_listed`, `department_other_facility`, `ward_facility_fixed`
- **Ruling:** `MS-R4` (a)
- **Source:** Wave 12; record W12-42.

### Multi-site: Sage, Relay, surgery, paging and live screens

**244. Unfiled Lens reads and Scribe drafts open only to their author**

- **What is built:** With more than one facility, a Lens read or a Scribe draft that has not yet been filed to a patient, and the photo or audio file behind it, can be opened only by the person who made it. This holds for accounts with no department too, such as governance and ward accounts.
- **Looser option:** Anyone at the author's facility could open it.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: enforced in `server/main.py` `serve_upload` (`_media_author`)
- **Ruling:** `MS-R5` (a)
- **Source:** Wave 12; record W12-1.

**245. Relay rooms with no facility are hidden**

- **What is built:** For any account limited to certain facilities, administrators included, a Relay case room with no patient, or a department room whose name the department master does not list, is hidden. A department room shows only when the master places that department at one of the person's own facilities. Direct messages are unchanged.
- **Looser option:** Show rooms that have no facility to administrators.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `relay_service.get_channels_for_user` with `_rooms_at_facilities`
- **Ruling:** `MS-R4` (a)
- **Source:** Wave 12; record W12-2.

**246. Surgical cases are visible only within the user's facilities**

- **What is built:** Every way a person can see a surgical case (clauses A to D) is kept to cases at their own facilities. Theatre staff and accounts with no department see only their own facility's theatre list. Someone named on the case team who works at another facility does not see the case. Clause C also needs the patient's latest visit or stay to be at the person's facility.
- **Looser option:** Let clause D (the named case team) reach across facilities, for visiting consultants.
- **If relaxed:** Not stated. Strict-side cost, from the line: a team member named on a case at another facility cannot see it.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `surgery_scope.surgical_list_filter`
- **Ruling:** `MS-R7` (nearest)
- **Source:** Wave 12; record W12-3.

**247. Paging skips staff whose facility does not hold the patient**

- **What is built:** If the patient's named attending, or the nurse who charted the observations, works under a facility scope that does not hold this patient, Orb does not page them. The page falls through to the patient's own department roster, and failing that to the role. A sweep or board scan run by an account limited to some facilities raises escalation chains only for those facilities. Moving a chain up to the next tier stays house-wide.
- **Looser option:** Page the named attending wherever they are.
- **If relaxed:** Not stated. Strict-side cost, from the line: the page goes to the department roster or the role instead of the named attending.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `escalation_service._user_with_role(patient_id=)`, sweep and `scan_new_observations` (facilities=)
- **Ruling:** `MS-R8` (nearest)
- **Source:** Wave 12; record W12-5.

**248. The ward brief is withheld without a facility scope**

- **What is built:** With more than one facility, the ward brief as the screen asks for it today (no facility scope passed) is withheld in full from anyone with no department. A ward reader is kept to their own department's facility. When a facility scope is passed, the watchlist and risk mix cover only those facilities, and the patient count, events, alert count and written summary are left out. The wave 12 deferred list ('Ward brief route') adds that the route still passes no scope, so a reader with no department gets no brief at all until a one-line change, and the card's tooltip still says 'department'.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `sage_narrator_service.get_brief(facilities=FACILITY_SCOPE_UNKNOWN)`
- **Source:** Wave 12; record W12-6.

**249. The latest visit decides the discharged list and final-bill picker**

- **What is built:** The facility of a patient's latest visit or stay decides which facility's discharged list and unseen counts they appear in. A patient discharged at facility 1 who is later booked or seen at facility 2 leaves facility 1's discharged list. That list is also the billing desk's picker for final bills, so the patient leaves facility 1's final-bill picker too.
- **Looser option:** Alternative named: use the facility of the latest admission instead.
- **If relaxed:** Not stated. Strict-side cost, from the line: facility 1's billing desk loses the patient from its final-bill picker.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `permissions.FACILITY_PATIENT_SQL` (facility of the latest visit or stay)
- **Ruling:** `MS-R5` (a)
- **Note:** The wave 12 checker called this flag debatable: it is marked as guarding a patient, while entry 419 is marked as not guarding one.
- **Source:** Wave 12; record W12-7.

**250. Sage answers as if another facility's patient does not exist**

- **What is built:** When someone names a patient or bed in Sage that exists only at another facility, Sage answers exactly as if no such patient existed. A facility-bound account with no department is told 'there is no patient matching that at' followed by the facility's name, and the model is told not to suggest anything about other facilities. A ward account gets the existing neutral notice ('not in the patient census available to you') with no facility named. If a list of patient ids arrives without its facility scope, no roster is built while two facilities exist, so it fails closed. The department notice still works inside one facility.
- **Looser option:** None named in `contracts.md` (the ruling's other options are in `docs/his_design/multisite.json`, `MS-R13`).
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `sage_service` out-of-scope locator (`_ScopedIds`, `FACILITY_NO_MATCH_SUFFIX`)
- **Ruling:** `MS-R13` (a)
- **Source:** Wave 12; record W12-9.

**251. Patient ids outside the facility get an immediate Sage refusal**

- **What is built:** Going beyond the ruling: if a facility-bound account with no department names a patient id outside its facilities, Orb answers at once, without asking the model, 'No patient matching that at' followed by the facility's name, the same as for an id that does not exist. The refusal is written to the security audit log and fails closed. Ward accounts keep the department notice. The voice assistant uses matching words.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: security audit action `sage_out_of_scope_patient_denied`; `voice_agent_service._facility_refusal_words`
- **Ruling:** `MS-R13` (builder's extension)
- **Source:** Wave 12; record W12-10.

**252. Live vitals never go to the all-departments channel**

- **What is built:** While two facilities exist, live vitals updates never go to the all-departments channel. A screen whose department no facility lists gets no live vitals. A screen's live connection is refused if its facility scope cannot be read, and dropped at its next update when its facility view changes, including the moment a second facility first appears (every open screen reconnects then). The queue nudge, which carries only a department name, goes to every facility's channel.
- **Looser option:** For the queue nudge: send it only to the department's own facility.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `ws/realtime` `vitals_rooms_for` and `AuthorizedSocketServer._fresh`
- **Source:** Wave 12; record W12-11.

**253. Sage counts cover only the user's facilities**

- **What is built:** Sage's counts across patients cover only the person's own facilities, and an empty scope counts zero. Reading a Relay room whose facility scope cannot be read returns no messages.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `sage_service.build_aggregate_context(facilities=)`
- **Source:** Wave 12; record W12-13.

**254. A visiting attending is paged at the patient's facility**

- **What is built:** When a patient is escalated, a named attending doctor with a live, valid visiting entry at the patient's facility is paged even while their session is at another facility. The department roster still pages home staff only. Nurses are never paged across facilities.
- **Looser option:** None named. The source names a stricter option instead: page only an attending whose current session is at the patient's facility.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` (`config.feature_enabled('multi_site')`, checked at call time); `facility_service.reachable_facilities_for`, used by `escalation_service._outside_patient_facility`
- **Ruling:** `MS-R8` (near)
- **Source:** Wave 13; record W13-40.

### Multi-site: medication orders, pharmacy and stores

**255. The pharmacy verify queue is limited to the pharmacist's facilities**

- **What is built:** Medicines carry no facility of their own, so the pharmacist's verify queue keeps medicines of patients whose latest visit or stay is at the pharmacist's facilities. Verifying or rejecting a medicine outside that scope answers 'This medication is no longer on the chart' and writes a refusal audit row, before any safety screen or change. An unknown medicine still gets its old error.
- **Looser option:** None named in `contracts.md` (the ruling's other options are in `docs/his_design/multisite.json`, `MS-R9`).
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `pharmacy_verification_service.list_pending_verification(facilities=)`, `routes/pharmacy_routes._facility_gate`
- **Ruling:** `MS-R9` (a)
- **Source:** Wave 12; record W12-35.

**256. Any facility's pharmacist can edit the shared drug master**

- **What is built:** The drug master and its governance, vendors and their licences, the last price paid and the one-batch-one-expiry check stay shared across the group. Any pharmacist tied to one facility may edit the drug master.
- **Looser option:** None named. This is already the looser setting; the source names a stricter option: require an enterprise grant for drug-master edits. To confirm: whether any facility's pharmacist should edit the shared drug master.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: no gate on drug-master edits
- **Source:** Wave 13; record W13-25.

**257. The NDPS register names only patients in scope**

- **What is built:** On an NDPS (controlled drug) register page or export, a patient is named only if the user's department and facilities give them access to that patient. Only entries from stores at the user's own facilities are listed.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `ndps_service.balances`, `_register_query` and `_decorate_entries` (facilities=), `patient_in_scope`
- **Ruling:** `MS-R5` (a)
- **Source:** Wave 13; record W13-27.

**258. Only group-level accounts run the order parity report and adoption**

- **What is built:** While more than one facility is on record, only a group-level account may run the medication-order parity report or adopt old medication orders into the shared store. A user limited to some facilities is refused and logged. Adoption at start-up is unchanged.
- **Looser option:** Filter the report and adoption to the patients of the caller's facilities.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `routes/medorders/core` `_require_group_wide`, error `group_access_required` (403), audit `facility_group_access_required`
- **Ruling:** `MS-R6` (a) (nearest)
- **Source:** Wave 13; record W13-32.

### Multi-site: laboratory and device feeds

**259. Device and feed data matching another site is not filed**

- **What is built:** Readings from a machine or feed not tied to a site, or that match only a patient at another site, are not filed: they go down today's unknown-patient path (vitals refused with the same words, observations left unlinked, or put in the unmatched-results queue when that switch is on), an audit row is written and the source shows on the health page. Strict choices: an ID that names a record at another site is refused even if it also names one at the feed's own site; a feed tied to a closed site files nothing; one key set for two sites binds neither; the old single key stays the main site's; once a second site exists the bedside monitor feed files nothing until `site.toml` lists it (a go-live step). Data entered by hand through the chart (the MANUAL lanes) is exempt because the screen already checked the patient. Audit rows are limited to one per source, reason and patient each 60 seconds, with a count.
- **Looser option:** None named; to confirm: the hand-charted exemption, the 60-second audit limit, and refusing an ID that matches records at two sites.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record); bindings in `site.toml` `[[facilities]]` `ingestion_sources` and `ORB_INGESTION_API_KEY_<CODE>`; monitor feed via `ORB_MONITOR_SOURCE_ID`; unlinked observations follow `ORB_RESULT_QUARANTINE`
- **Ruling:** `MS-R14` (a)
- **Source:** Wave 10; record W10-11.

**260. Lab staff see their own facility's lab work**

- **What is built:** Lab staff and admins (accounts with no department) see and act on their own facility's lab work: the facility that performs the sample, else the patient's facility when there is no sample. Ward accounts (with a department) see the lab rows of their own patients (their department, with the patient's latest visit at their facility).
- **Looser option:** None named. The source names a stricter option instead: also require the patient's facility for lab accounts.
- **If relaxed:** Not stated for relaxing. The line names the stricter option's risk: lab work, including a pending critical result, that no one can see or finish.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `lab_facility.list_clause` and `require_in_scope`, `specimen_facility_sql`
- **Ruling:** `MS-R5` (a)
- **Source:** Wave 13; record W13-28.

**261. Released lab reports open if the lab or the patient is in scope**

- **What is built:** Amending the rule above for reports: lab staff and admins can open and print a released report when either the facility that performed the sample or the patient (latest visit) is within their facilities. It is refused and logged only when neither is. Every other lab step keeps the performing-facility rule.
- **Looser option:** None named. The source names a stricter option instead: refuse such a report and drop it from the UHID and patient lists.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `lab_report_service.report_for` (`require_in_scope` `chart_read=True`)
- **Ruling:** `MS-R2` (a)
- **Source:** Wave 13; record W13-29.

**262. A new sample is performed at the patient's facility**

- **What is built:** A new sample is performed at its patient's facility (active admission, else latest visit, else registration facility, else facility 1). A recollection keeps the rejected sample's facility. A planned tube never takes tests from another facility. There is no routing to a central lab.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `lab_facility.performing_facility_on` (`facility_identity.document_facility_id`); `lab_specimens.facility_id`
- **Source:** Wave 13; record W13-30.

**263. Another facility's lab and medication records look not found**

- **What is built:** Any step on another facility's sample, result, order, critical result, report or medication order answers 'not found', the same as an unknown id, writes nothing, and leaves an audit row saying it was refused for facility reasons. A user whose department no facility lists sees nothing. Department-only refusals inside the lab stay unlogged, as before.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `lab_facility.audit_refusals`; `routes/medorders/core` `_facility_denied` (`permissions.facility_denial`); audit `patient_scope_denied` `scope_cause` 'facility'
- **Source:** Wave 13; record W13-31.

**264. Unmatched results attach only to patients in scope**

- **What is built:** A lab result that arrived without a matched patient can be attached only to a patient inside the reconciling user's department and facilities, else it is refused (409 `identity_not_confirmed`) and logged as before. The two wrong-patient checks still look across the whole appliance.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `result_quarantine_service.attach` reads `facility_scope(user)`, error `identity_not_confirmed` (409)
- **Ruling:** `MS-R14` (a)
- **Source:** Wave 13; record W13-33.

### Multi-site: audit, backups, downtime and exports

**265. Five internal audit lookups are not filtered by facility**

- **What is built:** Five internal checks that read the audit log (`_prescriber_override_on_file`, `_printed_before`, `observation_keyed_by`, `reissued_tokens`, `_recent_failures_on`) are not filtered by facility. Each reads audit rows about the patient, visit or order its screen already holds, and that screen's own scope check still applies. In plain words: no screen shows anything new. These checks only look at audit rows about a patient, visit or order the screen is already allowed to open.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None: classed 'appliance' in the facility SQL-site registry, so not filtered by facility
- **Source:** Wave 12; record W12-23.

**266. A restore rolls back every facility**

- **What is built:** Restoring a backup rolls back every facility on the appliance together; there is no per-facility restore. Orb says so on the Data and Backups screen when more than one facility is on record or `ORB_MULTI_SITE` is on, always in the backups API's `restore_scope` field, and in CIO security questionnaire answers 1.5, 5.4 and 5.8.
- **Looser option:** None named. There is no per-facility restore.
- **If relaxed:** Not stated in the source. In plain words: a restore done for one site's problem also erases everything every other site entered since that backup, including allergies, orders and results. There is no per-site restore.
- **Switch:** `ORB_MULTI_SITE` on, or more than one facility on record, for the note on Data and Backups; `restore_scope` in GET `/api/admin/backups` is always on
- **Ruling:** `MS-R12` (a)
- **Source:** Wave 12; record W12-24.

**267. Each satellite needs its own downtime mirror folder**

- **What is built:** Once a second facility exists, the shared mirror folder set by `ORB_WARD_PACK_MIRROR_DIR` holds only facility 1's downtime packs. Every other facility's packs go only to the mirror folder named for it in `site.toml`. A satellite with no mirror of its own gets no mirror copy; this is reported as `mirror_missing` and logged, and Orb never falls back to the shared mirror.
- **Looser option:** A subfolder of the shared mirror for each facility.
- **If relaxed:** Not stated. Strict-side cost, from the line: a satellite with no mirror of its own gets no mirror copy of its packs.
- **Switch:** `ORB_WARD_PACK_MIRROR_DIR` (shared mirror) and each facility's `downtime_mirror_dir` in `site.toml`; keyed on data (a second facility on record)
- **Source:** Wave 12; record W12-25.

**268. Overlapping mirror folders are refused for both facilities**

- **What is built:** If `site.toml` names the same mirror folder for two facilities, or one inside the other, Orb does not start and names the file and line. On every pack build with more than one facility on record, a mirror that is the same folder as, sits inside, or holds another facility's mirror or folder is not used, and both mirrors of a clashing pair are refused. So a shared `/Volumes/WardPacks` with SAT1 at `/Volumes/WardPacks/SAT1` also costs facility 1 its mirror until they are separated. This is logged on every build and reported as `mirrors_refused`.
- **Looser option:** Refuse only the satellite's mirror when it sits inside facility 1's.
- **If relaxed:** Not stated. Strict-side cost, from the line: facility 1 also loses its mirror until the folders are separated.
- **Switch:** `site.toml` `downtime_mirror_dir` (checked at boot) and `ORB_WARD_PACK_MIRROR_DIR`; keyed on data at each pack build
- **Source:** Wave 12; record W12-26.

**269. Downtime pack folders and ward identity per facility**

- **What is built:** Facility 1 keeps the usual export folder; every other facility uses its own folder, `<export>/facilities/<CODE>/`, with owner-only permissions (0o700). A ward name two facilities share is two wards with two packs. A patient belongs to the facility of their latest visit or stay. The signed-in ward-pack download gives only the ward at the anchor patient's facility, even to a group-wide account. Rebuilding a ward pack without naming a facility builds facility 1's ward and never mixes facilities.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `downtime_service.facility_export_dir` (`FACILITY_FOLDER` 'facilities')
- **Source:** Wave 12; record W12-27.

**270. Facility extract: only group-level items for shared patients**

- **What is built:** When a facility's data is extracted with the console tool, allergies, the problem list and current medicines are always included in full. A patient who also has any visit or stay at another facility gets only this facility's visits and stays plus those group-level items (no observations and no full medicine history), tagged group-level-only and listed in the manifest.
- **Looser option:** The whole chart for every included patient.
- **If relaxed:** Not stated in the source.
- **Switch:** `server/scripts/facility_extract.py` (console only, run by hand)
- **Ruling:** `MS-R2` (a), strict reading
- **Source:** Wave 12; record W12-28.

**271. FHIR bundles name facilities on multi-site**

- **What is built:** FHIR bundles name facilities (an Organization with the facility's HFR id, the encounter's service provider and the patient's managing organization) only when more than one facility is on record or `ORB_MULTI_SITE` is on. The full-chart export (FHIR `$everything`) then carries the whole chart, including other facilities' visits and stays and their Organizations. With one facility and the switch off, a bundle is unchanged.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` on, or more than one facility on record
- **Ruling:** `MS-R2` (a)
- **Source:** Wave 12; record W12-30.

### Multi-site: NABH, Command Center, admin and support services

**272. An unlisted department sees an empty Command Center**

- **What is built:** A user whose department is listed at no facility sees an empty Command Center, with words explaining why. They never see the whole hospital.
- **Looser option:** None named.
- **If relaxed:** Not stated. The line rules out such a user seeing the whole house.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `command_center_service.house_snapshot(facility_ids=)`, `facility_view` '`no_facility`'
- **Ruling:** `MS-R6`
- **Source:** Wave 13; record W13-6.

**273. Asking for another facility's figures returns 'not found'**

- **What is built:** Asking the Command Center or the NABH screens for a facility outside the user's reach gets 'not found' (404), the same answer as for a facility that does not exist.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: GET `/api/command-center?facility=`, `/api/nabh/indicators` and `/api/nabh/incidents` `?facility=`
- **Ruling:** `MS-R6`
- **Source:** Wave 13; record W13-7.

### Multi-site: visiting consultants and sign-in

**274. Only doctors can hold a visiting session**

- **What is built:** Only doctors can hold a visiting session at another facility. A nurse or ward clerk with a visiting entry is offered nothing and refused (403 `visiting_role`), and a visiting token for one is rejected (401).
- **Looser option:** Let nurses and ward clerks hold visiting sessions too.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` (`config.feature_enabled('multi_site')`, checked at call time); module constant `facility_service.VISITING_ROLES` = {'doctor'}; error `visiting_role` (403)
- **Ruling:** `MS-R7` (a)
- **Source:** Wave 13; record W13-36.

**275. One facility at a time per account**

- **What is built:** An account works at one facility at a time. Every facility switch ends the account's other sessions at their next request.
- **Looser option:** Keep the account's other sessions at their own facility.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` (`config.feature_enabled('multi_site')`, checked at call time); `users.token_version` bumped on every switch
- **Ruling:** `MS-R7` (a)
- **Source:** Wave 13; record W13-37.

**276. Shared ward tablets never switch facility**

- **What is built:** A shared ward tablet signed in by PIN can never switch facility (403 `shared_device`), and a token carrying both a tablet and a visiting facility is rejected (401).
- **Looser option:** Allow a switch on a tablet when the tablet's department is the visiting department.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` (`config.feature_enabled('multi_site')`, checked at call time); error `shared_device` (403); a token with both 'did' and 'fac' is 401
- **Ruling:** `MS-R7` (a)
- **Source:** Wave 13; record W13-38.

**277. Visiting sessions are re-checked on every request**

- **What is built:** Every request in a visiting session re-reads the visiting entry fresh from the database. Unless the entry is live, at an active facility, for an active department listed at that facility, for an active doctor whose own facility is another one, with `ORB_MULTI_SITE` on and more than one facility on record, the session is rejected (401, 'Your visiting access has ended. Sign in again.'). Turning `ORB_MULTI_SITE` off ends every visiting session.
- **Looser option:** Keep visiting sessions alive while the data is multi-facility.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` (`config.feature_enabled('multi_site')`, checked at call time); `auth._load_session_snapshot` and `auth._visiting_verdict`; turning `ORB_MULTI_SITE` off ends every visiting session
- **Ruling:** `MS-R7` (a)
- **Source:** Wave 13; record W13-39.

**278. Visiting sessions sign out, never reopen in place**

- **What is built:** A visiting session is never reopened in place. When it expires, goes idle (the usual idle lock is cancelled) or a switch finds it already ended, Orb signs out and goes back to the sign-in screen. A switch is refused while offline or while offline changes are waiting to send. At a switch the offline patient-data cache and nurse drafts are cleared and Orb reloads.
- **Looser option:** Lock the screen and sign in again in place.
- **If relaxed:** Not stated for relaxing. The line names the strict default's cost: a visiting consultant who steps away for 15 minutes loses unsaved input and must sign in and switch again.
- **Switch:** `ORB_MULTI_SITE` (`config.feature_enabled('multi_site')`, checked at call time); client window event 'orb:idle-lock' (ClinicianShell calls preventDefault only in a visiting session)
- **Source:** Wave 13; record W13-42.

**279. A password re-entered at another facility reloads the screen**

- **What is built:** When a password is re-entered at either password prompt, Orb carries on in place only if the sign-in lands at the same facility as the screen. Otherwise it reloads, and the prompt stays up until it does. This also reloads a home account that an administrator moved to another facility mid-session.
- **Looser option:** Carry on in place and refresh only the header.
- **If relaxed:** Not stated in the source.
- **Switch:** None (no flag); only while more than one facility is on record
- **Source:** Wave 13; record W13-43.

**280. A facility switch reloads every other tab**

- **What is built:** A facility switch in one tab signals every other signed-in tab to reload, whoever it shows. A tab also reloads when the stored sign-in becomes the same person at a different known facility. Another person signing in elsewhere, or a sign-out, changes nothing.
- **Looser option:** Leave other tabs alone until their next 401 (session rejected).
- **If relaxed:** Not stated in the source.
- **Switch:** None (no flag); a switch exists only with `ORB_MULTI_SITE` on and more than one facility; localStorage signal '`orb_facility_switch`' (`src/lib/facilitySwitchSignal.ts`)
- **Source:** Wave 13; record W13-44.

### Multi-site: practice facility tool and two-site rehearsal

**281. The practice facility tool runs only on database copies**

- **What is built:** The tool that sets up a practice second facility only runs on a copy of the database. It refuses the default database at `server/orb.db`, any hard link to it, and any machine set to the production profile, before it opens that database, in both its check and apply modes (`--check` and `--apply`). In apply mode, every check that needs no sign-in runs before the sign-in, so a copy it refuses is never signed in to. A refused run leaves no sign-in record and does not count toward anyone's lockout. On a real hospital box a new facility is added only on the Facilities screen.
- **Looser option:** None named.
- **If relaxed:** Not stated directly. The line implies that without the early check a refused run would leave sign-in or failed sign-in records and could count toward an account lockout; it does not spell out what seeding a live database would do.
- **Switch:** Always on (console script, no flag; it refuses `ORB_DEPLOYMENT_PROFILE`=prod)
- **Note:** Moved up on the checker's advice: this is what stops practice staff, patients and medicines being written into a real hospital's records.
- **Source:** Wave 14; record W14-1.

**282. The practice tool never skips the drug-safety check**

- **What is built:** The setup tool never skips the allergy and drug-safety check when it adds medicines. It never sends the safety acknowledgement (`safety_ack`) that would let a medicine through a warning. If the drug-safety check stops a medicine (a 428 answer), the tool stops too and lists what it had already written.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: relaxing it would let the tool push a medicine past an allergy or drug-safety warning with no person acknowledging it.
- **Switch:** Always on (console script, no flag)
- **Source:** Wave 14; record W14-3.

**283. The two-facility rehearsal fails on any cross-facility leak**

- **What is built:** The automated two-facility rehearsal fails if any data request the app answers (every GET route), made as the facility 2 nurse, administrator, pharmacist and cashier and the facility 1 nurse, returns a patient from the other facility. One known case, the Sage insights list, is allowed only while the fence (the list in `server/facility_fence.py` of route groups that stay shut while two facilities exist) still holds that route group shut. Once the fence is emptied, any such leak fails the rehearsal. It checks data requests, not screens: the screen-by-screen browser pass with screenshots is still to be done.
- **Looser option:** None named.
- **If relaxed:** A data request that returns another facility's patients could pass the rehearsal. In the 20 October rehearsal the Sage insights list showed facility 1 patients to the facility 2 administrator; the fence keeps that route group shut today, so nothing leaks in the product.
- **Switch:** Always on (test constant `KNOWN_BLIND_DISCLOSURES` in `server/tests/test_two_facility_e2e.py`, tied to `FACILITY_BLIND_SURFACES` in `server/facility_fence.py`)
- **Note:** The wave 14 notes add that the Sage insights list is not limited by department even at one site; see Also open.
- **Source:** Wave 14; record W14-6.

## Every other decision

Entries 284 to 480. Grouped by module.

### OPD and registration

**284. A resent registration returns only the patient id and UHID**

- **What is built:** If the desk sends the same registration twice (same request key) within 24 hours, the second answer gives only the patient id and UHID, never the record; after 24 hours it is refused (409 `registration_key_expired`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION`; `registration_service.REPLAY_WINDOW` (24 hours)
- **Source:** Wave 4; record W4-12.

**285. A date of birth can only be removed if an age replaces it**

- **What is built:** A date of birth can be removed only if a stated age is given in the same request; clearing a stated age when there is no date of birth is refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION`
- **Source:** Wave 4; record W4-13.

**286. Signed OPD notes can never be changed, but can be erased**

- **What is built:** Signed OPD notes can never be changed and addenda never change (database triggers); there is no delete trigger, so a patient's erasure request can still remove them.
- **Looser option:** Your choice whether OPD notes should instead be kept through erasure (erasure-retained).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD` (migration 62 triggers)
- **Source:** Wave 4; record W4-46.

**287. Completing or converting a visit needs a signed note or a reason**

- **What is built:** Completing a visit from the OPD screen needs a signed note or a written reason (409 `signed_note_required`). Converting a visit to an admission (wave 7) needs a signed consultation note, or a reason of at least 3 characters for not having one. Gap: the front-desk complete route in OPD mode needs `opd.consult` but does not apply this rule, because an existing test (`test_opd_scope.py`) pins completing with an empty request. The recommendation is to amend that test.
- **Looser option:** The front-desk complete route is already the looser path (no note or reason needed); the recommendation is to close it.
- **If relaxed:** Stated gap: from the front desk a visit can be completed with no signed note and no reason.
- **Switch:** `ORB_OPD`
- **Ruling:** `OPD-N8`
- **Source:** Waves 4 and 7; records W4-47 and W7-11.

**288. Only ticked, validated coded diagnoses reach the problem list**

- **What is built:** At signing, only a ticked and validated coded diagnosis is added to the patient's problem list, in the same step; unvalidated or uncoded diagnoses never are.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 4; record W4-49.

**289. The live queue signal carries only the department**

- **What is built:** The live 'queue changed' signal carries only the department, sent to that clinic and to the all-departments room; front-desk accounts get no live channel until `opd-9b`.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Note:** `opd-9b` (live updates for the front desk) was built in wave 8. Desk staff holding `registration.read` now join their own clinic's live channel; see entry 299.
- **Source:** Wave 4; record W4-52.

**290. Allergy reviews can be erased with the record**

- **What is built:** Allergy review records can never be edited (database trigger) but can be deleted, so a patient's erasure request can remove them (same as migrations 60 to 62).
- **Looser option:** Your choice: keep reviews through erasure (one `_ERASE_RETAINED` line plus a delete trigger).
- **If relaxed:** Not stated in the source.
- **Switch:** Migration 63 (written only with `ORB_OPD`)
- **Source:** Wave 5; record W5-7.

**291. Outpatient lab orders must match the hospital's own active tests**

- **What is built:** When the laboratory module is on, an outpatient lab order must match a test the hospital's own laboratory actually runs and has switched on; anything else is refused. With the laboratory module off, the standard LOINC codes and Orb's own lab codes are offered.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD` (with `ORB_LAB` deciding the catalogue rule)
- **Source:** Wave 6; record W6-2.

**292. Outpatient test orders go through even if their audit row fails**

- **What is built:** Outpatient test orders go live at once, with no draft step. If the audit record of the order cannot be written, the order still goes through (fail-soft), as with the other outpatient audits written inside the same step.
- **Looser option:** None named. The named alternative is stricter: fail-closed (refuse the order if its audit record cannot be written).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 6; record W6-5.

**293. Cancelling an outpatient test needs a reason**

- **What is built:** Cancelling an outpatient test needs a reason of at least 3 characters, only for that visit's own order, only while the visit is current and the patient is not admitted. A test the laboratory has taken over cannot be cancelled from the clinic.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 6; record W6-6.

**294. A note save is refused if a colleague saved in between**

- **What is built:** Every save of a note carries the version it was started from. If a colleague saved in between, this save is refused as out of date and the doctor reloads.
- **Looser option:** None named.
- **If relaxed:** The source states the purpose: a colleague's later save could otherwise be silently overwritten (a lost update).
- **Switch:** `ORB_OPD`
- **Source:** Wave 6; record W6-10.

**295. Appointment check-in and no-show rules**

- **What is built:** Check-in only on the appointment's own hospital day. A checked-in appointment closes with its visit and cannot be cancelled on its own. A no-show can only be rescheduled. A manual no-show only after the slot has started. One live follow-on booking per rescheduled one. No-shows are worked out when read, never written by a timer.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 7; record W7-3.

**296. The server will not start on a broken appointments table**

- **What is built:** If the appointments table holds orphan rows, unknown columns, indexes or triggers, or two live bookings in one doctor slot, the server will not start and names the rows; nothing changes until a person fixes them. The wave 7 dry runs found none on the three database copies.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (migration 68)
- **Source:** Wave 7; record W7-5.

**297. The waiting-room token board shows no names**

- **What is built:** The waiting-room token board shows token, doctor and room only, never a name, UHID, age or complaint. The board's device key travels in a header only, is stored scrambled (hashed) and shown once. Creating and revoking a board are audited (fail-closed). Reading the board writes no patient-access record.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Ruling:** `OPD-N7`
- **Source:** Wave 7; record W7-13.

**298. Repeat OPD queue reads are logged once per 10 minutes**

- **What is built:** Product ruling, listed because it reduces log entries. When the front desk reads the outpatient queue, the access log gets one entry per user per distinct set of visible visits per 10 minutes, not one per read. Any change in the set writes an entry at once; a restart starts fresh (which can only add entries); a failed log write refuses the read and lets the next read try again. With OPD off every non-empty read is logged as before.
- **Looser option:** To confirm the auditors accept skipping an identical re-read within 10 minutes. The stricter option is the old behaviour: log every read.
- **If relaxed:** Stated: fewer access-log entries, since an identical re-read within 10 minutes leaves no entry of its own.
- **Switch:** `ORB_OPD` (OPD mode; window `opd_queue_service.QUEUE_READ_AUDIT_WINDOW_SECONDS` = 600)
- **Ruling:** `OPD-N12` (b)
- **Source:** Wave 8; record W8-8.

**299. Only registration staff join the desk's live channel**

- **What is built:** Only staff holding `registration.read` join the outpatient desk's live-update channel, only while OPD is on, and the channel comes from the account's own department, never from what the screen asks for. The receptionist never joins a vitals channel. The update message carries only the department name.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 8; record W8-10.

**300. The front office day summary is counts only, with no log entry**

- **What is built:** The front office day summary shows counts only (no patient id, name, UHID, visit, token or complaint), so no access-log entry is written. Doctor names are shown. A future day is refused, a department account sees its own clinic only, and the cashier is refused.
- **Looser option:** None named. To confirm that these count-only reads need no access-log entry.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 8; record W8-11.

**301. The receptionist cannot record consent**

- **What is built:** The receptionist does not hold `consent.record`: her consent step is refused before any check or write and logged; the desk screen does not offer it and says a doctor records consent.
- **Looser option:** Give the receptionist `consent.record` once counsel agrees (deferred).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION`
- **Ruling:** `OPD-N3` (counsel-gated product ruling)
- **Source:** Wave 9; record W9-33.

**302. Consent for patients under 18**

- **What is built:** For a patient under 18, consent given by the patient is refused when the stated age or date of birth is under 18; a date of birth is counted to the earlier of the local and UTC date; an unknown age counts as a minor. Consent by 'guardian' or 'proxy' needs a name and relationship.
- **Looser option:** None named as looser. To confirm: whether 'proxy' ('Someone acting for them') may consent for a child, or only 'guardian' (both allowed today).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION` (module constant `registration_service.CONSENT_ADULT_AGE` 18)
- **Ruling:** None (DPDP s.2(f), s.9)
- **Source:** Wave 9; record W9-34.

**303. Guardian consent needs a named person**

- **What is built:** The consent method '`proxy_guardian`' at registration is refused unless consent is given by a guardian or proxy with a named person and relationship; the desk screen follows the server's list.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_REGISTRATION`
- **Source:** Wave 9; record W9-35.

**304. ICD-10 codes need a licence reference to load**

- **What is built:** No ICD-10 codes can be loaded without a licence reference. The loading script runs as a dry run unless the licence reference is given, and records it in the version and log entries. Re-checking stored diagnoses runs only on request, touches unsigned notes only and never ticks a diagnosis onto the problem list.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Ops script `scripts/load_terminology.py` (`--licence-ref`); no feature switch
- **Ruling:** `OPD-N11` (counsel-gated)
- **Source:** Wave 9; record W9-36.

**305. The bedside consent lane does not apply the under-18 rules**

- **What is built:** Extra item from the `W9-G` deferred list, not a built strict default: the existing bedside consent lane is unchanged. It still accepts the '`proxy_guardian`' method with consent given by the patient and reads no date of birth, so the new under-18 and guardian rules from registration do not apply there.
- **Looser option:** The stricter change, not built: apply the guardian-method and minor rule on the bedside lane too. It waits for your agreement because changing `record_consent_on` would change existing behaviour with every switch off.
- **If relaxed:** Not stated in the source.
- **Switch:** Not named (existing bedside consent lane, `compliance_routes` POST `/consent/{pid}`; unchanged with every switch off)
- **Ruling:** None (waits for your agreement)
- **Source:** Wave 9; record W9-48.

### Beds and ADT

**306. A death switches off only the patient's portal login**

- **What is built:** When a death is recorded, only the patient's portal login is switched off (security audit, with the user id kept so a reopen can restore it). No discharge link is sent. Diet orders and the patient's Relay room are left alone.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-P8`
- **Source:** Wave 3; record W3-9.

**307. One discharge outcome per stay**

- **What is built:** Each stay keeps one live discharge outcome, whichever writer records it first (the admin desk or the ADT end of stay). If the other writer records a different type, the stay is flagged 'needs reconciliation' (`needs_reconciliation`). When the desk writes after the ADT end of stay, it corrects that row in place and the desk's value is kept (wave 3 build notes, `W3-B`). A roster file that only says 'discharged' records no guessed type and no outcome.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. In plain words: the rule stops a stay carrying two conflicting outcomes, or a guessed one, in the hospital's discharge figures.
- **Switch:** `ORB_ADT`
- **Source:** Wave 3; record W3-10.

**308. Every bed board read that names a patient is logged, with no throttle**

- **What is built:** Every bed board or awaiting-bed read that names a patient writes one access audit row; if the row cannot be written the read fails (503). There is no throttle, so each open board writes one row every 30-second refresh.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 4; record W4-15.

**309. The housekeeping discharge sweep never touches a bed now in use**

- **What is built:** The housekeeping discharge sweep finds the bed from the ended stay and never changes a bed that a later active admission now holds (`adt-8c`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on
- **Source:** Wave 4; record W4-21.

**310. Deaths left out of readmission counts; the portal never says how a stay ended**

- **What is built:** NABH readmission counts leave out deaths using the recorded end type (shown as `deaths_excluded`); the patient portal says a stay ended with no summary and never states how it ended (`adt-8c`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT` (only ADT-written data differs)
- **Source:** Wave 4; record W4-22.

**311. Admission, bed and end times at most 72 hours back**

- **What is built:** Admission and bed times can be entered up to 72 hours back, never more than 5 minutes ahead, and a bed time never before the admission. Anything older is refused for everyone (400 `beyond_backtime`). The screen mirrors the limit: the end time defaults to when the screen opened, and a time older than the limit (72 hours if the setting cannot be read) or more than 5 minutes ahead is refused.
- **Looser option:** An administrator override beyond 72 hours with a reason (wave 5 deferred, not built).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT_BACKTIME_HOURS` (72)
- **Ruling:** `ADT-P3`
- **Source:** Wave 5; records W5-14 and W5-21.

**312. End of stay windows cannot be closed by accident**

- **What is built:** The End of stay and Reopen windows cannot be closed with Escape or by clicking outside; once an end is recorded the only choices are Done or Recorded in error.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 5; record W5-20.

**313. Lost reply on discharge: reload the chart before retrying**

- **What is built:** If the reply is lost on the discharge route (which has no request key), the doctor is told to reload the chart before trying again.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 5; record W5-22.

**314. Discharge summaries are linked to a stay only when certain**

- **What is built:** A discharge summary is linked to a stay only where that is certain (one admission, or a ledger link); otherwise left blank. NABH and the shadow scorecard count a repeat patient stay by stay, never past the start of the next stay, and leave out history-import encounters. The source asks the shadow-instrumentation owner to review.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on
- **Ruling:** None (amends a `W3-B` pin)
- **Source:** Wave 6; record W6-18.

**315. The ward census counts only complete days**

- **What is built:** Census: a ward's day counts only if it starts on or after the date the ward's movement record is complete, otherwise 'insufficient data', never zero. Hospital totals need every ward that existed that day to be complete. Department accounts see only their wards and no hospital totals. Whole past days only, at most 92.
- **Looser option:** None named. Three points to confirm: census reads are not audited (counts only); ICU means icu, nicu and picu, not hdu; a return to ICU within 48 h is counted on the day the patient left.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** None (`adt-13a`)
- **Source:** Wave 7; record W7-19.

**316. Absconded over 24 hours: a marker only, nothing closes**

- **What is built:** A patient absconded longer than 24 hours gets an advisory marker on the bed tile only; nothing is closed on a timer.
- **Looser option:** None named. Wave 7 deferred lists the rest of `ADT-S3` (a bed manager closing an absconded stay) as not built.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT_ABSCONDED_CLOSE_HOURS` (24)
- **Ruling:** `ADT-S3`
- **Source:** Wave 7; record W7-23.

**317. On leave over 48 hours: a marker only, nothing closes**

- **What is built:** A patient on leave longer than 48 hours gets an advisory marker on the bed tile only; nothing is closed on a timer.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT_LEAVE_PROMPT_HOURS` (48)
- **Ruling:** `ADT-P6`
- **Source:** Wave 7; record W7-24.

**318. Bed holds expire after 2 hours, or 24 for elective and post-op**

- **What is built:** A bed hold lasts 120 minutes for an admission, transfer or other purpose, and 24 hours for elective and post-op; shorter on request, never longer. An expired hold frees the bed at once and is recorded as expired (audited) at the next reservation write.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-P9`
- **Source:** Wave 7; record W7-25.

**319. A held bed takes only the patient it is held for**

- **What is built:** A held bed takes only the admission or patient it is held for, on every route. A hold naming nobody can be used by nobody until released. The refusal never names the patient. The held patient still meets the full cleaning rule.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-S2` (c) (for the held patient)
- **Source:** Wave 7; record W7-26.

**320. Bed indicators refuse periods with incomplete ward records**

- **What is built:** For the NABH bed occupancy (`QI-UTL-01`) and ICU return within 48 hours (`QI-OUT-04`) indicators, a period that starts before any counted ward's bed movement record is complete is refused in words, never counted in part, and no held-back value is kept. Inpatient days then fall back to the admissions figure with the reason printed.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 8; record W8-21.

**321. How ICU returns within 48 hours are counted**

- **What is built:** ICU returns within 48 hours (`QI-OUT-04`): a return in a new admission within 48 hours counts as a return. Moves where the patient died or left (any non-routine end, including one with no type) within 48 hours are left out of the denominator and counted separately. Every ward must have a complete record from the period start, even when one ICU ward is chosen. So the evidence pack's rate can only read higher than the census's same-stay count.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 8; record W8-22.

**322. Bed occupancy uses the midnight census and minimum sizes**

- **What is built:** Bed occupancy (`QI-UTL-01`) uses the midnight census over available beds, whole hospital-local days only, with today left out. The detail also shows licensed-bed and inpatient-day occupancy. A figure needs at least 30 available bed-days; ICU returns (`QI-OUT-04`) need at least 10 decided moves.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT` (floor `ORB_NABH_MIN_N_BED_OCCUPANCY`)
- **Ruling:** `ADT-P4`
- **Source:** Wave 8; record W8-23.

**323. Where each ward's bed count comes from**

- **What is built:** Wards whose bed register is complete take their bed count from the register; the others keep the `ORB_WARD_BEDS` site setting. A complete ward with zero census beds keeps the site setting. A complete ward that the site's ward discovery does not list is still added. Every figure says where it came from, only while the module is on.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Ruling:** `ADT-P5` (product, as recommended)
- **Source:** Wave 8; record W8-24.

**324. Strict locations: wards and beds not in set-up are refused**

- **What is built:** A ward not in bed setup is refused on every path, including corrections and roster moves. A bed not in setup is refused on wards whose register is complete, and kept unlinked with a warning on wards still being set up. The exact place already recorded is exempt. Roster rows that would create an admission at an unlisted place are rejected whole and listed in the dry run.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT_STRICT_LOCATIONS` (needs `ORB_ADT`)
- **Ruling:** `F104` (product ruling, applied)
- **Source:** Wave 9; record W9-5.

### Medication orders

**325. Orb will not start with the lifecycle on and the order store off**

- **What is built:** Orb will not start if the medication lifecycle switch is on while the medication order store is off; the switch is read again each time it is used.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE` (needs `ORB_MED_ORDER_STORE`)
- **Ruling:** `A2`, `MO-R9`
- **Source:** Wave 4; record W4-35.

**326. Medication order history is deleted with an erased record**

- **What is built:** When a patient's record is erased, the medication order history goes with it: its rows can never be edited but can be deleted, and the table is not on the list kept through erasure.
- **Looser option:** Option (b): keep the history through erasure (one registry line plus a delete trigger).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE`
- **Ruling:** `MO-R13` (a)
- **Source:** Wave 4; record W4-37.

**327. Old medicine rows are adopted only when the store is switched on**

- **What is built:** Old medicine rows are brought into the new order store only when the store is switched on, and never as a side effect of simply opening a page.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_STORE`
- **Source:** Wave 4; record W4-38.

**328. Changing orders needs the lifecycle switch**

- **What is built:** Changing medication orders is possible only with the lifecycle switch on (it looks switched off while the store only records); the whole medication order service looks switched off with the store off.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`; `ORB_MED_ORDER_STORE`
- **Source:** Wave 4; record W4-41.

**329. A given dose is tied to a supply only when certain**

- **What is built:** Custody link, scoped down: a given dose is tied to a supply row only when exactly one supply row of the order is delivered, received and not already used by an earlier dose; otherwise it is recorded as 'supply not linked' with the count of unused units. A dose marked not given carries no custody.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on for supply rows linked to a medication order
- **Source:** Wave 7; record W7-34.

**330. The shadow scorecard counts a CPOE medicine at signature**

- **What is built:** With the lifecycle on, the shadow scorecard counts a CPOE or order-set medicine as a clinical action once, at signature, with the signer as the actor. The source asks that this be noted beside any pilot agreement figure.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MED_ORDER_LIFECYCLE`
- **Source:** Wave 7; record W7-43.

**331. Ward Record computers show no outpatient prescriptions**

- **What is built:** On an Orb Ward Record computer all four outpatient prescription routes answer 'drug chart off' before reading any patient or drug, and the prescription panel shows nothing.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_WARD_RECORD`
- **Source:** Wave 8; record W8-19.

### Pharmacy store

**332. Ward stock is off by default**

- **What is built:** While ward stock is off, every ward stock route looks switched off, ward and crash-cart stores cannot be created, the nurse's ward stock section is not drawn, and the system refuses ward stock writes. With it on, a store's department is matched to the hospital's standard department name and an unknown department is refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` (under `ORB_PHARMACY_STORE`)
- **Ruling:** `GRN-RS5` (b)
- **Source:** Waves 3 and 8; records W3-14 and W8-34.

**333. Any failed add-on step undoes the whole stock movement**

- **What is built:** If any add-on step attached to a stock movement fails, the whole stock movement is undone ('block').
- **Looser option:** None named in this bullet (the Deferred list mentions a 'record' mode for the narcotics hook, `ORB_PHARMACY_NDPS_ON_FAILURE`, under `grn-13`).
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (stock movement hook registry; nothing registered yet)
- **Ruling:** `GRN-RS7`
- **Source:** Wave 3; record W3-15.

**334. Easing a drug's controlled status needs a reason and a second approver**

- **What is built:** Moving a drug to a less controlled classification needs a reason and a second person at approval level who is not the requester. It is refused while `NDPS` vault stock exists (NDPS means narcotic and psychotropic drugs; the source does not say whether that means stock of this drug only), and approval re-checks both.
- **Looser option:** None named. Only recommendation (b) is described.
- **If relaxed:** Not stated in the source. In plain words: the rule stops a narcotic or controlled drug being quietly reclassified out of control.
- **Switch:** `ORB_PHARMACY_STORE`
- **Ruling:** `GRN-R20` (recommendation (b))
- **Source:** Wave 3; record W3-16.

**335. Every dispense is all-or-nothing; unclassified drugs are not treated as controlled**

- **What is built:** Every dispense is one all-or-nothing step whose audit row must be written or the dispense fails. A drug that has not been classified is never treated as controlled.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on
- **Ruling:** None (epic `grn-4`, no ruling)
- **Note:** The checker flags this as possibly the less strict side: a drug nobody has classified gets no controlled-drug handling.
- **Source:** Wave 3; record W3-17.

**336. Purchase orders need the vendor's valid drug licence**

- **What is built:** A purchase order needs the vendor's drug licence number and valid-to date (refused 422 `vendor_licence_missing` or `vendor_licence_expired`); a warning shows within 30 days of expiry; the licence is checked again at submit, approve and issue.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Ruling:** `GRN-R10`
- **Source:** Wave 4; record W4-23.

**337. Controlled drugs go only into the NDPS vault**

- **What is built:** NDPS (narcotic and psychotropic) drugs can be ordered and received only into an NDPS vault, and a vault takes only NDPS drugs. An NDPS line needs the vendor's NDPS licence number (at purchase this part is the builder's addition). On transfers, NDPS drugs move only between the vault and the dispensing pharmacy; ward, crash-cart and returns stores are never transfer ends.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Ruling:** `GRN-R14`
- **Source:** Waves 4 and 5; records W4-25 and W5-28.

**338. The purchase order approver must differ from the creator**

- **What is built:** The person who approves a purchase order must be a different person from the one who created it (403 `same_person`); approving and issuing need `pharmacy_store.approve`; cancelling after approval needs the approve tier; a reason is always required.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 4; record W4-26.

**339. The server reorder level is used only while the pharmacy store is on**

- **What is built:** The formulary uses the server's reorder level only while the pharmacy store module is on; with it off the old 'under 30' low-stock rule stays.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 4; record W4-28.

**340. Purchase orders and receipts go through before their number series is set up**

- **What is built:** Purchase orders and goods receipts are not refused while the PHPO and PHGRN number series are unconfigured; there is no route to configure them yet. Each document records whether its series was configured. Both are listed in the deferred lists as to confirm.
- **Looser option:** The built state is already the loose one. The stricter option, refusing purchase orders and receipts until their series is configured, is not built.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE` (PHPO and PHGRN number series)
- **Ruling:** `GRN-R11`
- **Source:** Waves 4 and 5; records W4-55 and W5-74.

**341. Batch cost includes GST**

- **What is built:** A batch's cost per unit is its landed total (after discount, with GST) divided by paid plus free units; merged batches take a weighted average. Whether GST belongs in the landed cost is to confirm.
- **Looser option:** Leave GST out of the landed cost (to confirm).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Ruling:** `GRN-R12`
- **Source:** Wave 5; record W5-31.

**342. A stale receipt draft cannot be posted**

- **What is built:** A receipt draft posts only while its purchase order is still issued or partly received and every line it names is still open; otherwise it is refused (409) for everyone and can only be cancelled.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 5; record W5-32.

**343. Every controlled-drug transfer needs a PIN witness**

- **What is built:** Every controlled-drug dispatch and receipt needs a different, active `pharmacy_store.ndps` holder confirming with their own signing PIN, whether the NDPS register is on or off; the PIN locks after repeated wrong tries (429); wrong PINs audited. Open point: as built, the person who dispatched a controlled transfer can be the witness when it is received.
- **Looser option:** None named; the open point is whether to stop the dispatcher from witnessing the receipt.
- **If relaxed:** Not stated in the source.
- **Switch:** `pharmacy_transfer_service.NDPS_WITNESS_ALWAYS` = True (under `ORB_PHARMACY_STORE`)
- **Source:** Wave 5; record W5-33.

**344. Controlled-drug transfers need the NDPS permission**

- **What is built:** Every action on a controlled-drug transfer needs `pharmacy_store.ndps` (403 `ndps_permission_needed`, audited).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 5; record W5-34.

**345. Short receipts need a reason and are booked as not arrived**

- **What is built:** A short or zero receipt always needs a reason; the shortfall is recorded as not arrived, never put back or written off by the receiver; receiving more than was sent is refused; a dispatched transfer can never be cancelled.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 5; record W5-36.

**346. The transfer receiver must differ from the dispatcher**

- **What is built:** The person receiving a transfer must differ from the dispatcher (403 `same_person`, also a database check); the witness must differ from the person acting.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 5; record W5-37.

**347. The NDPS register stays off unless both settings are set**

- **What is built:** The narcotics and psychotropics (NDPS) register stays off unless both NDPS settings are set, and this is re-checked on every stock movement.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_NDPS_REGISTER` (needs `ORB_PHARMACY_NDPS_ON_FAILURE` set too)
- **Ruling:** `GRN-RS7` (held)
- **Source:** Wave 7; record W7-45.

**348. A witness for every NDPS loss, adjustment and vault move**

- **What is built:** A witness is required for wastage, destruction, write-off, adjustment, count variance, vendor return and every transfer into or out of the vault: a second active NDPS permission holder confirming with their own signing PIN, never the person acting. 5 wrong PINs in 15 minutes locks; a refused PIN is audited.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_NDPS_REGISTER` + `ORB_PHARMACY_NDPS_ON_FAILURE`
- **Ruling:** `GRN-R14` (made stricter)
- **Note:** From the checker, reading wave 7 deferred: `GRN-R14`'s 'every variance drafts an incident' was built as a one-click report (needs `ORB_NABH` on), not as an automatic draft. That is softer than the ruling's wording.
- **Source:** Wave 7; record W7-48.

**349. No ward use or outpatient sale of NDPS drugs while the register is on**

- **What is built:** While the register is on, ward consumption and outpatient sale movements, and any movement of a lot not held in a pharmacy store, are refused for a narcotic or psychotropic.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_NDPS_REGISTER` + `ORB_PHARMACY_NDPS_ON_FAILURE`
- **Source:** Wave 7; record W7-49.

**350. Rules for returns to the supplier**

- **What is built:** Posting a return to the supplier needs approval rights and never the person who drafted it. Only held, expired or within-90-days lots; units and eligibility are re-checked at posting; another vendor's lot is refused. A narcotic or psychotropic return needs the NDPS permission to draft or cancel, the vendor's NDPS licence on file and a third person witnessing by PIN at posting. A ward return awaiting inspection may leave by a vendor return; putting it back on the shelf stays refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE` (NDPS checks apply register on or off)
- **Ruling:** `GRN-RS3` (`grn-22`)
- **Source:** Wave 7; record W7-51.

**351. Short controlled-drug receipts raise a variance per line**

- **What is built:** A controlled-drug transfer received short raises one count-mismatch variance per short line, with a security audit record, in the receipt's own step.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_NDPS_REGISTER` + `ORB_PHARMACY_NDPS_ON_FAILURE`
- **Source:** Wave 7; record W7-52.

**352. Reopening an NDPS register that differs from the shelf**

- **What is built:** Reopening a register that differs from the shelf is allowed, but one reconciliation-mismatch variance and a security record (fail-closed) are raised first in the same step unless one is already open; if either fails, the opening is refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_NDPS_REGISTER` + `ORB_PHARMACY_NDPS_ON_FAILURE`
- **Source:** Wave 7; record W7-53.

**353. NDPS variances never absorb a later loss**

- **What is built:** Variances belong to the register they were raised against. Only open variances of the current register count as covering a difference, so a reconciled variance never absorbs a later loss: a loss can be recorded twice but never left out. A variance is raised only in the direction the register differs, and nothing is netted away.
- **Looser option:** None named.
- **If relaxed:** The source states the trade-off: covering differences with reconciled variances could leave a later loss out of the register.
- **Switch:** `ORB_PHARMACY_NDPS_REGISTER` + `ORB_PHARMACY_NDPS_ON_FAILURE`
- **Source:** Wave 7; record W7-54.

**354. Dispense quantity is capped at 1 unit by default**

- **What is built:** The pharmacist types the dispense quantity as a whole number from 1 to the site maximum, which is 1 by default. With the module off, quantity and batch are ignored.
- **Looser option:** Raise the site maximum above 1 (`ORB_PHARMACY_MAX_DISPENSE_UNITS`).
- **If relaxed:** Stated: billing still charges one unit per pharmacy order and custody names a supply for one dose only, so `grn-16` billing (units times MRP) should land before any site raises the maximum above 1.
- **Switch:** `ORB_PHARMACY_STORE` + `ORB_PHARMACY_MAX_DISPENSE_UNITS`
- **Ruling:** `GRN-R13` (product, applied)
- **Note:** Wave 10 built billing at MRP (entries 400 and 401); check that it charges units times MRP. Custody for several units is still not built (waves 7 and 8 deferred), so keep the maximum at 1 for now.
- **Source:** Wave 8; record W8-27.

**355. A ward can ask only for drugs on its own list**

- **What is built:** A ward store can ask only for drugs on its own active par list (the stock level the ward keeps of each drug; par above 0); anything else cannot be raised, approved, planned or dispatched, and lands quarantined if taken off the list while on its way. A line asks for no more than the store's maximum (or par). One open indent per drug and store.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` (under `ORB_PHARMACY_STORE`)
- **Ruling:** `GRN-RS5` (b)
- **Source:** Wave 8; record W8-36.

**356. Separation of duties on ward indents and receipts**

- **What is built:** The person who raises a ward indent never decides it; deciders hold `pharmacy_store.write` or approve. The person who receives on the ward holds `ward_stock.receive` in that store's department and is never the one who sent it. The pharmacy desk cannot receive a ward transfer, and a free transfer can never reach a ward store.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` (under `ORB_PHARMACY_STORE`)
- **Source:** Wave 8; record W8-39.

**357. Ward accounts see only their own department's stores**

- **What is built:** Stricter reading of the department rule: a ward account sees only stores of its own department (case and spaces ignored); another ward's indent or transfer looks not found; a ward account with no department sees no ward store. The pharmacy desk sees all.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` (under `ORB_PHARMACY_STORE`)
- **Source:** Wave 8; record W8-40.

**358. Rules for issuing and cancelling ward indents**

- **What is built:** One live transfer per indent. Issuing less than approved cuts the rest (a new indent asks for it). The ward can withdraw an indent only before it is decided; the pharmacy until the stock is on its way, with a reason, never while a transfer is live. Only ward imprest stores (the ward's standing stock) are filled (crash carts waited for `grn-23`). On the issue screen a line short of stock sends nothing unless the pharmacist types what there is (capped at what is usable); a line the ward may no longer hold can never be sent; lines with no problem default to everything still to send.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` (under `ORB_PHARMACY_STORE`)
- **Source:** Wave 8; record W8-41.

**359. Nurses hold the ward stock permissions**

- **What is built:** Nurses hold the ward stock permissions (read, receive, raise an indent, record a return), granted since wave 1; the pharmacist may also raise an indent on the ward's behalf.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` (under `ORB_PHARMACY_STORE`)
- **Ruling:** `GRN-R8` (a) (product, applied)
- **Source:** Wave 8; record W8-42.

**360. A drug that becomes NDPS in transit lands quarantined, with a witness**

- **What is built:** If a drug becomes narcotic or psychotropic after it was sent to a ward, the ward still receives it so it is never stranded, but a witness holding `pharmacy_store.ndps` must confirm with their own signing PIN, whatever the other witness settings say; the witness is never the nurse and never the sender. The units land quarantined, recorded on the receipt's log entry and, when the register is on, on the NDPS register.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` (register entry when `ORB_PHARMACY_NDPS_REGISTER` is on)
- **Ruling:** `GRN-R14`; `GRN-RS5`
- **Source:** Wave 8; record W8-43.

**361. The development seed sets illustrative GST on two drugs**

- **What is built:** The development seed sets an illustrative 5 per cent GST, a pack size and schedule H on MED-PANTO and MED-ATENOLOL only where the drug master holds none.
- **Looser option:** None named. To confirm or remove, since the design said GST rates are never seeded.
- **If relaxed:** Not stated in the source.
- **Switch:** Dev profile with `ORB_PHARMACY_STORE` on (seed data, not a clinical rule)
- **Source:** Wave 8; record W8-45.

**362. The receiver of a ward return can never write it off**

- **What is built:** The person who took a ward return in can never write it off, controlled drug or not (checked before any witness PIN and again under the write lock). A return the nurse took in on the ward can be written off by any pharmacist. The screen does not offer Write off to the receiver.
- **Looser option:** Set it to False: the receiver may write off alone, on screen too. The receiver never restocks either way.
- **If relaxed:** Not stated in the source.
- **Switch:** Module constant `pharmacy_store_service.RETURN_WRITE_OFF_NEEDS_SECOND_PERSON` (True)
- **Source:** Wave 9; record W9-21.

**363. NDPS ward returns need a PIN witness**

- **What is built:** Taking in, or writing off, a narcotic or psychotropic return needs `pharmacy_store.ndps` (refused and logged otherwise) and a second active NDPS holder witnessing with their signing PIN, register on or off. A nurse never records a controlled return; the ward brings it to the counter. With the register on, the return entry names the patient and the write-off entry names the witness. Never restocked.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE` (register entries with `ORB_PHARMACY_NDPS_REGISTER` and `ORB_PHARMACY_NDPS_ON_FAILURE`)
- **Source:** Wave 9; record W9-22.

**364. Cart shortfalls need a second person to confirm**

- **What is built:** Units a full cart check finds short leave the books only when a different `pharmacy_store.write` or approve holder confirms; the person who counted never confirms (refused, also blocked in the database). Refusing a check needs a reason and moves nothing.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE` + `ORB_PHARMACY_WARD_STOCK`
- **Source:** Wave 9; record W9-27.

**365. Cart over-counts are never added to stock**

- **What is built:** Extra units, and items the books do not show, are recorded on the cart check and never added to stock.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE` + `ORB_PHARMACY_WARD_STOCK`
- **Source:** Wave 9; record W9-28.

**366. Who records a cart check**

- **What is built:** A cart check is recorded by a `ward_stock.receive` holder (the nurse, own department; an account with no department sees no cart) or a `pharmacy_store.write` holder. The administrator can confirm (approve tier) but not record.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE` + `ORB_PHARMACY_WARD_STOCK`
- **Ruling:** `GRN-R8` (a) (reading)
- **Source:** Wave 9; record W9-30.

**367. Top-ups while a cart check waits**

- **What is built:** While a check waits, units it found short count as used in the top-up suggestion. The top-up is still an ordinary ward indent decided by the pharmacy, capped at the par ceiling. If the check is later refused, the top-up stands.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE` + `ORB_PHARMACY_WARD_STOCK`
- **Source:** Wave 9; record W9-32.

**368. Which ward cupboard a dose is booked against**

- **What is built:** A dose is booked against the single active ward stock cupboard of the patient's department that lists the drug or holds units of it (department names compared ignoring capitals and spaces). With two such cupboards the admission ward decides; if it cannot, nothing is booked (`store_ambiguous`). A crash cart is never drawn down by a dose.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_WARD_STOCK` with `ORB_PHARMACY_STORE`
- **Source:** Wave 10; record W10-17.

### Laboratory

**369. Ketones and blood gases stay as ward bedside tests**

- **What is built:** Ketones, VBG and ABG (blood gas) tests stay as ward bedside tests, outside the lab's order intake. Whether a test is a bedside test is read when the order is activated.
- **Looser option:** None named.
- **If relaxed:** Not stated. Go-live of `lis-4` and `lis-6` is pending `LIS-R18`.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R18`
- **Source:** Wave 3; record W3-20.

**370. What still works while the lab module is being switched off**

- **What is built:** While the lab module is being wound down (draining), no new orders are taken into the lab, the Setup screen is read-only, and the exceptions list stays readable. The order list and cancel still work, but label printing and collection are refused as switched off (`module_disabled`) and the Collect screen is hidden.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB` (module state 'draining')
- **Ruling:** `LIS-R21`
- **Source:** Waves 3 and 4; records W3-22 and W4-34.

**371. Device and monitor readings stay out of the unmatched results queue**

- **What is built:** Readings from bedside devices and monitor coding systems are never put in the unmatched-results queue; only lab results are. Two open questions for you: whether the 'GE' coding system belongs on the monitor list, and whether `csv_replay` data is queued.
- **Looser option:** None named. Two questions to confirm instead.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_RESULT_QUARANTINE`
- **Source:** Wave 3; record W3-27.

**372. When a lab order counts as resulted, cancelled and billed**

- **What is built:** Results are released test by test. The order counts as resulted (fixed result text, and the test becomes ready to bill) only when at least one test is released and every other is released or cancelled. A rejected test holds it back and blocks completion. The sample completes on the same rule. If every test on a lab order is cancelled, the order is cancelled and never billed. A partly cancelled panel still bills the panel code.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R16` (a)
- **Source:** Waves 4 and 6; records W4-31 and W6-32.

**373. Who can cancel a lab order**

- **What is built:** Cancelling a lab order needs `order.write` in the patient's department or `lab.receive`; the phlebotomist and the administrator cannot; a reason is required; released results are kept and unreleased ones withdrawn (`lis-7` cancel).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB` (served while the module drains)
- **Source:** Wave 4; record W4-33.

**374. Lab staff can only prompt a NABH incident, not file it**

- **What is built:** For unlabelled and mislabelled samples the lab gets a prompt only; lab roles cannot file the NABH incident (not in `REPORTER_ROLES`), so the prompt asks the ward in charge.
- **Looser option:** Let lab roles file the incident (a permission change for you, Wave 5 deferred).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_NABH` with `ORB_LAB`
- **Source:** Wave 5; record W5-53.

**375. A new analyser starts switched off**

- **What is built:** A new analyser starts switched off with no key (database check); switching it on needs a key, a supported protocol and connection, and a reason; switching it off needs a reason.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-56.

**376. Analyser keys are shown once and stored only as a fingerprint**

- **What is built:** An analyser key is 32 random bytes shown once and never cached; only a one-way fingerprint of it (SHA-256) is stored; it never appears in a read, list, audit row or log; replacing it needs a reason and kills the old key in the same step. To confirm: whether to use a slower fingerprint that is harder to guess instead of plain SHA-256.
- **Looser option:** None looser; the open point is whether to use a slower fingerprint that is harder to guess instead of plain SHA-256.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-57.

**377. One answer for every failed analyser sign-in**

- **What is built:** When an analyser connects, an unknown id, a missing key and a wrong key all get the same 'unauthenticated' answer, all checked the same way, so the reply's timing gives nothing away; the right key on a switched-off analyser gets '`instrument_disabled`'.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-58.

**378. Only HL7 v2 analysers can be registered**

- **What is built:** Only analysers that send the standard lab message format (HL7 v2) over the network (HTTP or MLLP) can be registered.
- **Looser option:** None you can choose today. ASTM analysers and plain network connections are deferred, not built (`lis-26`, `lis-27`, wave 5 deferred).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Ruling:** `LIS-R14`
- **Source:** Wave 5; record W5-59.

**379. Analyser addresses must be on the local network**

- **What is built:** An analyser's IP address must be private, loopback or link-local (else `host_not_local`); a host name is only checked for spelling, never looked up; no scheme or path.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB` (offline rule)
- **Source:** Wave 5; record W5-60.

**380. Analyser set-up and code mapping are split between two roles**

- **What is built:** Registering analysers, editing details, switching on or off and keys need `lab.interface` (the administrator); the code map needs `lab.master` (the pathologist); neither can do the other's part.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 5; record W5-62.

**381. Every report view, print and export is logged first**

- **What is built:** Every report view, print, list, turnaround read and CSV export writes an access record first, or is refused (503). The turnaround export carries sample numbers only, with spreadsheet formula cells made harmless.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 7; record W7-73.

**382. Only acknowledged criticals count in NABH and turnaround figures**

- **What is built:** In NABH and turnaround figures only an 'acknowledged' event counts; open criticals are never given a time. The NABH `QI-LAB-02` window is 15 minutes by default, and the site must set it to its own policy.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_NABH_LAB_CRITICAL_ACK_MINUTES` (default 15; with `ORB_LAB`)
- **Ruling:** `LIS-R3`
- **Source:** Wave 7; record W7-74.

**383. How turnaround is measured**

- **What is built:** Turnaround is measured from receipt to first release; exactly on target counts as within; a test with no target shows 'No target' (never 0% or 100%) and `QI-LAB-01` reads 'not computable' without targets; simulated results and impossible intervals are left out and counted; hospital-local days; at most 92 days, never past today.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 7; record W7-75.

**384. Lab reports on the Vigil chart load only on demand**

- **What is built:** On the Vigil chart's Labs dialog, the list of laboratory reports loads only when the dialog is opened and is never refreshed on a timer; one report is read only when opened; both reads are logged on the server. A refusal or the lab module being off shows no section; other failures are said in words. The list still shows while the lab module is winding down.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB`
- **Source:** Wave 8; record W8-26.

**385. Interfaced alerts post from a locked-down system account**

- **What is built:** Interfaced alerts are posted by a 'Laboratory interface' system account: lab technician role (no Relay, Sage or all-patient access), no department, an unusable password, kept inactive on every post. User Management hides it and refuses changes. Its posts are marked as from a bot with source '`lab_interface`'.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_CRITICAL_INTERFACED`
- **Source:** Wave 8; record W8-50.

**386. Should outside labs' criticals count in the lab's own NABH figures?**

- **What is built:** Extra item from the `W8-G` deferred list, not a built strict default: the lab's turnaround and NABH critical figures (`lab_tat_service.critical_times` and `nabh_service`) count every raised critical event, including those from an outside interfaced lab; the events carry `source_lane` 'interfaced'.
- **Looser option:** Keep outside labs' criticals out of the laboratory's own turnaround and NABH figures. Whether an external lab's criticals count toward `QI-LAB-02` is a ruling for you.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_CRITICAL_INTERFACED`
- **Ruling:** None (open ruling for you)
- **Source:** Wave 8; record W8-71.

### Patient portal

**387. Readmission reuses the portal login with a fresh password**

- **What is built:** On readmission the patient keeps their single patient portal (Bridge) login, but a fresh one-time password is shown once and every session from the earlier stay is signed out; this is logged, and refused if the log cannot be written. A patient with no login gets one. A login an administrator switched off is left untouched and no password is shown.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_READMISSION`
- **Ruling:** `R029` (2) (decided)
- **Source:** Wave 8; record W8-6.

**388. No lab result reaches the portal on a timer by default**

- **What is built:** Unset by default, so no lab result reaches the patient portal by itself, only by a doctor's release. If set: whole hours 1 to 8760; 0, negatives, fractions and values above a year read as unset, with a warning at start. The delay runs from each version's own release, and a version shows only if it was still current when its delay ran out (an amended or withdrawn version never reaches the patient by the delay; an unreadable end means never; a tie counts as not shown).
- **Looser option:** Set a delay of 1 to 8760 hours so results reach the portal without a doctor's release. Option (b), showing results at once, is not available: 0 reads as unset.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_PORTAL_DELAY_HOURS` (unset by default; under `ORB_LAB_PORTAL_REPORTS`)
- **Ruling:** `LIS-R11` (c)
- **Source:** Wave 8; record W8-52.

**389. Portal lab reports stop while the lab module winds down**

- **What is built:** Portal lab reports work only while the laboratory module is fully on; while it is winding down or off, every portal lab route looks not found.
- **Looser option:** Keep serving already released reports while the lab module winds down.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_PORTAL_REPORTS` while `ORB_LAB` is 'on'
- **Ruling:** `LIS-R11`; `LIS-R21` (strict reading)
- **Source:** Wave 8; record W8-55.

**390. Every portal lab read is logged**

- **What is built:** Every non-empty list and every report a patient reads writes an access-log entry marked as self-access; if it cannot be written the read is refused. The ward's portal-state read is logged by the usual patient access check. The release is logged as a clinical action, in its own step, refused if logging fails.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_PORTAL_REPORTS`
- **Source:** Wave 8; record W8-58.

**391. The portal release reuses the discharge publish permission**

- **What is built:** Extra item from the `W8-J` deferred list: the doctor's release of a lab report to the patient portal reuses the existing `discharge.publish` permission; no dedicated release permission was made.
- **Looser option:** None named as looser. The alternative is a dedicated release permission instead of `discharge.publish` (to confirm the reuse).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_LAB_PORTAL_REPORTS`
- **Ruling:** `LIS-R11`
- **Source:** Wave 8; record W8-70.

### Billing and money

**392. The OPD consultation is charged at completion**

- **What is built:** The consultation charge is raised when the visit is completed. If set to charge at registration, a cancelled visit (including left without being seen) voids a still-pending prepaid line, and an already-invoiced line is left for a refund at the payments desk. A registration fee is charged only while a registration-fee rule exists. A follow-up visit maps to OPD-FOLLOWUP or the rule's code and stays unpriced until the site prices it, never Rs 0.
- **Looser option:** `ORB_OPD_CHARGE_AT` 'registration' (charge at registration).
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD_CHARGE_AT` (ships 'completion')
- **Ruling:** `OPD-N6`
- **Source:** Wave 7; record W7-4.

**393. An outpatient bill can never carry inpatient charges**

- **What is built:** An outpatient bill can never carry an admission, or a charge tied to an admission or to any visit type other than outpatient: if one is named the bill is refused and nothing is billed. Its sweep of unbilled charges takes only outpatient charges with no admission. Callers that do not say the bill is outpatient behave exactly as before.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on, only for callers that name the bill as outpatient (`encounter_type` 'op')
- **Source:** Wave 8; record W8-7.

**394. The cashier is refused the day's outpatient list**

- **What is built:** The cashier counter's list of a day's outpatients needs `billing.read`, which only the administrator holds; the cashier is refused. A billing account limited to one department sees only that department's patients by the admission-based rule, so walk-ins with no admission are not shown. Every non-empty list writes a log entry naming every patient shown; if it cannot be written the list is refused.
- **Looser option:** A department-limited desk could see its clinic's walk-ins through the outpatient route; that needs the `opd_scope_drift` whitelist and a ruling.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_OPD`
- **Source:** Wave 8; record W8-9.

**395. Invoice and receipt supplier details never change once numbered**

- **What is built:** Once an invoice has a number, the six supplier columns printed on it never change, including on an invoice numbered before migration 72, which keeps printing the old block. A receipt's supplier columns never change after it is written. Deleting is not refused. (Details from the wave 9 'Always on' list.)
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (migration 72 triggers)
- **Source:** Wave 9; record W9-39.

**396. Bed days that cannot be priced are not charged**

- **What is built:** When bed days are posted by bed class, a day the system cannot price is not charged, and the posting result names the reason: the patient was in no bed at midnight, the bed has no class, the class has no bed-day code, the code is not on the price list, or there is no price for that day. It never guesses from the ward's default class, the ward-name tariff or zero. The class used is the bed's class as the bed board shows it today (no class history).
- **Looser option:** Fall back to the ward's default bed class when a bed has none.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 10; record W10-1.

**397. Leave and absconded days in bed-day billing**

- **What is built:** Product ruling already applied. A day the patient is on leave at the midnight count is charged at the class of the bed held for them, or, with the leave-rent setting off, not charged; either way the day is listed in the posting result. A day the patient has absconded is charged as a normal day on the ward.
- **Looser option:** Open question, no looser option named: should absconded days follow the leave-rent setting instead of always being charged?
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT_LEAVE_RENT_CHARGED` (default on), under `ORB_ADT`
- **Ruling:** `ADT-P6`
- **Source:** Wave 10; record W10-2.

**398. Bed days cannot be posted past the stay**

- **What is built:** On the bed-class path, staff cannot post bed days past the end of the stay, or past today for a patient still admitted (refused as `through_after_stay_end`). The older path where staff type a charge code still accepts any date, as before.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_ADT`
- **Source:** Wave 10; record W10-3.

**399. Bed days cannot land on another patient's admission**

- **What is built:** Posting bed days onto an admission that belongs to a different patient than the one on the bill is refused (`admission_patient_mismatch`). This is the only change in this cluster that works even with every switch off; the source says no legitimate request is affected.
- **Looser option:** None named.
- **If relaxed:** Not stated; the wave's always-on list says that before this guard the check covered only the patient named in the request, not the admission.
- **Switch:** Always on
- **Source:** Wave 10; record W10-4.

**400. Pharmacy MRP is treated as tax-inclusive**

- **What is built:** Builder's choice (the architect did not cover GST). A batch's MRP is treated as the top price including tax. For a taxable item the pre-tax value per unit is worked back out of the MRP, so pre-tax value plus GST never goes over units times MRP. A drug with an MRP but no billing item is billed at MRP under `RX-<drug>` with no GST.
- **Looser option:** None named; a stricter option is named instead: refuse to bill at MRP until the drug has a billing item.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_PHARMACY_STORE`
- **Ruling:** `GRN-R12`
- **Source:** Wave 10; record W10-5.

**401. Ward return credits never exceed what was billed**

- **What is built:** A drug returned from the ward is credited to the bill only while that order's charge is still live on the bill, at the same unit rate and GST, so a credit never exceeds what was billed per unit; otherwise the credit is skipped with `order_not_charged`. Every return is credited whatever its inspection outcome (pending, written off or restocked).
- **Looser option:** None named.
- **If relaxed:** Not stated; the source gives the reason for the rule: a credit should never exceed what was billed per unit.
- **Switch:** `ORB_PHARMACY_STORE`
- **Source:** Wave 10; record W10-6.

### Multi-site: set-up, fence and staff accounts

**402. Fence refusals logged once a minute per address**

- **What is built:** One audit record per client address and screen group per 60 seconds, carrying the count of refusals in between.
- **Looser option:** None named. The named alternative is stricter: one audit record per refusal. You are asked to confirm the throttle.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (fence audit throttle, 60 s)
- **Source:** Wave 7; record W7-77.

**403. The department list is seeded from existing staff spellings**

- **What is built:** The departments list is seeded so the spelling staff accounts already carry wins (covering roles first, in the front desk's order). Names are unique ignoring case. Variants are reported by the ops health check and never rewritten.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (migration 70 seeds the departments master)
- **Ruling:** `MS-R4` (a)
- **Source:** Wave 7; record W7-80.

**404. Facility reasons come first in refusal logs**

- **What is built:** When the patient's latest encounter is at another facility, a refusal counts as caused by the facility even if the department also fails, and both access checks then write a refusal log entry (including the one that writes none for a department-only refusal).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on, keyed on data (more than one facilities row), whatever `ORB_MULTI_SITE` says
- **Source:** Wave 8; record W8-66.

**405. One timezone per appliance**

- **What is built:** One timezone per appliance: a facility's timezone is blank (the appliance's own zone) or a named zone whose offsets match the appliance zone in January and July; anything else is refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on
- **Ruling:** `MS-R8`
- **Source:** Wave 9; record W9-37.

**406. One legal entity per appliance**

- **What is built:** One legal entity per appliance: every facility's PAN (its own PAN or characters 3 to 12 of its GSTIN) must match; a second PAN is refused, and the create check lists the PAN rule if the table already holds two.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on
- **Ruling:** `MS-R1` (a)
- **Source:** Wave 9; record W9-38.

**407. Rules for deactivating a facility**

- **What is built:** Deactivating a facility goes through the full create check as well; facility 1 is always refused; a reason is required; refused while outpatient visits or cash counter sessions carry no facility (until `multisite-4` / 7b).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE`
- **Note:** `multisite-4` and 7b were built in wave 11 (migrations 74 and 75), so new visits and counter sessions carry a facility. Older rows may not, because there is no backfill (`W11-C`, see Also open). An engineer should check whether this refusal still fires before you confirm it.
- **Source:** Wave 9; record W9-40.

**408. Credit note supplier snapshot, and fixed facility codes**

- **What is built:** Two product choices: a credit note takes a fresh supplier snapshot when its number is assigned, not the original invoice's; a facility's code is fixed once created.
- **Looser option:** None named. To confirm both choices.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on
- **Source:** Wave 9; record W9-41.

**409. Group-wide grant holders are protected from other admins**

- **What is built:** Builder's strictest reading: only another grant holder may reset the password, set the PIN, deactivate or activate, edit or move an admin who holds a live group-wide grant (others refused and logged), even with only one facility on record.
- **Looser option:** Protect grant holders only once more than one facility exists.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (inert while no grant exists)
- **Source:** Wave 9; record W9-43.

**410. Losing the admin role revokes a group-wide grant**

- **What is built:** Changing an admin's role away from admin revokes their live group-wide grant in the same step, with a security log entry (refused if it fails). Deactivating the account does not revoke it; reactivating it is for grant holders only.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Not named in the bullet (inert while no grant exists)
- **Source:** Wave 9; record W9-44.

**411. Facility-bound admins manage only their own facility's staff**

- **What is built:** An admin tied to certain facilities lists only those facilities' accounts and departments; every create, edit, password reset, PIN, deactivate, activate or move outside them is refused and logged. A department the master list does not hold is refused unless the admin is group-wide. An account with no department defaults to the creating admin's facility, with its home record written in the same step.
- **Looser option:** List every account and only refuse changes.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on, keyed on data (more than one facilities row)
- **Source:** Wave 9; record W9-45.

**412. With multi-site off, only the main facility is allowed**

- **What is built:** With multi-site off, any home facility other than facility 1 is refused (including the default for an admin at a second facility creating staff with no department on a restored two-facility database); the move and give-grant routes look switched off; `create_admin.py` `--facility` accepts only MAIN. Facility labels still show while two facilities are on record.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE`
- **Source:** Wave 9; record W9-46.

**413. An account's home facility follows its department**

- **What is built:** An account's home facility follows its department. For an account that has a department, a separate home facility cannot be set or moved (refused). Giving an account a department ends its home facility record. Removing a department while more than one facility is on record writes a home record at the facility the account worked at. A move is an end plus a new record in one locked step.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Not named in the bullet (`W9-H` facility users; moves need `ORB_MULTI_SITE`)
- **Source:** Wave 9; record W9-47.

**414. Number prefixes are checked across every site**

- **What is built:** Strictest reading. A new or changed number prefix is checked against every other series that writes into the same list of numbers at any site, the site's own included (for example PO against PHPO for purchase orders), and against every number already issued there. The check runs only when a prefix is set or changed. The counter, document type, financial year, reset rule and check digit can never be edited.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` (number series editor)
- **Source:** Wave 10; record W10-10.

**415. Department names are unique across the group and fixed once used**

- **What is built:** A department name already used at any site is refused, ignoring capitals, outer spaces and doubled inner spaces. Once any record or a Relay department room names a department, its site can never change; only an unused department can move, with a reason and a security audit row. The main site's list can still be completed with the switch off. Names that look like a bed number prefix are refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` (for any site other than the main one, and for any move)
- **Ruling:** `MS-R4`
- **Source:** Wave 10; record W10-12.

**416. Department spelling is saved from the master list**

- **What is built:** When a department is given to a staff account in User Management, it is saved in the master list's own spelling once there is more than one site. With one site the typed spelling is saved as before.
- **Looser option:** None named; the open question goes the other way: should one-site hospitals also save the master spelling (this would change current behaviour)?
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record)
- **Source:** Wave 10; record W10-14.

**417. Unknown site labels go to the main site at upgrade**

- **What is built:** When the upgrade runs, a ward's site code or a lab specimen's site label that matches no known site is left at the main site instead of stopping the server from starting. The wave notes no such row exists today.
- **Looser option:** None named; the other option the line names is stricter: refuse at start-up.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (migration 74)
- **Note:** The checker notes this one is built at the looser end: the stricter option is to refuse at start-up.
- **Source:** Wave 11; record W11-6.

### Multi-site: patient access, registration and OPD

**418. The department decides a visit's site**

- **What is built:** The department decides the site. An outpatient visit or clinic booking takes the site of the clinic's active department in the master list, never the desk's site. A visit to a clinic the list does not have is refused in plain words. A booking with no listed clinic takes the patient's current site, so it never moves the patient. An emergency case is booked at the patient's current site.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record)
- **Ruling:** `MS-R4`
- **Source:** Wave 11; record W11-5.

**419. A booking can be handled only at the facility that made it**

- **What is built:** A booking is shown, and can be cancelled, completed, checked in, marked no-show, rescheduled or counted in the forecast schedule, only at the facility where it was made, before the department rule is applied. A front desk at facility 2 cannot check in a booking made at facility 1.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `appointments_routes._appointment_visible` and `_appointment_or_404`
- **Ruling:** `MS-R5` (a)
- **Note:** The wave 12 checker called this flag debatable: it is marked as not guarding a patient, while entry 249 is marked as guarding one.
- **Source:** Wave 12; record W12-4.

**420. A new patient is registered at the registrar's facility**

- **What is built:** A new patient is recorded as registered at the registrar's facility (the department's facility, else the account's home facility, else facility 1). If the registrar's facility cannot be worked out, registration is refused (`facility_scope`) and audited.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Keyed on data; registration sits behind `ORB_REGISTRATION`; audit action `registration_facility_unknown`
- **Ruling:** `MS-R5` (a), strict reading
- **Source:** Wave 12; record W12-46.

**421. OPD queue actions are refused outside the user's facilities**

- **What is built:** Inside the OPD queue itself, Orb refuses skip, recall, assign, or a priority or room change on a visit outside the acting account's facilities, whatever the screen already checked. With more than one facility on record, an action with no named actor is refused. Token display boards are created and switched off only for the person's own facility's clinics. The wave 12 deferred list (`W12-F`) notes that listing boards does not yet receive the actor.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Keyed on data; OPD sits behind `ORB_OPD`; `opd_queue_service._load(actor_id=)`
- **Source:** Wave 12; record W12-47.

**422. Duplicate ABHA refusals name no one outside scope**

- **What is built:** When an ABHA health ID is refused as a duplicate in a group, the refusal names no one if the registration screen gives no facility scope, and a user with a scope is told only about clashes inside their own facilities. The queue of visits not yet linked to ABDM follows each visit's own facility.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `abdm_service` `SCOPE_NOT_GIVEN` sentinel on `record_abha` and `record_abha_on`; `unlinked_encounter_queue(facilities=)`
- **Source:** Wave 13; record W13-8.

### Multi-site: beds, admissions and imports

**423. Bed classes stay group-wide and any admin can edit them**

- **What is built:** Bed classes stay group-wide. Saving a bed class has no facility check (bed classes carry no facility; masters sit at group level by design), so any facility-bound administrator can change a bed class that affects bed-day billing at every facility. To confirm: whether bed-class edits should need a group-wide (enterprise) grant.
- **Looser option:** None named; this is already the looser side. Stricter option named: require an enterprise grant for bed-class edits.
- **If relaxed:** Stated: one facility's administrator can change bed-day billing for every facility.
- **Switch:** None: no facility check in `save_bed_class`
- **Source:** Wave 12; record W12-48.

**424. Beds at no facility are shown on their own line**

- **What is built:** A ward counts toward a facility's bed capacity only if the bed master places it there, or if all its open stays are at that one facility. Otherwise its beds count for no facility, and the group view shows them on their own line as 'Beds at no facility'.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `forecast_service.ward_facilities` and `capacity_for`; Command Center `unplaced_capacity`
- **Ruling:** `MS-R5`
- **Source:** Wave 13; record W13-5.

### Multi-site: Sage, Relay, surgery, paging and live screens

**425. Shadow-mode episodes limited to the user's facilities**

- **What is built:** For an account limited to some facilities, shadow-mode episodes about patients at other facilities, and episodes with no patient, are hidden. The house-wide total is not shown. An episode's detail answers 'not found' unless every patient it names is at the person's facilities.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `shadow_routes._patients_at_facilities`
- **Source:** Wave 12; record W12-8.

**426. Records of Sage answers are limited by facility**

- **What is built:** An account with no department that is bound to a facility can see and replay Sage turns about patients only inside its own facilities, and sees no turn that had no patient (such a turn records a department but no facility). Turns made by such accounts, and any turn whose scope cannot be read, are recorded as not hospital-wide (`hospital_wide` 0).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `sage_provenance_service._facility_bound`
- **Source:** Wave 12; record W12-12.

### Multi-site: medication orders, pharmacy and stores

**427. Stores, indents and orders stay within one site**

- **What is built:** Strict reading of `MS-R9` (a). A store takes its department's site, else its ward's, else its in-charge's; a store nothing places is refused (`facility_unknown`), and a store keeps its site (a department or ward at another site is refused). A stores request's store, the store it asks and its department must be at the same site; a purchase order's store and request must match; goods received into a store at a different site from the order are refused. One active dispensing store per site.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record)
- **Ruling:** `MS-R9`
- **Source:** Wave 11; record W11-13.

**428. A dispense uses only the patient's own facility's stock**

- **What is built:** A dispense takes stock only from batches at the patient's own facility (their active admission, else their latest visit), still picking in-date stock with the earliest expiry first. If that facility has none, the dispense is refused and the facility is named; stock is never drawn from another facility. A batch the pharmacist names at another facility is treated as not found (`lot_not_found`), and the stock check before an order, for a named patient, counts the same shelf.
- **Looser option:** None named.
- **If relaxed:** Not stated. The line rules out drawing a patient's stock from another facility.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `ORB_PHARMACY_STORE` for the shaped dispense; `pharmacy_stock_service.pick_fefo_lot` and `choose_dispense_lot` (`facility_id=`), `FACILITY_PATIENT_SQL`
- **Ruling:** `MS-R9` (a)
- **Note:** The wave 13 checker questioned this flag against entry 256: this one is marked as not guarding a patient, entry 256 as guarding one.
- **Source:** Wave 13; record W13-19.

**429. A batch is at a facility only if its records agree**

- **What is built:** A batch counts as being at a facility only when its own facility record and its store's facility agree. A batch where the two disagree cannot be dispensed anywhere, is hidden from facility-bound lists and is reported in the stock reconciliation. Old stock with no store belongs to facility 1 and is taken up only by facility 1's dispensing store.
- **Looser option:** Let the store's facility alone decide where a batch is.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `pharmacy_facility.lot_sql`; reconcile() `facility_mismatches`
- **Ruling:** `MS-R9` (a)
- **Source:** Wave 13; record W13-20.

**430. Stock transfers never cross facilities**

- **What is built:** A stock transfer never crosses facilities, even for a group-level account. It is checked when drafted and again when dispatched, and anything else is refused (409 `cross_facility_transfer`) with nothing written. A ward indent is filled only from the dispensing store at the ward store's own facility, else it is refused (`no_dispensing_store`).
- **Looser option:** An audited transfer between facilities backed by a drug licence document.
- **If relaxed:** Not stated beyond the reason the line gives: a drug licence is held per premises.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `pharmacy_transfer_service._check_one_facility`, error `cross_facility_transfer` (409); `no_dispensing_store`
- **Ruling:** `MS-R9` (a)
- **Source:** Wave 13; record W13-21.

**431. Another facility's store documents look not found**

- **What is built:** Another facility's store, purchase order, goods receipt, transfer, return to vendor, ward indent, crash-cart check or NDPS variance answers 'not found' and is logged, before anything is read, a witness PIN is asked for or anything is written. Creating a store at another facility is refused (403 `facility_scope`). Every list, report, option list and the crash-cart batch trace keeps to the user's own facilities.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `pharmacy_facility.gate` and `document_outside` (lane '`pharmacy_store`'); MasterError `facility_scope` (403)
- **Ruling:** `MS-R5` (a); `MS-R9` (a)
- **Source:** Wave 13; record W13-22.

**432. Reorder levels are judged per facility shelf**

- **What is built:** A drug's reorder level is judged against each facility's own shelf. A group-wide report shows one default reorder row per drug for each facility.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `pharmacy_stock_reports.reorder_report(facilities=)`
- **Ruling:** `MS-R9` (a)
- **Source:** Wave 13; record W13-23.

**433. Ward returns go back to the dispensing facility**

- **What is built:** Drugs returned from a ward go only into a returns quarantine store at the facility the batch was dispensed from; if that facility has none, the return is refused (`no_returns_store`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `pharmacy_store_service._returns_stores` and `_return_facilities`, error `no_returns_store`
- **Ruling:** `MS-R9` (a)
- **Source:** Wave 13; record W13-24.

**434. NDPS witnesses can be from any facility**

- **What is built:** Witnesses for NDPS (controlled drug), transfer and ward-receipt records can still be any eligible pharmacist anywhere in the group.
- **Looser option:** None named. This is already the looser setting (no architect option named); the source names a stricter option: a witness must work at the document's facility. To confirm: whether a witness may work at another facility.
- **If relaxed:** Not stated in the source.
- **Switch:** None built; the stricter option's hook is `pharmacy_facility.account_works_at`
- **Source:** Wave 13; record W13-26.

### Multi-site: laboratory and device feeds

**435. The printed lab report names the performing facility**

- **What is built:** With more than one facility, a printed lab report carries the configured name of the facility whose lab performed the sample, and no name if nobody set one. It never carries facility 1's name for a satellite's sample.
- **Looser option:** None named.
- **If relaxed:** Not stated. The line rules out a satellite's report carrying facility 1's name.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `routes/lab/reports.py` `_performing_hospital_name`
- **Source:** Wave 13; record W13-34.

**436. Bench counts are limited to the user's facilities**

- **What is built:** Under a facility limit, the lab bench section counts include only samples performed at the user's own facilities, for every account.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `lab_result_service.list_sections(facilities=)`, GET `/api/lab/worklists`
- **Source:** Wave 13; record W13-35.

### Multi-site: billing and money

**437. A second site's receipts wait for its own number series**

- **What is built:** Once a second hospital site exists, a receipt, refund, insurance claim or credit note for a patient at that site is refused in plain words before anything is saved, until that site has its own number series. It is never numbered from the main site's series. The source notes this could turn money away at a counter, but only after a second site is added, which the site-create gate blocks today.
- **Looser option:** Fall back to the main site's (facility 1's) number series.
- **If relaxed:** Not stated; the source names only the cost of the strict choice: money could be refused at a counter once a second site exists.
- **Switch:** None; keyed on data (a second facility on record); series set-up behind `ORB_MULTI_SITE`
- **Source:** Wave 10; record W10-7.

**438. A receipt belongs to the counter's site**

- **What is built:** A receipt belongs to the cash counter's site, else the bill's, else the patient's, and is numbered from that site's receipt series with that site's details printed. A counter at one site taking money for another site's bill is refused in plain words before a number is used. A refund is paid only at a counter of its own site. Opening a counter from an account whose site cannot be told is refused.
- **Looser option:** Let the bill's site decide instead of the counter's.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record)
- **Source:** Wave 11; record W11-11.

**439. Credit notes, invoices and claims follow the bill's site**

- **What is built:** A credit note is issued by the bill's own site (its stamp, invoice series and printed details); an issued invoice prints its own site's details; reversals and refunds stay with the receipt's site; a claim goes with the bill's site, and a bill and admission at different sites are refused. Known gap from the wave's deferred list: the invoice number at issue still comes from the main site's series (harmless with one site).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record)
- **Source:** Wave 11; record W11-12.

**440. A counter cannot collect on a bill once the patient moved facility**

- **What is built:** Once a patient's latest visit or stay is at facility 2, facility 1's counter cannot open or collect on that patient's old facility 1 bill. Screens that already checked the patient now also check the money document's own facility (the bill, receipt or other document). Screens that did not check the patient before (reading or reversing a receipt, refund and discount decisions, counters, claims by id, visits by id) check only the document's facility.
- **Looser option:** Let the document's own facility alone decide on reads addressed to a document ('plus the facility's own invoices').
- **If relaxed:** Not stated. Strict-side cost, from the line: facility 1's counter cannot collect on its own old bill once the patient has moved.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `enforce_patient_scope` plus the document's facility (`_require_own_document` in billing, payments and payer routes)
- **Ruling:** `MS-R5` (a)
- **Source:** Wave 12; record W12-32.

**441. Money actions check the admission's own facility**

- **What is built:** Every money action that names an admission checks that admission's own record, whichever patient it belongs to: billing charges, charge capture (dry runs too), bed days and the bill; payment receipts, applying a deposit, refunds and discounts; payer claims and policies. A stay at another facility answers 'Admission not found' and writes a refusal audit row before anything is numbered or saved. A discount naming another facility's charge line is refused the same way.
- **Looser option:** A check bound to the patient instead of the admission (named and rejected by the builder).
- **If relaxed:** Stated as the reason for rejecting it: the payments and payer services stamp from any admission row, so a patient-only check would not stop money landing on another facility's stay.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `billing_routes` and `payments_routes` `_require_own_admission`, `facility_documents.admission_row_facility`; audit action `facility_document_denied`
- **Ruling:** `MS-R5` (a)
- **Source:** Wave 12; record W12-33.

**442. A bill gathers only the user's facility's charges**

- **What is built:** When a bill is made without naming charges, Orb gathers only pending charge lines stamped at the person's own facilities. Another facility's lines stay pending for its own counter, and the bill reports `no_charges` if none are left. A group-wide account gathers as before.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `billing_service.create_invoice(facilities=)`
- **Ruling:** `MS-R5` (a)
- **Source:** Wave 12; record W12-34.

**443. Patient money summaries still add up every facility**

- **What is built:** Left as before: once a patient is in scope, the patient's billing summary, payments summary, the discharge money advisory, and the admission-specific deposit, summary and charges reads still add up the patient's money at every facility (the whole-record reading of `MS-R2` (a)). The wave 12 deferred list (`W12-E`) adds that the admission-specific reads are not checked against the admission.
- **Looser option:** None named; this is already the looser side. Stricter option named: show only the caller's facility's documents. To confirm: whether a desk should see a patient's money totals from every facility.
- **If relaxed:** Not stated. As built, a desk sees the patient's money totals from every facility.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: GET `/api/billing/patients/{id}/summary`, `/api/payments/patients/{id}/summary` and their `admission_id=` variants
- **Ruling:** `MS-R2` (a)
- **Source:** Wave 12; record W12-38.

**444. Where policies and discount requests belong**

- **What is built:** Policies and discount requests carry no facility of their own. A policy belongs to the facility of the patient's latest visit or stay (or of the policy's admission); a discount request belongs to its bill's facility, or else the patient's.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `facility_documents.policy_facility` and `discount_facility_condition`
- **Source:** Wave 12; record W12-39.

**445. Outpatient charges are stamped with the visit's facility**

- **What is built:** Outpatient charge lines are now stamped with the visit's facility (before, every line was stamped facility 1), so a satellite's outpatient charges appear only on its own counter. With one facility nothing changes.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `opd_charges_service.raise_op_line_on(facility_id=)`
- **Source:** Wave 12; record W12-40.

**446. Interstate GST is worked out from the place of supply**

- **What is built:** On a multi-site setup, when a bill's place of supply names an Indian state (two-digit GST code or state name), Orb decides whether IGST applies by comparing it with the billing facility's state (its state code, else the first two digits of its GSTIN), overriding the interstate tick the user gave. The user's tick decides only when either state cannot be told, and always on a single-facility install with the switch off.
- **Looser option:** Work out interstate GST from the place of supply on single-site installs too.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` on, or more than one facility on record (either one turns it on); `billing_masters.interstate_derivation_on` and `invoice_interstate_on`; invoice metadata `interstate_source`
- **Source:** Wave 13; record W13-14.

**447. Who may edit a price list**

- **What is built:** Under facility scope, a user tied to one facility may change only the price lists kept for their own facility; another facility's list answers 'not found'. Changing a group-wide list's rates, packages or plan, creating a new list (a new list applies to the whole group), and moving a list into or out of the group all need group-wide reach (an enterprise grant), else they are refused (403 `group_wide_access_required`). Every refusal is written to the audit log.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Keyed on data: only while more than one facility is on record (no switch of its own) for POST `/tariff-plans`, `/tariff-rates` and `/packages` (`billing_masters.assert_plan_editable`); the two PUT price-list routes also sit behind `ORB_MULTI_SITE` (`feature_gate('multi_site')`)
- **Ruling:** `MS-R6` (a)
- **Source:** Wave 13; record W13-15.

**448. Payer price lists stay group-wide**

- **What is built:** A payer's price list stays group-wide. A payer cannot be set up on a list kept for one facility, a list any payer prices on cannot be moved to a facility, and a payer list kept for another facility is ignored when a rate is worked out.
- **Looser option:** Per-facility payer price schedules.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `billing_masters.upsert_payer`, error `plan_used_by_payer`
- **Source:** Wave 13; record W13-16.

**449. Every facility keeps a default price list**

- **What is built:** No facility is left without a default price list. The group's default list cannot be moved to one facility, a list moved to a facility arrives as not the default, and a facility with no default of its own and no group default falls back to the first active list of its own or the group's, never another facility's.
- **Looser option:** None named.
- **If relaxed:** Not stated. The line rules out a facility falling back to another facility's price list.
- **Switch:** `ORB_MULTI_SITE`; `billing_masters.set_facility_default_tariff_plan` and `set_tariff_plan_scope`, error `group_default_plan`
- **Source:** Wave 13; record W13-17.

**450. Rate lookups with no facility read only group-wide lists**

- **What is built:** When a rate is looked up with no price list named and no facility given, only group-wide price lists are read. While every list is group-wide this changes nothing.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on; `billing_masters.resolve_rate` and `resolve_package_rate`
- **Source:** Wave 13; record W13-18.

### Multi-site: audit, backups, downtime and exports

**451. Old audit history without a facility is group-wide only**

- **What is built:** For a facility other than facility 1 with no recorded starting point in the audit log, Orb counts that start as the very first row (epoch 0). So no old audit row without a facility is handed to facility 1, and a facility 1 administrator bound to facility 1 sees none of that unattributed history. Only group-wide access sees it.
- **Looser option:** Work out the starting point from the date the facility was created (`created_at`).
- **If relaxed:** Not stated. Strict-side cost, from the line: a facility 1 administrator sees none of the unattributed history.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `services/audit_facility.audit_epoch` (the facility's `audit_epoch_id`)
- **Source:** Wave 12; record W12-14.

**452. Audit rows naming no facility are group-wide only**

- **What is built:** Audit rows written after that starting point that name no facility (the system account, unknown accounts, background sweeps with no patient, and rows a writer marked with no facility) are shown only to an administrator with group-wide access. This covers the audit log, its filters, the flight recorder, the admin overview, the staff list, compliance coverage and the impact count.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `services/audit_facility.py` (`facility_unresolved`)
- **Source:** Wave 12; record W12-15.

**453. The flight recorder follows the patient's current facility**

- **What is built:** Flight recorder events and order rows are shown by the patient's facility today, not the facility at the time of the event. Rows with no patient are hidden from facility-bound readers. An event outside the reader's facilities answers 'not found', like one that does not exist.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `flight_recorder_service` `get_timeline` and `get_event_detail` (facilities=), `permissions.FACILITY_PATIENT_SQL`
- **Source:** Wave 12; record W12-16.

**454. An audit row about a patient belongs to the patient's facility**

- **What is built:** An audit row about a patient belongs to the patient's facility, not the staff member's. A facility 1 nurse's refused attempt to open a facility 2 chart is a facility 2 row. A facility the writing code put in the row itself is kept (refusals about staff accounts name the target account's facility).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `audit_service.log_audit_event` attribution order
- **Source:** Wave 12; record W12-17.

**455. Failed logins are filed under the named account**

- **What is built:** A failed login is filed under the account the attempted user id names. If that id matches no account, the row names no facility and only group-wide access sees it.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `audit_service.log_audit_event` (`attempted_id`)
- **Source:** Wave 12; record W12-18.

**456. Unreadable audit rows are hidden**

- **What is built:** An audit row whose facility is not a whole number, or whose details cannot be read in a single-row check, is shown to no facility-bound reader. The tool that builds the audit facility index refuses to run while any row's details cannot be read, and names those rows.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: CLI `server/scripts/build_audit_facility_index.py` (index `idx_audit_facility`)
- **Source:** Wave 12; record W12-19.

**457. The audit facility filter only narrows**

- **What is built:** The facility filter on the audit log only narrows what a reader already sees. A facility outside the reader's scope and a facility that does not exist give the same 'not found'. With one facility, asking for facility 1 changes nothing and any other number is 'not found'.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: GET `/api/audit/logs` `?facility=`
- **Source:** Wave 12; record W12-20.

**458. The audit chain check stays appliance-wide**

- **What is built:** The audit chain check's overall answer (ok, where the chain broke, and the total count) stays appliance-wide by design, so the 'broken at' row may be another facility's row id. The per-patient count counts only rows the reader may see.
- **Looser option:** None named. The overall answer is appliance-wide by design.
- **If relaxed:** Not stated. As built (appliance-wide by design), the 'broken at' answer may name another facility's audit row id.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `flight_recorder_service.verify_chain(facilities=)`
- **Source:** Wave 12; record W12-21.

**459. Facility attribution can lag 30 seconds**

- **What is built:** Orb remembers each patient's and each account's facility for 30 seconds. A chart or account that moves to another facility is credited to the old facility for up to 30 seconds. Lookups that find no patient are not remembered.
- **Looser option:** None named.
- **If relaxed:** Not stated. As built, for up to 30 seconds after a chart or account moves, its audit rows are credited to the old facility.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `services/audit_facility.py` `CACHE_SECONDS` 30
- **Source:** Wave 12; record W12-22.

**460. The facility extract needs a reason and audits first**

- **What is built:** The extract tool needs a reason. It never writes over a folder that already has files in it. It writes one patient-data access audit row per included patient plus a summary row, in one step, before the extract is moved into place, and leaves nothing on disk if those rows cannot be written. A dry run writes and audits nothing.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `server/scripts/facility_extract.py` (console only); `phi_access` audit actions `facility_extract_written` and `facility_extract_patient_included`
- **Source:** Wave 12; record W12-29.

**461. Downtime 'capture all' covers only the user's facilities**

- **What is built:** The 'capture all' action for downtime snapshots captures only patients at the person's own facilities; an empty scope captures nobody. A group-wide account and the hourly automatic sweep capture every facility.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: POST `/api/downtime/capture-all` passes `permissions.facility_scope(user)`
- **Source:** Wave 12; record W12-31.

**462. Audit filing for visiting sessions**

- **What is built:** The switch's security log entry is filed under the facility the doctor moved to. A record-access entry naming no patient is filed under the session's facility; entries about a patient stay with the patient's facility. Every record read and scope refusal in a visiting session also records the session facility, the visiting entry and the doctor's home department. Other entries with no patient still go to the home facility (deferred).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** `ORB_MULTI_SITE` (`config.feature_enabled('multi_site')`, checked at call time); audit security `session_facility_switched`; `facility_service.session_audit_fields`
- **Ruling:** `MS-R5` (a) (reading of `W12-C`)
- **Source:** Wave 13; record W13-41.

### Multi-site: NABH, Command Center, admin and support services

**463. Numbering for stores, kitchen and NABH documents at a site with no series**

- **What is built:** Wave 10: stores and pharmacy documents, kitchen indents, NABH incident reports and NABH evidence packs kept taking numbers from the main site's series until a later step (`multisite-7b`) gave them their own site. Wave 11 built that step. When a site has no number series of its own, stores documents now stay unnumbered and the gap is logged, never numbered from the main site's series. A kitchen indent is refused at issue in plain words. A NABH incident report is never refused: it is stamped with its own site and numbered from the main site's incident series until its own exists. The checker asks that the wave 10 item not be confirmed on its own.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies once a second facility is on record
- **Note:** Evidence packs: see entries 467 and 468 (wave 13). A pack now takes numbers only from its own facility's NABHPACK series and is refused without one.
- **Source:** Waves 10 and 11; records W10-8 and W11-14.

**464. Wave 11 interim: no NABH pack while two sites exist (replaced by entry 467)**

- **What is built:** While two sites are on record, building a NABH evidence pack is refused (`pack_spans_facilities`) before anything is built, until a later step (`multisite-12`) makes packs per site.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record)
- **Note:** Replaced in wave 13. `multisite-12` was built, so packs are now made per facility (entries 467 and 468) and only a pack with no facility chosen is refused (`pack_spans_facilities`). Confirm entries 467 and 468, not this one.
- **Source:** Wave 11; record W11-15.

**465. Where an incident report is filed**

- **What is built:** An incident report is filed at the patient's site, else the department's, else the ward's, else the reporting account's. An anonymous report never uses the reporter, so with no patient, department or ward it is filed at the main site.
- **Looser option:** None named; to confirm: the main-site fallback for anonymous reports.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record)
- **Source:** Wave 11; record W11-16.

**466. Housekeeping units and machines keep their site**

- **What is built:** Housekeeping units and biomedical machines are given a site once. Re-registering or editing one onto another site's ward or department is refused, and taking over a typed-in bed onto another site's ward is refused.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None; keyed on data (a second facility on record)
- **Source:** Wave 11; record W11-17.

**467. Each NABH evidence pack covers one facility**

- **What is built:** In a hospital group, every NABH evidence pack covers one facility only. A group-level user who does not choose a facility is refused with 'Choose the facility the pack is for. No pack was made.', and both pack buttons stay disabled until a facility is chosen.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `nabh_service.build_evidence_pack(facility_id=)`, error `pack_spans_facilities`; NabhPanel pack buttons
- **Ruling:** `MS-R9`
- **Source:** Wave 13; record W13-1.

**468. NABH packs use the facility's own number series**

- **What is built:** A facility's NABH packs take numbers only from that facility's own pack number series (NABHPACK). If the facility has no series set up, the pack is refused. Facility 1's series is never borrowed.
- **Looser option:** None named.
- **If relaxed:** Not stated. The line rules out a facility's pack being numbered from facility 1's series.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: NABHPACK number series per facility, error `series_not_set_up`
- **Ruling:** `MS-R9`
- **Source:** Wave 13; record W13-2.

**469. NABH lab indicators are not computed per facility yet**

- **What is built:** NABH laboratory indicators are not worked out per facility. They show as not computable, and the pack carries a note saying so, until the lab turnaround-time lane carries the facility. That wait is a code change, not a data change: `nabh_service` has to pass the facility to `lab_tat_service.reported_tests` and `critical_times`, which already accept it.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: module constants `nabh_service.LAB_NOT_PER_FACILITY` and `FACILITY_PACK_LIMITATION`
- **Ruling:** `MS-R9`
- **Source:** Wave 13; record W13-3.

**470. Which facility a NABH event counts at**

- **What is built:** An NABH event counts at the facility of the patient's latest admission, else the facility where they registered, else facility 1. A single hospital stay never spans two facilities. When Orb looks for readmissions after a facility's discharges, it searches every facility.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `nabh_service.indicators(facility_id=)`
- **Ruling:** `MS-R4`; `MS-R3` (c)
- **Source:** Wave 13; record W13-4.

**471. Procurement, housekeeping and dietary records stay at one facility**

- **What is built:** A user cannot place a procurement store or indent, a housekeeping record or a dietary meal plan at another facility. Naming another facility's department, ward or in-charge is refused (403 `facility_scope`). Another facility's existing record, or a ward with no facility, answers 'not found' (404).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: error `facility_scope` (403); 404 for another facility's record or an unplaced ward
- **Source:** Wave 13; record W13-9.

**472. Maintenance contracts are limited to the user's machines**

- **What is built:** A user tied to one facility sees a maintenance contract only if it covers at least one of their machines, and only their own machines are listed and counted on it (single contract, lists, expiring view, overview). They can change a contract only if it covers no machine at another facility and no machine with no facility; otherwise only a group-level account can (403 `facility_scope`). Any contract they create or amend must cover at least one of their own machines (422 `assets_required`).
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: `routes/biomedical_routes` `_contracts_seen` and `_refuse_shared_contract`; errors `facility_scope` (403) and `assets_required` (422)
- **Source:** Wave 13; record W13-10.

**473. Employee placement and group-only shift settings**

- **What is built:** An employee belongs to a facility through their department, else through their linked login. Employees placed nowhere are hidden from users tied to one facility. Whole-department views need a department chosen (422). Shift types and holidays can be changed only by a group-level account (403 `group_setting`). Payroll runs with no department are hidden from users tied to one facility.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: errors `department_required` (422) and `group_setting` (403)
- **Source:** Wave 13; record W13-11.

**474. Retention reviews and breach records by facility**

- **What is built:** A data-retention review is for a group-level account only, unless a single patient is named (403 `group_wide_only`). In a data-breach record, a user tied to one facility sees only the affected patients at their own facilities, plus a count of how many were withheld.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: error `group_wide_only` (403); breach detail field `affected_patient_ids_withheld`
- **Source:** Wave 13; record W13-12.

**475. The admin overview counts only the admin's facility**

- **What is built:** For an admin tied to one facility, the overview's alert and record counts, the alerts list and the house total cover only their facility. Recent parse failures from the incoming data feeds (ingestion: raw message errors with no patient attached) are hidden from them. Ingestion totals still cover the whole appliance.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** None of its own; applies only while more than one facility is on record. Code: admin overview counts per facility
- **Source:** Wave 13; record W13-13.

### Multi-site: practice facility tool and two-site rehearsal

**476. The practice tool's messages say exactly what was written**

- **What is built:** The setup tool's messages say exactly what happened. It prints 'Nothing was written' only when nobody signed in. If it refuses after the sign-in, it says none of the plan was written but the sign-in itself is on the copy's audit trail. If the sign-in is refused, it says the attempt is on record and counts toward lockout.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (console script, no flag)
- **Source:** Wave 14; record W14-2.

**477. The practice tool records no vital signs**

- **What is built:** The setup tool records no vital signs or other observations for the patients it creates.
- **Looser option:** None named.
- **If relaxed:** Admission vitals would go in through the manual-entry path, so the chart would show them as if a person had charted them.
- **Switch:** Always on (console script, no flag)
- **Note:** The extractor marked this as not guarding a patient because the tool only runs on copies; you may want to read it as chart honesty.
- **Source:** Wave 14; record W14-4.

**478. The practice tool refuses demo-style labels**

- **What is built:** The setup tool refuses any plan whose names contain a word like demo, mock, test, dummy, fake, sample, example, placeholder, drill, sandbox, lorem, ipsum, seed or synthetic, or a placeholder written in angle brackets, so the setup tool cannot put such a label on any screen.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source.
- **Switch:** Always on (console script; the `LABEL_WORDS` constant in `server/scripts/seed_second_facility.py`)
- **Source:** Wave 14; record W14-5.

### Post-build fixes (wave 15)

**479. A department move skips only the search indexes when it looks for every use of the department**

- **What is built:** Before a department is renamed or moved, Orb looks for every table that uses its name. It now skips only the search index tables (tables created as virtual tables, today the knowledge corpus's fts5 and vec0 indexes), which it cannot read on an ordinary connection. Every other table is still checked. If any other table cannot be read, the move is refused, so a department is never moved because a check was quietly skipped.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. Skipping more tables could leave old department names behind in records the check never read.
- **Switch:** None (a builder default; no MS ruling covers it). `services/department_master.department_references`
- **Source:** Wave 15; record W15-1.

**480. Patient portal logins are left out of the "departments not in the list" check**

- **What is built:** The facility report's check for departments that are not in the department list now leaves out patient portal logins, because admission stores the ward in a portal login's department field, so ward names were wrongly reported as missing departments. Accounts with no role at all are still counted.
- **Looser option:** None named.
- **If relaxed:** Not stated in the source. Counting portal logins again would make ward names show up as missing departments on the operations page.
- **Switch:** None (a builder default). `services/facility_service.facility_report` (`departments_not_in_master` and `department_variants`)
- **Source:** Wave 15; record W15-2.

## Also open, not in these records

The checkers found a few more decisions in the deferred lists. They are not built defaults, so they have no record above, but they still need you.

- `R009` (held), wave 3 deferred: the discharge precondition check that every door calls checks nothing today. Entries 41 and 75 run through this check.
- `W9-F`: when a drug is reclassified as NDPS or becomes barred (entry 148), lots already on a crash cart are not held, so a barred drug can stay on a cart until someone removes it. Needs a ruling on the sweep. This guards a patient.
- `W9-G`: withdrawing a registration consent at the desk for an outpatient-only patient. Opening it through the OPD route widens who can write patient data, so it needs a ruling.
- `W10-A`: a single-site hospital still cannot mark its invoice, receipt or goods receipt number series as real, so prints keep saying the numbers are placeholders. A one-line change if you want it.
- `W10-A`: with the pharmacy store off, a bill charges the prescribed drug even when the pharmacy gave a substitute. The fix is small and works whatever the switches say, so it needs your approval.
- `W11-C`: no backfill for rows already at a second facility. A cheap backfill exists if you want it.
- `W8-G`: an outside lab's critical result that arrives for an unmatched patient never raises a critical alert, even after lab staff attach it to the right patient. The fix is one call after the attach saves. This guards a patient.
- `W8-G` (`LIS-R10` question): when the outside lab corrects an open critical to a normal value, the ward is not told at once. This guards a patient.
- `W8-G`: if raising an outside lab's critical fails after the result is saved, nothing tries again, so that critical is never sent. This guards a patient.
- `MO-R5` (held since wave 5): whether only a prescriber may acknowledge a severe allergy when a medicine is added on the ward chart. Nurse order entry (`R010`, `R011`) is held with it. This guards a patient.
- Wave 9 deferred (`W9-C`): when a drug is ordered by brand name, the order-time allergy check does not match it to its ingredient on the chart, CPOE, Relay and pharmacy approve lanes; only the bedside check catches it. This guards a patient.
- Sage insights list (`sage_proactive_service.get_active_insights`): the wave 14 notes say it is not limited by department even at one site. An administrator sees every insight, and anyone else sees every insight addressed to nobody, whatever their department. With two sites it also shows the other site's patients (entry 283).

## Appendix: every record and its entry

531 records. Each appears once.

| Record | Wave | Entry | Part | Entry title |
|---|---:|---:|---|---|
| W1-1 | 1 | 219 | Decide first | Front desk, ward clerk and lab accounts are locked out of Relay |
| W3-1 | 3 | 1 | Decide first | A doctor who registers a visit opens the record to their clinic |
| W3-2 | 3 | 2 | Decide first | OPD visits attached by internal id only, before the identity check |
| W3-3 | 3 | 3 | Decide first | Limits on a clinic's view of an outpatient's record |
| W3-4 | 3 | 4 | Decide first | A reused request key from another patient's visit is refused |
| W3-5 | 3 | 40 | Decide first | No shared beds |
| W3-6 | 3 | 41 | Decide first | Death, LAMA, DAMA and absconded ends are doctor-only, with the MRN typed back |
| W3-7 | 3 | 42 | Decide first | Placing a boarder needs an explicit acknowledgement |
| W3-8 | 3 | 43 | Decide first | Cleaning and out-of-service checks before a bed is used |
| W3-9 | 3 | 306 | Other | A death switches off only the patient's portal login |
| W3-10 | 3 | 307 | Other | One discharge outcome per stay |
| W3-11 | 3 | 130 | Decide first | Quarantined, recalled and substandard batches never come back into use |
| W3-12 | 3 | 131 | Decide first | Ward returns never go back on the shelf |
| W3-13 | 3 | 132 | Decide first | A drug's identity cannot be edited once it is in use |
| W3-14 | 3 | 332 | Other | Ward stock is off by default |
| W3-15 | 3 | 333 | Other | Any failed add-on step undoes the whole stock movement |
| W3-16 | 3 | 334 | Other | Easing a drug's controlled status needs a reason and a second approver |
| W3-17 | 3 | 335 | Other | Every dispense is all-or-nothing; unclassified drugs are not treated as controlled |
| W3-18 | 3 | 160 | Decide first | No critical limits ship; a critical needs an approved limit |
| W3-19 | 3 | 161 | Decide first | No reference ranges ship; H and L only against an approved range |
| W3-20 | 3 | 369 | Other | Ketones and blood gases stay as ward bedside tests |
| W3-21 | 3 | 162 | Decide first | 12-digit lab sample numbers with a check digit |
| W3-22 | 3 | 370 | Other | What still works while the lab module is being switched off |
| W3-23 | 3 | 163 | Decide first | Lab intake takes only new orders and never mixes patients in one tube |
| W3-24 | 3 | 164 | Decide first | Department staff see only their own patients' lab exceptions |
| W3-25 | 3 | 165 | Decide first | Only lab staff can match or discard an unmatched result |
| W3-26 | 3 | 166 | Decide first | Two identifiers must match to attach an unmatched result |
| W3-27 | 3 | 371 | Other | Device and monitor readings stay out of the unmatched results queue |
| W3-28 | 3 | 167 | Decide first | A decision on an unmatched result is final |
| W3-29 | 3 | 168 | Decide first | A corrected result replaces only one clear earlier result |
| W3-30 | 3 | 169 | Decide first | Attaching a result after its correction is already on the chart |
| W3-31 | 3 | 5 | Decide first | With OPD off, a reused request key can return another clinic's visit |
| W4-1 | 4 | 6 | Decide first | The duplicate check blocks a probable match, at the desk and at admission |
| W4-2 | 4 | 8 | Decide first | Duplicate search stops at 500 records and refuses if it may have missed one |
| W4-3 | 4 | 9 | Decide first | Department-limited desks: a typed date of birth can block registration in big name groups |
| W4-4 | 4 | 10 | Decide first | Stated identity check before every visit, check-in, conversion and readmission |
| W4-5 | 4 | 11 | Decide first | The front desk never records allergies |
| W4-6 | 4 | 12 | Decide first | OPD-only access hides contact and identity details |
| W4-7 | 4 | 13 | Decide first | Who may register patients and correct their details |
| W4-8 | 4 | 14 | Decide first | UHID rules: the hospital's own number or Orb's series, never guessed |
| W4-9 | 4 | 15 | Decide first | Duplicate refusals are audited, and an ABHA is always marked unverified |
| W4-10 | 4 | 16 | Decide first | A hidden date of birth scores as a full match for a same-age candidate |
| W4-11 | 4 | 17 | Decide first | Department-limited desks are not shown candidates found only by date of birth |
| W4-12 | 4 | 284 | Other | A resent registration returns only the patient id and UHID |
| W4-13 | 4 | 285 | Other | A date of birth can only be removed if an age replaces it |
| W4-14 | 4 | 44 | Decide first | The bed board names patients only inside the user's department |
| W4-15 | 4 | 308 | Other | Every bed board read that names a patient is logged, with no throttle |
| W4-16 | 4 | 45 | Decide first | How the bed board ranks bed states |
| W4-17 | 4 | 46 | Decide first | Untracked cleaning shows as such, and beds awaiting a clean are never offered |
| W4-18 | 4 | 47 | Decide first | Only a doctor can reopen an ended stay |
| W4-19 | 4 | 49 | Decide first | On reopen the old bed comes back only if it is free and usable |
| W4-20 | 4 | 48 | Decide first | What reopening a stay clears and restores |
| W4-21 | 4 | 309 | Other | The housekeeping discharge sweep never touches a bed now in use |
| W4-22 | 4 | 310 | Other | Deaths left out of readmission counts; the portal never says how a stay ended |
| W4-23 | 4 | 336 | Other | Purchase orders need the vendor's valid drug licence |
| W4-24 | 4 | 133 | Decide first | Unclassified, unconfirmed or incomplete drugs cannot be bought, received or moved in |
| W4-25 | 4 | 337 | Other | Controlled drugs go only into the NDPS vault |
| W4-26 | 4 | 338 | Other | The purchase order approver must differ from the creator |
| W4-27 | 4 | 134 | Decide first | Stock screens show no patient details |
| W4-28 | 4 | 339 | Other | The server reorder level is used only while the pharmacy store is on |
| W4-29 | 4 | 170 | Decide first | No back-dated sample collection times |
| W4-30 | 4 | 171 | Decide first | Sample labels print only after UHID and name both match |
| W4-31 | 4 | 372 | Other | When a lab order counts as resulted, cancelled and billed |
| W4-32 | 4 | 172 | Decide first | Lab-managed orders cannot be moved or typed into by hand |
| W4-33 | 4 | 373 | Other | Who can cancel a lab order |
| W4-34 | 4 | 370 | Other | What still works while the lab module is being switched off |
| W4-35 | 4 | 325 | Other | Orb will not start with the lifecycle on and the order store off |
| W4-36 | 4 | 80 | Decide first | 'Given' is refused on a held, unsigned or ended order |
| W4-37 | 4 | 326 | Other | Medication order history is deleted with an erased record |
| W4-38 | 4 | 327 | Other | Old medicine rows are adopted only when the store is switched on |
| W4-39 | 4 | 81 | Decide first | Adopting old medicine rows never guesses |
| W4-40 | 4 | 82 | Decide first | Hold, resume, stop, cancel and reject need a reason |
| W4-41 | 4 | 328 | Other | Changing orders needs the lifecycle switch |
| W4-42 | 4 | 83 | Decide first | Who verified a medicine is copied to the ward row and never cleared |
| W4-43 | 4 | 18 | Decide first | An OPD note signs only after an allergy review |
| W4-44 | 4 | 19 | Decide first | Outpatient vital signs never reach ward vitals or risk scores |
| W4-45 | 4 | 20 | Decide first | OPD writes stop once the patient is admitted |
| W4-46 | 4 | 286 | Other | Signed OPD notes can never be changed, but can be erased |
| W4-47 | 4 | 287 | Other | Completing or converting a visit needs a signed note or a reason |
| W4-48 | 4 | 21 | Decide first | Only doctors run the OPD queue; the administrator is refused |
| W4-49 | 4 | 288 | Other | Only ticked, validated coded diagnoses reach the problem list |
| W4-50 | 4 | 22 | Decide first | A clinic can read its own visits' consultations and tests, whatever the date |
| W4-51 | 4 | 23 | Decide first | OPD queue order puts emergencies first |
| W4-52 | 4 | 289 | Other | The live queue signal carries only the department |
| W4-53 | 4 | 24 | Decide first | Only a consulting doctor of the clinic can take a patient |
| W4-54 | 4 | 25 | Decide first | A taken token number is never reused that day |
| W4-55 | 4 | 340 | Other | Purchase orders and receipts go through before their number series is set up |
| W4-56 | 4 | 26 | Decide first | No follow-up prompt when a desk overrides an incomplete duplicate check |
| W5-1 | 5 | 10 | Decide first | Stated identity check before every visit, check-in, conversion and readmission |
| W5-2 | 5 | 11 | Decide first | The front desk never records allergies |
| W5-3 | 5 | 18 | Decide first | An OPD note signs only after an allergy review |
| W5-4 | 5 | 27 | Decide first | Allergy review answers must agree with the chart |
| W5-5 | 5 | 28 | Decide first | An allergy review can be recorded only on a current visit |
| W5-6 | 5 | 29 | Decide first | Visit-addressed answers show only the review status |
| W5-7 | 5 | 290 | Other | Allergy reviews can be erased with the record |
| W5-8 | 5 | 6 | Decide first | The duplicate check blocks a probable match, at the desk and at admission |
| W5-9 | 5 | 40 | Decide first | No shared beds |
| W5-10 | 5 | 50 | Decide first | Typed ward and bed text that clashes with another patient is refused |
| W5-11 | 5 | 43 | Decide first | Cleaning and out-of-service checks before a bed is used |
| W5-12 | 5 | 51 | Decide first | The terminal-clean authoriser must act in person |
| W5-13 | 5 | 43 | Decide first | Cleaning and out-of-service checks before a bed is used |
| W5-14 | 5 | 311 | Other | Admission, bed and end times at most 72 hours back |
| W5-15 | 5 | 42 | Decide first | Placing a boarder needs an explicit acknowledgement |
| W5-16 | 5 | 52 | Decide first | A recorded bed is never overwritten |
| W5-17 | 5 | 53 | Decide first | A resent admission returns only for the same user and patient |
| W5-18 | 5 | 41 | Decide first | Death, LAMA, DAMA and absconded ends are doctor-only, with the MRN typed back |
| W5-19 | 5 | 54 | Decide first | Ending a stay never closes an open escalation |
| W5-20 | 5 | 312 | Other | End of stay windows cannot be closed by accident |
| W5-21 | 5 | 311 | Other | Admission, bed and end times at most 72 hours back |
| W5-22 | 5 | 313 | Other | Lost reply on discharge: reload the chart before retrying |
| W5-23 | 5 | 135 | Decide first | A new drug stays a draft until a second pharmacist confirms it |
| W5-24 | 5 | 136 | Decide first | Duplicate drug names are refused |
| W5-25 | 5 | 137 | Decide first | Formulary file loads arrive as drafts |
| W5-26 | 5 | 138 | Decide first | Expiry date typed twice at goods receipt |
| W5-27 | 5 | 139 | Decide first | Expired batches refused; short shelf life needs a second person |
| W5-28 | 5 | 337 | Other | Controlled drugs go only into the NDPS vault |
| W5-29 | 5 | 140 | Decide first | A batch must keep one expiry and one MRP |
| W5-30 | 5 | 133 | Decide first | Unclassified, unconfirmed or incomplete drugs cannot be bought, received or moved in |
| W5-31 | 5 | 341 | Other | Batch cost includes GST |
| W5-32 | 5 | 342 | Other | A stale receipt draft cannot be posted |
| W5-33 | 5 | 343 | Other | Every controlled-drug transfer needs a PIN witness |
| W5-34 | 5 | 344 | Other | Controlled-drug transfers need the NDPS permission |
| W5-35 | 5 | 141 | Decide first | Transfers use only available, in-date batches |
| W5-36 | 5 | 345 | Other | Short receipts need a reason and are booked as not arrived |
| W5-37 | 5 | 346 | Other | The transfer receiver must differ from the dispatcher |
| W5-38 | 5 | 142 | Decide first | Units from a batch that changed in transit arrive held |
| W5-39 | 5 | 143 | Decide first | A recall or NSQ holds the whole batch everywhere |
| W5-40 | 5 | 161 | Decide first | No reference ranges ship; H and L only against an approved range |
| W5-41 | 5 | 173 | Decide first | Which reference range applies with no date of birth or sex |
| W5-42 | 5 | 174 | Decide first | The range used is the one approved at result entry |
| W5-43 | 5 | 175 | Decide first | How results are compared with range limits |
| W5-44 | 5 | 176 | Decide first | Too many decimals is refused, never rounded |
| W5-45 | 5 | 177 | Decide first | Results like 'less than 0.01' are never plotted or flagged |
| W5-46 | 5 | 178 | Decide first | Coded results must be on the test's list |
| W5-47 | 5 | 179 | Decide first | Edits before validation do not need a reason |
| W5-48 | 5 | 180 | Decide first | A result edit is refused if the result changed underneath |
| W5-49 | 5 | 181 | Decide first | A sample past its stability time needs 'Process it anyway' |
| W5-50 | 5 | 182 | Decide first | Sample receipt checks the barcode and that it was collected |
| W5-51 | 5 | 183 | Decide first | Rejecting a sample rejects every unreported test on it |
| W5-52 | 5 | 184 | Decide first | The ward hears of a rejection in a case room, else 'Phone the ward' |
| W5-53 | 5 | 374 | Other | Lab staff can only prompt a NABH incident, not file it |
| W5-54 | 5 | 185 | Decide first | A recollection always gets a new sample number |
| W5-55 | 5 | 186 | Decide first | Department accounts see only their own patients' lab lists |
| W5-56 | 5 | 375 | Other | A new analyser starts switched off |
| W5-57 | 5 | 376 | Other | Analyser keys are shown once and stored only as a fingerprint |
| W5-58 | 5 | 377 | Other | One answer for every failed analyser sign-in |
| W5-59 | 5 | 378 | Other | Only HL7 v2 analysers can be registered |
| W5-60 | 5 | 379 | Other | Analyser addresses must be on the local network |
| W5-61 | 5 | 187 | Decide first | Strict analyser code mapping |
| W5-62 | 5 | 380 | Other | Analyser set-up and code mapping are split between two roles |
| W5-63 | 5 | 80 | Decide first | 'Given' is refused on a held, unsigned or ended order |
| W5-64 | 5 | 84 | Decide first | A broken order link blocks 'given' until repaired |
| W5-65 | 5 | 85 | Decide first | Pharmacist decisions on linked rows are undone together |
| W5-66 | 5 | 86 | Decide first | Verifying a held order keeps the hold |
| W5-67 | 5 | 87 | Decide first | Pharmacist corrections are copied onto the order |
| W5-68 | 5 | 88 | Decide first | Signing needs dose, route and frequency; the route is never defaulted |
| W5-69 | 5 | 89 | Decide first | Unsigned drafts are never cancelled automatically |
| W5-70 | 5 | 90 | Decide first | Only unsigned drafts can be cancelled; a signed order is never cancelled from the chart |
| W5-71 | 5 | 91 | Decide first | A blank drug name is refused |
| W5-72 | 5 | 92 | Decide first | A second live order for the same ingredient is refused outright |
| W5-73 | 5 | 93 | Decide first | Only pharmacists verify medication orders |
| W5-74 | 5 | 340 | Other | Purchase orders and receipts go through before their number series is set up |
| W6-1 | 6 | 30 | Decide first | Outpatient doctors can order only lab tests and imaging |
| W6-2 | 6 | 291 | Other | Outpatient lab orders must match the hospital's own active tests |
| W6-3 | 6 | 31 | Decide first | The server, not the screen, decides what an outpatient order is |
| W6-4 | 6 | 32 | Decide first | Which visits can take an outpatient test order |
| W6-5 | 6 | 292 | Other | Outpatient test orders go through even if their audit row fails |
| W6-6 | 6 | 293 | Other | Cancelling an outpatient test needs a reason |
| W6-7 | 6 | 22 | Decide first | A clinic can read its own visits' consultations and tests, whatever the date |
| W6-8 | 6 | 35 | Decide first | What OPD prints show and never show |
| W6-9 | 6 | 18 | Decide first | An OPD note signs only after an allergy review |
| W6-10 | 6 | 294 | Other | A note save is refused if a colleague saved in between |
| W6-11 | 6 | 13 | Decide first | Who may register patients and correct their details |
| W6-12 | 6 | 6 | Decide first | The duplicate check blocks a probable match, at the desk and at admission |
| W6-13 | 6 | 7 | Decide first | Keep registration off at inpatient hospitals until readmission onto the same record |
| W6-14 | 6 | 33 | Decide first | Scribe quick-admit can send the same duplicate review |
| W6-15 | 6 | 34 | Decide first | Duplicate cards show minimal details, only to registration staff |
| W6-16 | 6 | 14 | Decide first | UHID rules: the hospital's own number or Orb's series, never guessed |
| W6-17 | 6 | 55 | Decide first | One active admission per patient, enforced by the database |
| W6-18 | 6 | 314 | Other | Discharge summaries are linked to a stay only when certain |
| W6-19 | 6 | 56 | Decide first | Who can change a treating department |
| W6-20 | 6 | 42 | Decide first | Placing a boarder needs an explicit acknowledgement |
| W6-21 | 6 | 40 | Decide first | No shared beds |
| W6-22 | 6 | 43 | Decide first | Cleaning and out-of-service checks before a bed is used |
| W6-23 | 6 | 57 | Decide first | Roster import cannot skip bed and cover checks |
| W6-24 | 6 | 58 | Decide first | Strict handover: the sending department loses the chart at once |
| W6-25 | 6 | 59 | Decide first | Moves need a request key, the expected bed and a reason |
| W6-26 | 6 | 60 | Decide first | Honest bed cleaning record |
| W6-27 | 6 | 188 | Decide first | A different signatory must release or amend a result |
| W6-28 | 6 | 189 | Decide first | A big change from the last result needs a reason before validation |
| W6-29 | 6 | 190 | Decide first | No delta check when the earlier result cannot be compared |
| W6-30 | 6 | 160 | Decide first | No critical limits ship; a critical needs an approved limit |
| W6-31 | 6 | 191 | Decide first | No automatic release of lab results |
| W6-32 | 6 | 372 | Other | When a lab order counts as resulted, cancelled and billed |
| W6-33 | 6 | 192 | Decide first | A release is never blocked by the order's state |
| W6-34 | 6 | 193 | Decide first | A release names one sample and goes through whole or not at all |
| W6-35 | 6 | 194 | Decide first | Only signatories can send a result back |
| W7-1 | 7 | 10 | Decide first | Stated identity check before every visit, check-in, conversion and readmission |
| W7-2 | 7 | 36 | Decide first | Desk bookings follow the outpatient scope rule |
| W7-3 | 7 | 295 | Other | Appointment check-in and no-show rules |
| W7-4 | 7 | 392 | Other | The OPD consultation is charged at completion |
| W7-5 | 7 | 296 | Other | The server will not start on a broken appointments table |
| W7-6 | 7 | 37 | Decide first | A reused booking key must match the stored booking |
| W7-7 | 7 | 38 | Decide first | A refused conversion preview shows only the visit reference |
| W7-8 | 7 | 10 | Decide first | Stated identity check before every visit, check-in, conversion and readmission |
| W7-9 | 7 | 19 | Decide first | Outpatient vital signs never reach ward vitals or risk scores |
| W7-10 | 7 | 94 | Decide first | Outpatient prescriptions never become inpatient medicines on their own |
| W7-11 | 7 | 287 | Other | Completing or converting a visit needs a signed note or a reason |
| W7-12 | 7 | 39 | Decide first | Only today's open visit in the user's clinic can be converted |
| W7-13 | 7 | 297 | Other | The waiting-room token board shows no names |
| W7-14 | 7 | 43 | Decide first | Cleaning and out-of-service checks before a bed is used |
| W7-15 | 7 | 40 | Decide first | No shared beds |
| W7-16 | 7 | 61 | Decide first | Bed swaps never change a treating department |
| W7-17 | 7 | 62 | Decide first | Undoing a move recorded in error |
| W7-18 | 7 | 63 | Decide first | Relay transfer cards follow the cover rule and never redirect |
| W7-19 | 7 | 315 | Other | The ward census counts only complete days |
| W7-20 | 7 | 65 | Decide first | Only doctors and the administrator record leave and absconding |
| W7-21 | 7 | 64 | Decide first | Patients on leave or absconded: clocks pause, nothing closes |
| W7-22 | 7 | 66 | Decide first | Rules for leave and absconding records |
| W7-23 | 7 | 316 | Other | Absconded over 24 hours: a marker only, nothing closes |
| W7-24 | 7 | 317 | Other | On leave over 48 hours: a marker only, nothing closes |
| W7-25 | 7 | 318 | Other | Bed holds expire after 2 hours, or 24 for elective and post-op |
| W7-26 | 7 | 319 | Other | A held bed takes only the patient it is held for |
| W7-27 | 7 | 67 | Decide first | Who may hold a bed, and limits on holds |
| W7-28 | 7 | 68 | Decide first | A hold's note is hidden outside the department |
| W7-29 | 7 | 104 | Decide first | Orb never raises a pharmacy supply request by itself |
| W7-30 | 7 | 93 | Decide first | Only pharmacists verify medication orders |
| W7-31 | 7 | 95 | Decide first | A pharmacy substitute never counts as verification |
| W7-32 | 7 | 96 | Decide first | Pharmacy steps on linked orders refuse mismatches and ended orders |
| W7-33 | 7 | 97 | Decide first | The dose check runs at pharmacy steps and only the prescriber clears it |
| W7-34 | 7 | 329 | Other | A given dose is tied to a supply only when certain |
| W7-35 | 7 | 98 | Decide first | How held medicines are labelled across Orb |
| W7-36 | 7 | 99 | Decide first | CPOE orders linked to the store stay under the store |
| W7-37 | 7 | 100 | Decide first | CPOE medicines must carry a structured dose |
| W7-38 | 7 | 101 | Decide first | The safety check runs at creation and again at signing |
| W7-39 | 7 | 102 | Decide first | Steps that put a drug on the ward are refused if out of step |
| W7-40 | 7 | 90 | Decide first | Only unsigned drafts can be cancelled; a signed order is never cancelled from the chart |
| W7-41 | 7 | 89 | Decide first | Unsigned drafts are never cancelled automatically |
| W7-42 | 7 | 103 | Decide first | Free-text CPOE medicines are marked as not on the formulary |
| W7-43 | 7 | 330 | Other | The shadow scorecard counts a CPOE medicine at signature |
| W7-44 | 7 | 105 | Decide first | Signed CPOE medicines raise no pharmacy request |
| W7-45 | 7 | 347 | Other | The NDPS register stays off unless both settings are set |
| W7-46 | 7 | 144 | Decide first | What happens when an NDPS register entry fails |
| W7-47 | 7 | 145 | Decide first | Opening the NDPS register for stock already on the shelf |
| W7-48 | 7 | 348 | Other | A witness for every NDPS loss, adjustment and vault move |
| W7-49 | 7 | 349 | Other | No ward use or outpatient sale of NDPS drugs while the register is on |
| W7-50 | 7 | 146 | Decide first | NDPS screens are for pharmacists only |
| W7-51 | 7 | 350 | Other | Rules for returns to the supplier |
| W7-52 | 7 | 351 | Other | Short controlled-drug receipts raise a variance per line |
| W7-53 | 7 | 352 | Other | Reopening an NDPS register that differs from the shelf |
| W7-54 | 7 | 353 | Other | NDPS variances never absorb a later loss |
| W7-55 | 7 | 195 | Decide first | Critical values go to the ward at technical validation, marked preliminary |
| W7-56 | 7 | 160 | Decide first | No critical limits ship; a critical needs an approved limit |
| W7-57 | 7 | 196 | Decide first | A lab callback closes a critical; once the ward card is live, so does the ward's in-app read-back |
| W7-58 | 7 | 197 | Decide first | No escalation ladder ships for critical results |
| W7-59 | 7 | 198 | Decide first | Discharge and transfer never close a critical |
| W7-60 | 7 | 199 | Decide first | Critical posting go-live is blocked on `LIS-R20` |
| W7-61 | 7 | 200 | Decide first | Every amendment and withdrawal posts a case-room notice |
| W7-62 | 7 | 188 | Decide first | A different signatory must release or amend a result |
| W7-63 | 7 | 201 | Decide first | A withdrawn result is marked in error and never closes a critical |
| W7-64 | 7 | 189 | Decide first | A big change from the last result needs a reason before validation |
| W7-65 | 7 | 202 | Decide first | Criticals keep working while the lab module drains |
| W7-66 | 7 | 203 | Decide first | A changed critical value is never silent |
| W7-67 | 7 | 204 | Decide first | A callback is refused if the value changed since the screen loaded |
| W7-68 | 7 | 205 | Decide first | Unauthorised tests print 'Pending' with no value |
| W7-69 | 7 | 206 | Decide first | What amended and withdrawn results print |
| W7-70 | 7 | 207 | Decide first | Interim report wording |
| W7-71 | 7 | 208 | Decide first | Age on the lab report is age at collection; date of birth never printed |
| W7-72 | 7 | 209 | Decide first | Who can read lab reports and turnaround lists |
| W7-73 | 7 | 381 | Other | Every report view, print and export is logged first |
| W7-74 | 7 | 382 | Other | Only acknowledged criticals count in NABH and turnaround figures |
| W7-75 | 7 | 383 | Other | How turnaround is measured |
| W7-76 | 7 | 220 | Decide first | A second facility shuts every patient screen |
| W7-77 | 7 | 402 | Other | Fence refusals logged once a minute per address |
| W7-78 | 7 | 221 | Decide first | An unreadable facilities table reads as the last known answer |
| W7-79 | 7 | 222 | Decide first | Only the administrator manages facilities |
| W7-80 | 7 | 403 | Other | The department list is seeded from existing staff spellings |
| W8-1 | 8 | 10 | Decide first | Stated identity check before every visit, check-in, conversion and readmission |
| W8-2 | 8 | 106 | Decide first | Readmission stops every medicine left running from the last stay |
| W8-3 | 8 | 69 | Decide first | The previous stay's vitals never count as latest after readmission |
| W8-4 | 8 | 70 | Decide first | A readmission cannot be dated before the last stay ended |
| W8-5 | 8 | 71 | Decide first | Only discharged patients can be readmitted |
| W8-6 | 8 | 387 | Other | Readmission reuses the portal login with a fresh password |
| W8-7 | 8 | 393 | Other | An outpatient bill can never carry inpatient charges |
| W8-8 | 8 | 298 | Other | Repeat OPD queue reads are logged once per 10 minutes |
| W8-9 | 8 | 394 | Other | The cashier is refused the day's outpatient list |
| W8-10 | 8 | 299 | Other | Only registration staff join the desk's live channel |
| W8-11 | 8 | 300 | Other | The front office day summary is counts only, with no log entry |
| W8-12 | 8 | 107 | Decide first | No outpatient prescription line before an allergy review |
| W8-13 | 8 | 108 | Decide first | Outpatient prescriptions are screened against other clinics' medicines |
| W8-14 | 8 | 109 | Decide first | Which visits can take an outpatient prescription |
| W8-15 | 8 | 110 | Decide first | What an outpatient prescription line must carry |
| W8-16 | 8 | 111 | Decide first | A printed prescription line can still be cancelled |
| W8-17 | 8 | 94 | Decide first | Outpatient prescriptions never become inpatient medicines on their own |
| W8-18 | 8 | 112 | Decide first | Outpatient prescribing works with the order store in record mode |
| W8-19 | 8 | 331 | Other | Ward Record computers show no outpatient prescriptions |
| W8-20 | 8 | 113 | Decide first | What the prescription list and print show |
| W8-21 | 8 | 320 | Other | Bed indicators refuse periods with incomplete ward records |
| W8-22 | 8 | 321 | Other | How ICU returns within 48 hours are counted |
| W8-23 | 8 | 322 | Other | Bed occupancy uses the midnight census and minimum sizes |
| W8-24 | 8 | 323 | Other | Where each ward's bed count comes from |
| W8-25 | 8 | 72 | Decide first | The ward clerk is held at its current limits |
| W8-26 | 8 | 384 | Other | Lab reports on the Vigil chart load only on demand |
| W8-27 | 8 | 354 | Other | Dispense quantity is capped at 1 unit by default |
| W8-28 | 8 | 147 | Decide first | A dispensed batch must be the right drug and usable |
| W8-29 | 8 | 114 | Decide first | A confirmed Sage medicine suggestion is all-or-nothing |
| W8-30 | 8 | 115 | Decide first | Sage's name match links only the supply request, not the order |
| W8-31 | 8 | 116 | Decide first | Sage supply requests need review before verification |
| W8-32 | 8 | 117 | Decide first | Scribe medicine extraction is all-or-nothing |
| W8-33 | 8 | 118 | Decide first | The pharmacy order timeline never shows bedside events |
| W8-34 | 8 | 332 | Other | Ward stock is off by default |
| W8-35 | 8 | 148 | Decide first | High-alert drugs and concentrated electrolytes are never ward stock; none of these, nor NDPS drugs, are cart stock |
| W8-36 | 8 | 355 | Other | A ward can ask only for drugs on its own list |
| W8-37 | 8 | 150 | Decide first | Ward stock changes no bedside rule |
| W8-38 | 8 | 149 | Decide first | NDPS drugs are never ward stock |
| W8-39 | 8 | 356 | Other | Separation of duties on ward indents and receipts |
| W8-40 | 8 | 357 | Other | Ward accounts see only their own department's stores |
| W8-41 | 8 | 358 | Other | Rules for issuing and cancelling ward indents |
| W8-42 | 8 | 359 | Other | Nurses hold the ward stock permissions |
| W8-43 | 8 | 360 | Other | A drug that becomes NDPS in transit lands quarantined, with a witness |
| W8-44 | 8 | 151 | Decide first | Mixed-status batches on a ward take the stronger hold |
| W8-45 | 8 | 361 | Other | The development seed sets illustrative GST on two drugs |
| W8-46 | 8 | 210 | Decide first | Outside lab criticals raise only on the sending lab's own flag |
| W8-47 | 8 | 211 | Decide first | The ward can close a critical in the app by read-back |
| W8-48 | 8 | 212 | Decide first | Open interfaced criticals hold the lab from switching off |
| W8-49 | 8 | 213 | Decide first | No repeat critical alert within 24 hours for the same value |
| W8-50 | 8 | 385 | Other | Interfaced alerts post from a locked-down system account |
| W8-51 | 8 | 214 | Decide first | What the ward's critical result card shows |
| W8-52 | 8 | 388 | Other | No lab result reaches the portal on a timer by default |
| W8-53 | 8 | 215 | Decide first | Critical results reach the portal only by a doctor's release |
| W8-54 | 8 | 216 | Decide first | Only doctors in the department can release a report to the portal |
| W8-55 | 8 | 389 | Other | Portal lab reports stop while the lab module winds down |
| W8-56 | 8 | 217 | Decide first | The portal never shows a replaced value as current |
| W8-57 | 8 | 218 | Decide first | Patients see only their own released results |
| W8-58 | 8 | 390 | Other | Every portal lab read is logged |
| W8-59 | 8 | 119 | Decide first | No dose before pharmacist verification, stat included |
| W8-60 | 8 | 120 | Decide first | No standard dose times ship |
| W8-61 | 8 | 121 | Decide first | Set dose times at least 2 hours apart |
| W8-62 | 8 | 122 | Decide first | Which set time a given dose counts for |
| W8-63 | 8 | 123 | Decide first | Changing the frequency wording cancels set dose times |
| W8-64 | 8 | 230 | Decide first | Facility-bound accounts open a patient only by the latest encounter |
| W8-65 | 8 | 223 | Decide first | Only the admin role can hold a group-wide access grant |
| W8-66 | 8 | 404 | Other | Facility reasons come first in refusal logs |
| W8-67 | 8 | 224 | Decide first | An account with an unlisted department sees no patient |
| W8-68 | 8 | 225 | Decide first | Unreadable facility maps fail rather than guess |
| W8-69 | 8 | 231 | Decide first | A clinic cannot open an outpatient registered at another facility |
| W8-70 | 8 | 391 | Other | The portal release reuses the discharge publish permission |
| W8-71 | 8 | 386 | Other | Should outside labs' criticals count in the lab's own NABH figures? |
| W9-1 | 9 | 73 | Decide first | A ward clerk can admit, with every clinical field left unassessed |
| W9-2 | 9 | 74 | Decide first | Only a doctor completes a clerk admission assessment |
| W9-3 | 9 | 75 | Decide first | A ward clerk can record 'left the ward' on a Ward Record computer |
| W9-4 | 9 | 76 | Decide first | A ward clerk can change department only on a named doctor's order |
| W9-5 | 9 | 324 | Other | Strict locations: wards and beds not in set-up are refused |
| W9-6 | 9 | 64 | Decide first | Patients on leave or absconded: clocks pause, nothing closes |
| W9-7 | 9 | 77 | Decide first | Absconding with an open escalation posts a case-room line |
| W9-8 | 9 | 78 | Decide first | Observations taken while 'away' still count and alert |
| W9-9 | 9 | 79 | Decide first | Clocks restart from the last real care on return |
| W9-10 | 9 | 54 | Decide first | Ending a stay never closes an open escalation |
| W9-11 | 9 | 92 | Decide first | A second live order for the same ingredient is refused outright |
| W9-12 | 9 | 124 | Decide first | How 'same ingredient' is decided |
| W9-13 | 9 | 125 | Decide first | Outpatient duplicates from other visits can be cleared with a reason |
| W9-14 | 9 | 126 | Decide first | PRN medicines go through every bedside check |
| W9-15 | 9 | 127 | Decide first | Sage medicine suggestions bind to a product only on a strong match |
| W9-16 | 9 | 128 | Decide first | Discharge reconciliation reads the order store |
| W9-17 | 9 | 90 | Decide first | Only unsigned drafts can be cancelled; a signed order is never cancelled from the chart |
| W9-18 | 9 | 80 | Decide first | 'Given' is refused on a held, unsigned or ended order |
| W9-19 | 9 | 129 | Decide first | Doses awaiting a pharmacist are grouped on the round sheet |
| W9-20 | 9 | 131 | Decide first | Ward returns never go back on the shelf |
| W9-21 | 9 | 362 | Other | The receiver of a ward return can never write it off |
| W9-22 | 9 | 363 | Other | NDPS ward returns need a PIN witness |
| W9-23 | 9 | 152 | Decide first | Limits on ward returns |
| W9-24 | 9 | 153 | Decide first | Ward returns screens need nurse or pharmacy rights |
| W9-25 | 9 | 148 | Decide first | High-alert drugs and concentrated electrolytes are never ward stock; none of these, nor NDPS drugs, are cart stock |
| W9-26 | 9 | 154 | Decide first | Crash carts follow the ward stock rules |
| W9-27 | 9 | 364 | Other | Cart shortfalls need a second person to confirm |
| W9-28 | 9 | 365 | Other | Cart over-counts are never added to stock |
| W9-29 | 9 | 155 | Decide first | A full cart check counts every batch |
| W9-30 | 9 | 366 | Other | Who records a cart check |
| W9-31 | 9 | 156 | Decide first | When cart seal and full checks fall due |
| W9-32 | 9 | 367 | Other | Top-ups while a cart check waits |
| W9-33 | 9 | 301 | Other | The receptionist cannot record consent |
| W9-34 | 9 | 302 | Other | Consent for patients under 18 |
| W9-35 | 9 | 303 | Other | Guardian consent needs a named person |
| W9-36 | 9 | 304 | Other | ICD-10 codes need a licence reference to load |
| W9-37 | 9 | 405 | Other | One timezone per appliance |
| W9-38 | 9 | 406 | Other | One legal entity per appliance |
| W9-39 | 9 | 395 | Other | Invoice and receipt supplier details never change once numbered |
| W9-40 | 9 | 407 | Other | Rules for deactivating a facility |
| W9-41 | 9 | 408 | Other | Credit note supplier snapshot, and fixed facility codes |
| W9-42 | 9 | 223 | Decide first | Only the admin role can hold a group-wide access grant |
| W9-43 | 9 | 409 | Other | Group-wide grant holders are protected from other admins |
| W9-44 | 9 | 410 | Other | Losing the admin role revokes a group-wide grant |
| W9-45 | 9 | 411 | Other | Facility-bound admins manage only their own facility's staff |
| W9-46 | 9 | 412 | Other | With multi-site off, only the main facility is allowed |
| W9-47 | 9 | 413 | Other | An account's home facility follows its department |
| W9-48 | 9 | 305 | Other | The bedside consent lane does not apply the under-18 rules |
| W10-1 | 10 | 396 | Other | Bed days that cannot be priced are not charged |
| W10-2 | 10 | 397 | Other | Leave and absconded days in bed-day billing |
| W10-3 | 10 | 398 | Other | Bed days cannot be posted past the stay |
| W10-4 | 10 | 399 | Other | Bed days cannot land on another patient's admission |
| W10-5 | 10 | 400 | Other | Pharmacy MRP is treated as tax-inclusive |
| W10-6 | 10 | 401 | Other | Ward return credits never exceed what was billed |
| W10-7 | 10 | 437 | Other | A second site's receipts wait for its own number series |
| W10-8 | 10 | 463 | Other | Numbering for stores, kitchen and NABH documents at a site with no series |
| W10-9 | 10 | 232 | Decide first | One UHID series for the whole group |
| W10-10 | 10 | 414 | Other | Number prefixes are checked across every site |
| W10-11 | 10 | 259 | Decide first | Device and feed data matching another site is not filed |
| W10-12 | 10 | 415 | Other | Department names are unique across the group and fixed once used |
| W10-13 | 10 | 239 | Decide first | No moves or admissions across facilities until `MS-R3` is ruled |
| W10-14 | 10 | 416 | Other | Department spelling is saved from the master list |
| W10-15 | 10 | 157 | Decide first | Ward stock is used only when the order says ward stock |
| W10-16 | 10 | 158 | Decide first | Ward stock can go negative, flagged, never blocking a dose |
| W10-17 | 10 | 368 | Other | Which ward cupboard a dose is booked against |
| W10-18 | 10 | 159 | Decide first | Ward stock booking never blocks or delays the dose |
| W11-1 | 11 | 242 | Decide first | Roster import refuses cross-site readmissions and moves |
| W11-2 | 11 | 240 | Decide first | Roster import refuses lines outside the account's sites |
| W11-3 | 11 | 241 | Decide first | History import runs at one site and refuses split stays |
| W11-4 | 11 | 233 | Decide first | Which site a patient's chart belongs to |
| W11-5 | 11 | 418 | Other | The department decides a visit's site |
| W11-6 | 11 | 417 | Other | Unknown site labels go to the main site at upgrade |
| W11-7 | 11 | 226 | Decide first | Unclassified screens are shut while two sites exist |
| W11-8 | 11 | 227 | Decide first | Only the seasonal calendar is cleared for two sites |
| W11-9 | 11 | 228 | Decide first | When a query counts as safely keyed to one patient |
| W11-10 | 11 | 229 | Decide first | Cross-site leak test: 19 shut lanes would leak if opened |
| W11-11 | 11 | 438 | Other | A receipt belongs to the counter's site |
| W11-12 | 11 | 439 | Other | Credit notes, invoices and claims follow the bill's site |
| W11-13 | 11 | 427 | Other | Stores, indents and orders stay within one site |
| W11-14 | 11 | 463 | Other | Numbering for stores, kitchen and NABH documents at a site with no series |
| W11-15 | 11 | 464 | Other | Wave 11 interim: no NABH pack while two sites exist (replaced by entry 467) |
| W11-16 | 11 | 465 | Other | Where an incident report is filed |
| W11-17 | 11 | 466 | Other | Housekeeping units and machines keep their site |
| W12-1 | 12 | 244 | Decide first | Unfiled Lens reads and Scribe drafts open only to their author |
| W12-2 | 12 | 245 | Decide first | Relay rooms with no facility are hidden |
| W12-3 | 12 | 246 | Decide first | Surgical cases are visible only within the user's facilities |
| W12-4 | 12 | 419 | Other | A booking can be handled only at the facility that made it |
| W12-5 | 12 | 247 | Decide first | Paging skips staff whose facility does not hold the patient |
| W12-6 | 12 | 248 | Decide first | The ward brief is withheld without a facility scope |
| W12-7 | 12 | 249 | Decide first | The latest visit decides the discharged list and final-bill picker |
| W12-8 | 12 | 425 | Other | Shadow-mode episodes limited to the user's facilities |
| W12-9 | 12 | 250 | Decide first | Sage answers as if another facility's patient does not exist |
| W12-10 | 12 | 251 | Decide first | Patient ids outside the facility get an immediate Sage refusal |
| W12-11 | 12 | 252 | Decide first | Live vitals never go to the all-departments channel |
| W12-12 | 12 | 426 | Other | Records of Sage answers are limited by facility |
| W12-13 | 12 | 253 | Decide first | Sage counts cover only the user's facilities |
| W12-14 | 12 | 451 | Other | Old audit history without a facility is group-wide only |
| W12-15 | 12 | 452 | Other | Audit rows naming no facility are group-wide only |
| W12-16 | 12 | 453 | Other | The flight recorder follows the patient's current facility |
| W12-17 | 12 | 454 | Other | An audit row about a patient belongs to the patient's facility |
| W12-18 | 12 | 455 | Other | Failed logins are filed under the named account |
| W12-19 | 12 | 456 | Other | Unreadable audit rows are hidden |
| W12-20 | 12 | 457 | Other | The audit facility filter only narrows |
| W12-21 | 12 | 458 | Other | The audit chain check stays appliance-wide |
| W12-22 | 12 | 459 | Other | Facility attribution can lag 30 seconds |
| W12-23 | 12 | 265 | Decide first | Five internal audit lookups are not filtered by facility |
| W12-24 | 12 | 266 | Decide first | A restore rolls back every facility |
| W12-25 | 12 | 267 | Decide first | Each satellite needs its own downtime mirror folder |
| W12-26 | 12 | 268 | Decide first | Overlapping mirror folders are refused for both facilities |
| W12-27 | 12 | 269 | Decide first | Downtime pack folders and ward identity per facility |
| W12-28 | 12 | 270 | Decide first | Facility extract: only group-level items for shared patients |
| W12-29 | 12 | 460 | Other | The facility extract needs a reason and audits first |
| W12-30 | 12 | 271 | Decide first | FHIR bundles name facilities on multi-site |
| W12-31 | 12 | 461 | Other | Downtime 'capture all' covers only the user's facilities |
| W12-32 | 12 | 440 | Other | A counter cannot collect on a bill once the patient moved facility |
| W12-33 | 12 | 441 | Other | Money actions check the admission's own facility |
| W12-34 | 12 | 442 | Other | A bill gathers only the user's facility's charges |
| W12-35 | 12 | 255 | Decide first | The pharmacy verify queue is limited to the pharmacist's facilities |
| W12-36 | 12 | 234 | Decide first | The front desk cannot reach another facility's patient without a match ticket |
| W12-37 | 12 | 235 | Decide first | Rules for the group-wide identity search |
| W12-38 | 12 | 443 | Other | Patient money summaries still add up every facility |
| W12-39 | 12 | 444 | Other | Where policies and discount requests belong |
| W12-40 | 12 | 445 | Other | Outpatient charges are stamped with the visit's facility |
| W12-41 | 12 | 239 | Decide first | No moves or admissions across facilities until `MS-R3` is ruled |
| W12-42 | 12 | 243 | Decide first | A ward belongs to its department's facility |
| W12-43 | 12 | 236 | Decide first | Facility-bound accounts see only their own facility's registration, OPD and beds |
| W12-44 | 12 | 237 | Decide first | Duplicates at another facility count but are not shown |
| W12-45 | 12 | 238 | Decide first | The identity check can be run from another facility |
| W12-46 | 12 | 420 | Other | A new patient is registered at the registrar's facility |
| W12-47 | 12 | 421 | Other | OPD queue actions are refused outside the user's facilities |
| W12-48 | 12 | 423 | Other | Bed classes stay group-wide and any admin can edit them |
| W13-1 | 13 | 467 | Other | Each NABH evidence pack covers one facility |
| W13-2 | 13 | 468 | Other | NABH packs use the facility's own number series |
| W13-3 | 13 | 469 | Other | NABH lab indicators are not computed per facility yet |
| W13-4 | 13 | 470 | Other | Which facility a NABH event counts at |
| W13-5 | 13 | 424 | Other | Beds at no facility are shown on their own line |
| W13-6 | 13 | 272 | Decide first | An unlisted department sees an empty Command Center |
| W13-7 | 13 | 273 | Decide first | Asking for another facility's figures returns 'not found' |
| W13-8 | 13 | 422 | Other | Duplicate ABHA refusals name no one outside scope |
| W13-9 | 13 | 471 | Other | Procurement, housekeeping and dietary records stay at one facility |
| W13-10 | 13 | 472 | Other | Maintenance contracts are limited to the user's machines |
| W13-11 | 13 | 473 | Other | Employee placement and group-only shift settings |
| W13-12 | 13 | 474 | Other | Retention reviews and breach records by facility |
| W13-13 | 13 | 475 | Other | The admin overview counts only the admin's facility |
| W13-14 | 13 | 446 | Other | Interstate GST is worked out from the place of supply |
| W13-15 | 13 | 447 | Other | Who may edit a price list |
| W13-16 | 13 | 448 | Other | Payer price lists stay group-wide |
| W13-17 | 13 | 449 | Other | Every facility keeps a default price list |
| W13-18 | 13 | 450 | Other | Rate lookups with no facility read only group-wide lists |
| W13-19 | 13 | 428 | Other | A dispense uses only the patient's own facility's stock |
| W13-20 | 13 | 429 | Other | A batch is at a facility only if its records agree |
| W13-21 | 13 | 430 | Other | Stock transfers never cross facilities |
| W13-22 | 13 | 431 | Other | Another facility's store documents look not found |
| W13-23 | 13 | 432 | Other | Reorder levels are judged per facility shelf |
| W13-24 | 13 | 433 | Other | Ward returns go back to the dispensing facility |
| W13-25 | 13 | 256 | Decide first | Any facility's pharmacist can edit the shared drug master |
| W13-26 | 13 | 434 | Other | NDPS witnesses can be from any facility |
| W13-27 | 13 | 257 | Decide first | The NDPS register names only patients in scope |
| W13-28 | 13 | 260 | Decide first | Lab staff see their own facility's lab work |
| W13-29 | 13 | 261 | Decide first | Released lab reports open if the lab or the patient is in scope |
| W13-30 | 13 | 262 | Decide first | A new sample is performed at the patient's facility |
| W13-31 | 13 | 263 | Decide first | Another facility's lab and medication records look not found |
| W13-32 | 13 | 258 | Decide first | Only group-level accounts run the order parity report and adoption |
| W13-33 | 13 | 264 | Decide first | Unmatched results attach only to patients in scope |
| W13-34 | 13 | 435 | Other | The printed lab report names the performing facility |
| W13-35 | 13 | 436 | Other | Bench counts are limited to the user's facilities |
| W13-36 | 13 | 274 | Decide first | Only doctors can hold a visiting session |
| W13-37 | 13 | 275 | Decide first | One facility at a time per account |
| W13-38 | 13 | 276 | Decide first | Shared ward tablets never switch facility |
| W13-39 | 13 | 277 | Decide first | Visiting sessions are re-checked on every request |
| W13-40 | 13 | 254 | Decide first | A visiting attending is paged at the patient's facility |
| W13-41 | 13 | 462 | Other | Audit filing for visiting sessions |
| W13-42 | 13 | 278 | Decide first | Visiting sessions sign out, never reopen in place |
| W13-43 | 13 | 279 | Decide first | A password re-entered at another facility reloads the screen |
| W13-44 | 13 | 280 | Decide first | A facility switch reloads every other tab |
| W14-1 | 14 | 281 | Decide first | The practice facility tool runs only on database copies |
| W14-2 | 14 | 476 | Other | The practice tool's messages say exactly what was written |
| W14-3 | 14 | 282 | Decide first | The practice tool never skips the drug-safety check |
| W14-4 | 14 | 477 | Other | The practice tool records no vital signs |
| W14-5 | 14 | 478 | Other | The practice tool refuses demo-style labels |
| W14-6 | 14 | 283 | Decide first | The two-facility rehearsal fails on any cross-facility leak |
| W15-1 | 15 | 479 | Other | A department move skips only the search indexes when it looks for every use of the department |
| W15-2 | 15 | 480 | Other | Patient portal logins are left out of the "departments not in the list" check |
