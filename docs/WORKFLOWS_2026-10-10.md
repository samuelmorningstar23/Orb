# Orb workflows: what we have, what a hospital needs, what is missing

Date: 2026-10-10. Written up from a code read, not from the manuals.

## 1. What this is

The first complete list of Orb's workflows set against what an owner-run Indian hospital of 80 to 250 beds does every day. A workflow is a sequence a named person performs to get an outcome, from a trigger to a done state. Screens and routes are only the means.

How it was made. Seven readers each took one slice of the code (ward clinical lane; medicines and pharmacy; documentation, models and communication; diagnostics, theatre and house-wide screens; front office, money and the patient; back office, facilities and people; quality, statutory records and administration) and listed every workflow from server/main.py, server/config.py, the route and service files, the panels and src/shellConfig.tsx. Nothing was run or edited. A second pass wrote the needs from five hospital lenses (nursing superintendent, medical superintendent, owner and finance, pharmacist and stores, quality and medical records) with the Indian rule behind each, and matched every need to the inventory. A code verifier re-read the code behind every disputed cell and raised 19 corrections, all applied below and listed in section 7. A critic added 48 needs the first pass missed; those carry a status guess marked unverified. A hospital reader then ranked every gap from the buyer's chair, once for a full Orb hospital and once for a founding ward on Orb Ward Record.

Headline counts after corrections and additions:

- Inventory: about 245 workflow entries across the seven slices, roughly 220 distinct once overlaps are folded. About two in five are complete and on by default, a quarter are dark (built, off by default), the rest partial, server only or stubs.
- Needs: 192 rows. First pass, 144 rows: 7 built, 68 partial, 16 dark, 4 planned, 49 missing (after the verifier moved the WHO checklist from built to partial, DNAR and high-alert drugs from missing to partial, and consultant appointments from partial to dark). Critic's additions, 48 rows: 36 guessed as nothing, 11 as partly, 1 as built but dark.
- Buyer's ranking of the 144 verified gaps: 21 block the sale of a full Orb hospital, 49 are daily pain, about 50 are assessment gaps (paper an assessor or inspector reads), about 20 are nice; the last two tiers are counted by hand and the four tiers add to a few short of 144. For the founding ward almost everything is "not touched" or "nice"; three things bite it: no pain score on the observation sheet, the downtime pack living on the appliance's own disk, no offsite backup.

Status words: complete (works end to end on a default install), partial (a named step missing), dark (built, off by default), server only (routes, no screen), stub (empty router), missing (nothing in code or plan), planned (in docs/HIS_BUILD_PLAN_2026-10.md, not built). WR means available on a founding ward computer under ORB_WARD_RECORD. Switch names drop the ORB_ prefix.

## 2. What Orb has today

### 2.1 Ward clinical lane

| Workflow | Who | Status | Switch | WR | Gap inside it |
|---|---|---|---|---|---|
| Admit through the form | doctor, nurse, admin | complete | none; ADT, REGISTRATION add pickers | yes | free-text bed; DOB optional; chips carry no reaction |
| Quick admit from Scribe | doctor, nurse | complete | MODELS | no | no DOB or first obs; note not linked |
| Admit an outpatient onto own record | OPD doctor | dark (verifier: button exists) | OPD | no | needs OPD and REGISTRATION on |
| Roster import from old HIS | admin | complete | none | hidden | absent patients never discharged |
| Bed board: assign, move, link, swap, correct | clerk, bed manager, doctor | dark (verifier: swap, undo exist) | ADT | tab kept | nurses cannot move anyone |
| Bed register setup and reconcile | bed manager | dark | ADT | tab kept | codes immutable; two ward sources |
| Transfer from the front desk | doctor, admin | complete | FRONTDESK | no | no acceptance; off the WR rail |
| Transfer with the beds module | doctor, clerk | dark | ADT | tab kept | no acceptance or nursing handover |
| Leave, absconded, returned | doctor, admin | dark (verifier: PresenceDrawer exists) | ADT | tab kept | nurses refused (ADT-S7); clocks ignore presence |
| Hold a bed; midnight census | bed manager, admin | dark (verifier: screens exist) | ADT | tab kept | no bed request from ER or OT |
| End of stay (LAMA, DAMA, death), reopen | doctor | dark | ADT | tab kept | off: a death is a discharge; chains never closed; no MCCD |
| Bedside observation set with NEWS2 | nurse, doctor | complete | SCORING | yes, no score | no pain, GCS, urine, glucose; one patient per sheet |
| Clerk copies a round from the sheet | ward clerk | complete | none | yes | one set per save; no read-back or scan |
| Strike out a set | nurse, doctor, clerk | complete | none | yes | risk_scores row left; clerk sees 24 h |
| Ward board and chart | doctor, nurse, admin | complete | SCORING | yes, readings | one department only |
| Nurse shift board | nurse | complete | none | yes, obs only | no assignment or acknowledgement; derived dose times |
| Relay deterioration alert | nurse, doctor | complete | SCORING | no | in-app only; no pager; no cooldown |
| Escalation chain | nurse, doctor, sweep | partial | SCORING | no | no screen; acknowledge route (vigil_routes.py:1307) unwired; no roster |
| Sepsis Six | nurse, doctor | complete | SEPSIS_AUTOSTART | no | NEWS2 7 is not a sepsis screen; ticks order nothing |
| Ward round from the tablet | doctor | complete | MODELS (dictation) | no | prose only; off the WR rail |
| Today (doctor home) | doctor | complete | none | no | drafts and alerts only |
| Story, mark reviewed | doctor, nurse | complete | none | yes | watermark per clinician |
| Clinical note | doctor, admin | complete | none | yes | nurses cannot write one; no amend |
| Allergy and problem list after admission | doctor by API | server only | none | yes | no add or deactivate control |
| Discharge: draft, review, print, link | doctor | complete | DISCHARGE_LINK, MODELS | yes | draft not editable; no TTO; link needs DOB |
| Open the summary from the link | patient | complete | DISCHARGE_LINK | yes | hospital network only; English |
| Bridge ends at end of discharge day | patient | complete | rule R029 | yes | ask needs models |
| Print the inpatient record | doctor, nurse | complete | none | yes | no obs grid, nursing notes, signature |
| Downtime snapshot and ward pack | sweep, nurse, admin | partial | none | yes, no score | pack on the appliance; clerk cannot read it |
| I-PASS handover (one patient, the list) | nurse, doctor | complete | 404 under WR | no | never signed or received |
| Ward observation interval | admin | complete | none | yes | no effective-from date |
| Monthly rounds file | admin | complete | none | yes | no on-screen view, sign-off or past-file list |
| Monitor feed (HL7 MLLP, ingest door) | integrator | dark | MONITOR_ENABLED | no | never run on hardware |
| Vitals simulator | demo operator | dark | INGESTION_MODE | no | demo only |

### 2.2 Medicines and pharmacy

Three medicine records never meet on a default build: medications (the chart), orders (CPOE) and helix orders (supply). The join is the order store, dark and server only.

