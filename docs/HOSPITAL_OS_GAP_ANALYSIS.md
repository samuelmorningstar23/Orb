# What a hospital OS is expected to have, and where Orb stands

Written 2026-09-07. Two sweeps: an evidence-based inventory of Orb's own codebase, and
research into what Indian and global hospital systems ship, weighted to what an Indian
hospital CIO would require. Primary regulatory documents were fetched and read in full
rather than summarised second hand.

This is a gap analysis, not a roadmap. It says what exists, what does not, and which gaps
are commercial blockers rather than feature requests.

---

## 1. The three findings that change decisions

Everything else in this document is a list. These three are not.

### 1.1 ABDM v3 is a live procurement gate, and Orb is at zero

The current incentive scheme is DHIS Corrigendum 7, dated 9 April 2026. Two clauses matter:

- From **July 2026 onward, a Digital Solution Company that has not migrated all three
  milestones to ABDM v3 APIs earns nothing, and neither does the hospital using it**, until
  compliance lands.
- A **CERT-In empanelled Web Application Security Assessment (WASA)** is mandatory for
  incentives. Transactions before a valid WASA report are ineligible whatever the claim date.

Orb's ABDM service is 87 KB and its own header states there is no gateway, no HIP callback
URL, no HIU registration, no consent-manager subscription, no gateway token, no outbox, no
retry queue, no sandbox credential, and that Orb has not been onboarded to the National
Health Authority in any capacity. Every function is `record_*`. It returns
`"gateway_connected": False` literally, and does not validate the ABHA check digit.

So this is not a missing feature. **A hospital adopting Orb today would forfeit money it
currently earns.** That converts ABDM from a parity item into a disqualifier, and it is
compounded by the NABH dependency in 1.3.

Rates have already halved once (Corrigendum 6 to 7). Record linking is now Rs 10 per
KYC-verified ABHA-linked transaction for a diagnostic report or discharge summary, Rs 5 for
the lesser categories, of which the software company keeps Rs 5 and Rs 2.50. **Corrigendum 7
expires at the end of September 2026**, roughly three weeks from this writing, and its
replacement is not published. Any financial model leaning on DHIS should treat the rate as a
variable with seven revisions since January 2023.

### 1.2 Orb's deterioration detection is medical device software, and the deck decides its class

The final CDSCO guidance (Doc No. CDSCO/MD/GD/MDSW/01/2026, 92 pages) excludes plain
hospital information systems in section 5.2, then pulls them straight back in:

> If any HIS/CIS or LIS/IMS has any additional functions that allow its use for any medical
> purposes (e.g., image analysis/modification as an aid in diagnosis, quantification of
> physiological parameters for clinical decision-making, real-time patient monitoring, etc.),
> then it may be considered as a medical device.

Quantification of physiological parameters for clinical decision making, and real-time
patient monitoring, are both named. A NEWS2 score computed from vitals and surfaced as an
escalation alert is inside that sentence, not outside it. Lens is inside the image-analysis
clause as well.

Classification runs on a two-axis grid. "Drive clinical management" explicitly includes "to
triage or identify early signs of a disease or condition". So:

| How the same capability is described | Situation | Class | Licensed by |
|---|---|---|---|
| Triages or flags early signs of deterioration | Critical | **C** | CDSCO Central |
| Informs clinical management, aggregates and presents | Serious | **A** | State Authority |

CDSCO states risk class is "fundamentally based on the intended use", evidenced in
labelling, instructions for use, **websites and promotional material**. The deck and the
website are therefore regulatory artefacts. This is a positioning decision with a price
attached, and it should be made deliberately rather than discovered during a filing.

Related: non-device functions are not themselves regulated, but the manufacturer must
"submit clarification on the impact of non-device functions on the safety and effectiveness
of the MDSW function". The thinking-mode safety floor is exactly the sort of thing that
clarification would have to describe.

### 1.3 NABH has published a free functional specification, and nobody is using it

The **NABH Digital Health Standards for Hospitals, 2nd Edition (September 2025)**, 148
pages, is a separate voluntary accreditation with Silver, Gold and Platinum tiers. It
contains 182 objective elements across 8 chapters, of which 17 are Core and mandatory at
every tier.

