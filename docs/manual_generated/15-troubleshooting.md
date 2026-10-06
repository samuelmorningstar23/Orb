## 15. Operations, Troubleshooting & FAQ

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![Revenue Integrity analyses coding and documentation-improvement opportunities for a patient.](assets/manual/revenue-integrity.png)

*Revenue Integrity analyses coding and documentation-improvement opportunities for a patient.*

This chapter is the reference you reach for when something is not behaving: the screen says your session expired, Sage answers with an apology, a panel says it is updating, or you need to reset a colleague's password, take a backup, or restore the demo ward to a clean state. The first half is written for whoever operates the Orb machine day to day; the second half is a frequently-asked-questions list and a glossary of the abbreviations used throughout this manual.

The single most useful fact for troubleshooting Orb: **everything runs on one machine in your facility**. When something misbehaves, the cause is almost always local, a service that is not running, a model that is not loaded yet, or a browser tab holding an old version of the interface, and the fix is almost always quick.

### 15.1 Starting and stopping Orb

Orb starts with a single command. On the machine that hosts Orb, open the Terminal application, change into the Orb folder, and run:

```
./run-orb.sh
```

Then open a browser to **http://localhost:8000** (or the address your administrator has published for the machine). The startup script, in order:

1. **Checks that setup has been completed.** If not, it stops with a message telling you to run `./setup-mac.sh` first, the one-time task that installs dependencies, pulls the AI models (roughly 21 GB of downloads), and seeds the demo database.
2. **Starts the Ollama AI engine** if it is installed but not already running. Ollama is the local service that hosts the language models; if it is missing entirely, Orb still starts, see section 15.5.
3. **Starts the Orb server**, which serves the application interface and all data services on port 8000.
4. **Prints a sign-in reminder**, on a demo installation, `Sign in: DOC001 / password`.

To **stop** Orb, return to that Terminal window and press **Ctrl-C**. The script shuts down everything it started. Closing the Terminal window has the same effect, Orb does not keep running in the background after the window is gone, so on a shared ward machine, leave the window open (minimized is fine).

There is also a developer mode, `./run-orb.sh --dev`, which serves a live-reloading copy of the interface on port 5173. Clinical users should ignore it; it exists for people modifying the software.

#### The first start takes longer

The very first time Orb starts after setup, it builds Sage's clinical knowledge index. You will see the message `Initializing clinical knowledge base (chunking & embedding...)` in the Terminal, and it can sit there for several minutes: Orb downloads the BGE-M3 embedding model (about 2.2 GB) from the internet, then reads and indexes the bundled clinical guidelines. This happens once; later starts skip it. If the download fails (for example, no internet connection at first boot), Orb still runs, but Sage's guideline retrieval returns nothing until the index is built on a later start. Administrators can also skip the index build deliberately with the `ORB_SKIP_KNOWLEDGE_BASE` setting.

#### A quick health check

If you are unsure whether the server is up at all, visit **http://localhost:8000/api/health** in a browser. A running server replies instantly with a short "status: ok" message. No reply at all means the server is not running, start it as above.

### 15.2 The AI models: what runs on this machine

Orb's intelligence is entirely local. Nothing you type, dictate, or upload is sent to a cloud AI service. The models involved:

| Model | Role in Orb | Approximate size |
|---|---|---|
| MedGemma 27B (multimodal) | The main clinical model: Sage answers, Lens image analysis, Scribe note drafting, Bridge patient answers | ~17 GB |
| MedGemma 4B | The lighter "ambient" tier: fast background tasks such as Sage's ambient intelligence | ~3.3 GB |
| Gemma 3 1B | Small utility model pulled at setup | ~1 GB |
| BGE-M3 | The embedding model behind Sage's guideline search (RAG) | ~2.2 GB |
| Whisper large-v3 (optional) | The local speech-recognition engine for Scribe, Sage voice mode and Ward Round dictation. Installed with the appliance and never downloaded at runtime; where it is not installed, voice features say so up front and typing still works | varies |

Two operational consequences follow from these sizes:

**Memory.** The 27B model needs a machine with generous unified memory, it is intended for the 48 GB-and-up Apple Silicon machines. Smaller machines (for example an 8 GB Mac mini) run a reduced profile that uses the 4B model only; on such machines the 27B model must not be installed, because it will not load.

**Warm-up.** A model must be loaded into memory before it can answer, and loading 17 GB takes time. Orb manages this for you:

- **At startup**, Orb pre-loads both language-model tiers into memory so the first real question is fast, not cold.
- **Every five minutes**, a background heartbeat pings the models so they stay loaded. The heartbeat runs at the lowest possible priority and skips itself entirely whenever real work is in flight, a model that is actively answering is warm by definition, and a keep-warm ping never queues ahead of a clinician's request.
- The keep-loaded window defaults to **one hour** and is configurable (the `ORB_OLLAMA_KEEP_ALIVE` setting; a value of `-1` means "never unload"). If the Ollama engine is restarted, the heartbeat re-warms the models automatically.

**What latency to expect.** With the models warm, the normal state, Sage begins streaming its answer within a few seconds. If you catch the system cold (immediately after starting Orb, or right after an Ollama restart), the first response can take noticeably longer while the 27B model loads; subsequent responses return to normal speed. Also note that **only one AI generation runs at a time**: the machine has one GPU, and Orb deliberately queues AI work so that jobs do not degrade each other. If a colleague's Lens analysis is mid-stream, your Sage question waits its turn, a short delay, not a fault.

### 15.3 "Session expired": what it means and what to do

At some point you will be working and a dialog will cover the screen: **"Session expired"**, with the text *"For your security, your session has ended. Re-enter your password to continue as [your name]."* Nothing is lost, the dialog exists precisely so you can re-authenticate **without losing in-progress work**. Type your password and press **Continue**; the screen unlocks exactly where you were. Or press **Sign out** to return to the login page.

Common reasons the dialog appears:

- **Your sign-in is more than 24 hours old.** Sessions expire after 24 hours by design.
- **The server was restarted** after a database reseed or a configuration change. Reseeding recreates the user accounts, which invalidates every previously issued token; the browser's next request is rejected and the dialog appears. Re-enter your password and carry on.
- **An administrator reset your password or revoked your sessions**, which signs the account out everywhere.
- **Your account was deactivated.** Password re-entry will not succeed; contact your administrator.

If your account uses two-factor authentication, the dialog cannot complete a password-only re-entry. It will tell you: *"Your account requires MFA, sign out and log in again with your authenticator code."* Press **Sign out** and log in normally with your 6-digit code.

A near-identical dialog titled **"Screen locked"** appears after **15 minutes of inactivity** on a clinician workstation: *"This workstation locked after a period of inactivity."* The remedy is the same, password, **Continue**. In both cases the dialog is deliberately blocking: the Escape key does nothing, and the only ways out are re-authentication or an explicit sign-out. This is a patient-privacy feature, not a malfunction.

### 15.4 "Updating to the latest version..."

When your administrator installs an updated build of Orb, any browser tab that was already open still holds the previous version of the interface. The moment you switch to a panel whose files changed in the update, that panel briefly shows:

> **Updating to the latest version...**

and the page then reloads itself **once**, automatically. This is routine, the reload fetches the new interface, and you land back where you were (you may need to re-enter your password if the update also restarted the server; see 15.3).

If a panel instead shows **"This panel hit an error and couldn't be shown."** with a **Reload panel** button, click the button. If the error returns immediately, refresh the whole browser page. If it *still* returns, the server itself is likely down or mid-restart, check the health address from 15.1 and, if needed, restart Orb.

### 15.5 When Sage, Lens, or Scribe error: the AI engine is down

Orb boots and runs perfectly well without its AI engine, patients, vitals, orders, messaging, and pharmacy all keep working. What stops working is everything that generates text or reads images: **Sage** answers, **Lens** analyses, **Bridge** patient Q&A, and **Scribe** drafting all return errors until the engine is back. The typical symptom is Sage replying *"Sorry, I encountered an error. Please try again."* to every question, or Lens failing every analysis.

Work through this list:

1. **Is Ollama running?** On the host machine, check for the Ollama application. The simplest recovery is to stop Orb (Ctrl-C) and run `./run-orb.sh` again, the script starts Ollama automatically if it is installed. If Ollama was never installed on this machine, it must be installed once from ollama.com (macOS requires the graphical installer).
2. **Are the models actually installed?** If Ollama is up but a configured model is not pulled, Orb logs a loud, specific error at startup naming the missing model and suggesting the fix (`ollama list` shows what is installed). AI features using the missing model fail until it is pulled or the configuration points at an installed model.
3. **Is it merely busy?** One generation at a time, remember. A long Lens analysis or a batch of background scoring can make Sage feel briefly unresponsive. Busy is not broken, wait a few seconds and retry.
4. **Sage answers, but cites no guidelines?** The knowledge index was never built (usually a failed first-boot download, see 15.1). Restart Orb while the machine has internet access so the index can build.

One subtle failure deserves its own warning: **the wrong model variant**. The correct models are *vision-capable*; a well-known community build of the 27B model is text-only, and if a machine is misconfigured to use it, Lens does not fail, it accepts your image and returns confident prose about nothing. The setup script checks for vision capability after pulling models and warns if the check fails. If Lens output ever seems disconnected from the image you uploaded, stop using it and have your administrator verify the installed model.

### 15.6 Passwords, lockouts, and resets

**Changing your own password.** Any signed-in user can change their own password (you must supply your current password). Doing so revokes **all** of your sessions, including the one you are in, you will sign in again with the new password. Passwords must be at least the minimum length the form enforces.

**Resetting someone else's password (administrators).** Open the **Command** tab (the admin dashboard), go to the **Users** tab, find the person in the list, and click **Reset password** on their row. The "Reset password" dialog lets you type a new password or click **Generate** to create a strong one, with a copy button to hand it to the user securely. Confirming the reset sets the new password *and revokes every existing session for that account*, the user is signed out everywhere and must sign in with the new password. The action is recorded in the audit log.

**Lockouts.** Repeated failed sign-in attempts temporarily lock the account, and repeated attempts from one network address are rate-limited (Chapter 2 covers the sign-in flow in detail). Locks clear on their own after the cool-down; an administrator password reset also gives a clean start.

**Two-factor authentication.** Enrollment and disabling live behind **Security & MFA** in the sidebar footer. Disabling MFA requires both your current password and a live authenticator code. If a user has lost their authenticator entirely, an administrator resolves it from the Users panel.

### 15.7 Demo data and reseeding

A demonstration installation ships with a fully populated synthetic hospital: roughly 13 staff accounts, 30 admitted patients, 37 message channels, 99 medication orders, 720 vitals readings, 90 lab results, 60 clinical notes, 5 surgeries, and a pharmacy catalog of 14 drugs with inventory lots. In demo mode, simulation bots stream fresh synthetic vitals continuously so the wards look alive.

The seeded demo accounts (password `password` for all staff):

| ID | Name | Role |
|---|---|---|
| DOC001 | Dr. Meera Sharma | Doctor |
| NUR001 | Priya Reddy | Nurse |
| PHA001 | Deepa Joshi | Pharmacist |
| ADM001 | Rajesh Kumar | Administrator |
| P001 | (patient portal) | Patient, password `patient123` |

**Reseeding** restores this clean state after a demo or training session has scrambled the data. It is an operator action run on the host machine: the seeding script wipes all patient-related tables, reloads the clinical and pharmacy demo data, randomizes admission dates so the ward looks current, relinks the P001 patient login, backfills labs, notes, and surgeries, and finally **re-seals the audit hash chain** so the freshly seeded database verifies as tamper-free. Two things to know:

- **Everyone is signed out.** Reseeding recreates user accounts, so every open session hits the "Session expired" dialog on its next request.
- **Seed on a warm database.** The pharmacy tables are created at first application startup; seeding a brand-new database that has never booted can silently skip the pharmacy catalog, leaving Helix empty. The setup script handles this correctly; if the pharmacy ever appears empty after a reseed, re-run the pharmacy seed.

