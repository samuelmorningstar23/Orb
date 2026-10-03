## 13. Administration & Governance

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![The administration workspace: user accounts, audit trail, model governance, and compliance.](assets/manual/admin-panel.png)

*The administration workspace: user accounts, audit trail, model governance, and compliance.*

![The AI model panel, opened from the header chip. Administrators can load or unload each local tier.](assets/manual/ai-model-configure.png)

*The AI model panel, opened from the header chip. Administrators can load or unload each local tier.*

![Unloading asks for confirmation and lists exactly which clinical features stop working until the model is loaded again.](assets/manual/ai-model-unload-confirm.png)

*Unloading asks for confirmation and lists exactly which clinical features stop working until the model is loaded again.*

Every hospital that runs Orb needs one person who can answer three questions at any moment: *who can do what in this system, what has actually happened in it, and can we prove both?* The Admin console is where that person works. It gathers user management, the tamper-evident audit trail, AI model governance, the live compliance posture, backups, data erasure, and downtime preparedness into a single screen, and it is available only to accounts with the **admin** role (in the demo environment, sign in as **ADM001, Rajesh Kumar**).

This chapter covers the Admin console tab by tab, then the three governance surfaces that live outside it: EHR Downtime Mode, the Revenue Integrity panel, and the operational metrics endpoint used by IT.

### 13.1 Opening the Admin console

1. Sign in with an admin account (employee ID and password).
2. In the left sidebar, click **Admin** (the shield icon, at the bottom of the navigation list).
3. A row of sub-tabs appears across the top of the panel. Click any of them to switch views.

| Sub-tab | What it shows |
|---|---|
| **Overview** | Pre-aggregated 24-hour governance snapshot: audit volume, ingestion health, alerts, auth activity |
| **Audit Logs** | The full searchable, filterable audit trail |
| **Trace** | Every audit event that shares one correlation ID, ordered as a pipeline |
| **Ingestion** | Recent device/interface payloads and their parse status |
| **Security** | Authentication and permission events only |
| **Alerts** | Vigil and Relay alert-generation records |
| **Model Governance** | The AI/ML model registry with provenance and validation status |
| **Impact** | Quantified ROI and outcomes dashboard with tunable assumptions |
| **Trust Center** | Live compliance posture: trust score, control checklist, MFA adoption, RBAC matrix |
| **Flight Recorder** | Replayable timeline of audit events, system events and order transitions, with chain verification |
| **Users** | User accounts: create, change role, reset password, deactivate/activate |
| **Data & Backups** | Database backups and GDPR patient erasure |

Admins also see most clinical panels (Patients, Messages, Sage, Forecast, Command, Simulator, Revenue, Appointments) so they can follow up on anything the governance views surface.

### 13.2 Overview: the governance snapshot

The **Overview** tab answers "is the system healthy right now?" with six stat cards, all computed over the last 24 hours:

- **Ingestion Mode**, whether the deployment runs in **Simulation**, **Ingestion**, or **Hybrid** mode (live interfaces vs. the built-in demo generator), plus whether audit simulation is On or Off.
- **Audit Events (24h)**, total audit rows written, broken down by category chips (e.g. `phi_access`, `authentication`).
- **Ingested Payloads (24h)**, interface payloads received, with chips for **parsed**, **failed**, and **dupes**.
- **Source Distribution**, distinct ingestion sources and the volume from each.
- **Alert Activity (24h)**, Vigil and Relay alerts generated, combined and per module.
- **Records Written (24h)**, vitals and observations actually inserted into the chart.

Below the cards, a **Recent Parse Failures** table lists the last five payloads that could not be parsed (time, source, error text, correlation ID), and an **Auth Summary (24h)** section shows login successes, failures and permission denials as outcome-badged chips.

### 13.3 User management (Users tab)

The **Users** tab lists every account in the system, ordered by role and name. Each row shows **Name**, **ID**, **Role** (color-coded chip: doctor, nurse, admin, pharmacist, patient), **Department**, **MFA** (**On** if the user has enrolled a TOTP authenticator, **Off** otherwise), **Status** (**Active** or **Disabled**, disabled rows are tinted), and an **Actions** column. Every action described below is written to the audit log with the acting admin's identity.

#### Creating a user