| Workflow | Who | Status | Switch | WR | Gap inside it |
|---|---|---|---|---|---|
| Allergy record for screening | doctor, nurse, OPD doctor | partial | none | yes | no entry after admission; free text |
| Chart a medicine | doctor, nurse | complete | drug chart (off under WR) | no | nurse charts unsigned (R010); no stop |
| Stop, hold, resume | doctor | stub | MED_ORDER_LIFECYCLE | no | router empty; stopped drug stays due |
| CPOE medication order | doctor, nurse | partial | drug chart | no | never reaches the round |
| Order set | doctor, nurse | partial | drug chart | no | a JSON file, not a committee |
| Sign a draft (order store) | doctor | server only | MED_ORDER_STORE, LIFECYCLE | no | no screen calls /api/medorders |
| Ask pharmacy to supply | doctor, pharmacist | server only | same | no | no button; one unit per row |
| Adopt chart rows, parity | admin | server only | MED_ORDER_STORE | no | no Admin section |
| Signed note charts unverified rows | doctor, pharmacist | partial | MODELS | no | stops become a note nobody acts on |
| Sage drafts a medicine | doctor, pharmacist | partial | MODELS | no | doctor lacks helix.write: 403 |
| Relay ambient card to chart and pharmacy | doctor | partial | MODELS, SAGE_AMBIENT | no | placeholder dose can be verified (F-A) |
| Pharmacist verify queue | pharmacist | complete | drug chart | no | model rows only; clears severe allergy |
| Helix review, substitute, dispense | pharmacist | complete | drug chart | no | one unit per dispense |
| Nurse signs for a delivery | nurse | complete | drug chart | no | per unit; no overdue state |
| Give a dose or record not given | nurse, doctor | complete | drug chart | no | derived due times; no witness, barcode, infusion rate |
| Prescriber override | doctor | complete | drug chart | no | no remote countersign |
| Print medication list | doctor, nurse | complete | drug chart | no | a list, not a MAR grid |
| Reconcile at discharge | doctor | partial | drug chart | no | continued means everything ever active (R009) |
| Home medicines at admission | doctor, nurse | partial | drug chart | no | chips, never reconciled |
| Patient reads medicines in Bridge | patient | complete | drug chart | no | the chart, not a prescription |
| Add a drug to the formulary | two pharmacists | dark | PHARMACY_STORE | no | off: seed data, no screen |
| Masters: classes, stores, par, vendors, licences | pharmacist, admin | dark | PHARMACY_STORE | no | approver has no Pharmacy tab |
| Pharmacy purchase order | pharmacist, admin | dark | PHARMACY_STORE | no | no PO print |
| Goods receipt | pharmacist | dark | PHARMACY_STORE | no | no receipt without a PO |
| Stock, reorder, near expiry, ledger | pharmacist | dark | PHARMACY_STORE | no | read only |
| Transfers between stores | pharmacists | dark | PHARMACY_STORE | no | pharmacy internal only |
| Vendor return and credit | pharmacist, admin | dark (verifier: view exists) | PHARMACY_STORE | no | credit never reaches accounts |
| NDPS register | pharmacist, witness | dark (verifier: NdpsView exists) | PHARMACY_STORE, NDPS_REGISTER, NDPS_ON_FAILURE | no | no ward shift count; no Form 3E |
| Quarantine, recall, write off | pharmacist | stub | PHARMACY_STORE | no | lots router empty (grn-10) |
| Stock take | pharmacist, admin | stub | PHARMACY_STORE | no | counts router empty (grn-12) |
| Ward imprest, indent, crash cart | nurse, pharmacist | stub | PHARMACY_WARD_STOCK | no | ward router empty (grn-21, 23) |
| Ward returns | nurse, pharmacist | stub | PHARMACY_STORE | no | returns router empty (grn-15) |
| Outpatient prescription | OPD doctor | stub | OPD, MED_ORDER_STORE | no | nothing exists (opd-12) |

### 2.3 Documentation, the models and team communication

| Workflow | Who | Status | Switch | WR | Gap inside it |
|---|---|---|---|---|---|
| Draft a note from dictation or typing | doctor, nurse, surgeon | complete | MODELS | no | ASR still Whisper; chunks lose split drug names |
| Second pass against the transcript | doctor, nurse | dark | SCRIBE_VERIFY_ENABLED (false) | no | client asks, server ignores silently |
| Edit and sign a draft | doctor | complete | none | no | no ward-wide signing queue; no PIN |
| Reject a draft | doctor | complete | none | no | author not told |
| Revise a signed note | doctor, nurse | complete | none | no | re-extraction charts again |
| Auto-chart from a signed note | bot, pharmacist | partial | MODELS | no | vitals stamped at signing time |
| Handover list with nurse commentary | nurse | complete | 404 under WR | no | no acknowledgement; refused offline |
| Sage assessment before the round | doctor | partial | MODELS | no | serialised; verdict lives in Relay |
| Sage guideline question | all clinical | complete | MODELS | no | no non-model fallback; English |
| Sage about a patient | doctor, nurse | partial | MODELS | no | bed matcher open (R026); answers without a chart |
| Fast or careful thinking, floor | doctor, nurse | complete | FAST_MODE, SAFETY_FLOOR | no | voice lane has no toggle |
| Sage history | all | complete | MODELS | no | clear hides, provenance keeps |
| Floating orb | doctor, nurse, admin | complete | MODELS | no | text only |
| Insights feed act or dismiss | doctor, nurse | complete | MODELS, SCORING | no | bedside nurse rarely sees it |
| Ward brief | doctor, nurse, admin | complete | MODELS and SCORING | no | snapshot needs no model yet hidden |
| Sage by voice | doctor, nurse | dark | VOICE_AGENT_ENABLED (false) | no | 4B, no sources |
| Case room and department messaging | doctor, nurse, admin | complete | none; hidden under WR | no | department rooms seeded only; no pharmacists |
| Direct conversation | doctor, nurse | server only | none | no | panel filters it out |
| Alert card: confirm or escalate | doctor, nurse | complete | SCORING | no | escalate pages nobody |
| Wrong-room flag | sender, readers | complete | none | no | case rooms only; synthetic tuning |
| Ambient action card confirm | doctor | partial | MODELS, SAGE_AMBIENT | no | PIN lane has no screen |
| Approve or decline a drafted assessment | doctor | complete | SCORING and MODELS | no | guard deletes 3 in 10 correct flags |
| Surgical team confirm or decline | surgeon, team | partial | none | no | decline re-routes in demo only |
| Founding ward with the models off | operator | complete | WARD_RECORD, MODELS | yes | model-free features go dark too |

### 2.4 Diagnostics, theatre and house-wide screens

Every laboratory lane is dark: ORB_LAB defaults off and the lab roles exist only while it is on. Orb ships three bedside tests and no reference ranges.

