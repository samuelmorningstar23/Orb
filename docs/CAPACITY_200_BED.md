# Orb at 200 Beds: Storage and Appliance Sizing

*Written 7 September 2026. Hardware and Apple India pricing checked against
apple.com the same day, on the M5 generation announced for 22 September.
Companion to [HARDWARE_SPEC.md](HARDWARE_SPEC.md),
which specifies the 60-bed pilot ward. This document answers a different
question: what does a whole 200-bed hospital need, at two levels of activity.*

The 200-bed mid-size hospital is the base case the five-year financial model is
built on (`fundraising/ORB_FINANCIAL_MASTER.md`, section on base-case segment).
Until now there was no hardware specification for it. This is that document.

Two scenarios, as asked:

| | A | B |
|---|---|---|
| Occupancy | 97.5% (195 beds full, always) | 75% (150 beds) |
| Staffing | Full | 75% |
| OPD per day | 700 | 400 |
| Monitor-integrated beds | 40 | 30 |
| Named users on the system | 330 | 250 |
| Peak concurrent sessions | ~170 | ~110 |

---

## The short answer

| | A: busy, full staff | B: medium, 75% staff |
|---|---|---|
| **Internal storage, 3-year life** | **4 TB** | **2 TB** |
| **Internal storage, year 1 only** | 2 TB | 2 TB |
| Primary | Mac Studio **M5 Ultra**, 96 GB | Mac Studio **M5 Ultra**, 96 GB |
| Standby | Mac Studio **M5 Max**, 64 GB, 2 TB | Mac Studio **M5 Max**, 64 GB, 2 TB |
| Backup drives | 3 x 4 TB | 3 x 2 TB |
| UPS | 2000 VA / 1600 W | 1500 VA / 1000 W |
| Second AI machine | Needed, blocked on code (section 7) | Optional |

The memory figure does not move between 60 beds and 200 beds, and that surprises
people, so it is worth saying plainly: the 30 GB model working set is the same
whether the hospital has 60 patients or 200. Beds do not add models. 96 GB is
the right number at both sizes, for the reasons in HARDWARE_SPEC section 3.

What does move is storage, and what breaks is the AI tier's throughput. Those
are the two findings below.

---

## 1. Where the bytes actually go

Measured in the development database on 7 September 2026, not estimated:

| Table | Rows | Payload bytes/row |
|---|---|---|
| `audit_logs` | 93,964 | 524 |
| `shadow_events` | 93,574 | 187 |
| `vitals` | 13,883 | **91** |

On-disk cost is 1.95x payload for this schema, measured as live pages (146.0 MB)
over summed payload (75.0 MB) across 336 indexes. `audit_logs` carries six of
those indexes on its own.

**The audit log is the storage driver, not vitals.** An audit row costs about
1.0 KB on disk against 178 bytes for a vitals reading, roughly six times as
much. And audit volume scales with *staff activity*, which is exactly the axis
these two scenarios differ on. This inverts the intuition the pilot spec was
built around, where vitals ingestion was the thing being sized for.

Audit volume is bounded by design rather than by traffic: read-only GETs are
deliberately not logged by `services/audit_middleware.py` (too high-volume), so
polling does not generate audit rows. Only mutations and explicit `phi_access`
points do. Without that decision these numbers would be roughly ten times worse.

### Derived daily volumes

| | A | B |
|---|---|---|
| Audit rows/day | 55,950 | 41,250 |
| Vitals rows/day | 29,730 | 22,080 |
| Clinical rows/day (orders, meds, notes, billing) | 13,762 | 8,925 |
| **Database growth** | **102 MB/day, 37 GB/yr** | **74 MB/day, 27 GB/yr** |

Vitals are dominated entirely by monitor integration: 40 integrated beds at one
reading every two minutes is 28,800 rows/day, against 930 for the 155 beds on
manual entry. If the hospital integrates no monitors, vitals become a rounding
error and the database grows about 15% slower.

### Media, which is the largest and least certain term

HARDWARE_SPEC section 11 lists uploaded media as unmeasured. It is still
unmeasured, and at 200 beds it is now the biggest line, so here is a range
instead of a number. Orb is not a PACS: there is no DICOM path and no study
storage. Media is photographs and scans (X-ray images, ECG traces, report
scans, consent forms) capped at 25 MB each by `routes/lens_routes.py`, with no
downscaling on upload.

