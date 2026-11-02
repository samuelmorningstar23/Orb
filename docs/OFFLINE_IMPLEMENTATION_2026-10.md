# Offline implementation pass, 11 October 2026

## Executive scope

Samuel authorised implementation of the missing features that need no external
permission, with Orb kept offline and held clinical decisions left to him.
This pass extends the local hospital system; it does not turn on a live site's
modules, train on restricted data, transmit claims or connect to national services.
Existing checkout changes are preserved. No project instruction file was found in
the previously documented repository/worktree/history search or this checkout's
instruction-file check.

## Prioritised matrix and execution sequence

| Order | Capability | Implementation and expected behaviour | Boundary |
|---|---|---|---|
| 1 | Shared local workflow controls | Dedicated permissions, default-off module switches, immutable facility/department provenance, bounded validation, version conflicts, same-transaction audit and patient search | Keep the current multi-facility fence; never expand clinical scope implicitly |
| 2 | Complaints, mortuary, CSSD | Persistent local registers, explicit state transitions, accountability, searchable bounded queues and screens | No remote messaging, body certification or automated sterilisation/device control |
| 3 | Reporting and cohort queries | Allowlisted fields, operators and aggregates, scoped parameterised queries, saved definitions and CSV export | No arbitrary SQL or externally hosted warehouse; no clinical risk scoring |
| 4 | Finance tools | Explicit charge attribution, configured payout rules, reproducible settlements and mapped balanced accounting export | Integer paise; never infer the treating doctor as the earning doctor, or invent accounting/tax rules |
| 5 | ED, care plans and radiology records | Manual triage/assessment and disposition records; problem/goal/intervention/evaluation plans; study workflow, report signing and addenda | No automatic treatment, prescribing, discharge or DICOM/device/network transport |
| 6 | Perioperative and dialysis records | Authored anaesthesia/recovery documentation, implant lot linkage and dialysis session records | Existing surgery lifecycle stays authoritative; no automatic device control or invented clinical thresholds |
| 7 | Record governance | Explicit local legal holds and controlled local records/release tracking | Restricted record-class policy requiring an unresolved governance ruling remains held |
| 8 | Specified encounter isolation | Bridge current-stay filtering under the already recorded R029 decision | Fresh readmission password reissue already exists; preserve portal access rules |
| 9 | Data engineering | Non-sepsis dataset adapters with synthetic validation; assess the PostgreSQL port against executable acceptance | No restricted corpus access, training claims or placeholder driver-only backend; PostgreSQL remains open |
| 10 | Integration and verification | Shell navigation, strict typing, scoped API tests, transaction/concurrency tests, disabled-module checks, offline checks and build verification | Report actual completed scope and remaining technical limitations precisely |

## Files and ownership

- Shared integration: `server/config.py`, `server/permissions.py`,
  `server/main.py`, `src/lib/roles.ts`, `src/shellConfig.tsx`,
  `src/pages/ClinicianShell.tsx`; new shared local workflow helpers and picker.
- Operations: new local operations service/router, migration 81, API, panel and tests.
- Reporting/finance: new reporting and finance service/router, migration 82,
  APIs, panels and tests.
- Clinical documentation: new domain services/routers, migration 83,
  APIs, panels and tests.
- Governance and remaining data work: separate migrations 84 onward, explicit
  service integrations and tests. Migration numbers are reserved for this pass.

## Implemented local capabilities

These are persistent workflows with APIs and screens, rather than static mock
panels. The database schema adds migrations 81–86. They run through the existing
SQLite/SQLCipher connection seam. Tests use disposable plaintext SQLite databases;
they do not establish encrypted-appliance or hospital acceptance.

