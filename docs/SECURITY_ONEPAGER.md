# Orb: Security One-Pager

*For the hospital CIO, before Orb goes on your network. I wrote this from the
source code, not from a roadmap. Where a control is partial, it says partial.
First written 1 August 2026; every fact re-checked on 3 September 2026. The
line-by-line version is `docs/CIO_SECURITY_QUESTIONNAIRE.md`.*

**The box.** One Mac Studio that you own, on your LAN. It runs everything: the
application, the clinical database, the language models (MedGemma 27B and 4B
through a local Ollama process), speech-to-text (Whisper) and the search
indexes. Nothing runs anywhere else. On the pilot wards Orb replaces the existing
HIS as the system of record, which is why the backup and key answers below
matter more than they would for a read-only add-on.

**The data.** Patients, admissions, vitals, orders, medications, notes,
documents, messages, users and the audit log, in one SQLite file. Imaging and
consultation audio sit in a separate directory.

**Where it goes: nowhere.** There is exactly one inference provider in the code
and it points at the local Ollama process. No cloud AI SDK is installed. Every
inference is written to the audit chain with the model that served it. On
4 October 2026 I ruled that Orb has no internet connection of any kind, and
removed the outbound heartbeat and browser cloud dictation under that rule.
One exception remains in this build, carrying no patient data:

| Outbound | What leaves | Default |
|---|---|---|
| **Orb Pulse** (weather, air quality, openFDA recalls, CDC flu) | a latitude and longitude and public query filters; every call is logged | on; you can block it at the firewall with no loss of clinical function |

Dictation and spoken answers are on-device only. Browser cloud speech is not
supported and cannot be switched on; the speech engine and its model are
installed with the appliance and never download anything.

**Encryption at rest.** The database and its WAL sidecars are opened through
SQLCipher (AES-256); backups inherit it. Media is AES-256-GCM under a second
key. Files are forced to `0600`. Two partials. If the key is set but the SQLCipher
driver is missing, the app logs a warning and runs on plaintext instead of
refusing to start, so the startup log has to be checked, not assumed; I check it
at install and you can too. And the keys live in `.env` on the same disk, so
this protects copies (backups, exports, a pulled SSD), not a running or unlocked
stolen machine. FileVault is required alongside it. On 3 September I opened the
development database with its key and recomputed the audit chain: 91,743 rows,
none broken.

**Encryption in transit.** TLS is opt-in. The installer generates a self-signed
certificate that must be replaced before go-live. Session cookies are marked
`Secure` automatically over HTTPS. Gaps: no HSTS, CSP or X-Frame-Options
headers, and a missing certificate produces a warning rather than a refusal to
start. Orb must not be reachable over plain HTTP; your network placement decides
that.

**Who can access what.** Five roles (doctor, nurse, pharmacist, admin, patient)
mapped to named permissions in one file and enforced server-side on every route.
A hidden UI tab is never the control. The role is re-read from the database on
every request, so a stale token cannot widen access. Denials are audit-logged
with actor, route and the missing permission. An admin cannot write the patient
record, chart vitals, prescribe, place orders, sign a note, publish a discharge
or execute a Relay action card; the clinical chart is read-only to them. Two
exceptions you should know about, because they are writes: when I last checked
(3 September) the admin role still held the pharmacy permission, which includes
verifying an AI-drafted medication into an active order, and the surgical
permission. If you want those separated from governance, tell me and I will
split the role; the five money desks (billing, payments, front desk,
procurement, cashier) were split into their own roles on 18 August the same
way. A patient sees only their own record.

**How access is authenticated.** bcrypt passwords; a 24-hour session delivered
as an HttpOnly cookie (unreadable to JavaScript) plus a double-submit CSRF token
on every state-changing request. Bumping a user's token version revokes every
outstanding session at once; that happens on logout, password change, MFA reset
and deactivation. Lockouts: 5 failures per IP and 8 per account, 15 minutes, the
account counter persistent. TOTP MFA is implemented, single-use per time-step,
with an admin break-glass reset. MFA can be mandated per role, and a mandated
user cannot get a session without enrolling. The setting ships empty in the
example configuration; on the pilot appliance I set it to `admin`, so an
administrator cannot sign in without enrolling and clinicians keep fast sign-in.
Whether nurses and doctors are mandated too is your call, and it is a
configuration change, not a build.

**What is logged, and why you can trust it.** Middleware audits every
state-changing request on top of richer per-route events. The audit table is
append-only at the database layer: `UPDATE` and `DELETE` abort. Each row hashes
the previous row plus its own fields, so altering history breaks a chain that a
verifier recomputes on demand. For security, clinical-access, PHI-access and
authentication events the audit write is fail-closed: if it cannot persist, the
action does not proceed. Three partials as of 1 August, all three since fixed.
(1) Login and MFA events were not in the fail-closed set; they were logged
fail-soft, so a failed audit write would not have blocked a login. Fixed on
4 September 2026: a login whose audit row cannot be written now fails and no
session is issued. (2) The fail-closed set named two categories no code ever
emitted, one a leftover from a hosted-AI lane that no longer exists; the
per-inference AI records that are written went to a fail-soft category. Fixed
the same day: the dead names are gone and the AI-inference category is on the
list. One limit I would rather state than imply away: the AI lane catches the
audit error and still answers, so an AI turn is not refused on an audit outage;
the failed write is logged at CRITICAL. (3) Browser-originated rows used to
carry no acting user, because the middleware read the actor from a bearer
header and the browser signs in with a cookie. That one was fixed on 14 August
2026; browser rows now carry the actor. A test now checks that every name in
the fail-closed set is emitted by real code, so the first defect cannot
quietly return. This is tamper-evidence, not notarisation:
someone with the raw file and the algorithm could recompute a consistent chain
offline. No external anchoring, no WORM export and no retention policy yet.