| Attachments/day | A | B |
|---|---|---|
| Low (60 / 30) | 33 GB/yr | 14 GB/yr |
| **Mid (150 / 80)** | **82 GB/yr** | **38 GB/yr** |
| High (400 / 200) | 219 GB/yr | 95 GB/yr |

At the mid case media is more than twice the database. The first week of
shadow mode should measure this before the second appliance is ordered.

---

## 2. How long the data legally has to be kept

**I am not a lawyer and this is not legal advice.** Counsel's written view on
retention is already an open pre-go-live item in
`CIO_SECURITY_QUESTIONNAIRE.md` (item 5 of the closing table). What follows is
the working position Orb already encodes in `retention_policies`, seeded by
`migrations.py:_DPDP_RETENTION_SEED`, with the gaps I can see in it.

### The short version

**The floor is 3 years. The number to size storage against is 8 years, and some
records never become deletable at all.**

There is no single Indian statute setting one hospital-wide record retention
period. It is a patchwork, and the binding pieces for a private hospital are:

| Rule | Period | What it covers |
|---|---|---|
| Indian Medical Council (Professional Conduct, Etiquette and Ethics) Regulations 2002, Reg. 1.3.1 | **3 years** from commencement of treatment | Indoor patient records, the general baseline |
| Limitation Act 1963 | 3 years | The civil claim window the 3-year figure is really protecting against |
| Limitation Act 1963, s.6 (legal disability) | Until majority **plus** 3 years | A minor's record, so effectively **to age 21** |
| Consumer Protection Act 2019, s.69 | 2 years from cause of action | Medical negligence complaints |
| Drugs and Cosmetics Rules 1945, Rule 65 | 3 years | Prescription and Schedule H1 dispensing registers |
| PCPNDT Act 1994, s.29 | 2 years, or until proceedings conclude | Prenatal diagnostic records |
| Medico-legal cases and anything under legal hold | Indefinite | Until proceedings and appeals close |
| DPDP Act 2023, s.8(7) | The other direction | Erase once the purpose is served, unless a law requires retention |

The last row is the one people miss. DPDP pushes *toward* deletion. Keeping
everything forever "to be safe" is not automatically defensible under it, which
is why Orb records a disposition and a cited authority per category rather than
a single global number.

### What Orb encodes today

`retention_policies` is deliberately mutable, so a site whose counsel disagrees
can correct it without a code deploy.

| Category | Retention | Erasable |
|---|---|---|
| `clinical_record` | 1,095 days (3 yr) from last clinical activity, **or age 21 if the patient was a minor** | No |
| `prescription_and_dispensing` | 1,095 days, **or age 21 if the patient was a minor** | No |
| `imaging` | 1,095 days (PCPNDT floor is 2, Orb applies 3), **or age 21 if the patient was a minor** | No |
| `medico_legal_record` | Indefinite, until the hold lifts | No |
| `audit_trail` | **2,920 days (8 yr)** | Never |
| `breach_record` | 2,920 days (8 yr) | Never |
| `consent_record` | Indefinite | Never |
| `ai_interaction_transcript` | 365 days | Yes |
| `ambient_audio` | **30 days** after the note is signed | Yes |
| Portal accounts, non-clinical contact, marketing | No fixed period | Yes |

Two of those deserve a note. The **audit trail is never erasable** because the
rows are SHA-256 chained: removing one destroys tamper-evidence for every row
after it. And **raw dictation audio is deleted at 30 days**, on the reasoning
that the signed note is the medical record and the recording is working
material. That is the single best storage decision in the schema.

### The minority protection, added 7 September 2026

The most serious gap in this list has been closed. The flat three-year rule
would have allowed a paediatric record to be listed as destroyable years before
the claim window closed, because under Limitation Act s.6 limitation does not
start running until the child turns 18. `retention_review` now holds those
records back:

| Behaviour | Detail |
|---|---|
| Protected until | Age 21, configurable via `ORB_MINOR_RETENTION_AGE` |
| Categories covered | `clinical_record`, `prescription_and_dispensing`, `imaging` |
| Not covered | Consent-basis categories. A child has the same erasure rights as anyone over Sage's working memory |
| Unknown age | Held back and reported separately, never released |
| Stated in the register | Migration 34 writes the s.6 citation into `retention_policies.authority` |

