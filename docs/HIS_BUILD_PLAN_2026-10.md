# Whole-hospital-system build plan, October 2026

Written on 3 October 2026 after Samuel chose to build the whole-hospital-system gaps now. Six epics were each mapped against the code by one architect, attacked by an independent critic, and revised; a planner then merged them into one sequence. Everything new sits behind a feature flag that is off by default, so the Ward Record profile and the demos do not change. The approved sales plan (docs/fundraising/SALES_PLAN_2026-10.md) still sells the one-ward Ward Record and defers this work; this plan builds it dark.

## The plan in brief

Build plan for the six HIS epics: OPD and registration, ADT and bed master, single medication-order store, pharmacy inventory and GRN, LIS, and multi-site. Everything new sits behind a default-off flag, so the Ward Record profile and the demos do not change.

**Shape**
- 14 waves, 81 clusters, about 460 engineer-days scheduled.
- About 37 more engineer-days are held: 7 increments blocked by Samuel's held rulings, plus 3 LIS increments waiting on external inputs.
- The highest migration was checked in code: 47, vitals_carried_from (server/migrations.py:5494). New migrations 48 to 75 are pre-allocated, one owner per number.

**Three wave-1 layout changes that make disjoint clusters possible**
1. One opt-in flag system, built once. It registers every OPD, ADT, GRN and LIS flag, setting, permission constant and v1 grant up front. config.py, roles.py and shellConfig.tsx are then rarely touched again.
2. Migrations move to one file per migration under server/migrations_his/, picked up automatically by version number. migrations.py is edited only in wave 1.
3. Route packages are pre-created and registered in main.py in wave 1: adt, lab and pharmacy_store, plus the OPD, registration and schedule route files. New panels find their views and tile actions automatically from their folders (import.meta.glob). After that, clusters mostly add files instead of editing shared ones.

**Duplicates merged across the designs**
- The flag helpers proposed by all six epics become one. adt-0, grn-19, lis-2 and the role and frontend parts of opd-3 and adt-17 fold into wave 1.
- One users-role rebuild covers receptionist, ward_clerk and the three lab roles. Each role can only be created while its feature is on.
- adt-2 alone owns patient_movements; opd-1 no longer creates it.
- One readmission design: opd-17a, 17b and 17c absorb adt-18a and 18b. The one-active-admission index lands once.
- The ghost POST /api/vigil/patients is retired once, in wave 1.

**Added**
- multisite-8h1, 8h2 and 8h3: about 7.5 days to sweep the new HIS routes for facility scope. The multi-site architect could not have counted these routes.

**Order**
- Wave 1: platform. It touches no file a running session owns; config.py and main.py are limited to the flag block and /api/health, rebased after the CORS session.
- Waves 2 and 3: schema and core writers for OPD, ADT, GRN and LIS.
- Medication-order store code: waves 4 to 10, starting only after Samuel rules its architectural decisions.
- Multi-site: spine in wave 7, scope core in wave 8, then registry and sweeps in waves 11 to 14. These run after every other epic has stopped adding SQL. No other epic depends on multi-site, so the whole tail can be dropped.

**Rulings**
- Increments blocked by held rulings (R001, R002, R009, R010, W111, R023) are in held_for_samuel.
- New safety rulings raised by the architects are built to the architect's recommendation. Where the architect shipped a stricter interim default (LIS-R5, LIS-R6, GRN-RS7), that default stays until Samuel confirms. Their flags stay off until he confirms them.

**Pushback**
This is about 115 engineer-weeks at the sales plan's 4 engineer-days a week. It competes directly with the Ward Record, the only priced product. Every wave ends in a mergeable state, so Samuel can stop after any wave.

## Samuel's answers, 3 October 2026

- The 65 new safety rulings the architects raised: build each with the strictest option named, keep its flag off at every site, and confirm each with Samuel before anything goes live. The ten rulings already held (R001, R002, R009, R010, R011, W111, R023 to R026) stay held and unbuilt.
- Medication orders A1: new med_orders and med_order_events tables; medications stays as the ward view (strangler).
- Multi-site M1: one appliance per legal entity, facility_id on rows in the one encrypted database.
- The three new modules (ADT, pharmacy store, lab) default OFF, departing from the 11 August "every module on" ruling for these three only.
- Every other decision in the table below (A2 to A5, M2 to M6, X1 to X4) follows the recommendation.

## Decisions for Samuel before the architectural waves

| Decision | Options | Recommendation |
|---|---|---|
| Medication orders A1: where the authoritative medication order lives | (a) New med_orders and med_order_events tables become the record. medications stays as the ward projection (about 20 readers and 37 test files keep working). Helix rows are supply children, many per order. CPOE orders are the draft view. Nothing is renamed or rebuilt (strangler). (b) Rebuild medications into the order store. It is the FK parent of administrations and both override tables, so the rebuild runs outside the migration transaction. (c) Make helix_patient_med_orders the order. Wrong grain: one row is one dispense of one unit. | (a). Waves 4 to 10 assume it. If Samuel picks anything else, the medication-order clusters and opd-12 and grn-17 stop; nothing else depends on them. |
| Medication orders A2: rollout modes and what happens if lifecycle mode is switched off (MO-R9) | (a) Two flags. ORB_MED_ORDER_STORE (record mode): the chart, Sage and Scribe lanes write through, and legacy semantics are kept. ORB_MED_ORDER_LIFECYCLE (authoritative): sign, verify, hold, stop. Switching lifecycle off after use fails closed: held orders stay ungivable and linked CPOE orders become cancel-only. (b) One flag with a big-bang cutover. (c) Two flags, but held orders are released and CPOE orders unlinked when the flag goes off. | (a), with an operations note before any site turns lifecycle on. |
| Medication orders A3: how the bedside dose decision stays safe while two stores coexist | (a) Two invariants. I1: the verification fact stays on medications.verified_by, which every verify path writes; the store adds only 'held'. I2: every mutation of a linked row writes through the store, keyed on the link and not the flag. The eMAR check sits below the replay and discharge guards and is re-checked after the INSERT. (b) Once a row is adopted, the eMAR trusts only the store. | (a). A legacy writer can then never make a verified drug ungivable, and an offline replay never gets a 4xx. |
| Medication orders A4: supply rows and the ledger after erasure (MO-R12, MO-R13) | Supply: (a) the prescriber picks pharmacy, ward stock or patient's own on each order, defaulted per drug from the drug master later; (b) every signed formulary order gets a pharmacy supply row; (c) supply stays manual. Ledger after a patient's erasure: (a) deleted with the medication record, with tamper evidence kept in the hash-chained audit; (b) retained append-only, with a DELETE-abort trigger and an ERASURE_RETAINED_TABLES entry. | Supply (a); erasure (a). The build assumes both. Erasure (b) is a one-line registry change plus a trigger if Samuel prefers it. |
| Medication orders A5: how much to build now | (a) Day one (medorders-1 to 10) plus the follow-ons that need no held ruling: 11 (verify before first dose), 19 (authored schedule) and 20 (exact Sage binding). About 56 engineer-days. 12 to 18 stay held on R001, R002, R009, R010, W111 and W099. (b) Day one only, about 47 engineer-days. | (a) as scheduled. 11, 19 and 20 can be dropped without touching anything else. |
| Multi-site M1: tenancy (MS-R1) | (a) One appliance per legal entity, with a row-level facility_id in the one SQLCipher DB, one PAN across all facilities and no tenant_id. (b) One appliance per facility with a federated patient index. (c) Row-level, with a tenant_id for franchises. | (a). It keeps one DPDP data fiduciary, keeps CIO questionnaire 1.5 true, and gives consultants one login. facilities.uuid keeps federation open for later. |
| Multi-site M2: what switches isolation on | (a) Data: once more than one facilities row exists (rows are never deleted), facility scope is enforced whatever the flag says. A runtime fence answers 503 on any route not yet swept. The flag only gates growth. (b) The ORB_MULTI_SITE flag itself. | (a). A flag-off restore of a two-facility backup cannot leak. The fence makes an unfinished screen an outage, not a disclosure. |
| Multi-site M3: department, ward and bed codes (MS-R4) | (a) Unique across the group, each owned by one facility. Ward-scoped staff are then isolated without touching the 116 user-taking scope call sites. (b) Duplicates allowed, with every comparison facility-qualified. This roughly doubles the multi-site work and adds facility_id to the OPD token index. | (a). The ADT bed master already enforces case-insensitive unique codes. |
| Multi-site M4: chart visibility across facilities (MS-R2) and attribution | (a) The whole chart is visible. The facility is derived through the patient's admissions, under an invariant that refuses cross-facility re-admission and department moves until encounter attribution exists. (b) This facility's encounters only, with allergies, active medications and problems group-wide. Needs admission_id on about 20 clinical tables: 10 to 15 days in the MPI epic, not in this plan. (c) This facility's encounters only. | (a). Never filter allergies, active medications or problems by facility. |
| Multi-site M5: build order against ADT, and who owns the departments master | (a) ADT first. adt-1 (wave 2) creates wards with a facility_code label 'MAIN'. multisite-1 (wave 7) creates a departments master seeded from users, site.toml, admissions and ADT wards. multisite-4 adds wards.facility_id. (b) multisite-1 before adt-1, as its architect asked. That pulls the architectural epic into wave 2, ahead of Samuel's decisions. | (a). Multi-site stays behind its decisions and off the critical path, and the code-uniqueness contract still holds because every ward is 'MAIN' until a second facility exists. |
| Multi-site M6: audit attribution and department-less staff (MS-R5) | Audit: (a) the facility goes into hashed metadata_json, only when multi-facility data exists. History before the first expansion belongs to facility 1. The index is built by a maintenance CLI. (b) A new audit_logs column, which bumps the chain and risks the int-versus-TEXT target_id trap. Department-less admin, pharmacist and cashier: (a) the facility of the patient's latest encounter; (b) any encounter ever; (c) active, or within N days. | Audit (a); staff (a). |
| Cross-epic X1: one default-off flag system for all HIS work | (a) One config._opt_in helper. FEATURE_FLAGS is published as 'features' on /api/health. OPT_IN_MODULES (adt, pharmacy-store, lab) sit inside 'modules', default off. Client defaults fail toward hidden. This departs from the 2026-08-11 'every module defaults ON' ruling for these three modules only, and test_money and the shell pins are amended to say so. (b) Each epic builds its own; the six designs proposed five different helper names. | (a), built once in wave 1 with every OPD, ADT, GRN and LIS flag and setting registered. The medication-order and multi-site flags are added in their own first waves. |
| Cross-epic X2: migration file layout | (a) One file per migration under server/migrations_his/, collected by version and refused on a duplicate. migrations.py is edited only in wave 1. (b) Keep every migration in migrations.py with one owning cluster per wave. That caps schema work at about 8 days a wave and adds about four waves. | (a). The existing runner already tolerates gaps and out-of-order landing, so the change is a small collection hook. |
| Cross-epic X3: code layout so several clusters can build one epic in the same wave | (a) Route packages (routes/adt, routes/lab, routes/pharmacy_store, routes/medorders) and service packages (services/adt, services/medorders), pre-created and registered in main.py once. New panels find their views and tile actions with import.meta.glob, the pattern shellConfig already uses. (b) One route file and one service file per epic, so each epic gets one cluster per wave. | (a). Without it, OPD, ADT, GRN and LIS each run one cluster a wave and the plan grows by several waves. |
| Cross-epic X4: roles, the patients rebuild, and one readmission design | (a) One users-role rebuild (migration 48) for receptionist, ward_clerk, lab_technician, pathologist and phlebotomist, each creatable only while its feature is on. One patients rebuild (migration 52), adding only 'registered'. One readmission design: opd-17a/17b/17c absorb adt-18a/18b, and the one-active-admission index lands once in migration 65. This shared identity core is also the Ward Record's returning-patient path. (b) A rebuild per epic and two readmission paths. | (a). Every users or patients rebuild is an FK-parent rebuild that commits the runner's transaction, so fewer is safer. A second writer of admission_service would also compete with the running Scribe work. |

## Held for Samuel

