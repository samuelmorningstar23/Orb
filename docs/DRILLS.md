# Drills: Restore and Downtime

*Version 2 · first written 1 August 2026, updated 3 September 2026 · For me as
the operator, for hospital IT, and for the ward.*

Two things in this pilot are insurance. One is the ability to bring the
clinical record back after the appliance is lost. The other is the ability to
run the ward without Orb at all. Both are built. Neither is real until somebody
has done it with their own hands, so here is where each one stands today.

| | Built | Rehearsed |
|---|---|---|
| Desk drill on a backup file (`restore-drill.sh`) | Yes | On my machine, most recently today. On 15 August it certified the wrong file, which is the most useful thing it has done so far (section 1.5). |
| Full swap-and-boot onto a second machine, with hospital IT | Yes (`standby-restore.sh`) | Never. There has been no site, and no hospital IT to do it with. |
| The key typed from the printed escrow sheet | Yes (the ceremony's Part D) | Never. The ceremony has not been held. |
| Downtime snapshot, ward pack, print | Yes | The software side runs every hour on my machine. The ward side has never been rehearsed. |

One more line, because it is the reason this document exists. As I write this,
nothing is scheduled on my own development machine. The newest backup on it is
nineteen days old, and it fails today's drill because it predates the last
migration. I wrote the schedule installer. If I can let that happen on the
machine I sit at every day, a hospital will let it happen on a machine in a
cupboard. The schedule goes in on install day and the drills go on a calendar
with a name against them.

Everything below is written against what the software does today, including
the places where it is thinner than the drill would like. Those places are
marked. A drill written against an imagined feature teaches the ward the wrong
reflex.

---

## Contents

- [Drill 1, Restore](#drill-1-restore)
  - [1.1 Three levels](#11-three-levels)
  - [1.2 The desk drill](#12-the-desk-drill)
  - [1.3 The full rehearsal](#13-the-full-rehearsal)
  - [1.4 Pass/fail checklist](#14-passfail-checklist)
  - [1.5 What a failure means](#15-what-a-failure-means)
  - [1.6 Restore drill sign-off sheet](#16-restore-drill-sign-off-sheet)
- [Drill 2, Downtime](#drill-2-downtime)
  - [2.1 What the software actually provides](#21-what-the-software-actually-provides)
  - [2.2 The two scenarios, and why they are different](#22-the-two-scenarios-and-why-they-are-different)
  - [2.3 Who declares downtime](#23-who-declares-downtime)
  - [2.4 What ward staff do](#24-what-ward-staff-do)
  - [2.5 Running the drill](#25-running-the-drill)
  - [2.6 Reconciliation afterwards](#26-reconciliation-afterwards)
  - [2.7 Pass/fail checklist](#27-passfail-checklist)
  - [2.8 Downtime drill sign-off sheet](#28-downtime-drill-sign-off-sheet)
- [Schedule](#schedule)

---

# Drill 1: Restore

**Purpose.** Prove that the hospital's clinical record can be brought back onto
a different machine, by the people who would have to do it, using only what
would be available on the day.

**Who is in the room.** Me and hospital IT. The realistic disaster is the one
where I am an hour away and IT is standing in front of a dead Mac Studio. They
need to have done this once with their own hands, with me watching rather than
typing.

---

## 1.1 Three levels

They test different things and none of them substitutes for another.

| | **Desk drill** | **Nightly standby restore** | **Full rehearsal** |
|---|---|---|---|
| Proves | The newest backup file is readable, complete, at the right schema version, and its audit chain is intact | The newest backup restores onto the standby machine, unattended, every night | The whole path: a human swaps the file, boots, signs in, sees patients |
| Touches the live database | Never | Never. It refuses to run on any machine where a live Orb answers. | Never. A second machine is used. |
| Who | Me, or the scheduler | The scheduler, on the standby | Me and hospital IT |
| How often | Monthly on the schedule, and after any backup failure | Every night at 03:30 | Before go-live, then quarterly |
| Effort | Fourteen seconds on a 619 MB backup today | None. Read the receipt. | An hour or two |
| Command | `./scripts/restore-drill.sh` | `./scripts/install-standby-schedule.sh` once; `./scripts/standby-restore.sh --check-only` to look | Section 1.3 |

The desk drill covers most of the risk, because an unreadable or stale backup
is the common failure. The nightly standby restore covers the transport and
the second machine. The rehearsal covers the day itself: whether the standby
boots, whether anyone knows where the key is, whether the uploads came back
too.

---

## 1.2 The desk drill

```bash
cd /opt/orb
set -a && . ./.env && set +a
./scripts/restore-drill.sh
```

It takes the newest backup from the canonical backup directory, copies it to a
scratch directory, and works only on the copy. That matters because opening a
SQLCipher database creates `-wal` and `-shm` sidecars beside it and can write
to the file, and a drill that mutates the artefact it is validating is not a
drill. It refuses to run if pointed at the live database. It deletes the
scratch copy on exit, including on Ctrl-C, unless you pass `--keep-scratch`,
in which case it warns you that you have just left a copy of patient data on
disk. **Safe to run mid-clinic.**

Useful flags: `--backup <file>` drills one specific file instead of the newest.
`--scratch-dir <dir>` puts the working copy somewhere with room. The
environment it reads: `ORB_DB_ENCRYPTION_KEY` (required), `ORB_BACKUP_DIR`,
`ORB_DB_PATH`, `ORB_BACKUP_MIN_BYTES` (1 MiB by default) and
`ORB_BACKUP_REQUIRE_NONEMPTY` (section 1.5).

### What it checks

The drill runs the same verifier the backup job runs
(`scripts/lib/verify_restore.py`), with the schema policy set to fail rather
than warn. Every line maps to a way a backup can be quietly useless.

| Check | What it rules out |
|---|---|
| `size floor` | A zero-byte or truncated file. In July the old backup script wrote empty files, passed an integrity check on them, and pruned the real backups to make room. |
| `decrypt+open` | The key is wrong, or the file is damaged |
| `integrity_check` | Structural corruption |
| `schema version` | A backup older than the code you would restore it into. Restoring one brings whole modules back empty. |
| `module tables` | Tables missing that the schema says must exist. The manifest knows 88 tables in 17 groups. |
| `rows:audit_logs`, `rows:patients`, `rows:users`, `rows:vitals` | An empty-but-valid database |
| `seeded reference` | The rows migrations write themselves (document number series, shift types, payers, the self-pay tariff) have been deleted. A restore from such a file could not number an invoice. |
| `audit chain` | Each row's previous-hash must equal the row hash before it. A record that fails this is indistinguishable from a tampered one. |
| `admin present` | A restore nobody can sign into. Informational by default; `--require-admin` makes it hard. |
| Module data | Counts, not verdicts. See below. |

The module counts need a sentence. `EMPTY` on billing is correct before the
billing desk goes live and a real loss after it, and nothing inside the file
can tell those apart. The day a module goes live, put its group name in
`ORB_BACKUP_REQUIRE_NONEMPTY` in `.env` (for example `billing,payments`) so an
empty one becomes a failure instead of a line of text nobody reads at 04:00.

Exit codes: `0` PASS · `1` could not run · `2` FAIL.

### What today's run looks like

This is my own machine on 3 September 2026, lightly abbreviated. It is a FAIL,
and it is the right kind of FAIL.

```
[drill] Backup under test : /Users/.../backups/orb-backup-20260815-162957.db
[drill] Size              : 619M (632918016 bytes)
[drill] Age               : 19 day(s) old
[drill]   size floor ....... PASS (>= 1048576 bytes)
[drill]   decrypt+open ........ PASS
[drill]   integrity_check ..... PASS
[drill]   schema version ...... FAIL  backup is at v32, this build ships v33
[drill]   module tables ....... PASS  all 88 present
[drill]   rows:audit_logs ..... PASS  91,299 rows
[drill]   rows:patients ....... PASS  31 rows
[drill]   rows:users .......... PASS  20 rows
[drill]   rows:vitals ......... PASS  62,038 rows
[drill]   seeded reference .... PASS  document_number_series 10, duty_shift_types 4, ...
[drill]   audit chain ......... PASS  91,299 rows, linkage intact
[drill]   (info) login-capable admins: 1
[drill] Module data (counts, not verdicts):
[drill]   billing ...............        40 rows across 8/11 tables
[drill]   biomedical ............     EMPTY  (6/6 tables present)
[drill]   ...
[drill] VERIFY FAILED  1 check(s): schema version

[drill] RESULT: FAIL  do not rely on this backup.
[drill][fail] If it failed on `schema version` or `module tables`, walking further
[drill][fail] BACK will only make that worse. Those failures mean too OLD, not corrupt.
```

Read what it is saying. The file is intact, the key works, the chain is
unbroken, and it would still be the wrong thing to restore, because it predates
migration 33 and everything that migration holds. The fix is not an older
backup. The fix is a backup taken now. On an appliance with the schedule
installed, this line can only appear in the window between an upgrade and the
next 02:30, and the backup job's own schema warning would have told me first.

**Record every drill**: date, which backup, PASS or FAIL, and if FAIL, which
line. "We have backups" is a claim. "We drilled on the 1st and it passed" is
the fact an auditor, a CIO, or a court will ask for.

---

## 1.3 The full rehearsal

### Before the day

- [ ] The standby Mac exists, is on the hospital network, and has Orb
      installed per [INSTALL_RUNBOOK.md](INSTALL_RUNBOOK.md)
- [ ] The key ceremony is complete and both envelopes are in place
      ([KEY_CEREMONY.md](KEY_CEREMONY.md))
- [ ] Hospital IT knows they are booked for two hours
- [ ] A real backup from the last 24 hours exists on the off-box volume

### Which key, and the rule about envelopes

The rehearsal is meant to prove the path that will exist on the real day, and
on the real day the appliance's `.env` is gone. So the key has to come from
paper. There are two ways to do that without breaking the ceremony's own
rules, and I want both written down because the ceremony document treats an
opened envelope seriously.

- **The first rehearsal happens on ceremony day, inside Part D.** Part D of
  [KEY_CEREMONY.md](KEY_CEREMONY.md) already has hospital IT type the key from
  the printed sheet onto the standby machine and prove that a real backup
  opens. Carry straight on from there into steps 1 to 7 below, with the same
  typed key, before the sheet is sealed in Part E. One afternoon, one typed
  key, and both documents are satisfied.
- **Later rehearsals use the appliance's key, plus the check digit.** Every
  quarter, verify both envelopes by their check digits as section 10 of the
  ceremony document describes. That proves the envelopes hold the key without
  opening them. Then rehearse the swap with the key from `.env`.
- **If you open an envelope for any reason, it is spent.** Section 8 of the
  ceremony document says so: re-seal fresh envelopes the same day, and record
  both events. If the seal was not intact when you got to it, that is section
  11, and the key gets rotated.

### The rehearsal

Work on the **standby machine** throughout. The production appliance is not
touched at any point.

> **Two ways to do steps 1 to 5, and you should know both.**
> Steps 1 to 5 below are the manual swap. Do them by hand at the first
> rehearsal, because the point is that hospital IT has moved the file
> themselves and knows what each step is for.
>
> For every restore after that, `./scripts/standby-restore.sh` does the same
> thing with guard rails that hands do not have. It verifies the file before
> touching anything, with the schema policy set to fail and an admin account
> required. It stages the copy on the same filesystem and moves it into place,
> so a power cut mid-copy leaves the previous database intact. It keeps one
> generation back as `orb.db.previous`. It unpacks the matching uploads
> archive. It refuses to run at all if a live Orb answers on the port, or if
> the database's write-ahead log was touched in the last five minutes
> (`ORB_STANDBY_FORCE=1` overrides that, and you should have a reason). It
> warns if the newest backup is more than two days old. And it writes a
> receipt to `server/ops/standby/last-restore.json` on every exit, pass or
> fail, which is what the schedule and the heartbeat read. See
> [WARM_STANDBY.md](WARM_STANDBY.md). Learn the manual path once, use the
> script thereafter.

**Step 0, prove the backup before you move anything.**

```bash
cd /opt/orb
export ORB_DB_ENCRYPTION_KEY=<typed from the sheet, or from .env per the rule above>
./scripts/restore-drill.sh --backup /Volumes/OrbBackup/orb-backup-YYYYMMDD-HHMMSS.db
```

Discovering that the backup is unreadable after you have moved the live
database is how a bad day becomes a catastrophic one.

**Step 1, stop the server on the standby machine.**
Never swap the database file under a running server. It corrupts both copies.

**Step 2, keep whatever is there. Do not overwrite.**

```bash
mv server/orb.db server/orb.db.broken-$(date +%Y%m%d-%H%M%S)
```

**Step 3, put the backup in place.**

```bash
cp /Volumes/OrbBackup/orb-backup-YYYYMMDD-HHMMSS.db server/orb.db
chmod 600 server/orb.db
```

**Step 4, remove stale sidecars.**

```bash
rm -f server/orb.db-wal server/orb.db-shm
```

They belong to the old file and will corrupt the new one. This is the step
people forget.

**Step 5, restore the media too.**

Lens images and Scribe audio live beside the database backup as
`orb-backup-<same timestamp>-uploads.tar`. The timestamps match on purpose.
Restoring an X-ray from one night against a chart from another is its own kind
of clinical error.

```bash
tar -xf /Volumes/OrbBackup/orb-backup-YYYYMMDD-HHMMSS-uploads.tar -C server/
```

The files inside are individually encrypted under `ORB_UPLOAD_ENCRYPTION_KEY`,
so the server needs **both** keys to show them. If only one was escrowed, this
is where you find out.

**Step 6, start, with the same keys.**

```bash
set -a && . ./.env && set +a     # both keys must be the ones the backup was written with
cd server && ../server/.venv/bin/python main.py
```

**Step 7, verify from the outside, as a user.**
Not with a database query. Open a browser on a different device and check:

- [ ] Sign in works
- [ ] The ward board lists patients
- [ ] Open a patient: demographics, allergies and medications are there
- [ ] Recent vitals are present, and their newest timestamp is what you
      expect from that backup
- [ ] A Lens image or Scribe recording opens (this proves the uploads key)
- [ ] The audit log renders

**Step 8, write down the data-loss window.**

```
Backup timestamp        : ____________
Would-be incident time  : ____________
Data that would be lost : ______ hours of vitals, notes and orders
```

The default schedule is one backup a day at 02:30, so the honest number is
**up to 24 hours**. [WARM_STANDBY.md](WARM_STANDBY.md) explains how to run
several a day if the ward wants a shorter window. On the real day the ward has
to be told which window was lost, because nothing in Orb makes that gap
visible to a clinician after the fact.

**Step 9, put the standby machine back.**
Restore its own database from the file you moved aside in step 2, or wipe and
re-install it. Do not leave a second machine on the network holding a stale
copy of the patient record. An out-of-date record that looks live is a
clinical hazard, not a spare.

**Step 10, re-seal if you opened anything.**
Per the rule above, and the same day.

---

## 1.4 Pass/fail checklist

Every line must be `yes`. A single `no` is a FAIL, and a FAIL means the
hospital does not currently have a proven recovery path.

| # | Check | Pass |
|---|---|---|
| 1 | A backup from within the last 24 hours existed on the off-box volume | ☐ |
| 2 | `restore-drill.sh` on that backup returned `RESULT: PASS`, with `schema version` passing | ☐ |
| 3 | The key was typed from the printed sheet (ceremony day), or both envelopes were verified by check digit and the appliance key was used | ☐ |
| 4 | The audit hash chain verified as intact | ☐ |
| 5 | The database swapped onto the standby machine and the server started | ☐ |
| 6 | Sign-in worked | ☐ |
| 7 | Patients, allergies, medications and vitals were all present and correct | ☐ |
| 8 | Uploads restored and at least one image or recording opened | ☐ |
| 9 | Hospital IT performed the steps, with me observing | ☐ |
| 10 | The data-loss window was calculated and written down | ☐ |
| 11 | The standby machine was returned to a safe state | ☐ |
| 12 | Any opened envelope was re-sealed the same day | ☐ |

### Timings

Record the actual elapsed time for each phase. Do not carry a target forward
from a document that has never been run. The first rehearsal sets the
baseline, and later drills are compared against it.

For reference: on my machine today, copying and verifying a 619 MB backup took
fourteen seconds. Copying is not the slow part. The slow parts are locating
the key, starting the server, and the models warming, which is about a minute
for the 27B from cold.

| Phase | Target | Actual |
|---|---|---|
| Key onto the standby machine (sheet or `.env`) | | |
| Step 0, drill the backup | | |
| Steps 1 to 5, swap database and uploads | | |
| Step 6, server up and answering `/api/health` | | |
| Step 7, verified as a user | | |
| **Total, key to working ward view** | | |

After the first rehearsal, agree a target total with the hospital and write it
into the pilot record. That number is the answer to "how long would we be
without the record?", and the CIO will ask.

---

## 1.5 What a failure means

| Failure | Severity | Do this |
|---|---|---|
| `decrypt+open ... FAIL` with the escrowed key | **Severity 1** | The escrow is wrong. Try the second envelope. If both fail, the backups are unrecoverable and the hospital must be told today. [INSTALL_RUNBOOK.md](INSTALL_RUNBOOK.md) F4. |
| `audit chain ... FAIL` | **Severity 1** | That backup is not a compliance record. Drill older backups to find the last good chain. Treat as an incident. |
| `schema version ... FAIL` or `module tables ... FAIL` | High | The backup is too old for the code. **Do not walk backwards.** Older is worse. Find out why no newer backup exists (usually the job stopped after an upgrade), take one now, and drill it. |
| `seeded reference ... FAIL` | High | Not an age problem. Something deleted rows the migrations wrote. Escalate; a restore from this file comes back unable to number an invoice. |
| An older backup passes but the newest does not | High | You now know the real data-loss window. Record it and fix the cause before the next nightly run. |
| The database restores but uploads do not | Medium | The record has holes. Usually `ORB_UPLOAD_ENCRYPTION_KEY` was never escrowed. Fix the escrow, not the restore. |
| The standby machine will not start Orb | Medium | Not a data problem, but it is the recovery path. Fix and re-drill. |
| It worked, but took far longer than agreed | Medium | Report the number as it is. A four-hour recovery is a different clinical risk from a one-hour one. |

### The one the drill got wrong: 15 August 2026

On 15 August the drill passed a backup that should never have passed. The
shell scripts and the Python code answered "where do backups live?" two
different ways, one directory beside the repo and one beside the database, and
the two had quietly diverged for a fortnight. The drill picked from the stale
one and certified a file at schema version 4 when the build was at version 32.
Every artefact-level check passed, because the file was a perfectly good copy
of a database from three weeks earlier. Verify-before-rotate protects the file.
It does not protect the search path.

What changed: there is now one canonical backup directory, defined once in
`config.BACKUP_DIR`, and a regression test that fails if any resolver
disagrees with it or if a second directory appears. The stale files were
quarantined into `stale-backups/` with a README that says, in its title, that
they are not restorable. The `schema version` and `module tables` checks were
added the day before for the same class of reason.

The lesson I want in the room at every drill: a PASS is a statement about the
file the drill chose. Read the first line of the output, the path, before you
read the verdict.

**Report a failed drill the same day.** A drill that finds a problem has done
its job. A drill nobody reports has not.

---

## 1.6 Restore drill sign-off sheet

```
════════════════════════════════════════════════════════════════════════════
  ORB, RESTORE DRILL SIGN-OFF
════════════════════════════════════════════════════════════════════════════
  Drill reference   : ORB-RD-________________
  Date              : ____________   Start ________   End ________
  Type              : [ ] Desk drill   [ ] Full rehearsal
  Hospital          : _______________________________________________
  Standby machine   : _______________________________________________

  Backup drilled    : _______________________________________________
  Backup timestamp  : ____________     Size ____________
  Source            : [ ] appliance   [ ] off-box volume ______________
  Key source        : [ ] .env on the appliance, envelopes verified by
                          check digit on ____________
                      [ ] typed from the printed sheet (ceremony day)
                      [ ] opened envelope ref ______________  (re-seal due
                          the same day)

────────────────────────────────────────────────────────────────────────────
  RESULTS
────────────────────────────────────────────────────────────────────────────
  size floor .................... [ ] PASS  [ ] FAIL
  decrypt + open ................ [ ] PASS  [ ] FAIL
  integrity_check ............... [ ] PASS  [ ] FAIL
  schema version ................ [ ] PASS  [ ] FAIL    backup v____ build v____
  module tables ................. [ ] PASS  [ ] FAIL    ____ of 88
  row floors (audit_logs/patients/
    users/vitals) ............... [ ] PASS  [ ] FAIL    patients: ________
  seeded reference .............. [ ] PASS  [ ] FAIL
  audit hash-chain linkage ...... [ ] PASS  [ ] FAIL    rows:     ________
  admin present ................. [ ] yes   [ ] no
  modules required non-empty .... ______________________________  [ ] all PASS

  Full rehearsal only:
  database swapped .............. [ ] PASS  [ ] FAIL
  server started ................ [ ] PASS  [ ] FAIL
  sign-in ....................... [ ] PASS  [ ] FAIL
  patients / allergies / meds ... [ ] PASS  [ ] FAIL
  vitals present, newest at ..... ____________
  uploads restored, media opened  [ ] PASS  [ ] FAIL  [ ] n/a
  audit log renders ............. [ ] PASS  [ ] FAIL

  OVERALL : [ ] PASS   [ ] FAIL

────────────────────────────────────────────────────────────────────────────
  DATA-LOSS WINDOW
────────────────────────────────────────────────────────────────────────────
  Backup timestamp ____________   Assumed incident time ____________
  Loss window ________ hours.  Acceptable to the ward? [ ] yes [ ] no
  If no, action agreed: _______________________________________________

────────────────────────────────────────────────────────────────────────────
  TIMINGS
────────────────────────────────────────────────────────────────────────────
  Key onto the standby machine ................. ________
  Backup drilled ............................... ________
  Database + uploads swapped ................... ________
  Server answering /api/health ................. ________
  Verified as a user ........................... ________
  TOTAL ........................................ ________
  Agreed target for the next drill ............. ________

────────────────────────────────────────────────────────────────────────────
  FINDINGS AND ACTIONS
────────────────────────────────────────────────────────────────────────────
  ____________________________________________________________________
  ____________________________________________________________________
  Owner ____________________  Due ____________

────────────────────────────────────────────────────────────────────────────
  SIGNATURES
────────────────────────────────────────────────────────────────────────────
  Orb operator     ____________________  ____________________  __________
                        (name)                (signature)        (date)
  Hospital IT      ____________________  ____________________  __________
  Witness          ____________________  ____________________  __________

  Envelope re-sealed (if one was opened) : [ ] yes, on ____________
                                           [ ] n/a
════════════════════════════════════════════════════════════════════════════
```

---

# Drill 2: Downtime

**Purpose.** Prove that the ward can keep working when Orb cannot be relied
on, that the people on shift know it without being told, and that the data
written on paper gets back into the record afterwards.

**Who is in the room.** The ward nurse in charge, at least two ward nurses who
will actually be on shift, the physician champion or their delegate, and me.
Not IT. This drill is about the ward, not the machine.

**Where it stands.** Every part of the software below exists and runs on my
machine every hour. No ward has ever run this drill, because there has been no
ward. The measured numbers the drill asks for (how long a nurse takes to print
the pack, how long reconciliation takes) do not exist yet, and I have not
invented them.

---

## 2.1 What the software actually provides

Read this section before writing a local policy against it. Some of it is
stronger than people expect and some is thinner, and the thin parts are where
the drill earns its keep.

### The downtime snapshot

Orb freezes each patient's essential clinical picture into a durable local
record and serves it back read-only. The capture path and the read path are
plain database reads plus the rule-based NEWS2 calculation. No AI model, no
network, no external service. That is deliberate. A snapshot can always be
taken and always be read, even on a box with no models loaded and no
connectivity at all.

What one snapshot contains:

| In the snapshot | Not in the snapshot |
|---|---|
| Demographics, MRN, ward and bed, attending, emergency contact | Orders and the order history |
| **Active allergies**, first, and prominent | The eMAR administration schedule (what is due, and when) |
| Active problems, with ICD-10 codes | Unsigned draft notes |
| Active and PRN medications with dose, route, frequency | Lab results as a panel |
| The latest vitals row and its rule-based NEWS2 score with drivers | Imaging |
| The last **five** signed notes | Anything written after the snapshot was taken |
| The last five risk scores | |

The exclusions are not a criticism. They are what the ward has to know in
advance. **If your ward's downtime policy depends on knowing what medication
is due at 14:00, the snapshot does not carry that**, and the drill must find
another answer, which is usually the paper drug chart.

### How snapshots get taken

- **Automatically, every hour.** A background sweep captures every admitted
  patient once an hour, starting shortly after the server boots.
- **On demand, ward-wide.** The Command Center has a **Capture all** button
  (admin permission) that snapshots every admitted patient immediately and
  writes the count to the audit log.
- **On demand, one patient.** The Vigil patient view has a **DOWNTIME** button
  that opens that patient's snapshot view in a new tab, where **Capture
  snapshot** (or **Re-capture now**) takes a fresh one.

### Reading and printing one

The downtime view is a plain, high-contrast, print-shaped page: allergies
first, vitals and NEWS2, problems, medications, recent notes, and a banner
that says DOWNTIME SNAPSHOT with the capture time. The **Print** button opens
the browser's print dialog for that one patient.

### The ward-level print pack

Every hourly sweep also renders one self-contained HTML pack per ward into
`server/downtime_exports/`: a census index page, then one page per admitted
patient with the identity band, allergies (prominent), code status and any
open escalation, the latest vitals with NEWS2, active medications with doses,
active problems, and the most recent signed notes, truncated on paper with the
full text in the per-patient snapshot. Monochrome, one patient per page, each
page stamped with its ward, its snapshot capture time and the pack's
generated-at time.

Two ways to reach it:

- **In the app**: the downtime view has a **Print ward pack** button that
  opens the pack in a new tab for the browser's print dialog. One print action
  per ward, not one per patient.
- **On disk, with the app down**: the files are named predictably.
  `ward-pack.<ward>.<hash>.latest.html` is always the freshest copy, hourly
  timestamped siblings are kept for three days (`ORB_WARD_PACK_RETENTION_DAYS`),
  and any browser opens them straight from the filesystem. Like the FHIR
  bundles, they are plain unencrypted files (directory mode `0700`, files
  `0600`), protected by file permissions and FileVault rather than by the
  database key.

**What is still manual.** The pack never prints itself onto paper. A human
opens it and presses Print at the agreed cadence, and in scenario D-2
(appliance dead, disk unreachable) only paper printed in advance exists. A
patient whose snapshot cannot be captured appears in the pack as an explicit
page reading "SNAPSHOT UNAVAILABLE for this patient. Use the paper chart and
the bedside record." The pack never silently omits an admitted patient. The
eMAR schedule is still not in it, so the paper drug chart remains part of the
answer.

### The Downtime Readiness indicator

The Command Center shows **Downtime Readiness**: the percentage of admitted
patients holding a snapshot newer than 24 hours, with a footer of the form
"28/30 snapshots · oldest 41 min".

Two things to understand about that number:

- The automatic sweep runs hourly, so in normal operation snapshots are under
  an hour old. The KPI's 24-hour window is far more generous than that.
- **A green readiness figure therefore does not mean "fresh."** It means "not
  older than a day." Read the oldest-snapshot age next to it. That is the
  number that tells you something.

### The FHIR self-export

Once a day, on the sweep's 24th tick, Orb writes a complete FHIR bundle for
every admitted patient to `server/downtime_exports/`. These are
standards-based, portable, and readable without Orb running at all. They are
kept for 14 days (`ORB_FHIR_EXPORT_RETENTION_DAYS`).

Two caveats:

- They are **plain JSON, not encrypted**. On my machine today that directory
  holds 277 MB: 30 patient bundles and 84 ward packs across six wards. That
  is readable PHI, and it is one of the reasons FileVault is mandatory on this
  appliance.
- They are **not a bedside artefact.** A bundle with thousands of resources is
  not something a nurse reads at 3 a.m. It is an escrow copy for the hospital
  and a migration path, not part of this drill.

---

## 2.2 The two scenarios, and why they are different

This is the part most downtime plans get wrong, and it is the reason the drill
has two halves.

### Scenario D-1: Orb is degraded, but the appliance is serving

The AI is down, the monitor feed has stopped, the network to some devices is
broken, Ollama has crashed, and yet `/api/health` answers and the app loads.

**Downtime Mode works.** Staff open the snapshot view on screen, print what
they need, and carry on. This is the scenario the feature was built for, and
it is the most likely one.

### Scenario D-2: The appliance itself is down

Frozen, powered off, dead, or unreachable.

**Downtime Mode does not work.** The snapshot view is served by Orb. The page
fetches the snapshot from the appliance, so if the appliance is not answering,
neither is the downtime view. The snapshots are safe, they are rows inside the
encrypted database, but nobody can read them until the appliance is back.

There are exactly two things that help in D-2:

1. **Paper printed in advance.** Nothing else is available at the bedside.
2. **The standby machine**, restored per Drill 1, which takes as long as
   Drill 1 takes.

So the standing rule, and it is the whole point of this drill:

> Once clinicians depend on Orb, the ward prints a fresh pack for every
> admitted patient at an agreed cadence, at minimum once per shift, and keeps
> it in a known folder at the nurses' station. Paper that exists before the
> outage is the only thing that helps during one.

Agree the cadence with the ward and write it here: `Print cadence: ____________`.

During Phase A of shadow mode this matters less, because no clinical decision
depends on Orb and an outage has no clinical consequence. **Rehearse it
anyway, before clinician access begins.** The habit has to exist before the
dependency does.

---

## 2.3 Who declares downtime

One person, named in advance, so nobody spends the first ten minutes deciding
who decides.

| | |
|---|---|
| **Declares downtime** | The **ward nurse in charge** on the shift. Not me, not IT. The person accountable for care on the ward at that moment. |
| **Declares it over** | The same role, after I confirm Orb is working *and* the nurse in charge is satisfied. Two conditions, both required. |
| **Told immediately** | Me, hospital IT duty, the physician champion |
| **Told at the start of the next shift** | Everyone, at handover, including which window is missing from the record |

The threshold for declaring is deliberately low. **If Orb is not usable and
it is not obviously coming back within a few minutes, declare it.** Declaring
downtime costs a little paper. Not declaring costs an hour of everybody
quietly waiting for a screen to load.

Reverting is normal and is not a failure. Declare it, then undeclare it if the
system comes back.

---

## 2.4 What ward staff do

Print this section and put it in the downtime folder.

**The first minute**

1. The nurse in charge declares downtime. Say it out loud on the ward.
2. Fetch the printed pack from the nurses' station folder.
3. Note the **time downtime started** on the paper log. Reconciliation depends
   on this time.

**While Orb is down**

4. **Care continues exactly as it would have without Orb.** Nothing on this
   list changes a clinical decision. Vital signs are taken on the normal
   schedule and written on paper observation charts. Medicines are given and
   signed for on paper. Escalation happens by voice and bleep, as it did
   before Orb existed.
5. Use the printed pack for allergies, active problems, current medication and
   the last recorded observations. **Check its capture time.** It is printed
   on the page, and anything after that time is not in it.
6. If the appliance is serving but degraded (scenario D-1), a fresh snapshot
   can still be printed on demand. If it is not serving, the paper you have is
   what you have.
7. **Keep every piece of paper.** Do not discard an observation chart because
   it "will be entered later." It is the record until it is.

**When Orb returns**

8. The nurse in charge confirms with me, then declares downtime over and
   records the **end time**.
9. Reconciliation begins (section 2.6). It is a named task with a named owner.
   It does not happen by itself.

**What never happens during downtime**

- No one waits for Orb before acting on a deteriorating patient.
- No one treats a snapshot as current. It is a point-in-time copy and says so
  on the page.
- No clinical decision is deferred because "the system will have it."

---

## 2.5 Running the drill

Announce it. This is a rehearsal, not a surprise inspection. An unannounced
downtime drill on a live ward is itself a patient-safety risk.

**Choose a quiet period. Allow ninety minutes.**

### Part 1: Preparation (me, 15 minutes)

- [ ] Confirm the ward has an agreed downtime folder location, and that it is
      empty or stale on purpose for this drill
- [ ] Note the current Downtime Readiness figure **and the oldest snapshot age**
- [ ] Press **Capture all** in the Command Center
- [ ] Confirm readiness moves to 100 % and the oldest age resets

### Part 2: Print the pack, and time it (ward staff, timed)

**This is the measurement that matters.** Have a nurse do it, not me.

- [ ] Start a stopwatch
- [ ] Open any patient's downtime view, press **Print ward pack**, and print
      the pack from the new tab
- [ ] Stop the stopwatch
- [ ] Print one per-patient downtime view as well, and time that. It is the
      fallback if the pack will not open.

```
Patients in pack     : ________
Elapsed (ward pack)  : ________
Elapsed (one patient): ________
```

The ward pack turns a per-patient cost into one print action per ward, and
the pack time is the number that decides the print cadence in section 2.2.
Before filing the printout, **check the pack's census count against the ward
board**. A pack that lost a patient is worse than no pack. A patient whose
snapshot failed appears as an explicit SNAPSHOT UNAVAILABLE page, never as an
omission, so a count that matches the board is a real check.

- [ ] Read one printed sheet as a clinician would. Is the allergy information
      unmissable? Is the capture time visible? Would you act on this at 3 a.m.?

### Part 3: Scenario D-1, degraded (30 minutes)

Simulate the AI tier being unavailable without touching the database. On the
appliance, unload the models through the admin panel (non-destructive; the
next request reloads them), or stop Ollama.

- [ ] The ward board still loads and shows patients
- [ ] A patient chart still opens
- [ ] Vitals still display, and NEWS2 is still calculated
- [ ] The downtime view still captures and prints
- [ ] Sage returns nothing, and staff can tell that it is Sage that is
      degraded rather than the record
- [ ] The nurse in charge decides, from what they can see, whether they would
      declare downtime here, and says why

Restore the models. Confirm they come warm again.

### Part 4: Scenario D-2, appliance down (30 minutes)

**Do this on the drill day with a real stop, not a simulated one.** Stop the
Orb server. Nothing clinical depends on it during shadow mode, which is
exactly why this is the right time to rehearse it.

- [ ] The ward loses the app entirely, including the downtime view. Confirm
      that the staff observe this and that it is not a surprise to them.
- [ ] Staff switch to the printed pack and paper charts, unprompted
- [ ] Run twenty minutes of ordinary ward work on paper: one set of
      observations per patient, one medication round, one simulated escalation
- [ ] The nurse in charge declares downtime and records the start time
- [ ] Escalation happens by voice and bleep, and works
- [ ] Restart Orb. Time how long until the ward board is usable again,
      including model warm-up.

```
Server stopped        : ________
Server restarted      : ________
Ward board usable     : ________
Total ward downtime   : ________
```

### Part 5: Reconciliation (section 2.6, timed)

- [ ] Enter the paper observations back into Orb
- [ ] Time it, and record the number

### Part 6: Debrief (everyone, 15 minutes)

Three questions, asked out loud:

1. What did we not have on paper that we needed?
2. What took longer than expected?
3. What would have been different at 3 a.m. with two staff and a
   deteriorating patient?

Write the answers on the sign-off sheet. They are the useful output of the
drill. The checkboxes are only evidence that it happened.

---

## 2.6 Reconciliation afterwards

Everything recorded on paper during downtime is missing from Orb until
somebody puts it there. **This is the part that quietly does not happen**, and
it produces a clinical record with a hole in it that nobody notices for weeks.

**Assign one named person and one deadline before the drill starts.**

| What | How it gets back in | Notes |
|---|---|---|
| Vital signs taken on paper | Entered through the normal vitals screen | Enter the **true observation time**, not the time of entry, or the deterioration trend is wrong |
| Medications given | Entered through eMAR | |
| Notes written on paper | Entered as notes and signed | Say in the note that it was written during downtime |
| Machine observations from a gateway that buffered | Replayed over the ingestion API | Every ingested observation carries an `observed_at` time. A reading the device dates before something Orb already accepted from that source is stored, audited as a backfill, and kept out of live scoring and alerting, so a replay cannot fire stale alerts at the ward. Whether the gateway buffered at all is a question for biomedical engineering, and the answer should be found out **before** go-live. |

**Honest gap: there is no bulk reconciliation tool.** Back-entry is manual,
through the normal screens, one item at a time. That is why the drill times
it. If reconciling twenty minutes of paper takes an hour, the ward needs to
know that in advance and staff for it.

**Record the gap explicitly.** Start time, end time, which beds, what was
back-entered and what was not. During shadow mode this matters twice over. An
ingestion gap has to be visible in the scorecard's denominators rather than
silently absorbed, or the pilot will later report coverage it did not have.

---

## 2.7 Pass/fail checklist

| # | Check | Pass |
|---|---|---|
| 1 | The ward knew where the downtime folder was, without being told | ☐ |
| 2 | A ward-wide capture completed and readiness reached 100 % | ☐ |
| 3 | The full ward printed, and the elapsed time was recorded | ☐ |
| 4 | A nurse read a printed sheet and confirmed allergies and capture time were unmissable | ☐ |
| 5 | D-1: with the AI tier down, the record, vitals and NEWS2 remained usable | ☐ |
| 6 | D-1: staff could tell it was the AI that was degraded, not the record | ☐ |
| 7 | D-2: with the appliance stopped, staff switched to paper unprompted | ☐ |
| 8 | Downtime was declared by the nurse in charge, and the start time recorded | ☐ |
| 9 | Twenty minutes of ward work ran on paper, including one escalation | ☐ |
| 10 | Escalation by voice and bleep worked | ☐ |
| 11 | Orb restarted and the time to a usable ward board was recorded | ☐ |
| 12 | Paper data was reconciled into Orb, and the time was recorded | ☐ |
| 13 | The data gap was recorded, times and beds | ☐ |
| 14 | A named person owns each debrief action, with a date | ☐ |

**A drill that surfaces problems has passed.** The failure state is a drill
where everything went smoothly because nothing was actually tested.

---

## 2.8 Downtime drill sign-off sheet

```
════════════════════════════════════════════════════════════════════════════
  ORB, DOWNTIME DRILL SIGN-OFF
════════════════════════════════════════════════════════════════════════════
  Drill reference   : ORB-DD-________________
  Date              : ____________   Start ________   End ________
  Ward              : _______________________________________________
  Census on the day : ________ admitted patients
  Announced?        : [ ] yes (required)   [ ] no

────────────────────────────────────────────────────────────────────────────
  PEOPLE PRESENT
────────────────────────────────────────────────────────────────────────────
  Nurse in charge   ____________________  ____________________  __________
                         (name)                (signature)        (date)
  Ward nurse        ____________________  ____________________  __________
  Ward nurse        ____________________  ____________________  __________
  Physician champion / delegate
                    ____________________  ____________________  __________
  Orb operator      ____________________  ____________________  __________

────────────────────────────────────────────────────────────────────────────
  PART 1, PREPARATION
────────────────────────────────────────────────────────────────────────────
  Downtime Readiness before ........ ______ %   oldest snapshot ______ min
  Capture all completed ............ [ ] yes  [ ] no   patients ________
  Downtime Readiness after ......... ______ %   oldest snapshot ______ min

────────────────────────────────────────────────────────────────────────────
  PART 2, PRINTING THE PACK   (the number that sets the print cadence)
────────────────────────────────────────────────────────────────────────────
  Printed by (role) ................ ____________________
  Patients printed ................. ________   matches ward board [ ] yes [ ] no
  Elapsed (ward pack) .............. ________    Elapsed (one patient) ________
  Allergies unmissable on the page . [ ] yes  [ ] no
  Capture time visible ............. [ ] yes  [ ] no
  Agreed print cadence going forward ______________________________

────────────────────────────────────────────────────────────────────────────
  PART 3, SCENARIO D-1 (degraded: AI tier down, appliance serving)
────────────────────────────────────────────────────────────────────────────
  Ward board loaded ................ [ ] yes  [ ] no
  Chart opened ..................... [ ] yes  [ ] no
  Vitals + NEWS2 displayed ......... [ ] yes  [ ] no
  Downtime view captured + printed . [ ] yes  [ ] no
  Degradation was legible to staff . [ ] yes  [ ] no
  Would the nurse in charge declare downtime here?  [ ] yes  [ ] no
  Why: ________________________________________________________________

────────────────────────────────────────────────────────────────────────────
  PART 4, SCENARIO D-2 (appliance stopped)
────────────────────────────────────────────────────────────────────────────
  Downtime declared by ............. ____________________  at ________
  Staff switched to paper unprompted [ ] yes  [ ] no
  Observations recorded on paper ... ________ sets
  Medication round on paper ........ [ ] yes  [ ] no
  Simulated escalation by voice/bleep worked  [ ] yes  [ ] no
  Server stopped ______  restarted ______  ward board usable ______
  TOTAL WARD DOWNTIME .............. ________
  Downtime declared over by ........ ____________________  at ________

────────────────────────────────────────────────────────────────────────────
  PART 5, RECONCILIATION
────────────────────────────────────────────────────────────────────────────
  Owner ............................ ____________________
  Items back-entered ............... ________
  Elapsed .......................... ________
  Entered with TRUE observation times [ ] yes  [ ] no
  Anything NOT back-entered, and why:
  ____________________________________________________________________

  DATA GAP RECORDED
    From ____________ to ____________   Beds affected ____________________
    Recorded in the operating log       [ ] yes  [ ] no

────────────────────────────────────────────────────────────────────────────
  PART 6, DEBRIEF
────────────────────────────────────────────────────────────────────────────
  1. What did we not have on paper that we needed?
  ____________________________________________________________________
  2. What took longer than expected?
  ____________________________________________________________________
  3. What would have been different at 3 a.m., short-staffed, with a
     deteriorating patient?
  ____________________________________________________________________

  ACTIONS                                        OWNER            DUE
  ____________________________________________   ______________   ________
  ____________________________________________   ______________   ________

────────────────────────────────────────────────────────────────────────────
  OVERALL : [ ] PASS   [ ] PASS WITH ACTIONS   [ ] FAIL, re-drill required
  Re-drill date if required : ____________
════════════════════════════════════════════════════════════════════════════
```

---

# Schedule

| Drill | When | Who | Evidence |
|---|---|---|---|
| Backup | Every night at 02:30, as the `com.orbhospital.backup` job installed on the day | The scheduler | `logs/orb-backup.log`; the heartbeat's backup age |
| Restore, desk drill | Monthly, day 1 at 04:00, as the `com.orbhospital.restore-drill` job (`install-backup-schedule.sh --with-drill`), and by hand after any backup failure | The scheduler, then me reading the log | `logs/orb-restore-drill.log` and the sign-off sheet in section 1.6 |
| Standby restore | Every night at 03:30 on the standby, as the `com.orbhospital.standby-restore` job; monthly I run `standby-restore.sh --check-only` and read the receipt | The scheduler, then me | `logs/orb-standby-restore.log` and `server/ops/standby/last-restore.json` |
| Restore, full rehearsal | **On ceremony day, before go-live**, then quarterly | Me and hospital IT | Sign-off sheet, section 1.6 |
| Downtime | **Before clinician access begins**, then every six months and after any real outage | The ward and me | Sign-off sheet, section 2.8 |
| Key escrow verification | Annually, and at any custodian change, by check digit, without opening an envelope | Chair and custodians | [KEY_CEREMONY.md](KEY_CEREMONY.md) section 10 |

Both drills are hard prerequisites in the shadow-mode readiness checklist in
[SHADOW_MODE.md](SHADOW_MODE.md): "Backups scheduled and running on the
appliance, and one restore actually performed with hospital IT" and "Downtime
print packs generated and the drill rehearsed." Neither is a nice-to-have, and
neither can be signed off from a document. As of today both boxes are
unticked, and they stay unticked until a hospital is in the room.

Keep every completed sheet in the pilot record. When the CIO asks what happens
if the machine dies, the answer is a dated sheet with signatures on it, not a
paragraph.

---

## Related documents

| Document | What it covers |
|---|---|
| [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md) | Backup, verification and the full restore procedure |
| [WARM_STANDBY.md](WARM_STANDBY.md) | The second Mac, the nightly restore onto it, and the 3 a.m. switchover |
| [KEY_CEREMONY.md](KEY_CEREMONY.md) | The keys these drills depend on, and how to open an envelope |
| [INSTALL_RUNBOOK.md](INSTALL_RUNBOOK.md) | Install, and every failure mode with a page |
| [SHADOW_MODE.md](SHADOW_MODE.md) | How the pilot runs, and the readiness checklist these drills satisfy |
| [USER_MANUAL.md](USER_MANUAL.md) | How every module works for the people using it |
