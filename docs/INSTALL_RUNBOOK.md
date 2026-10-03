# Orb Install Runbook

*Version 2 · first written 1 August 2026, updated 3 September 2026 · For
whoever installs and operates the Orb appliance, which for the pilot is me,
and for the hospital IT staff who will stand next to me at 3 a.m.*

This document has two halves.

**Part 1, Install.** How to take a Mac Studio out of a box and end up with an
appliance that is holding real patient records safely. It is a checklist, in
order, with the reason for each step. The steps that look like bureaucracy are
usually the ones that have already gone wrong once, on my machine, this year.

**Part 2, Failure modes.** One page per way this thing breaks. Symptom,
diagnosis, fix, who to call. Every page in Part 2 describes something that has
actually happened on this codebase, or a failure the code explicitly defends
against. Nothing here is imagined, and where a fix has not been re-measured I
say so.

If you are reading this because something is broken right now, go to
[Part 2](#part-2-failure-modes) and find your symptom in the
[index](#failure-index). Do not read Part 1 first.

Everything below was re-checked against the code on 3 September 2026. Where a
number comes from a measurement, the date of the measurement is next to it.

---

## Contents

**Part 1, Install**
1. [Before the day](#1-before-the-day)
2. [Hardware prep](#2-hardware-prep)
3. [macOS account model](#3-macos-account-model)
4. [FileVault](#4-filevault)
5. [OS hardening](#5-os-hardening)
6. [Where the software lives](#6-where-the-software-lives)
7. [Network placement](#7-network-placement)
8. [Install Orb](#8-install-orb)
9. [First boot and the admin account](#9-first-boot-and-the-admin-account)
10. [MFA enrolment and role review](#10-mfa-enrolment-and-role-review)
11. [Ingestion configuration](#11-ingestion-configuration)
12. [Backups and the schedule](#12-backups-and-the-schedule)
13. [Acceptance test](#13-acceptance-test)
14. [Handover](#14-handover)

**Part 2, Failure modes**
- [Failure index](#failure-index)
- [The three numbers to write on the wall](#the-three-numbers-to-write-on-the-wall)
- [F1, The appliance is frozen](#f1-the-appliance-is-frozen)
- [F2, No new backup this morning](#f2-no-new-backup-this-morning)
- [F3, Backup verification failed](#f3-backup-verification-failed)
- [F4, The restore drill failed](#f4-the-restore-drill-failed)
- [F5, Disk full](#f5-disk-full)
- [F6, AI features degraded, model not loaded](#f6-ai-features-degraded-model-not-loaded)
- [F7, Monitor feed stopped / ingestion gap](#f7-monitor-feed-stopped--ingestion-gap)
- [F8, Database is locked](#f8-database-is-locked)
- [F9, Port 8000 in use, orphaned processes, or the server will not stop](#f9-port-8000-in-use-orphaned-processes-or-the-server-will-not-stop)
- [F10, Nobody can sign in](#f10-nobody-can-sign-in)
- [F11, The screens stopped updating by themselves](#f11-the-screens-stopped-updating-by-themselves)
- [F12, Sage is slow during a busy period](#f12-sage-is-slow-during-a-busy-period)
- [F13, The appliance rebooted](#f13-the-appliance-rebooted)
- [F14, Scribe cannot transcribe](#f14-scribe-cannot-transcribe)
- [Escalation](#escalation)
- [What to write down after every incident](#what-to-write-down-after-every-incident)

---

# Part 1: Install

## 1. Before the day

Do not travel to the hospital until all of these are true. Each one is a
half-day lost if it is discovered on site.

| Item | Why it blocks |
|---|---|
| The Mac Studio is on site, unboxed, and powered | It is the appliance. There is no cloud half. |
| A wired network port exists where the appliance will live, on the ward's clinical VLAN | Wi-Fi is not acceptable for the system of record |
| Someone from hospital IT is available for two hours | Network, VLAN, firewall and the ward's own device list are theirs, not mine |
| The physician champion knows the install is happening | They own the clinical decisions this install makes possible |
| An external SSD for off-box backups | An on-appliance backup does not survive the appliance |
| A second Mac for the standby, if the hospital has agreed to one | The nightly restore onto it is the only automated proof that backups restore ([WARM_STANDBY.md](WARM_STANDBY.md)) |
| Two tamper-evident envelopes and a working printer | The key ceremony is done the same day, see [KEY_CEREMONY.md](KEY_CEREMONY.md) |
| The model weights are already downloaded | `medgemma:27b` is 17 GB and `medgemma:4b` is 3.3 GB on disk |

Bring on a USB drive: the Orb source, the Ollama installer, the Node
installer, and the model weights. Assume the hospital network will not let you
download 20 GB on the day. It usually will not.

**Allow a full day.** A rushed install of a system that holds patient records
is not a saving.

---

## 2. Hardware prep

1. **Power.** The appliance goes on the ward's UPS or protected circuit if one
   exists. If it does not, say so in writing. An unprotected appliance loses
   up to 24 hours of data on a power cut (see [F13](#f13-the-appliance-rebooted)).
2. **Physical security.** Locked room, locked cupboard, or a locked rack. The
   database is encrypted at rest, but disk encryption protects copies, not a
   running machine somebody can sit down at. Physical access is admin access.
3. **Sizing.** Everything I have measured was measured on an Apple M2 Max with
   64 GB of unified memory. The 27B model takes 17 GB resident, the 4B takes
   3.3 GB, the search models and speech engine another 6 GB or so, and all of
   it has to stay in memory at once. The full argument and the pilot bill of
   materials are in [HARDWARE_SPEC.md](HARDWARE_SPEC.md). Below 64 GB the
   models contend for memory and none of the latency numbers in `docs/perf/`
   hold.
4. **Disk headroom.** Four things grow: the database, its write-ahead log, the
   rotating backups, and the export directory. On my development machine on
   3 September 2026, with 31 patients and a month of simulated monitor
   vitals: database 716 MB, of which the shadow instrument's event table is
   the largest single thing at 881,548 rows; backups 619 MB each, and the
   schedule keeps 14, so budget about 9 GB; exports 277 MB; uploads 22 MB. A
   real ward at monitor cadence grows faster than a simulator. Set a floor of
   200 GB free and check it weekly.
5. **Sleep.** Set the display to sleep and the computer never to sleep. An
   appliance that sleeps stops ingesting vitals and stops running its backup.

---

## 3. macOS account model

Create exactly one account, the **operator account**. Orb runs in that
account's session, and so does the backup job.

- Name it something institutional (`orbops`), not a person's name. People
  leave.
- Give it a strong password and record where that password lives.
- **Enable automatic login** for this account.
  System Settings ▸ Users & Groups ▸ Automatic login.

Automatic login looks like a security regression, and I want the reason it is
the right call written down rather than taken on faith.

The nightly backup runs as a launchd **LaunchAgent**, not a LaunchDaemon. A
LaunchDaemon would run as root at boot with nobody logged in, but to read the
database encryption key it would have to read the operator's `.env`, which
puts the key and the ciphertext it protects at the same privilege level on the
same machine. That is what encryption at rest exists to prevent. It would also
write root-owned backup files the operator can no longer rotate.

So the job runs as the operator, which means it only runs while the operator
is logged in. Automatic login is what makes an unattended reboot come back to
a logged-in session. The trade is acceptable because the Orb server also runs
in that session: if nobody is logged in, Orb is not running, no new patient
data is being written, and the backup is idle at exactly the moment there is
nothing new to lose. The failure domains match.

The full argument is in the header of `scripts/install-backup-schedule.sh`.

> **Re-check automatic login after every macOS update.** Updates turn it off.
> That is [F2](#f2-no-new-backup-this-morning), and it is silent.

---

## 4. FileVault

Turn FileVault on. System Settings ▸ Privacy & Security ▸ FileVault.

**Store the FileVault recovery key with the hospital, not with Apple, and not
in an iCloud account.** Write it on the install record.

FileVault is not redundant with Orb's own database encryption, and the
difference matters when a CIO asks:

- **SQLCipher** encrypts `orb.db` and its sidecars. It protects the database
  file wherever it travels: a nightly backup, an export, a cloned volume, a
  decommissioned SSD. It does not protect a running machine, because the key
  is in `.env` on the same disk.
- **FileVault** encrypts the whole volume, including the things Orb does not
  encrypt individually.

That second point is concrete. The daily FHIR self-export and the hourly ward
print packs are written to `server/downtime_exports/` as **plain JSON and
plain HTML**. The directory is mode `0700` and the files are `0600`, so no
other user account can read them, but they are not ciphertext. On my machine
that is 277 MB of readable PHI. FileVault is what protects it from a stolen or
decommissioned disk. Do not skip it.

---

## 5. OS hardening

Work through this list and tick each line on the install record.

| Setting | Where | Value | Why |
|---|---|---|---|
| Firewall | Privacy & Security ▸ Firewall | **On**, block all incoming except signed apps you approve | The appliance serves the ward, not the internet |
| Remote Login (SSH) | General ▸ Sharing | **Off** | The pilot has no inbound remote access, by design. Support is on-site only. |
| Screen Sharing / Remote Management | General ▸ Sharing | **Off** | Same |
| File Sharing, Printer Sharing, Media Sharing | General ▸ Sharing | **Off** | Reduce the surface; none are needed |
| iCloud Drive, Desktop & Documents sync | Apple Account ▸ iCloud | **Off** | Sync would push PHI off-box and break the no-cloud promise outright |
| Automatic software updates | General ▸ Software Update | **Download only, do not auto-install** | An unattended OS update reboots the appliance and can disable automatic login |
| Screen lock | Lock Screen | Require password **immediately** after screensaver | The console is an unauthenticated admin session |
| Guest user | Users & Groups | **Off** | |
| Time Machine | General ▸ Time Machine | **Off**, or explicitly excluded from the Orb repo | Time Machine to an unencrypted or shared destination is an uncontrolled PHI copy |
| Analytics & Siri | Privacy & Security | **Off** | Nothing about a clinical appliance should phone home |

**Do not install anything else on this machine.** It is an appliance, not a
workstation. Every extra package is something that can hold a mutex, eat a
core, or fill the disk at 2 a.m.

---

## 6. Where the software lives

Put the repo at **`/opt/orb`**. Not in `~/Documents`, not in `~/Desktop`, not
in `~/Downloads`, and not anywhere under iCloud Drive.

Two independent reasons, and both have bitten me:

1. **macOS TCC.** launchd jobs are blocked from reading those directories. A
   backup job whose repo sits in `~/Documents` loads cleanly, reports healthy
   in `launchctl list`, and then fails every night with `Operation not
   permitted`. `scripts/install-backup-schedule.sh` now preflights this
   through launchd itself: it installs a temporary agent, kicks it, waits for
   it to prove it can read the repo, and refuses to report success if it
   cannot. The preflight only helps if you read its output.
2. **iCloud.** `~/Documents` is iCloud-synced on many Macs by default. That
   would quietly copy patient data out of the building.

```bash
sudo mkdir -p /opt/orb
sudo chown -R "$(whoami):staff" /opt/orb
# copy the source into /opt/orb
```

If you must use another location, it has to be outside all four protected
directories and outside iCloud, and the installer's preflight must pass.

---

## 7. Network placement

**The appliance answers only on the ward's clinical network. Nothing from the
internet reaches it.**

Agree these five things with hospital IT, in writing, before you plug in:

1. **A static IP or a DHCP reservation.** The ward's tablets and station
   desktops will be pointed at it by address. It cannot move.
2. **A DNS name** on the hospital's internal resolver, for example
   `orb.hospital.local`. Use the name everywhere, so a future IP change is one
   DNS edit rather than a ward-wide re-configuration.
3. **No inbound NAT, no port forward, no VPN route from outside.** The pilot's
   support model is on-site only. If the hospital wants remote support later,
   that is a separate written decision with its own controls, not a default.
4. **Which monitors will reach it, and how.** Whatever pushes vital signs
   needs a route to the appliance, either to port 8000 for the HTTP ingestion
   endpoints or to port 2575 if Orb's own HL7 listener is used (section 11).
   Biomedical engineering owns this side.
5. **Outbound.** Orb needs no internet to run. Leave outbound open only if the
   hospital is comfortable with it. The one thing that uses it is the operator
   heartbeat, which carries no patient data: its payload is a fixed allowlist
   of operational keys and it makes no database read at all. The sender ships
   **disabled** and does nothing unless `ORB_HEARTBEAT_URL` is set. The
   receiving end that turns a missed beat into a phone call is
   `ops/heartbeat_receiver.py`, which I run on a box outside the hospital; set
   it up in [Stand up the pager](#stand-up-the-pager-the-heartbeat-receiver).
   Until both ends are live, nothing pages anybody. See
   [WARM_STANDBY.md](WARM_STANDBY.md) and [Escalation](#escalation).

### Bind address and TLS

Two settings people get wrong on the first install:

- **`ORB_HOST` defaults to `127.0.0.1`**, and the installer writes that value.
  It means only the appliance itself can reach Orb; bedside tablets and
  nurse-station desktops get nothing. Set `ORB_HOST=0.0.0.0` so it listens on
  the LAN.
- **`ORB_CORS_ORIGINS` must contain the exact origin staff will type**, for
  example `https://orb.hospital.local:8000`. If it does not, the realtime
  Socket.IO connection is rejected, the boards stop updating by themselves,
  and the browser's retries soon make every screen hang for 15 to 25 seconds
  at a time, see
  [F11](#f11-the-screens-stopped-updating-by-themselves).

TLS in transit is not optional on a ward network. The installer generates a
self-signed certificate (4096-bit, one year, common name `localhost`) and
points `ORB_TLS_CERT` and `ORB_TLS_KEY` at it. That is a development
certificate. Either:

- replace it with a certificate from the hospital's CA at the same paths, or
- terminate TLS at a reverse proxy on the same box, unset both variables and
  set `ORB_TLS_TERMINATED_UPSTREAM=1` to declare that you did.

Running the `prod` profile with neither logs a warning at startup that patient
data must not travel in cleartext, and then serves in cleartext. Do not ship
that.

The Trust Center's "TLS in transit" row reads the certificate Orb serves: it
fails on an expired or unreadable certificate or a missing key, is amber on the
installer's self-signed certificate, on one that names no host or on one that
expires within 30 days, and is green on a CA-issued certificate. Behind a proxy
(`ORB_TLS_TERMINATED_UPSTREAM=1`) it stays amber, because the application
cannot see the proxy; there the evidence is the proxy certificate and the
network diagram. With neither, it is red in the `prod` profile.

Orb sets its own security headers on every response: `X-Content-Type-Options:
nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: SAMEORIGIN` and a
`Content-Security-Policy` that forbids framing by other sites, plugins and
`<base>` rewriting. `Strict-Transport-Security` is sent only on a connection
that is already HTTPS (directly, or as the proxy reports in
`X-Forwarded-Proto`). A proxy in front does not need to add them.

---

## 8. Install Orb

> **Use `./install.sh`, not `./setup-mac.sh`.**
> `setup-mac.sh` is my developer script. It writes `ORB_DEPLOYMENT_PROFILE=dev`
> and `ORB_DEMO_MODE=true`, and it seeds demo clinicians whose password is
> `password`. That is correct for my laptop and catastrophic on a ward.

### 8.1 Prerequisites

```bash
python3 --version     # 3.11 or newer; I build and test on 3.12
node --version        # 20 or newer; I build on 22
openssl version       # the installer stops without it
ollama --version
```

### 8.2 Run the installer

```bash
cd /opt/orb
./install.sh
```

It is idempotent and safe to re-run. It will not overwrite an existing
`.env`, existing certificates, or data. In order it: creates `server/.venv`
and installs `server/requirements.txt`; generates `.env` (mode `0600`) with
`ORB_DEPLOYMENT_PROFILE=prod` and fresh `openssl rand -hex 32` values for
`ORB_JWT_SECRET`, `ORB_INGESTION_API_KEY`, `ORB_DB_ENCRYPTION_KEY`,
`ORB_UPLOAD_ENCRYPTION_KEY` and `ORB_DISCHARGE_JWT_SECRET` (the patient
discharge-portal signing key, kept apart from the staff key; the `prod`
profile refuses to start without it, or with it equal to `ORB_JWT_SECRET`),
plus `ORB_REQUIRE_UPLOAD_ENCRYPTION=1`; generates the self-signed certificate; runs the
migrations; creates the first admin with MFA enrolment, and fails the install
if no administrator exists at the end; builds the web application into
`./dist`.

**Because `ORB_DB_ENCRYPTION_KEY` is generated here, the database created
from this point on is encrypted at rest from birth.** That key is now the
single most important secret in the building. The key ceremony happens today,
not next week: [KEY_CEREMONY.md](KEY_CEREMONY.md).

### 8.3 Pull the models

```bash
ollama pull medgemma:27b            # primary clinical tier, 17 GB
ollama pull medgemma:4b             # ambient tier, 3.3 GB
ollama list                         # confirm both are present
```

Use `medgemma:27b` from the official Ollama library. Do **not** substitute
`alibayram/medgemma:27b`. That community build packages the text-only
weights and cannot take images, which breaks Lens image analysis silently,
with no error message. `setup-mac.sh` checks that each pulled model reports
the `vision` capability; do the same by hand with `ollama show medgemma:27b`.

### 8.4 Edit `.env` for the appliance

Open `/opt/orb/.env` and set:

```bash
ORB_DEPLOYMENT_PROFILE=prod          # fail-closed secret checks; no auto-reload
ORB_HOST=0.0.0.0                     # reachable from the ward
ORB_PORT=8000
ORB_CORS_ORIGINS=https://orb.hospital.local:8000   # the exact origin staff type

ORB_LLM_MODEL=medgemma:27b
ORB_AMBIENT_LLM_MODEL=medgemma:4b    # see the note below; the template defaults this to the 27B
ORB_OLLAMA_KEEP_ALIVE=-1             # never idle-unload the models (the code default is 1h)

ORB_INGESTION_MODE=ingestion         # REAL DATA ONLY, see section 11
ORB_DB_POOL=1                        # the code default; written so it is visible, see the note below
ORB_REQUIRE_UPLOAD_ENCRYPTION=1      # uploads fail closed rather than silently storing cleartext
ORB_MFA_REQUIRED_ROLES=admin         # see section 10
```

**The ambient tier.** `.env.example` sets `ORB_AMBIENT_LLM_MODEL` to the 27B,
and if the variable is absent the code also defaults it to the 27B. Ambient
work (Relay classification, alert garnish, note extraction) is frequent and
small, and it belongs on the 4B. Set it explicitly. During the 1 August alert
storm the ambient model alone ran the GPU at a 76 % duty cycle; that is the
load you do not want on the model a clinician is waiting for.

**Pooling.** `ORB_DB_POOL=1` reuses pooled database connections instead of
opening and closing one per query. It is the code's default: an appliance
that boots with no `ORB_DB_POOL` line at all is pooled, so the line above
changes nothing and is written only so the setting is visible. It is on
deliberately. Connection open/close churn on a large SQLCipher database was
the trigger for the 1 August deadlock ([F1](#f1-the-appliance-is-frozen)),
and pooling is the state the soak test passes in.

**The value that matters is `0`, and nothing should ever write it.**
`ORB_DB_POOL=0` selects the path that froze the appliance. `.env.example`
carries no `ORB_DB_POOL` line for exactly that reason. Until 14 August it
shipped a commented-out `ORB_DB_POOL=0` under the words "Off by default", and
an operator following that template would have re-enabled the freeze. Verify
the effective state rather than the presence of the line:
[F8](#f8-database-is-locked) has the one-liner.

Keep `.env` at mode `600`:

```bash
chmod 600 /opt/orb/.env
```

### 8.5 Confirm encryption is actually on

Setting the key is not the same as the database being encrypted. If the
SQLCipher driver is not importable, the application **logs a warning and runs
on plaintext**. It does not fail. I found this on my own machine in July: key
set, hook present, file plaintext. Check, do not assume:

```bash
cd /opt/orb/server
.venv/bin/python scripts/encrypt_db.py --check
```

Then start the server once and read the first twenty lines of its log. A line
saying the key is set but no SQLCipher driver is installed and it is
`falling back to PLAINTEXT sqlite3` means the database is not encrypted and
you must stop and fix it before any patient data arrives.

---

## 9. First boot and the admin account

```bash
cd /opt/orb
set -a && . ./.env && set +a
cd server && ../server/.venv/bin/python main.py
```

Watch for these lines. The first is the startup banner naming the profile, and
it must say `prod`.

```
Database backend: sqlite (pool=on)
Model 'medgemma:27b' pre-loaded (GPU warm, keep_alive=-1)
Model 'medgemma:4b' pre-loaded (GPU warm, keep_alive=-1)
Serving built SPA from /opt/orb/dist (single-origin deploy)
Mode: INGESTION
API server ready on http://0.0.0.0:8000
```

If `keep_alive` reads `1h` you did not set `ORB_OLLAMA_KEEP_ALIVE=-1`; the
models will be evicted after an hour idle and every first question of the
morning will pay a cold start.

Then from another machine on the ward network:

```bash
curl -fsS https://orb.hospital.local:8000/api/health
# {"status":"ok","service":"orb-hospital-os","version":"0.1.0","modules":[...]}
```

### The admin account

There are **no default credentials**. Migrations alone leave no account
anyone can sign in with. `install.sh` runs `server/scripts/create_admin.py`;
if you skipped the prompts, run it now:

```bash
cd /opt/orb && set -a && . ./.env && set +a
server/.venv/bin/python server/scripts/create_admin.py --enroll-mfa
```

The minimum password length is 12 characters. The script is idempotent: if a
real admin already exists it says so and exits without doing anything.

Ignore the line `Sign in: DOC001 / password` printed by `run-orb.sh`. That
refers to my demo seed and must never exist on this machine.

### Make it start on boot

The server must come back after a power cut without a human. Install a
LaunchAgent for it in the operator's session (the same reasoning as section
3), or use the hospital's preferred supervision method. Whichever you choose,
prove it: reboot the appliance and confirm `/api/health` answers without
anyone touching a keyboard. Write the result on the install record.

---

## 10. MFA enrolment and role review

### Enrol

Orb implements TOTP multi-factor authentication (RFC 6238), compatible with
Google Authenticator, Authy or 1Password. Enrolment is self-service and
two-phase: the server issues a secret and an `otpauth://` URI, and MFA only
switches on after the user has entered one live code, so nobody can lock
themselves out with a mis-scanned QR code. Verification accepts one 30-second
step of clock skew either side and refuses a code that has already been used.

Enrol the admin account first, from the Security section of the app, or let
`create_admin.py --enroll-mfa` do it at creation.

### Enforce

**This is the step that is easy to skip and easy for a CIO to catch.**

`ORB_MFA_REQUIRED_ROLES` is **empty by default**, which means MFA is
available to everyone and mandatory for nobody. The compliance report says so
in as many words: with the variable unset it reports the MFA control as a
warning, "TOTP MFA is available per-user but no role is currently mandated",
with the org-wide adoption percentage next to it.

Set it at install:

```bash
ORB_MFA_REQUIRED_ROLES=admin
```

With that set, an admin who has not enrolled cannot get a session. The login
returns HTTP 403 with `mfa_enrollment_required` and a ten-minute enrolment
ticket scoped to the enrolment endpoints and nothing else, so the account can
still complete enrolment but can do nothing until it has.

Decide with the CIO whether clinicians are also in scope. My pilot decision is
**MFA for admins, fast authentication for clinicians**. A code at every login
on a ward is a real safety cost, and it is a deliberate trade, not an
oversight. Write the decision down either way.

### Role review

Six roles are enforced server-side by permission, not by hiding buttons. The
list is defined once, in `server/roles.py`, and the permissions in
`server/permissions.py`.

| Role | In one line |
|---|---|
| `doctor` (alias `clinician`) | Full clinical read/write. The only role that can prescribe, sign, and publish a discharge. |
| `nurse` | Clinical read, vitals, medication administration, draft notes. No prescribing, no signing, no discharge publish. |
| `pharmacist` | Pharmacy workflow, procurement requests, and Sage for context. No general chart access, and cannot approve a purchase. |
| `admin` | Governance, audit, user management, and a read-only clinical chart. Also the back-office desks (billing, payments, payer, front desk, procurement), split out into their own permissions on 18 August so they can be handed to non-admin staff. **Plus** the two clinical writes below. |
| `cashier` | The cash counter as its own account type since 18 August: record and read payments, close the counter. Cannot approve a payment leaving. No seeded user holds this role; the hospital creates them. |
| `patient` | Their own record only, through the Bridge portal |

The `admin` role cannot write the patient record, chart vitals, prescribe,
place an order, sign a note, publish a discharge, or execute a Relay action
card. It **can** do two things that are clinical writes, and I say both out
loud in the CIO review rather than let them be found: `helix.write` (the
pharmacy workflow, including verifying an AI-drafted medication into an
active order a nurse can administer) and `surgery.write` (surgical cases and
the WHO checklist). Splitting those off the admin role is on my list; as of
today they are still there.

Do not read this table out as the authority. Trust Center ▸ **Show matrix**
renders the grid live from `server/permissions.py`, with all six roles, and
that is the answer to "what can your account do?"

Before go-live, sit with the champion and walk the actual user list. Every
account gets a named human and a justified role. Delete anything left over
from testing.

---

## 11. Ingestion configuration

**`ORB_INGESTION_MODE` must be `ingestion` on this machine.**

| Value | Behaviour |
|---|---|
| `ingestion` | Real data only. The simulator does not run. **This is the appliance setting, and the code default.** |
| `simulation` | Synthetic patients and synthetic vitals. Ingestion endpoints return 503. |
| `hybrid` | Both. |

Flipping a live appliance to `simulation` or `hybrid` starts writing invented
clinical data into a real patient database. There is no undo for that beyond
a restore.

### Two ways vitals can arrive

**Over HTTP, from a gateway.** Real observations arrive at these endpoints,
authenticated with the ingestion API key, checked in constant time:

| Endpoint | Payload |
|---|---|
| `POST /api/ingest/json` | Structured JSON observations |
| `POST /api/ingest/hl7` | Raw HL7 `ORU^R01` messages |
| `POST /api/ingest/fhir` | FHIR Observation resources |
| `POST /api/ingest/replay` | A buffered batch, replayed with the original observation times |
| `GET /api/ingest/status` | Recent ingestion events and their parse status |

The write endpoints take `X-API-Key: <ORB_INGESTION_API_KEY>`, plus optional
`X-Correlation-Id` for tracing and `X-Message-Id` for de-duplication. Ask
whoever configures the gateway to send `X-Message-Id`, because it is what
makes a retry safe: the id is stored with the event and a repeat is dropped.

`GET /api/ingest/status` is different: it is **not** an API-key endpoint. It
requires a signed-in session holding the `ingest.monitor` permission, which
in practice means an admin account. You read it from the app, or with a
session cookie, not with the ingestion key.

**Directly, over HL7 MLLP.** Orb also has its own HL7 v2 listener, so a
monitor or a central station that speaks MLLP can talk to the appliance
without a gateway in between. It is off by default and configured entirely
from `.env`:

```bash
ORB_MONITOR_ENABLED=true
ORB_MONITOR_PROTOCOL=hl7v2-mllp      # the only protocol today
ORB_MONITOR_MODE=listen              # listen: the monitor connects to Orb; connect: Orb dials the monitor
ORB_MONITOR_HOST=0.0.0.0
ORB_MONITOR_PORT=2575                # IANA's MLLP port
ORB_MONITOR_SOURCE_ID=ward-a-central
ORB_HL7_DEFAULT_TZ=Asia/Kolkata      # for messages whose timestamps carry no offset
ORB_MONITOR_CLOCK_SKEW_TOLERANCE_SECONDS=900
```

I want to be exact about the state of this listener. It has unit tests, it
parses recorded messages, and it has never been connected to a real monitor,
because I have not had one. The first real device is the acceptance test.
Until then, plan the pilot around the gateway path, or around manual entry,
and treat the listener as a bonus that gets proved on site.

### Configure with biomedical engineering

1. Inventory every monitor in scope: make, model, firmware, and what it can
   emit. This is the highest-value output of the whole install visit.
2. Point the gateway at `https://orb.hospital.local:8000/api/ingest/hl7` with
   the API key, or point the central station at port 2575 if the listener is
   in use.
3. Send one message, then read the ingestion status back from the admin
   panel, or with an admin session (see the note above about why the API key
   does not work here).
4. Confirm the observation appears on the patient's chart in the app.
5. **Then unplug the monitor and confirm you can tell.** Knowing what a
   stopped feed looks like before it happens is the entire point of
   [F7](#f7-monitor-feed-stopped--ingestion-gap).

Manual nurse entry is the redundancy layer behind the monitors, not the plan.
Confirm with the ward which beds are on which.

---

## 12. Backups and the schedule

The full runbook is [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md). This is
the install-day subset.

```bash
cd /opt/orb
set -a && . ./.env && set +a

# Nightly 02:30 backup + monthly restore drill (day 1, 04:00), with an off-box copy.
./scripts/install-backup-schedule.sh --offsite /Volumes/OrbBackup --with-drill --run-now
```

Then confirm all four of these, on the day:

```bash
launchctl list | grep orbhospital        # com.orbhospital.backup and com.orbhospital.restore-drill
ls -lt backups | head -3                 # a real file, hundreds of megabytes, not bytes
ls -lt /Volumes/OrbBackup | head -3      # the off-box copy exists
./scripts/restore-drill.sh               # RESULT: PASS
```

Then confirm the one thing I got wrong on my own machine: put the drill date
in a calendar with a name on it. On 3 September 2026 my development machine
had nothing scheduled and its newest backup was nineteen days old. The script
is not the control. The calendar is.

Things about the backup you should understand before you rely on it. Each
comes from a real failure.

- **The backup earns the right to rotate.** `scripts/backup.sh` snapshots
  through SQLite's online backup API on a codec-aware connection, then
  verifies the result with `scripts/lib/verify_restore.py`: a size floor,
  opening it with the key, `PRAGMA integrity_check`, the schema version, all
  88 tables the migrations create, real row counts in `patients`, `vitals`,
  `audit_logs` and `users`, the reference rows the migrations seed, and the
  audit hash-chain linkage. If any of that fails it exits 2 and **rotates
  nothing**. The July version did not verify: it wrote zero-byte files,
  passed an integrity check on them (an empty file is a valid empty database),
  and pruned the real backups to make room. Fourteen nightly runs would have
  destroyed every good copy while reporting success each time.
- **The drill asks a harder question than the backup does.** Both run the
  same verifier, but `backup.sh` runs it with the schema policy set to warn
  and the drill with it set to fail. The difference is deliberate. The backup
  job is deciding whether to keep a snapshot of the live database, and a
  snapshot of a behind-schema live database is still the best copy of it in
  existence, so refusing to keep it would invent a fresh way to have no
  backups. The drill is deciding whether a file is a good thing to restore,
  and a backup older than the shipped schema is not. If `backup.sh` prints a
  schema warning, the live database is behind the installed code; treat that
  as its own fault, not a backup fault.
- **Empty modules are reported, not judged.** The drill prints a row count
  per module group (billing, payments, claims, procurement, front desk,
  biomedical, housekeeping, ABDM, workforce, dietary, NABH, and the clinical
  rails). `EMPTY` is correct before that module goes live and a real loss
  after it, and nothing inside the file distinguishes them. The day a module
  goes live, put its group name in `ORB_BACKUP_REQUIRE_NONEMPTY` in `.env`
  so an empty one becomes an exit code instead of a line of text nobody reads
  at 04:00.
- **The key is not in the backup.** Every backup is SQLCipher-encrypted with
  `ORB_DB_ENCRYPTION_KEY`, which lives in `.env` on the machine the backups
  exist to survive. Lose it and every backup is permanently unreadable. This
  is why the key ceremony is a same-day task.
- **The uploads travel with the database.** Lens images and Scribe audio are
  archived beside each verified backup as `orb-backup-<stamp>-uploads.tar`,
  copied off-box with it, and rotated with it. The files inside are encrypted
  under `ORB_UPLOAD_ENCRYPTION_KEY`, a second key that must be escrowed too.
- **There is one backup directory.** The off-box destination is
  `ORB_BACKUP_OFFSITE_DIR` (or the `--offsite` flag), retention is
  `ORB_BACKUP_RETAIN` (14). On 15 August the shell scripts and the Python
  code disagreed about where backups lived and the drill certified a stale
  file from the wrong directory. The location is now defined once, in
  `config.BACKUP_DIR`, with a test that fails if a second directory appears.
  [DRILLS.md](DRILLS.md) section 1.5 has the story.

**Data loss expectation, say it out loud to the ward:** backups are daily, so
a restore loses everything since the last one, **up to 24 hours** of vitals,
notes and orders. [WARM_STANDBY.md](WARM_STANDBY.md) explains how to run
several a day.

### Stand up the pager: the heartbeat receiver

The appliance's outbound heartbeat reports backup age, disk headroom, loop
health and access posture every five minutes, but **a beat pages nobody until
something is listening**, and a frozen appliance's only external signal is a
beat that stops arriving. The listening end is `ops/heartbeat_receiver.py`:
one file with no dependencies beyond Python, run on any always-on box I
control **outside the hospital** (a small VPS, or the standby Mac). This is
not install-day work for the hospital. It is my homework, done before
go-live.

```bash
# On the receiver box: the same token and secret the appliance's .env will
# carry, kept in a mode-600 env file, never on the command line.
export ORB_HEARTBEAT_TOKEN=...   ORB_HEARTBEAT_SECRET=...
python3 heartbeat_receiver.py --port 8787 --state /var/lib/orb-receiver/state.json \
    --max-silence 15 --ntfy-topic <your-private-topic>

# On the appliance: uncomment ORB_HEARTBEAT_URL / _TOKEN / _SECRET in .env
# (there is a worked example in .env.example) and restart Orb.
```

The sender's thresholds are in the same block of `.env.example`:
`ORB_HEARTBEAT_INTERVAL_S` (300), `ORB_HEARTBEAT_BACKUP_MAX_AGE_H` (36) and
`ORB_HEARTBEAT_DISK_MIN_PCT` (10). Alarm channels on the receiver:
`--ntfy-topic` (the ntfy phone app is the two-minute path to a real pager),
`--smtp host:port:from:to`, or `--on-alert '<any shell command>'`. TLS flags
and a ready-to-paste systemd unit and launchd plist are in the file's own
header; the flags are in [ops/README.md](../ops/README.md).

Then **prove the pager pages**, the same discipline as the restore drill,
because an unproven pager is a decorative one:

```bash
python3 heartbeat_receiver.py --state /tmp/sim.json --simulate --ntfy-topic <topic>
```

Exactly **4 notifications** must reach the phone: CRITICAL, SILENT,
RECOVERED, CLEARED. Fewer means the path is broken. Re-run monthly, alongside
the drill, and after any change to the receiver box.

---

## 13. Acceptance test

Nothing on this list is optional, and none of it is "obviously fine". Tick
each line on the install record with a time and an initial.

**Platform**

- [ ] `curl /api/health` returns 200 from a ward device, not from the appliance
- [ ] The startup log shows both models pre-loaded with `keep_alive=-1`, and `Mode: INGESTION`
- [ ] `scripts/encrypt_db.py --check` confirms the database is encrypted
- [ ] The startup log contains **no** `falling back to PLAINTEXT sqlite3`
- [ ] FileVault is on and the recovery key is recorded
- [ ] Automatic login is on; reboot the machine and confirm Orb comes back unattended

**Access**

- [ ] Admin can sign in; there are no accounts with the password `password`
- [ ] Admin MFA is enrolled and `ORB_MFA_REQUIRED_ROLES=admin` is in force
- [ ] A test account for each role sees only what that role should see
- [ ] Every account maps to a named human

**Clinical path**

- [ ] One test observation ingested by API appears on a chart
- [ ] Vigil shows a NEWS2 score for that patient
- [ ] Sage answers a general clinical question, with citations
- [ ] A downtime snapshot captures and prints, and a ward pack opens ([DRILLS.md](DRILLS.md))
- [ ] Realtime works: two browsers open the ward board, a change in one appears in the other without a refresh

**Durability**

- [ ] `./scripts/backup.sh` produces a verified backup
- [ ] `./scripts/restore-drill.sh` returns `RESULT: PASS`, including the schema check
- [ ] The off-box copy exists on the external volume
- [ ] Key ceremony complete, both envelopes sealed and stored ([KEY_CEREMONY.md](KEY_CEREMONY.md))
- [ ] Full restore rehearsed with hospital IT on the standby machine ([DRILLS.md](DRILLS.md))

**Load**

- [ ] The soak test passes at the ward's real bed count:

```bash
cd /opt/orb/server
.venv/bin/python scripts/soak_test.py --minutes 15 --pool --census 60
```

It boots an isolated server on port 8010 against a **copy** of the database,
never the live one, and asserts: zero failed `/api/health` probes, health p99
under 250 ms, interactive reads p99 under 2 s with an error rate under 0.5 %,
and the process surviving the window. The reference run is 11 August 2026 on
my machine: 3,634 requests, zero errors, health p99 5.3 ms, chart p99 75.4 ms,
boot to healthy 2.1 s, process memory 285 to 373 MB. Earlier soak reports in
`docs/perf/` from 1 August were run under the wrong profile and are void;
the 11 August files are the ones to compare against.

- [ ] The alert-storm health probe passes. The soak does not generate an alert
      storm, so this is a separate, manual check: an isolated instance on a
      database copy with the simulator on and about fifteen critical patients,
      while `/api/health` is polled every half second through a Sage query.
      It last passed on 1 August with 380 of 380 probes answering. The API
      used to wedge under exactly this load, it no longer does, and it is the
      kind of thing that comes back quietly. Re-run it before every phase gate.

**Watchdog**

- [ ] `curl /api/health/loop` returns `"healthy": true`
- [ ] `server/ops/watchdog/status.json` exists and its `written_utc` is current
- [ ] A `deadman-pid<N>-<timestamp>.log` file exists with only its header in it

---

## 14. Handover

Leave these behind, on paper, in the ward and with hospital IT:

1. **This runbook**, printed, in the same cupboard as the appliance.
2. **The escalation card**, [Escalation](#escalation), filled in with real
   names and real numbers.
3. **The install record**, every checkbox above, dated and initialled by me
   and by hospital IT.
4. **The key ceremony record**, countersigned, per [KEY_CEREMONY.md](KEY_CEREMONY.md).
5. **The drill sign-off sheets**, per [DRILLS.md](DRILLS.md).

And say one thing to the ward manager in person, because it is the sentence
the whole pilot rests on during shadow mode: *nothing clinical depends on this
machine yet. If it stops, care continues exactly as it does today, and you
call the number on the card.*

---

# Part 2: Failure modes

## Failure index

Find the symptom. Start there.

| You are seeing | Go to |
|---|---|
| Nothing loads. The app hangs. `/api/health` never returns. | [F1](#f1-the-appliance-is-frozen) |
| No new file in `backups/` this morning | [F2](#f2-no-new-backup-this-morning) |
| The log says `[backup][fail] verification FAILED` | [F3](#f3-backup-verification-failed) |
| The restore drill says FAIL | [F4](#f4-the-restore-drill-failed) |
| Writes failing, "disk full", the machine is sluggish | [F5](#f5-disk-full) |
| Sage returns nothing. Lens does nothing. Scribe produces empty notes. | [F6](#f6-ai-features-degraded-model-not-loaded) |
| Vitals stopped arriving for some or all beds | [F7](#f7-monitor-feed-stopped--ingestion-gap) |
| Errors mentioning `database is locked` | [F8](#f8-database-is-locked) |
| The server will not start, or will not stop; port 8000 is in use | [F9](#f9-port-8000-in-use-orphaned-processes-or-the-server-will-not-stop) |
| Correct password, cannot get in | [F10](#f10-nobody-can-sign-in) |
| The boards only update when you refresh | [F11](#f11-the-screens-stopped-updating-by-themselves) |
| Sage takes 20 seconds during a busy period | [F12](#f12-sage-is-slow-during-a-busy-period) |
| The machine restarted on its own | [F13](#f13-the-appliance-rebooted) |
| Scribe says "Transcription engine unavailable on this appliance", dictation dead, everything else fine | [F14](#f14-scribe-cannot-transcribe) |

---

## The three numbers to write on the wall

Fill these in and tape them inside the cupboard door.

```
Appliance address : https://orb.hospital.local:8000
Repo path         : /opt/orb
Operator account  : orbops
```

And the three commands that answer "is it alive?", in the order to run them:

```bash
curl -fsS -m 5 https://orb.hospital.local:8000/api/health          # 1. does it answer at all
cat /opt/orb/server/ops/watchdog/status.json                       # 2. what does the watchdog say
pgrep -fl "main.py"                                                # 3. is the process even there
```

---

## F1: The appliance is frozen

**Symptom.** The app does not load. `/api/health` hangs rather than erroring.
The machine may look idle. Nothing recovers on its own.

This has happened three times on this codebase, all in July and August, all
on my machine. All three causes are found and fixed. **The diagnosis
procedure still matters**, because the next freeze will be the fourth cause,
and the evidence you collect in the first sixty seconds is what makes it
solvable.

### Step 0: Understand what a freeze is here

Orb's API is served by a single Python event loop. If that loop stops being
scheduled, everything served by it stops, including `/api/health`. A health
check cannot detect this failure, because the health check is the thing that
died. Do not conclude "the network is broken" from a hanging health probe.

### Step 1: Collect evidence BEFORE you touch anything

**Do not `kill -9`. Do not reboot. Not yet.** The evidence lives in the frozen
process, and killing it destroys the only thing that will explain the outage.

```bash
# Find the process.
PID=$(pgrep -f "main.py" | head -1); echo "$PID"

# 1. PYTHON FRAMES FIRST. Dumps every thread's Python stack to the server's
#    stderr. Does not kill anything. Run it three times, about 8 seconds apart.
kill -USR1 "$PID"; sleep 8; kill -USR1 "$PID"; sleep 8; kill -USR1 "$PID"

# 2. C FRAMES SECOND. Python stacks stop at the C boundary, and the C
#    boundary is where two of the three known freezes actually were.
sample "$PID" 3 -file /tmp/orb-sample-$(date +%Y%m%d-%H%M%S).txt
```

**Why three dumps, eight seconds apart:** if the three dumps are
byte-identical, the process is deadlocked, not merely slow. That distinction
is the whole diagnosis, and it is what made the 1 August incident conclusive.

**Why `kill -USR1` first and `sample` second:** `USR1` is cheap, instant, and
tells you which Orb code is involved. `sample` is slower and heavier but is
the only way to see C frames (`conn.close()`, `json.dump`, `take_gil`), which
is where the answer usually is.

### Step 2: Read the automatic evidence

The appliance may have already written the report for you. Orb runs a
watchdog that watches the loop from outside the loop.

```bash
cat  /opt/orb/server/ops/watchdog/status.json
ls -lt /opt/orb/server/ops/watchdog/
```

- **`stall-<timestamp>-pid<N>.txt`**, written by a daemon thread when the
  loop has not ticked for 30 seconds. It contains the beat age, a full thread
  inventory, the effective `ORB_DB_POOL` setting, and an explicit list of
  which threads are in the database layer, which is the fingerprint of the
  known deadlock. Read the top of this file before anything else.
- **`deadman-pid<N>-<timestamp>.log`**, written by a C-level timer that does
  not need the interpreter, when the loop has not re-armed it for 60 seconds.
  This is the lane that survives a totally wedged interpreter. If this file
  has grown past its header, the appliance was genuinely frozen, not slow.
- **`status.json`**, refreshed every 30 seconds by the daemon thread, never by
  the loop. A `written_utc` that is minutes stale means even the watchdog
  thread is starved, which is itself the alarm.

The watchdog **never restarts anything**. It observes and records. Recovery
is a human decision, deliberately: a watchdog that restarts a clinical system
on a false positive is more dangerous than the bug it was written for.

### Step 3: Match against the three known causes

**F1a, a network-configuration lookup on the event loop.**
*Fingerprint:* the main thread is in
`httpx.AsyncClient.__init__` → `get_environment_proxies` →
`getproxies_macosx_sysconf`.
*What happened:* constructing an httpx client is synchronous, and with the
default `trust_env=True` it resolves proxy configuration, which on macOS is an
IPC round-trip into `configd` with no timeout and no async form. When
`configd` is contended, the loop blocks indefinitely. This wedged the API
twice on 1 August, once for three and a half hours.
*Fix, in place:* every loopback client passes `trust_env=False`, and
`server/tests/test_httpx_client_config.py` fails the build if a new one does
not. If you see this fingerprint again, a new client was added without the
flag.

**F1b, SQLCipher connection churn deadlocking on the file mutex.**
*Fingerprint:* two or more threads in `__psynch_mutexwait`, one inside
`conn.close()` → `sqlite3WalClose` → `unixLock`, another inside
`openDatabase` → `unixOpen` → `findReusableFd`, and the main thread in
`take_gil`. Process at about 0.2 % CPU, unrecoverable without `kill -9`.
*What happened:* many threads opening and closing connections to a large
SQLCipher database contend on SQLite's per-inode VFS mutex.
*Fix, in place:* pooled connections remove the churn, and pooling is the
code's default; an appliance with no `ORB_DB_POOL` line is safe. **Check the
effective state, not the config line** (the one-liner is in
[F8](#f8-database-is-locked)); the stall dump prints `ORB_DB_POOL` in its
header for exactly this reason. If it reads `0`, somebody wrote it. Find out
who and why before you delete it.

**F1c, in-process embedding models starving the loop of the GIL.**
*Fingerprint:* main thread stalled in `take_gil`; hot frames in
`tokenizers.abi3.so` or `libomp`. `/api/health` times out for minutes and
`SIGTERM` is ignored, because a starved loop cannot run its signal handlers.
*What happened:* the BGE-M3 embedder and reranker ran inside the API process
and held the GIL for most of their wall time.
*Fix, in place:* both models run in a separate child process with its own
interpreter and its own GIL, started with spawn, never fork. If you see this
again, check whether `ORB_KNOWLEDGE_INPROCESS` has been set; that variable
disables the isolation and exists only for the test suite.

### Step 4: Recover

Only after steps 1 to 3.

```bash
kill "$PID"          # polite first; give it 30 seconds
kill -9 "$PID"       # only if it has not exited
# restart Orb the way this appliance starts it
curl -fsS https://orb.hospital.local:8000/api/health
```

### Step 5: Tell people

- **Ward:** during shadow mode, nothing clinical depended on the appliance, so
  there is no clinical urgency, but the outage window must be recorded,
  because the ingestion gap has to show up in the scorecard's denominators
  rather than being silently absorbed. After clinician access begins, this is
  a downtime event: see [DRILLS.md](DRILLS.md).
- **Me:** keep every artefact, the three `USR1` dumps, the `sample` output,
  the watchdog files. Copy them off the box. They are the incident.

### Who to call

Me first. This is not a hospital IT problem unless the machine itself is dead
or off the network.

---

## F2: No new backup this morning

**Symptom.** `ls -lt backups | head -3` shows nothing from last night. No
error anywhere, because nothing ran.

This is the most dangerous failure in the document precisely because it is
silent. Nobody is paged. The appliance looks perfectly healthy. It is also the
one I have personally let happen for nineteen days on my own machine, so I am
not writing this from a height.

**Diagnose, in this order:**

```bash
launchctl list | grep orbhospital                 # is the job even registered?
tail -n 50 /opt/orb/logs/orb-backup.log           # what happened last night
grep -c '\[backup\]\[fail\]' /opt/orb/logs/orb-backup.log   # 0 is what you want
```

| What you find | What it means | Fix |
|---|---|---|
| The job is not listed at all | The LaunchAgent was never installed, or was unloaded | Re-run `./scripts/install-backup-schedule.sh --offsite … --with-drill` |
| The job is listed, log is empty since a date | The operator was logged out, usually after a macOS update or a power cut | Re-enable **automatic login** (section 3), then `launchctl kickstart gui/$(id -u)/com.orbhospital.backup` |
| `Operation not permitted` in the log | macOS TCC: the repo is in a protected directory | Move the repo to `/opt/orb` (section 6). Granting Full Disk Access to `/bin/bash` also works but grants it to every bash script this machine ever runs. |
| `ORB_DB_ENCRYPTION_KEY is not set` | The job could not read `.env` | Check `.env` exists, is readable by the operator, and is mode 600 |

**Run one by hand immediately**, whatever the cause. You are currently
running with no recent backup:

```bash
cd /opt/orb && set -a && . ./.env && set +a && ./scripts/backup.sh
```

**Prevent.** The outbound heartbeat reports the newest backup's age, and the
receiver raises `backup_stale` on a phone once the age passes
`ORB_HEARTBEAT_BACKUP_MAX_AGE_H` (36 hours by default). That only pages once
**both ends are configured and the `--simulate` drill has landed four
notifications** (see [Stand up the pager](#stand-up-the-pager-the-heartbeat-receiver)).
Until then the daily operator check is the control, and it is the first line
of the daily rhythm: did last night's backup run, and is the newest file from
last night?

**Who to call.** Me. Same day, not tomorrow.

---

## F3: Backup verification failed

**Symptom.** `logs/orb-backup.log` contains `[backup][fail] verification
FAILED`, or `scripts/backup.sh` exits 2.

**What it means, and the good news first.** The snapshot was unusable, and
because it was unusable **nothing was rotated**. Your older backups are
intact, by design. This is the script doing its job.

**Diagnose:**

```bash
df -h /                                   # disk full is the most common cause
cd /opt/orb && set -a && . ./.env && set +a
./scripts/backup.sh                       # run it by hand and read the failure
./scripts/restore-drill.sh                # is the most recent GOOD backup still good?
```

| Cause | Sign | Fix |
|---|---|---|
| Disk full | `df` shows no space | [F5](#f5-disk-full) |
| Wrong or missing key | Verification cannot open the result | Confirm `ORB_DB_ENCRYPTION_KEY` in `.env` is unchanged. If someone rotated it, the old key is required for old backups. |
| Live database damaged | `PRAGMA integrity_check` fails on the source | Stop and escalate. Do not rotate, do not restart repeatedly. |
| Row-count floor not met | The database genuinely has very few rows | Only plausible on a brand-new install |
| A required module is empty | `required:<group>` FAIL | Somebody set `ORB_BACKUP_REQUIRE_NONEMPTY` for a module that is live and its table is now empty. That is a data question, not a backup question. Escalate. |

**Who to call.** Me, same day. If the live database is the thing that failed
integrity, that is a severity 1: stop writing to it and go to
[F4](#f4-the-restore-drill-failed) to find the newest good backup.

---

## F4: The restore drill failed

**Symptom.** `./scripts/restore-drill.sh` prints `RESULT: FAIL` (exit 2).

**What it means.** You do not currently have a proven recovery path. Treat it
as an incident the same day.

The drill never touches the live database. It copies the backup to scratch,
opens the copy with the key, and runs the shared verifier
(`scripts/lib/verify_restore.py`). Read which line failed, because some of
them mean the opposite of the others:

| Line | Meaning | Do this |
|---|---|---|
| `size floor ... FAIL` | The file is under 1 MiB. It is not a backup. | Drill older backups, and find out what wrote it |
| `decrypt+open ... FAIL` | The key in your environment is not the key that backup was written with, or the file is damaged | Try the escrowed key ([KEY_CEREMONY.md](KEY_CEREMONY.md) section 8). Then drill an older backup. |
| `integrity_check ... FAIL` | That file is corrupt | Drill older backups |
| `rows:patients ... FAIL` | The backup is structurally valid but effectively empty | Drill older backups; suspect the source database |
| `audit chain ... FAIL` | The audit hash-chain linkage is broken in that backup | Do **not** treat that backup as a compliance record. Drill older ones to find the last good chain, and escalate; a broken chain is indistinguishable from a tampered record. |
| `schema version ... FAIL` | That backup predates migrations this build ships, and restoring it brings whole modules back **empty** | **Do NOT drill older backups. Older is worse.** Go forward: find the newest backup instead, and if every one fails this way, the backup job has not run since before an upgrade. Fix the job, take a backup now. |
| `module tables ... FAIL` | Tables the schema should have are missing, whatever the version ledger says | Same direction as above, newer, not older. If the newest backup fails this, suspect the live database, not the backups. |
| `seeded reference ... FAIL` | Rows the migrations write themselves (document number series, shift types, payers, the self-pay tariff) are gone | Not an age problem. Something deleted them. Escalate; a restore from this would come back unable to number an invoice. |
| `admin present ... FAIL` | Only with `--require-admin`: no login-capable admin in the file | A restore nobody can sign into. Create one after restoring (section 9), and find out why there was none. |

**Walk backwards until you find a good one, but only for the first five
rows above:**

```bash
for f in $(ls -1t /opt/orb/backups/orb-backup-*.db); do
  echo "── $f"
  ./scripts/restore-drill.sh --backup "$f" && break
done
```

This loop is why the schema checks exist. It walks backwards, and before
14 August the drill had nothing to say about age: a backup from before the
admin spine shipped satisfied every check it had and printed `RESULT: PASS`,
so this loop would happily stop on a file that restores an appliance with no
billing, no payments, no claims, no stock and no incident register. The
drill now refuses such a file and says so in the failure text. On my own
machine on 3 September it refused my newest backup for exactly that reason:
the file was intact and nineteen days behind the code.

The answer you need is not "it failed" but **"how far back is the newest
good backup?"**, because that number is the real data-loss window, and the
ward has to be told it.

**Who to call.** Me immediately. The champion if the newest good backup is
more than 24 hours old, because that is a clinical-record question, not a
technical one.

---

## F5: Disk full

**Symptom.** Writes fail. Backups fail verification. The machine is sluggish.
`df -h /` shows little or no space.

**Diagnose, find what grew:**

```bash
df -h /
du -sh /opt/orb/server/orb.db /opt/orb/server/orb.db-wal \
       /opt/orb/backups /opt/orb/server/downtime_exports \
       /opt/orb/server/uploads /opt/orb/logs
```

Four things grow, and each has a different fix. Reference sizes are from my
development machine on 3 September 2026, with 31 patients and a month of
simulated monitor vitals:

| Directory | Observed | Grows with | Safe action |
|---|---|---|---|
| `server/orb.db` (+ `-wal`) | 716 MB | Bed count × days × vitals frequency, and the shadow instrument's event table, which is the largest thing in the file | Never delete. Never delete the `-wal` file while the server is running. |
| `backups/` | 619 MB **per backup**, ×14 retained | Retention setting × database size | Lower retention: `ORB_BACKUP_RETAIN=7`. Confirm off-box copies exist first. |
| `server/downtime_exports/` | 277 MB | Patients' FHIR bundles (kept 14 days) and hourly ward packs (kept 3 days) | Self-prunes. Force it early with `ORB_FHIR_EXPORT_RETENTION_DAYS=7` or `ORB_WARD_PACK_RETENTION_DAYS=1`. Safe to delete files here; they are a convenience copy, regenerated. |
| `server/uploads/` | 22 MB | Lens images, Scribe audio | **This is primary PHI.** Never delete. Move to larger storage instead. |

The export directory has run away before: 390 files against 30 admitted
patients, because discharged patients' bundles were never reaped. Retention
now handles it, but check this directory first. It is the usual culprit.

**Emergency space, in order of safety:**

```bash
# 1. Orphaned temp files from an interrupted export: always safe.
find /opt/orb/server/downtime_exports -name '*.tmp' -mmin +60 -delete

# 2. Old FHIR bundles: safe; regenerated.
find /opt/orb/server/downtime_exports -name '*.fhir.json' -mtime +7 -delete

# 3. The plaintext pre-encryption copy, if it is still there. It is
#    UNENCRYPTED PHI and should not exist on a live appliance at all.
ls -la /opt/orb/server/orb.db.plaintext-* 2>/dev/null
#    Delete only after confirming the encrypted database works.
```

**Never** free space by deleting backups before confirming the off-box copies
exist and drill clean.

**Prevent.** Add disk headroom to the weekly check. The heartbeat alarms when
free space drops under `ORB_HEARTBEAT_DISK_MIN_PCT` (10 %), once the pager is
stood up.

**Who to call.** Me. Hospital IT if the fix is more storage.

---

## F6: AI features degraded, model not loaded

**Symptom.** Sage returns empty answers. Lens does nothing. Scribe produces
empty notes. Everything else, charts, vitals, orders, messaging, works fine.

**This is a degradation, not an outage.** The clinical record is intact and
the ward can work. Say that first when you report it.

**Diagnose:**

```bash
# What does Orb think is warm? (needs a signed-in session: model topology is
# deployment metadata, not for anonymous callers)
curl -fsS https://orb.hospital.local:8000/api/health/models

# Is Ollama alive at all?
curl -fsS http://localhost:11434/api/tags

# What is actually resident right now?  With keep_alive=-1, UNTIL reads "Forever".
ollama ps
ollama list
```

| Sign | Cause | Fix |
|---|---|---|
| `"ollama_reachable": false` | Ollama is not running | Start it: `ollama serve`. Confirm it starts on boot. |
| Startup log: `Warmup got HTTP 404 for model '<name>'` | The tag in `ORB_LLM_MODEL` is not pulled | `ollama list`, then `ollama pull <tag>` or correct the variable |
| `"warm": false` for a configured model | Evicted from memory | The keep-warm loop re-loads it within five minutes. To force it, use the admin panel's model load control, or `POST /api/health/models/load` (admin permission). |
| Models keep getting evicted | `ORB_OLLAMA_KEEP_ALIVE` is a finite value | Set `ORB_OLLAMA_KEEP_ALIVE=-1`. The code default is one hour. |
| The 4B tier ballooned memory | Loaded at its default 128K context by something other than Orb | Orb warms it at a 4K context explicitly; if this recurs, check nothing else is driving Ollama directly |

**What each tier costs you when it is gone.** This is what to tell
clinicians, and it is the same list the app shows before an operator unloads
a model:

- **Primary (27B) missing:** Sage clinical answers and Ask Sage, Lens image
  analysis, Scribe note structuring, Bridge patient Q&A and discharge
  summaries, the Sage ward brief.
- **Ambient (4B) missing:** Vigil alert recommendations in Relay, Relay
  ambient drafted actions, Scribe transcript correction and verification.
  Every one of these falls back to a deterministic path when the tier is out;
  nothing waits on it.

Unloading is non-destructive. Weights stay on disk; the next request reloads
them, paying roughly a minute of cold start for the 27B.

**If only dictation is dead**, Scribe shows *"Transcription engine
unavailable on this appliance"* while Sage and Lens answer normally, that is
not this page. The speech engine is a separate runtime from the Ollama
models: see [F14](#f14-scribe-cannot-transcribe). The in-app status message
still points at F6; start there, land on F14.

**Who to call.** Me, same day, not urgently. Nothing clinical is lost.

---

## F7: Monitor feed stopped / ingestion gap

**Symptom.** Vitals stopped arriving, for one bed, for one monitor, or for
everything. Charts show a flat line of no data rather than abnormal data.

**This is the failure mode most likely to be mistaken for good news.** A
patient with no new vitals produces no alerts. Silence looks like stability.

**Diagnose, from the appliance outwards.**

Read the ingestion status from the admin panel, or with an admin session
against `GET /api/ingest/status`. It needs the `ingest.monitor` permission.
The ingestion API key will not open it, which catches people out at exactly
the wrong moment. Ask two questions:

- `?limit=20`, **is anything arriving at all?**
- `?status=failed&limit=20`, **is anything arriving and being rejected?**

Those two answers split the problem cleanly: nothing arriving is a network or
gateway problem, arriving-and-failing is a message-format problem.

| What you see | Cause | Fix |
|---|---|---|
| No recent events at all | Nothing is sending. The gateway, the network, or the monitor. | Work outwards: gateway process, network path, monitor. Biomedical engineering owns the far end. |
| Events with `parse_status = failed` | Malformed messages, or a format change | Read `parse_error`. A monitor firmware update changing message shape is a real cause. |
| HTTP 401 from the sender | Wrong or rotated `X-API-Key` | Re-issue the key to the gateway |
| HTTP 503 saying ingestion is disabled | `ORB_INGESTION_MODE` is `simulation` | Set it back to `ingestion` and restart. **Then check whether synthetic data was written while it was wrong.** |
| Some beds fine, one bed dead | That monitor or its cable | Biomedical engineering |
| The MLLP listener stopped accepting | Orb's own listener (section 11) died with its source | Restart Orb; the listener starts with it. Check `ORB_MONITOR_ENABLED` is still `true`. |

**Two things to do regardless of cause:**

1. **Tell the ward the affected beds are on manual observation** until the
   feed is back. Manual nurse entry is the redundancy layer and this is what
   it is for.
2. **Record the gap**: start time, end time, which beds. An ingestion gap
   must show up in the shadow scorecard's denominators, not be silently
   absorbed. A gap that is not recorded turns into a false claim about
   coverage later.

**When the feed comes back**, a gateway that buffered can replay what it held
through `POST /api/ingest/replay` with the original observation times. Orb
stores those readings, audits each one as a backfill, and keeps them out of
live scoring and alerting, so a replay cannot fire an hour of stale alerts at
the ward.

**Who to call.** Biomedical engineering for the monitor and the gateway. Me
for anything on the appliance. The ward nurse in charge immediately, because
their observation routine changes.

---

## F8: Database is locked

**Symptom.** Errors mentioning `database is locked`, or requests that hang
for about thirty seconds and then fail.

**Why thirty seconds:** SQLite's busy timeout is set to 30 s. A contended
writer can legitimately sit for that long. So a single slow request is not
necessarily a fault. A repeating pattern is.

**Diagnose:**

```bash
grep -i "database is locked" /opt/orb/logs/*.log | tail -20
pgrep -fl "main.py"                        # exactly ONE server process
ls -la /opt/orb/server/orb.db-wal          # a huge WAL means a long-running writer

# Is pooling actually ON? Ask for the EFFECTIVE state, not for a line of config.
# `grep ORB_DB_POOL .env` was the check here once and it was wrong in both
# directions: pooling is ON by default in code, so an absent line means ON,
# and .env.example carries no such line, so on a fresh install the grep finds
# nothing and looks like a fault when everything is correct.
cd /opt/orb && set -a && . ./.env && set +a && \
  server/.venv/bin/python -c \
  "import sys; sys.path.insert(0,'server'); import db_backend; \
   print('pool =', 'on' if db_backend.POOL_ENABLED else 'OFF, see F1b')"
```

That evaluates the same expression the server evaluates, against the same
`.env`. To confirm the state of the process that is actually running, read
it out of the boot log; the server logs `Database backend: sqlite (pool=on)`
once at startup:

```bash
grep -m1 "Database backend:" /opt/orb/logs/*.log     # expect: sqlite (pool=on)
```

| Cause | Fix |
|---|---|
| Two Orb processes against one database | [F9](#f9-port-8000-in-use-orphaned-processes-or-the-server-will-not-stop). This is the most common cause and the most damaging. |
| `pool = OFF` above | Somebody set `ORB_DB_POOL=0`. Remove that line (absent means on) and restart. Do not "fix" it by setting `=1` without understanding who turned it off, because the unpooled path is what froze the appliance on 1 August. |
| A manual `sqlite3` session left open | Close it. And note that the stock `sqlite3` CLI cannot open the encrypted database at all; if someone got a prompt, they are pointed at the wrong file. |
| A script of your own opened the database while the server runs | It will block up to 30 s on the write lock and look exactly like a wedge. Check `/api/health` first; if health is fast, it is your script queuing behind the server. |
| A backup or migration running | Wait. `scripts/backup.sh` uses the online backup API and is safe on a hot database; it does not need exclusive access. |

**If locking is persistent and accompanied by a hang rather than an error,
stop and go to [F1](#f1-the-appliance-is-frozen).** Two threads wedged in the
SQLite file mutex is what F1b looks like from the outside, and the evidence
you need expires when you restart.

---

## F9: Port 8000 in use, orphaned processes, or the server will not stop

**Symptom.** The server will not start; the error mentions the address
already being in use. Or the app behaves strangely, showing stale data, and
two processes are running. Or you asked the server to stop and it is still
there a minute later.

**This one is worth taking seriously even though it looks trivial**, because
two Orb processes writing one database is a corruption risk, not an
inconvenience.

**Diagnose:**

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN     # who holds the port
pgrep -fl "main.py"                  # Orb server processes, expect exactly one
pgrep -fl "orb-fhir-export\|knowledge"   # child workers
ps -ef | grep -i ollama | grep -v grep
```

Orb legitimately runs more than one process. Know which is which before you
kill anything:

- **The API server** (`main.py`), one, and only one.
- **The knowledge worker**, a child process hosting the embedding and
  reranking models (roughly 2 GB each). It is deliberately a separate process
  so its model work cannot starve the API. It is reaped on clean shutdown,
  and it exits on its own if its parent dies.
- **Ollama**, a separate service holding the model weights. Killing it does
  not lose data; the next request reloads.

**Fix:**

```bash
PID=$(lsof -nP -iTCP:8000 -sTCP:LISTEN -t | head -1)
ps -p "$PID" -o pid,ppid,etime,command    # confirm it is Orb before you touch it
kill "$PID"; sleep 15
kill -9 "$PID" 2>/dev/null || true        # only if it survived
pgrep -fl "main.py"                       # should now be empty
```

An orphaned knowledge worker holding a couple of gigabytes after an unclean
shutdown is a known class, which is why shutdown explicitly reaps it. If you
see one with no live parent, kill it.

### The server will not stop

Until 14 August this was a real defect, and it was the worst version of this
page: six background tasks swallowed their cancellation, so the server sat at
"Waiting for application shutdown" forever, holding the port, while the old
process kept writing to the encrypted database and the new one could not
start. That is the two-writers hazard wearing a different hat.

Shutdown is now bounded. The server cancels its background tasks, waits at
most ten seconds for them, and then abandons any survivor with an error line
that names the task and says it did not stop within 10 s. So:

- If the server stops within about fifteen seconds, that is normal.
- If the log shows the "did not stop within 10s" line, the server still
  exits; copy the log line into the incident notes, because it names a task
  that needs fixing.
- If the server is still there after thirty seconds with no such line, it is
  not a shutdown problem. Go to [F1](#f1-the-appliance-is-frozen) and collect
  the dumps before you kill it. An unkillable process is evidence, not just
  an obstacle.

---

## F10: Nobody can sign in

**Symptom.** Correct credentials, no session.

**Diagnose by what the screen says:**

| Message / behaviour | Cause | Fix |
|---|---|---|
| Locked out after repeated attempts | Five failed attempts from one address locks that address for 15 minutes. Separately, eight failed attempts on one account lock the account for 15 minutes, and that one is stored in the database. | Wait it out. Both clear themselves. |
| `mfa_required` | The account has MFA enrolled and no code was entered | Enter the current code from the authenticator app |
| `Invalid MFA code`, repeatedly, with a correct-looking code | Clock skew, or the code was already used | Verification allows one 30-second step of skew and refuses a code already consumed. Check the phone's clock is set automatically. |
| `mfa_enrollment_required` | The role is in `ORB_MFA_REQUIRED_ROLES` and the account has not enrolled | Complete enrolment. The login response carries a ten-minute ticket that unlocks only the enrolment endpoints, so this is recoverable without touching the server. |
| `Password reset required by an administrator.` (HTTP 403) | The account's password is stored with a legacy hash from before the current password scheme. The password was correct, and the server refuses it anyway, by default. | An admin resets the password. The escape hatch `ORB_ALLOW_LEGACY_SHA256=1` exists and should not be used on an appliance. |
| `Account deactivated` | The account was deactivated by an admin | An admin reactivates it |
| Nothing happens; the page hangs | Not an auth problem | [F1](#f1-the-appliance-is-frozen) |

**Total admin lockout**, every admin locked out, or the only admin lost their
authenticator. This needs filesystem access to the appliance:

```bash
cd /opt/orb && set -a && . ./.env && set +a
server/.venv/bin/python server/scripts/create_admin.py
```

The script does nothing if a real admin already exists, so it cannot be used
to paper over a lockout by adding a second admin. It is for the case where
there is genuinely no usable admin account. Recovering a specific locked
account is a database operation and needs me on site.

**Who to call.** Me. Note that this is a design consequence, not a defect:
there is no remote access and no vendor override. Nobody outside the building
can reset an account.

---

## F11: The screens stopped updating by themselves

**Symptom.** The ward board is correct after a manual refresh but does not
update on its own. New vitals do not appear live. Within a few seconds of
login, screens also start to hang on "Loading..." for 15 to 25 seconds at a
time, then fill in all at once.

**Cause, nine times out of ten:** the realtime Socket.IO connection was
rejected because the browser's origin is not in `ORB_CORS_ORIGINS`. The very
first requests work, so it looks nearly fine at first, which is what makes
this confusing. It then gets worse: the browser keeps retrying the socket, and
each retry leaves a long-poll request open for about 25 seconds. A browser
allows only six open requests to one address, so after a handful of retries
every ordinary request (a patient list, the appointments board) waits in line
behind them. Measured on 3 October 2026: the appointments list took 17.5
seconds to load with the origin missing and under a tenth of a second with it
listed.

**Diagnose.** Open the browser's developer console on an affected device and
look for a 400 or 403 on `/socket.io/` requests (the first one succeeds, the
POSTs that follow are rejected) and a failed WebSocket handshake. Then:

```bash
grep ORB_CORS_ORIGINS /opt/orb/.env
```

**Fix.** The value must contain the **exact** origin staff type: scheme,
host and port, no trailing slash:

```bash
ORB_CORS_ORIGINS=https://orb.hospital.local:8000
```

Restart Orb after changing it. If staff reach the appliance by more than one
name (IP and DNS name), list both, comma-separated.

**Other causes:** the device dropped off Wi-Fi; a proxy between device and
appliance is not passing WebSocket upgrades. Check a wired station desktop.
If that updates live and the tablets do not, it is the network, not Orb.

---

## F12: Sage is slow during a busy period

**Symptom.** Sage usually answers in a few seconds. During busy periods, with
several deteriorating patients at once, it takes twenty seconds or more.

**What was measured, and when.** On 1 August, under an alert storm, a Sage
query returned in 20.7 s against 8 to 12 s when quiet. It was not queue
waiting: the reserved interactive lane worked and no background job ran
alongside the query. Generation itself ran about twice as slowly.

My first theory was wrong, and it is worth knowing so nobody re-tests it. It
was not cache eviction. An A/B test with a byte-identical prompt hit the
cache 30 times out of 30, including repeats that overlapped storm GPU work by
94 %. Instrumenting the real calls showed why the theory could not have been
right: every storm inference went to the small ambient model, not the 27B,
and the two are separate processes with separate caches. The ambient model
was running at a 76 % GPU duty cycle. That contention degrades decode, not
prefill.

**What changed since.** On 10 August I bounded background work. The
background governor is on by default in production (`ORB_BG_LLM_GOVERNOR`),
admits at most 12 background model calls per rolling minute across every
background producer (`ORB_BG_LLM_MAX_PER_MIN`), batches ambient jobs into
150 ms windows of up to eight so they take one turn on the queue instead of
eight, and yields to a clinician: a background job waits up to 3 s for a
clinician's turn to finish and then takes its deterministic fallback
(`ORB_BG_LLM_YIELD_WAIT_SECONDS`), and nothing background runs for 2 s after
the last interactive turn releases (`ORB_BG_LLM_QUIET_AFTER_INTERACTIVE_SECONDS`).

**What has not changed.** I have not re-measured the 20.7 s number under the
governor. The 14 August wedge bench ran a three-patient storm, but it timed
the ambient assessment pipeline, not an interactive Sage query. So the honest
statement today is: the mechanism that caused the slowdown is bounded, and
the effect on an interactive question during a storm is unmeasured.

**What to do now:** nothing on the appliance. Do not turn the governor off.

**What to tell clinicians:** ask Sage, and expect it to be slower during a
busy period. The answer is not wrong, it is late. The clinical record, the
vitals and the alerts are unaffected.

**What to record:** the time, the question, and the observed delay. That is
the measurement I do not have, and a week of it from a real ward is worth
more than another benchmark on my desk.

---

## F13: The appliance rebooted

**Symptom.** Uptime is short. Nobody restarted it deliberately. Usually a
power cut, sometimes a macOS update.

**Check these six things in order:**

```bash
# 1. Is Orb back?
curl -fsS https://orb.hospital.local:8000/api/health

# 2. Is the operator logged in? (nothing works if not)
who

# 3. Are the models warm again?
ollama ps

# 4. Did the backup job survive?
launchctl list | grep orbhospital

# 5. Is automatic login still on?  macOS updates turn this off
#    System Settings ▸ Users & Groups ▸ Automatic login

# 6. Was any data lost?
ls -lt /opt/orb/backups | head -3
```

**What was lost.** Everything written since the last backup, if the database
itself did not survive: up to 24 hours. Usually nothing is lost, because the
database survives a reboot fine; the loss window matters only if you then
have to restore. Check whether ingestion has a gap across the outage, and
record it.

**The step people miss:** re-verify automatic login. If a macOS update turned
it off, the appliance is now running (because someone logged in to check) but
the backup job will silently stop the next time nobody is at the keyboard.
That is [F2](#f2-no-new-backup-this-morning), a week later, and it will look
unrelated.

**Prevent.** Put the appliance on the ward UPS. Set software updates to
download but not install.

---

## F14: Scribe cannot transcribe

**Symptom.** The Scribe screen shows an amber banner: *"Transcription engine
unavailable on this appliance."* The dictation and live-streaming mic buttons
are disabled, their tooltip says to see the operator runbook, and if a
browser-side transcript exists Scribe says it is using the browser
transcript rather than pretending it transcribed anything. **Everything else
works**: charts, typed notes, note structuring, Sage, Lens.

**This is not [F6](#f6-ai-features-degraded-model-not-loaded).** F6 is the
Ollama language models. This is the speech engine, a separate runtime
(`lightning_whisper_mlx`, pinned in `server/requirements.txt`) that either
runs inside the server's venv or as a separate warm Whisper daemon. Ollama
can be perfectly healthy while this is down, and vice versa. The UI discovers
the state at load: it probes `GET /api/scribe/asr/status` when Scribe opens,
so nobody dictates two minutes of a ward round into a dead engine first.

**Diagnose:**

```bash
# What does Orb itself say? (needs a signed-in session with Scribe access;
# the `reason` field is the diagnosis in words)
curl -fsS https://orb.hospital.local:8000/api/scribe/asr/status

# 1. Is the ASR runtime importable in the server venv?
/opt/orb/server/.venv/bin/python -c "import lightning_whisper_mlx" && echo OK

# 2. Is the warm Whisper daemon answering its socket?
curl -fsS https://orb.hospital.local:8000/api/scribe/daemon/status
```

| Sign | Cause | Fix |
|---|---|---|
| Import fails in step 1 | The runtime never made it into this venv | `/opt/orb/server/.venv/bin/pip install -r /opt/orb/server/requirements.txt`, then restart Orb |
| Step 1 OK but the status still says unavailable | Worker script missing or daemon expected but dead | Check `/api/scribe/daemon/status`; restart the daemon (below) |
| Daemon `not_running` and you rely on it | The warm daemon process died or was never started | `/opt/orb/server/.venv/bin/python /opt/orb/server/services/whisper_daemon.py` in its own session; the socket is `ORB_WHISPER_SOCKET` (default `ipc:///tmp/orb-whisper.sock`), the toggle is `ORB_WHISPER_DAEMON_ENABLED` (default `true`) |

Then reopen the Scribe tab. The availability probe runs on mount, so the
banner clears itself once either path answers. No database or model surgery
is involved.

One thing I have to say plainly: the speech engine is not installed on my
development machine, so this page is written from the code and not from a
failure I have watched. It is the one page in Part 2 that is.

**What to tell the ward.** Dictation is down; typing is not. Notes can be
typed and structured normally, and nothing already saved is affected.

**Who to call.** Me, same day. Clinically this is an inconvenience, not an
incident, but log it, because a box that lost its ASR runtime after an update
will lose it again on the next one.

---

## Escalation

Fill this in with real names and numbers before you leave site. Print it and
tape it inside the cupboard door.

| Role | Name | Contact | Call them for |
|---|---|---|---|
| Ward nurse in charge | ________ | ________ | Anything affecting patient care right now |
| Hospital IT duty | ________ | ________ | Network, power, physical access, the machine itself |
| Biomedical engineering | ________ | ________ | Monitors, cables, the vitals gateway |
| Orb operator (me) | ________ | ________ | Everything about the software |
| Physician champion | ________ | ________ | Any clinical decision, any decision to stop |

**Two things that are true of this pilot and should be said plainly:**

1. **There is no inbound remote access.** No one can dial in and fix this.
   Every software fix requires me to be physically present. Response time is
   travel time. Agree what that number is with the hospital and write it
   here: `Response expectation: ____________`.

2. **Nothing pages anybody until the pager is stood up and drilled.** The
   appliance writes evidence to disk (`server/ops/watchdog/status.json`,
   `logs/orb-backup.log`, the `/metrics` endpoint) and the outbound heartbeat
   carries it off the box, but the sender **ships disabled** and the
   receiving end (`ops/heartbeat_receiver.py`) pages only once it is running
   somewhere, both ends share the token and secret, and the `--simulate`
   drill has landed all 4 notifications on a phone, see
   [Stand up the pager](#stand-up-the-pager-the-heartbeat-receiver). Until
   that drill passes, **the daily operator check is the monitoring system.**
   Do not describe it as anything more than that to the CIO.

---

## What to write down after every incident

Seven lines. The same seven every time. They are what turn three separate
outages into one diagnosable pattern.

```
Date / time started      :
Date / time resolved     :
Symptom, as first seen   :
Evidence collected       :   (which files, where they were copied to)
Cause, if known          :
What the ward was told   :
Data gap, if any         :   (start, end, which beds)
```

Copy the evidence off the appliance the same day. A stall dump on a machine
that later gets rebuilt is a lesson nobody gets to learn.

---

## Related documents

| Document | What it covers |
|---|---|
| [KEY_CEREMONY.md](KEY_CEREMONY.md) | Generating, printing, sealing, storing and testing the encryption keys |
| [DRILLS.md](DRILLS.md) | The restore drill and the downtime drill, with sign-off sheets |
| [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md) | Full backup, verification and restore procedure |
| [WARM_STANDBY.md](WARM_STANDBY.md) | The second Mac and the nightly restore onto it |
| [HARDWARE_SPEC.md](HARDWARE_SPEC.md) | Sizing, measured footprints, and the pilot bill of materials |
| [SHADOW_MODE.md](SHADOW_MODE.md) | How the pilot runs, and what must be true before it starts |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Configuration reference and the containerised deployment |
| [PILOT_ROADMAP.md](PILOT_ROADMAP.md) | The 13-week sequence, counted from signature |
| `docs/perf/` | Soak and latency measurements referenced by the acceptance test |
