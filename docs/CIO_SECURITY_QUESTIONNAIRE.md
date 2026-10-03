# Orb: Pre-Answered Security Questionnaire

*For the pilot hospital's IT and information-security function. Version 2:
first written 1 August 2026, every answer re-checked against the source code on
3 September 2026. Answers describe the build as it exists today, not a roadmap.*

I am answering the questions a hospital IT and security function normally asks
before I am asked them, so that the long-lead items surface before we sign
rather than after. Send me your own questionnaire when it is ready and I will
answer it line by line; this document exists so nothing here is a surprise.

Please read the "Not yet" and "Legal" answers first. A questionnaire that is
honestly 80% answered is worth more to both of us than one that is 100%
aspirational, because you will find the difference, and I would rather you found
it here.

### How to read the status column

| Status | Meaning |
|---|---|
| **Implemented** | In the build today. Verifiable in the code and, for most items, in the live Trust Center report. |
| **Partial** | The control exists but is incomplete or depends on configuration. The gap is named in the answer. |
| **Not yet** | Planned and scheduled, not built. The pilot phase it lands in is given. |
| **Hospital** | Not a software control. The hospital owns it; I will help specify and rehearse it. |
| **Legal** | Requires counsel's sign-off, not an engineer's opinion. I have engaged counsel; these are flagged rather than guessed. |

---

## 1. System overview and architecture

**1.1 Describe the system and where it runs.**
*Implemented.* Orb is a single-appliance hospital operating system. One Mac
Studio, purchased and owned by the hospital, sits on the hospital LAN and runs
every component: the web application (FastAPI/uvicorn, single process), the
clinical database (SQLite), the language models (MedGemma 27B and 4B via a local
Ollama process), speech-to-text (Whisper, on-device), the embedding model
(BGE-M3), and the vector and full-text search indexes. There is no second tier,
no cloud service and no vendor-hosted component.

**1.2 Is this SaaS, on-premise, or hybrid?**
*Implemented.* On-premise, single-tenant, hardware owned by the hospital. Orb is
licensed software running on your machine, not a service I operate for you.

**1.3 What does it integrate with?**
*Partial.* For the pilot: a roster import from the existing HIS (CSV is
acceptable), an ingestion API for structured data (JSON and HL7 v2 accepted
today; a FHIR adapter is present), and bedside-monitor integration for selected
high-acuity beds. The monitor adapter (HL7 v2 over MLLP, with per-vendor
profiles) is built and ships switched off; it has been tested against a bench
simulator and not yet against a real monitor, which needs your biomedical
team's access to the devices. Manual nurse entry is the redundancy layer behind
it.

**1.4 Does Orb replace or sit alongside our existing system?**
*Implemented.* For the pilot wards, Orb replaces the incumbent HIS and is the
system of record. That is why the backup, restore and key-escrow answers below
matter more than they would for a read-only add-on: if `orb.db` is gone and the
backups cannot be read, there is no upstream system holding a second copy.

**1.5 Multi-tenancy: can another customer's data reach ours?**
*Implemented.* There is no multi-tenancy. Your appliance holds your data and
nothing else. There is no shared control plane, no telemetry service and no
vendor account with access.

---

## 2. Access control and identity

**2.1 What authentication is used?**
*Implemented.* Employee ID plus password. Passwords are hashed with salted
bcrypt. On success the server issues a 24-hour JWT delivered as an HttpOnly
`orb_session` cookie, so a cross-site script cannot read it, plus a JS-readable
double-submit CSRF token that must be echoed on every state-changing request.
Machine clients may use `Authorization: Bearer` instead and are CSRF-exempt
because the header cannot be forged cross-site.

**2.2 Do you support MFA?**
*Implemented for administrators, optional for clinicians.* TOTP (RFC 6238)
multi-factor authentication is built: per-user secrets, two-phase enrolment so
nobody can lock themselves out with a mis-scanned QR code, single-use
enforcement per time-step so a code cannot be replayed, a one-step skew window,
and an admin break-glass reset for a lost authenticator.

MFA can be made mandatory per role (`ORB_MFA_REQUIRED_ROLES`). When a role is
mandated, a correct password on an unenrolled account does not produce a
session; it produces a 10-minute enrolment ticket, signed with a different key,
that unlocks the two enrolment endpoints and nothing else. The setting is empty
in the example configuration. On the pilot appliance configuration I have set it
to `admin`, so an administrator cannot sign in without enrolling. Adoption per
role is reported on the Trust Center. If you want it mandatory for clinicians
too, say so and I will set it; the trade-off is speed at the bedside, which is
why it is not the default for them.

**2.3 Do you support SSO / SAML / OIDC / LDAP / Active Directory?**
*Not yet.* Orb has its own local user store. There is no directory integration.
For a two-ward pilot with a named user list that is workable; for a
hospital-wide rollout it is a gap I would need to close, and I would rather say
so now than discover it at scale.

**2.4 Describe the authorisation model.**
*Implemented.* Roles are mapped to named permissions in a single file
(`server/permissions.py`; the role registry itself is `server/roles.py`), and
every protected route declares the permissions it requires. The check runs server-side; the UI hiding a tab is
a convenience, never the control. The role is re-read from the database row on
every request, so a token minted before a role change cannot widen access.
Denied requests are written to the audit log with the actor, the route and the
specific missing permission.