Read as a product document rather than an accreditation scheme, it is the closest thing to a
government-blessed definition of what a hospital OS must do in India. Its chapters map
almost one to one onto module boundaries: access and continuity, care of patients,
medication management, digital infrastructure, digital operations, finance and procurement,
human resources, information management.

Specific obligations inside it that are build work, not paperwork:

- Generate and capture an ABHA and link it to the internal patient identifier, including for
  patients without phones
- Link **lab reports, radiology reports and images, and the discharge summary** to the
  patient's ABHA address
- Fetch a patient's past records generated at **other institutions** (the M3 capability)
- Register the facility in the Health Facility Registry and practitioners in the Healthcare
  Professionals Registry
- Exchange in FHIR, code with SNOMED CT or WHO ICD, tag lab results with LOINC
- A digital mechanism to **qualify and label a case as medico-legal**
- A mechanism to **restrict access to medico-legal and MTP reports** in the repository
- Digital capture of emergency codes and staff response times
- Drug-drug and food-drug interaction checking, narcotics per statutory requirement
- Encryption at rest **including cloud backup** and in transit, endpoint protection, audit
  trails on all core clinical applications, UPS and generator
- Disaster recovery and service continuity, access control including MFA

Orb already satisfies a meaningful fraction of the infrastructure chapters. It satisfies
almost none of the ABDM-linked ones.

Separately, **NABH Digital Health Standards for HIS/EMR Systems, 1st Edition (September
2024)** certifies the *product*, not the hospital. Two-year validity, published test cases,
a self-attestation route, and India was reportedly first in the world to publish one. For a
vendor with no reference sites this is the cheapest third-party credibility available and
should be priced out.

Note the dependency chain that results: **NABH certification needs operational deployments
plus ABDM M3, and ABDM M3 needs a gateway Orb has never connected to.** ABDM is on the
critical path to accreditation, not beside it.

---

## 2. Table stakes: what a CIO needs before piloting

Derived from Indian vendor module lists, the government's own eHospital 11-module
definition, NABH DHS, and Indian HMIS tender patterns. Status column is verified against the
codebase.

| # | Requirement | Orb status |
|---|---|---|
| 1 | OPD registration, appointments, queue, OP billing | **Partial.** Front desk and OPD visits built. Appointments have no doctor column, no slot master, no availability check |
| 2 | IPD, admission/discharge/transfer, live bed board | **Partial.** ADT built. **No bed master and no beds table**, beds are free-text strings |
| 3 | Billing across the Indian payer topology | **Built.** Tariff plans, package rates, deposits, refunds, cashier sessions, integer-paise arithmetic |
| 4 | Pharmacy with batch and expiry | **Built.** Strict dispensing state machine, lots, expiry, stock movements |
| 5 | LIS with analyser interfacing | **Absent as a category.** Orb receives results over HL7 and FHIR but has no specimen tracking, accession numbers, analyser interface, result authorisation or QC |
| 6 | RIS and PACS | **Absent.** Zero DICOM anywhere. Lens is an uploaded-image AI read, disclaimed and off by default in production |
| 7 | ABDM to M2 or M3 on v3 APIs | **Absent.** Records only, never onboarded. See 1.1 |
| 8 | CERT-In empanelled WASA report | **Absent.** No reference anywhere in the repo |
| 9 | Audit trail, encryption at rest and in transit, RBAC with MFA, tested DR | **Built and strong.** SHA-256 hash-chained audit, SQLCipher at rest, AES-256-GCM for PHI media, TOTP MFA, consistent backup |
| 10 | Migration from the incumbent, and an exit path | **Partial.** History import is complete server-side with 43 tests, but **no UI calls it** and nothing reads the provenance columns back |
| 11 | MLC flagging and restricted record classes (MLC, MTP, PCPNDT) | **Absent.** Medico-legal exists only as a retention-policy category |
| 12 | Discharge summary | **Built**, and see 3.3 |
| 13 | On-site support and uptime SLA | **Partial.** Ops health page exists and is explicitly "no remote support by design". Indian tenders routinely want five years of maintenance and stationed manpower |

---

## 3. Where Orb is genuinely ahead

This matters as much as the gap list, because it is what a feature-list comparison will
hide. Counts are out of fifteen Indian vendors surveyed.

