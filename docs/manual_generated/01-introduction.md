## 1. Welcome to Orb Hospital OS

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

Orb is a hospital operating system: a single application through which clinical staff monitor patients in real time, message each other, place orders, document encounters by voice, manage the pharmacy, run the operating theatre schedule, and let a locally hosted medical AI assist with all of it. This chapter explains what Orb is, why it is built the way it is, what each module does, what its AI can and cannot be trusted with, and how the rest of this manual is laid out.

### 1.1 What Orb is

Two words define Orb's design: **local-first** and **AI-native**.

**Local-first** means the entire system, the application, the database, and every AI model, runs on a single machine inside your hospital. There is no cloud service behind it. When a nurse opens the patient board, when a doctor asks the AI assistant a question, when a pharmacist verifies a medication, the request travels across your local network to a server in your building and comes straight back. Nothing about a patient is transmitted to any outside company, and the system keeps working when the internet connection does not.

**AI-native** means artificial intelligence is not a bolt-on chatbot in the corner; it is woven through the clinical workflow. The same locally hosted medical language model (MedGemma, a 27-billion-parameter model with vision capability) powers the Sage assistant, the Lens image reader, Scribe's note drafting, the Bridge patient portal's plain-language answers, and the Revenue module's coding suggestions. A smaller companion model handles quiet background work, classifying messages, extracting structure from conversations, so the main model stays responsive for clinicians. Speech recognition (Whisper large-v3) and the semantic search that grounds Sage's answers in clinical guidelines (BGE-M3 embeddings) also run entirely on the same machine.

You use Orb through an ordinary web browser. Staff sign in with an Employee ID and password on the **Clinician Login** screen; patients use the separate **Patient Access** screen with a Patient ID. After signing in, clinicians land in a shell with a vertical sidebar of module tabs on the left, a header across the top, and the active module filling the rest of the screen. Orb is also an installable web app: on a ward tablet it can be added to the home screen, and its offline mode keeps the shell loading and queues the three kinds of write that are safe to apply without a server answer, Relay messages, free-text clinical notes, and bedside medication administration, replaying them automatically when connectivity returns (the header shows an "Offline" badge, a pending count, and a "Synced" confirmation). **Everything else fails visibly rather than queueing, and that is deliberate.** New medication orders, CPOE orders, observation sets, admissions and discharges all require a live connection, because each of them can come back from the server carrying a safety response, a severe drug-interaction block, an allergy interlock, a computed NEWS2 escalation, that a clinician cannot acknowledge while offline. A queued order that silently applies the moment the network returns is a worse failure than an order that refuses now.

### 1.2 The privacy thesis: PHI never leaves the building

Most AI products for healthcare send patient data to a cloud provider for processing. Orb's founding argument is that this is an unnecessary risk. Because modern desktop hardware can now run capable medical language models locally, Orb performs **all inference on premises**: the language model, the vision model, the speech-to-text engine, and the document-search index all live on the Orb server itself. Protected Health Information (PHI), anything that identifies a patient, is never uploaded anywhere.

This stance is enforced in the software, not just promised:

- **There is no cloud AI lane at all.** This is stronger than "off by default", and it is worth stating precisely because it is the claim a reviewer will check. Orb ships exactly one chat provider, the local one, and there is no cloud AI SDK in its dependencies at all, so there is no hosted lane to enable, no provider to configure and no switch to leave in the wrong position. Every clinical AI request runs on the machine in your building, and the audit record for each one is written with its lane recorded as local because that is the only value it can take. (An optional hosted lane existed in an earlier version; it was removed entirely in July 2026, and this paragraph previously described its controls. If you are auditing against an older copy of this manual, that is why.)
- **No cloud speech recognition, ever.** Browser-based cloud dictation streams your voice to an outside service, so Orb does not support it and there is no setting that turns it on. Voice input uses only the local speech engine (Whisper) on the Orb server, which is installed with Orb and never downloads anything while running, and spoken answers use only voices that run on your own device.
- **Self-contained by design.** Fonts and interface assets are bundled with the application rather than fetched from the internet, so Orb renders correctly on an offline, locked-down hospital network.
- **Accountability alongside autonomy.** Every change anyone makes is written to a tamper-evident audit log in which each entry is cryptographically chained to the previous one, deleting or editing history breaks the chain visibly. Access to patient media (images, audio) is authenticated, scoped to the patient, and refused outright if the audit record cannot be written first.