| Workflow | Who | Status | Switch | WR | Gap inside it |
|---|---|---|---|---|---|
| Laboratory catalogue | pathologist | dark | LAB | no | everything typed in by the site |
| Reference range, critical limit | pathologist | dark | LAB | no | nothing ships; one-person approval |
| Order a test, lab takes it in | ward, hook | dark | LAB | no | no charge at order; no lab requisition (orders list prints) |
| Collect at the bedside with labels | nurse, phlebotomist | dark | LAB | no | printer per ward; no back-dating (LIS-R6) |
| Receive, reject, recollect | lab technician | dark | LAB | no | ward told only via a case room |
| Enter results | lab technician | dark | LAB | no | keyboard only |
| Technical validation, delta flags | lab technician | dark | LAB | no | no autoverification |
| Authorise into the chart | pathologist | dark | LAB | no | one-pathologist lab blocked; nobody told |
| Amend or withdraw a result | pathologist | dark (verifier: view exists) | LAB | no | treating doctor not told |
| Cancel a lab-managed order | ward, lab | dark | LAB | no | phlebotomist not told |
| Raise a critical, tell the ward | lab, ward | dark | LAB; held on LIS-R20 | no | no ward acknowledgement (lis-16) or board chip (lis-17) |
| Close a critical with a callback | lab | dark (verifier: CriticalView closes it) | LAB | no | imaging has no critical path |
| Find, check, print a report | lab, ward | dark | LAB | no | no letterhead; chart modal lists none |
| Turnaround times | pathologist, admin | dark | LAB | no | no target ships |
| Intake exceptions | lab | server only | LAB | no | unmapped code never retried |
| Register an analyser, map codes | admin, pathologist | dark | LAB, LAB_ANALYSER_LISTENER | no | listener flag has no code |
| Receive analyser results | analyser | stub | LAB | no | no HTTP, MLLP or ASTM intake |
| Outside HL7 result to the chart | integrator, admin | complete | INGESTION_MODE, API key | yes, no tab | never links to the order; no critical |
| Reconcile an unmatched result | lab staff | dark | RESULT_QUARANTINE, LAB | no | quarantine off stores patient-less |
| Finish open work after switch-off | lab | dark | LAB off with open work | no | drains forever on one open critical |
| Lens image read, file to chart | doctor, nurse | dark | LENS_ENABLED (off in prod), MODELS | no | not radiology; no DICOM |
| Imaging order with typed result | doctor, nurse | partial | none | no | no worklist, slot, images |
| Theatre list and case | surgeon, anaesthetist | partial | none | no | no route creates or cancels an elective case |
| Emergency case from a Sage transfer | Relay clinician, surgeon | partial | MODELS | no | always OR-E, 120 minutes |
| Alert team, confirm or decline | surgeon, team | partial | none | no | roster hardcoded in the panel |
| Prep-for-surgery package | surgeon | partial | none | no | an alert, not a checklist |
| WHO checklist | surgeon, anaesthetist, nurse | partial (verifier: no case can be created) | SURGERY_REQUIRE_CHECKLIST | no | no phase order or lock |
| Start and complete, post-op note | surgeon | partial | SURGERY_REQUIRE_CHECKLIST | no | note is a stub; no counts, implants, register, PACU |
| Pre-operative risk draft | surgeon | partial | MODELS | no | nothing stored; no ASA on the case |
| 7-day forecast and capacity | doctor, nurse, admin | partial | none | no | untrained model, rules labelled |
| Discharge readiness | doctor, nurse, admin | partial | none | no | heuristic; no date on the chart |
| Surge simulator | admin, clinicians | partial | none | no | fictional constants; nothing saved |
| Command Center | admin, doctors | partial | none | no | UTC day; no money, OPD, stock |
| Pulse seasonal calendar | forecast.read | partial | [site] state | yes | draft awaiting clinician review |

### 2.5 Front office, money and the patient

Registration and OPD are off by default; OPD needs registration and the front desk. Billing, payments, payer and front desk are on and reach the administrator only; the cashier sees Payments; the receptionist exists only with REGISTRATION on.

| Workflow | Who | Status | Switch | WR | Gap inside it |
|---|---|---|---|---|---|
| Find a returning patient, confirm identity | desk, doctor, admin | dark | REGISTRATION | no | ward-scoped caller cannot reach another ward |
| Register a new patient with UHID | desk, doctor | dark | REGISTRATION | no | orb-mode UHID refuses until series set; no notice (opd-19) |
| Register a visit, issue a token | desk, doctor, admin | dark | REGISTRATION | no | no slot consumed; Take re-numbers the slip |
| OPD visit from the ward list | doctor, admin | partial | FRONTDESK | no | walk-ins cannot be registered by default |
| Waiting line: priority, room, LWBS | desk, admin, doctor | dark | OPD | no | desk polls; no day summary (opd-18) |
| Transfer between wards, history | admin, doctor | complete | FRONTDESK | no | nurse cannot move; free-text bed without ADT |
| Clinic timetable | admin | dark | REGISTRATION | no | information only without OPD |
| Correct registration details | desk, doctor | dark | REGISTRATION | no | opens only from a fresh visit; DOB not editable |
| Possible-duplicate worklist | desk, admin | server only | REGISTRATION | no | no screen; never merged |
| Run the clinic queue as the doctor | doctor | dark | OPD | no | only the doctor takes |
| Allergy review, consultation, sign, print | doctor | dark | OPD | no | no prescription (opd-12); no ICD-10 list |
| Order investigations on a visit | doctor | dark | OPD | no | no charge at order |
| Finish the visit, raise the charge | doctor | dark | OPD | no | unpriced on a fresh site |
| Convert outpatient to inpatient | doctor | dark (verifier: OpdPanel opens the admit form) | OPD | no | readmission onto the record not built (opd-17c) |
| Waiting-room token board | admin, patients | dark (verifier: real kiosk page) | OPD | no | boards made by API only |
| Appointment for a ward or discharged patient | doctor books | partial | none | hidden | no cancel, reschedule, no-show here |
| Consultant slot booking, day list, cancel, check-in | front desk | dark (verifier: AppointmentsDayView exists) | OPD, REGISTRATION | no | opd-16 is built; no reminder leaves the building |
| Follow-up request from Bridge | patient | partial | none | yes | nobody confirms it |
| Price list: items, plans, rates, packages | admin | partial | BILLING | no | plans, packages, class rates API only |
| Capture charges | admin | partial | BILLING | no | bed days API only; nursing, OT manual |
| Invoice: issue, cancel, credit, print | admin | complete | BILLING | no | placeholders until series and GSTIN set; no e-invoice |
| Outpatient bill in one step | admin | server only | BILLING | no | createOpdInvoice has no caller (opd-14) |
| Financial clearance advisory | doctor, nurse, admin | complete | BILLING, DISCHARGE_FINANCIAL_ENFORCE | no | enforce switch has no override step |
| Till: payment, receipt, close shift | cashier, admin | complete | PAYMENTS | no | no printed receipt; apply advance API only |
| Reverse a receipt | admin | complete | PAYMENTS | no | cashier cannot |
| Refund and discount approvals | cashier, admin | partial | PAYMENTS | no | no screen raises a request |
| Day-end takings | cashier, admin | complete | PAYMENTS | no | no print; no bank deposit |
| Set up payers | admin | complete | PAYER | no | contacts, GSTIN not editable |
| Record cover, mark verified | admin | complete | PAYER | no | never at admission |
| Claim and pre-authorisation | admin | complete | PAYER | no | ward never sees the approved amount |
| Claim to settlement, aging, denials | admin | complete | PAYER | no | settlement never touches the bill |
| Revenue Integrity | doctor, nurse, admin | partial | none | no | writes nothing; placeholder rupees |
| One-time Bridge login | admitting clinician, patient | complete | none | yes | no reissue |
| Patient reads status, team, vitals, documents | patient | complete | none | yes | no named nurse; no bills; English |
| Patient asks; red flag reaches the ward | patient, ward | complete | MODELS | no | US 988 line in the text |
| Patient downloads a FHIR bundle | patient | server only (verifier: no Bridge caller) | none | no | unreadable by a patient |
| Discharge link with DOB | clinician, patient | partial | DISCHARGE_LINK | yes | stated-age patients get none |

### 2.6 Back office, facilities and people

Default-on modules (procurement, housekeeping, workforce, biomedical, dietary) stay on unless set to 0. There is no storekeeper, purchase officer, housekeeping, cleaner, HR, ward in-charge, biomedical engineer, dietitian or catering role; every back-office desk is an admin account. ADT bed workflows are in 2.1.