The clinical roles are doctor (with `clinician` as an alias), nurse, pharmacist,
admin and patient. On 18 August 2026 I split the money desks out of the admin
role into their own roles (billing, payments, front desk, procurement, cashier)
so a cashier can be staffed without governance rights; that split is covered by
parity tests.

Least-privilege properties worth knowing: an admin cannot write the patient
record, chart vitals, prescribe, place lab or imaging orders, sign a note,
publish a discharge or execute a Relay action card; the clinical chart is
read-only to the admin role. A pharmacist sees the pharmacy workflow and Sage,
nothing else. A patient account can reach only its own record through the
Bridge portal.

Two admin write permissions I am naming rather than letting you find. As of 3
September the admin role still holds `helix.write` (the pharmacy workflow:
verify, reject, dispense, adjust stock) and `surgery.write` (manage surgical
cases, the WHO checklist, start and complete). The first is the material one:
verifying an AI-drafted medication converts it into an active order a nurse can
then administer, so an admin account is a second human who can clear that gate,
not only the pharmacist. That was a design choice for a two-ward pilot with a
small named staff list, not an oversight, but if you want governance separated
from pharmacy and surgical write access it is a change to one file and I will
make it before install, the same way I split the money desks. Say which you
want.

**2.5 Password policy?**
*Partial.* Minimum 8 characters, bcrypt-hashed, enforced on creation, admin
reset and self-service change. There is no complexity requirement, no expiry, no
history or reuse check, and no dictionary screening. Legacy SHA-256 hashes from
earlier builds are rejected by default since 10 August 2026; the only way to
accept one is an explicit migration flag (`ORB_ALLOW_LEGACY_SHA256`) that I
would set for one login and then remove.

**2.6 Account lockout and brute-force protection?**
*Partial.* Two layers. Per IP: 5 failed attempts in 15 minutes. Per account: 8
failed attempts in 15 minutes, stored in the database so it survives a restart
and cannot be evaded by rotating source IPs. The per-IP limiter is in-process
memory: it is lost on restart, and behind a reverse proxy it would key on the
proxy's address unless the proxy header is trusted. The server runs
single-process today, so it functions; a multi-worker deployment would need it
moved into the database.

**2.7 Session management: timeout, revocation, concurrent sessions?**
*Implemented.* Sessions expire after 24 hours. Every issued token carries the
user's `token_version`; incrementing that value revokes every outstanding
session for that user at once, across all devices. It is incremented on logout,
password change, MFA reset and account deactivation. `POST /api/auth/refresh`
slides a live session forward without a re-login. Concurrent sessions are
permitted and are revoked together, not individually; there is no per-device
session list.

**2.8 Privileged access: who at the vendor can reach our data?**
*Implemented.* Nobody, remotely. There is no inbound remote access to the
appliance, no vendor support tunnel, no telemetry channel and no vendor account
in the user table. I operate the appliance on-site only. If you want remote
support later, that is a change you would grant explicitly and I would document;
it does not exist today.

**2.9 How are service-to-service credentials handled?**
*Implemented.* One: the ingestion API key (`X-API-Key` on `/api/ingest/*`),
compared in constant time. In the production profile the application refuses to
start if that key is unset, as it does for the JWT signing secret, so a
misconfiguration cannot silently open a real-patient write path. Secrets live in
a `chmod 600` `.env` file on the appliance.

---

## 3. Encryption

**3.1 Encryption at rest: database.**
*Partial.* `orb.db` and its WAL/SHM sidecars are opened through SQLCipher
(AES-256) when `ORB_DB_ENCRYPTION_KEY` is set, which is the shipping appliance
configuration; the installer generates the key. Files are forced to `0600`.
Validated on 30 July 2026 on Apple Silicon alongside the vector extension,
full-text search and WAL mode. On 3 September 2026 I opened the development
database read-only with its key and recomputed the audit chain: 91,743 rows,
none broken.

It fails closed (since 3 October 2026): if the key is set but the SQLCipher
driver is missing, Orb refuses to open the database, on the live path and on
the backup path, instead of running in plaintext; and at boot it asks the open
connection for its cipher version and stops if there is none. The Trust Center
and the Data & Backups screen report what the connection shows, not whether a
key is configured.

One qualification: what it protects is copies, not the live machine: the key sits in `.env`
on the same disk, so anyone who can read the filesystem can read both. It
defeats a stolen or discarded SSD, a backup volume, an exported disk image. It
does not defeat an attacker who is already on the running host. FileVault is
required alongside it, and that is a hospital-side control.

**3.2 Encryption at rest: uploaded media and derived data.**
*Partial.* Imaging and consultation audio are encrypted individually with
AES-256-GCM under a separate key (`ORB_UPLOAD_ENCRYPTION_KEY`). Authenticated
encryption means a tampered file fails to decrypt rather than returning corrupt
data. In the production profile an upload that cannot be encrypted is refused
rather than written in cleartext (the default since 3 October 2026; the
installer also writes `ORB_REQUIRE_UPLOAD_ENCRYPTION=1`). An explicit `0`
opts out, and the Trust Center shows that opt-out. Certain PHI-derived cache
columns are separately field-encrypted with the same scheme.

There are two keys, not one. Escrowing one and losing the other still loses
patient data.