1. Click **Create user** (top right of the panel).
2. Fill in the dialog: **User ID** (this becomes the login ID, e.g. `DR-0142`), **Full name**, **Role** (doctor, nurse, admin, pharmacist, or patient), **Department** (optional), and a **Temporary password** of at least 8 characters. The password field is shown in clear text so you can read it out or copy it.
3. Click **Create user**. The list refreshes and a "User created" confirmation appears.

Hand the ID and temporary password to the new user; they should change the password from their own session (self-service password change immediately signs them out everywhere, including their current session, so they will log back in with the new password). Duplicate IDs are rejected with a clear error.

#### Changing a role or department

Click **Role** on the user's row. The **Change role & department** dialog opens pre-filled; adjust either field and click **Save changes**. Role changes take effect on the user's next request, permissions are checked per-request against the current role, so there is no need to force a re-login.

#### Resetting a password

Click **Reset password** on the row. The dialog warns that this **revokes all of the user's active sessions**. Type a new password (minimum 8 characters) or click **Generate** for a random one, click **Copy** to put it on the clipboard, then **Set password**. The user is signed out of every device instantly and must log in with the new password.

#### Deactivating and reactivating

Click **Deactivate** on an active user. The confirmation dialog states exactly what will happen: the user *"will be blocked from logging in and all their active sessions will be revoked."* Confirm with **Deactivate**. Session revocation is immediate, any token the user already holds stops working on their next request. A disabled account is re-enabled with **Activate** on its row.

Two guardrails apply: you cannot deactivate your own account (this prevents locking out the last administrator mid-session), and deactivation never deletes anything, the account, its history and its audit trail remain intact.

#### MFA status and signing PINs

The **MFA** column is read-only for the admin: enrollment is self-service. Each staff member enrolls their own authenticator app via the **Security & MFA** button in the sidebar footer of their session; once enrolled, the chip flips to **On**. The Trust Center (section 13.6) tracks adoption per role, including any roles your deployment has configured as MFA-mandatory.

Separately from login MFA, Orb uses a short numeric **signing PIN** as the second factor when a clinician confirms agentic orders. A user provisions their own PIN by re-entering their password (a stolen session token alone can never mint a signing credential); an admin can provision or replace a PIN for any user. Every provisioning is audit-logged.

### 13.4 The audit trail and hash-chain integrity

Orb's audit log is append-only and **tamper-evident**: every row is sealed into a SHA-256 hash chain, meaning each entry's fingerprint incorporates the fingerprint of the entry before it. Altering or deleting any historical row breaks the chain from that point forward, and the break is detectable and locatable. This is the backbone of every governance claim in this chapter.

#### Audit Logs tab

The filter bar accepts a **Correlation ID**, a **Patient ID**, and dropdowns for action type (**All actions**), category (**All categories**) and outcome (**All outcomes**, success, failure, partial success, denied). Press Enter in either text field or click the search button to apply. Results are paginated 25 per page with a running total.

Each row shows Timestamp, Correlation, Category, Action, Outcome, Actor, Patient and Summary. The correlation ID is a link: clicking it jumps to the **Trace** tab with that ID pre-loaded, showing every event that shares it, for example, one device payload traced from *ingestion request received* through *vital inserted* and *NEWS2 recomputed* to *Vigil alert generated*. This is the fastest way to answer "why did this alert fire?"

The **Security** tab is the same log pre-filtered to authentication and permission events, and the **Alerts** tab isolates Vigil and Relay alert-generation records, useful for reviewing alert volume without wading through the full trail.

#### Flight Recorder

The **Flight Recorder** tab is the forensic replay surface: a unified, time-ordered stream of audit events, internal system events, and order state transitions, which an admin can scrub through like a video.

- **Chain badge**, the header always shows the current integrity verdict: a green **Chain verified · N** badge (N sealed rows checked), or a red **Tampered at #N** badge identifying the first broken row. This verification runs live against the actual database every time the recorder loads.
- **Scope**, type into **Filter by patient ID…** and press Enter (or click **Reload**) to replay everything that happened to one patient; leave it empty for the hospital-wide stream (up to the most recent 500 events).
- **Transport bar**, step backward (◀), **play/pause** (advances automatically about once per second), step forward (▶), plus a scrubber slider and a position counter (`current / total`) with the selected event's timestamp.
- **Timeline list**, each entry is color-coded by lane (**Audit**, **Event**, **Order**) and carries a ✓ when its chain link verifies or a ⚠ flag if that row is implicated in a chain break.
- **Detail pane**, for the selected event: Time, Actor, Target, Patient, Correlation, Detail, and the complete **Raw record** exactly as stored.