| Workflow | Who | Status | Switch | WR | Gap inside it |
|---|---|---|---|---|---|
| Item master and vendors | admin | partial | PROCUREMENT | no | blacklist, store creation API only |
| Raise and approve an indent | admin | complete | PROCUREMENT | no | one account does both |
| Purchase order: draft, approve, issue | admin | partial | PROCUREMENT | no | no print; cancel API only |
| Goods receipt | admin | partial | PROCUREMENT | no | straight to stock; no hold or debit note |
| Issue stock to a department | admin | partial | PROCUREMENT | no | issue is the only movement on screen |
| Reorder, expiring, deliveries due | admin | complete | PROCUREMENT | no | nobody told |
| Register beds for cleaning | admin | complete | HOUSEKEEPING | no | one bed at a time |
| Turn a bed around | admin as cleaner | partial | HOUSEKEEPING | no | no role; nurse cannot mark dirty |
| Out of service and back | admin | complete | HOUSEKEEPING | no | not linked to a breakdown |
| Ward readiness and turnaround | admin | complete | HOUSEKEEPING | no | readiness and capacity disagree |
| Linen movements | admin | partial | HOUSEKEEPING | no | counts only |
| Staff register and holidays | admin | partial | WORKFORCE | no | shift templates no screen; not linked to logins |
| Duty roster | admin | complete | WORKFORCE | no | only admin can read it; escalation ignores it |
| Attendance from the muster | admin | partial | WORKFORCE | no | retyped from paper; no leave request |
| Monthly muster, payroll export | admin | complete | WORKFORCE | no | no month lock |
| Equipment register | admin | partial | BIOMEDICAL | no | down or condemned API only |
| Calibration and PM | admin | complete | BIOMEDICAL | no | ward attention has no screen |
| Breakdown and return to service | admin | partial | BIOMEDICAL | no | ward cannot report one |
| AMC or CMC contract | admin | server only | BIOMEDICAL | no | entry has no screen |
| Ward sees its devices needing attention | nurse, doctor | server only | BIOMEDICAL | no | no tab or card |
| Diet order including NPO | doctor, nurse | complete | DIETARY | no | no admission prompt; not linked to OT list |
| Ward diet list | nurse | complete | DIETARY | no | no tray card; intake not charted |
| Kitchen counts and indent | admin, nurse | complete | DIETARY | no | trays, not raw materials |
| Close lapsed diet orders | nurse, doctor | complete | DIETARY | no | catering desk cannot |
| Facility identity, multi-site | admin | stub | MULTI_SITE | no | one read route |

### 2.7 Quality, statutory records and administration

Nothing here is dark. The finding runs the other way: ten real workflows are routes with no screen, and several Trust Center controls pass on the route existing.

| Workflow | Who | Status | Switch | WR | Gap inside it |
|---|---|---|---|---|---|
| Report an incident | admin on screen; nurse, doctor, pharmacist by API | partial | NABH | no | tab on the admin rail only |
| Investigate, actions, close | admin | complete | NABH | no | no reminders, RCA template, sign-off |
| Quality indicators | admin | complete | NABH | no | 11 only; no HAI, occupancy; no trend |
| Record a disposition | admin | server only | NABH | no | no screen; second table beside ADT's |
| NABH evidence pack | admin | complete | NABH | no | Markdown or JSON; no documents |
| Record ABHA and verification | admin | partial | ABDM | no | correction has no button; nothing transmitted |
| Unlinked encounters, care context | admin | complete | ABDM | no | records a step done elsewhere |
| ABDM consent artefacts | admin | server only | ABDM | no | only revoke wired |
| DPDP consent register | doctor | server only | none | no | no screen; not the desk |
| Data principal access export | admin | server only | none | no | JSON; no request log |
| Right-to-erasure request | admin | complete | none | yes | manual steps untracked |
| Retention matrix review | admin | server only | MINOR_RETENTION_AGE | no | no disposal register |
| Breach register | admin | server only | none | no | no clock or letter |
| Staff FHIR export | doctor, admin | server only | none | no | no labs, documents, encounters |
| Audit log search and trace | admin | complete | none | yes | no export; no access report; no anomaly alert |
| Security activity | admin | complete | none | hidden | no unlock button |
| Flight Recorder | admin | complete | none | yes | verifies only on demand |
| Trust Center | admin | complete | none | yes | read only; no snapshot |
| Model registry | admin, chip | complete | none | no | static seed |
| Sage provenance replay | admin | complete | SAGE_PROVENANCE; 404 models off | no | no export; 400-day prune |
| Create a staff account | admin | complete | none | yes | no registration number; no forced change |
| Change role, deactivate, reactivate | admin | complete | none | yes | no reason or date |
| Reset a password; own password | admin; user by API | partial | none | yes | self-service has no screen |
| MFA enrol and disable | staff | complete | MFA_REQUIRED_ROLES (empty) | yes | optional by default; no recovery codes |
| Sign in as staff or patient | all | complete | none | yes | lockout cleared by API; no forgot-password |
| Tablet PIN sign-in | admin by API, ward | partial | none | yes | enrol, revoke, PIN have no screen |
| Break-glass: unlock, MFA reset, PIN | admin | server only | none | no | the 3 a.m. lockout needs curl |
| Create a backup | admin | partial | BACKUP_DIR, RETAIN_DAYS | yes | no restore, receipt, schedule view, offsite |
| History import from legacy EHR | admin | server only | none | no | CSV only; no screen |
| Ingestion monitor, unmatched queue | admin, lab | complete | INGESTION_MODE, RESULT_QUARANTINE | no | no alarm when a feed stops |
| Appliance health on site | hospital IT | server only | METRICS_TOKEN | no | API or terminal only; /metrics open |
| Governance snapshot, alert list | admin | complete | SHADOW_ENABLED | no | hidden under WR |
| Impact rupee model | admin, owner | complete | none | no | cited defaults |
| End-of-shift outcomes | charge nurse, doctor | complete | SHADOW_ENABLED | yes | nothing reminds the nurse |
| Shadow scorecard, export | admin | complete | SHADOW_ENABLED | no | false alert rate unadjudicated |
| Adjudicate a shadow episode | clinician as admin | server only | SHADOW_ENABLED | no | no screen |

## 3. What a hospital of this size needs

Priority: must, should, could. Status is against Orb after the verifier's corrections. Rows marked (unverified) are the critic's additions with a status guess nobody has read the code for.

### 3.1 Front office, registration and appointments

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Consultant appointment, session list, phone reminder | should | dark | token and time band custom; offline rule |
| New patient registered once: UHID, duplicate check, notice, card | must | dark | ABDM; DPDP notice; APMCE Act register |
| Returning patient found; duplicates merged, reversibly | should | dark | the phone number is the identifier |
| OPD token, fee receipted, called in order, no-show | must | dark | fee before consultation; GST-exempt receipt |
| Unknown patient under a temporary identity (unverified) | should | partly | MLC practice; NABH AAC |
| Inbound referral accepted with a bed, outcome sent back (unverified) | should | partly | NABH AAC 1 and 6 |
| Referring doctor recorded, sent the summary, never paid (unverified) | could | nothing | NMC Ethics reg 6.4 |
| Health check package on one slip (unverified) | could | nothing | CEA tariff display |
| Charter, registration, tariff displayed; written estimate | should | missing | APMCE display duties; NABH PRE |

### 3.2 OPD and clinics

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Consultation note and a prescription with the registration number | must | dark | IMC 2002 clause 1.5; Schedule H1 |
| OPD pharmacy sale: batch, MRP, GST, H1 register | must | missing | D and C Rule 65; Forms 20, 21; NPPA |
| Scheduled drugs and implants at or below NPPA ceiling (unverified) | must | nothing | DPCO 2013 paras 24 to 26 |
| Vaccine with batch, next due, AEFI, U-WIN (unverified) | should | nothing | UIP; AEFI 2024 |
| Animal bite schedule, defaulters, register (unverified) | should | nothing | NRCP; IDSP |
| Physiotherapy charted and charged per session (unverified) | should | nothing | NABH COP |
| Antenatal case with RCH ID and EDD list (unverified) | should | nothing | RCH; PMSMA |
| 48-hour call, review booked, 30-day readmissions | could | partial | NABH readmission indicator |

### 3.3 Emergency

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Triage, casualty register, MLC flag (API takes triage_severity; the form never sends it) | must | missing | Parmanand Katara 1989; NABH AAC |
| MLC registered, police told, injury certificate, file restricted | must | missing | BNSS s.397; POCSO ss.19, 21 |
| Road accident cashless for 7 days; stabilise first (unverified) | must | nothing | MV Act s.162 scheme 2025; CEA s.12 |
| STEMI, stroke, trauma clocks (unverified) | should | nothing | NABH COP 3; STEMI India |
| Poisoning and snake bite: antidotes, register (unverified) | should | nothing | Poisons Act; IDSP |
| Brought dead register, MLC when cause unknown (unverified) | should | nothing | RBD Form 4A; CrPC s.174 |
| Code blue recorded; drills quarterly | must | missing | NABH COP; AHA or IRC |