### 3.1 Closed-loop medication administration: 0 of 15 Indian vendors have it

Indian systems stop at pharmacy dispensing. Orb runs prescribe, pharmacist verify, dispense,
and barcode-confirmed administration as one loop, with an allergy interlock and a safety
gate at administration time, and AI-charted medications landing as `REVIEW REQUIRED` until a
pharmacist clears them. Global suites treat this as a flagship capability. No surveyed
Indian vendor ships it.

### 3.2 Nursing is systematically under-built by incumbents

Only 6 of 15 vendors name nursing or a medication administration record at all. It is
usually a sub-bullet of ward management. For a product whose wedge is deterioration
detection, the incumbent surface here is thin enough to be genuine differentiation rather
than catch-up.

### 3.3 Discharge summary is the highest-value ABDM record and nobody markets it

Universal in practice, marketed by almost no vendor, and now jointly the top-rate ABDM
record type at Rs 10 per linked transaction. Orb generates it from voice through Scribe.
This is the rare case where the product advantage, the incentive scheme and the
accreditation requirement all point at the same artefact.

### 3.4 Others where Orb is in a small minority

| Capability | Indian vendors with it | Orb |
|---|---|---|
| Order sets as a governed library | 1 of 15 | 50 KB service, re-runs every safety gate |
| NABH quality reporting as a live engine | 2 of 15 | 102 KB service |
| Infection control | 1 of 15 | Sepsis bundle, incident categories |
| Duty roster | 1 of 15 | Built, with payroll handed off deliberately |
| Biomedical asset maintenance | 4 of 15 | 84 KB, preventive maintenance and calibration |

Two cautions on breadth. Module count is cheaply claimed: two obscure vendors publish 51 and
38 modules, well past Practo or Attune, and one vendor claims "100+" and "42+" for the same
product on two of its own pages. Breadth is not a moat. Second, the government's eHospital
list includes Dietary and **Laundry**, so a CIO's mental checklist may include things that
feel unserious.

---

## 4. Statutory registers Orb does not have

Verified absent by search. Each is a legal obligation with an inspector attached, not a
feature request.

| Obligation | Requirement |
|---|---|
| **Medico-legal cases** | Retained until final disposal of the case, potentially decades, including where only a notice has been received. NABH DHS requires digital MLC labelling and restricted access |
| **MTP** | Monthly Form II to the CMO, Form III admission register kept 5 years, the woman referenced only by serial number. Confidentiality breach carries up to 1 year imprisonment under the 2021 amendment |
| **PCPNDT** | Form F per ultrasound, incomplete entries themselves punishable, monthly reports before the 5th, images preserved 2 years |
| **Birth and death** | Centralised CRS portal since 1 October 2023, Form 4 for institutional deaths |
| **Notifiable disease** | IDSP via the IHIP platform in P and L formats. **Nikshay for TB is a separate mandatory system**, so two integrations |
| **Blood bank** | Licensed separately under Schedule F Part XII-B, with e-RaktKosh reporting. A regulated establishment, not a module |
| **Schedule H1** | Separate register with prescriber and patient details, kept 2 years from last entry |
| **NDPS** | Forms 3D, 3E, 3H for essential narcotics, kept 2 years |
| **Biomedical waste** | Daily register, monthly website updates, annual report, barcoded bags. Usually an integration with the treatment facility |
| **AERB** | eLORA licensing per radiology installation, radiological safety officer, licence display. Equipment master and expiry tracking are the software's part |

Orb's DPDP retention policy table already cites the right statutes, including the Drugs and
Cosmetics Rules and PCPNDT section 29, so the awareness exists. The registers themselves do
not. Retention is also not one number: it is a per-record-class policy with an indefinite
legal-hold state, which few systems model.

---

## 5. Absent entirely

Confirmed by search returning zero across the codebase.

**Whole categories:** laboratory information system, DICOM and picture archiving,
multi-site or multi-tenancy, conventional MIS and statutory reporting, care plans,
controlled-substance register, blood bank, casualty and emergency triage, medical records
department, dialysis, mortuary, ambulance, telemedicine, CSSD, laundry.