Demo content is gated by the `ORB_DEMO_MODE` setting so fabricated data can never leak into a real deployment: it defaults **on** in the development profile and **off** in production. Never run the demo profile or simulation bots against real patient data.

### 15.8 Backups

Administrators take backups from inside Orb. Open the admin dashboard and choose the **Data & Backups** tab. The **Database Backups** section lists existing snapshots (filename, size, date) and offers a create button. Clicking it opens the confirmation dialog:

> **Create backup?** A consistent snapshot of the live database will be written to the server's backups directory and integrity-checked. This is safe to run while the system is in use.

Press **Create backup**. The snapshot is taken with the database's online-backup mechanism, so it is transactionally consistent even while clinicians are actively writing; it is then integrity-checked before being accepted, and the action is recorded in the audit log. If the live database is encrypted at rest, the backup is written encrypted with the same key, no plaintext patient data touches disk. Backups older than the retention window (**14 days** by default; configurable with `ORB_BACKUP_RETAIN_DAYS`) are pruned automatically after each successful backup.

Three deliberate limitations to understand:

- **There is no restore button.** Restoring a snapshot over the live database silently discards every write made since it was taken, including audit records, so it is intentionally not a one-click action. Restores are performed by an operator with access to the host machine, **with the server stopped**.
- **Backups live on the same machine as the database.** They protect against data corruption and mistakes, not against the loss of the machine itself. Copy backup files to separate media or another machine on a schedule your organization sets.
- **Backups contain patient data.** Treat backup files with the same care as the live database.

### 15.9 Safety & limitations

- **The demo profile is for demonstration machines only.** Before any deployment touches real patient data, the production profile must be enabled, simulation switched off, and the compliance gaps the project openly documents (encryption at rest is optional and key-based; disaster-recovery, retention, and data-protection processes are roadmap items) must be addressed. Local-first design removes the cloud data-egress risk; it does not by itself make a deployment compliant with health-data regulation.
- **AI output is draft decision support, never ground truth.** Sage, Lens, Scribe, and Forecast produce material for clinician review. The models are not clinically validated diagnostic devices, and none of them can place orders or change records on their own.
- **Errors are loud, not hidden.** Orb prefers specific, visible errors (a missing model is named in the log; a failed integrity check rejects the backup) over silent degradation. If something looks wrong, the Terminal window on the host machine is the first place the cause will be written.

### 15.10 Frequently asked questions

**Does Orb need an internet connection?**
Only during installation, before the appliance joins the hospital network (software packages and model files). After that Orb never connects to the internet: it is strictly offline, and a missing model is reported, not downloaded.

**Is any patient data sent to a cloud AI?**
No, all models run on the machine in your facility, including speech recognition. Orb does not use the browser's own cloud speech recognizer and cannot be set to, and spoken answers use only voices that run on your device.

**Why is the first AI answer after startup slow?**
The model was still loading into memory. Orb pre-warms its models at startup and keeps them warm; if you beat the warm-up, the first answer pays the loading cost. Later answers are fast.

**Sage says "Sorry, I encountered an error. Please try again.", every time. What now?**
The AI engine is down or a model is missing. Work through section 15.5, starting with a restart of Orb.

**Can two people use the AI at the same time?**
They can ask at the same time, but generations run one at a time on the single GPU, in priority order. A short queueing delay under load is normal.

**I closed the Terminal window on the host machine. Is Orb still running?**
No, closing the window (or pressing Ctrl-C in it) stops Orb. Start it again with `./run-orb.sh`, and leave the window open on machines that host the system.

**The machine rebooted. Does Orb come back by itself?**
Not automatically. Log in to the host machine and run `./run-orb.sh` again.

**I forgot my password.**
Ask an administrator to use **Reset password** in the Users panel. You get a new password and are signed out of all existing sessions.

**Why was I suddenly asked for my password mid-shift?**
Either 15 idle minutes passed ("Screen locked"), your 24-hour session expired, or the server restarted ("Session expired"). Enter your password and continue, nothing is lost.

