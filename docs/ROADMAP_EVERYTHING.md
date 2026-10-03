# Roadmap: every module, to a sellable standard

Written 2026-09-07, from the gap analysis in `docs/HOSPITAL_OS_GAP_ANALYSIS.md`.
Covers all 28 gaps: 17 absent modules, 8 partial, 3 that exist in name only.

**The concern, stated once.** "Perfect everything" and "ship a pilot" compete for the same
solo capacity, and the honest finding below is that code is not the constraint. Orb's build
velocity is the fastest thing in this project. What gates a sale is an Indian legal entity,
a security audit, and three certifications that run on other people's calendars. Every module
could be built and Orb would still not be sellable. So this roadmap starts the slow track
first and lets the fast track fill the waiting time. That is the whole plan in one sentence.

---

## 1. Three tracks, three different physics

The existing capacity model in `PILOT_ROADMAP.md` already splits work into an engineering lane
(code) and a Samuel lane (a body, a signature, a relationship). Everything below inherits
that split, with one addition that does most of the work.

| Track | What it is | What sets the duration | Can it be compressed? |
|---|---|---|---|
| **BUILD** | Self-contained code | Effort | Yes, and this project is unusually fast at it |
| **INTEGRATE** | An adapter to somebody else's system | A counterparty who answers | Partly |
| **REGISTER** | An entity, a licence, an audit, a certification | A queue you are in | **No** |

**Why the third track decides the plan.** In August the entire admin spine, billing, payer,
procurement, NABH, ABDM records, biomedical, workforce, housekeeping and dietary, nine
substantial modules, landed inside roughly two weeks. That is the observed BUILD velocity and
the estimates below are calibrated on it, not on generic industry figures. Nothing on the
REGISTER track moves at anything like that speed, and no amount of working harder changes it.

**Integrate is not a lesser answer.** On an RFP checklist a working integration scores the
same line as a build, at a fraction of the cost, and for the regulated establishments it is
the only correct answer. A blood bank is licensed by the state drugs controller and belongs
to the hospital, not to Orb. The biomedical waste barcode system is operated by the treatment
facility. Building either would be building somebody else's licence.

---

## 2. Start these now, because they are queues

Nothing here is hard. All of it is slow, and none of it starts until somebody starts it.

| # | Item | Duration | Blocks | Note |
|---|---|---|---|---|
| R1 | **Indian legal entity decision** | Weeks to months | R2, R3, everything ABDM | NHA onboarding assumes an Indian entity. The incorporation plan currently parks this until post-pilot. It is now the first domino |
| R2 | **CDSCO intended-use position, in writing** | 2 to 4 weeks | The deck, the website, all copy | Decides Class A versus Class C. A Form MD-12 test licence covers evaluation pilots for three years and is the cheap route in |
| R3 | **CERT-In empanelled WASA** | 4 to 8 weeks | ABDM incentives, every CIO security review | Required for incentives regardless of certification. A CIO will ask for it anyway |
| R4 | **ABDM sandbox then production certification** | 12 to 20 weeks | DHIS revenue, NABH | Functional testing, WASA, an NHA demo, Health Tech Committee sign-off, go-live approval. Mostly waiting |
| R5 | **NABH HIS/EMR product certification** | 8 to 12 weeks | Credibility with no reference sites | Self-attestation route exists, test cases are published. Cheapest third-party proof available |
| R6 | **Licensed drug interaction source** | Commercial negotiation | Replacing 40 to 59 curated pairs | The current set is self-described as not comprehensive |
| R7 | **Terminology licences** | Days | Real ICD-10, LOINC, SNOMED validation | LOINC and ICD are free. SNOMED CT is covered for Indian use through the national licence |

**If only one thing happens this month, make it R1 and R2.** R1 because everything ABDM
queues behind it, and R2 because it costs nothing to decide and currently every deck slide is
quietly making the decision by accident.

---

## 3. Build track, phase by phase

Estimates are solo, AI-assisted, calibrated on the August admin-spine run. They assume the
module ships to Orb's own bar, which is tests plus the three-theme visual gate, not a demo.

### Phase 1: unblock the sale (about 5 weeks of build)

Small items with outsized commercial effect. Every one of these is a thing a buyer notices.