**Multi-site deserves separate mention.** No table carries a facility or tenant column. A
chain running five hospitals needs five independent Orbs with no shared patient index and no
consolidated reporting. Given the target buyer is a CIO, this constrains which accounts are
sellable at all.

**Present in name only.** These are honestly documented in-code, several with tests that
fail if the disclaimer wording is removed, which is unusually good engineering. The risk is
that they all default to visible, so a pilot site sees the tab and assumes the function.

| Module | What it actually does |
|---|---|
| ABDM and ABHA, 87 KB | Records only, no gateway |
| Payer, TPA and claims, 95 KB | Records only, nothing is transmitted to any payer |
| Terminology validation | 76 hand-seeded codes, and the bulk loader it points to does not exist |
| Drug interactions | 40 to 59 curated pairs, self-described as not comprehensive |
| PM-JAY | A payer-type enum value, no BIS, TMS or NHCX integration |

---

## 6. Competitive note

KLAS published Global Hospital EHR Market Share 2026 on 7 July 2026, covering 2025
decisions. Its Asia section headlines share growth for **Akhil Systems** and **Aosta**, with
Aosta selected in the largest decisions in the Asia and Oceania region. The vendors winning
Indian hospital EHR decisions are Indian ones. That independently supports the white-space
thesis and names two competitors not currently on the landscape watch list.

Epic shows no evidence of any Indian deployment, and its own country-specific page lists 11
countries without India. Oracle Health has modest presence concentrated in public health.
InterSystems TrakCare is the best-evidenced global suite in India, but the citations are from
2009 and 2011 and current status is unverified.

Incumbent web hygiene is poor and worth knowing when positioning: one competitor's domain
does not resolve at all, another serves an expired certificate, a third returns 403 on its
published address.

---

## 7. Suggested sequence

Ordered by whether the item blocks a sale, not by effort.

1. **Decide the CDSCO intended-use position and align the deck to it.** Costs nothing to
   decide, expensive to get wrong, and it gates how everything else may be described.
2. **ABDM M1 and M2 on v3 APIs, plus a WASA.** This is the difference between costing a
   hospital money and earning it one. It also unblocks NABH.
3. **Bed master and appointment slots.** Both are small, both are table stakes, and both are
   currently free text where a CIO expects a resource model.
4. **A UI for history import.** The hard part is built and tested. Without a screen it cannot
   be used on cutover day, which is the one day it exists for.
5. **MLC labelling and restricted record classes.** Cheap to build, legally expensive to
   omit, and explicitly named in NABH DHS.
6. **Lab report document ingestion.** No OCR library needed: Lens already does upload,
   encrypt at rest, decrypt in memory and post to a vision model. Point that spine at
   extraction, write to `observations`, and require human verification before anything
   becomes clinical fact. MedGemma 1.5 4B scores 91 macro F1 on exactly this task against
   the 27B's 76. See `docs/MODEL_BENCHMARKING.md` section 2b.
7. **Then the category gaps**, in whatever order the first pilot's hospital actually needs:
   LIS, RIS and PACS, blood bank, MIS reporting.

---

## 8. Uncertainty register

Stated so nobody repeats these as settled fact.

- Whether ABDM compliance is genuinely **mandatory** for PM-JAY empanelment. Widely asserted
  in secondary sources, no NHA primary document found. Treat as probable, not settled.
- NHCX adoption figures for 2026 and CGHS 2026 rate revisions. Secondary only, and the CGHS
  portal refused connection. Do not quote CGHS numbers without checking the portal.
- The NABH 6th Edition hospital standard's information-management chapter. The Digital Health
  standard was obtained in full, the 6th edition text was not.
- Whether TrakCare is still live at its cited Indian sites. Sources are 2009 and 2011.
- What replaces DHIS Corrigendum 7 after 30 September 2026. Not published.

A caution on sourcing: several top search results for CGHS, NABH and ABDM topics come from
the Gujarat competitor already on the quarterly re-scan list, which SEO-squats this exact
vocabulary. None of its pages were used here, and its numbers should be treated as marketing.

Primary documents that were read in full: NABH Digital Health Standards for Hospitals 2nd
Edition, the CDSCO Medical Device Software guidance, DHIS Corrigenda 6 and 7, and the ABDM
Milestone 3 sandbox specification.