| Module | Implemented behaviour | Explicit boundary |
|---|---|---|
| Complaints / feedback | Original statement, assignment, investigation, response notes, resolution, closure/reopening and history | No outbound notification or inferred clinical finding |
| Mortuary | Identity and receiving references, storage and custody, holds, authorised handover and distinct active witness account reference | The witness reference is not a witness signature; no death certification |
| CSSD | Pack register, manually authored cycle evidence, quarantine, release, recall, issue/return and surgery/patient custody linkage | No sterilisation recipe, automatic expiry or device actuation |
| Reporting | Allowlisted patient/admission/OPD/invoice/receipt datasets, filters, diagnosis-coded cohorts, grouped measures, owner-scoped saved definitions and CSV | No arbitrary SQL; identifying filters cannot be saved in definitions |
| Finance tools | Explicit charge-to-doctor attribution, effective configured payout rules, reproducible draft statements, distinct-person approval, payment reference and balanced mapped journal exports | Clerk identity is never treated as earning doctor; no tax/rate invention, bank transfer or accounting-system submission |
| Radiology | Study worklist and authored reports, progression through performed/reported/signed states, signed-record addenda | No PACS, DICOM, image transport or automated interpretation |
| Care plans | Authored problems, goals, interventions, ownership, due/completion and evaluation records | No autonomous treatment or new nursing/prescribing authority |
| Emergency | Authored triage, assessment, timepoints and disposition documentation | Registered-patient intake requires registration authority; existing scoped ED documentation does not widen the inpatient chart |
| Perioperative | Existing-surgery-linked team, anaesthetic/recovery observations and implant lot documentation | Existing surgery lifecycle remains authoritative |
| Dialysis | Prescribed and actual session values, complications and completion records | Prescription changes remain doctor-only; no device control or invented thresholds |
| Records governance | File custody, release requests with approval before handover, explicit legal holds, revision conflicts and history | No unresolved restricted-class access/break-glass policy; no invented retention periods |
| Radiation safety | Equipment licence and officer-appointment references, validity dates, search, withdrawal and history | Records evidence only; does not issue a licence or clear equipment for use |
| Infection surveillance | Human-authored definitions, observed exposure days, reviewed case inclusion, isolation records and transcribed susceptibility data | Explicit ended/finalised population windows and configured suppression; no invented definitions, susceptibility interpretation or external reporting |

All new modules default to **off**. The `.env.example` additions are reference
configuration only; the live `.env` was not changed. The shell uses the server's
module states and dedicated permissions. Active stored identities, facility and
department boundaries are rechecked in services; the multi-facility fence remains
in force. Mutations use version guards and audit in the same transaction. Patient
search is bounded and audited; no new browser PHI persistence was introduced.

| Module ID | Environment switch |
|---|---|
| `feedback` | `ORB_FEEDBACK` |
| `mortuary` | `ORB_MORTUARY` |
| `cssd` | `ORB_CSSD` |
| `reporting` | `ORB_REPORTING` |
| `finance-tools` | `ORB_FINANCE_TOOLS` |
| `radiology` | `ORB_RADIOLOGY` |
| `care-plans` | `ORB_CARE_PLANS` |
| `emergency` | `ORB_EMERGENCY` |
| `perioperative` | `ORB_PERIOPERATIVE` |
| `dialysis` | `ORB_DIALYSIS` |
| `record-governance` | `ORB_RECORD_GOVERNANCE` |
| `radiation-safety` | `ORB_RADIATION_SAFETY` |
| `infection-surveillance` | `ORB_INFECTION_SURVEILLANCE` |

The initial operational/governance/reporting grants use the existing admin role.
Clinical documentation uses existing doctor/nurse roles: doctors author and sign
clinical records; nurses author care/ED/perioperative/session observations with
read-only RIS access. Admin clinical access is read-only. No new profession or
restricted-record entitlement is inferred. Backend and frontend grants agree.

## Correctness and privacy repairs

- Legal holds protect erasure during adjudication and again immediately before
  execution, including holds created after the decision. A blocked execution
  rolls back whole. Enforcement remains active even when the register switch is
  off. Retention review distinguishes legally held records and conservatively
  recognises malformed hold categories. New operational categories without an
  agreed retention policy are listed explicitly rather than assigned invented
  periods or automatically purged.