**3.3 Encryption in transit.**
*Partial.* The application can serve HTTPS directly (`ORB_TLS_CERT` and
`ORB_TLS_KEY`), or sit behind a TLS-terminating reverse proxy. Session cookies
are marked `Secure` automatically when the connection is HTTPS, honouring
`X-Forwarded-Proto`. Two gaps: the installer generates a self-signed certificate
that must be replaced before go-live, and in the production profile a missing
certificate produces a log warning rather than a refusal to start. The Trust
Center reads the certificate Orb serves (expiry, issuer, host names): it is red
with no HTTPS at all in production, amber on the self-signed certificate or
behind a proxy it cannot see, and green on a CA-issued certificate. Orb must
not be reachable over plain HTTP on your network. I will confirm the
certificate and the listener at install and record it.

**3.4 Security headers (HSTS, CSP, X-Frame-Options)?**
*Partial.* Since 3 October 2026 Orb sets `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: SAMEORIGIN` and a
Content-Security-Policy that forbids framing by other sites, plugins and
`<base>` rewriting, on every response. HSTS is sent on HTTPS connections only.
The policy does not yet restrict scripts to Orb's own origin: the page still
carries one inline script, and that tightening follows its removal.
Cross-origin requests are restricted by an explicit allow-list in the
production profile, and the Trust Center reads the policy actually installed.
Patient discharge-portal links are signed with their own key, separate from
the staff sign-in key.

**3.5 Key management: where do keys live, who holds them, how are they rotated?**
*Partial, and the highest-risk open item.* Keys live in the appliance's `.env`
file. They are not in the backups, deliberately, because a backup containing its
own key is not encrypted in any meaningful sense. There is no cloud KMS, because
a KMS would put disaster recovery behind an internet connection on the one day
you least control your environment, and would break the no-cloud commitment
this product is sold on.

The plan, scheduled for the dress-rehearsal phase, is two sealed printed
envelopes, one in the hospital safe and one held by me, each containing both
keys with a transcription checksum, tested by reading the envelope copy onto a
different machine and running a restore drill with it.

That ceremony has not happened yet. Until it does, the hospital's entire
clinical record depends on one file on one Mac. There is no re-key tooling:
rotating a key strands every backup taken under the old one, so the old key must
be retained until those backups age out. There is no vendor override and no way
to brute-force a lost key.

---

## 4. Audit and logging

**4.1 What is audited?**
*Implemented.* Every state-changing API request is captured by middleware as a
safety net, on top of richer per-route events. Authentication (success, failure,
rate-limit, lockout, MFA enrolment, failure and reset, logout), authorisation
denials with the exact missing permission, PHI access, clinical actions,
administrative user-lifecycle changes, patient erasure, and one record per AI
inference naming the model that served it and whether the request was
classified as containing PHI.

One gap, since closed. Until 14 August 2026 the middleware safety-net rows
carried no actor for browser sessions, because that layer read the acting user
only from a bearer header and the browser signs in with a cookie. The
14 August dry run caught it and I fixed it the same day; browser-originated
rows now carry `actor_user_id` and `actor_role`. I mention it because the
original test for that field sent a bearer header, which is why it passed, and
that is the kind of green test I no longer accept as evidence.

**4.1a Are read accesses audited?**
*Partial.* PHI reads on the routes that call the audit helper explicitly are
recorded. The middleware net covers `POST`, `PUT`, `PATCH` and `DELETE` only and
deliberately does not log `GET`, so a bulk read on a route that does not audit
itself leaves no middleware row. Do not assume universal read logging.

**4.2 Is the audit log tamper-evident?**
*Partial, and precisely so.* Two layers. At the database layer, `UPDATE` and
`DELETE` on `audit_logs` are blocked by triggers that abort the statement; the
application's own database user cannot rewrite history. On top of that, each row
stores a SHA-256 hash computed over the previous row's hash plus a fixed,
ordered set of its own fields. Altering or removing a row breaks the chain, and
a verifier recomputes the whole chain on demand. It also runs as part of the
monthly restore drill, because a restored backup with a broken chain is
indistinguishable from a tampered one.

The limit: this is tamper-evidence, not notarisation. It proves nothing was
altered through the application and it detects file-level edits. Someone with
the raw database file, the key and knowledge of the algorithm could recompute a
consistent chain offline. Closing that requires anchoring chain checkpoints to
something outside the appliance, a WORM store or a printed and countersigned
checkpoint. I do not do that today. A re-sealing utility exists for repairing
development data and refuses to run outside the development profile without an
explicit override flag.

**4.3 Can an audit write be skipped?**
*Partial.* For security, clinical-access, PHI-access and
authentication events the audit write is fail-closed: if it cannot be persisted,
the action does not proceed. Patient erasure, for example, aborts entirely rather
than erasing unaudited, and a login whose audit row cannot be written issues no
session. Purely operational categories are fail-soft so an audit hiccup cannot
take down a ward-facing path.

The gap, which I found while writing the first version of this document, was
fixed on 4 September 2026. The fail-closed list had named five categories, and
two of them were dead:

- `auth` was never used; every login, MFA and lockout event is written under
  `authentication`, which was not on the list. So authentication events were
  logged fail-soft: recorded on the chain in normal operation, but a failed
  audit write would not have blocked a login. `authentication` is now on the
  list.
- `ai_egress` was never used either. It was a leftover from the optional
  hosted-AI lane that was removed in July 2026. The per-inference records that
  are written go to `ai_inference`, which is now on the list in its place.