### 1.3 Who uses Orb

Orb has five roles, and the sidebar adapts to each. Access is enforced on the server, not just hidden in the interface.

| Role | Typical user | Sidebar tabs they see |
|---|---|---|
| Physician | Doctors | Patients, Orders, Messages, Sage, Scribe, Lens, Forecast, Command, Simulator, Revenue, Appointments (doctors in a surgical department see Surgical Suite in place of Appointments) |
| Nurse | Ward nurses | Patients, Orders, Messages, Sage, Scribe, Lens, Forecast, Revenue, Appointments |
| Pharmacist | Pharmacy staff | Pharmacy, Verify Queue, Sage |
| Administrator | Governance and operations | Patients, Messages, Sage, Forecast, Command, Simulator, Revenue, Appointments, Admin |
| Patient | The patient, at the bedside during their admission (the login closes at the end of the discharge day) | The Bridge portal only (a separate, simplified interface) |

Demonstration installations are seeded with sample accounts so you can explore every role (all with the password "password" unless noted): **DOC001** Dr. Meera Sharma (physician), **NUR001** Priya Reddy (nurse), **PHA001** Deepa Joshi (pharmacist), and **ADM001** Rajesh Kumar (administrator). The demo patient portal account is **P001** with the password "patient123". On demo builds, the login screen shows Quick access chips that fill these in with one click; production builds do not include them.

Below the sidebar tabs, every clinician sees an **Admit Patient** button (pharmacists excepted), a user card showing their name, role, and department with a lock icon labelled **Security & MFA** for enrolling in multi-factor authentication, and a **Sign Out** button. The header on every screen carries the current module's name and subtitle, the offline/sync status badge, a clock, the **Pulse** season panel, a critical-alert notification bell, a language toggle that switches the navigation chrome and sign-in screen, not the clinical content, between English and Hindi, and a theme toggle that cycles Dark → Light → Night Shift. Pressing Cmd-K (Ctrl-K on other keyboards) opens a command palette for jumping straight to any *currently admitted* patient from anywhere.

### 1.4 The module map

Each sidebar tab opens one module. Orb's internal name for a module is a "pillar", you will see both the tab label (for example, "Patients") and the pillar name (for example, "Orb Vigil") in the header. Here is what each one is for; every module gets its own chapter later in this manual.

**Vigil, the "Patients" tab.** The live patient board and the heart of clinical monitoring. Vigil streams vital signs in real time, computes NEWS2 early-warning scores (including the Scale 2 oxygen-saturation adjustment for COPD patients), classifies each patient's trajectory as improving, stable, or deteriorating, and raises deterioration alerts. Clicking a patient opens their full record: vitals with sparkline trends, notes, risk history, imaging analyses, and medications. NEWS2 scores of 7 or higher automatically open a time-tracked Sepsis Six care bundle.

**Orders, the "Orders" tab.** Computerized provider order entry (CPOE) for laboratory tests, imaging, and procedures. Orders are validated against standard clinical terminologies (LOINC, SNOMED, ICD-10), and medication orders pass through order-time allergy and drug–drug interaction screening before they reach the pharmacy.

**Relay, the "Messages" tab.** Internal clinical messaging: direct messages, group channels, and a dedicated case room per patient. Automated bot alerts post into Relay when a patient deteriorates, so the conversation and the clinical event live in the same place. Message delivery is instant over Orb's real-time channel.