### 3.4 Admission counter and payers

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Admission against the UHID with IP number and bed | must | partial | bed head ticket |
| Payer route, signed estimate, deposit at admission | must | partial | NABH PRE; Rights Charter; IT 269ST |
| Scheme verified, pre-authorised, claimed, reconciled | must | partial | PM-JAY; Aarogyasri; EHS, CGHS, ECHS, ESIC |
| TPA pre-auth, enhancement, discharge window, co-pay, settlement | must | partial | IRDAI 2024 circular; NHCX; ROHINI |
| Empanelment contracts with validity; claims blocked on lapse (unverified) | should | partly | IRDAI 2016; PM-JAY HEM |
| Day care stay with a day-care package (unverified) | should | partly | IRDAI day-care list |
| Informed consent per procedure in their language, witnessed | must | partial | NABH PRE; MCI 7.16; Samira Kohli |
| Privacy notice and data consent; minors (unverified status planned) | must | planned | DPDP ss.5 to 9; Rules 2025 |

### 3.5 Ward nursing

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Wristband with two identifiers checked | must | partial | NABH patient safety goal |
| Nursing assessment in the hour; doctor's in 24 hours | must | partial | NABH AAC |
| Care plan revised daily, countersigned | must | missing | NABH AAC, COP |
| Observations at the ordered frequency, overdue visible | must | built | TPR chart; NABH COP |
| Escalation to a named doctor in set minutes | must | partial | NABH COP; NEWS2 or MEWS |
| Consultant round: timed note, orders acknowledged, visit billed | must | partial | round sheet |
| Nurse handover signed, tasks carried, narcotic count | must | partial | NABH COP |
| Nursing note every shift (admission form has one box only) | must | missing | NABH IMS; CPA 2019 |
| Pain scored with every set, analgesic, rescored | must | missing | NABH COP; fifth vital |
| Intake and output balanced daily | should | missing | I/O sheet |
| Bedside glucose, device under lab QC, insulin | should | missing | GRBS; NABH AAC POCT |
| Neuro observations at interval | should | partial | neuro chart |
| Falls and pressure scoring, bundle, turning chart | must | partial | NABH COP; PSQ |
| Nutrition screen, dietitian, diet to kitchen | must | partial | NABH COP; FSSAI |
| Lines, catheters, drains dated; device days | must | missing | NABH HIC; VIP |
| Restraint on a timed order, 2-hourly checks | must | missing | NABH COP; MHCA s.97 |
| Isolation started, signposted, terminal clean | must | partial | NABH HIC |
| Patient education with teach-back | must | missing | NABH PRE |
| Sepsis screen and hour-1 bundle | should | built | Surviving Sepsis; ICMR |
| DNAR visible at handover (code status captured once at admission, read by nothing) | must | partial | Common Cause; ICMR DNAR 2020 |
| Crash cart checked, sealed every shift | must | planned | NABH COP, MOM |
| Oxygen prescribed to a target, charted, billed (unverified) | should | nothing | NABH MOM; MoHFW 2021 |
| Intra-hospital transport recorded (unverified) | could | nothing | NABH COP; ISCCM |
| Leave of absence with bed held (unverified) | could | built, dark | TPA room-rent rules |
| Night supervisor's round and report (unverified) | could | nothing | NABH HRM |

### 3.6 Medicines and pharmacy

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Order with duration, pharmacist check, supply, MAR signed | must | partial | NABH MOM |
| High-alert and LASA list, two-person check (ISMP list exists; no witness) | must | partial | NABH MOM; ISMP |
| Allergy with reaction on band and chart, override reasoned | must | partial | NABH MOM, PRE |
| Narcotics double-locked, traced, counted, return filed | must | dark | NDPS Act, 2015 Rules; Forms 3A to 3E |
| Reconciliation at admission, transfer, discharge | should | partial | NABH MOM |
| Verbal orders read back, countersigned in 24 h | must | missing | NABH MOM; R010 |
| ADR reported to PvPI, added to allergies | must | missing | PvPI; NABH MOM |
| Medication error graded, P and T review | must | partial | NABH MOM, PSQ; NCC MERP |
| Restricted antibiotic approval, antibiogram | must | partial | NABH HIC; ICMR AMSP |
| Weight at the door; paediatric dose per kg (unverified) | must | nothing, by design | NABH MOM 4; IAP |
| Chemotherapy on protocol, BSA, double check (unverified) | could | nothing | NABH MOM 7; NCG |
| Formulary with P and T control, non-formulary route | must | partial | D and C schedules; DPCO |
| Ward indent, supplied with batch, acknowledged, charged | must | partial | indent book; H1 and NDPS registers |
| Take-home dispensed, returns credited, counselling | should | partial | NABH MOM, PRE |

### 3.7 Laboratory

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Order with two identifiers, tube and accession | must | dark | NABH AAC |
| Bedside collection, bench receipt or rejection | must | dark | NABH two identifiers |
| Analyser results, flags, pathologist signature | must | dark | CEA signatory; NABL 112; ASTM |
| Critical phoned, read back, action written | must | dark | NABH AAC |
| Report on letterhead within TAT | should | dark | WhatsApp norm; NABH TAT |
| Amend with original kept, doctor told | should | dark | NABH AAC, PSQ |
| Internal QC and EQAS | must | missing | NABH AAC; EQAS schemes |
| Send-out register | must | partial | NABH AAC outsourced |
| Surgical specimen logged, histology acknowledged | must | missing | NABH COP |
| Culture staging, antibiogram | should | missing | NABH HIC |
| HIV counselling, consent, confidentiality | must | missing | HIV Act 2017; NACO |
| Results acknowledged by the ordering doctor; pending at discharge (unverified) | should | partly | NABH AAC 7 |
| NABL document set (unverified) | could | partly | NABL 112 |

### 3.8 Radiology and blood

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Imaging ordered, screened, archived, radiologist report, critical phoned | must | partial | NABH AAC; AERB; Telemedicine 2020 |
| Form F before obstetric ultrasound; register; monthly report | must | missing | PCPNDT Act, Rules 2003 |
| AERB eLORA licence, TLD badges, QA, RSO | must | missing | Radiation Protection Rules 2004 |
| Outside imaging sent and report filed (unverified) | could | nothing | NABH AAC 4 |
| Blood crossmatched, issued, two-checked, monitored, reactions reported | must | missing | D and C Part XIIB; NBTC; HvPI |
| Blood centre donor side, e-RaktKosh (unverified) | should | nothing | Schedule F; Form 27C |

### 3.9 Theatre, anaesthesia, CSSD

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Surgery booked, list published, implants and blood, team confirmed | must | partial | NABH COP; scheme pre-auth |
| Pre-anaesthesia check with ASA grade | must | partial | NABH COP; ISA |
| Ward prep and signed checklist to OT, NPO visible | must | partial | WHO safe surgery |
| WHO checklist attested (attest works; no elective case can exist) | must | partial | NABH COP |
| Operative note, counts, implant traced, OT register | must | partial | NABH COP; MDR 2017; NPPA caps |
| Anaesthesia record at ISA standard | must | missing | NABH COP; ISA |
| Recovery scored, released, handed over | must | partial | NABH COP |
| OT environment validated, machine checked per list | must | missing | NABH HIC; ISA |
| CSSD load tracked and traced | must | missing | NABH HIC |
| Sterilisation on the standard form, reported (unverified) | could | nothing | FP Indemnity Scheme 2013 |