It is a floor applied over `retention_policies` rather than a row inside it,
because "was this patient a child" is a property of the patient and that table
is keyed by category. Eight tests cover it, including the boundary at exactly
21 and the case where `date_of_birth` and `age` contradict each other.

**One thing this surfaced that the hospital needs to fix.** Every patient row in
the development database has a NULL `date_of_birth` and a populated `age`, so
the protection runs on the `age` fallback, not on date of birth. That fallback
is safe in one direction only: `age` is a snapshot that is refreshed upwards, so
a stale value understates the patient's real age and can only over-protect. It
can never release a record early. But a hospital that does not capture date of
birth is relying on that fallback for a statutory retention decision, and
capturing DOB at registration should be on the go-live checklist.

### Gaps I would still put in front of counsel

These are not encoded today and I think they should be:

1. **Billing, GST and procurement records.** Orb now holds them, and none of
   the financial retention rules are in the seed: Companies Act 2013 s.128(5)
   is 8 years, Income Tax Rule 6F is 6 years, GST is 72 months from the annual
   return due date.
2. **MTP records.** The MTP Rules require the admission register to be kept
   longer than the general clinical period. If the hospital performs MTPs this
   needs its own category.
3. **State Clinical Establishments rules.** Several states set their own
   retention in their Clinical Establishments (Registration and Regulation)
   rules, which can exceed the MCI baseline. This has to be checked per site,
   not once nationally.
4. **Regulatory status of the citation itself.** Orb cites the MCI 2002
   regulations. The NMC's replacement professional conduct regulations were
   notified in 2023 and then withdrawn pending review, so 2002 remains the
   operative baseline, but this is live ground and counsel should confirm what
   is in force on the day the hospital signs.
5. **The EHR Standards for India 2016 recommend lifetime retention** of the
   electronic health record. That is guidance rather than binding law, but a
   NABH assessor or a CIO may well cite it, and it points the opposite way from
   the 3-year floor. Decide which one the hospital is buying before go-live.

### What this means for sizing

**Nothing meaningful is deletable at the end of year 1.** Only ambient audio
(30 days) and AI transcripts (365 days) roll off, and both are small. The
clinical record, the audit trail and the media accumulate monotonically for at
least 3 years, and the audit trail for 8.

So storage grows in a straight line for the life of the deployment. Year 1 is
the first slice of an 8-year curve, not a steady state, and a machine bought
for year 1 alone will need replacing or expanding before the retention period
it exists to satisfy has run.

---

## 3. The backup policy is what forces the disk size, not the data

Three years of live data is 358 GB (A) and 195 GB (B) at the mid media case.
That fits on 1 TB with room to spare. The database is genuinely small.

What does not fit is the backup regime as it ships today. `scripts/backup.sh`
retains 14 full copies (`ORB_BACKUP_RETAIN=14`) and, at line 182, re-tars the
entire uploads directory every night, uncompressed, as a full copy alongside
each database backup.

| 3-year on-box backup footprint | A | B |
|---|---|---|
| Current policy (14 fulls, nightly full media tar) | **5,017 GB** | **2,726 GB** |
| Recommended (3 on-box DB fulls, one mirrored media copy) | 595 GB | 362 GB |

Under the current policy scenario A needs an 8 TB machine and writes a 246 GB
tar every night forever. That is not a storage problem to buy your way out of,
it is a backup design that is O(total media) per night and was sized for a ward.

Media is immutable once uploaded. It should be mirrored incrementally, not
re-tarred. Three changes, in order of value:

1. Mirror `server/uploads/` incrementally instead of `tar -cf` per night.
2. Reduce `ORB_BACKUP_RETAIN` on the appliance to 3, and keep the depth on the
   rotating external drives where it belongs.
3. Add a periodic `VACUUM`. `auto_vacuum` is 0 and nothing vacuums, so the file
   never gives space back. The development database is 716 MB of which 570 MB
   (79.6%) is freelist. That is measured, and it means the 68 MB/day growth
   figure in HARDWARE_SPEC section 2 is file growth including bloat, not data
   growth. Real data growth is roughly a quarter of it.

### Year 1 on its own

Asked directly: how much storage does one year need. Live data first, then the
disk it actually has to sit on.

