# Warm standby and switchover

**If the primary appliance is down and you are reading this at 3 a.m., skip to
[Switchover](#switchover-the-3-am-procedure). Everything above it is background.**

---

## Why there is a second Mac

The hospital buys the appliance and Orb replaces their HIS. There is no legacy
system underneath. When the Mac Studio dies, PSU, SSD, a dropped logic board, a
macOS update that will not boot, two wards lose charts, orders and the eMAR at
the same moment, and "we have backups, give us a day" is not an answer anyone
will accept.

Three things are easy to confuse, and only the third one saves you at 3 a.m.:

| | What it gives you | Time to a working ward |
|---|---|---|
| **Backup** (`scripts/backup.sh`) | The bytes exist, verified and encrypted | Most of a day |
| **Drill** (`scripts/restore-drill.sh`) | Proof the bytes are restorable | Most of a day |
| **Warm standby** (`scripts/standby-restore.sh`) | A second Mac already holding last night's database, same Orb build, plugged in | **Minutes** |

The day comes from rebuilding a bare Mac: Xcode command line tools, python, the
venv, `sqlcipher3`, Ollama, pulling a 17 GB model, `npm ci && npm run build`,
then the restore. The standby does all of that once, in advance, and then only
re-copies the database each night.

**The standby is 24 hours behind at worst.** That is a deliberate trade, and the
wards must be told about it during a switchover, see step 6.

---

## Building the standby (once)

The standby is a full Orb install that never serves traffic.

1. **Install Orb normally on the second Mac.**
   ```
   git clone <repo> && cd Orb
   ./install.sh
   ```
   This builds the venv (with `sqlcipher3`), pulls models, and builds the SPA.
   All of that downloads, so do it before this Mac joins the hospital network,
   exactly as for the primary (`docs/INSTALL_RUNBOOK.md` section 8): Orb is
   strictly offline. When it asks for an admin account, create one, it is
   harmless, and the nightly restore will replace the whole user table with the
   primary's anyway.

2. **Copy the keys from the primary.** The standby needs **both**:
   - `ORB_DB_ENCRYPTION_KEY`, the database is SQLCipher-encrypted; without the
     exact same key every backup is permanently unreadable.
   - `ORB_UPLOAD_ENCRYPTION_KEY`, a *different* key protecting Lens images and
     Scribe audio (AES-256-GCM per file).

   Copy them by hand into the standby's `.env`. Do not sync `.env`: the standby
   should not inherit the primary's TLS paths.

   > Escrowing only the DB key still loses every image and every dictation.
   > Both keys, in the same envelope, off both machines.

3. **Point the standby at the primary's backups.** The primary writes with
   `--offsite`; the standby reads that same directory.
   ```
   # on the primary
   ./scripts/install-backup-schedule.sh --offsite /Volumes/OrbSync --with-drill

   # on the standby, in .env
   ORB_STANDBY_SOURCE_DIR=/Volumes/OrbSync
   ```
   Any transport works, an SMB share, an rsync target, a physically-carried SSD.
   The script only cares that `orb-backup-*.db` files appear there.

4. **Do NOT start Orb on the standby.** Two Orb servers on one ward LAN, both
   accepting orders, is a worse incident than the outage. The standby runs the
   restore job and nothing else. `standby-restore.sh` refuses to run if it finds
   an Orb answering on `ORB_PORT`, and refuses if the target database's `-wal`
   file was touched in the last 5 minutes.

5. **Install the nightly restore.**
   ```
   ./scripts/install-standby-schedule.sh --hour 3 --minute 30
   ```
   Runs an hour after the primary's 02:30 backup, so it picks up the same night's
   snapshot rather than the previous one.

---

## What the nightly job actually does

`scripts/standby-restore.sh`, in order:

1. Refuses to run if this machine looks like the primary (live HTTP on
   `ORB_PORT`, or a database whose `-wal` is being written).
2. Picks the newest `orb-backup-*.db` in the source directory.
3. **Verifies before touching anything**, size floor, decrypt with the key,
   `PRAGMA integrity_check`, row-count floors on `patients` / `vitals` /
   `audit_logs` / `users`, audit hash-chain linkage, and that at least one
   login-capable admin exists. Same checks as `restore-drill.sh`. *The standby is
   allowed to be behind. It is never allowed to be corrupt.*
4. Stages the verified copy **on the same filesystem** as the target and `mv`s it
   into place, so a power cut leaves the previous database intact.
5. Keeps one generation as `orb.db.previous`.
6. Unpacks the matching `*-uploads.tar` so images and dictation come with the
   chart.
7. Writes `server/ops/standby/last-restore.json`, the receipt, on every exit,
   pass or fail. Nothing reads it automatically and nothing pages anyone: Orb
   has no internet connection. A person reads it in the daily check (`"ok":
   true`, and a `finished_utc` from last night), along with the job's log,
   `logs/orb-standby-restore.log`, where `grep -c '\[standby\]\[fail\]'`
   should print 0. Run by hand, the script's exit code says the same thing:
   0 restored, 1 a precondition failed, 2 verification failed and nothing was
   installed. **A standby that quietly stops restoring is a cold standby
   wearing a warm standby's clothes**, and the receipt is the only thing that
   shows it before you need it.

Verification failure installs **nothing** and exits 2, leaving last night's good
standby in place.

---

## Switchover: the 3 a.m. procedure

> Print this. Tape it inside the cupboard with the appliance.
> Expect this to take **10–20 minutes**, most of it step 1.

### Step 0: Note the time. Write it down.
You will need it for step 6, and for the incident record.

### Step 1: Confirm the primary is really dead. *(Do not skip this.)*
Running two Orbs on the ward LAN, one on the old IP, one on the new, means two
sets of orders and two eMARs, and the chart a nurse sees depends on which
machine their browser cached. That is worse than the outage.

From any machine on the ward:
```
curl -m 3 http://<primary-ip>:8000/api/health
```
- **Answers `{"status":"ok"}`** → the primary is alive. Do not switch over. You
  have a network or client problem, not an appliance problem.
- **Times out / connection refused** → go and look at the machine. Is it powered?
  Screen? If it is up but Orb is not, try restarting Orb first (see
  `docs/DEPLOYMENT.md`), a restart is minutes and loses nothing.
- **The machine is dead or will not boot** → continue.

**Physically power the primary off, or pull its network cable.** This is the step
people skip and regret. It must be incapable of coming back up mid-switchover.

### Step 2: Bring the standby's data as far forward as you can.
The standby holds last night's snapshot. If the primary's disk is still readable
(dead PSU, dead screen, macOS won't boot but the SSD is fine), pull the newest
backup off it, or the live `orb.db`, onto the sync volume first, then:

```
cd <repo> && set -a && . ./.env && set +a
./scripts/standby-restore.sh                 # newest available
```
If you recovered the live `orb.db` itself:
```
./scripts/standby-restore.sh --backup /Volumes/rescue/orb.db
```
It verifies before installing. If verification fails it installs nothing and
tells you to try the previous generation, do that rather than forcing it.

If nothing can be recovered from the primary, skip to step 3; you go live on last
night's data.

### Step 3: Confirm somebody can sign in.
```
server/.venv/bin/python server/scripts/access_review.py
```
Look for `login-capable admins: 1` or more. If it says **0**, the restored user
table has no usable admin and nobody will get past the login screen:
```
server/.venv/bin/python server/scripts/create_admin.py --enroll-mfa
```

### Step 4: Start Orb on the standby.
```
cd <repo> && set -a && . ./.env && set +a
cd server && nohup .venv/bin/python -m uvicorn main:socket_app \
    --host 0.0.0.0 --port 8000 > /tmp/orb.log 2>&1 &
```
Wait for `API server ready`, then:
```
curl -m 5 http://127.0.0.1:8000/api/health
```

The first Sage answer will be slow (cold model, ~1 minute for the 27B). That is
expected and it is not a reason to abort, vitals, charts, orders and the eMAR
are all available immediately; only the AI features wait.

### Step 5: Repoint the ward at this machine.
Whichever of these your site uses:
- **Static IP takeover (fastest):** give the standby the primary's IP. Nothing on
  any ward device needs to change.
- **DNS:** update the `orb.<hospital>.local` A record and flush caches.
- **Neither:** tell the wards the new address. Slowest and most error-prone, 
  prefer IP takeover.

Verify from an actual ward tablet, not from the appliance.

### Step 6: Tell the wards what is missing. *(Clinical safety step.)*
The standby is as fresh as its last restore. Read it:
```
cat server/ops/standby/last-restore.json
```
`finished_utc` is when the standby last took a snapshot; the snapshot itself is
from the primary's backup earlier that night. **Everything charted after that is
gone**, notes, vitals entered by hand, medication administrations, orders.

Announce, in the handover and in Relay, in these words:

> *"Orb has failed over to the backup appliance. Anything charted after
> `<time from step 6>` is not in the system. Re-enter vitals, drug rounds and
> orders from your paper notes for that window. Do not assume a medication was
> given because it is not showing as due."*

The eMAR is the dangerous one: a missing administration record looks exactly like
a dose that was never given.

### Step 7: Restart protection, immediately.
The standby is now the primary and it has **no standby of its own**.
```
./scripts/install-backup-schedule.sh --offsite /Volumes/OrbSync
./scripts/backup.sh                  # do not wait for 02:30
```
Then rebuild a standby on the repaired or replacement Mac.

---

## Failing back

There is no rush and no clever merge. Once the original machine is repaired:

1. Take a fresh verified backup on the **current** (ex-standby) machine.
2. Restore it onto the repaired machine using this same procedure in reverse.
3. Switch over during a quiet window, with the same step 6 announcement.
4. Make the ex-standby the standby again.

**Never merge two databases.** The audit log is a hash chain and the ids are
sequential; two divergent chains cannot be reconciled, and an attempt would
produce a record that fails `verify_audit_chain()`, legally worse than a gap.

---

## Rehearse it

An untested standby is a rumour, exactly like an untested backup.

**Monthly**, on the standby, with the wards untouched:
```
./scripts/standby-restore.sh --check-only     # verifies, installs nothing
```

**Quarterly**, in a maintenance window: do a *real* switchover, steps 1–5, run
a ward round on the standby for an hour, then fail back. The first time you do
this you will find something: a missing key, an unmounted share, a stale model,
a firewall rule. Find it in a maintenance window rather than at 3 a.m.

Record every drill. Date, who ran it, how long steps 1–5 took, what broke.

---

## Known limits: say these out loud before pilot

- **Up to 24 hours of charting is lost** in an unplanned failover. Shortening it
  means more frequent backups (`--hour`/`--minute`, or several jobs a day), which
  is cheap; the real floor is the transport to the sync volume.
- **This is not replication.** No streaming, no synchronous commit, no automatic
  failover. Every step above is a human decision, on purpose, an automatic
  failover on a false positive would put two appliances on the ward LAN.
- **Both keys or nothing.** `ORB_DB_ENCRYPTION_KEY` and
  `ORB_UPLOAD_ENCRYPTION_KEY` must both be escrowed somewhere neither Mac can
  take with it. Lose the DB key and every backup on both machines is permanently
  unreadable, there is no recovery path and no vendor who can help.
- **A shared sync volume is a single point of failure too.** If both Macs mount
  the same NAS and that NAS dies, the standby stops advancing silently. Nothing
  pages anyone when it does; a person reading the receipt's `finished_utc` and
  the job's log in the daily check is what catches it.

## Related

- `docs/BACKUP_AND_RESTORE.md`, backup mechanics, restore drill, key escrow
- `scripts/standby-restore.sh`, the nightly job (`--help` documents every flag)
- `scripts/install-standby-schedule.sh`, installs it as a launchd job