### 3.10 ICU and specialty units

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| ICU hourly flow sheet, ventilator, bundles, step-down | must | partial | NABH COP; ISCCM |
| Dialysis sessions, segregated machines, water, register (unverified) | should | nothing | PMNDP; AAMI |
| NICU record linked to mother, feeds, weight (unverified) | should | nothing | NABH COP; FBNC; IMS Act |
| Birth on Form 1, newborn banded, partograph | must | missing | RBD Act Form 1; PCPNDT |
| Delivery register, Robson audit, stillbirths (unverified) | must | nothing | RBD Form 3; LaQshya |
| MTP forms and register, monthly report (unverified) | must | nothing | MTP Act 1971, 2021 |
| Psychiatric admission under the Act (unverified) | could | nothing | MHCA 2017 ss.85 to 99 |
| Brain-stem death on Form 10 | could | missing | THOTA 1994 |

### 3.11 Discharge, transfer and end of stay

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Discharge planned with an expected date; desks told | should | partial | IRDAI 3-hour rule |
| Summary signed and handed over in their language | must | built | NABH AAC; MCI 1.3 |
| Bed vacated, cleaned, marked ready | should | partial | NABH FMS, HIC |
| Transfer out with acceptance, summary, ambulance register | must | partial | NABH AAC; AIS 125 |
| Bed board true for every bed; every move timed, reasoned and accepted by the receiving nurse; room class billed from that time | must | dark | bed head ticket; TPA room-rent rules |
| LAMA or DAMA form witnessed, bill settled, police for MLC | must | dark | LAMA and DAMA forms |
| Death declared, Form 4, body released, death register, review | must | dark | RBD Act 2023; MCI 1.3.3; BNSS |
| Mortuary register, cold chamber log, release, unclaimed (unverified) | should | nothing | MoHFW mortuary guidelines |

### 3.12 Infection control and quality

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| HAI surveillance, device days, hand hygiene, HICC | must | missing | NABH HIC; NHSN |
| Needle-stick in 2 h, PEP, 6-month follow-up | must | partial | NACO PEP; NABH HIC |
| IDSP, Ni-kshay, maternal death reporting | must | missing | IDSP; TB Gazette 2018; MDSR |
| Incident in 24 h by any staff member, RCA, closure, sentinel to NABH | must | partial | NABH PSQ |
| Monthly indicators per ward, trended, minuted | must | partial | NABH PSQ |
| Death reviewed at M and M in 7 days | must | partial | NABH COP, PSQ |
| Internal and clinical audits, CAPA | must | missing | NABH PSQ, ROM |
| Document control for SOPs | must | missing | NABH ROM, IMS |
| Committees on calendar with minutes | should | missing | NABH ROM; POSH |
| Complaint in 48 h, grievance register, feedback | must | missing | NABH PRE; Rights Charter; CPA 2019 |
| Disaster plan drilled yearly | must | missing | NABH FMS; NDMA 2016 |
| Violence against staff logged | could | partial | Telangana Medicare Service Persons Act 2008 |

### 3.13 Billing, finance and accounts

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Rate card and packages per service, class, payer; displayed | must | partial | CEA, APMCE; CGHS, HBP, GIPSA |
| Consumables, procedures, nursing captured same shift | must | partial | consumable sheet |
| Consultant visits posted, payouts with TDS | should | missing | TDS 194J |
| OT charge sheet in 24 h with implant sticker | must | missing | NPPA caps; TPA rule |
| Interim bill every 48 to 72 h, credit limit watch | must | partial | TPA enhancement |
| Final bill with GST split, settled, clearance to ward | must | built | GST 03/2022; itemised bill |
| Discount, write-off, refund under a matrix | should | partial | 12A and 80G |
| Receipt with serial; cash ceiling refused | must | partial | IT 269ST |
| Day-end cash by denomination, POS and UPI matched | must | built | owner's daily collection |
| GST invoices, e-invoice, GSTR data | must | partial | CGST 12/2017; Rule 42 |
| Credit parties invoiced, receivables aged | must | partial | corporate MoUs; TDS 194J |
| Owner's daily and monthly MIS; DMHO return | must | partial | NABH ROM; DMHO |
| Morning census and critical list on one sheet | should | partial | night report |
| Vendor payables, TDS, MSME window | must | missing | IT 43B(h); TDS 194C |
| TDS deposit, 24Q and 26Q, Form 16, 26AS (unverified) | must | partly | IT ss.192, 194C, 194J; s.44AB |
| Bank, petty cash, POS and UPI reconciliation (unverified) | should | partly | Companies Act books |
| Budget versus actual (unverified) | could | nothing | practice |
| Fixed asset register, depreciation, disposal (unverified) | should | partly | Companies Act Sch. II; E-Waste Rules |
| Insurance policies renewed, claims intimated (unverified) | should | nothing | CPA 2019; PLI Act 1991 |

### 3.14 Supply and stores

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Requisition to numbered PO, quotations, second approver | must | partial | Forms 20B, 21B |
| GRN with batch, shelf life, MRP, debit note, invoice to accounts | must | partial | D and C purchase bill; e-way bill |
| Near-expiry pulled FEFO, returned, credited, destroyed via BMW | must | dark | NABH MOM; BMW Rules |
| NSQ or recall actioned, patients identified | must | planned | CDSCO NSQ; MDR 2017 |
| Stock counts with variances approved | should | planned | audit closing stock |
| Fridge drugs 2 to 8 C logged, excursions | must | missing | NABH MOM; UIP cold chain |
| Oxygen and gas stock, pipeline, licence | must | missing | NABH FMS; PESO |
| Consignment implants counted, invoiced on use (unverified) | should | nothing | GST consignment; NPPA |

### 3.15 Facilities and engineering

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Device register with PM, calibration, AMC, ward flagged | must | partial | NABH FMS; Legal Metrology |
| Breakdown from the ward, standby, downtime | should | partial | NABH FMS |
| Fire NOC, extinguishers, drills, training | must | missing | Telangana Fire Act 1999; NBC 2016 |
| DG tests, UPS, earthing, electrical audit | must | missing | CEA regulations |
| Licence calendar with expiry and owner | must | missing | APMCE; TSPCB; DCA; AERB; PESO |
| BMW segregated, weighed, manifested, Form IV | must | missing | BMW Rules 2016 |
| Housekeeping checklists, audits to HICC | should | partial | NABH HIC, FMS |
| Water, OT air, dialysis water, pest control | should | missing | IS 10500; NABH HIC |
| Facility work orders, building PM calendar (unverified) | should | partly | NABH FMS 4; Lifts Act |
| Consent to operate, STP, Form V, hazardous and e-waste (unverified) | must | nothing | Water and Air Acts; HOWM 2016 |
| Scales stamped under legal metrology (unverified) | could | nothing | Legal Metrology Act 2009 |
| Visitor and attendant control (unverified) | could | nothing | NABH FMS; PSARA |

### 3.16 People

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Credentialing, privileging, expiry blocks the roster | must | missing | NABH HRM; TSMC; TNMC |
| Doctors' on-call rota per specialty per day (unverified) | must | nothing | NABH HRM, COP; CEA 24x7 |
| Duty roster with in-charge and ratio, swaps | must | partial | INC norms; NABH HRM |
| Attendance, overtime, leave; month locked | should | partial | Telangana Shops Act |
| Payroll with PF, ESI, PT, TDS, payslips | must | partial | EPF; ESI; PT; Gratuity |
| Onboarding file, health check, hepatitis B, scoped login | must | partial | NABH HRM |
| Mandatory training calendar with expiry | must | missing | NABH HRM |
| Appraisal, disciplinary, POSH committee | should | missing | NABH HRM; POSH 2013 |
| Exit with no-dues, same-day revocation | should | partial | Gratuity; DPDP |
| Outsourced staff: licence, PF and ESI proof before invoice (unverified) | should | nothing | CLRA 1970; EPF s.8A |
| Doctor-to-doctor handover (unverified) | should | partly | NABH COP; IPSG |