The caveat is the AI lane. `ai_inference` is fail-closed at the audit layer, but
the code that calls the model catches the audit error and still answers, so an
AI inference whose audit row cannot be persisted proceeds anyway; the failed
write is logged at CRITICAL. That is a deliberate choice not to let an audit
hiccup take down the offline clinical engine, and I would rather state it than
imply the opposite. A test now checks that every name on the fail-closed list
is emitted by real code, so a renamed category cannot quietly go dead again.

**4.4 Who can read the audit log?**
*Implemented.* The `audit.read` permission, held only by the admin role. Access
to the audit log is itself audited.

**4.5 Log retention and rotation?**
*Not yet.* There is no retention policy and no rotation. The audit log is
append-only and grows without bound; at pilot volume that is manageable, but it
is not a policy and I will not describe it as one. Defining a retention period
that satisfies Indian medical-records requirements is a Legal item (section 9).

**4.6 Can logs be exported to our SIEM?**
*Partial.* Audit records are queryable through an admin API and the appliance
exposes a Prometheus metrics endpoint with coarse operational counts and no PHI
(open by default; gate it with `ORB_METRICS_TOKEN`). There is no syslog
forwarder and no native SIEM connector. If you need one, tell me the format; the
data is structured and the export is small work.

**4.7 Time synchronisation?**
*Hospital.* All timestamps are UTC from the host clock. Keep the appliance on
your NTP source; an audit chain is only as good as its clock.

---

## 5. Backup, recovery and business continuity

**5.1 How are backups taken?**
*Implemented, scheduled at install.* Nightly at 02:30 via a launchd job that the
install script sets up. The snapshot uses SQLite's online backup API through the
encrypted connection, so it is transactionally consistent on a hot database; a
plain file copy of a live WAL database produces a file that looks fine and is
silently torn. Uploaded media is archived beside it with the same timestamp,
because restoring an image from one night against a chart from another is its
own kind of clinical error. To be plain about the current state: on my
development machine nothing is scheduled today; the schedule is installed on
your appliance during the hardening phase and verified in the restore drill.

**5.2 Are backups encrypted? Where are they stored?**
*Implemented.* Backups are SQLCipher-encrypted with the same database key; media
files carry their own AES-256-GCM encryption. Fourteen copies are retained
locally, and each verified backup is copied to an off-box destination when one
is mounted. Because the files are already ciphertext, the off-box destination
needs to be trusted for availability, not confidentiality.

**5.3 How do you know a backup is good?**
*Implemented, and this is the part I test hardest.* The backup script verifies
before it rotates: size floor, opens the result with the key, runs an integrity
check, and asserts real row counts. If verification fails it exits with an error
and rotates nothing, so a bad run can never prune good backups. That is not
theoretical caution. An earlier version of the script wrote zero-byte "backups",
pruned the real ones to make room, and reported success every night.

Two later findings, disclosed because they are the point. Until 14 August 2026
the verifier checked 4 of the 66 tables it knew about; it now checks all 88 in
the current schema. And on 15
August a restore drill certified the wrong file: two backup folders had quietly
diverged for a fortnight, and the drill picked a backup at schema version 4 when
the build was at version 32. The stale folder is quarantined, every backup-path
resolver now has to agree, and both are permanent tests.

A monthly restore drill takes the newest backup, copies it to scratch, opens it
with the key, and asserts integrity, row-count floors and unbroken audit-chain
linkage. It never touches the live database and refuses if pointed at it.

**5.4 What are the RPO and RTO?**
*Partial.* RPO: 24 hours. Backups are nightly; a restore loses everything since
the last one: vitals, notes, orders. Nothing in Orb makes that loss visible to
clinicians after the fact, so the ward must be told which window was lost. If 24
hours is not acceptable for live patients, I add a midday run; that is your
decision and I would like it made before go-live.

RTO: not measured. The restore procedure is written and the drill proves the
backup is readable, which is most of the risk, but the full swap-and-boot path
has not been rehearsed end to end on a Mac Studio. That rehearsal is scheduled
for the dress-rehearsal phase, with your IT staff present. I will give you a
measured RTO after it, not before.

**5.5 Is there a standby or high-availability option?**
*Partial.* The warm-standby tooling is built: a nightly job on a second Mac that
verifies a backup before installing it (size floor, decrypt, integrity check,
row-count floors, audit-chain linkage, and that a login-capable admin exists),
stages it on the same filesystem and moves it into place so a power cut leaves
the previous copy intact, keeps one generation back, unpacks the matching media
archive, and writes a receipt the heartbeat reports. It refuses to run if it
detects a live Orb on the port. The switchover runbook is written
(`docs/WARM_STANDBY.md`).

What does not exist is the second Mac. Today there is one machine, so none of
that is in force. It becomes real when the standby is purchased and built, in
the appliance-hardening phase, and, like the restore path, it is not proven
until it has been rehearsed. The primary reliability line remains that the
appliance does not fail: soak tests at pilot volume, load testing, and the
performance work behind them, with the standby as insurance designed never to be
used.

This is not replication: no streaming, no synchronous commit, no automatic
failover. A switchover is a human decision at every step, deliberately, because
an automatic failover on a false positive would put two appliances on the ward
LAN, each accepting orders. Worst-case data loss on an unplanned failover is the
same 24 hours as a restore.

**5.6 What happens if the appliance is down during a shift?**
*Implemented.* Downtime mode holds a durable, local, read-only snapshot of each
patient's must-know picture: demographics, allergies, problems, active
medications, the latest vitals with the rule-based NEWS2 score, and recent
signed notes. It is assembled from database reads only, no language model and no
network, and it prints. A per-ward pack is regenerated hourly and can be read
straight off the filesystem when the server is down. The drill for using it is
rehearsed with your IT staff before go-live, not just documented.