Use the Flight Recorder for incident reconstruction ("show me everything around 03:20 for bed 12"), for demonstrating tamper-evidence to an auditor, and for verifying that a suspected integrity problem is or is not real.

### 13.5 AI model governance (Model Governance tab)

Orb treats model honesty as a governance feature. The **Model Governance** tab is a registry of the system's four *clinical decision* model surfaces, each with: **Model** (name and task), **Version**, **Training data** (the lineage statement), **Checkpoint** (badge: **present** or **none**, whether a trained model file actually exists on this machine), **Validated** (badge: **validated** or **not validated**), and **Active**.

The registry is deliberately blunt. Every deployment shows exactly these four rows and no others, the registry is written from a fixed list in the code, and any row not on that list is deleted on start-up precisely so a stale entry cannot over-claim:

| Surface | What the registry states |
|---|---|
| Vigil NEWS2 | Deterministic rule-based early-warning scorer (RCP NEWS2, incl. Scale 2 for COPD), no training data by design |
| Vigil CfC overlay | **Untrained**, no checkpoint; the deterioration overlay stays inactive and risk falls back to NEWS2 alone ("fails closed") |
| Forecast LSTM | Trained on **synthetic** demo data, explicitly not validated on real clinical data |
| Sage LLM | MedGemma 27B running locally via Ollama; pretrained, not fine-tuned on your data; decision-support only |

**What the registry does not cover.** These four are the surfaces that produce a clinical judgement. The other models Orb runs locally are not listed in this tab: the smaller MedGemma 4B that serves ambient Relay classification and the fast tier of Thinking Mode, the Whisper speech-to-text model behind Scribe and voice, the vision model behind Lens, and the embedding/reranking models behind guideline retrieval.

The panel's own words set the policy: *"Orb never presents an untrained or unvalidated model as validated."* The same provenance is exposed to clinicians in miniature: AI outputs across the app carry a provenance chip identifying which model and version produced them and whether that model is validated, and the chip links back to this tab for the full record. If a checkpoint has been added or removed on disk, use the registry re-sync action to recompute checkpoint presence.

### 13.6 Compliance & Trust Center

The **Trust Center** tab renders a live compliance-posture report computed from the running system, not a static document. Most probes read real state, so amber and red results appear when they are true. **One exception:** the *TLS / encryption in transit* control is hardcoded to amber and reads no configuration, so it reports that Orb does not terminate TLS even where `ORB_TLS_CERT`/`ORB_TLS_KEY` are set and Orb is serving HTTPS itself.

At the top, a **Trust Score** ring shows a 0–100 weighted score across all controls (green at 80+, amber at 50–79, red below), alongside counts of **Passing**, **Warnings** and **Failing** controls and a **Models validated** tally, plus the generation time and deployment profile.

Below it:

- **Tamper-evident audit log**, a full chain verification with a badge reading **Verified, chain intact** or **Broken, tampering detected**, the number of sealed rows checked, and (if broken) the first failing row number. Category pills summarize audit coverage.
- **Control checklist**, each control shows a Pass/Warn/Fail/Unknown chip, concrete **evidence** ("what we observed"), and a **Remediation** hint. The controls evaluated are: *Encryption at rest, database*, *Encryption at rest, PHI uploads*, *Multi-factor authentication*, *Tamper-evident audit log*, *Role-based access control*, *Backup & recovery*, *Session revocation*, and *Data residency / on-prem*.
- **MFA adoption**, TOTP enrollment per role as progress bars, with the org-wide percentage and a **required** marker on any role your deployment mandates MFA for.
- **Posture mini-cards**, encryption at rest for the database and PHI uploads (SQLCipher and AES-256-GCM respectively, when keys are configured), model validation counts, and the on-prem attestation (whether the server binds only to this machine, and the size of the CORS allow-list).
- **RBAC matrix**, click **Show matrix** to expand the complete roles × permissions grid, exactly as enforced per-route on the server. This is the authoritative answer to "can a nurse do X?"