- medorders-12 (verbal and telephone orders with read-back and countersign): blocked by held R010.
- medorders-13 (Scribe-extracted medicines as drafts): blocked by held W111.
- medorders-14 (stat order completes on its first given dose): blocked by held R001.
- medorders-15 (pharmacy substitution as an amendment the prescriber accepts): blocked by held R002. Until then, day one refuses any verification binding that is not the same ingredient.
- medorders-16 (Helix approve and Relay confirm lanes onto the store): blocked by held R010 and the open W099 (wire or retire).
- medorders-17 (medication reconciliation at admission, transfer and discharge, W105): its acknowledgement before the discharge summary is a discharge gate, which is held R009. It also needs MO-R7. Needs one migration number when released.
- medorders-18 (retire the legacy medication write paths): depends on medorders-16, so it is blocked by R010.
- lis-17 (open lab critical shown on the Vigil board and chart): build-blocked on held R023 (intended-use statement).
- multisite-20 (inter-facility transfer workflow): blocked by held R009 and by MS-R3. MS-R3 stays at option (c), refuse, until R009 is ruled.
- Go-live only (the code is built in waves 7 and 8 behind default-off switches): the LIS critical notification lis-15, the ward critical card lis-16 and interfaced criticals lis-22. LIS-R20 asks whether they sit inside held R023, so they stay switched off until Samuel answers.
- Not decided, not scheduled: ADT-S6 (whether host-ward nurses can open a boarder's chart). It changes PATIENT_DEPARTMENT_SQL and its Sage twin. Until Samuel decides, the boarder acknowledgement and the case-room line built in adt-5 and adt-6a stand in.
- Not decided, not scheduled: MO-R5 (prescriber-only acknowledgement of a severe allergy on every lane). It overlaps held R010 and R011, so only the new sign step gets prescriber-only acknowledgement, as a side effect of its design.
- R024 and R026 are not decided anywhere. grn-4, grn-6, grn-14, medorders-5b and medorders-8 only wait for the Relay work in progress to land, and either R024 answer works. multisite-10 leaves the Sage bed matcher untouched.
- External inputs Samuel must get from a site: lis-21 needs a real site ORU sample (W108). lis-26 and lis-27 (ASTM framer, transport and host query) need the site's analyser list and protocol documents. opd-20 is built in W9-G, but no ICD-10 release is loaded until counsel confirms the licence (OPD-N11).
- Go-live confirmations: the new safety rulings below were applied as build defaults, and their flags stay off at every site until Samuel confirms them. OPD-S1 to S8; ADT-S1, S2, S3, S4, S5, S7, S8, S9; MO-R1, R2, R3, R6, R8, R10; GRN-RS1, RS2, RS3, RS5, RS6, RS7; LIS-R1 to R10, R16 to R19, R21, R22; MS-R2, R4, R8, R11, R13, R14. Where the architect shipped a stricter interim (LIS-R5 two people, LIS-R6 no back-dating, GRN-RS7 register disabled until a mode is chosen), the strict default stays until he confirms.

## Product rulings applied

- How rulings were applied: non-safety rulings follow the architect's recommendation. New safety rulings raised by the architects also follow it, as build defaults only; their flags stay off until Samuel confirms them (listed in held_for_samuel). Held rulings R001, R002, R009, R010, R011, W111, R023, R024, R025, R026 are not decided anywhere in this plan.
- OPD-S1 (a): to attach a visit or an admission to an existing record, the server checks two identifiers the patient states and answers only 'match' or 'no match'. The stored date of birth is never shown. Rate-limited and audited.
- OPD-S2 (a): a probable duplicate gives a warning, needs a reason and opens a worklist item. Exact UHID or active ABHA is a hard refusal. ORB_DUPLICATE_MODE defaults to warn.
- OPD-S3 (a) and OPD-S4 (a): OPD vitals are never copied into vitals, and OPD prescriptions never reach the inpatient chart at conversion. Both are listed with their times for reconciliation.
- OPD-S5 (a): no allergy field at the front desk. OPD-S6 (a): no automatic prompt on OPD vitals.
- OPD-S7 (a): signing a consultation, and writing an OPD prescription, need an allergy review: allergies charted, 'none known', or 'unable to obtain' with a reason.
- OPD-S8 (a): readmission discontinues previous-stay active and prn medications in the same transaction and lists them for re-prescribing.
- OPD-N1: hospital-number mode by default. Orb mode uses a Verhoeff check digit, never resets yearly, and is refused until the series is configured.
- OPD-N2: the OPD record view opens for 7 days to the clinic's department. It shows identity, allergies, problems, OPD-linked results and all clinics' signed consultations. Self-registered visits count, flagged and audited.
- OPD-N3: receptionist role holds registration.read and registration.write. consent.record is granted only after counsel agrees. Admin gets registration.read only; money stays with the cashier.
- OPD-N4: hospital-wide search returns minimal fields, needs at least 3 characters, returns at most 20 results and audits matched ids. OPD-N5 (R018): DOB is optional; a stated age is stored with its date.
- OPD-N6: a site setting, defaulting to charging at completion. Follow-up visits use their own OPD-FOLLOWUP code. Cancel and LWBS void a pending prepaid line.
- OPD-N7: the token board shows token, doctor and room only. OPD-N8: a visit may complete without a signed note if a reason is given. OPD-N9: queue priorities come from a site list. OPD-N10: ID document type plus last 4 digits only.
- OPD-N11: diagnoses are free text with an optional code; counsel confirms the ICD-10 licence before any load. OPD-N12 (b): one queue read audit per user per visible visit set per 10 minutes.
- ADT-S1 (a): an occupied bed is refused, and 'add an extra bed' creates a temporary bed with its own label. This replaces the design's interim shared-bed-with-reason.
- ADT-S2 (c): beds awaiting a terminal clean are blocked unless a named person authorises. Standard-clean and out-of-service beds are allowed with a reason. Wards that do not track cleaning raise no warning.
- ADT-S3: death, LAMA and DAMA are doctor-only, with read-back. A nurse or clerk records the physical departure only after a doctor's discharge. The bed manager closes an absconded stay after 24 h. In the Ward Record the clerk records 'left the ward'.
- ADT-S4 (a): patients on leave or absconded stay on boards with a 'Not on ward' chip. Obs and medication due times and escalation pause and resume; nothing is closed.
- ADT-S5 (a): a clerk admission marks clinical fields 'Not yet assessed', never NKDA, and the board flags 'admission assessment due'.
- ADT-S7: doctors and nurses record leave and absconded. Nurses get a read-only board for their own ward.
- ADT-S8 (b): a clerk may change the treating department only to carry out a transfer that names the ordering doctor.
- ADT-S9 (a): after readmission, previous-stay vitals show only with their date and a 'previous stay' label, never as the latest. Sage memory is archived and restarted, and the case room gets a dividing line.
- ADT-S6 is not applied: the scope change is outside these epics.
- F104: warn about unlisted wards and beds until the bed manager marks setup complete, then strict per site. ADT-P3: back-time up to 72 h. ADT-P4: report both occupancy methods. ADT-P5: capacity comes from the register for complete wards. ADT-P6: bed held on leave, rent per site setting, doctor prompted after 48 h. ADT-P9: reservations expire after 120 min (24 h for elective and post-op).
- ADT-P8: only the portal login deactivation is built (restored by reopen) and no discharge link is issued. Stopping diet orders and making the Relay room read-only are not scheduled. ADT-P2 (Ward Record turns ORB_ADT on) is noted, not applied: the Ward Record is outside this plan.
- MO-R1 (a): every linked order needs pharmacist verification before its first dose. Stat and urgent first doses are allowed and flagged.
- MO-R2 (a): only the prescriber can hold, resume, stop or cancel a live order. MO-R3 (a): PRN orders can be given, and screening includes PRN, held and signed CPOE orders. MO-R4 (a): medication.verify for pharmacists only.
- MO-R6 (b): a same-ingredient duplicate needs acknowledgement with a reason. MO-R8 (a): dose times come from a signed site_config file, with no built-in default.
- MO-R9 (a): switching the lifecycle flag off fails closed. MO-R10 (a): unsigned drafts are never auto-cancelled; drafts older than 24 h rise in the to-sign queue. MO-R11 (a): non-formulary free text is allowed, with same-ingredient binding at verification.
- MO-R12 (a): the prescriber picks the supply source per order. MO-R13 (a): the ledger is deleted with the record on erasure. MO-R7 is not applied: it is bound to held medorders-17.
- GRN-RS1 (b): a new drug is a draft until a second pharmacist confirms it. Identity edits on drugs with orders or stock are refused (retire and replace).
- GRN-RS2 (b): a different pharmacist releases quarantine, with a reason; recalled and NSQ are terminal. GRN-RS3 (b): only sealed, in-date, non-cold-chain, non-NDPS unit packs are restocked after inspection.
- GRN-RS5 (b): ward and crash-cart stock comes from a per-store approved list that starts from the ISMP high-alert bar, and an eMAR order is still required. The ward-stock switch stays off until confirmed.
- GRN-RS6 (b): expiry is typed twice at receipt. GRN-RS7: both failure modes are built; the register stays disabled until a mode is confirmed. GRN-RS4 is not built: no increment exists for it.
- GRN-R7: the pharmacist holds pharmacy_store read, write and ndps; admin holds read and approve. GRN-R8 (a): nurses get department-scoped ward stock, indent and return grants. GRN-R9 (a): a pinned pharmacy-desk tab exception.
- GRN-R10: vendors need a valid drug licence, with a warning 30 days before expiry. GRN-R11: separate document series per pharmacy document type. GRN-R12: lot cost; billing at batch MRP, falling back to the tariff.
- GRN-R13: the pharmacist enters the dispense quantity, bounded. GRN-R14: narcotics are received into the vault and dispensed from it, with a second-user PIN witness; every variance drafts an incident; no NDPS stock in seeds.
- GRN-R15: ward stock is topped up by count until grn-17. GRN-R16: 90-day near-expiry window and 90-day minimum shelf life at receipt. GRN-R17: one dispensing store per site. GRN-R18: unclassified drugs are refused for purchase and receipt. GRN-R19: the ledger has a single writer; triggers only on the NDPS register. GRN-R20 (b): a two-person downgrade, refused while vault stock exists.
- LIS-R1 (a) and LIS-R2 (a): no critical limits and no reference ranges ship; the pathologist approves them. LIS-R3: three tiers, closed by a phone log or an in-app read-back, never by a Relay post.
- LIS-R4 (a): a critical is communicated at technical validation, marked preliminary. LIS-R5: two people by default (strict interim); one signatory doing every step is a site setting.
- LIS-R6: back-dating is refused until confirmed; afterwards up to 2 h with a reason. LIS-R7 (a): no DOB means no range and no flag. LIS-R8 (a): no autoverification. LIS-R9 (a): a delta check needs a reason before validation. LIS-R10 (a): every amendment raises a notice.
- LIS-R11 (c): portal lab reports off by default; shown after a delay or clinician release, with no colour. LIS-R12: three lab roles. LIS-R13: 12-digit accession with a Damm check digit. LIS-R14 (a): HL7 now, ASTM when a site sends documents. LIS-R15: browser printing to thermal printers.
- LIS-R16 (a): partial panels release analyte by analyte. LIS-R17 (a): only lab roles reconcile quarantined results. LIS-R18: ketones, VBG and ABG are bedside tests (ward_poct). LIS-R19 (c): post-discharge criticals go to the last ward's card and the lab phones. LIS-R21 (a): switching the module off drains open work. LIS-R22 (a): interfaced criticals off by default. LIS-R23 (a): no shadow lab lane.
- MS-R1 (a), MS-R2 (a), MS-R4 (a), MS-R5 (a), MS-R6 (a), MS-R7 (a) with separate accounts for the first go-live, MS-R9 (a), MS-R11 (a), MS-R12 (a), MS-R13 (a), MS-R14 (a).
- MS-R8: one timezone. One shared escalation ladder only if every facility's clinical lead signs it; per-facility ladders (about 3 more days) are not scheduled. MS-R3 stays at (c), refuse, because of held R009.
- R029 (4 October 2026, outside this plan's rulings, applies to W6-B): Bridge is for the admission only. The login works while the patient is admitted and until the end of the hospital-local day of their latest discharge (services/patient_portal_access, judged from the record on every request, sign-in and socket connect), so a readmitted file opens again with no switch to flip. When W6-B readmits into the same file it must also (1) scope Bridge to the current stay: /api/bridge/medications reads every stay's medicines, and the discharge card reads the latest summary, so a readmitted patient would see the previous stay's drugs as current; (2) issue a fresh one-time Bridge password at readmission, since the first one was handed over at the earlier stay; (3) keep portal_access's 'latest discharged_at' rule pointing at the stay that just ended once discharge_summaries carries admission_id.

## Waves

### Wave 1

Shared platform: one default-off flag system for all six epics, one users-role rebuild, per-file migration discovery, pre-created route packages, plus two fixes that need no schema. Touches no file a running session owns (config.py and main.py: flag block and /api/health only, rebased after the CORS session). Hot files this wave: config.py, main.py, migrations.py, roles.py, permissions.py owned by W1-A; shellConfig.tsx owned by W1-B; src/api/client.ts never edited.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W1-A Server platform spine | his-1 (new) opt-in flag platform: config._opt_in; FEATURE_FLAGS (registration, opd, adt_strict_locations, pharmacy_ward_stock, ndps_register with its refusal without ORB_PHARMACY_NDPS_ON_FAILURE, lab analyser_listener, result_quarantine, result_corrections, oru_order_link, lab_critical_interfaced, lab_portal_reports) published as 'features' on /api/health; OPT_IN_MODULES adt, pharmacy-store, lab merged into HOSPITAL_MODULE_ENV and enabled_modules, default off; every OPD, ADT, GRN and LIS setting with its default; test_money pin becomes 'non-opt-in ON, opt-in OFF' (absorbs the flag parts of opd-1, adt-0, grn-2, lis-1), his-1 (cont.) migration discovery: server/migrations_his/m00NN_<name>.py collected by version, refused at import on a duplicate or a filename/version mismatch, merged into the sorted MIGRATIONS run, his-1 (cont.) route skeletons registered once in main.py: routes/adt, routes/lab, routes/pharmacy_store packages (one empty sub-router per concern) and opd_routes, registration_routes, schedule_routes, each 404 while its flag is off, his-1 (cont.) pre-registration with a table-exists guard: retention sources for every planned OPD, LIS and GRN PHI table and ERASURE_RETAINED_TABLES for live ndps_register rows; fail-closed audit categories patient_identity, patient_movement, clinical_config, his-2 (new) migration 48 users_role_batch (receptionist, ward_clerk, lab_technician, pathologist, phlebotomist) with LATEST_USERS_ROLE_CHECK; roles.ROLE_FEATURE map; ward_clerk in DEPARTMENT_REQUIRED_ROLES; permission constants and v1 grants for OPD, ADT, GRN, LIS as listed under product rulings; admin_users_routes refuses a role whose feature is off (absorbs the role parts of opd-3, adt-17, lis-2 and the grant part of grn-19), grn-1 migration 49 helix_tables_in_runner | 52 files | 2 | 7.5 days. Migration 48 is an FK-parent rebuild of users: run it on a backup_db.py copy on the isolated verify stack first. No epic may add a users column (it would abort the next role rebuild). Every grant is inert because every new route 404s while its flag is off. The SALES Ward Record profile sees only the role CHECK widening. |
| W1-B Client platform shell | his-3 (new) client opt-in platform: OPT_IN_MODULE_IDS with DEFAULT_MODULE_FLAGS derived (opt-in false, also after a failed or non-OK health probe); DEFAULT_FEATURE_FLAGS all false with currentFeatureFlags(); DIAGNOSTIC_MODULE_IDS=['lab'] excluded from the MONEY derivation; pinned PHARMACY_DESK_MODULE_IDS=['pharmacy-store'] exception; ModuleOffNotice; shellConfig.test default-ON pins amended (absorbs adt-0, lis-1, grn-2 and grn-19 shell parts), his-4 (new) tabs, rails, roles: module tabs 'adt' (Beds), 'pharmacy-store', 'lab' with panel paths not yet present (not installed); 'opd' feature tab on doctor and clinician rails; ROLE_CONFIG for receptionist (Front Desk), ward_clerk (Beds) and the three lab roles, each with the module-off notice; pharmacist rail entry for pharmacy-store; roles.ts mirror of every new constant and grant; App.tsx UserRole and the /display/opd kiosk route with a stub page; UsersPanel hides roles whose feature is off; en and hi labels (absorbs the frontend parts of opd-3 and adt-17) | 12 files | 0 | 4 days. Ids, permission strings and grants must match W1-A exactly (contract in this plan); the CLINICAL_MODULE_IDS ['dietary'] pin and the money-adjacency test stay as they are apart from the one pinned pharmacy-desk exception. Contrast gate, no em dashes. |
| W1-C Procurement cores and Pharmacy Store shell | grn-20 (part a): split each procurement write into a connection-taking _<name>_core(db, ...) plus the unchanged public wrapper; the expiry 'today' becomes a parameter; pure refactor, grn-2 (remainder): PharmacyStorePanel shell that discovers views from ./pharmacyStore/*View.tsx with an honest empty state; src/api/pharmacyStore/index.ts | 7 files | 0 | 3 days. All 75 procurement tests must pass unchanged; stores procurement is ON by default everywhere. |
| W1-D Observation reader parity and ghost route retirement | lis-5 (part a): services/lab_observations.py (void and superseded filters, julianday(COALESCE(observed_at, recorded_at)) DESC, id DESC, previous_result, display_value) adopted by the chart lab query, downtime pack, Tier 1 creatinine lane and timeline; guard test covering FROM/JOIN and from_sql strings; labTrends passes simulated for observation points, opd-1 (part g): retire the ghost POST /api/vigil/patients with a 410 naming the admission door; soak_test.py and test_patient_name_rule.py move to /api/admissions/admit (also covers adt-5's legacy-route item) | 12 files | 0 | 3 days. Both changes are ungated on purpose: the Tier 1 lane stops reading voided and simulated creatinine (a parity defect fix), and the ghost route stops creating admission-less patients. Samuel can ask for the Tier 1 part to sit behind ORB_LAB instead. |

### Wave 2

Schema foundations for OPD, ADT, GRN and LIS and the first services that need only that schema. Migrations 50 to 57. Hot files this wave: main.py owned by W2-D (lab lifespan hook only); permissions.py owned by W2-E; config.py, roles.py, shellConfig.tsx, migrations.py untouched (new migration files have one owner each).

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W2-A Front desk schema, movement ledger and the registered status | opd-1 (schema): migration 50 registration_opd_foundation (frozen opd_visits DDL and new columns, admissions.opd_visit_id, orders.opd_visit_id, document_number_series resets_yearly and check_digit with an unconfigured UHID row, patient_demographics, append-only patient_identity_changes, patient_duplicate_candidates, patient_match_keys, opd_display_boards); ensure_frontdesk_schema kept in step; patient_movements left to adt-2, adt-2: migration 51 adt_movement_ledger (numbered patient_movements, old shape rebuilt with explicit rowid copy, vocabulary trigger, id columns and indexes); frontdesk_service imports the DDL; billing _admission_moves reads move types only, opd-2: migration 52 patients_status_registered (FK-parent rebuild through _atomic_table_rebuild, frozen column list, database.SCHEMA parity); never-admitted rows excluded from the shadow scorecard and NABH inpatient-days | 11 files | 3 | 7 days. Migration 52 runs on every install including the Ward Record: verified backup first, dry run on a dev DB copy, row count and foreign_key_check assertions. The shadow-instrumentation owner reviews the scorecard change (pilot evidence instrument). The 36 frontdesk tests pass unchanged. |
| W2-B ADT bed master | adt-1: migration 53 adt_bed_master (wards with a 'MAIN' facility_code label, ward_rooms, bed_classes, bed_master_events, housekeeping_units columns also in HOUSEKEEPING_TABLES and the lazy ALTER); housekeeping_service.register_unit_on; bed_master_service; Bed setup API in routes/adt/master.py; housekeeping register refuses bed units with ORB_ADT on | 6 files | 1 | 4 days. Permission constants, router registration and roles.ts mirror were done in wave 1. Ward codes are immutable once referenced and are unique case-insensitively, which already satisfies multi-site MS-R4 while every ward is 'MAIN'. |
| W2-C Pharmacy store schema and domain guards | grn-3: migration 54 pharmacy_store_schema (no CHECK on columns added to Helix tables, _ref naming, NDPS register with lane and triggers, ledger backfill with opening rows and seq); reseed wiring (EXTRA_TABLES_TO_CLEAR, ndps_register conditional predicate), grn-20 (part b): domain guards in every id-taking core, GRN domain from its PO, store master partition, vendor core, pharmacy document series | 7 files | 1 | 6.5 days. Legacy tmp_path DB test: SUM(ledger) equals every lot quantity afterwards; the inventory_items is_pharmacy CHECK still refuses 1. |
| W2-D LIS module state, contract and observation links | lis-1 (remainder): services/lab_module.py with on, draining and off state computed at startup and cached; compliance drain check; require_lab_module(allow_draining) on routes/lab; main.py lifespan calls lab_module.startup/shutdown (the only main.py edit LIS ever needs); LabPanel shell discovering ./lab/*View.tsx; src/api/lab/index.ts, lis-3: migration 55 lab_contract; lab_master_service; starter catalogue (ketones, VBG, ABG as ward_poct, no ranges or limits) loaded and registered in terminology only with ORB_LAB on; cutover events, lis-5 (part b): migration 56 observations_lab_links; chart query selects lab_result_id; labTrends takes status from the lab's own flag for LIS rows | 19 files | 2 | 6 days. /api/health never opens a DB connection for module_states. Terminology row count unchanged with ORB_LAB off. |
| W2-E Registration desk dependency and clinic sessions | opd-3 (remainder): permissions.require_registration_desk(mode) with audited denial; frontdesk_routes moves OPD reads, the doctors list, register and cancel onto it; transfers keep require_frontdesk_write, opd-15: migration 57 clinic_sessions; clinic_schedule_service and schedule_routes; Clinic timings view; FrontDeskPanel learns to discover ./frontdesk/*View.tsx when the registration flag is on | 10 files | 1 | 5 days. Receptionist: chart routes 404 or 403, transfers 403, payments 403. |

### Wave 3

Core writers: the opt-in OPD scope arm, the single ADT location writer and end of stay, the single Helix stock writer, LIS order intake, the unmatched-results queue. Migrations 58 to 60. Gate: the Relay agentic-confirm (R024) session has merged before W3-C starts. Hot files: permissions.py owned by W3-A; no other hot file touched.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W3-A OPD visit scope arm | opd-7: permissions.patient_in_opd_scope reached only through require_patient_access/enforce_patient_scope(opd_arm=True) on a pinned whitelist; opd_registration_allowed allows any patient with no active admission in OPD mode; create_opd_visit audits in-transaction plus 'opd_scope_opened' (self_registered flag); GET /api/opd/patients/{id}/record via new opd_service | 8 files | 0 | 4.5 days. patient_in_scope, scope_patient_ids_sql and PATIENT_DEPARTMENT_SQL are unchanged, so the Sage parity test and Sage provenance are untouched. OPD-N2 applied (7 days, extent ii, self-registered counts and is flagged). |
| W3-B ADT writer core and end of stay | adt-3: migration 58 adt_admission_location (ward_id, bed_id, presence and discharge_type columns with vocabulary triggers, partial unique bed lock); services/adt package with core.py (current_location, record_movement_on, locate_admission_on); reconciliation, link, register-complete, rename and retire guards, adt-8a: migration 59 adt_discharge_dispositions; _close_admission_on extracted from discharge_patient; end_stay with server read-back (confirm_mrn), de-duplicated dispositions, discharge_preconditions returning [] on every door; roster close through end_stay when on | 14 files | 2 | 8 days. The admissions-trigger note lives in the migration file, not database.py. Open escalation chains are listed at end of stay, never closed (R009 held). ADT-P8 applied for the portal login only (deactivated on death, restored by reopen). Flag-off discharge snapshot identical. |
| W3-C Helix single stock writer and pharmacy masters | grn-4: pharmacy_stock_service as the only writer of lots and movements (dispensable predicate, FEFO, relative decrement, seq, hook registry for NDPS); transition_order_status in db_session(immediate=True) without the in-body commit; in-transaction clinical_access helix_dispense_recorded; seeds open lots through the service; AST single-writer guard, grn-5: pharmacy_master_service and routes/pharmacy_store/masters.py: drug commercial and regulatory fields, two-person classification downgrade, pharmacy stores with legacy-lot adoption, par levels, vendors and drug licences; loader refuses identity rewrites of in-use rows; MastersView | 17 files | 0 | 8 days. Gate: R024 session merged; rebase on insert_order; test_agentic_confirm_race.py untouched. Always-on change: every dispense now fails closed on an audit outage. Later GRN increments put their logic in their own modules and call post_movement; only grn-8, grn-13, grn-14 and grn-17 edit pharmacy_stock_service, each in a different wave. |
| W3-D LIS order intake and setup screens | lis-6: post-commit intake hook on activation plus ensure_intake sweep from lab_module.startup, cutover and ward_poct skip, panel explosion, specimen planning, accession (hospital-local day, Damm, loud overflow), lis-4: lab Setup view (tests, panels, ranges, critical limits, rejection reasons) with loud 'not configured' counts | 12 files | 0 | 7 days. LIS-R13 and LIS-R18 applied. Orders activated before the cutover are never intaken. |
| W3-E Unmatched results queue and corrected results | lis-19: migration 60 result_quarantine; unresolved results to the queue with ORB_RESULT_QUARANTINE on; attach and discard need ingest.reconcile with two-identifier confirmation; admin sees list and counts only; ReconcileView discovered by LabPanel, lis-20: external OBX 'C' supersedes the prior final for the same patient, code, sub-id and filler or sender placer, never the generated group id | 12 files | 1 | 5.5 days. LIS-R17 applied (lab roles only). test_ingestion_alert_parity stays green. ingestion_service.py is then touched serially in waves 7, 8 and 10. |

### Wave 4

First usable slices behind their flags (registration, bed board, pharmacy purchasing and stock views, LIS guard and collection, OPD queue and consultation record) and the medication-order store foundation. Gate: Samuel has ruled the medication-order architectural decisions before W4-E starts; Bridge session merged before W4-B; CORS session merged before W4-F. Migrations 61 and 62. Hot files: config.py and main.py owned by W4-E.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W4-A Registration and duplicate detection | opd-4: registration_service, patient_matching (normalisers, keys, self-healing sync_keys), registration_routes; one immediate transaction for patient, demographics, match keys, unverified ABHA placeholder via abdm_service.record_abha_on and the patient_identity audit; UHID hospital or orb mode (Verhoeff, refused until configured); minimal-field hospital-wide search; check_identity oracle with rate limit, opd-5: matcher bands, hard matches on UHID and active ABHA, server-enforced duplicate mode, worklist and resolve | 9 files | 0 | 8 days. OPD-S2 applied as warn-with-reason (default ORB_DUPLICATE_MODE=warn). Measure sync_keys plus match on 100k synthetic patients in SQLCipher. |
| W4-B Bed board, reopen and stay-time helpers | adt-4a: bed_board with the full precedence (occupancy and reservation above out_of_service, ledger-derived needs_cleaning, 'cleaning not tracked'); awaiting and unlinked lists; BedBoardPanel discovering ./adt/*View.tsx and tile actions from src/components/adt/actions/*Action.tsx, adt-8b: reopen a summary-less end recorded in error (correction row, voided disposition), adt-8c: services/stay_times.py adopted by forecast, impact, NABH readmission, the bridge discharge view, the /discharged list and the housekeeping sweep (keyed on the ended admission's bed) | 22 files | 0 | 8 days. The board renders server status words generically, so later presence and reservation states need no BoardView edit. The m15 note correction goes into the stay_times docstring (migrations.py is not edited). Gate: Bridge session merged (bridge_routes is next to its file). |
| W4-C Pharmacy purchasing and stock views | grn-7: pharmacy_purchasing_service (PO create, submit, approve, issue, cancel through the cores with in-transaction audit; licence and classification checks; reorder suggestions to draft PO); PurchasingView, grn-9: stock, paged ledger, reorder and near-expiry reports in a new pharmacy_stock_reports.py; Formulary uses the server reorder level when present | 14 files | 0 | 6 days. GRN-R10, R11, R18 applied. Works with ORB_PROCUREMENT patched off. |
| W4-D LIS order guard and specimen collection | lis-7: OrderManagedByLab guard keyed on lab_order_items (409 on /transition and /result whatever the flag), conn-taking helpers, lab-mediated cancel, completion rule, OrdersPanel lab-managed display and catalogue typeahead, lis-8: Code 128 SVG encoder, label print view, ward Samples view, phlebotomy list, collect with two-identifier confirmation in one immediate transaction | 20 files | 0 | 8 days. LIS-R6 back-dating ships refused (strict interim), LIS-R15 and R16 applied. The guard stops early 'completed' lab billing. |
| W4-E Medication-order store foundation (record mode) | medorders-1: migration 61 med_order_store (med_orders, med_order_events with a no-update trigger only, medications.med_order_id, held_at, hold_reason, helix_patient_med_orders.med_order_id also in HELIX_SCHEMA with a guarded index step); ORB_MED_ORDER_STORE and ORB_MED_ORDER_LIFECYCLE join FEATURE_FLAGS (lifecycle without store refuses import); enforce_medication_safety gains exclude_med_id; services/medorders package core (state machine, projection, fingerprint, transition_in_txn); routes/medorders package pre-created and registered (core, verify, lifecycle, supply), medorders-2: boot adoption only with the store flag on (never on a GET), medorder_parity.py, parity report | 19 files | 1 | 6 days. Starts only after the architectural decisions are ruled. Package layout lets later medorders clusters own separate modules. Erase plan, reseed and retention registries re-run unchanged. |
| W4-F OPD queue and consultation record | opd-8: queue actions (call, skip, recall, assign, priority, room, LWBS as a coded cancel with no charge), /start and /complete need opd.consult in OPD mode, GET /api/opd/my-queue, OpdPanel feature tab (Sage assistant hidden, no view_context patient), content-free 'opd:queue_changed' nudge via realtime.schedule_on_loop; queue logic in a new opd_queue_service; add-walk-in deferred to W5-A, opd-10: migration 62 opd_consultations and append-only addenda; draft, save, sign, addenda; free-text diagnoses with optional code and code_status; OPD vitals kept in vitals_json and never scored; timeline shows OPD visits; dedicated no-scoring AST guard | 17 files | 1 | 8 days. Desk polls every 30 s until opd-9b (audit cost stated). OPD-N8 (completion without a signed note needs a reason) and OPD-N9 applied. realtime.py is imported, not edited. |

### Wave 5

Clinical entry points: register view and identity check, admission into a master bed, drug identity governance and goods receipt, LIS receipt and result entry, the medication-order chart lane, eMAR check and API, analyser registry, pharmacy transfers, end-of-stay screens. Gates: Scribe quick-admit and AdmitPatientModal ACVPU sessions merged before W5-B; Sage bed session confirmed off sage_routes.py before W5-C; CORS merged before W5-E. Migrations 63 and 64. Hot files: permissions.py and roles.ts owned by W5-E.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W5-A Register view, identity check, OPD allergy review | opd-6a: identity_check required for a visit picked from search (stated identifiers, match or no match, never the stored DOB); Register view search-first, IdentityConfirmDialog, duplicate warning with reason, DOB or stated age with the R018 hint, unverified ABHA placeholder, opd-10a: migration 63 patient_allergy_assessments (append-only); allergy review through clinical_records_service.record_allergies_on plus the assessment in one audited transaction; signing needs a review, opd-8 (remainder): add walk-in in OpdPanel reusing RegisterPatientView | 16 files | 1 | 7 days. OPD-S1, S5, S7 applied. allergy_interlock and drug_safety_service unchanged, so R011 is untouched. Browser check on an isolated verify stack. |
| W5-B Admit into a master bed and bed setup | adt-5: AdmissionRequest gains optional ward_id, bed_id, awaiting_bed, admitted_at, out_of_service_reason, boarder_acknowledged (ward and bed keep their str types); master-aware admit in an immediate session through locate_admission_on with the admission movement and audit; Idempotency-Key; boarder acknowledgement with clinical_access audit and case-room line; POST /api/adt/admissions/{id}/bed; BedPicker in AdmitPatientModal when flags.adt, adt-4b: Bed setup view (wards, rooms, bulk beds with preview, classes, site-config import, reconciliation, register complete) | 13 files | 0 | 7 days. Gate: both admission sessions merged; re-read the files first. ADT-S1 applied as (a): an occupied bed is refused and 'add an extra bed' creates a temporary bed with its own label (the shared-bed reason is not built). ADT-S2 applied as (c). ADT-P3 72 h back-time. Flag-off admission row snapshot unchanged and a missing bed field is still 422. |
| W5-C Drug identity governance and pharmacy goods receipt | grn-6: add a drug and govern its identity (draft, second-person confirm, retire and replace); ordering and suggestion paths filter identity_status='active' while safety screens read every row; sage_service.refresh_formulary_terms_from_db called, not edited, grn-8: pharmacy GRN with batch, MRP, double-typed expiry against site_today, free quantity, supplier invoice; post in one immediate transaction through receive_grn_lines; NDPS drugs only into the vault | 16 files | 0 | 8 days. GRN-RS1 (b), RS6 (b), R12 and R16 applied. Fix the seed's two brands tagged with the wrong generic before any site verifies through the store. |
| W5-D LIS receipt, worklists and result entry | lis-9: receive and reject with reason codes, recollect, rejected-no-decision list, NABH incident prompt, lis-10: lab_result_service entry and edit at reporting precision, comparators, coded values, flags only against approved ranges, worklists and entry grid | 15 files | 0 | 7.5 days. LIS-R2 and R7 applied: no ranges ship; no flag without an approved range; missing DOB means no range. |
| W5-E Medication orders: chart lane, eMAR order-state check and API | medorders-3: chart add through await run_db(create_chart_order) in one transaction (also takes the gate off the event loop); eMAR order_allows_dose for linked rows, 'given' only, below the replay and discharge guards with a post-INSERT re-check; legacy verify and reject mirror through the store keyed on the link, medorders-4: routes/medorders/core.py (list, get, events, create, sign, draft cancel, to-sign queue with 'Entered by a nurse', admin parity and adopt), scope with audited 404, MEDICATION_VERIFY constant, socket emit via realtime.schedule_patient_vitals with ids only | 17 files | 0 | 7.5 days. MO-R4 applied: medication.verify to the pharmacist only. The chart lane keeps today's nurse behaviour (R010 and R011 held). Re-run the eMAR, safety-gate, dose-guard, allergy and pharmacy-verification suites with the store on and expectations unchanged. |
| W5-F Analyser registry | lis-23: migration 64 lab_analysers (instruments with hashed credential and rotation, test-code map); Analysers view; map changes audited clinical_config | 5 files | 1 | 3 days. |
| W5-G Pharmacy-internal transfers | grn-11: pharmacy_transfer_service (FEFO issue plan, dispatch into in-transit, receipt by a different user, partial receipt, cancel); TransfersView | 5 files | 0 | 3 days. Uses post_movement and pick_fefo_lot; does not edit pharmacy_stock_service. |
| W5-H End of stay and reopen screens | adt-9: discharge modal 'How the stay ended'; EndStayDrawer with typed MRN read-back and open escalations and bundles listed; ReopenStayDialog; 'Record end of stay' tile action | 8 files | 0 | 3 days. Select absent with the flag off; existing discharge modal tests pass. |

### Wave 6

Second slices: OPD consultation screen and demographics, admission-door duplicate checks and stay keying, ADT transfers with housekeeping turnaround, one pharmacist verification with hold and stop, pharmacy expiry and stock take, LIS validation and release, analyser intake. Migrations 65 and 66. No hot file touched.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W6-A OPD consultation screen, investigations, demographics and prints | opd-11: ConsultationEditor, DiagnosisPicker on GET /api/opd/terminology/search, AllergyReviewPanel, timed vitals shown as plain values, visit-scoped orders through orders_service.create_order(opd_visit_id) with no order sets, disposition dialog, consultation print, opd-6b: DemographicsDrawer (reasoned correction with row_version, history), UHID card and token slip prints opened from the register view | 17 files | 0 | 7 days. orders_routes untouched. Prints carry name and users.registration_number only. |
| W6-B Admission-door duplicate checks and stay keying | opd-5b: admit_patient and Scribe quick-admit honour ORB_UHID_SOURCE and run the hard match when registration is on; a duplicate MRN on /admit becomes 409 for every profile, opd-17b (absorbs adt-18a's migration): migration 65 stay_keying (discharge_summaries.admission_id with backfill, partial unique one-active-admission index after a pre-check); discharge writes admission_id; /vigil/discharged one row per patient; NABH and scorecard per stay; one case room per patient pinned; written audit of stay-keyed readers | 10 files | 1 | 6.5 days. The 500-to-409 change and the one-active-admission index are always-on. test_scribe_quick_admit and test_admission_form_choices stay green. Shadow-instrumentation owner reviews. |
| W6-C ADT transfers and housekeeping turnaround | adt-6a: services/adt/transfer.py with rules A, B, C verbatim, cover resolved before BEGIN IMMEDIATE, lock and expected_bed_id, boarder acknowledgement; /api/frontdesk/transfers delegates when on and skips its post-commit audit; roster updates write movements; MoveDrawer and Move action, adt-7: migration 66 housekeeping_movement_links; sync_adt_movements with one task key per event; best-effort sync after commit | 16 files | 1 | 6.5 days. Release gate: ORB_ADT is not switched on at any Sage site until adt-6c (W7-C). The 36 frontdesk, 76 roster and 46 housekeeping tests pass with the flag off. |
| W6-D One pharmacist verification; hold, resume and stop | medorders-5a: verify() and reject() with dose, route, role and exclude_med_id; same-ingredient binding only (other products refused pending held R002); placeholder correction as amended events; Verify Queue routes call the store for linked rows, medorders-6a: hold, resume, stop with reasons; held_at projection; nurse board on-hold list, separate 'ON HOLD: do not give' paper MAR section, voice agent label, MedRoundSheet On hold group | 13 files | 0 | 7.5 days. MO-R2 applied (prescriber-only hold, resume, stop). 'Not given (held)' always allowed. |
| W6-E Pharmacy expiry, recall and stock take | grn-10: quarantine, recall, NSQ, write-off and the release leg in a new pharmacy_store_service; recall trace including ward-stock issues, audited phi_access fail-closed; ExpiryView, grn-12: count sheets (blind option), approval by a different user, count_variance movements, reasoned adjustments; CountsView | 9 files | 0 | 7 days. GRN-RS2 (b) applied: release by a different pharmacist with a reason; recalled and NSQ terminal. |
| W6-F LIS validation, release and delta checks | lis-11: technical validation and authorisation with release into observations in one immediate transaction; completion rule; release never blocked by order state, lis-12: delta checks via lab_observations.previous_result; reason required before technical validation | 8 files | 0 | 7 days. LIS-R5 ships the strict interim (two different people) with the recommended single-signatory mode as a site setting; LIS-R9 applied. |
| W6-G Analyser framework, HL7 adapter and MLLP listener | lis-24: AnalyserAdapter framework with conformance suite, router writing entered results only, HL7 ORU adapter, HTTP intake with X-Instrument-Key, lis-25: MLLP listener on MllpFramer with finish-the-batch, started from lab_module.startup only with ORB_LAB and the listener switch | 12 files | 0 | 7 days. LIS-R8 (no autoverification) and R14 applied. Entered results go through lab_result_service functions (call only). |

### Wave 7

Workflow completion: OPD charges and appointments, OPD-to-inpatient conversion and token board, ADT swap, Relay lane and census, presence and reservations, Helix supply hook and held labels, CPOE drugs through the store, NDPS register, LIS amendments and lab-side criticals, reports and TAT, and the multi-site spine. Gates: Relay session merged (W7-C, W7-E); Sage and Bridge sessions merged (W7-E); Samuel has ruled the multi-site decisions before W7-J. Migrations 67 to 70. Hot files: config.py, main.py, permissions.py, shellConfig.tsx owned by W7-J.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W7-A OPD charges and appointments with a doctor and slot | opd-13: migration 67 opd_charge_rules; consultation code by doctor, then department, then OPD-CONSULT; follow-up code; registration fee; payer at registration; prepaid charge voided on cancel or LWBS; charge logic in a new opd_charges_service, opd-16: migration 68 appointments_slots (in-transaction rebuild with orphan and clash pre-checks, origin and department carried, both indexes recreated); doctor and slot booking, reschedule, check-in creating the visit, no-show; cancel and complete ward-scoped; every date check on hospital_time; Appointments day view | 13 files | 2 | 8 days. Also fixes two pre-existing appointment defects (cross-ward cancel and complete; server-clock dates). OPD-N6 applied. test_bridge_appointments and test_forecast_appointments_booked_only stay green. |
| W7-B OPD to inpatient conversion and token board | opd-17a (absorbs adt-18a core): admit_existing_patient for status 'registered' only, one immediate transaction, same patient and MRN, case room and profile created once, no second portal login, admissions.opd_visit_id, AdmitPatientModal existing-patient mode with the adt-5 BedPicker, OpdPanel 'Advise admission', opd-9: name-free token board with per-board device keys (opd_display_boards), board snapshot in opd_queue_service, kiosk page reading the key from the URL fragment, Display boards view | 13 files | 0 | 6.5 days. OPD-S3 and S4 applied: no OPD vitals copied into vitals and no OPD prescription onto the inpatient chart; both listed with their times for reference. OPD-N7 applied. A 'discharged' patient is refused here (W8-A). |
| W7-C ADT swap, correction, Relay transfer lane and census | adt-6b: swap with both admissions in scope and both expected bed ids, correction rows, adt-6c: sage_handoff_service._dispatch_to_transfer calls adt transfer with rule C cover when ORB_ADT is on; confirmation stands as the boarder acknowledgement, adt-13a: adt_census_service (midnight census, transfers, ends by type, per-stay days, occupancy, ALOS, return to ICU) with 'insufficient data' before each ward's ledger start; Census view and CSV | 13 files | 0 | 7 days. Gate: Relay session merged; verify relay_routes.py:419 holds no outer session. The flag-off Relay department-list defect (ward labels from portal logins) should get its own task for the Relay owner. ADT-P4 applied. |
| W7-D ADT presence and bed reservations | adt-10: leave and absconded recording (movement plus presence_status, bed held), 'Not on ward since HH:MM' on Vigil and nurse rows, display only, adt-12: migration 69 bed_reservations; reserve, release, expire; locate_admission_on refuses a bed reserved for someone else; retire refuses reserved beds | 19 files | 1 | 5 days. ADT-S7 applied (doctors and nurses record presence; grants pre-registered in wave 1); ADT-P9 applied. Obs-due and escalation outputs byte-identical until adt-11. |
| W7-E Helix supply hook, custody link and held labels in readers | medorders-5b: on_applied callback in transition_order_status and receive_order; linked Helix steps screened with exclusion, dose and role; placeholder refused for linked rows (closes F-A for them); idempotent request-supply; dose links a supply row only when exactly one received unconsumed unit exists, medorders-6b: held labels in I-PASS, discharge reconciliation, the portal ('On hold', no colour, no verdict), FHIR on-hold, Sage context; reader registry test | 14 files | 0 | 7 days. Gates: Relay, Sage and Bridge sessions merged. MO-R12 applied (prescriber picks the supply source). Re-run test_agentic_confirm_race and the Helix lifecycle, gate, substitution and ward-receipt suites with the store on. |
| W7-F CPOE and order-set drugs reach the ward after a prescriber signs | medorders-7: lifecycle-only linked drafts for CPOE and order-set drugs; sign projects to board, eMAR and screen; guarded orders.status mirror plus order_events; linked /orders transitions refused except a delegated cancel; shadow clinical.action at sign | 10 files | 0 | 5 days. Keeps the lis-7 lab-managed guard intact. MO-R10 and MO-R11 applied. Note the shadow-scorecard timing change in its documentation before any pilot runs lifecycle mode. |
| W7-G NDPS register and return to vendor | grn-13: ndps_service through the stock-service hook, running balance per drug and store, PIN witness by a different user, both failure modes built, register refuses to enable without ORB_PHARMACY_NDPS_ON_FAILURE, audited CSV export, grn-22: vendor return documents (PHVR) with in-transaction audit | 12 files | 0 | 7 days. GRN-RS7 stays unset (strict interim): the register cannot be enabled until Samuel confirms a mode. GRN-R14 applied (vault receipt, PIN witness, no NDPS seed stock). |
| W7-H LIS amendments and lab-side critical notification | lis-13: amend and withdraw with versions, superseded observations, post-commit notice, lis-15: services/case_room_alerts.py extracted from _persist_relay_alert (wrapper keeps the Vigil sender and shadow emits); lab_critical_service with delivery status, no-case-room escalation, callback log, sweep from lab_module.startup, tiers from site.toml with no default ladder; Critical view | 16 files | 0 | 7.5 days. Built behind ORB_LAB; go-live is held (LIS-R20 asks whether these surfaces sit inside held R023). LIS-R1, R3, R4, R10, R19 applied as build defaults. test_ingestion_alert_parity stays green. |
| W7-I LIS reports and turnaround | lis-14 (without the Vigil modal): lab_report_service, LabReportPrintView with signatory, range snapshot and versions, Reports view, lis-18: lab_tat_service, TAT view and CSV, optional NABH indicators | 12 files | 0 | 6 days. The Vigil Lab Reports modal change lands in W8-D, where VigilPanel is free. |
| W7-J Multi-site spine | multisite-1: ORB_MULTI_SITE joins FEATURE_FLAGS; migration 70 facilities_spine (facilities with id 1 MAIN, departments seeded from users, site.toml, admissions and ADT wards.department, user_facilities, enterprise_access_grants, patients.registered_facility_id, admissions.facility_id, two admissions indexes); facility_service read helpers with a 30 s multi_facility_data cache; facility_fence with the '*' sentinel and middleware active only with multi-facility data; /api/health platform object; ops_health checks, multisite-2 (part): facility.read and facility.manage constants and the shellConfig platform-flag read; facility_routes skeleton registered | 12 files | 1 | 4.5 days. Starts only after Samuel rules the multi-site decisions; if he defers, drop every multi-site cluster and nothing else changes. Isolation is keyed on data, so this is inert until a second facilities row exists. Run with the test env, never the shipping .env. |

### Wave 8

Readmission onto the same record, OPD prescription and counter billing, LIS ward card and interfaced criticals, Helix AI lanes and verification screens, ward stock, verify-before-dose and authored schedules, the multi-site scope core. Gates: Sage session merged (W8-A, W8-E); CORS merged (W8-B); Bridge merged (W8-J). Migration 71. Hot files: permissions.py owned by W8-K; server/ws/realtime.py owned by W8-B.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W8-A Readmit a discharged patient onto the same record | opd-17c (absorbs adt-18b): admit_existing_patient extended to 'discharged' patients with an identity check; previous-stay active and prn medications discontinued in the readmission transaction and listed for re-prescribing; profile updated never blanked; case room reused with a READMISSION line; Sage room memory archived and restarted; previous-stay vitals and risk rows shown only with their date and a 'previous stay' label on the board, NEWS2 and Sage, never as current latest | 8 files | 0 | 7 days. OPD-S8 and ADT-S9 applied as build defaults. Every latest-vitals reader keeps ORDER BY timestamp DESC, id DESC and is covered by one test. This is also the Ward Record's 'returning patient reopens the same record'. Highest-risk clinical increment in the plan. |
| W8-B Counter billing, front-office MIS and live desk nudges | opd-14: GET /api/billing/op-patients, Charges picker includes outpatients, OPD counter form uses the existing createOpdInvoice wrapper, opd-18: daily front-office MIS (aggregates only, hospital-local day) and Day summary view, opd-9b: realtime.connect joins registration.read holders to opd rooms; desk stops polling; queue read audit deduped per OPD-N12 | 13 files | 0 | 6.5 days. test_ws_origin stays green. Cashier still cannot reach billing screens. |
| W8-C OPD prescription on the shared store | opd-12: outpatient-setting med_orders via a new services/medorders/outpatient.py, screened by enforce_medication_safety(entry_point='opd_prescription'); opd.consult and medication.prescribe; never writes medications or Helix rows; printed prescription | 6 files | 0 | 3.5 days. Needs ORB_MED_ORDER_STORE; if Samuel defers the medication-order store, this increment waits with it. OPD-S7 applied: refused until an allergy assessment exists. |
| W8-D NABH indicators, capacity source, ward clerk checks and the Vigil lab reports list | adt-13b: QI-UTL-01, QI-OUT-04 and per-stay inpatient days for complete wards only, adt-14: capacity from the bed register for register-complete wards, refreshed after master writes, adt-17 (remainder): clerk scope and refusal tests (role, grants and creation gating landed in wave 1), lis-14 (remainder): VigilPanel Lab Reports modal lists LIS reports | 10 files | 0 | 6 days. ADT-P5 applied. NABH keeps the rule that residual bias may only make the hospital look worse. |
| W8-E Pharmacy verification screens, AI lanes through the store, dispense quantity | medorders-5c: PharmacyVerifyQueue on /queue/verification with the allergy list and a same-ingredient picker; Helix modal shows the linked order and timeline, medorders-8: Sage handoff writes chart row, med_order and supply row in one transaction with the same LIKE binding (resolve_by_name_in_txn); Scribe extraction creates med_orders; relay_routes.py:420 untouched, grn-14: dispense quantity and lot choice with the lot bound to the effective drug | 15 files | 0 | 8 days. One cluster because all three edit the Helix review modal and helix_service. GRN-R13 applied. W111 untouched (Scribe drugs stay 'prescribed, needs pharmacist'). |
| W8-F Ward indents and imprest stock; seeds and docs | grn-21: issue indents from ward imprest stores, FEFO issue plan, dispatch, ward receipt by a different user, imprest list starting from the ISMP high-alert set; nurse Ward stock section, grn-18: seed_pharmacy_store after seed_helix and before seed_pharmacy_queue (dev profile, flag on); GST 72-month retention mapping; procurement boundary text corrected; manual hand-edited | 17 files | 0 | 6 days. GRN-RS5 (b) and GRN-R8 (a) applied; ORB_PHARMACY_WARD_STOCK stays off until Samuel confirms RS5. Never re-run _assemble.py on the manual (assembly drift). |
| W8-G LIS ward critical card and interfaced criticals | lis-16: CriticalLabCard on the nurse home and the doctor home (the lab's own flag and the agreed limit, no Orb-computed severity), read-back acknowledgement, lis-22: OBX-8 HH/LL/AA from an external LIS raises a critical callback with ORB_LAB_CRITICAL_INTERFACED on, service identity system_lab added to database.SERVICE_ACCOUNT_IDS | 11 files | 0 | 6 days. Built behind default-off switches; go-live held with lis-15 on LIS-R20 (an R023 question). LIS-R22 applied as the build default. |
| W8-I Verify before first dose and prescriber-authored schedule | medorders-11: order_allows_dose requires projection verified_by for every linked order, with stat and urgent first doses allowed and flagged, medorders-19: migration 71 med_order_schedule_times; due times from a signed site_config map with no built-in default; the board uses authored times when present | 10 files | 1 | 7 days. MO-R1 and MO-R8 applied as build defaults (follow-on increments that need no held ruling); both can be dropped without affecting anything else. |
| W8-J Patient portal lab reports | lis-29: authorised LIS reports in the portal behind ORB_LAB_PORTAL_REPORTS after the site delay or clinician release, flags as printed text with no colour, self-scope only | 3 files | 0 | 2 days. Below the LIS architect's cut line; optional. LIS-R11 (c) applied. Gate: Bridge session merged. |
| W8-K Multi-site facility scope core | multisite-5a: facility_scope_for reading only id, role and department plus caches (all six user loaders agree); permissions.facility_scope, FACILITY_PATIENT_SQL, facilities= keyword on scope_patient_ids_sql and patient_in_scope; facility-caused denials audited in both helpers; UserOut and /me carry the facility | 6 files | 0 | 5 days. With one facility, test_patient_scope_routes and test_caller_named_patient_scope are green and the statement count is unchanged. MS-R5 (a) applied. |

### Wave 9

Clerk admission and departure, strict locations, live bed board, presence-aware clinical lanes, store-backed screening, medication lifecycle screens, ward returns, crash carts, consent at registration, terminology loader, facility identity and facility users. Migration 72. Hot files: permissions.py and roles.ts owned by W9-G; shellConfig.tsx owned by W9-D; realtime.py owned by W9-A.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W9-A Clerk admission and departure, strict locations, live bed board | adt-17b: clerk registration-level admission with clinical fields 'Not yet assessed' (never NKDA, interlock treats unknown as unknown, board flags 'admission assessment due'); clerk 'left the ward' through end_stay with discharge_preconditions and read-back; clerk executes a department transfer only when it names the ordering doctor, adt-16: ORB_ADT_STRICT_LOCATIONS refuses unlisted wards and beds on every lane, adt-19: content-free 'adt:changed' {ward_id} emitted after commit from the route layer; board refetches, 30 s poll as fallback | 17 files | 0 | 6.5 days. ADT-S5, S3 and S8 (b) applied as build defaults; clerk grants were pre-registered in wave 1. F104 applied: warn until a ward is register-complete, then strict per site. |
| W9-B Clinical lanes honour presence | adt-11: one presence helper read by nurse_shift_service (obs and med due paused with the reason), the escalation sweep (pause and resume, never close), sepsis auto-start, the vitals simulator (skips absent patients) and the ward pack ('not on ward') | 7 files | 0 | 3.5 days. ADT-S4 (a) applied as the build default. It changes no discharge or end-of-stay chain handling, which stays with held R009. |
| W9-C Store-backed interaction and duplicate screens; exact Sage binding | medorders-9: _screen_interactions reads live signed orders (PRN and held included, drafts excluded) plus unlinked legacy rows with self-exclusion; same-ingredient duplicate as a 428 with reason; PRN orders givable; discharge reconciliation reads store statuses, medorders-20: Sage handoff binds the reconciler match at 0.8 and screens the matched product's name | 9 files | 0 | 5.5 days. MO-R3 (a) and MO-R6 (b) applied. Bedside override path unchanged (R011 held). |
| W9-D Medication lifecycle screens and ward returns | medorders-10: MedOrderList and MedOrderTimeline in the chart Medications modal (text status chips, supply line, permission-driven Sign, Hold, Resume, Stop), Awaiting pharmacist group, held refusal mapped in AdministerModal, DeliveriesSheet names the order, doctor's to-sign chip, shellConfig reads features, grn-15: ward returns into returns quarantine, inspection with restock to the original lot, nurse recording from DeliveriesSheet | 15 files | 0 | 8 days. The chart Add form keeps posting to the vigil route (R010 held). GRN-RS3 (b) applied for the restock leg. Browser walk as doctor, nurse and pharmacist on an isolated verify stack. |
| W9-E Facility identity and invoice supplier snapshot | multisite-2: facility_routes GET and PATCH for facility 1 with the flag off; create and deactivate 404 with the flag off, then the create gate (always refuses while the fence holds '*'); migration 72 invoice_supplier_snapshot written once at numbering; reprints use the snapshot; identity helpers in a new facility_identity.py; Facilities admin tab with the identity card | 14 files | 1 | 4.5 days. Facility 1 identity works with ORB_MULTI_SITE off (W162 half needs no ruling); an issued GST invoice never changes after issue. |
| W9-F Crash-cart stores | grn-23: crash_cart store kind with par levels from the approved list, seal-and-check record, top-up suggestions, cart expiry on the overview | 5 files | 0 | 2 days. Behind ORB_PHARMACY_WARD_STOCK (GRN-RS5 confirmation needed before go-live). |
| W9-G Consent at registration and terminology loader | opd-19: registration step records notice acknowledgement and treatment consent through consent_service in the preferred language; consent.record granted to the receptionist, opd-20: server/scripts/load_terminology.py (the loader the docstring names), idempotent with a version row, never rewriting signed notes | 8 files | 0 | 4 days. OPD-N3: the receptionist consent grant ships only after counsel agrees. OPD-N11: the loader is built but no ICD-10 release is loaded until counsel confirms the licence. |
| W9-H Facility users, grants and shell chip | multisite-6: facility column and home-facility select for department-less staff; home row written in the same transaction; refusals for department-scoped staff and out-of-scope moves; enterprise grants (security, fail-closed); grant_enterprise_access.py bootstrap; roleLine shows the facility with multi-facility data | 8 files | 0 | 3.5 days. MS-R6 (a) admin-only grants applied. |

### Wave 10

Billing completions, facility-aware admission and ingestion binding, ward stock consumption. Migration 73. After this wave every non-held increment outside multi-site is built. Hot files: config.py owned by W10-B.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W10-A Billing owner: bed-class bed days, pharmacy MRP, per-facility numbering | adt-15: bed-day charges by the bed class at the midnight census from stay_start, leave days per site setting, 'Post bed days through [date]', grn-16: pharmacy candidates priced from the dispense movement's lot MRP and the dispensed drug, returns as their own credit key, multisite-7a: migration 73 document_series_per_facility (rebuild with facility_id and UNIQUE(facility_id, series_code)); next_document_number(series_code, facility_id=1) at the four lookups and about 13 call sites; prefix validation against other facilities and issued numbers; series editor | 17 files | 1 | 8 days. One cluster because all three edit billing_service. The series rebuild must carry opd-1's resets_yearly and check_digit columns and the UHID row. ADT-P6 and GRN-R12 applied. The billing owner should also review the pre-existing defect where billing names the prescribed drug, not the substitute (flag-independent). |
| W10-B Facility-aware admission and ingestion binding | multisite-3: covering departments and resolve_admission_department facility-aware; admissions and patients stamped from the departments master; cross-facility transfers refused with an audit row; site.toml [[facilities]] validated, multisite-16: MLLP and REST sources bound to a facility; _resolve_patient_id(db, raw, facility_id); unbound or cross-facility matches take today's unknown-patient path with an audit row | 10 files | 0 | 6 days. MS-R4 and MS-R14 applied. The vitals INSERT path (clinical_time, carried_from) is not touched. |
| W10-C Ward stock consumption and the analyser simulator | grn-17: post-commit 'consumption' movement against the ward imprest lot after an eMAR 'given' for a drug linked to helix_medication_id; never blocks or delays a dose, lis-28: DEMO_MODE-only analyser simulator through the real path with source 'simulated' and honesty labels | 6 files | 0 | 4.5 days. grn-17 needs the medication-order store (eMAR link to formulary_id); lis-28 is below the LIS cut line and optional. |

### Wave 11

Multi-site columns and the SQL-site registry, built over a codebase where every other epic has stopped adding SQL. Migrations 74 and 75. Hot files: permissions.py owned by W11-A.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W11-A Encounters and wards carry the facility | multisite-4: migration 74 facility_columns_encounters (opd_visits, appointments, surgeries, roster and history import runs, plus wards.facility_id mapped from the 'MAIN' label and lab_specimens.site_id); writers stamp the facility; FACILITY_PATIENT_SQL gains OPD visits and appointments; roster import enforces the one-facility-per-patient attribution invariant | 8 files | 1 | 4 days. The ops_health count of patients with admissions at more than one facility must stay 0. |
| W11-B SQL-site registry, real fence list, leak sweep | multisite-5b: registry classifying every SQL site on patients, admissions, opd_visits, appointments, surgeries, orders, medications, invoices, payments and audit_logs, including the new HIS packages; AST ratchet; FACILITY_BLIND_SURFACES generated from the blind rows (replaces '*'); leak sweep over every GET and the enumerated POST read lanes; INSERT INTO users ratchet | 14 files | 0 | 4 days. The registry is split by area so each later sweep cluster edits only its own file. |
| W11-C Money, stores and assets carry the facility | multisite-7b: migration 75 facility_columns_admin (invoices, payments, refunds, claims, deposits, counters, encounter charges, stores, indents, POs, GRNs, housekeeping units, biomedical assets, kitchen indents, NABH incidents and exports, ABDM care contexts, enrolled devices, Helix lots and movements; nullable on tariff plans, forecasts, holidays); writers stamp the facility | 12 files | 1 | 3.5 days. |

### Wave 12

Multi-site scope sweep, part 1: clinical lanes, Sage and sockets, audit, exports, pharmacy queue and money, front desk, OPD and ADT lanes. Hot files: main.py owned by W12-A; realtime.py owned by W12-B.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W12-A Scope sweep: clinical lanes | multisite-8: facilities passed at every blind clinical row (board, discharged list, search, timeline, orders, eMAR, bundles, Lens, FHIR export, forecast, appointments, escalation, sepsis, surgery scope, consent, ward brief, shadow routes, media route, Scribe and Relay case lists) | 23 files | 0 | 5 days. |
| W12-B Scope sweep: Sage lane, out-of-scope locator and socket rooms | multisite-10: sage_service _scope_sql and _patient_in_scope changed in lockstep with permissions (parity on two-facility fixtures); _out_of_scope_locators limited to the caller's facilities; 'no matching patient at <facility>'; facility socket rooms once multi-facility data exists | 11 files | 0 | 4.5 days. The bed matcher is not touched (R026 held). MS-R13 applied. |
| W12-C Audit attribution and every audit reader | multisite-11: facility in hashed metadata only with multi-facility data, resolver with caches, truncation stub keeps facility_id; every audit_logs reader classified and filtered by the epoch rule; build_audit_facility_index.py maintenance CLI | 13 files | 0 | 4.5 days. Time the index CLI on a backup copy first. test_audit_trail_attack and test_audit_fail_closed_categories stay green. |
| W12-D Downtime packs, exports and backup statements per facility | multisite-15: ward packs and the nightly self-export per facility folder with local mirrors; FHIR Organization and Encounter.serviceProvider; facility extract CLI; restore-rolls-back-every-facility statement on Data and Backups and in the CIO questionnaire | 9 files | 0 | 3.5 days. MS-R12 applied. |
| W12-E Scope sweep: pharmacy queue, money, front desk and group identity match | multisite-9a: pharmacy queue (zero scope calls today), Helix routes, payments, billing, payer and front desk facility-scoped, document-keyed for money and queues, multisite-17: group-wide minimal-demographics identity match for the desk, audited with a reason and rate-limited | 13 files | 0 | 7.5 days. MS-R9 (a) and MS-R11 (a) applied. |
| W12-F Scope sweep: OPD and ADT lanes | multisite-8h1 (new): facility scope and stamping for opd_routes, registration_routes, schedule_routes, OPD services, the routes/adt package, services/adt, bed master and census | 22 files | 0 | 3.5 days. Not in the multi-site architect's estimate: these routes did not exist when that design was written. |

### Wave 13

Multi-site scope sweep, part 2, and per-facility configuration. Hot files: permissions.py owned by W13-E.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W13-A Administrative lanes and Command Center per facility | multisite-9b: dietary, compliance, NABH, ABDM, procurement, workforce, housekeeping, biomedical, admin routes and the Command Center named list facility-scoped, multisite-12: house_snapshot(facility_ids), capacity per facility from the bed master, NABH indicators and evidence export per facility, Command Center selector for enterprise users | 19 files | 0 | 8 days. |
| W13-B Price lists per facility | multisite-13: default_tariff_plan(facility_id) replaces the four inline queries; facility plan, then group plan, then default; interstate from the facility state code; price list editor | 6 files | 0 | 3 days. |
| W13-C Pharmacy stock per facility and pharmacy-store sweep | multisite-14: dispensing draws only the admission facility's lots (dispensable predicate gains the facility), GRN into a facility store, stock and formulary on-hand per facility, multisite-8h3 (new): facility scope for the routes/pharmacy_store package and pharmacy store services | 24 files | 0 | 5.5 days. Never move create_order's transaction boundary (R024). |
| W13-D Scope sweep: LIS and medication-order lanes | multisite-8h2 (new): facility scope for the routes/lab package, lab services and quarantine, the routes/medorders package and services/medorders; performing_facility stamping for lab work | 21 files | 0 | 2.5 days. Not in the multi-site architect's estimate. |
| W13-E Visiting consultants: session facility switch | multisite-19: 'visiting' user_facilities rows with their own department, JWT 'fac' claim validated per request, POST /api/auth/facility audited, effective department overlaid so department_scope is unchanged | 6 files | 0 | 4 days. MS-R7 (a) applied. Separate accounts remain the first-go-live option. |

### Wave 14

Two-facility go-live drill: the create gate passes through the product only when every sweep is done.

| Cluster | Increments | Files | Migrations | Note |
|---|---|---|---|---|
| W14-A Two-facility drill and runbook | multisite-18: second-facility seed on the isolated verify stack (honest wording, no demo labels in the app), audit index built on a realistic copy, create gate passed through the product, browser pass as F1 nurse, F2 nurse, F2 admin, F2 pharmacist, F2 cashier and enterprise admin, two-facility soak, docs/MULTI_SITE.md runbook (flag, grant bootstrap, index window, ingestion keys, satellite mirrors, reverse-proxy Host caveat) | 3 files | 0 | 3 days. FACILITY_BLIND_SURFACES must be empty and the leak sweep strict everywhere; booting with the flag off and two facilities keeps isolation. |

## Migrations

**Numbers claimed outside this plan:** 76, surgery_who_checklist_column (server/migrations_his/m0076_surgery_who_checklist_column.py), claimed on 4 October by the agentic-confirm session: a plain ADD COLUMN surgeries.who_checklist, no rebuild, no FK. Any later HIS number (for example medorders-17 when its ruling is released) starts at 77; re-read server/migrations_his/ before taking one.

**Current state (verified in code)**
- The highest migration is 47, vitals_carried_from, registered at server/migrations.py:5494.
- The runner applies every version missing from schema_migrations, in ascending order (migrations.py:5496-5513). It checks membership, not MAX(version).
- So a gap, or a higher number landing before a lower one, is safe. Nothing in this plan depends on landing order beyond its stated dependencies.

**Mechanism (wave 1, cluster W1-A)**
- New migrations live one per file: server/migrations_his/m00NN_<name>.py.
- Each file exports one Migration whose version must equal NN.
- The package collects them by version and merges them into the sorted MIGRATIONS run.
- Import fails on a duplicate version or a filename/version mismatch, so a collision fails in CI and at boot, as today.
- migrations.py is edited only in wave 1: the collection hook and LATEST_USERS_ROLE_CHECK. That removes it as a shared hot file for every later wave.

**Allocation (one owner per number)**
- 48 users_role_batch: W1-A, his-2. FK-parent rebuild of users.
- 49 helix_tables_in_runner: W1-A, grn-1.
- 50 registration_opd_foundation: W2-A, opd-1. Does not create patient_movements.
- 51 adt_movement_ledger: W2-A, adt-2.
- 52 patients_status_registered: W2-A, opd-2. FK-parent rebuild of patients.
- 53 adt_bed_master: W2-B, adt-1.
- 54 pharmacy_store_schema: W2-C, grn-3.
- 55 lab_contract: W2-D, lis-3.
- 56 observations_lab_links: W2-D, lis-5 part b.
- 57 clinic_sessions: W2-E, opd-15.
- 58 adt_admission_location: W3-B, adt-3.
- 59 adt_discharge_dispositions: W3-B, adt-8a.
- 60 result_quarantine: W3-E, lis-19.
- 61 med_order_store: W4-E, medorders-1. Its Helix column also goes into HELIX_SCHEMA; the index is created only in guarded code.
- 62 opd_consultations: W4-F, opd-10.
- 63 patient_allergy_assessments: W5-A, opd-10a.
- 64 lab_analysers: W5-F, lis-23.
- 65 stay_keying: W6-B, opd-17b. Includes the one-active-admission partial unique index that adt-18a also asked for, built once, after a pre-check.
- 66 housekeeping_movement_links: W6-C, adt-7.
- 67 opd_charge_rules: W7-A, opd-13.
- 68 appointments_slots: W7-A, opd-16. An in-transaction rebuild; appointments is not an FK parent.
- 69 bed_reservations: W7-D, adt-12.
- 70 facilities_spine: W7-J, multisite-1.
- 71 med_order_schedule_times: W8-I, medorders-19.
- 72 invoice_supplier_snapshot: W9-E, multisite-2.
- 73 document_series_per_facility: W10-A, multisite-7a. This rebuild carries opd-1's resets_yearly and check_digit columns and the UHID row.
- 74 facility_columns_encounters: W11-A, multisite-4. Also adds wards.facility_id, mapping the 'MAIN' label to facility 1.
- 75 facility_columns_admin: W11-C, multisite-7b.

**Held increments**
Held increments get no number now. medorders-12 and medorders-17 each need one and take the next free number when Samuel releases them.

**Rules every migration follows**
- Additive only: ADD COLUMN and CREATE IF NOT EXISTS, guarded by PRAGMA introspection.
- No new FOREIGN KEY, so no new table becomes an FK parent.
- One statement per conn.execute, never executescript (it would commit the runner's transaction).
- No CHECK on columns added to Helix tables, which are FK parents; their enums are validated in the service.
- Vocabularies on admissions and patient_movements are enforced by triggers, so they can be widened later without a rebuild.
- Every migration is tested three ways: on a fresh DB, on a tmp_path legacy DB, and run twice.
- The suite runs without the shipping .env.

**The two FK-parent rebuilds (48 users, 52 patients)**
- Both use database._atomic_table_rebuild with a frozen column list, plus row-count and PRAGMA foreign_key_check assertions.
- Each is a separate deploy step, done after a verified backup_db.py backup and a dry run on a dev DB copy on the isolated verify stack.
- Both commit the runner's open transaction by construction, which is why they are batched to one rebuild per table.
- No epic may add a users column; it would abort the next role rebuild (migrations.py:4773).
- ADT adds no patients or admissions status values: presence and discharge type are columns. So patients is rebuilt only once.

**Lazy schema owners**
- grn-1 brings the Helix tables into the runner.
- opd-1 freezes the opd_visits DDL and adt-2 freezes patient_movements. ensure_frontdesk_schema and init_helix_db stay as idempotent fallbacks that are kept in step.

**Flags and profiles**
Migrations run on every profile, including the Ward Record and demo DBs, with the flags off. New columns stay NULL and new tables stay empty.

**Collisions**
If a running session claims a number in 48 to 75 first, renumber this plan's not-yet-merged file to the next free number and update this table before merge. Never renumber a migration that has shipped to any DB.

## Risks

- Size and priority. About 460 engineer-days are scheduled, plus about 37 held. At the sales plan's 4 engineer-days a week that is roughly 115 engineer-weeks, against an approved plan whose only priced product is the Ward Record. Every wave ends in a mergeable state, so Samuel can stop after any wave. Multi-site (waves 7 and 9 to 14) and the medication-order store can each be dropped without affecting the other epics, except that opd-12 and grn-17 need the store.
- Some changes are always on, whatever the flags say: the users and patients FK-parent rebuilds (48, 52); the patient_movements rebuild (51); Helix tables moving into the runner (49); dispenses running in an immediate transaction that fails closed on an audit outage (grn-4); the Tier 1 creatinine parity fix (lis-5a); the ghost-route 410; duplicate MRN returning 409 instead of 500 (opd-5b); and the one-active-admission index (65). They reach the Ward Record and the demo DBs too.
- The two FK-parent rebuilds can lose or orphan patient or user rows if the column list is wrong. Mitigations: frozen column lists, row-count and foreign_key_check assertions, a verified backup first, and a dry run on a dev DB copy on the isolated verify stack before any real DB.
- Wave 1 changes core files: migration discovery, route packages, glob-discovered views and every grant registered at once. A defect there blocks every later wave. Review W1-A and W1-B as one block, and pin the grants in test_rbac and roles.test before anything builds on them.
- Grants are registered before the code that uses them. They are inert because every route 404s while its flag is off, but they enlarge the permission matrix that reviewers see. The parity tests must state which grants are deliberately unused.
- Multi-site still leaks if a SQL site is missed. The ratchet, the runtime fence and the leak sweep reduce that risk, but the sweep only finds identifiers it plants. This plan added about 7.5 days (multisite-8h1 to 8h3) for routes the multi-site architect could not have counted. Any HIS code added after wave 11 must register its SQL sites in the area registry.
- Running sessions. Clusters that edit admission_service, scribe_routes, AdmitPatientModal, helix_service, relay_routes or sage_handoff_service, sage_service or sage_routes, bridge_routes, realtime.py or the config.py and main.py CORS block carry gates, and must re-read those files after the owning session merges. About ten sessions edit the tree, so run ListAgents before every wave and land each increment as its own commit.
- Migration numbers 48 to 75 are pre-allocated. If another branch claims one first, the duplicate check stops the boot. Renumber the unmerged file and update the owner table; never renumber a shipped migration.
- Readmission is the sharpest clinical hazard. Until W8-A, a returning patient cannot reopen the same record. W8-A then changes every latest-vitals reader and Sage memory, under ADT-S9 and OPD-S8 build defaults that Samuel has not yet confirmed. It is the highest-risk increment in the plan.
- The medication-order store keeps two stores in step during the move. I1, I2, the parity report and the reader registry reduce drift, and the paper downtime MAR is the most dangerous reader because it has no link check. The seed tags two brands with the wrong generic, which the same-ingredient check would accept; fix the seed before any site verifies through the store.
- The shadow scorecard, the pilot's only evidence instrument, changes in opd-2 and opd-17b. In lifecycle mode, medication CPOE events move from create to sign. The shadow-instrumentation owner must review all three before any pilot runs with these flags on.
- Shared domain files (frontdesk_service, admission_service, billing_service, nabh_service, vigil_routes, helix_service) are edited in many waves. File sets are disjoint inside each wave, but rebase conflicts across waves are certain; keep increments small.
- Pre-existing defects found by the architects are fixed only where an increment touches them: appointment cancel scope and server-clock dates (W7-A), the ghost route (W1-D), and OPD audits written after commit (W3-A). Others need separate tasks now: Helix verify accepting the 'REVIEW REQUIRED' dose and self-screening a Sage pair (F-A, F-B); the Relay transfer lane writing ward labels as departments when the flag is off; billing naming the prescribed drug instead of the substitute; and relay_routes.py:420 running the handoff synchronously on the event loop.
- The escalation ladder tick has no patient-status filter, so chains keep escalating after any end of stay, including death. This is R009 territory and unchanged by the plan. End-of-stay screens list open chains and never close them.
- Boarders stay invisible to host-ward nurses (ADT-S6, not scheduled). The bed board will make that gap visible to staff.
- Audit volume. Front-desk queue polling writes up to about 1,200 phi_access rows per open screen per day until opd-9b (W8-B). Audit already drives appliance storage.
- The suite cannot run with the shipping .env. Every increment's tests patch flags and must run under the test env. Browser verification of each screen needs the isolated verify stack.
- No go-live is possible without inputs only Samuel or a site can supply: hospital critical-value lists and reference ranges, analyser protocol documents, a site ORU sample, ICD-10 licence advice, an NDPS failure mode, and confirmation of every new safety ruling applied here as a build default.

## Epic increments

### Patient registration and OPD (26 increments, 84.5 engineer-days)

| Id | Increment | Days | Depends on | Safety | Held by |
|---|---|---|---|---|---|
| opd-1 | Feature flags and additive schema foundation | 3 |  |  |  |
| opd-2 | patients.status admits 'registered' (FK-parent rebuild) | 2.5 | opd-1 | yes |  |
| opd-3 | Receptionist role, registration permissions and the desk dependency | 3 | opd-1 |  |  |
| opd-7 | Opt-in OPD scope arm and the OPD record view | 4.5 | opd-1, opd-3 | yes | OPD-N2 |
| opd-4 | Registration service and API | 4.5 | opd-2, opd-3, opd-7 | yes |  |
| opd-5 | Duplicate and returning-patient detection | 3.5 | opd-4 | yes | OPD-S2 |
| opd-5b | Admission doors join the patient index | 2.5 | opd-5 | yes | OPD-S2 |
| opd-6a | Register view and returning-patient identity check | 4 | opd-4, opd-5 | yes | OPD-S1 |
| opd-6b | Demographics drawer and prints | 3 | opd-6a |  |  |
| opd-8 | Doctor OPD queue tab and queue actions | 4 | opd-3, opd-6a, opd-7 |  |  |
| opd-9 | Name-free token display board with device keys | 2.5 | opd-8 |  |  |
| opd-9b | Live queue nudges for desk accounts | 1 | opd-8 |  |  |
| opd-10 | OPD consultation record (server) | 4 | opd-7, opd-8 | yes |  |
| opd-10a | OPD allergy review and allergy assessment status | 2.5 | opd-10 | yes | OPD-S7 |
| opd-11 | OPD consultation screen, investigations and prints | 4 | opd-10a | yes |  |
| opd-12 | OPD prescription on the shared medication-order store | 3.5 | opd-11, medorder-1 | yes | OPD-S7 |
| opd-13 | OPD charges: registration fee, clinic codes, follow-up rule, payer at registration | 3.5 | opd-4, opd-8 |  |  |
| opd-14 | Outpatient billing at the counter | 3 | opd-13 |  |  |
| opd-15 | Clinic session master and availability | 3 | opd-3 |  |  |
| opd-16 | Appointments with a doctor and a slot, check-in, no-shows, receptionist booking | 4.5 | opd-15, opd-8 |  |  |
| opd-17a | Convert a registered outpatient to inpatient | 3.5 | opd-4, opd-6a, opd-10, opd-5b | yes | OPD-S1, OPD-S3, OPD-S4 |
| opd-17b | Stay keying for repeat admissions | 4 | opd-2 | yes |  |
| opd-17c | Readmit a discharged patient onto the same record | 4.5 | opd-17a, opd-17b | yes | OPD-S1, OPD-S8 |
| opd-18 | Front office MIS | 2.5 | opd-8 |  |  |
| opd-19 | DPDP notice and consent at the registration desk | 2 | opd-6a |  | OPD-N3 |
| opd-20 | Diagnosis code list load | 2 | opd-10 |  | OPD-N11 |

Rulings raised by this epic:

- (safety) OPD-S1 (new, patient safety): before the desk attaches a visit, or a clinician attaches an admission, to an EXISTING record found by search, which identifiers must be confirmed, and how? Recommendation: (a). Wrong-patient selection attaches someone else's allergies and history, and showing the stored DOB to a hospital-wide searcher would leak the discharge-link second factor. This also governs the Ward Record clerk's returning-patient screen.
- (safety) OPD-S2 (new, patient safety): when registration or an admission door finds a probable duplicate, should the server warn or block? Recommendation: (a). Duplicate charts split the allergy history, but blocking a sick walk-in at the counter is worse.
- (safety) OPD-S3 (new, patient safety, the analogue of held W111; does not decide W111): do vitals taken in OPD enter the observation chart and count as the latest vitals after conversion? Recommendation: (a). Otherwise hours-old OPD values become the board's latest reading and its NEWS2.
- (safety) OPD-S4 (new, patient safety, related to held R010): do active OPD prescriptions carry onto the inpatient medication chart at conversion? Recommendation: (a). The eMAR would otherwise offer a nurse a take-home prescription as an inpatient dose.
- (safety) OPD-S5 (new, patient safety): may the front desk record allergies or 'no known allergies' at registration? Recommendation: (a), which is how opd-4 is built. A clerk-typed NKDA would silence the interlock.
- (safety) OPD-S6 (new, patient safety, related to held R023): does an abnormal vital typed into an OPD consultation prompt anyone? Recommendation: (a) for now, because in this design only the consulting doctor enters OPD vitals and is the person who acts on them, and any automatic rule is decision-support wording that R023 governs. Revisit before any triage-nurse vitals entry is built.
- (safety) OPD-S7 (new, patient safety): how is 'asked, no known allergies' recorded, and may an OPD prescription be written when the patient's allergy status was never assessed? Recommendation: (a). The interlock cannot tell NKDA from never asked (allergy_interlock.py:360-366), and an OPD prescription leaves the building with nobody else checking it. The assessment never changes how the interlock screens.
- (safety) OPD-S8 (new, patient safety): when a discharged patient is readmitted onto the same record, what happens to medication rows from the earlier stay that are still 'active' or 'prn'? Recommendation: (a) now, and consider (b) later as its own discharge change. Discharge leaves medications 'active' and the eMAR's only guard is patients.status 'admitted' (emar_service.py:604-613), so without this every old drug becomes administrable again on readmission.
- OPD-N1: where does the UHID come from at each site, and in what format? Recommendation: Hospital mode by default (beside-HIS, Ward Record); orb mode with a check digit for a site Orb replaces; never reset per year; orb mode refused until the series is configured.
- OPD-N2: how far does an OPD visit open the patient's record to that clinic, for how long, and does a visit the reader registered themselves count? Recommendation: 7 days, extent (ii), self-registered (x). The inpatient chart, chart writes and FHIR export stay admission-only whatever is chosen. (iii) only through the existing inpatient scope until a clinician asks for it.
- OPD-N3: create a receptionist role; should it also take consent (F123) and money; and should admin hold registration.write? Recommendation: Receptionist with registration.read and registration.write, plus consent.record once counsel agrees. Money stays with the cashier role. Admin keeps registration.read only; registering patients is the desk's and the doctor's job.
- OPD-N4: may a ward-scoped account search the whole patient index (minimal fields) to avoid duplicate registrations? Recommendation: Yes: UHID, name, sex, age, phone last 4, last visit date and 'inpatient now' only; minimum 3 characters, 20 results, every search audited with the matched ids. The full record stays scoped.
- OPD-N5 (existing R018): must the desk capture date of birth? Recommendation: Optional, with stated age stored with its date and the 'no date of birth, no discharge link' hint. Never store an estimated DOB.
- OPD-N6: when is the consultation fee charged, and is there a registration fee or free follow-up window? Recommendation: Site setting, default completion. A follow-up window maps to its own OPD-FOLLOWUP code the site prices, never an implicit zero. Cancel and LWBS void a pending prepaid line.
- OPD-N7: what does the waiting-room token board show? Recommendation: Token, doctor and room only (DPDP), on a per-board device key.
- OPD-N8: may a visit be completed without a signed consultation note? Recommendation: Allowed with a reason, because many clinics will keep paper notes at first.
- OPD-N9: which queue priority categories apply? Recommendation: A site-configured list with a reason captured, shown on the doctor's queue.
- OPD-N10: what identity document data may registration store? Recommendation: Type plus last 4, never a full Aadhaar number; counsel to confirm.
- OPD-N11 (new): which diagnosis code list does OPD load, and under what licence? Recommendation: Free text plus optional code now (opd-10 works without a load). Counsel to confirm the licence terms of the chosen ICD-10 release before opd-20 loads it.
- OPD-N12 (new): may an unchanged OPD queue re-read skip writing a new phi_access row? Recommendation: (b). The disclosure is identical, and polling desks would otherwise write about 1,200 rows per screen per day, while audit already drives appliance storage.

### Bed master and ADT (28 increments, 78.5 engineer-days)

| Id | Increment | Days | Depends on | Safety | Held by |
|---|---|---|---|---|---|
| adt-0 | Opt-in module switch and the 'adt' shell entry | 1.5 |  |  |  |
| adt-1 | Bed master schema, single bed writer and Bed setup API | 4.5 | adt-0 |  |  |
| adt-2 | Movement ledger as a numbered migration, vocabulary by trigger | 2.5 |  |  |  |
| adt-3 | Admission location columns, bed lock, ADT writer core and reconciliation | 3 | adt-1, adt-2 | yes |  |
| adt-4a | Bed board API, Board view and the awaiting and unlinked lists | 4 | adt-3 | yes |  |
| adt-4b | Bed setup view | 3 | adt-4a |  |  |
| adt-5 | Admit into a master bed, or awaiting bed | 4 | adt-3, adt-4a | yes |  |
| adt-6a | Transfer and bed change through the master | 4 | adt-5 | yes |  |
| adt-6b | Bed swap and movement correction | 2.5 | adt-6a | yes |  |
| adt-6c | Relay agentic transfer lane through the ADT writer | 1 | adt-6a | yes |  |
| adt-7 | Housekeeping turnaround linked to ADT | 2.5 | adt-6a |  |  |
| adt-8a | End of stay with types, server read-back and de-duplicated dispositions | 5 | adt-3 | yes |  |
| adt-8b | Reopen a stay recorded in error | 2 | adt-8a | yes |  |
| adt-8c | Stay start and end helpers adopted by every summary-keyed reader | 2 | adt-8a |  |  |
| adt-9 | Discharge type, End of stay and Reopen screens | 3 | adt-8a, adt-8b, adt-4a | yes |  |
| adt-10 | Leave and absconded recording, 'Not on ward' shown on boards | 2.5 | adt-3, adt-4a | yes |  |
| adt-11 | Clinical lanes honour presence | 3.5 | adt-10 | yes | ADT-S4 (new safety ruling raised by this design) |
| adt-12 | Bed reservations | 2.5 | adt-5, adt-6a |  |  |
| adt-13a | Census service with a per-ward ledger start | 3.5 | adt-6a, adt-6c, adt-8a, adt-8c |  |  |
| adt-13b | NABH utilisation and outcome indicators from the ledger | 2 | adt-13a |  |  |
| adt-14 | Capacity screens from the bed register | 2 | adt-13a |  |  |
| adt-15 | Bed-day charges by bed class | 3 | adt-1, adt-6a, adt-8c |  |  |
| adt-16 | Strict locations (F104) | 1.5 | adt-5, adt-6a |  |  |
| adt-17 | Ward clerk role (read and same-department bed moves) | 3 | adt-6a, adt-10 | yes |  |
| adt-17b | Clerk admission and 'left the ward' departure | 4 | adt-17, adt-8a | yes | ADT-S5 and ADT-S3 (new safety rulings raised by this design) |
| adt-18a | Readmit an existing registered patient: core | 3 | adt-5, opd-registration (create-or-find patient increment) | yes |  |
| adt-18b | Readmission carry-over boundaries | 2.5 | adt-18a, adt-8c | yes | ADT-S9 (new safety ruling raised by this design) |
| adt-19 | Live bed board updates | 1 | adt-4a |  |  |

Rulings raised by this epic:

- (safety) ADT-S1. When the chosen bed already holds an admitted patient, what happens? Today it is warned and logged, never blocked (admission_service.py:120). The bed label is how nurses, the eMAR round and Sage find a patient. Recommendation: (a). Every patient keeps a unique bed label and an extra bed is counted honestly. Until ruled, the build ships (b), which never blocks a patient.
- (safety) ADT-S2. May a patient be admitted or moved into a bed housekeeping has not recorded clean, including one awaiting a terminal clean after an isolation patient? And into a bed recorded out of service? Recommendation: (c). Until ruled, the build warns and records, and needs a typed reason for an out-of-service bed. Wards that do not track cleaning raise no clean warning.
- (safety) ADT-S3. Who may end an admission, by type: routine departure after a doctor's discharge, LAMA, DAMA, absconded closure, death? May the Ward Record clerk mark 'left the ward' with no discharge summary? Recommendation: Death, LAMA and DAMA stay doctor-only (discharge.generate, scope, read-back). A nurse or clerk records the physical departure only after a doctor's discharge exists. The bed manager closes an absconded stay after 24 h (site setting), with police intimation noted for MLC cases. In the Ward Record profile the clerk may record 'left the ward' with the time.
- (safety) ADT-S4. While a patient is on leave or absconded, do they stay on clinical boards, and do obs-due timers, medication-due lists, escalation ladders, sepsis auto-start and the vitals simulator keep running? Recommendation: (a). Absconded patients with an open escalation also notify the treating doctor in Relay. Removing them recreates the orphaning failure.
- (safety) ADT-S5. May a non-clinical ward clerk create an admission, and what does the chart show for allergies and other clinical fields until a clinician completes them? Orb cannot tell NKDA from 'never asked' today (allergy_interlock.py:364). Recommendation: (a). Required for the Ward Record and front-office admission. 'Not yet assessed' must never print as NKDA.
- (safety) ADT-S6. A boarder lies on another department's ward. Scope follows the treating department, so the host ward's nurses cannot open the chart of a patient in their own beds. Whose board are they on? Recommendation: (a), built as one change to PATIENT_DEPARTMENT_SQL and its sage_service twin under the parity test. Until ruled, a boarder placement needs an explicit acknowledgement naming who will and will not see the chart, is audited as clinical_access and posts a case-room line; scope is unchanged.
- (safety) ADT-S7. Who may record leave and absconded (which, once S4 is built, pauses clinical timers), and do nurses get the Beds tab read-only for their ward? Recommendation: Doctors and nurses record leave and absconded; the clerk records them only in the Ward Record profile, where no timer runs. Nurses get the read-only board scoped to their ward, no master edits and no transfers. Until ruled, only admins and doctors hold presence and nurses get nothing new.
- (safety) ADT-S8. May a non-clinical clerk change a patient's treating department? That changes who can read the chart; frontdesk_routes.py:140-161 calls it 'not a bedside decision'. Recommendation: (b). Until ruled, the clerk holds ADT_BED only (moves that keep the treating department).
- (safety) ADT-S9. When a registered patient is readmitted, what carries over from the previous stay: the last vitals and risk row (which would show as 'latest' on the board, NEWS2 and Sage until a new set is charted), the Sage room memory, and the case room history? Recommendation: (a). The readmit entry point stays closed until this is ruled.
- F104 (open). Should the server refuse a ward or bed the hospital has not configured? Recommendation: Warn (keep typed text unlinked) until the bed manager marks setup complete, then turn strict on per site.
- ADT-P2. Does the Ward Record profile turn ORB_ADT on for its clerk pickers, moves and 'left the ward' (sales plan item 184)? Recommendation: (a): one ADT writer and no second free-text lane to retire. Re-cost the Ward Record admission work at about 54.5 engineer-days plus OPD create-or-find, not 11 to 17.5, and say plainly that clerk admission and 'left the ward' cannot ship until S5 and S3 are ruled, and the returning patient until S9.
- ADT-P3. How far back may admission, transfer and departure times be entered, and by whom? Recommendation: 72 h for ADT writers; older needs an admin and a reason; show both event time and recorded time.
- ADT-P4. What is the occupancy denominator and census method? Recommendation: Report both; the NABH indicator uses available beds and midnight census and states the method.
- ADT-P5. Should the capacity screens take bed counts from the master per ward once that ward's register is complete? Recommendation: Yes per ward, labelled 'from bed register'; ORB_WARD_BEDS stays the fallback.
- ADT-P6. On leave: is the bed held, is room rent charged, and how long before a leave becomes a discharge? Recommendation: Held; rent per site setting (default charged); leave over 48 h prompts the doctor to discharge.
- ADT-P8. On death, what is closed automatically: portal login, discharge link, diet orders, pending pharmacy orders, the Relay case room? Recommendation: Deactivate the portal login (restored only by a reopen), no discharge link, stop diet orders, Relay room read-only; pending pharmacy orders when the single medication-order store lands. Nothing deleted. Until ruled, adt-8a issues no link and leaves the login as today.
- ADT-P9. Default bed reservation expiry? Recommendation: 120 min for ER and transfer, 24 h for elective and post-op, site-configurable.

### Single medication-order store (23 increments, 78.0 engineer-days)

| Id | Increment | Days | Depends on | Safety | Held by |
|---|---|---|---|---|---|
| medorders-1 | Schema, flags, state machine, registries and the gate exclusion passthrough | 3.5 |  | yes |  |
| medorders-2 | Boot adoption and the parity report | 2.5 | medorders-1 | yes |  |
| medorders-3 | Chart lane write-through, the eMAR order-state check and the legacy verify mirror | 4 | medorders-1, medorders-2 | yes |  |
| medorders-4 | Medication-order API, scoping, the verify permission and the socket event | 3.5 | medorders-3 | yes |  |
| medorders-5a | One pharmacist verification: service and Verify Queue | 3.5 | medorders-4 | yes |  |
| medorders-5b | Helix hook, idempotent supply and the scoped custody link | 4 | medorders-5a | yes |  |
| medorders-5c | Pharmacy screens for the unified verification | 3 | medorders-5a, medorders-5b | yes |  |
| medorders-6a | Hold, resume and stop, and the readers that drive administration | 4 | medorders-4 | yes | MO-R2 (new, safety) |
| medorders-6b | Held state in presentation readers and the reader registry | 3 | medorders-6a | yes | MO-R2 (via medorders-6a) |
| medorders-7 | CPOE and order-set drugs reach the ward after a prescriber signs | 5 | medorders-4, medorders-5a | yes |  |
| medorders-8 | Sage handoff and Scribe extraction write through the store | 3 | medorders-3, medorders-5b | yes |  |
| medorders-9 | Interaction and duplicate screens and discharge reconciliation read the store | 3 | medorders-7, medorders-8 | yes | MO-R3 (new, safety); MO-R6 for the duplicate acknowledgement |
| medorders-10 | Lifecycle on the chart, the round, deliveries and the doctor's home | 5 | medorders-4, medorders-5a, medorders-7 | yes |  |
| medorders-11 | [Follow-on] Pharmacist verification before the first dose | 2 | medorders-5b | yes | MO-R1 (new, safety) |
| medorders-12 | [Follow-on] Verbal and telephone orders with read-back and countersign | 4 | medorders-7 | yes | R010 |
| medorders-13 | [Follow-on] Scribe-extracted medicines as drafts | 2 | medorders-8 | yes | W111 |
| medorders-14 | [Follow-on] Stat order completes on its first given dose | 1.5 | medorders-3 | yes | R001 |
| medorders-15 | [Follow-on] Substitution as an order amendment | 4 | medorders-5b | yes | R002 |
| medorders-16 | [Follow-on] Helix approve and Relay confirm lanes onto the store | 2.5 | medorders-5b, medorders-8 | yes | R010 (and W099 for the Relay half) |
| medorders-17 | [Follow-on] Medication reconciliation at admission, transfer and discharge (W105) | 5 | medorders-9, adt-1 | yes | MO-R7 (new, safety); the discharge gate itself is R009 |
| medorders-18 | [Follow-on] Retire the legacy write paths (single writer) | 2.5 | medorders-8, medorders-9, medorders-16 | yes | R010 (through medorders-16) |
| medorders-19 | [Follow-on] Prescriber-authored administration schedule (W117) | 5 | medorders-7 | yes | MO-R8 (new, safety) |
| medorders-20 | [Follow-on] Exact formulary binding for the Sage lane | 2.5 | medorders-8 | yes |  |

Rulings raised by this epic:

- (safety) MO-R1: Must every medication order be verified by a pharmacist before its first dose? Today only AI or agentic sources need it. Recommendation: (a), with a retrospective window set by the site pharmacist (suggest before the next scheduled dose). Follow-on medorders-11.
- (safety) MO-R2: Who may hold, resume, stop, and cancel a live medication order? Today no route does any of these. Recommendation: (a)
- (safety) MO-R3: Should a drug charted as PRN be givable (today it shows on the round but every dose is refused, emar_service.py:556)? And should the order-time interaction and duplicate screen include PRN, held and signed CPOE orders, but not unsigned drafts? Recommendation: (a)
- MO-R4 (W178): Should an administrator keep the ability to verify medication orders? Today they can, through helix.write. Recommendation: (a). In code it stays granted to admin until you rule.
- (safety) MO-R5: Should acknowledging a SEVERE allergy alert at order time need medication.prescribe on every lane, as the bedside override has since 2026-08-15? Today anyone with medication.write or order.write can type a reason (medication_safety_gate.py:250), including a nurse on the chart lane. Recommendation: (a). The store's sign step gives (b) by construction, but the nurse chart lane still allows (c) and overlaps the held R010 and R011, so this ruling stays open.
- (safety) MO-R6: At order time the same ingredient is already on a live order. Notice, 428 acknowledgement with a reason, or block? Recommendation: (b). Stat top-ups and route changes are legitimate.
- (safety) MO-R7: What happens to live medication orders at discharge, death or transfer? Recommendation: (a). The discharge gate itself is R009, which stays held.
- (safety) MO-R8 (W117): Standard administration times per frequency (for example TDS 08/14/20 or 06/14/22)? Recommendation: (a), with no built-in default.
- (safety) MO-R9: If lifecycle mode is switched off after use, held orders stay ungivable (the bedside check follows the order link, not the flag) and linked CPOE orders can only be cancelled through Orders (all other transitions answer 409). They must be re-prescribed from the chart. Accept? Recommendation: (a), with an operations note before any site turns lifecycle on.
- (safety) MO-R10: What happens to drafts nobody signs, for example order-set drafts? Recommendation: (a)
- MO-R11: May a CPOE medication order name a drug that is not on the formulary? Recommendation: (a) until the GRN drug master lands.
- MO-R12 (new): Which orders need a pharmacy supply request, and which are given from ward stock or the patient's own supply? Until ruled, Orb creates no automatic supply row for chart or CPOE orders; only today's lanes (Sage handoff) and an explicit 'Request from pharmacy' create one. Recommendation: (a). It does not change who may give a dose (that is MO-R1), only whether the pharmacy queue sees the order.
- MO-R13 (new): After a patient's erasure, should the medication-order ledger (drug names, doses, reasons) be deleted with the record, or retained? Recommendation: (a). The design builds (a); (b) is a one-line registry change plus a DELETE trigger if you choose it.
- (safety) HELD R010: Who is the prescriber of record for a Sage-drafted drug, and may nurses enter medication orders? Recommendation: Held; not decided here. Nurses keep today's chart lane (including acknowledging severe alerts there) and can create CPOE drafts. Follow-on medorders-12, 16 and 18 wait.
- (safety) HELD R001: Should the server block a second administration of a stat order? Recommendation: Held. Follow-on medorders-14 waits.
- (safety) HELD R002: May a pharmacist substitute without the prescriber, and from which list? Recommendation: Held. Day one refuses any non-same-ingredient binding at verification instead of deciding; Helix substitution is unchanged; follow-on medorders-15 waits.
- (safety) HELD R011: May a nurse override a bedside dose block, and must a moderate same-drug re-challenge carry a reason before the order is written? Recommendation: Held. No increment changes the bedside override path.
- (safety) HELD W111: Should Scribe-extracted data be proposed rather than live? (medication half) Recommendation: Held. Extracted drugs stay 'prescribed, needs pharmacist'; follow-on medorders-13 waits.
- (safety) HELD R024: When a Relay confirm races a dismissal, what happens to the pharmacy order? Recommendation: Held. Every store write has a function that joins the caller's transaction, so either answer works.
- (safety) HELD R009: Discharge block or warn for a critical patient? Recommendation: Held. MO-R7 is only about what happens to orders; the gate stays R009.
- W099 (open): Wire or retire the agentic confirm and pre-flight routes? Recommendation: Not decided here. Follow-on medorders-16 follows the choice.

### Pharmacy inventory and goods receipt (23 increments, 69 engineer-days)

| Id | Increment | Days | Depends on | Safety | Held by |
|---|---|---|---|---|---|
| grn-1 | Create Helix tables inside the migration runner | 1 |  |  |  |
| grn-2 | Opt-in module switch, permission constants, admin-only Pharmacy Store skeleton | 2 |  |  |  |
| grn-3 | Pharmacy store schema, ledger backfill and reseed wiring | 5 | grn-1 |  |  |
| grn-4 | One writer for drug stock; dispense refactor, race fix and dispense audit | 4 | grn-3 | yes |  |
| grn-20 | Procurement connection-taking cores and domain guards | 3 | grn-3 |  |  |
| grn-5 | Pharmacy masters: drug fields, classification, stores and adoption, par levels, vendors, loader guard | 4 | grn-2, grn-4, grn-20 | yes |  |
| grn-6 | Add a drug and govern its clinical identity | 3 | grn-5 | yes | GRN-RS1 |
| grn-7 | Pharmacy purchase orders | 3 | grn-5, grn-20 |  |  |
| grn-8 | Pharmacy goods receipt: batch, expiry, MRP, free quantity, supplier invoice | 5 | grn-4, grn-7 | yes |  |
| grn-9 | Stock, ledger, reorder and near-expiry views | 3 | grn-4, grn-5 |  |  |
| grn-10 | Quarantine, recall, recall trace and write-off | 4 | grn-8, grn-9 | yes | GRN-RS2 (release leg only; the rest can ship) |
| grn-11 | Pharmacy-internal transfers, including NDPS vault to dispensing | 3 | grn-4, grn-5, grn-9 | yes |  |
| grn-12 | Stock take and adjustments | 3 | grn-9 |  |  |
| grn-13 | NDPS register, witness and reconciliation | 5 | grn-5, grn-8, grn-11 | yes | GRN-RS7 (to enable the register; the build can proceed) |
| grn-14 | Dispense quantity and batch choice at dispense | 2 | grn-4 | yes |  |
| grn-15 | Ward returns of dispensed medicines | 3 | grn-4, grn-10, grn-14 | yes | GRN-RS3 (restock leg); GRN-R8 (nurse recording) |
| grn-16 | Pharmacy charges at batch MRP | 2 | grn-14, grn-15 |  |  |
| grn-17 | Ward stock consumption from eMAR administrations | 3 | grn-21, mos-* | yes |  |
| grn-18 | Seeds, retention mapping and operator docs | 2 | grn-8, grn-11 |  |  |
| grn-19 | Pharmacist desk access to Pharmacy Store | 1 | grn-2 |  | GRN-R7, GRN-R9 |
| grn-21 | Ward indents, ward imprest stores and ward receipt | 4 | grn-11, grn-20 | yes | GRN-RS5 (enabling ward stock); GRN-R8 (nurse grants) |
| grn-22 | Return to vendor (later wave) | 2 | grn-10, grn-20 |  |  |
| grn-23 | Crash-cart stores and checks (later wave) | 2 | grn-21 | yes | GRN-RS5 |

Rulings raised by this epic:

- (safety) GRN-RS1: Who may add a drug to the formulary, or change a drug's clinical identity (generic name, strength, form)? generic_name drives allergy and interaction screening. Recommendation: (b), with the class preview shown before save. Held for Samuel.
- (safety) GRN-RS2: Can a quarantined batch be released back to dispensable stock, and by whom? Are recalled or NSQ batches ever releasable? Recommendation: (b). Held for Samuel.
- (safety) GRN-RS3: May medicines returned from a ward re-enter dispensable stock? Recommendation: (b). Held for Samuel.
- (safety) GRN-RS4: Should a batch be dispensed when it expires within days, possibly before the supply runs out? Recommendation: (b), N a site setting defaulting to 30 days, until W117 gives a supply end date. Not built by this epic until ruled.
- (safety) GRN-RS5: Which drugs may be held as ward imprest or crash-cart stock, and does administration from ward stock still need an active eMAR order? Recommendation: (b), starting the barred list from drug_safety_service's ISMP set. ORB_PHARMACY_WARD_STOCK stays off until ruled. Held for Samuel.
- (safety) GRN-RS6: How is expiry entered at GRN, given that a typo can make an expired batch dispensable? Recommendation: (b) now, (a) when scanners exist. Held for Samuel.
- (safety) GRN-RS7 (new): When the NDPS register write fails, or would leave a negative balance, during a dispense of a controlled drug, what happens to the dispense? Recommendation: (b), so a bookkeeping fault never withholds analgesia while the variance is still captured in the same transaction. Both modes are built; the register cannot be enabled until this is ruled. Held for Samuel.
- GRN-R7: Who holds the pharmacy-store permissions? Should a storekeeper role exist? Recommendation: Pharmacist read, write and ndps; admin read and approve, dropping write after this ruling; no new role in v1.
- GRN-R8 (W128): May nurses raise ward indents, see their ward's stock, receive ward transfers and record returns? Recommendation: (a).
- GRN-R9: May the pharmacist have a Pharmacy Store tab that shows purchase values, breaking the 'no money screen on a clinical rail' pin? Recommendation: (a). The pharmacist already holds procurement.write through the API.
- GRN-R10: Must a vendor have a valid drug licence on file before a pharmacy PO or GRN? Recommendation: Refuse, and warn 30 days before expiry.
- GRN-R11: Separate number series for pharmacy documents? Recommendation: Separate, configured by the site before first use.
- GRN-R12: Drug costing and billing price. Recommendation: Lot cost; bill the dispensed drug at batch MRP, with the tariff only where no MRP exists. Coordinate with the billing owner.
- GRN-R13: Who decides dispense quantity until W117 exists? Recommendation: Pharmacist-entered and bounded.
- GRN-R14: NDPS controls: where narcotics are dispensed from, who may witness, what a variance triggers, and whether seeds may hold NDPS stock. Recommendation: Vault-only receipt. Dispense directly from the vault in v1, with the cabinet option available through transfers. Second-user PIN witness. Every variance drafts an incident. No NDPS stock in seeds. Confirm form layouts with W170.
- GRN-R15: How is ward stock consumption accounted before eMAR links to the drug master? Recommendation: Count-based top-up now; grn-17 after the mos epic, never blocking a dose.
- GRN-R16: Near-expiry window, alert delivery and minimum shelf life at receipt. Recommendation: 90 days, screen only, refuse receipts with under 90 days left unless approve tier overrides.
- GRN-R17: Satellite pharmacies as dispensing points in v1? Recommendation: One per site in v1.
- GRN-R18: May a drug with no schedule or NDPS classification be purchased or received? Recommendation: Refuse. Classification never gates a dispense of existing stock.
- GRN-R19: Should the drug ledger be append-only by trigger? Recommendation: The latter. Ledger rows reference patient orders and are wiped on reseed.
- GRN-R20 (new): Who may downgrade a drug's NDPS or schedule classification? Recommendation: (b). An upgrade applies at once, because it only adds control.

### Laboratory information system (29 increments, 92.0 engineer-days)

| Id | Increment | Days | Depends on | Safety | Held by |
|---|---|---|---|---|---|
| lis-1 | Opt-in lab switch, module state (on, draining, off) and Lab tab skeleton | 2.5 |  |  |  |
| lis-2 | Lab roles and lab.* and ingest.reconcile permissions | 2 | lis-1 |  |  |
| lis-3 | LIS contract migration, test master service and starter catalogue | 4 | lis-1, lis-2 | yes | LIS-R2, LIS-R7, LIS-R18 (go-live) |
| lis-4 | Lab setup screens (tests, panels, ranges, critical limits, rejection reasons) | 3 | lis-3 | yes | LIS-R1, LIS-R2 (go-live) |
| lis-5 | Observations lab links, one julianday-ordered reader, chart card fix | 3.5 |  | yes |  |
| lis-6 | Order intake, specimen planning and accession numbers | 4 | lis-3 | yes | LIS-R13, LIS-R18 (go-live) |
| lis-7 | Order lifecycle guard, lab-mediated cancel, completion rule, OrdersPanel | 4 | lis-6 | yes | LIS-R16 (go-live) |
| lis-8 | Specimen labels and collection (ward Samples view, phlebotomy list) | 4 | lis-7 | yes | LIS-R6 (go-live) |
| lis-9 | Specimen receipt, rejection and recollection | 3 | lis-8 | yes |  |
| lis-10 | Section worklists and manual result entry with flags | 4.5 | lis-9 | yes | LIS-R2, LIS-R7 (go-live) |
| lis-11 | Technical validation, authorisation and release into observations | 4.5 | lis-10, lis-5, lis-7 | yes | LIS-R5, LIS-R16 (go-live) |
| lis-12 | Delta checks | 2.5 | lis-10, lis-5 | yes | LIS-R9 (go-live) |
| lis-13 | Amended and withdrawn results with versions | 2.5 | lis-11 | yes | LIS-R10 (go-live) |
| lis-14 | Lab report printing and chart reports list | 3.5 | lis-11 | yes |  |
| lis-15 | Lab critical notification: shared case-room delivery, callback log, tiers (lab side) | 5 | lis-11 | yes | LIS-R1, LIS-R3, LIS-R4, LIS-R19, LIS-R20 (go-live) |
| lis-16 | Ward critical card and in-app read-back acknowledgement | 3 | lis-15 | yes | LIS-R20 (go-live), LIS-R3 (go-live) |
| lis-17 | Open critical lab results on the Vigil board and chart | 2 | lis-16 | yes | R023 (build) |
| lis-18 | Turnaround time tracking and lab dashboard | 3 | lis-11 |  |  |
| lis-19 | Unmatched results quarantine (W109) with clinical reconciliation | 3.5 | lis-2, lis-5 | yes | LIS-R17 (go-live) |
| lis-20 | External corrected results supersede the prior final | 2 | lis-5 | yes |  |
| lis-21 | External ORU results linked to CPOE lab orders | 2.5 | lis-5, lis-7, lis-20 | yes | external input: real site ORU sample (W108) |
| lis-22 | Critical callback for interfaced results (OBX-8 HH/LL/AA) | 3 | lis-15, lis-16 | yes | LIS-R22, LIS-R1, LIS-R3 (go-live) |
| lis-23 | Analyser registry, instrument credentials and test-code map | 3 | lis-3 | yes |  |
| lis-24 | Analyser framework core, HL7 v2 adapter and HTTP intake | 4.5 | lis-23, lis-10, lis-19 | yes | LIS-R8, LIS-R14 (go-live) |
| lis-25 | MLLP listener for lab analysers | 2.5 | lis-24 | yes |  |
| lis-26 | DEFERRED: ASTM E1381 framer and E1394 record parser | 4 | lis-24 | yes | external input: site analyser list and protocol documents |
| lis-27 | DEFERRED: ASTM TCP transport and host query | 3 | lis-26, lis-25 | yes | external input: site analyser list and protocol documents |
| lis-28 | DEFERRED: analyser simulator for demos (honesty-labelled) | 1.5 | lis-25 |  |  |
| lis-29 | DEFERRED: patient portal lab reports | 2 | lis-14 | yes | LIS-R11 (build) |

Rulings raised by this epic:

- (safety) LIS-R1: Does Orb ship any default critical limits, and who approves the site's critical value list? Recommendation: (a), with a printable worksheet of common critical analytes and blank limits. W083 and W108 already call this NABH policy.
- (safety) LIS-R2: Does Orb ship default reference ranges? Recommendation: (a). Ranges depend on analyser, method and population.
- (safety) LIS-R3: Critical callback policy: who is called, how fast, and what closes a callback? Recommendation: Tier 1 ward nurse in charge plus the ordering doctor via the case room, 15 minutes; Tier 2 attending or duty doctor, 15 minutes; Tier 3 duty medical officer. The site checks timings against its NABH policy. Close by (i) or (ii), never (iii). An undelivered post (no case room) counts as tier due at once.
- (safety) LIS-R4: When is a critical value communicated, and is the unauthorised value visible? Recommendation: (a). NABL practice; waiting for a visiting pathologist delays a potassium of 7.
- (safety) LIS-R5: May one person enter, technically validate and authorise the same result? Recommendation: (a), with (b) available. Until ruled, the setting ships ON (two people), the strictest default.
- (safety) LIS-R6: Patient identification and timing at collection, with no wristband barcodes yet Recommendation: Confirm name and UHID on screen before labels print and print at the point of collection; batch pre-printing only at the lab's own counter; back-dating up to 2 hours with a reason. Until ruled, back-dating is refused.
- (safety) LIS-R7: Which range applies when date of birth or sex is not recorded? Recommendation: (a). Only ranges covering every age (or sex 'any') still apply. Critical limits still apply when the approved limit does not depend on age or sex. patients.age is never used because it is static.
- (safety) LIS-R8: Autoverification of analyser results Recommendation: (a). Every result gets human technical validation and authorisation, which also keeps the LIS clear of IVD-software arguments while R023 is held.
- (safety) LIS-R9: Delta checks: what a flag does, and who sets thresholds Recommendation: (a). Thresholds per analyte by the lab director; none ship active; comparator values are never delta-checked.
- (safety) LIS-R10: Who is told when a released result is amended or withdrawn? Recommendation: (a), plus a NABH incident prompt when a wrong-patient result is withdrawn.
- (safety) LIS-R11: Should patients see LIS lab reports in the portal? Recommendation: Off by default; (c) when a site turns it on; flags as printed text with no colour, matching the portal vitals ruling.
- LIS-R12: Which new staff roles, and how much do lab roles reach? Recommendation: The three roles; 'pathologist' covers any authorised signatory; hospital-wide, with no Relay, Sage or patient.read_all; nurses and doctors can also collect.
- LIS-R13: Accession number format Recommendation: 12 digits: YYMMDD of the hospital-local day (rollover at local midnight), a 5-digit sequence, then a Damm check digit (catches every single-digit error and adjacent transposition, unlike plain mod-10 or Luhn on 09/90). Numeric only for older analysers and Code 128C. Refuse loudly at 99999, never wrap. Optional site prefix.
- LIS-R14: How far does the analyser layer go? Recommendation: (a). A hospital's existing middleware is accepted as an HL7 source; each analyser takes 1 to 3 field days with the vendor's document.
- LIS-R15: Label and scanner hardware Recommendation: Browser printing to 50 x 25 mm thermal printers at each ward station and the lab, USB keyboard-mode scanners, procured by the hospital; no print server in v1.
- (safety) LIS-R16: When does an order show 'resulted', and are partial panels visible? Recommendation: (a), with pending analytes shown as pending on the report and rejected items blocking completion until recollected or cancelled.
- (safety) LIS-R17: Who may attach an unmatched result to a patient, or discard it? Recommendation: (a) now, (c) later; never (d), because attaching is a wrong-patient decision that feeds Tier 1, the chart and the ward pack. A site without lab staff keeps ORB_RESULT_QUARANTINE off.
- (safety) LIS-R18: Which tests are done at the bedside and stay outside the LIS? Recommendation: Ship those three as ward_poct, because the DKA and asthma order sets repeat them hourly at the bedside; the lab director reclassifies per site (for example ABG run in the lab). Reclassification applies to orders activated afterwards.
- (safety) LIS-R19: Who receives a lab critical for a patient who has been discharged or is mid-transfer? (Sits beside held R009 and does not decide it.) Recommendation: (c) until a site policy says otherwise; notifications are never auto-closed by discharge or transfer. Whether discharge should warn about an open lab critical is part of R009 and is left to that ruling.
- (safety) LIS-R20: Do the lab critical surfaces (the Laboratory Relay post and the ward CriticalLabCard) fall inside held R023's intended-use scope? Recommendation: Samuel decides. The design presents them as lab communication (option a) and builds them behind ORB_LAB, but marks go-live as blocked on this ruling so R023 is not decided by implication. lis-17 (Vigil board chip) stays build-blocked on R023.
- (safety) LIS-R21: What happens to LIS work when ORB_LAB is switched off? Recommendation: (a). Hard off hides open criticals and half-processed specimens.
- (safety) LIS-R22: Should Orb raise a critical callback from an external lab's OBX-8 HH/LL/AA flag (the W083 follow-up)? Recommendation: (a), off by default; Orb relays the sending lab's own flag and never computes one; the external lab's own phone call is logged as the ward's read-back acknowledgement.
- LIS-R23: Should lab critical delivery be part of the pilot's shadow evidence? Recommendation: (a). The pilot's headline metric is NEWS2-based; revisit with a planned shadow schema change.

### Multi-site support (23 increments, 88.0 engineer-days)

| Id | Increment | Days | Depends on | Safety | Held by |
|---|---|---|---|---|---|
| multisite-1 | Facility spine, departments master, flag and runtime fence (no behaviour change) | 4 |  |  |  |
| multisite-2 | Facility identity, supplier snapshot and the Facilities screen | 4.5 | multisite-1 |  |  |
| multisite-3 | Departments and admissions carry the facility | 3 | multisite-1 | yes | MS-R4 (new, not held: department, ward and bed codes unique across the group) |
| multisite-4 | Encounters carry the facility; roster import guards | 3.5 | multisite-3, multisite-5a | yes |  |
| multisite-5a | Facility scope core, independent of the user loader | 5 | multisite-1 | yes |  |
| multisite-5b | Safety nets: SQL-site registry ratchet, real fence list, leak sweep | 4 | multisite-5a | yes |  |
| multisite-6 | Users, enterprise grants and the shell chip | 3.5 | multisite-5a | yes |  |
| multisite-7a | Document numbering per facility | 3 | multisite-1, multisite-2 |  |  |
| multisite-7b | Money, stores and assets carry the facility | 3.5 | multisite-7a |  |  |
| multisite-8 | Scope sweep: clinical lanes | 5 | multisite-5b | yes |  |
| multisite-9a | Scope sweep: pharmacy queue, money and front desk | 4.5 | multisite-5b, multisite-7b | yes |  |
| multisite-9b | Scope sweep: remaining administrative lanes | 4 | multisite-5b, multisite-7b | yes |  |
| multisite-10 | Scope sweep: Sage lane, out-of-scope locator and socket rooms | 4.5 | multisite-5b | yes |  |
| multisite-11 | Audit attribution and every audit reader | 4.5 | multisite-5b | yes |  |
| multisite-12 | Command Center, capacity and NABH indicators per facility | 4 | multisite-5b, multisite-7b, adt-1 |  |  |
| multisite-13 | Price lists per facility | 3 | multisite-7b, multisite-2 |  |  |
| multisite-14 | Pharmacy stock and dispensing per facility | 4 | multisite-9a, grn-1, medorder-1 | yes |  |
| multisite-15 | Downtime packs, exports and backup statements per facility | 3.5 | multisite-4, multisite-5b | yes |  |
| multisite-16 | Ingestion sources bound to a facility | 3 | multisite-3, multisite-5a | yes |  |
| multisite-17 | Group identity match at registration | 3 | multisite-5a, opd-1 | yes |  |
| multisite-18 | Two-facility go-live drill and runbook | 3 | multisite-3, multisite-4, multisite-6, multisite-7b, multisite-8, multisite-9a, multisite-9b, multisite-10, multisite-11, multisite-12, multisite-15, multisite-16 |  |  |
| multisite-19 | Visiting consultants: one account, session facility switch | 4 | multisite-6, multisite-10 | yes |  |
| multisite-20 | Inter-facility transfer workflow | 4 | multisite-8, medorder-1, adt-1 | yes | R009 (held); MS-R3 (new) |

Rulings raised by this epic:

- MS-R1. Tenancy model: one appliance per legal entity with a row-level facility_id, or one appliance per facility with a federated patient index? May different legal entities ever share an appliance? Recommendation: (a). It is one DPDP data fiduciary, it keeps CIO questionnaire 1.5 true, and consultants get one login. facilities.uuid keeps federation possible later.
- (safety) MS-R2. Once a patient is in your scope, do you see their records from encounters at other facilities in the group? Recommendation: (a). Never filter allergies, active medications or problems by facility; hiding them is how a known anaphylaxis is missed at the second site.
- (safety) MS-R3. When a patient moves between facilities (transfer, or roster import re-admission or department change), what happens to active medication orders, escalation chains, Sepsis bundles and diet orders? Recommendation: (c) until ruled, then (a), in line with the R009 recommendation. Until then every move into another facility is refused or reported and audited.
- (safety) MS-R4. Must department names and ward and bed codes be unique across the group, or may two facilities both have 'ICU'? Recommendation: (a). It isolates ward-scoped staff without touching the scope call sites, and it removes wrong-patient risk in bed-based lookups. (b) roughly doubles the estimate.
- MS-R5. Which patients may a facility-bound department-less account (admin, pharmacist, cashier) open? Recommendation: (a). (b) gives permanent chart access, including to a patient now admitted elsewhere. State the rule in the CIO questionnaire.
- MS-R6. Who may hold group-wide (enterprise) access? Recommendation: (a), matching the F109 recommendation of no hospital-wide clinical accounts. (b) only if MS-R9 is ruled (b).
- MS-R7. Visiting consultants at two facilities: one account with a per-session facility switch, or separate accounts? Recommendation: (a), built in multisite-19; (b) for the first go-live.
- (safety) MS-R8. In v1, must all facilities on one appliance share one timezone and one escalation ladder? Recommendation: One zone (all Indian sites are IST). Per-facility ladders unless every clinical lead signs the shared one; a ladder that pages a role a satellite does not staff at night stalls.
- MS-R9. Is pharmacy stock and the verify queue per facility, or one central pharmacy for the group? (F109 recommended keeping pharmacy hospital-wide; under (a) that means facility-wide.) Recommendation: (a). Drug licences are per premises and stock must be where the patient is. (b) would need MS-R6 (b).
- (safety) MS-R11. Patient identity across facilities: one group MRN per person, or per-facility MRNs linked to one person? Recommendation: (a). Duplicates across sites split the allergy list. Matching and merge belong to the OPD/MPI epic; multisite-17 supplies the group-wide identity match.
- MS-R12. Do we accept that restoring a backup rolls back every facility on the appliance? Recommendation: (a). State it in the CIO questionnaire and on Data and Backups.
- (safety) MS-R13. When a clinician names a patient in Sage who exists only at another facility, what does Sage say? Recommendation: (a). Today's department notice exists so a clinician never concludes a record is missing; naming the facility in (a) keeps that distinction without turning Sage into a group-wide existence lookup.
- (safety) MS-R14. In a multi-facility install, what happens to monitor or lab data from a source not bound to a facility, or whose identifier matches only a patient at another facility? Recommendation: (a). A vital filed on the wrong patient is worse than a vital held for review.

Full designs, critiques and revisions: the design workflow journal (wf_79565c1a-659); per-epic JSON copied to docs/his_design/.