**Why does the login page show one-click demo account chips?**
You are on a demonstration or development installation. Production builds never show working credentials on the login page.

**A panel said "Updating to the latest version..." and the page reloaded. Did I break something?**
No, that is Orb refreshing a browser tab that predated a software update. It reloads once and carries on.

**Lens accepted my image but its report reads like it never looked at it.**
Possible wrong model variant (a text-only build masquerading as the multimodal model). Stop relying on the output and have your administrator verify the installed models.

**Why is the pharmacy (Helix) empty on a fresh installation?**
A seeding pitfall on a never-booted database: the pharmacy tables did not exist yet when the seed ran. Re-run the pharmacy seed (the setup script does this correctly).

**Can I restore a backup from the admin screen?**
No, by design. Restores discard everything written since the snapshot and must be done by an operator on the host machine with the server stopped.

**How long are backups kept?**
14 days by default; older snapshots are pruned automatically after each new backup. The retention window is configurable.

**Does Orb keep working if the upstream hospital EHR is unreachable?**
Yes, Orb includes a read-only downtime mode that preserves continuity of care from its latest snapshots when upstream systems are unavailable.

**Is Orb's AI a medical device? Can I act on its output directly?**
No. Every AI output in Orb, Sage answers, Lens observations, Scribe drafts, Forecast predictions, is decision support for a qualified clinician to review, edit, and take responsibility for.

**Who can take backups or manage users?**
Only administrator accounts. Backup creation, user management, and password resets are gated behind admin-only permissions, and every such action is written to the append-only audit log.

### 15.11 Glossary

| Term | Meaning |
|---|---|
| **NEWS2** | National Early Warning Score 2, the vital-signs scoring system Vigil uses to flag deterioration. Scale 2 is its alternative oxygen-saturation scoring for chronic type-2 respiratory failure. |
| **RAG** | Retrieval-Augmented Generation, Sage retrieves relevant passages from the local guideline index and grounds its answer in them, with citations. |
| **SOAP** | Subjective, Objective, Assessment, Plan, a standard clinical note structure Scribe can draft. |
| **I-PASS** | Illness severity, Patient summary, Action list, Situation awareness, Synthesis, the structured shift-handoff format Orb implements. |
| **eMAR** | Electronic Medication Administration Record, what was given, when, and by whom. |
| **CPOE** | Computerized Provider Order Entry, electronic clinical ordering with safety screening at order time. |
| **FHIR** | Fast Healthcare Interoperability Resources, the data-exchange standard Orb uses for ingestion and export. |
| **WAL** | Write-Ahead Log, the database journaling mode Orb runs in; the source of the small companion files beside the database file, which contain patient data too. |
| **JWT** | JSON Web Token, the signed sign-in token your browser holds; expires after 24 hours. |
| **MFA / TOTP** | Multi-factor authentication via time-based one-time passwords, the rotating 6-digit authenticator codes. |
| **LLM** | Large Language Model, the class of AI model (here, MedGemma) that generates text. |
| **Ollama** | The local service that hosts Orb's language and vision models. |
| **MedGemma** | Google's medical model family; Orb uses the 27B multimodal model as its main engine and 4B for ambient tasks. |
| **BGE-M3** | The embedding model behind Sage's semantic guideline search. |
| **Embedding** | A numeric representation of text that enables search by meaning rather than exact wording. |
| **LSTM** | Long Short-Term Memory, the neural network behind Forecast's length-of-stay and trajectory predictions. |
| **PHI** | Protected Health Information, identifiable patient data, which never leaves the local machine. |
| **RBAC** | Role-Based Access Control, permissions attach to roles, which is why different roles see different tabs. |
| **Audit hash chain** | The append-only audit log, each entry cryptographically chained to the previous one so tampering is detectable. |
| **Unified memory** | Apple Silicon's shared CPU/GPU memory pool, why model sizes translate directly into machine memory requirements. |
| **Keep-warm** | Pre-loading AI models at startup and pinging them every five minutes so they stay resident and fast. |
| **Demo mode** | The configuration gate that allows synthetic patients and simulated vitals on demo machines and forbids them in production. |