| Year 1, live data | A | B |
|---|---|---|
| Database | 37.3 GB | 26.9 GB |
| Media, low attachment rate | 32.9 GB | 14.2 GB |
| Media, **mid** | **82.1 GB** | **38.0 GB** |
| Media, high | 219.0 GB | 94.9 GB |
| **Live total, mid case** | **119 GB** | **65 GB** |

The live data for a year is small. What it needs around it is not: 30 GB macOS,
30 GB of model weights, the retained backups, and free space equal to the live
set so a restore has somewhere to land.

| Disk needed for year 1 | A | B |
|---|---|---|
| Current backup policy, mid media | **1,971 GB** | **1,098 GB** |
| Current backup policy, high media | 4,161 GB | 2,009 GB |
| Recommended policy, mid media | 497 GB | 310 GB |
| Recommended policy, high media | 915 GB | 484 GB |

Read the top row first, because it is the one that applies if nothing changes.
**On the shipping backup policy, one year of a busy 200-bed hospital does not
fit on a 2 TB machine at the high media rate, and only just fits at the mid
rate.** The 1 TB in HARDWARE_SPEC is not close.

Fix the backup policy and the same year fits in 1 TB with room to spare. That
is the whole argument for doing the backup work before the purchase order, not
after: it is the difference between a 1 TB machine and a 4 TB one, and between
a 2 TB backup drive and an 8 TB one.

Because nothing is deletable at the end of year 1 (section 2), do not buy for
year 1. Buy for the retention period. Year 3 is roughly three times the live
figures above, and the audit trail keeps accumulating to year 8.

### What to buy

Sized for the high media case with the recommended policy, plus 30 GB models,
30 GB macOS, and restore headroom of half the live set:

| | A | B |
|---|---|---|
| Internal SSD | **4 TB** | **2 TB** |
| Backup drives (3, rotating A/B/C) | **4 TB** each | **2 TB** each |

If the backup policy is *not* changed before go-live, scenario A needs 8 TB
internal and 8 TB drives. Changing the policy is much cheaper than the disks.

---

## 4. The application tier is not the problem

The 11 August soak (`docs/perf/soak-20260811-050842`) ran 15 minutes at a
60-patient census: 3,634 requests, zero errors, p99 under 110 ms, server RSS
285 to 373 MB. That is about 4 requests/second.

Scaled to scenario A the client polling intervals (Vigil board 30 s, insights
20 s, care bundles 15 s, Relay 30 s, doctor and nurse panels 60 s) put roughly
170 concurrent sessions at about 34 requests/second. That is 8.5x the tested
rate. Server memory should land near 1 to 1.5 GB, still trivial against a 30 GB
model set, and the CPU cost is well under one core.

**This has not been measured and should not be assumed.** The soak harness
drives census, not concurrency. Before a 200-bed go-live, run it again at 170
sessions. I expect it to pass comfortably; I have not shown that it does.

SQLite is a single-writer store, and scenario A writes about 100,000 rows/day,
averaging just over one write per second with peaks perhaps twenty times that.
That is well inside what SQLite in WAL mode handles. It is not a concern.

---

## 5. The AI tier is the problem, and it is a fixed absolute budget

This is the part that does not scale by buying a bigger Mac.

Three limits, all process-wide, all independent of how many beds the hospital
has:

| Limit | Value | Where |
|---|---|---|
| Concurrent interactive Sage turns | **1** | `MAX_INTERACTIVE`, hardcoded, `services/llm_queue_service.py:43` |
| Concurrent background LLM streams | **1** | `max_concurrent`, hardcoded, same file line 251 |
| Background admissions per minute | **12** | `BG_LLM_MAX_PER_MIN`, `config.py:274`, env-overridable |

### Interactive Sage holds up

Cost per ambient call is 0.8 GPU-seconds, derived from the measured alert storm
(76% GPU duty at 57 calls/min). Sage turns measure 3.2 to 4.2 s simple and 8 to
12 s when they pull guideline text.

| | A | B |
|---|---|---|
| Sage turns/day | ~700 | ~380 |
| Peak (60% of turns inside a 4-hour rounds window) | 1.8/min | 0.9/min |
| Utilisation of the single slot, M2 Max class at 8 s | 23% | 13% |
| Added queue wait | ~2.4 s | ~1.2 s |
| Same on an M5 Ultra at ~3 s (1.2 TB/s, projected) | 9%, ~0.3 s | 5%, ~0.2 s |