| # | Item | Est. | Why now |
|---|---|---|---|
| B1 | **Multi-site: facility scoping across 158 tables** | 2 to 3 wks | **Do this first or accept it is permanently hard.** 158 tables, 34 migrations, no production data yet, and SQLite cannot rebuild a foreign-key parent inside a transaction. The cost only rises from here, and it decides whether chains are sellable at all |
| B2 | **ABDM M1, M2 and M3 client on v3 APIs** | 3 to 4 wks | Build it while R1 and R4 are queuing so certification is not waiting on code |
| B3 | **Bed master, allocation, tariff linkage** | 1 wk | No beds table exists today. Beds are free-text strings and housekeeping states feed no capacity number |
| B4 | **Appointment slots, clinician availability, conflict prevention** | 1 wk | Currently checks the time is in the future, not that anyone is free |
| B5 | **History import UI** | 3 days | Server side is complete with 43 tests and no screen calls it. It exists for cutover day and cannot be used on cutover day |
| B6 | **Medico-legal labelling, restricted record classes** | 1 wk | MLC, MTP and prenatal records need a hard access boundary. Named explicitly in the NABH digital standard |
| B7 | **Results inbox, acknowledgement, critical callback** | 1 wk | Joins the orders table to the structured observations table, which today are not connected |
| B8 | **Terminology bulk loader** | 2 days | The script the code points you to does not exist. Until it does, validation runs against 76 hand-seeded codes |
| B9 | **Role taxonomy, 6 roles to about 12** | 3 days | Six storable roles for twelve modelled job functions. No radiographer, lab tech, physio, dietitian or biomedical engineer |

### Phase 2: the two universal-tier holes (about 8 weeks)

These are the only two modules that all fifteen surveyed vendors ship and Orb does not have.

| # | Item | Est. | Scope |
|---|---|---|---|
| B10 | **Laboratory information system** | 4 to 5 wks | Test and panel masters, specimen collection and tracking, accession numbers, barcode labels, worklists, analyser interfacing, result authorisation and pathologist sign-out, delta checks, amended results, turnaround tracking, quality control |
| B11 | **Radiology information system** | 2 wks | Modality worklist, radiographer workflow, reporting templates, report sign-off, study registry |
| B12 | **Lab report document ingestion** | 1 to 2 wks | No text-recognition library needed. Lens already uploads, encrypts, decrypts in memory and posts to a vision model. Point that spine at extraction, write to observations, require human verification before anything becomes clinical fact. Use MedGemma 1.5 4B, which scores 91 macro F1 here against the 27B's 76 |

### Phase 3: statutory registers (about 5 weeks)

Individually small. Each needs a registration on the REGISTER track to actually transmit.

| # | Item | Est. |
|---|---|---|
| B13 | Controlled substances: Schedule H1 register, NDPS Forms 3D, 3E, 3H | 1 wk |
| B14 | Termination of pregnancy: Form II monthly statement, Form III register, serial-number pseudonymity | 1 wk |
| B15 | Prenatal diagnostics: Form F, monthly return, two-year image retention | 1 wk |
| B16 | Birth and death via the civil registration portal, Forms 4 and 4A | 4 days |
| B17 | Legal-hold state on retention, indefinite for medico-legal | 3 days |
| B18 | Radiology licence and safety officer tracking against equipment master | 3 days |

### Phase 4: remaining modules (about 14 weeks)

Ordered by market prevalence, so the RFP-visible ones land first.

| # | Item | Est. | Prevalence |
|---|---|---|---|
| B19 | Management reporting and statutory returns, with a report builder | 2 to 3 wks | 15 of 15 |
| B20 | Casualty and emergency triage | 2 wks | 9 of 15 |
| B21 | Operating theatre depth: anaesthesia record, implants, recovery | 2 to 3 wks | 8 of 15 |
| B22 | Feedback and complaints | 3 days | 8 of 15 |
| B23 | Medical records department | 1 wk | 6 of 15 |
| B24 | Doctor payout and revenue share | 1 wk | 6 of 15 |
| B25 | Sterile supply, instrument sets and cycles | 1 wk | 5 of 15 |
| B26 | Care plans | 1 wk | Named in NABH |
| B27 | Infection control depth: surveillance, isolation, antibiogram | 2 wks | 1 of 15 |
| B28 | Mortuary | 3 days | 2 of 15 |
| B29 | Specialty verticals, dialysis first | 2 wks each | 7 of 15 |
| B30 | Clinical data warehouse and cohort queries | 2 wks | 0 of 15 |