**Theft or loss.** The database is ciphertext without the key, but the key is on
the same disk, so FileVault is what makes that true for a powered-down machine.
The mitigations are physical and yours: a locked room, FileVault, a short
auto-lock. Revocation is per user, not per device, and there is no remote wipe.
Report a theft immediately; my response is key rotation and a rebuild from
backup.

**Failure.** Nightly 02:30 backups of database and media, verified before
anything rotates (opened with the key, integrity-checked, row-count floors; a
failed verification rotates nothing), copied off-box when a volume is mounted,
14 retained. Until 14 August the verifier checked 4 of the 66 tables it knew about; it now
checks all 88 in the current schema. On 15 August a restore drill caught the drill itself certifying a
stale backup from a second backup folder; that fault class is now a permanent
test. A monthly drill proves a backup is readable and its audit chain intact.
Worst-case loss is 24 hours, and the ward has to be told what window was lost.
Downtime mode keeps a printable read-only snapshot per patient (allergies,
problems, active medicines, latest vitals and NEWS2, recent notes) that works
with no models and no network. Gaps, stated plainly: on the development
appliance nothing is scheduled yet, the schedule installer ships and scheduling
is an install-day step; the full swap-and-boot restore has not been rehearsed on
your hardware; nothing pages anyone off site, because the appliance has no
internet connection, so monitoring is a daily on-site check of the ops status
page, the Trust Center and the backup log; the warm-standby restore job and
switchover runbook are built but the second machine has not been bought; and the key
escrow ceremony (two sealed envelopes, both keys) has not happened. Until it
does, the whole record depends on one file on one machine.

**Stability, stated plainly.** The appliance froze three times in the week of 28
July. All three were root-caused and fixed: a macOS proxy lookup running on the
event loop, the interpreter lock held across an encrypted-database close, and a
lock starvation from in-process embedding. On 14 August a dry run found six
places where a cancelled task could hang shutdown; all six are fixed and shutdown
is now bounded at 10 seconds. The valid load test is from 11 August: 15 minutes
at a 60-bed census, 3,634 requests, zero errors, health p99 5 ms, no request
over 200 ms, run twice. I voided every earlier soak report myself because the
harness had been measuring the wrong process under the wrong profile. I tell you
this because this class of fault comes back quietly, and a vendor who hides it
is the worse bet.

**Split of responsibility.**

| Hospital | Samuel (Orb) |
|---|---|
| Physical security, FileVault, screen lock | The software, its updates, this documentation |
| Network placement, firewall, TLS certificate | Backup, verify and drill tooling, and the restore procedure |
| Physician champion and the authorised user list | Running the key ceremony with you; on-site operation |
| Custody of the sealed key envelopes | Root-causing and disclosing defects |
| Accepting (or rejecting) a 24-hour recovery point | Engaging counsel on DPDP and device classification |
| Telling the ward what was lost after any restore | No inbound remote access is used or requested |

**Not claimed.** No HIPAA, DPDP or ISO certification. No external penetration
test. No third-party assessment. No dependency vulnerability scanning in CI. No
connection to the ABDM gateway yet; the ABHA screen records work done elsewhere.
Orb is decision support, not a diagnostic device; the imaging module ships
disabled in production, and the learned prediction models ship switched off
until they are trained and validated on real ward data. The pilot starts in
shadow mode: Orb observes and records, and no clinician acts on what it
produces.

**Six capability limits worth stating before you find them.**

- **Drug-drug interaction cover is a starter set, not a database.** 55 curated
  high-consequence pairs across 12 drug classes. A maintained source (First
  Databank, Micromedex, DDInter) must be bulk-loaded before go-live; the loader
  ships and the engine reads from the table, so this is a data task, not a
  build. A drug Orb cannot classify is flagged Not screened; a drug it can
  classify but has no pair for returns "No alerts" with no flag.
- **Data-subject erasure does not currently work.** The DPDP patient-erasure
  workflow is built and reachable in the Admin console, but it aborts against
  the append-only guard on shadow-mode records for any real patient. Treat
  erasure as unavailable until I fix it, and say so if asked.
- **The RxNorm drug vocabulary is not shipped.** It is licence-gated (UMLS). The
  loader is included; the data must be obtained by the hospital. Until it is
  loaded, dictation's drug-name accuracy rests on speech-recogniser biasing and
  a model correction pass, not on a dictionary check.
- **The on-device speech engine is an install step.** The dictation pipeline is
  built and gated; the Whisper runtime is not installed on my development
  machine today, and it goes on your appliance at install, before the
  appliance joins your network. It never downloads anything afterwards, and
  without it voice is off: there is no browser fallback.
- **Bedside-monitor feeds are bench-tested only.** The HL7v2 adapter exists and
  is off by default; it has not yet run against a real monitor, which needs your
  biomedical team's access.
- **The Hindi interface covers navigation only.** 65 strings: sidebar, header,
  login. All clinical content is English.

*The appliance also serves a live posture report (Admin, Trust Center) computed
from real system state rather than from this document. It is built to show
amber. One caveat: of its nine controls, the TLS-in-transit row was hardcoded to
amber when I last checked and does not read configuration, so it reports "Orb
does not terminate TLS" even on a deployment where Orb is serving HTTPS itself.
Read that row against your actual deployment, not as a probe result.*