### 3.17 Medical records, compliance and IT

| Need | Pri | Status | Indian rule |
|---|---|---|---|
| Record assembled in 48 h, deficiencies chased, ICD-10 coded | must | partial | NABH IMS; MCI 1.3 |
| Retention, holds, certified destruction | must | partial | MCI 1.3.1; DPDP 8(7) |
| Patient's records in 72 h; releases logged | must | partial | MCI 1.3.2; DPDP s.11 |
| Corrections with the original visible; late entries labelled | must | built | NABH IMS, DHS |
| Access by role, logged, leavers removed, chain proven | must | built | NABH IMS; DPDP 8(5) |
| Data principal requests answered in time | should | partial | DPDP ss.11 to 13 |
| Breach logged, patients and Board told in 72 h | must | partial | DPDP 8(6); CERT-In 2022 |
| ABHA, HFR, HPR, HIP and HIU sharing | should | partial | ABDM; DHIS |
| Narcotic, MTP, PCPNDT, MLC registers and returns | must | missing | MTP Forms I to III; NDPS Forms 3A to 3E |
| Medical certificates with serial and register (unverified) | should | nothing | NMC Ethics reg 1.3.3 |
| Medico-legal claim file, insurer intimated (unverified) | should | nothing | CPA 2019; CrPC s.91 |
| Statutory inspection recorded and answered (unverified) | should | nothing | D and C; PCPNDT; AERB |
| Downtime declared, paper kit, back-entry reconciled, drilled | must | partial | NABH IMS, DHS |
| Daily backup checked, offsite, restore proven, keys escrowed | must | partial | NABH IMS; DPDP Rules 2025 |
| Ethics committee for any study | could | missing | NDCT Rules 2019 |

## 4. What is missing, ranked by the buyer