A **Warn** on an encryption control typically means the corresponding key is not configured on this deployment; the remediation text says exactly what to set. The Trust Center is strictly read-only reporting.

### 13.7 EHR Downtime Mode

Downtime Mode is Orb's answer to "what does the ward look at when the EHR, the network, or the AI stack is down?" It is a durable, local, **read-only** snapshot of each patient's essential clinical picture, demographics, allergies front and center (with an explicit "No known drug allergies recorded (NKDA)" statement when clean), latest vitals with the NEWS2 score and level, and current medications, rendered as a high-contrast, print-friendly document. Reading and capturing snapshots involves no AI and no network, which is precisely what keeps it usable during an outage.

#### Viewing and capturing a snapshot

1. From a patient's Vigil view, click **DOWNTIME** in the patient command bar. The snapshot opens in a new browser tab titled **EHR Downtime Mode** ("Local read-only snapshot · works offline").
2. If no snapshot exists yet, the page says so and invites you to capture one. Click **Capture snapshot** (or **Re-capture now** to refresh an existing one). Any clinician with vitals access can capture; every capture is audit-logged.
3. Click **Print** to produce the paper copy. The banner on every snapshot states **DOWNTIME SNAPSHOT**, the capture time, and who captured it, so a printed sheet is never mistaken for a live chart.
4. **Reload** re-reads the stored snapshot (it does not re-capture).

#### Downtime readiness (Command Center)

The Command Center includes a **Downtime Readiness** KPI: the percentage of currently admitted patients who have a recent snapshot, the raw count (e.g. "14/16 snapshots"), and the age of the oldest one. It turns amber below 80% and red below 50%. The **Capture all** button beside it snapshots *every* admitted patient in one action, run it as a pre-downtime drill, before planned maintenance, or on a schedule. The result ("N snapshots captured") appears inline and the KPI refreshes immediately. Capture-all is an admin action; it is also audit-logged with the count captured.

The operational habit to build: keep readiness green during normal operations, and print snapshots for the ward *at the start* of any planned outage, the printed sheet is the fallback, and it can only contain what was captured before the systems went down.

### 13.8 Backups and data erasure (Data & Backups tab)

#### Database Backups

The **Database Backups** card lists every existing snapshot (Filename, Size, Created, newest first) and shows the current count. To create one:

1. Click **Create backup**.
2. The confirmation dialog explains that a consistent snapshot of the live database will be written to the server's backups directory and integrity-checked, and that this is **safe to run while the system is in use**, the snapshot is transactionally consistent even mid-write, and if the database is encrypted at rest the backup is written encrypted with the same key.
3. Click **Create backup** to confirm. A "Backup created" toast appears and the list refreshes. The creation is audit-logged with filename and size.

Backups older than the retention window (14 days by default, configurable by IT) are pruned automatically after each successful backup. Note what is deliberately absent: **there is no Restore button.** As the panel states, restores must be performed out-of-band by an operator with the server stopped, a one-click restore would silently discard every record written since the snapshot, including audit rows, and would break the audit hash chain.

#### Data Erasure (GDPR right to be forgotten)

The **Data Erasure** card permanently anonymizes one patient and deletes all of their clinical records, vitals, medications, notes, documents, messages, appointments, risk scores, snapshots and related data.

1. Type the patient's ID (e.g. `P001`) into the field and click **Erase patient**.
2. A typed-confirmation dialog opens. You must type the exact phrase **ERASE {patient ID}** before the red **Erase patient** button arms. The dialog restates the consequences: irreversible, audit trail preserved.
3. On success you see "Patient P001 erased, N clinical record(s) removed."