**5.7 Alerting on backup failure?**
*Built, not yet deployed.* The outbound no-PHI heartbeat that carries backup
age, disk headroom, loop health and access posture runs on its own daemon thread
so a frozen event loop cannot silence it, issues no database query at all, and
key-allowlists its payload before send. It ships disabled and does nothing
unless `ORB_HEARTBEAT_URL` is set. The receiving end exists since 10 August
2026: a standalone receiver with a dead-man alarm that pages when a beat is
missed or unhealthy, with a simulation drill that must produce the page before I
trust it. I deploy that receiver outside your building before go-live. Until it
is live, the control is my daily check, and the monthly drill is a calendar
event with a human name on it.

**5.8 Can we get our data out?**
*Implemented.* FHIR R4 export of the clinical record, with provenance tags
marking Orb as the source and flagging anything AI-derived; an unverified
AI-suggested medication is exported as a draft proposal, never as an active
order, so it cannot silently become a live order in a receiving system. The
database itself is a single SQLite file you own, on hardware you own. There is
no lock-in and no vendor-held copy to request.

---

## 6. Vulnerability and patch management

**6.1 How are vulnerabilities in dependencies identified and patched?**
*Not yet; this is a genuine gap.* Dependencies are pinned by version in
`requirements.txt` and `package-lock.json`, which makes builds reproducible and
prevents surprise upgrades, but there is no automated vulnerability scanning: no
Dependabot, no `pip-audit`, no `npm audit` gate in CI, and no SBOM. For a single
appliance with no inbound internet exposure the practical risk is lower than for
an internet-facing service, but that is a mitigating circumstance, not a
control. Adding a scanner to CI is small work and I will do it before go-live if
you require it.

**6.2 What does your CI actually gate on?**
*Implemented.* Every push runs the backend test suite, a TypeScript typecheck
and production build, frontend unit tests, a lint ratchet that blocks new lint
debt, an automated visual audit that drives every screen in every role and both
themes and fails on contrast, visibility or JavaScript errors, and five
end-to-end browser tests. Measured on 3 September 2026: 6,438 backend tests
collected across 180 files (the last full run, 18 August, passed 6,424 with 0
failures), 1,109 frontend tests across 90 files, and a doc-citation gate that
checks 9,130 cross-references. The backend suite includes dedicated suites for
role-based access control, authentication, MFA and session revocation, account
lockout, CSRF, audit-chain integrity, database and field encryption, adversarial
prompt handling, and the allergy-override rules.

One caveat about that suite: it runs against a plaintext database. The encrypted
driver has a distinct failure mode (see 6.4) that green tests structurally
cannot reproduce, which is why I also run soaks and drills.

**6.3 How is the appliance patched? What is the update process?**
*Partial.* Updates are applied on-site by me, with a backup taken immediately
before, and are reversible by restoring that backup. macOS updates are a
hospital control; note that they can reset the automatic-login setting that the
nightly backup job depends on, and that setting must be re-checked after every
one. There is no automatic update channel and no unattended patching; I consider
that correct for a clinical appliance, but it means patch latency depends on a
scheduled visit.

**6.4 Known defects you are disclosing.**
*Implemented, disclosed deliberately.* You should weigh a vendor by what it
volunteers, so here is the list.

1. The appliance froze three times in the week of 28 July 2026. All three were
   root-caused and fixed: a macOS proxy-configuration lookup running
   synchronously on the event loop (twice on 1 August, once for three and a
   half hours, with the health endpoint dead; a regression test now fails the
   build if a new client omits the fix); the encrypted-database driver holding
   Python's global lock across a database close, which froze the process for
   roughly a millisecond per megabyte of write-ahead log; and a lock starvation
   from running the embedding model in-process, fixed by moving that work into
   a separate process.
2. On 14 August a dry run of all eight ward workflows found six places where a
   cancelled background task could hang shutdown. All six are fixed and shutdown
   is now bounded at 10 seconds, with 13 new tests.
3. On 15 August a browser check found that an old medication override could be
   carried forward by a nurse, so that a nurse could give ceftriaxone to a
   patient with charted anaphylaxis and the server returned 200 OK. The gate had
   guarded the write lane but not the carry lane. It was closed the same day:
   only a prescriber can clear a block over a charted allergy, carried overrides
   without a prescriber's stamp fail closed, and the case is a permanent test.
4. The same day, the backup incident described in 5.3.

Evidence after the fixes: the valid load test is from 11 August 2026, 15 minutes
at a 60-bed census, 3,634 requests, zero errors, health p99 5 ms, no request over
200 ms, run twice with the same result. I voided every earlier soak report,
including the alert-storm figures the first version of this document quoted,
because the harness had been measuring the wrong process under the wrong
profile. The storm probe is re-run before every phase gate.

---

## 7. Incident response and breach notification

**7.1 Do you have a documented incident response plan?**
*Partial.* Runbooks exist for the failure modes I have hit: backup failure,
restore, downtime mode, appliance stall. The appliance carries a watchdog that
records evidence of a stalled process without ever restarting anything, because
a watchdog that restarts a clinical system on a false positive is more dangerous
than the bug it was written for. What does not yet exist is a formal
security-incident plan with named roles, severity definitions and contact
timelines. That is a joint document with the hospital, and it should be agreed
before go-live rather than drafted during an incident.

