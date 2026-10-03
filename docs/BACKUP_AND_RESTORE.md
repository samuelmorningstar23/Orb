# Orb Hospital OS: Backup & Restore Runbook

Orb **replaces** the EHR. It is the system of record, not a sidecar. There is no
upstream system holding a second copy of these patients, if `orb.db` is gone and
the backups cannot be read, the clinical record is gone. Everything below follows
from that.

**Audience:** the person who operates the appliance. No prior context assumed.

---

## The short version

| | |
|---|---|
| What is protected | `server/orb.db`, the whole clinical record, and `server/uploads/` (Lens images, Scribe audio) as a tar beside it |
| What is **not** protected | `.env` (both encryption keys) and Ollama model weights, see [§2](#2-what-is-backed-up--and-what-is-not) |
| Where it goes | `backups/orb-backup-YYYYmmdd-HHMMSS.db`, plus an off-box copy if configured |
| How often | Daily at 02:30 via launchd |
| Retention | Newest 14 (~2.2 GB at current size) |
| Encryption | SQLCipher, keyed with `ORB_DB_ENCRYPTION_KEY` |
| **Worst-case data loss** | **Up to 24 hours**, see [§5](#5-how-to-restore) |
| Monthly duty | Run the restore drill. [§4](#4-the-restore-drill) |

Three commands are the whole job:

```bash
cd /path/to/Orb
set -a && . ./.env && set +a          # load the key; every command below needs it

./scripts/install-backup-schedule.sh --offsite /Volumes/OrbBackup   # once
./scripts/restore-drill.sh                                          # monthly
./scripts/backup.sh                                                 # ad hoc, e.g. before an upgrade
```

---

## 1. Why these scripts and not `cp`

`cp orb.db backup.db` on a live database produces a file that *looks* fine and is
silently torn, SQLite is in WAL mode and a plain copy can catch a half-written
page or miss WAL frames entirely. `scripts/backup.sh` delegates to
`server/scripts/backup_db.py`, which uses SQLite's **online backup API** through
Orb's own codec-aware connection. That is transactionally consistent on a hot
database and SQLCipher-aware, so it is safe to run at 02:30 while the ward is
still admitting.

The backup then has to *earn* the right to replace older ones. `backup.sh`
verifies before it rotates: size floor, opens the result with the key, runs
`PRAGMA integrity_check`, and asserts real row counts in `patients`, `vitals`,
`audit_logs` and `users`. **If verification fails it exits 2 and rotates
nothing.** This is not theoretical caution, an earlier version of this script
wrote zero-byte "backups" and pruned the real ones to make room, reporting
success every time. Read the header of `scripts/backup.sh`; it is the best
argument in this repo for why "the backup job succeeded" is not the same as "we
have a backup".

---

## 2. What is backed up: and what is not

**Backed up:** `server/orb.db`. That is patients, vitals, admissions, orders,
medications, notes, clinical documents, messages, users, the audit log, and the
Sage vector/FTS indexes. The overwhelming majority of the clinical record.

**Also backed up (added 2026-07-30):** `server/uploads/`, Lens images and Scribe
audio, as `orb-backup-<same timestamp>-uploads.tar` beside the database, copied
off-box with it and rotated with it. The files inside are already individually
encrypted (AES-256-GCM under `ORB_UPLOAD_ENCRYPTION_KEY`), so the tar is not
re-encrypted; it inherits that protection. Media and database share a timestamp
on purpose, restoring an X-ray from one night against a chart from another is
its own kind of clinical error. Override the source with `ORB_UPLOADS_DIR`.
If the archive step fails, the run still succeeds with a loud warning: a verified
database backup is worth keeping, and a job that fails outright is a job an
operator turns off.

**Not backed up, know this before you rely on any of it:**

| Not covered | Why it matters | What to do |
|---|---|---|
| `.env` | Holds **both** encryption keys. Deliberately excluded, a backup containing its own key is not encrypted in any meaningful sense. | Escrow separately. [§6](#6-the-key-escrow-problem) |
| Ollama model weights | Large, re-downloadable, no PHI. | Re-pull after a rebuild. |

**There are two keys, not one.** `ORB_DB_ENCRYPTION_KEY` unlocks the database;
`ORB_UPLOAD_ENCRYPTION_KEY` unlocks the media. Escrowing one and losing the other
still loses patient data.

---

## 3. The schedule

```bash
cd /path/to/Orb
./scripts/install-backup-schedule.sh --offsite /Volumes/OrbBackup
```

Installs a **LaunchAgent** (`com.orbhospital.backup`) that runs `scripts/backup.sh`
daily at 02:30. Options: `--hour` / `--minute`, `--retain`, `--offsite`,
`--with-drill` (also schedules a monthly drill), `--run-now`, `--uninstall`.
Re-running it is safe, it rewrites and reloads the job.

The key is **not** written into the plist. The job runs
`cd <repo> && set -a && . ./.env && set +a && exec ./scripts/backup.sh`, exactly
what a human types, so `ORB_DB_ENCRYPTION_KEY` lives in one place on disk.

### LaunchAgent, not LaunchDaemon: and what that costs you

A LaunchDaemon runs as root at boot, with nobody logged in. A LaunchAgent runs as
the operator, only while they are logged in. We chose the **Agent**, because
making the job root would mean root reading the operator's `.env` to get the DB
key, concentrating key and ciphertext in one privilege level, which is the exact
thing encryption at rest exists to avoid, and would write root-owned backup
files that the operator can no longer rotate or copy. The full argument is in the
header of `scripts/install-backup-schedule.sh`.

The cost is a hard requirement:

> **Turn on automatic login for the operator account.**
> System Settings ▸ Users & Groups ▸ Automatic login.
> Without it, a power cut or an OS update reboots the Mac to a login window and
> backups stop silently. Re-check this after every macOS update.

This is defensible because the Orb server itself runs in that same user session:
if nobody is logged in, Orb is not running and no new patient data is being
written. The agent is down exactly when there is nothing new to lose. If you ever
move Orb to run headless as a root daemon, move this job with it, and solve the
key problem properly at that point (a dedicated service account, key in the System
keychain, not root reading a dotfile).

### macOS TCC will bite you: the installer checks for it

macOS blocks launchd jobs from reading `~/Documents`, `~/Desktop`, `~/Downloads`
and iCloud Drive. A job whose repo is in one of those directories loads fine,
reports healthy in `launchctl list`, and then fails **every night** with
`Operation not permitted`, the same silent-failure class as the zero-byte backup
bug.

The installer runs a preflight through launchd itself and refuses to report
success if the job cannot reach the repo. If it fires:

1. **Move the repo out of the protected directory** (e.g. `/opt/orb`). Preferred.
   An appliance holding the hospital's system of record should not keep it in a
   user's Documents folder anyway, that path is also iCloud-synced on many Macs,
   which would quietly push PHI off-box and break the no-cloud promise outright.
2. Or grant Full Disk Access to `/bin/bash`. Works, but grants FDA to every bash
   script this machine ever runs.

### Checking on it

```bash
launchctl list | grep orbhospital           # is it registered?
tail -n 50 logs/orb-backup.log              # what happened last night
grep -c '\[backup\]\[fail\]' logs/orb-backup.log   # 0 is what you want
ls -lt backups | head -3                    # newest file should be from last night
launchctl kickstart gui/$(id -u)/com.orbhospital.backup   # run it now
```

The log grows slowly (~20 lines/night) and is never rotated. Truncate it yearly,
or add a `newsyslog.d` entry.

---

## 4. The restore drill

**Monthly. Put it in a calendar. This is the point of the whole document.**

```bash
set -a && . ./.env && set +a
./scripts/restore-drill.sh
```

It takes the newest backup, copies it to scratch, opens it with the key, and
asserts: `integrity_check`, row-count floors on `patients` / `vitals` /
`audit_logs` / `users`, and, the deeper check, that the **audit hash-chain
linkage** is unbroken, each row's `prev_hash` matching the previous row's
`row_hash`. That chain is the compliance artefact; a restore with a broken chain
is indistinguishable from a tampered record.

It never touches the live database. It works on a copy (opening a SQLCipher file
creates `-wal`/`-shm` sidecars and can write to it, a drill that mutates the
thing it is validating is not a drill), it refuses if pointed at the live DB, and
it deletes its scratch copy on exit, including on Ctrl-C. Safe to run mid-clinic.

Exit codes: `0` PASS · `1` could not run · `2` FAIL.

```
[drill]   decrypt+open ...... PASS
[drill]   integrity_check ... PASS
[drill]   rows:patients ..... PASS, 30 rows
[drill]   audit chain ....... PASS, 675 rows, linkage intact

[drill] RESULT: PASS
```

**If it fails**, drill the next backup down (`./scripts/restore-drill.sh <older>`)
to find out whether you have lost one backup or all of them. Treat a failure as an
incident the same day: you currently have no proven recovery path.

Record each drill, date, which backup, PASS/FAIL. "We have backups" is a claim.
"We drilled on the 1st and it passed" is a fact, and it is the one an auditor,
a CIO, or a court will ask for.

---

## 5. How to restore

**Data loss expectation, state it out loud:** backups are daily. A restore loses
**everything since the last backup, up to 24 hours** of vitals, notes and orders.
For a pilot with real patients, decide in advance whether 24h is acceptable or
whether you need a second midday run (`--hour 14` on a second installed job, or
tighten the schedule). Nothing in Orb makes this loss visible to clinicians after
the fact, so the ward must be told what window was lost.

```bash
# 0. Know which backup you are restoring, and prove it BEFORE you touch anything.
./scripts/restore-drill.sh --backup backups/orb-backup-20260730-210824.db

# 1. Stop the server.
#    (however you run it: do not skip this; swapping the file under a live
#     server corrupts both copies)

# 2. Keep the current database. Do not overwrite it. If the restore is wrong
#    you want the option to go back.
mv server/orb.db server/orb.db.broken-$(date +%Y%m%d-%H%M%S)

# 3. Put the backup in place.
cp backups/orb-backup-20260730-210824.db server/orb.db
chmod 600 server/orb.db

# 4. Remove stale sidecars: they belong to the OLD file and will corrupt the new one.
rm -f server/orb.db-wal server/orb.db-shm

# 5. Restore the media too (see §2): the uploads archive with the SAME timestamp.
#    tar -xf /Volumes/OrbBackup/orb-backup-20260730-210824-uploads.tar -C server/

# 6. Start the server with the SAME ORB_DB_ENCRYPTION_KEY.
set -a && . ./.env && set +a
# ... start Orb ...

# 7. Verify from the outside: log in, open a patient, confirm recent vitals,
#    check the audit log renders. Then tell the ward what time window was lost.
```

Step 0 is not optional ceremony. Discovering the backup is unreadable *after*
you have moved the live database is how a bad day becomes a catastrophic one.

---

## 6. The key escrow problem

**Read this paragraph twice.**

Every backup is SQLCipher-encrypted with `ORB_DB_ENCRYPTION_KEY`. **The key is not
in the backup.** It lives in `.env` on the appliance, the same machine the
backups exist to survive. If that machine dies, or `.env` is lost, or someone
rotates the key without keeping the old one, **every backup you hold is
permanently unreadable ciphertext.** There is no recovery, no vendor override, no
brute force. The clinical record is gone while you are looking at 14 files that
appear to contain it.

Corollaries people get wrong:

- **Rotating the key strands every backup taken under the old one.** There is no
  re-key tooling. If you rotate, keep the old key until the last backup written
  under it has aged out of retention.
- **There are two keys.** `ORB_UPLOAD_ENCRYPTION_KEY` protects Lens images and
  Scribe audio. Escrow both, together, or you restore a record with holes in it.
- **The off-box copy does not solve this.** Shipping ciphertext to a second
  location without shipping the key just means the unreadable files are in two
  places.

### The options, honestly

| Option | Strengths | Real costs |
|---|---|---|
| **Printed key in a hospital safe** | Survives every digital failure, ransomware, disk death, cloud outage. Hospitals already run controlled physical custody, so it fits existing process. Trivial to audit: the envelope is sealed or it is not. | Only as good as the safe's key discipline. Someone must physically retype 64 characters under pressure at 3am, build in a checksum and *test the transcription*. Silent single point of failure if the building floods or burns. |
| **Second sealed offline copy** (USB/paper, different building) | Removes the single-site risk of the safe. Cheap. Still fully offline. | Two copies, two custody chains, two things to remember to update on rotation. A forgotten stale copy is worse than none, it will be tried first in an emergency and will fail. |
| **macOS Keychain on a second Mac** | Encrypted at rest, no transcription errors, easy to script the retrieval. | The recovery path now depends on a *second computer* booting, unlocking, and having a working account, precisely the assumption that failed on machine one. Keychains are also lost on OS reinstall more often than people expect. Not a standalone answer; fine as a convenience copy alongside a paper one. |
| **A managed KMS** (AWS KMS, Vault, etc.) | Proper access control, audit trail, rotation workflow, no human transcription. The mature answer at scale. | **Breaks the no-cloud promise**, which for Orb is not a detail, it is the product's central claim to Indian hospital CIOs, and part of the data-residency story. It adds a network dependency to disaster recovery: the day the appliance dies, you need working internet and valid cloud credentials to read your own patients' records. Self-hosted Vault keeps data local but is now a second production system to run, back up, and escrow the key *for*. |

### What I would do, and why

**Printed key in the hospital safe, plus a second sealed envelope in a different
building. Paper primary, Keychain on the admin's Mac as a convenience copy only.**

The reasoning: this is a **one-appliance, on-prem, no-cloud** product in an Indian
hospital. The realistic disasters are the mundane ones, the Mac Studio dies, the
SSD fails, someone reinstalls macOS, the person who set it up leaves. Paper in a
safe survives all of those, has no dependencies, and cannot be ransomwared. The
recovery cost, retyping 64 characters, is paid at most once, in the one scenario
where an hour of typing is irrelevant.

I would not reach for a KMS here. Not because it is worse engineering, it is
better engineering, but because it contradicts the promise the product is sold
on, and it makes disaster recovery depend on the internet on the exact day you
have least control over your environment. Revisit that when Orb runs in more than
one hospital and key custody becomes an operational burden rather than an
envelope.

Whatever you choose, do these three things:

1. **Escrow both keys** the day the appliance goes live, before real patients.
2. **Test the escrow copy**, read the key from the envelope onto a different
   machine and run `./scripts/restore-drill.sh` with it. An untested escrow is a
   guess about your own handwriting.
3. **Write down who can open the safe**, and make sure it is more than one living
   person.

---

## 7. Off-box copies

**An on-appliance backup does not survive the appliance.** Fourteen verified
backups sitting on the same SSD as `orb.db` protect you against exactly one
failure, someone deleting rows. They protect you against nothing else: not a
dead Mac Studio, not a failed SSD, not theft, not fire, not ransomware that walks
the filesystem.

```bash
./scripts/install-backup-schedule.sh --offsite /Volumes/OrbBackup
```

`backup.sh` copies each *verified* backup to that destination and `chmod 600`s it.
If the volume is not mounted it warns loudly and keeps the local copy, it does
not fail the backup, but that warning in `logs/orb-backup.log` means you are
running with no off-box protection and should be treated as an alert.

Practical options, in the spirit of the appliance: an external SSD that lives in a
different room and is swapped weekly; a second Mac or NAS on the hospital LAN;
encrypted media in the same safe as the key envelope, **in a separate sealed
envelope**, because key and ciphertext in one container is a single point of
compromise, not a backup strategy.

Because the file is already SQLCipher-encrypted, the destination does not need to
be trusted for confidentiality. It does need to be trusted for **availability**,
and it needs the same retention discipline, a full off-box volume silently stops
receiving copies.

---

## 8. When something is wrong

| Symptom | What it means | Do this |
|---|---|---|
| `logs/orb-backup.log` shows `Operation not permitted` | macOS TCC, see [§3](#macos-tcc-will-bite-you--the-installer-checks-for-it) | Move the repo out of `~/Documents`, or grant FDA to `/bin/bash` |
| No new file in `backups/` this morning | Job not loaded, or the user was logged out | `launchctl list \| grep orbhospital`; check auto-login |
| `[backup][fail] verification FAILED` | The snapshot was unusable, **nothing was rotated**, older backups are intact by design | Check disk space and that `ORB_DB_ENCRYPTION_KEY` is right; run `./scripts/backup.sh` by hand |
| Drill says `decrypt+open ... FAIL` | The key in your environment is not the key that backup was written with, or the file is damaged | Try the escrow copy of the key; drill an older backup |
| Drill says `audit chain ... FAIL` | That backup's audit chain is broken, do not treat it as a compliance record | Drill older backups to find the last good one; treat as an incident |
| `ORB_DB_ENCRYPTION_KEY is not set` | You forgot `set -a && . ./.env && set +a` | Run it, retry |

---

## 9. Known gaps

Honest list, so nobody is surprised in front of a hospital CIO:

- **The media needs a second key.** `server/uploads/` has been archived beside
  every backup since 30 July, but the files inside are encrypted under
  `ORB_UPLOAD_ENCRYPTION_KEY`, so escrowing the database key alone still loses
  the images and recordings. Both keys go in the envelopes.
- **Daily cadence means up to 24h of loss.** Acceptable for a pilot only if the
  ward is told; not obviously acceptable for a live hospital.
- **Nothing pages anybody yet.** A failed backup writes to a log file. The
  outbound heartbeat reports backup age and the receiver in `ops/` can raise it
  on a phone, but only once both ends are stood up and the simulate drill has
  passed. Until then the daily check is the alarm, and the monthly drill needs
  a calendar event with a human name on it.
- **Restore has never been done end-to-end on this appliance**, the drill proves
  the backup is readable, which is most of the risk, but not the swap-and-boot
  path. Do one full rehearsal on a spare machine before go-live.
- **Key escrow is unresolved** until you act on [§6](#6-the-key-escrow-problem).
  Until then, this appliance's entire clinical record depends on one `.env` file
  on one Mac.

---

## See also

- `scripts/backup.sh`, the snapshot + verify + rotate script (read its header)
- `scripts/install-backup-schedule.sh`, the launchd installer and the Agent/Daemon argument
- `scripts/restore-drill.sh`, the monthly proof
- `server/scripts/backup_db.py`, the SQLCipher-aware online-backup engine
- `server/scripts/encrypt_db.py`, encryption-at-rest migration and its verification
- `docs/DEPLOYMENT.md` §11, encryption at rest, key custody