One interactive slot survives both scenarios on average. It is bursty at rounds,
when eight wards start at once, and a hardcoded 1 leaves no room to absorb that.

### Ambient AI does not

| | 60-bed pilot | A | B |
|---|---|---|---|
| Peak ambient demand | 57 calls/min (measured) | ~185/min | ~142/min |
| As a share of one GPU | 76% | **247%** | **190%** |
| Admitted by the governor | 12/min (21% of demand) | 12/min (**6%**) | 12/min (**8%**) |

The governor ceiling is 12 per minute for the whole hospital, not per ward and
not per bed. So the ambient layer (the narrator, Sage insights, timeline
projections, Vigil's garnish, room fit) gets the same absolute budget at 200
beds that it gets at 60. **Per-patient ambient coverage at 200 beds is about a
third of what the pilot ward receives.**

Nothing breaks and nothing crashes. Every governed caller has a deterministic
fallback by design, which is the right design. But a hospital told it is buying
what the pilot ward had would be getting a third of it, and that is a claim to
be careful about in a sales conversation.

---

## 6. Why a second Mac does not fix this today

The obvious answer is to put the models on their own machine.
`ORB_OLLAMA_BASE_URL` (`config.py:112`) supports exactly that and its own
comment names "a remote GPU box" as a supported target. Two things stop it from
helping:

1. **There is one base URL for both tiers.** `LLM_MODEL` (27B, careful) and
   `THINKING_FAST_MODEL` (4B, ambient) resolve to the same host across all 55
   call sites. The 27B is latency-bound and the 4B is throughput-bound, they
   are the two things that should be separated first, and today they cannot be.

2. **The queue serialises in-process, wherever Ollama runs.** `MAX_INTERACTIVE`
   and `max_concurrent` are module constants, not configuration. Moving Ollama
   to another machine moves the work but not the limit; the app still admits
   one stream at a time.

So the order of work for 200 beds is code first, hardware second:

| Step | Change | Unlocks |
|---|---|---|
| 1 | Per-tier base URLs (`ORB_OLLAMA_FAST_BASE_URL` alongside the existing one) | Ambient 4B moves to its own box |
| 2 | Make `MAX_INTERACTIVE` and `max_concurrent` configurable, defaulting to 1 | More than one stream when there is more than one GPU |
| 3 | Raise `BG_LLM_MAX_PER_MIN` in proportion to available GPU | Ambient coverage back to pilot levels |

Only after step 1 does the second machine earn its price. It does not need to be
large: the ambient model is 3.3 GB, so a Mac mini M5 Pro with 64 GB is enough,
and ambient work is throughput-bound rather than bandwidth-bound.

Raising the constants without adding a GPU is not a fix. It converts shedding
into queueing, which is worse, because a shed ambient call takes its
deterministic fallback immediately and a queued one arrives late.

---

## 7. What to order

### Scenario A: busy, always occupied, full staff

| Item | Specification | Qty |
|---|---|---|
| Primary appliance | Mac Studio, **M5 Ultra**, 96 GB, **4 TB** | 1 |
| Standby appliance | Mac Studio, **M5 Max**, 64 GB, 2 TB | 1 |
| Ambient AI host (after step 1 above) | Mac mini, **M5 Pro**, 64 GB, 1 TB | 1 |
| Rugged external SSD | **4 TB** USB-C | 3 |
| UPS | **2000 VA / 1600 W**, pure sine, USB HID | 1 |
| Switch, console, cabinet, cabling | As HARDWARE_SPEC sections 7 and 8 | 1 set |

### Scenario B: medium busy, 75% staff

| Item | Specification | Qty |
|---|---|---|
| Primary appliance | Mac Studio, **M5 Ultra**, 96 GB, **2 TB** | 1 |
| Standby appliance | Mac Studio, **M5 Max**, 64 GB, 2 TB | 1 |
| Rugged external SSD | 2 TB USB-C | 3 |
| UPS | **1500 VA / 1000 W**, pure sine, USB HID | 1 |
| Switch, console, cabinet, cabling | As HARDWARE_SPEC sections 7 and 8 | 1 set |

Scenario B runs on the pilot architecture with more disk. Ambient coverage is
about 40% of the pilot ward's per patient, which is a trade worth stating to the
hospital rather than discovering.

### Current Apple options and prices

Checked on apple.com and Apple India on 7 September 2026. The M5 Mac Studio is
**pre-order, available from 22 September**, which the deployment timeline has to
absorb: HARDWARE_SPEC assumes a purchase order by end of week 3 and delivery in
weeks 5 to 7, and a launch-window build-to-order will not beat that.

| Machine | Bandwidth | Memory options | Storage options | Apple India from |
|---|---|---|---|---|
| Mac Studio M5 Ultra (30C/64C) | **1.2 TB/s** | 96, 256 GB (512 GB late Oct) | 1, 2, 4, 8, 16 TB | ₹6,29,900 (96 GB, 1 TB) |
| Mac Studio M5 Max (18C/32C) | 460 GB/s | 36, 48, 64, 128 GB | 512 GB to 8 TB | ₹2,79,900 (36 GB, 512 GB) |
| Mac Studio M5 Max (18C/40C) | 614 GB/s | 36, 48, 64, 128 GB | 512 GB to 8 TB | quote |
| Mac mini M5 Pro | 307 GB/s | 24, 48, 64 GB | 512 GB to 8 TB | quote |

Prices are the base configurations Apple lists; memory and storage upgrades are
extra and should come from a formal reseller quote, as HARDWARE_SPEC section 10
already insists.

Three things follow from this table:

1. **The M5 Ultra base configuration is already the right primary.** 96 GB and
   1 TB is exactly the memory this document asks for; only the storage needs
   upgrading. That was not true of the M3 Ultra, where 96 GB was a paid option.
2. **256 GB is now a sensible option, and for the first time it buys something
   real.** At 96 GB the extra memory would sit idle, because the model set is
   30 GB. At 256 GB you can hold several 27B instances resident at once, which
   is precisely what raising `MAX_INTERACTIVE` above 1 needs (section 6). If
   the AI-tier code work is going to happen, specify 256 GB and skip the second
   machine.
3. **The UPS in HARDWARE_SPEC is now undersized.** Apple rates the M5 Mac
   Studio at **480 W maximum continuous**, against the 200 to 250 W that
   document assumes. One machine at peak already exceeds the 600 W unit's
   rating once the switch and display are on it, and scenario A has two or
   three machines on the same UPS.

---

## 8. What I have not measured

In the same spirit as HARDWARE_SPEC section 11.

- **Concurrency.** No soak has run above about 4 requests/second. The 34
  requests/second figure for scenario A is derived from client polling
  intervals, not observed. Run the soak at 170 sessions before committing.
- **Media.** Still unmeasured, and now the largest storage term. The range in
  section 2 spans 6.6x. Measure it in shadow week 1.
- **Audit volume per real clinical action.** The per-day figures are built
  bottom-up from workflow counts, not from a hospital. The measured bytes-per-row
  are solid; the rows-per-day carry roughly a 2x band either way.
- **Ambient demand scaling.** I scale the measured 57 calls/min linearly with
  census. Some ambient work is per-ward or per-event rather than per-patient, so
  linear is likely an overestimate. It would have to be wrong by more than 4x to
  change the conclusion.
- **Everything in HARDWARE_SPEC section 11 still applies.** Orb has still never
  run on a Mac Studio, and the M5 Ultra speed figures here remain a projection
  from published memory bandwidth, now extrapolated three generations from a
  400 GB/s M2 Max. Treat the 45 tokens/s figure as a hypothesis to test on the
  delivered machine, not a promise to a hospital.
- **The retention position in section 2 has not been reviewed by a lawyer.**
  It is my reading of the rules Orb already cites plus the gaps I can see. The
  six gaps listed there are the questions to put to counsel, not conclusions.
  Nothing in section 2 should reach a hospital's legal team as settled.

---

## Related documents

| Document | What it covers |
|---|---|
| [HARDWARE_SPEC.md](HARDWARE_SPEC.md) | The 60-bed pilot appliance, in purchase-order detail |
| [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md) | The backup schedule this document proposes changing |
| [DATA_LAYER.md](DATA_LAYER.md) | Schema, encryption at rest, retention |
| `fundraising/ORB_FINANCIAL_MASTER.md` | Why 200 beds is the base case |