**7.2 Who do we contact, and how fast do you respond?**
*Partial.* Me, directly, and I site the pilot within an hour of where I am. There
is no 24x7 rota, no on-call team and no contractual SLA today. For a two-ward
shadow-mode pilot operated by its builder I think that is honest and adequate;
for wider deployment it would not be, and I would rather agree the target now
than imply one I cannot meet.

**7.3 Breach notification: what would you tell us, and when?**
*Legal, with an engineering floor.* My commitment is to notify the hospital's
named contact without undue delay and in any case within 24 hours of becoming
aware of a suspected compromise of the appliance or its data, with what I know,
what I do not, and what I am doing. The hospital, as the entity holding the
patient relationship, is the party with statutory notification duties under
India's DPDP Act; the contractual notification window and the precise
apportionment of duties must be set by counsel and written into the agreement,
not assumed from this document.

**7.4 What forensic evidence would be available after an incident?**
*Partial.* The hash-chained audit log (actor, route, IP, user agent, outcome,
correlation ID), the per-inference AI records, the watchdog's process dumps, and
the backup series, which, because backups are retained for 14 days, gives a
14-day window of point-in-time database states. There is no host-level EDR, no
network flow logging and no off-box log shipping. Host and network monitoring
are hospital-side controls.

---

## 8. Data residency, sub-processors and third parties

**8.1 Where is patient data stored and processed?**
*Implemented.* On the appliance, in your building. Storage and every AI
inference (language model, vision, speech-to-text, embeddings) happen on that
machine. No patient data is transmitted to any external service by design, and
there is no cloud inference provider in the code: the model layer has exactly
one provider and it points at the local process.

**8.2 List every sub-processor that touches patient data.**
*Implemented.* None. There is no cloud vendor, no analytics service, no error
reporting service, no model API and no vendor-side copy of your data. This is
the central claim of the product and it is enforced in the code rather than
promised in a policy.

**8.3 Then what does the appliance talk to on the internet?**
*Implemented, and disclosed in full.* Two categories, neither carrying patient
data:

- Environmental and public-health feeds: Open-Meteo (weather, air quality),
  openFDA (drug recalls) and CDC influenza data. Outbound read-only requests
  whose parameters are a latitude and longitude and public query filters. Every
  call is logged. These can be blocked at your firewall with no loss of clinical
  function.
- Model weights and software updates, downloaded once at installation, not
  during operation.

Optionally, and off by default: browser-based cloud dictation. The Web Speech
API streams microphone audio to the browser vendor's speech service, which would
be egress of spoken PHI, so the server ships it disabled and the client asks the
server for permission before starting it, treating "no answer" as no. All three
voice surfaces enforce that check, and local Whisper transcription is the source
of truth regardless. Honest scope: this is a client-side gate, so a modified
browser could bypass it; the definitive control is your firewall. I recommend
leaving it off.

**8.4 Does data cross a national border?**
*Implemented.* No patient data leaves the appliance, so no patient data leaves
India. The public feeds in 8.3 are outbound requests to overseas servers carrying
no personal data.

**8.5 What happens to our data if we stop working with you?**
*Implemented.* Nothing leaves. You own the hardware and the database file. I
would hand over the keys (you already hold the escrow envelope), the FHIR export
tooling and the documentation. There is no vendor-held copy to delete because
there is no vendor-held copy.

---

## 9. Data protection: DPDP Act 2023 obligations

*Every answer in this section is engineering-verified but has a Legal overlay:
the mapping of Orb's technical controls onto the Act's defined roles and duties
must be confirmed by counsel, who is engaged. I describe what the software does;
I do not offer a legal conclusion about what the Act requires of you. For dates:
the DPDP Rules were notified on 13 November 2025 and the substantive obligations
on data fiduciaries take effect on 13 May 2027.*

**9.1 Who is the Data Fiduciary and who is the Data Processor?**
*Legal.* My working assumption is that the hospital is the Data Fiduciary and
Orb is a Data Processor acting on the hospital's instructions. Because Orb runs
entirely on hospital-owned hardware with no vendor access to the data, the
processing footprint is unusually small, but the classification and the
resulting contractual duties are counsel's call, and they should be written into
the agreement before real patient data flows.

**9.2 Consent and notice.**
*Hospital / Legal.* Orb does not collect consent from patients; it records what
the hospital's clinicians enter and what the hospital's systems send it. Notice
and consent to patients for the processing of their data, including its
processing by an AI system, is the hospital's obligation, and the wording is a
legal question. Ask counsel specifically whether AI-assisted drafting requires
distinct notice.

**9.3 Purpose limitation and data minimisation.**
*Partial.* Orb holds what a hospital record system holds, for the purpose of
delivering care. There is no secondary use: no analytics product, no data sale,
no model training on your data by me. The one forward-looking use, fine-tuning
the clinical model on shadow-mode data, is explicitly deferred beyond the pilot
window and would need separate written agreement before it happened. The gap:
the ingestion pipeline has no automated minimisation, so whatever a source
system sends is retained.

**9.4 Right of access and correction.**
*Partial.* Patients have a portal (Bridge) giving self-scoped access to their
own status and record. Correction runs through the clinical workflow, a
clinician amends the record, rather than through a self-service data-subject
request interface. There is no dedicated data-subject-request workflow with
tracked timelines, and that is a Legal and process question as much as a
software one.