Guardrails: erasure is **refused for a currently admitted patient** (discharge or transfer first, erasing a live inpatient's record would be a clinical-safety hazard). The audit trail is deliberately *not* deleted; a single audit row recording the erasure (containing only the patient ID, no clinical data) remains so the fact of erasure stays provable. The operation is idempotent, repeating it on an already-erased patient reports "already erased" and changes nothing.

### 13.9 Revenue integrity and coding review

The **Revenue** tab in the main sidebar (available to doctors, nurses and admins, it is documentation-derived, so it follows documentation-read access) hosts the **Revenue Integrity** panel: AI medical coding and clinical documentation improvement (CDI) review for one patient at a time.

1. Enter a **Patient ID** and click **Analyze** (when opened from a patient context, the ID is pre-filled).
2. The headline banner shows **Captured today** → **Potential with CDI**, with the difference as a **Capture opportunity** uplift figure.
3. **Suggested codes** lists ICD-10/SNOMED candidates as code chips, each with the coding system, a plain-language display, the documentation **evidence** it was derived from, and a **confidence** percentage bar.
4. **CDI gap flags** lists documentation gaps (for example, a non-specific diagnosis that could be documented to higher specificity), each with a rationale and an estimated dollar impact.

Read the banner under the headline before acting on any number: *"Illustrative estimate … Not real payer rates; a coder/CDI specialist must confirm before billing."* Every dollar figure is computed from a stated assumption model with visible caveats and citations. The panel suggests and flags, it never submits codes, and nothing here feeds a billing system directly. Its correct use is as a worklist for your coding/CDI team, not as a billing engine.

### 13.10 Operational metrics for IT

For monitoring, the server exposes a standard Prometheus-format metrics page at `/metrics` on the application port. It reports only coarse operational counts, process uptime, database file size, total patients, total orders, total audit rows, currently active admissions, and the local AI queue depth and active workers, and **never contains patient data**. Because it is PHI-free, it is readable without login by default so a standard monitoring scrape works out of the box; if the port is reachable beyond the host, IT can require a bearer token via server configuration. Watching `orb_llm_queue_depth` is the quickest way to spot an overloaded AI backend before clinicians feel it.

### Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| The **Admin** tab is missing from the sidebar | The signed-in account is not an admin. Check its role on the Users tab from an admin session. |
| A deactivated user reports they can still see data | Their open page is showing already-loaded content; the next server request will be rejected. Have them refresh. |
| Flight Recorder or Trust Center shows a red **Tampered** / **Broken** badge | Treat as a security incident: note the reported row number, preserve the database and recent backups, and investigate filesystem access. The badge means a stored audit row no longer matches its sealed hash, it does not fix itself. |
| **Create backup** fails with an integrity error | The fresh copy failed its integrity check and was discarded; the live database was untouched. Check disk space and health, retry, escalate if it persists. |
| Patient erasure returns "currently admitted" | By design. Discharge or transfer the patient, then erase. |
| Downtime page says no snapshot has been captured | Click **Capture snapshot**, or run **Capture all** from the Command Center to cover every admitted patient. |
| Model Governance shows **none** under Checkpoint | Honest reporting: no trained model file exists for that surface, which fails closed to its deterministic path. Information, not an error. |
| Overview shows parse failures climbing | Open the failure's correlation ID in **Trace** to see where the pipeline stopped; check **Ingestion** for the raw payload status. |

### Safety & limitations

- **The audit chain proves integrity, not omniscience.** It guarantees that recorded events have not been altered or deleted; it cannot record actions taken entirely outside Orb (for example, someone reading a printed downtime sheet).
- **Backups protect data; the UI deliberately cannot restore them.** Every restore is a destructive, operator-led procedure with the server stopped. Plan restores; never improvise them.
- **Patient erasure is irreversible** and intentionally hard to trigger: typed confirmation, an admitted-patient block, and a preserved audit record of the erasure itself. There is no undo.
- **Model governance statements are the ground truth for AI trust.** If the registry says a model is not validated or its checkpoint is absent, no output from that surface should be treated as validated decision support, and Orb will not present it as such.
- **Revenue Integrity figures are illustrative.** They use a stated assumption model, not payer contracts. A qualified coder or CDI specialist must confirm every code before billing.
- **The Trust Center reports; it does not enforce.** An amber MFA control does not block logins, pair the report with policy (mandate MFA for privileged roles, review the Users tab for **Off** chips on admin accounts).
- **Downtime snapshots are only as fresh as their capture time.** The Downtime Readiness KPI exists precisely so stale snapshots are visible before you need them. The AI stack plays no part in downtime views, by design, nothing on that page depends on a model being up.