---

## 4. Integrate track

Each of these is a module on the checklist and a fraction of the build cost. Several are the
only correct answer, because the thing being integrated with is somebody else's licence.

| # | Item | Est. | Why integrate |
|---|---|---|---|
| I1 | **Imaging archive over DICOM** | 2 to 3 wks | Do not build an archive. Speak DICOM to the one the hospital has, or ship an open-source engine as the appliance's archive. Modality worklist, query and retrieve, store |
| I2 | **Blood bank** | 1 to 2 wks | A separately licensed establishment under Schedule F Part XII-B with its own inspector and e-RaktKosh reporting. Integrate, never build |
| I3 | **Claims exchange and scheme claims** | 3 to 4 wks | The health claims exchange plus the scheme transaction system. This is what turns the records-only payer module into a real one |
| I4 | **Payment gateway** | 1 wk | Today the payment methods are labels with a manually keyed reference number |
| I5 | **General ledger** | 1 wk | Every Indian hospital already runs one. Export, do not rebuild |
| I6 | **Payroll export** | 1 wk | Already the standing ruling. Statutory columns stay empty by design |
| I7 | **Notifiable disease and tuberculosis reporting** | 1 to 2 wks | Two separate national systems, so two integrations |
| I8 | **Biomedical waste** | 1 wk | The treatment facility operates the barcode system and grants access |
| I9 | **Teleconsultation** | 1 wk | Commodity. Embed one |
| I10 | **Ambulance dispatch** | 1 wk | Usually third party |

---

## 5. What this actually adds up to

| Track | Work | Elapsed if started now |
|---|---|---|
| BUILD | About 32 weeks of solo effort across 30 items | Roughly **Q2 2027**, faster if the August parallelism holds |
| INTEGRATE | About 14 weeks across 10 items, plus counterparties | Overlaps the build track |
| REGISTER | 7 items, mostly queues | **12 to 18 months, and it has not started** |

Read those three rows together. The build track finishes first. The register track is the
one that decides when Orb can be sold, and today it is at zero with a legal-entity decision
still parked until post-pilot.

**So the sequencing rule for the whole roadmap is: never let the build track be the reason
the register track has not started.** Filing, incorporating and booking an audit are cheap in
hours and expensive in calendar. Code is the opposite.

---

## 6. Three things I would push back on

Stated once, and then the roadmap above stands as asked.

1. **Perfecting all 28 competes with the pilot, and the pilot is the thing that unlocks
   everything else.** NABH product certification wants deployments. Investors want a live
   site. A module nobody has used in a hospital is not yet a selling factor, it is a claim.
   The fastest route to "every module is a selling factor" probably runs through one hospital
   using nine modules, not through twenty-eight modules nobody has used.

2. **Breadth is the cheapest thing in this market to claim and the easiest to match.** Two
   obscure vendors already publish 51 and 38 modules. One advertises both "100+" and "42+"
   for the same product on two of its own pages. Nobody wins on module count. The things
   competitors cannot copy quickly are the closed-loop barcode medication administration that
   none of the fifteen ship, the governed order sets that one ships, and the hash-chained
   audit. Those deserve depth before the twenty-eighth module gets breadth.

3. **The three name-only modules are a bigger commercial risk than the seventeen absent
   ones.** An absent module is an honest gap a buyer can plan around. A tab labelled claims
   that records and never transmits is discovered during a pilot, and that conversation is
   much worse. Either connect them or hide them behind their flags before a site sees them.

None of that is a reason not to build everything. It is a reason to build it in the order
above rather than in the order of the gap list.

---

## 7. What I am least sure about

- **The register-track durations** come from the published process steps, not from anyone who
  has been through it. Certification queues are the kind of thing that takes twice as long as
  documented. Treat these as optimistic.
- **The build estimates** assume the August velocity holds. That run was mostly self-contained
  records modules with no external counterparty. The laboratory system and the imaging archive
  are not that shape, and I would not be surprised if either ran over.
- **The claims exchange and scheme integration** may need empanelment that Orb cannot hold on
  its own behalf, in which case it becomes a per-hospital integration rather than a product
  feature. Worth checking before promising it.
- **What replaces the current incentive corrigendum after September 2026** is unpublished, so
  the commercial urgency behind ABDM could change in either direction.