**9.5 Right to erasure.**
*Built, but currently failing; do not rely on it yet.* Everything described in
this answer exists and is reachable in the Admin console, but on this build the
purge aborts with a server error for any patient who has real shadow-mode
records, which in the pilot is every patient. The cause: the erasure plan is
derived from the schema, so it includes the `shadow_events` table, and that
table is deliberately append-only at the database layer for non-simulator rows;
the immutability guard that makes the shadow-mode evidence trustworthy rejects
the delete and the whole transaction rolls back. Nothing is partially erased
(the transaction is atomic), but nothing is erased either. The fix is to exclude
or specially handle `shadow_events` the way `audit_logs` already is, and it is
on my list ahead of go-live. Until it lands, treat data-subject erasure as
unavailable and say so in the agreement. The rest of this answer describes the
intended and implemented behaviour.

An admin can erase a patient (`POST /api/admin/patients/{id}/erase`). The
erasure plan is derived from the live database schema on every call rather than
a maintained list, so a table added by a future update is covered the day it
appears; the previous hand-maintained list had fallen a dozen changes behind and
was leaving full records in place while reporting success. It anonymises the
patient row and purges every patient-linked clinical row in one transaction; it
refuses to erase a currently admitted patient; it is idempotent; and the audit
record of the erasure is retained (patient identifier only, no personal data) so
the erasure itself remains provable.

The limit, stated plainly: erasure does not reach existing backups. Backups are
immutable ciphertext and roll off on a 14-day retention. An erased record
therefore persists in encrypted backups for up to 14 days. That is a common and
generally defensible position, but it must be stated in the agreement rather
than discovered, a Legal item.

**9.6 Retention.**
*Not yet / Legal.* There is no automated retention or purge job for clinical
data or audit records. Medical-record retention periods in India are set by
statute and by the hospital's own policy, and the number must come from counsel
and your records officer. Once I have it, implementing a purge job is
straightforward; implementing it against a guessed number would be worse than
not having one.

**9.7 Do you carry out a DPIA?**
*Not yet / Legal.* No Data Protection Impact Assessment has been produced. For
an AI system processing health data one is likely expected, and it is on the
counsel workstream before go-live. I will contribute the technical sections.

**9.8 Security safeguards under the Act.**
*Implemented / Partial.* Encryption at rest and in transit, role-based access
control, tamper-evident audit logging, session revocation, backups with verified
restores and MFA are all in place, with the caveats named throughout this
document. What is not in place: a formal security-incident plan (7.1), a
retention policy (9.6), a DPIA (9.7), external penetration testing (11.1) and
dependency vulnerability scanning (6.1). Whether that set satisfies the Act's
"reasonable security safeguards" standard is counsel's judgement, not mine.

---

## 10. Medical device and clinical decision support classification

**10.1 Is Orb a medical device? Is it registered under the Medical Devices Rules?**
*Legal.* I do not present Orb as a medical device and it holds no CDSCO
registration. My position is that it is clinical decision support: it drafts,
flags, summarises and retrieves, and every clinical action requires a qualified
clinician's explicit approval before it becomes an order or a signed record. The
classification of AI-based clinical software under the Indian Medical Devices
Rules is an area of active regulatory development, and counsel is engaged
specifically to give a written view before go-live. I am flagging this rather
than asserting a conclusion, because a vendor's confident opinion on its own
regulatory status is worth very little to a CIO.

**10.2 What safeguards keep the AI in a decision-support role?**
*Implemented.* Everything the AI produces is labelled a draft. A clinician
approves before anything is committed, and the approval is audit-logged with the
clinician's identity. Deterministic gates sit in front of chart-changing actions
so a safety net does not depend on the model's own cooperation. Clinical answers
carry inline citations to their source. The risk score driving deterioration
alerts is NEWS2, a published rule-based score computed in code, not a model
output, so the alerting layer is explainable without reference to the model.

**10.3 What about the imaging module?**
*Implemented.* Lens ships disabled in production. It is a general-purpose vision
model writing reference prose in the most regulated space Orb touches, with no
validated imaging model behind it. Enabling it is an explicit operator decision
that accepts it as clinician reference only and never a diagnostic device. My
recommendation for the pilot is to leave it off.

**10.4 How is the AI's performance validated?**
*Partial, and this is the pilot's central open question.* The pilot begins in
shadow mode: Orb runs on real ward data, forms its own view, drafts what it
would have said, logs all of it, and shows none of it to any clinician. That is
the only window in which "was it right, was it early, would acting on it have
changed anything" is answerable, because from the moment a clinician sees an Orb
alert, Orb has changed the outcome it is being measured against.

What is instrumented today is the full loop (what was flagged and when, what was
drafted, what was routed, what the clinician actually did) and the composite
scorecard over it, plus a charge-nurse end-of-shift screen for recording what
happened to each patient (built 10 August 2026). What remains open is the
definition: what counts as the ground-truth escalation event and who records it.
That decision belongs to your physician champion, and without it the headline
metric, detection lead time against what actually happened, cannot be computed.
It is a request of the hospital, and it is on the critical path.