The hospital reader ranked the 144 verified gaps twice: for a full Orb hospital (every module on) and for a founding ward (Orb Ward Record, everything else on the hospital's existing system). The 48 unverified additions were not ranked.

### 4.1 Blocks the sale of a full Orb hospital (21)

| Gap | The hospital's reason |
|---|---|
| Registration with UHID | no file opens without a UHID; the switch is off and the series unconfigured |
| OPD token, fee, queue | OPD cash is the daily bread; a counter that cannot bill a walk-in cannot replace ours |
| OPD prescription with registration number | a consultant who cannot print one will not touch the screen |
| OPD pharmacy sale and H1 register | the biggest till in the building; the drug inspector reads the register |
| Payer, estimate, deposit at admission | the counter's first question is cash or scheme; the owner's first demand is the deposit |
| Nursing assessment within the hour | no superintendent runs a ward where the first assessment has no form and no clock |
| Medicine order to MAR with pharmacist check | a stopped drug staying active and unsigned nurse charting cannot be allowed |
| Lab order and intake | five hundred tests a day cannot start from three tests and no requisition |
| Analyser results and ranges | hand-keying is what our lab left behind; the pathologist will refuse |
| Lab report on letterhead | the report is what the patient paid for |
| Surgery booked, OT list published | the list goes up every evening; a case cannot even be booked |
| ICU flow sheet | the one chart I would never split between screen and paper |
| Nursing note every shift | otherwise the hospital runs on two records |
| Bed board with acceptance and class billing | the heartbeat for the MS, the counter and billing; a typed bed name is not a board |
| Death with Form 4 and the register | the law; a death recorded as a discharge cannot be our record |
| Birth register and newborn record | Form 1 within 21 days is the law for any labour room |
| Rate card and packages | how we quote and bill every surgery |
| Printed receipt with serial; 269ST ceiling | a receipt that cannot be printed is not a receipt |
| Owner's MIS on one page | census, collections and outstanding on one sheet decides whether he signs |
| ABDM compliance | every empanelment form asks; a strictly offline product answers no |
| Offsite backup and a proven restore | if the box burns the records go with it, and the law wants three years |

### 4.2 Daily pain in a full Orb hospital (49)

Front office and money: consultant appointments (reception runs on a diary and WhatsApp); duplicate merge (found weekly, never mergeable); casualty register (thirty to sixty a day on paper); MLC flag (files cannot be held); ward clerk admission (night admissions wait for the counter); scheme and TPA desks (finance reconciles in Excel; the discharge-day argument continues); consumable capture (the biggest leak); consultant visit posting (the owner's standing argument); OT charge sheet; interim bill and credit limit (the discharge-day shock); discounts and refunds (the cashier argues with the patient); receivables aged (the owner's weekly question); the morning census sheet (the night supervisor still writes it).

Ward and medicines: wristbands (two hundred by hand daily); escalation to a named doctor (the sister would still phone; the score would change nothing at night); round orders (copied from screen to paper); signed and received handover; high-alert witness (or a paper witness book beside the screen); an allergy found on day three (a red sticker); NDPS shift count and return; discharge reconciliation (every drug ever charted); verbal orders (a nurse's name on a prescription she did not write); intake and output; pain score (a second sheet beside the screen, also on the founding ward); glucose chart; take-home medicines and ward returns; bed turnaround (the nurse must mark the bed dirty herself).

Diagnostics and theatre: sample collection (a printer per ward, no back-dating); critical closure and ward acknowledgement (the book stays); amend on screen; send-out register; specimen register (a lost specimen is a sentinel event); imaging sign-off and critical path; transfusion record; PAC with ASA grade; pre-op checklist (the kitchen still feeds a fasting patient); counts, implant sticker, OT register.

Supply, people, records: formulary edited as a file (every P and T addition waits on a programmer); ward indent (the paper indent book stays); PO print and single-account approval; GRN debit notes; near-expiry credit; stock take (a figure nobody can correct is a figure nobody trusts); roster invisible to the ward; records assembly and ICD coding; release of records (JSON is not a copy); downtime pack inside the box (printed to every ward by hand each night).

### 4.3 Assessment gaps (about 50)

These stay on paper in the hospital's own file and the ward does not miss them day to day; the NABH assessor, the drugs inspector, the pollution board or the DMHO asks for them. Assessor: Form F register, consent forms, care plan, falls and pressure scoring, nutrition screen, neuro chart, education record, restraint, isolation, DNAR, code sheet and drills, crash cart checklist, recovery score, CSSD register, line and device charts, incident reporting by the nurse herself, indicator trends and minutes, M and M, audits and CAPA, document control, grievance register, display boards, credentialing, training register, onboarding file, HAI surveillance, needle-stick follow-up, fire file, disaster plan, device certificates, fridge logs, licence file, BMW register, transfer acceptance, LAMA statistics, retention certificates, DPDP notice and breach register, ADR and PvPI, P and T grading, antibiogram, QC and EQAS, HIV confidentiality, microbiology staging, AERB file. Inspector or DMHO: notifiable disease register, statutory registers in the prescribed form, GST return export (the CA files from Tally). Founding ward: downtime (the clerk copies from the sheet anyway) and backup (the paper sheet stays the original; a lost box loses a month's file).

### 4.4 Nice (about 20)

Anaesthesia record (paper everywhere, anaesthetists prefer it), OT environment logs, expected discharge date, 48-hour call, vendor payables (Tally), gas logs, breakdown from the ward, DG and electrical logs, housekeeping checklists, water and pest reports, attendance and leave (biometric and Excel), payroll (the bureau), appraisals and POSH, exit no-dues, violence log, committee minutes, data principal requests, brain-stem death, ethics committee.

### 4.5 The founding ward

Under Orb Ward Record the ward buys the observation record and the monthly rounds file and keeps everything else on the hospital's existing system. Of the 144 ranked gaps all but a handful are "not touched" or "nice" for it. Three bite it: pain is the fifth vital on every NABH observation sheet, so a sheet without a pain column makes the sister keep a second sheet (daily pain); the downtime pack lives on the appliance's own disk (assessment gap, the paper sheet is the source anyway); a lost box loses a month's file with no offsite copy (assessment gap). Two sales-plan promises in SALES_PLAN_2026-10.md section 3 are not met by the code: the clerk admitting with a ward and bed picker (admit needs patient.write; adt-17b not built) and the clerk marking a patient as left with the time (end of stay is doctor only). Escalation is "nice" today because scoring is off, and becomes the core the day the clinicians switch scoring on.

## 5. What Orb has that nobody asked for

Inventory workflows that matched no need. One line each and whether to keep selling it.

| Workflow | Keep selling? |
|---|---|
| Quick admit from a Scribe note | keep as a convenience; never lead with it, it skips DOB and the first obs |
| Roster import from the old HIS | keep; put it on the Ward Record rail, it is the cutover tool |
| History import from the legacy EHR | keep as an engineer's tool; no screen, do not sell |
| Adopt chart rows into the order store | internal migration step, not a feature |
| Vitals simulator | demo only; never a feature |
| Today (doctor home) | keep; costs nothing |
| Patient story and mark reviewed | keep; the chart's reading surface |
| Second pass against the transcript | switch it on by default or stop claiming it |
| Reject a draft note | keep; part of signing |
| Auto-chart from a signed note | pause until the signing-time vitals stamp is fixed |
| Sage drafts a medicine to pharmacy | stop; the doctor who sees the card gets 403 |
| Relay ambient card to chart and pharmacy | stop until the PIN lane has a screen and the placeholder dose cannot be verified |
| Approve or decline a drafted assessment | keep; Tier 1 is deterministic |
| Sage assessment before the round | keep as an extra |
| Sage guideline and patient questions, thinking toggle, history, floating orb | keep for the full hospital; say the bed matcher is unsolved (R026) |
| Sage by voice | do not sell; off by default, 4B without sources |
| Sage provenance replay and the model registry | keep; the governance story a CIO asks for |
| Case room and department messaging | keep for the full hospital; it needs no model, but the founding programme sells the observation record and the rounds file, not team chat |
| Direct conversation | not sellable until the panel shows it |
| Wrong-room flag | keep; a real safety catch, tuned on synthetic data |
| Ward Record profile with the models off | keep; it is the founding programme |
| Forecast, surge scenario, Pulse calendar | keep as labelled projections, never predictions; Pulse needs review first |
| Finish lab work after switch-off | plumbing |
| Linen movements | keep quietly; a par level would make it worth mentioning |
| Facility identity and multi-site | not sellable; one read route |
| Bridge: login, status, team, vitals, ask bar, medicines, end of discharge day | keep for the full hospital; the ask bar is dead on a founding ward |
| Trust Center and impact model | keep for the CIO conversation; both read only |
| End-of-shift outcomes, scorecard, adjudication | keep; the pilot's evidence instrument, not a hospital feature |
| Tablet PIN sign-in, break-glass, password reset | finish the screens before showing them |

## 6. The shortest honest list

Ten things to build next, in order. The first three serve the founding programme (the observation record and the rounds file); the rest are for a full Orb hospital and follow the waves in docs/HIS_BUILD_PLAN_2026-10.md.

1. Pain score on the observation sheet, stored and printed with the set. The one gap that bites the founding ward on its first day. BUILT 21 October 2026: `vitals.pain_score` (migration 87), keyed on the nurse's sheet and the clerk's copy, printed on the inpatient record and the downtime pack, trended on the chart, counted as a blank by the rounds file; never fed to NEWS2.
2. Ward clerk admits with a ward and bed picker and marks a patient as left with the time (adt-17b). The sales plan promises both; the code refuses both.
3. Roster import on the Ward Record rail. It needs no model, and it is how the ward list reaches the observation record on day one; hiding it costs the founding ward its cutover tool for a rail rule. Relay messaging stays a full-hospital feature: the founding programme sells the observation record and the rounds file, nothing more.
4. A nursing note type nurses can write. Nurses hold vitals.write and not patient.write, so nursing documentation is paper everywhere; this blocks a full hospital sale on its own.
5. Allergy add and deactivate on the chart after admission, with reaction and severity. The routes and the interlock exist; only the controls are missing.
6. Stop, hold and resume a charted medicine (W6-D, the empty lifecycle router). The largest clinical gap: a stopped drug stays due on the round.
7. An escalation screen with the acknowledge route wired, and a doctors' on-call rota it reads so the chain names a real person. Without this the score changes nothing at 2 a.m.
8. Money at the counter: receipt print with the configured series, refund and discount requests raised at the counter, the one-step outpatient bill (opd-14). Three small screens that unblock billing and payments.
9. Elective case booking and a theatre master. Until a surgery row can be created from a screen, the WHO checklist, team alert and operative note cannot serve a real elective list.
10. Death, LAMA and DAMA on a default site: end types without ORB_ADT, a Form 4 print, a death register, one dispositions table shared with the NABH indicators.

After these: analyser intake for the lab (lis-24, lis-25) with shipped reference ranges, and the ward imprest (grn-21), the real supply path for most doses in India.

## 7. How this was checked

The code verifier re-read the code behind every disputed cell after the needs were matched. Nineteen corrections were raised; every one is applied above.

Status changes: WHO checklist, built to partial (attest works; no route creates a surgery, cases are seed data or a Sage handoff). DNAR, missing to partial (a code status with a DNR option captured once at admission, printed in the downtime pack, read by nothing else). High-alert drugs, missing to partial (an ISMP list in drug_safety_service gates dose ceilings and agentic dispatch; AdministerRequest has no witness field). Consultant appointments, partial to dark (slot booking, day list, cancel, reschedule, check-in and no-show exist on the front desk's Appointments view behind ORB_OPD and ORB_REGISTRATION; opd-16 is built, not waiting on Wave 7).

Screens the first pass said did not exist and do: ADT swap, reserve and undo actions, the PresenceDrawer and CensusView (dark behind ORB_ADT); lab ReportedResults for amend and withdraw and CriticalView for the callback that closes a critical (dark behind ORB_LAB); the pharmacy store's NdpsView and VendorReturnsView (dark behind ORB_PHARMACY_STORE); the /display/opd token board kiosk and the OPD panel's convert-to-inpatient button (dark behind ORB_OPD). Each status stayed as it was because the switch defaults off; the evidence and gap wording were corrected.

Evidence corrections: the escalation gap points at the unwired acknowledge route (vigil_routes.py:1307) rather than the tier policy; the patient FHIR download has no Bridge caller, so it is server only and off the release-of-records row; an orders list print exists (OrdersPrintView), so the lab gap is a lab-formatted requisition, not any print; the admission form carries a one-off nursing_notes box, so the nursing note gap is "nothing per shift"; the admission API accepts triage_severity and the form never sends it, so the casualty gap is "no screen collects triage".

What was not checked. The 48 additions carry the critic's status guesses from text searches and were not read in the code; they are marked unverified throughout. Wave 7 of the HIS build is uncommitted in the working tree (registration, front desk, bridge, ADT presence and reservations, census, facilities spine, charge rules, appointment slots), so several server-only and dark rows may move while this is read. No server was started, no test was run and no screen was driven in a browser; "complete" means the code path is wired end to end, not that it was exercised. The hospital ranking is one reader's voice standing in for five lenses, not a survey of buyers. The Indian rules in section 3 are as the needs writer gave them and were not checked against statute text. The tier counts in 4.3 and 4.4 are approximate, read by hand from 144 rows, and the four tiers add to 139, so a few ranked gaps sit in no tier.