**Sage, the "Sage" tab.** The clinical AI assistant. You ask a question in plain language; Sage retrieves relevant passages from the locally indexed clinical guideline corpus and live data on the patient under discussion, then drafts an answer grounded in that evidence, streaming it word by word with citations. Sage also runs a proactive background sweep that surfaces insights without being asked, and a floating Sage assistant is available from any tab. Its guardrails are described in section 1.5.

**Scribe, the "Scribe" tab.** Voice-to-text clinical documentation. Scribe transcribes dictation or ambient conversation using the local Whisper engine, applies a second-pass correction of medical vocabulary (**not** an RxNorm dictionary check, the RxNorm vocabulary is licence-gated and is not shipped with Orb), and structures the result into standard formats such as SOAP notes, discharge summaries, and handoffs. Every note is a draft until a physician reviews and signs it, nurses can create drafts but cannot sign. Transcription supports English, Hindi, and automatic detection for code-switched speech.

**Lens, the "Lens" tab.** Medical image analysis. Upload a photograph or export of an ECG, X-ray, CT slice, MRI, or clinical photo, and the vision model streams back a structured draft read, Findings, Impression, Recommendations, for your review. Lens is deliberately the most restricted AI feature in Orb: every output carries a non-removable disclaimer, filing an analysis to a patient's record requires an explicit clinician review-and-edit step, and the module is disabled entirely in production deployments unless an administrator opts in. It is a reference aid, never a diagnostic device.

**Forecast, the "Forecast" tab.** Predictive intelligence for patient flow. A neural network (a multi-task LSTM with temporal attention) trained on patient time-series estimates expected length of stay, discharge probability, and acuity trajectory, alongside ward-level census and demand forecasts. When the neural model is unavailable, Forecast falls back to transparent heuristic estimates rather than failing.

**Command, the "Command" tab.** The house-wide operations command center: hospital-level occupancy, admissions and discharge flow, staffing pressure, and operational key indicators on one screen, intended for charge roles and administrators watching the whole building rather than one ward.

**Simulator, the "Simulator" tab.** The what-if capacity simulator. Adjust assumptions, admission surges, staffing levels, bed availability, and see projected strain on the hospital before it happens. Useful for surge planning and staffing decisions; it changes nothing in the live system.

**Revenue, the "Revenue" tab.** Revenue-integrity and clinical documentation improvement. The AI reviews clinical documentation and suggests ICD-10 coding, flagging likely under- or mis-coded encounters for human review. Suggestions are proposals for a coder or clinician to accept, not automatic changes.

**Appointments, the "Appointments" tab.** Scheduled clinical follow-ups and reviews for your patients.

**Surgical Suite, the "Surgical Suite" tab.** Shown to doctors in surgical departments. A live theatre board with the weekly surgical calendar, pre-operative planning, the WHO Surgical Safety Checklist (sign-in, time-out, sign-out), team alerts, and emergency intake.

**Pharmacy (Helix), the "Pharmacy" tab.** The pharmacist's home: formulary and inventory management, drug-interaction screening, and phonetic reconciliation of free-text drug names to the hospital's own loaded medication catalogue (the RxNorm release itself is licence-gated and is not shipped with Orb).

**Verify Queue, the "Verify Queue" tab.** The pharmacist verification queue. Medications that entered the record through AI extraction (for example, from a Scribe note) are explicitly marked unverified and held here until a pharmacist reviews them. Elsewhere in Orb, unverified medications are visibly flagged so no one mistakes an AI extraction for a pharmacist-checked order.