**10.5 Model provenance and governance.**
*Implemented.* The models are MedGemma (Google), Whisper (OpenAI) and BGE-M3
(BAAI), downloaded once and run locally. The appliance records which model
served each inference on the tamper-evident audit chain, so "which model saw
this patient" is answerable after the fact. A model-governance view reports how
many of the deployed models are formally validated and how many are not, and
reports the unvalidated ones as unvalidated: today that reads 1 of 4, and the
one is the NEWS2 engine. The learned prediction models ship switched off until
they are trained and validated on real ward data.

---

## 11. Assurance, testing and de-provisioning

**11.1 Have you had an external penetration test?**
*Not yet.* No external penetration test, no third-party security assessment, no
security certification of any kind. What exists is internal: the test suites
listed in 6.2, including adversarial-input and role-escalation tests, and
multi-agent code audits run before each phase gate. If a penetration test is a
condition of your approval, tell me now; it is a scheduling and cost question
with a long lead time, and it belongs in the pre-go-live window, not after it.

**11.2 Are you HIPAA, ISO 27001, SOC 2 or HITRUST certified?**
*Not yet.* None of them. Orb is a pre-pilot product built by one person. Running
entirely on-premises removes the cloud data-egress risk that most of those
frameworks are largely concerned with, but it does not substitute for the
surrounding programme (the impact assessment, the retention policy, the incident
plan, the drills), and I will not claim that it does.

**11.3 How are users de-provisioned when staff leave?**
*Implemented.* An admin deactivates the account. Deactivation sets the account
inactive and increments the token version, so every outstanding session dies
immediately, and the login path also rejects inactive accounts, so a fresh login
with valid credentials fails too. The account is retained, not deleted, so its
audit history remains attributable. An admin can additionally clear the
account's MFA factor and re-provision its order-signing PIN. Role changes take
effect on the next request, because the role is re-read from the database each
time rather than trusted from the token.

The process gap: there is no periodic access review. Nobody is prompted to
re-confirm the user list. For a two-ward pilot with a short named list that is
manageable, and I would propose a quarterly review as a joint calendar item.

**11.4 What physical security does the appliance need?**
*Hospital.* A locked room or rack; FileVault enabled; screen lock with a short
auto-lock; a network segment appropriate to a clinical system of record.

One trade-off you should know about, because it is uncomfortable and I would
rather you heard it from me. The nightly backup runs as the operator's own user
account rather than as root, deliberately, because a root job would have to read
the operator's key file, concentrating key and ciphertext at one privilege
level, which is the exact thing encryption at rest exists to prevent. The cost
is that automatic login must be enabled on the appliance, or an unattended
reboot stops backups silently. That weakens physical security in exchange for
backup reliability. It is defensible because Orb itself only runs in that user
session (if nobody is logged in, nothing is being written and nothing new is at
risk), but it makes the locked room a real requirement rather than a
nice-to-have.

**11.5 Can we verify any of this ourselves?**
*Implemented.* The appliance serves a live Trust Center report generated from
actual system state, not from a document: encryption posture, audit-chain
verification status and row count, MFA adoption per role, the full role and
permission matrix, validated versus unvalidated models, and a control checklist
with amber and red statuses shown honestly. It is built to show warnings rather
than a green wall. One caveat: its TLS-in-transit row is hardcoded to amber and
does not read configuration, so read that row against your actual deployment. I
will walk through the report with you at the review, and you can open it
yourself any time afterwards.

---

## Summary of open items

The things on this list are why the CIO review happens before go-live, not
after. Status as of 3 September 2026.

| # | Open item | Owner | Target |
|---|---|---|---|
| 1 | Key-escrow ceremony: two sealed envelopes, both keys, tested against a restore | Joint | Dress rehearsal |
| 2 | End-to-end restore rehearsal on the appliance; measured RTO | Joint | Dress rehearsal |
| 3 | MFA mandated for admin accounts | Orb | Done in the pilot configuration; confirm at install |
| 4 | Real TLS certificate; confirm no plain-HTTP listener (Orb now sets its own security headers, 3.4) | Joint | Install |
| 5 | Counsel's written view: DPDP roles, retention period, device classification | Legal | Before go-live |
| 6 | Ground-truth outcome capture: definition and owner (the capture screen exists) | Hospital champion | Blocking the headline metric |
| 7 | Security-incident plan with named roles and timelines | Joint | Before go-live |
| 8 | Dependency vulnerability scanning in CI | Orb | On request |
| 9 | Heartbeat receiver deployed outside the hospital (both ends are built) | Orb | Appliance hardening |
| 10 | Warm-standby second machine (the restore job and switchover runbook are built; the hardware is not bought) | Joint | Appliance hardening |
| 11 | Whether `helix.write` and `surgery.write` stay in the admin role (2.4) | Hospital decision | Before install |
| 12 | External penetration test: required for approval or not? | Hospital decision | Decide now (long lead) |
| 13 | Access-review cadence | Joint | Agree at review |
| 14 | Two dead fail-closed audit categories (4.3) | Orb | Before install |
| 15 | Patient erasure blocked by the shadow-event guard (9.5) | Orb | Before go-live |
| 16 | Bedside-monitor adapter tested on the ward's real devices | Joint | Integration phase |
| 17 | On-device speech engine installed on the appliance | Orb | Install |

*Companion documents: `docs/SECURITY_ONEPAGER.md` (one page, for the review
meeting), `docs/BACKUP_AND_RESTORE.md` (the operator's runbook),
`docs/DEPLOYMENT.md` (encryption at rest in detail), `docs/SHADOW_MODE.md` (how
the pilot runs and what the scorecard can and cannot prove).*