- Patient exports slice shared accounting journals and payout batches through
  patient-owned child records. Shared batch totals and other patients' entries
  are excluded. History/event rows with patient ownership participate in the
  existing schema-walk export.
- R029 Bridge medicines, vitals/status and discharge readers now use the current
  stay. Explicit encounter linkage takes precedence over legacy projection
  timestamps. No clinical medication status or discharge policy is changed.
- OPD counter submission now uses one transaction for charges, invoice, number
  allocation and audit calls. An unpriced later line or propagated exception
  leaves no stray charge/invoice and consumes no number, with pooling on or off.
  Existing billing-category audit failures remain fail-soft; this pass does not
  change that recorded policy or claim that every audit storage failure aborts
  a counter transaction.
- Non-sepsis Vigil extract handling validates owner-supplied labels, timestamps,
  subject-level splits and training-only normalisation. It matches the live
  feature/sequence contract and requires an explicit research output path.
  Validation-only writes no checkpoint; no training, dataset download or runtime
  model promotion was performed. See [the extract contract](VIGIL_LABELLED_EXTRACT.md).

The subsequent [completion and browser acceptance report](OFFLINE_COMPLETION_AND_BROWSER_2026-10.md) records the follow-up fixes and synthetic real-browser/light-dark verification. The results below describe the preceding implementation pass.

## Remaining engineering work

This pass does not close every historical roadmap item. In particular,
PostgreSQL remains deliberately rejected by `server/db_backend.py`: the current
migrations, FTS/vector extensions, SQL idioms and transaction semantics require a
real port and database-specific acceptance. No PostgreSQL server, `psql` or
container runtime was available for that acceptance here. SQLite/SQLCipher
remains the supported application path; a driver-only scaffold would not satisfy
the plan.

R026 has a recorded ruling but its latest candidate remains under review and is
not imported from failed/held work. Other historical AI-model, device, deployment,
whole-product readmission and detailed HIS epic acceptance remains tracked in the
existing audit and build plan. The manual workflows above are not a claim that
every requirement of those larger epics has shipped.

## Held and external work excluded

- R001/R002/R009/R010/R011/W111/W099 and their dependent medication/discharge
  changes; lis-17/R023; inter-facility transfer/MS-R3; unresolved restricted
  record access/break-glass policy.
- ABDM, NHCX/PM-JAY, insurer transmission, payment gateways, external reporting
  portals, PACS/modality/analyser connections and credentialed data acquisition.
- No production feature enablement, live migrations, patient data inspection,
  model downloads or regulated clinical go-live.

## Validation record

Fresh checks for this pass are recorded below; the earlier audit's results do
not validate these new features. The [machine-readable verification record](audit-2026-10/offline-implementation-verification.json)
contains the exact backend selection and command/evidence references.

| Check | Result |
|---|---|
| Combined backend selection covering new modules, privacy/retention, billing, Bridge, migrations and unauthenticated surfaces | **570 passed**, 64.43 s |
| Follow-up after final CSSD visibility, surveillance seal and retention-clock changes | **74 passed**, 3.02 s; overlaps the combined selection |
| Full frontend suite | **3,549 passed in 360 files**, 29.03 s |
| `tsc -b` and Vite production/PWA build | Passed |
| ESLint ratchet | Passed: 9 existing warnings, zero errors; baseline not increased |
| Contrast gate | 378 token/surface pairs, 3,346 component call sites and 96 hover/focus surfaces passed |
| Documentation citation check | 9,020 citations resolve; 490/490 symbol anchors correct |
| `git diff --check` | Passed |

Additional independent reviews covered access scope before pagination, mixed
patient finance exports, statement approval/reversal concurrency, fresh stored
identity, single-facility fences, audit rollback and frontend request ownership.
The tests use synthetic data. New-module real-browser/hospital acceptance,
encrypted hardware behaviour, clinical outcome validation and external-service
certification were not performed. Existing jsdom canvas/navigation diagnostics
are still emitted in the frontend suite; all assertions pass.