**Admin, the "Admin" tab.** The governance console, visible to administrators only. Its sub-tabs cover a system overview, the audit log, the **Flight Recorder** (a forensic timeline that replays everything that happened to a patient or hospital-wide while continuously verifying the audit chain's integrity, with a green "verified" or red "tampered" badge), compliance posture, impact metrics, AI model governance, security, the trust center, the data-ingestion monitor, and alert operations.

**Pulse, the heartbeat-line icon in the header, on every screen.** Season and conditions: what usually rises in your hospital's state at this time of year (dengue and malaria in the monsoon, for example) and what is coming up in the next 60 days, read from a seasonal calendar built into Orb. Pulse makes no internet connection. Click the icon to open the panel.

**Bridge, the patient portal.** A separate, simplified interface for patients, reached through the Patient Access login. Bridge translates the clinical record into plain, reassuring language, shows medications and reports, generates patient-friendly discharge summaries and timelines, and answers patient questions in non-technical terms without ever dispensing direct medical advice. Discharged patients can also be given a time-limited access link that expires automatically and locks after repeated failed attempts.

**Two further surfaces** sit outside the sidebar: the **I-PASS Handoff** page, which drafts structured shift-change handoffs in the I-PASS format (Illness severity, Patient summary, Action list, Situation awareness, Synthesis) for the outgoing clinician to edit and confirm; and **Downtime Mode**, a read-only patient snapshot page fed by hourly automatic snapshots of every admitted patient, so the ward retains a picture of its patients even if the network or an upstream EHR fails.

### 1.5 What the AI can and cannot do

Orb's position, stated plainly in its own documentation, is that its models are **not fine-tuned or clinically validated, outputs are decision support, not ground truth.** Everything the AI produces is a draft for a qualified human to accept, edit, or discard. Concretely:

**What it can do.** Sage can answer clinical questions grounded in the local guideline corpus and the selected patient's live data, citing what it retrieved and stating its confidence. Scribe can turn speech into a structured draft note. Lens can produce a first-pass structured description of an image. Forecast can estimate length of stay and discharge probability. Bridge can rephrase the record for a patient. The Revenue module can propose codes. The ambient model can classify and summarize in the background.

**What it will not do, by design.** The guardrails are deterministic code, not polite requests to the model:

- **Sage cannot modify the chart.** It is read-only decision support. If you ask it to "add paracetamol to the patient's medications," a mutation-detection gate recognizes the request, the interface states explicitly that nothing was done, and an action card requires an explicit clinician click to perform anything real. A hallucinated "done, I've added it" cannot stand, because the refusal is enforced outside the model.
- **Sage refuses unsafe and manipulative prompts.** Queries matching harm patterns or prompt-injection attempts (such as "ignore previous instructions") receive a canned refusal before the model is ever consulted.
- **High-risk topics escalate deterministically.** Certain queries always trigger an escalation banner and a detailed display mode regardless of what the model itself says.
- **Retrieval refuses to bluff.** If nothing in the guideline corpus is genuinely relevant to your question, Sage says so rather than citing a tangential document as authoritative, and each citation is labelled by source tier (local policy, local formulary, external guideline).
- **Nothing files itself.** Scribe notes require a physician's signature. Lens analyses require a review-and-edit step and carry a permanent disclaimer. AI-extracted medications are quarantined as unverified until a pharmacist clears them in the Verify Queue.
- **Risk scores can escalate but not reassure.** Vigil's neural deterioration overlay may raise a patient's computed risk above the NEWS2 baseline but is never allowed to lower it, the validated score is the floor.

**What it cannot promise.** The model can be wrong, confidently. It can mishear a drug name despite correction layers, misread an image, or produce a plausible answer that does not apply to your patient. Latency also varies: the first AI request after a quiet period may take noticeably longer while the model loads, and simultaneous heavy use (a Lens analysis during several Sage conversations) is queued rather than parallelized, because one shared model serves the whole hospital. Treat every AI output exactly as you would a suggestion from a well-read student: often useful, always requiring your judgement, never a substitute for it.

### 1.6 Hardware and deployment at a glance

Orb is designed to run on a single Apple Silicon Mac, a Mac Studio–class machine for a department, or a well-equipped MacBook Pro for evaluation. The main 27-billion-parameter model occupies roughly 17 GB of unified memory on its own, so **64 GB of unified memory is the recommended configuration**; smaller machines can run Orb with reduced model tiers in the development profile. The server process listens on port 8000 and serves both the application and its data interface; clinicians simply browse to the Orb address on the hospital network. All persistence is a single database file on that machine, there is no external database to operate. Administrators should note that the server binds only to the local machine by default and must be deliberately configured (with TLS) before being exposed to the wider network.

### 1.7 An honest word on compliance

Orb's documentation is deliberately candid here, and this manual repeats it: **local-first removes the cloud data-egress risk, but it does not by itself make a deployment HIPAA, GDPR, or DPDP compliant.** What is implemented today: strong password hashing, token-based sessions with server-side revocation, role-based access control on every endpoint, an append-only hash-chained audit log with a middleware safety net that records every state-changing request (that net records method, path and outcome but **not the acting user** on browser sessions, actor attribution comes from the per-route audit events), optional multi-factor authentication, optional encryption at rest for the database and uploaded media, and opt-in TLS for data in transit. What is not yet in place and is on the roadmap: backup and disaster recovery, data-subject-rights and erasure workflows, a retention policy, a data-protection impact assessment, and business-associate agreements. Until those exist, **Orb should not be deployed against real patient data.** Demonstration and pilot environments use the seeded synthetic dataset described above.

### 1.8 How this manual is organized

The manual follows the way you will actually meet the system. This chapter and the next cover orientation: signing in, the shell, navigation, themes, languages, and offline behavior. The chapters after that take the modules one at a time, in roughly the order they appear in the sidebar, the patient board and monitoring first, then ordering and messaging, then the AI surfaces (Sage, Scribe, Lens), then forecasting and operations (Forecast, Command, Simulator, Revenue), then the pharmacy pair, the Surgical Suite, and admissions, handoff, and discharge workflows. Later chapters cover the Bridge patient portal, the Admin console and audit tooling, and downtime and recovery procedures. Every module chapter follows the same shape: what the module is for, how to open it, a tour of every control, step-by-step common workflows, what the AI contributes and where it is limited, and a troubleshooting section. You do not need to read the manual linearly, each chapter stands alone, but every reader should finish this chapter and the orientation chapter before their first shift on the system.

### Troubleshooting

| Symptom | Likely cause and what to do |
|---|---|
| The Orb page will not load | The Orb server is not running or you have the wrong address. Confirm the address with your administrator; on the server itself, Orb answers on port 8000. |
| "Sign In" fails repeatedly, then stops responding | After 5 failed attempts within 15 minutes from the same location, login is temporarily locked to slow password guessing. Wait 15 minutes and try again, or contact your administrator. |
| Signed in, but a tab you expect is missing | Sidebar tabs depend on your role and department (see the table in 1.3). If your role is wrong, an administrator must correct your account. |
| The first Sage/Lens/Scribe request is very slow | The model is loading into memory after a quiet period. Subsequent requests are much faster. If it never completes, the AI runtime on the server may be stopped, contact your administrator. |
| An "Offline" badge appears in the header | Your device lost its network connection. Orb keeps working from cache and queues eligible writes; when the badge changes to "Synced," they have been delivered. If it instead reports that queued actions failed to sync, those actions did **not** go through, re-enter them. |
| You were logged out without warning | Sessions expire after 24 hours, and certain security events (password change, administrator revocation) end all of a user's sessions immediately. Sign in again. |
| A panel shows "This panel hit an error" | A single module failed without affecting the rest of the shell. Click **Reload panel**. If Orb was just updated, you may briefly see "Updating to the latest version...", this is routine. |

### Safety & limitations

- Orb's AI features are **clinical decision support**. They do not diagnose, prescribe, or treat, and no Orb output replaces the judgement of a licensed clinician.
- The underlying models are **not clinically validated or fine-tuned for your institution**. Lens in particular ships as an investigational reference and is disabled in production unless an administrator explicitly accepts that posture.
- Guardrails reduce, but cannot eliminate, model error. Always verify drug names, doses, laterality, and patient identity in AI-drafted content before signing.
- The demonstration dataset is synthetic. Simulated data generation is disabled by default in production so fabricated clinical data can never be written into a real deployment.
- Orb is not yet certified for regulated PHI. Review section 1.7 before any deployment decision involving real patient data.
