# The Orb Technical Manual

> **Audience:** a new engineer (intern level) who knows a little Python and/or JavaScript but nothing about Orb.
> **Goal:** by the time you finish a section, you should understand *what a piece of code does*, *why it exists*, and *what else breaks if you change it*. This manual is meant to be read at the desk while the code is open in another window.
>
> _Generated 2026-06-26. Fact-check pass 2026-08-15, see **§17, Verification Status**, which lists exactly what was re-verified against the code in that pass, what was corrected, and what is still **unverified**. Read §17 before you quote this manual to anyone outside the team._
>
> _This single document is **assembled** from the per-area source files in `docs/manual_sections/`. To update it, edit the relevant `NN_*.md` there and re-run `python3 docs/manual_sections/_assemble.py` (which also rebuilds the table of contents), or edit this file directly._

> ### The rule this manual is held to
>
> **Every claim is traced to `file:line`, and no claim is written that was not verified by reading the code.** Not inferred from another document, not remembered from a previous version, not assumed because the function name says so. Writing this manual the first time found four live event-loop violations no test had caught, precisely because that rule was followed.
>
> The corollary is the part people skip: **where a claim cannot be verified, it is listed as unverified rather than asserted.** A 500-page manual that is 5% wrong is a liability, a CIO who greps for one false claim stops believing the other 95%. §17 is where the honest gaps live.
>
> Line numbers are as of the 2026-08-15 pass. They drift; the file paths and symbol names do not. If a citation is off by a few lines, grep for the symbol.

---

## 0. Start Here

### 0.1 What is Orb, in one paragraph

Orb is an **on-premises ("on-prem") AI platform for hospitals**. "On-prem" means every piece of it, the database, the AI models, the web server, runs on a computer **inside the hospital**, so **patient data (PHI: Protected Health Information) never leaves the building**. That single constraint is Orb's whole reason to exist: the cloud AI products hospitals would otherwise use are illegal or non-compliant in markets (the UK's NHS, the EU, India) where the law says patient data must stay local. Orb is the alternative: a private AI layer that sits *on top of* the hospital's existing systems and never phones home.

Orb is not one feature, it is an **integrated suite of modules** that share one database, one event system, and one set of local AI models. As of 2026-08-15 there are two families of them:

- **Twelve clinical / AI modules**, Vigil, Forecast, Sage, Scribe, Ingestion, Helix, Lens, Pulse, Bridge, Relay, Surgical Suite, Admission/Discharge (§0.5, §3–§9).
- **Eleven hospital-administration modules**, billing, payments, payer/TPA, procurement, front desk, biomedical, housekeeping, workforce, dietary, NABH, ABDM. Each has a server switch, a nav tab and a lazily-loaded panel, and **all eleven default ON** (`server/config.py:559-570` for the switch semantics, `server/config.py:720-733` for the id→env-var map). They are documented in **§16**.

> **Correction (2026-08-15).** Until this pass the paragraph above said "ten modules" and the table in §0.5 listed twelve rows. Both numbers were wrong and had been for some time.

**Orb replaces the hospital information system, it does not sit on top of one.** This is a change of intent, and it is written into the code, not just the pitch: `server/config.py:542-544`, *"Orb REPLACES the hospital information system, so it has to carry the parts a hospital cannot run without: billing, payments, payer/insurance, procurement and the front desk"*, and `src/shellConfig.tsx:45-47` repeats it as the reason the eleven admin tabs exist at all. Earlier printings of this manual said in §0.7 that Orb "sits *on top of* [the EHR], not replacing it". That sentence is now wrong; it is corrected in the glossary below.

> ⚠️ **Status (important context for every section):** The application is **built end-to-end**, every module has working APIs, a UI, and live data flowing through it. But the **machine-learning models are not yet trained on real clinical data**, training them on real hospital data is the milestone the company is raising money to fund. Throughout the code you will see *gates* that deliberately **hide or refuse to emit AI outputs that would be untrustworthy** when a model is untrained (for example, the Forecast ICU-transfer head and the Vigil CfC overlay). This is a deliberate safety stance, "we don't show untrained outputs as if they were validated", not a bug. Where you see code that *fails closed* (does nothing rather than guess), that is correct.

### 0.2 How to read this manual

- **Each numbered section (1–15) covers one area of the codebase.** They're independent, jump to the one you need.
- Inside a section, each **`###` heading is one source file**. Under it you'll find: the file's role, the libraries it uses (and *why*), a walk-through of every function, and a **"⚠️ If you change this"** note listing what else in Orb is affected.
- **Clinical and technical jargon is defined the first time it appears** in a section, and there's a consolidated glossary in §0.7 below.
- If you're about to change code, read that file's section *and* the **change checklist in §0.8** before you start.

### 0.3 The big picture (architecture)

Orb has four moving parts: a **backend** (Python), a **frontend** (React), a set of **local AI models**, and a **database**. They fit together like this:

```
                          ┌─────────────────────────────────────────────┐
   Hospital systems       │                 THE HOSPITAL'S COMPUTER       │
   (EHR, monitors,        │                 (Apple Silicon, on-prem)      │
    lab machines)         │                                               │
        │  HL7 / CSV / JSON│   ┌───────────────┐      ┌────────────────┐  │
        └─────────────────┼──▶│   INGESTION    │      │  LOCAL AI MODELS│  │
                          │   │ (parse → vital)│      │ • MedGemma (LLM)│  │
   Clinician's browser    │   └──────┬─────────┘      │   via Ollama    │  │
        │   REST + WebSocket│         │                │ • Whisper (ASR) │  │
        │                  │   ┌──────▼─────────────┐  │   via MLX       │  │
        ▼                  │   │   FastAPI BACKEND  │  │ • BGE-M3 (embed)│  │
   ┌──────────┐  HTTP/WS   │   │  routes/ services/ │◀─┤ • CfC  (Vigil)  │  │
   │  REACT   │◀──────────┼──▶│  + Socket.IO       │  │ • BiLSTM(Forecast)│ │
   │ FRONTEND │  JSON      │   │  "nervous system": │  └────────────────┘  │
   │  (src/)  │  events    │   │  event bus, LLM    │                      │
   └──────────┘            │   │  queue, GPU coord. │      ┌────────────┐  │
                          │   └─────────┬──────────┘─────▶│  SQLite DB │  │
                          │             │   read / write   │  (orb.db)  │  │
                          │             └──────────────────│  WAL +     │  │
                          │                                 │  sqlite-vec│  │
                          │                                 └────────────┘  │
                          └─────────────────────────────────────────────┘
```

- **Backend** (`server/`): a **FastAPI** app (FastAPI is a Python web framework). It exposes a **REST API** (normal request→response endpoints, in `server/routes/`) and a **Socket.IO / WebSocket** layer for *live* updates (new vitals, new messages, streaming transcription). The actual logic lives in `server/services/`. See §1, §10.
- **Database** (`orb.db`): a single **SQLite** file. SQLite is a database that is just a file on disk, no separate database server. Orb runs it in **WAL mode** (Write-Ahead Logging, lets readers and writers work at the same time) and loads **sqlite-vec** (an extension that stores AI "embeddings" so Orb can do similarity search for its knowledge base). Every table is documented in §2.
- **Local AI models:** all run on the same machine. **MedGemma** is a medical large language model (LLM), served by **Ollama** (a tool that runs LLMs locally). **Whisper** turns speech into text (ASR = Automatic Speech Recognition), run via Apple's **MLX** framework. **BGE-M3** turns text into vectors ("embeddings") for the knowledge base. **CfC** and **BiLSTM** are small custom neural nets for Vigil and Forecast. Because there is only one GPU, a **GPU coordinator** and an **LLM queue** make sure only one heavy model runs at a time (§10).
- **Frontend** (`src/`): a **React + TypeScript** single-page app, built with **Vite**. It talks to the backend through one file, `src/api/client.ts`, which holds the login token and the Socket.IO connection. See §14.

### 0.4 The two ways data moves (read this before anything else)

Almost everything in Orb is one of these two flows. Learn them and the rest is detail.

**Flow A, a clinician clicks something (request/response):**
```
Browser → src/api/client.ts → HTTP request → FastAPI route (routes/*.py)
→ permission check (permissions.py) → service function (services/*.py)
→ SQLite read/write → JSON response → React updates the screen
```

**Flow B, something happens in the hospital (live/event-driven):**
```
A new vital arrives (Ingestion) → inserted into `vitals` → NEWS2 recomputed (Vigil)
→ an "event" is published on the internal EVENT BUS (event_bus.py)
→ subscribers react (nervous_system_triggers.py): maybe an alert is raised
→ the alert is written to the DB AND pushed over Socket.IO
→ every connected browser updates instantly, no refresh
```

The second flow is what the code calls Orb's **"nervous system"**: modules don't call each other directly; they **publish events** and other modules **react**. This keeps modules loosely coupled. It's all in §10.

### 0.5 The modules (and where each is documented)

#### The clinical / AI modules

| Module | What it does (plain English) | Backend section |
|---|---|---|
| **Vigil** | Watches vital signs and warns when a patient is **deteriorating** (NEWS2 score + an AI overlay). | §3 |
| **Forecast** | Predicts **length of stay, discharge, acuity, ICU transfer**, and hospital-wide bed demand. | §4 |
| **Sage** | An AI **clinical decision-support** assistant that answers grounded in a medical knowledge base (RAG), plus ambient listening and shift handoff. | §5 |
| **Scribe** | **Ambient documentation**, listens to a visit and drafts the clinical note (speech → text → structured note). | §6 |
| **Ingestion** | The **front door for data**, parses HL7 / CSV / JSON from hospital systems into vitals and labs. | §7 |
| **Helix** | **Pharmacy & medications**, formulary, orders, inventory, pharmacist sign-off. | §8 |
| **Lens** | **Medical imaging** analysis (a vision-language model reads an image). | §8 |
| **Pulse** | **Season and conditions**: a built-in seasonal calendar for the hospital's state, no internet. | §8 |
| **Bridge** | The **patient-facing** portal/bot, with an emergency red-flag safety guardrail. | §8 |
| **Relay** | Secure **care-team messaging**, the live channel other modules post alerts into. | §9 |
| **Surgical Suite** | **Operating-room** scheduling and emergency surgery escalation. | §9 |
| (+ Admission / Discharge) | Patient **admission** and **discharge** (incl. secure patient access to the discharge summary). | §9 |

#### The hospital-administration modules (§16)

Added under the founder ruling of 2026-08-11 (`server/config.py:544-546`). Each is a **module** in a specific sense: one server flag, one nav tab, one lazily-loaded panel, and the tab id, the flag key and the module id are deliberately the same word (`server/config.py:720-733`; `src/shellConfig.tsx:48-53`).

| Module | Env switch | API prefix | What it does |
|---|---|---|---|
| **Billing** | `ORB_BILLING` | `/api/billing` | Service/tariff masters, charge capture, invoices. |
| **Payments** | `ORB_PAYMENTS` | `/api/payments` | Receipts, advances/deposits, refunds, discount approvals, cash counter. |
| **Payer / TPA** | `ORB_PAYER` | `/api/payer` | Payer master, patient policies, pre-auth, TPA/insurer/PM-JAY claims and denials. |
| **Procurement** | `ORB_PROCUREMENT` | `/api/procurement` | Vendors, non-pharmacy item master, indents, POs, GRN, stock. |
| **Front desk** | `ORB_FRONTDESK` | `/api/frontdesk` | OPD registration, counters, consulting queue, **transfers**, patient financial summary. |
| **Biomedical** | `ORB_BIOMEDICAL` | `/api/biomedical` | Asset register, preventive-maintenance and **calibration** schedules, breakdown log, AMC/CMC. |
| **Housekeeping** | `ORB_HOUSEKEEPING` | `/api/housekeeping` | Bed turnaround state machine, cleaning requests, turnaround reporting, linen. |
| **Workforce** | `ORB_WORKFORCE` | `/api/workforce` | Duty roster, attendance, overtime, payroll **export**. Deliberately not payroll. |
| **Dietary** | `ORB_DIETARY` | `/api/dietary` | Diet orders, ward meal plans, kitchen counts and indent, NPO handling. |
| **NABH** | `ORB_NABH` | `/api/nabh` | Incident/adverse-event register, quality-indicator dashboard, assessor evidence export. |
| **ABDM / ABHA** | `ORB_ABDM` | `/api/abdm` | ABHA capture, verification records, care-context links, consent artefacts. |

Three properties a CIO or auditor will ask about, all verified in this pass:

1. **All eleven default ON, and a blank value means ON.** `_module_enabled` (`server/config.py:562-570`) returns `True` for unset *and* for empty; only an explicit `0` / `false` / `off` / `no` turns a module off (`server/config.py:559`). The stated reason is in the file: an operator who fat-fingers `ORB_BILLING=` to empty must land on the documented default rather than silently lose the billing screen.
2. **None of them gates a clinical write path.** Stated at `server/config.py:555-557` and repeated per-module for the two whose subject matter is clinical, dietary (`server/config.py:691-697`) and NABH (`server/config.py:710-715`). Turning all eleven off removes nav tabs and their routes; medication ordering, administration, vitals, alerts, shadow capture, audit and the offline queue are untouched.
3. **The frontend learns the switch board from `GET /api/health`**, unauthenticated, because the shell must know which tabs to build at first paint and that is the only endpoint it can read before a session exists (`server/main.py:985-1004`, returning `config.enabled_modules()` at `server/config.py:735-758`). The payload carries no PHI and no secret, it is a list of which screens this appliance offers.

Two of the eleven carry a caveat the manual must not bury:

- **ABDM transmits nothing.** There is no ABDM API client in Orb, no HIP callback, no HIU registration, no consent-manager subscription, no gateway token, no outbox, no retry queue, no sandbox credential; Orb has not been onboarded to the NHA (`server/services/abdm_service.py:1-11`). The module is a **record of steps performed elsewhere**, and `server/config.py:594-597` says so in the config file too.
- **Discharge financial clearance is advisory and OFF by default, and must stay off for the pilot** (`server/config.py:760-786`, `DISCHARGE_FINANCIAL_ENFORCE` at `server/config.py:786-787`). A discharge may compute and display an outstanding balance and warn about it; it may not stop the discharge. Two reasons are recorded in the file, and the first alone is sufficient: a discharge decision is a clinical decision, and in India holding a patient over a bill is unlawful.

The remaining sections cover the things *underneath* the modules: core/bootstrap (§1), the database schema (§2), the infrastructure/"nervous system" (§10), the bots & login (§11), developer scripts (§12), the tests (§13), the frontend (§14), setup/ops (§15), the hospital-administration modules in detail (§16), and the manual's own verification status (§17).

### 0.6 The repository map

```
Orb Project/
├── server/                  ← the Python backend (everything runs from here)
│   ├── main.py              ← app entry point: FastAPI, Socket.IO, 54 routers, middleware  [§1]
│   ├── config.py            ← all settings & ORB_* environment variables (109 distinct)     [§1, §15]
│   ├── database.py          ← the base SQLite schema + connection helpers + run_db          [§2]
│   ├── db_backend.py        ← backend selection, placeholder, pool, DB-API exception aliases [§2]
│   ├── migrations.py        ← the versioned migration ledger (30 migrations)                [§2]
│   ├── auth.py              ← password hashing, JWT, session cookie, CSRF, device PIN       [§1]
│   ├── permissions.py       ← RBAC **and** ward/department PHI scoping                      [§1]
│   ├── services/            ← the real logic, one+ file per module (~110 files)             [§3–§11, §16]
│   ├── routes/              ← the HTTP API endpoints, one file per module (53 files)        [§3–§11, §16]
│   ├── models/              ← the neural nets (vigil_cfc.py, forecast_lstm.py) + schema classes [§3, §4, §12]
│   ├── ws/                  ← WebSocket endpoints (realtime.py, scribe_stream.py)           [§6, §10]
│   ├── adapters/            ← outbound integration adapters
│   ├── eval/                ← retrieval / model evaluation harnesses
│   ├── corpus/              ← the clinical knowledge corpus loaded into the RAG store       [§5]
│   ├── site_config/         ← per-site configuration files
│   ├── dev_tools/           ← developer-only utilities
│   ├── ops/                 ← appliance ops (bootstrap, watchdog, model contract checks)
│   ├── backups/             ← backup working directory
│   ├── downtime_exports/    ← downtime-mode paper exports
│   ├── scripts/             ← seed/reset/train/benchmark utilities (run by hand)            [§12]
│   └── tests/               ← the pytest suite (181 files, 6,161 collected tests)           [§13]
├── src/                     ← the React frontend (the product UI)                           [§14]
│   └── shellConfig.tsx      ← the shell's ONE role input: home + tabs + primary action      [§14]
├── ops/                     ← off-appliance ops (Grafana dashboard)
├── docs/
│   ├── ORB_TECHNICAL_MANUAL.md  ← THIS MANUAL (assembled from docs/manual_sections/)
│   ├── MODULE_MANUAL.md / USER_MANUAL.md ← the non-engineer manuals
│   ├── ORB_ANALYSIS.md      ← the 132-finding code audit
│   ├── ORB_FIX_LOG.md       ← what was fixed and why
│   ├── BACKUP_AND_RESTORE.md, KEY_CEREMONY.md, SHADOW_MODE.md, PILOT_ROADMAP.md
│   └── fundraising/         ← investor materials (deck, market research)
└── orb.db                   ← the SQLite database file (NOT committed, contains PHI)
```

> **Correction (2026-08-15).** The old map said `server/tests/` held **285 tests**. `pytest --collect-only` reports **6,161 tests across 181 files**. The map also omitted `migrations.py`, `db_backend.py`, `adapters/`, `eval/`, `corpus/`, `site_config/`, `dev_tools/`, `ops/` and `shellConfig.tsx` entirely, several of which are load-bearing (see §2 and §14).

### 0.7 Glossary (clinical + technical terms you'll meet everywhere)

**Clinical:**
- **PHI**, Protected Health Information. Any data that identifies a patient. The reason Orb is on-prem.
- **EHR / HIS**, Electronic Health Record / Hospital Information System: the hospital's system of record. **Orb is built to replace it**, which is why it carries billing, payments, payer, procurement and the front desk (`server/config.py:542-547`). *Previous printings of this manual said Orb "sits on top of it, not replacing it", that is no longer the design.* Orb still **interoperates** with an incumbent in both directions (FHIR/HL7 in via §7, FHIR R4 out via `server/services/fhir_export_service.py`), and during the shadow pilot the incumbent keeps running the money (`server/config.py:779-784`).
- **Vital signs**, the basic body measurements: heart rate, blood pressure (systolic/diastolic), respiratory rate, temperature, SpO2 (oxygen saturation %), and consciousness level.
- **NEWS2**, National Early Warning Score 2. A UK-standard **points score** computed from vital signs; a higher score means a sicker, deteriorating patient. The core of Vigil (§3 explains the exact scoring).
- **AVPU**, a quick consciousness scale: **A**lert, responds to **V**oice, to **P**ain, or **U**nresponsive.
- **Sepsis**, a life-threatening over-reaction to infection; catching it early saves lives (a Vigil target).
- **Acuity**, how sick / resource-intensive a patient is.
- **SaMD**, Software as a Medical Device. If software makes diagnostic claims, regulators treat it like a medical device (needs clearance). Orb's non-diagnostic modules avoid this; the diagnostic ones (Vigil/Lens/Sage) are on a clearance path.
- **HL7 / FHIR**, healthcare data formats. **HL7 v2** is the old pipe-delimited message format hospitals still use; **FHIR** is the modern JSON standard. *Correction: FHIR is not "on Orb's roadmap", it is implemented in both directions.* Inbound: `FHIRAdapter` parses FHIR R4 Bundles and single resources (`server/services/ingestion_adapters.py:682-713`, registered at `:1117`), §7. Outbound: `server/services/fhir_export_service.py` serialises Orb data back to FHIR R4 for write-back into an EHR, tagging every resource with an Orb provenance tag and downgrading unverified AI-suggested medications to `status=draft` / `intent=proposal` so they can never land in the receiving EHR as an active order (`server/services/fhir_export_service.py:11-21`).
- **ADT**, Admit / Discharge / Transfer messages (patient movement events).
- **SOAP note**, a clinical note structured as Subjective / Objective / Assessment / Plan.
- **RxNorm**, a standard vocabulary of drug names, used to normalize medication text.

**Technical:**
- **FastAPI**, the Python web framework Orb's backend is built on.
- **ASGI / uvicorn**, ASGI is the async Python web-server standard; **uvicorn** is the program that actually runs the FastAPI app.
- **REST endpoint / route**, a URL the frontend calls (e.g. `GET /api/vigil/patients`).
- **Socket.IO / WebSocket**, a *persistent* connection for live two-way updates (vs. one-shot REST).
- **Service**, a Python module in `services/` holding business logic, called by routes.
- **JWT**, JSON Web Token. After you log in, the server gives your browser a signed token; the browser sends it on every request to prove who you are.
- **RBAC**, Role-Based Access Control. Permissions are granted by role (doctor, nurse, patient, admin). See §1.
- **bcrypt**, the algorithm used to hash passwords so the raw password is never stored.
- **LLM**, Large Language Model (here, MedGemma).
- **RAG**, Retrieval-Augmented Generation: fetch relevant facts from a knowledge base and give them to the LLM so its answer is grounded in real sources, not made up. See §5.
- **Embedding**, a list of numbers representing the *meaning* of a piece of text, so similar texts have similar numbers. Stored via sqlite-vec.
- **ASR**, Automatic Speech Recognition (Whisper). See §6.
- **Event bus / publish-subscribe**, modules emit named events; other modules subscribe and react, without calling each other directly. Orb's "nervous system." See §10.
- **Idempotent**, doing the same operation twice has the same effect as once (e.g., the same HL7 message won't insert two vitals). Important in Ingestion (§7) and Helix (§8).
- **Fail-closed**, when unsure, do nothing / deny, rather than guess. A safety stance used in Scribe note-signing (§6) and Bridge (§8). In Orb the phrase has a precise, split meaning worth learning now, because the two halves point in opposite directions and both are deliberate:
  - **Audit fails closed.** For the categories `security`, `clinical_access`, `phi_access`, `authentication` and `ai_inference`, a failed audit write raises `AuditWriteError` (`server/services/audit_service.py:43-45`; set corrected 2026-09-04, it had named `auth` and `ai_egress`, which no call site ever emitted). What happens next is the caller's decision. Serving PHI that could not be recorded defeats the control, so the PHI is not served, e.g. `/uploads/{filename}` returns **503** rather than the file (`server/main.py:955-956`). A login whose audit write fails issues no token, because nothing in `server/routes/auth_routes.py` catches the error (an unhandled 500). The three `record_inference` callers catch it and still serve the AI turn, so AI inference is fail-closed at the audit layer only (§10.5).
  - **Shadow fails open.** The shadow-mode evidence log must never fail a clinical action; a shadow write that cannot happen is lost, not escalated (`server/services/shadow_service.py:9-11`).
- **`run_db`**, the house rule for database access from async code. Blocking SQLite work called from an `async def` must go through `await run_db(fn, …)` (`server/database.py:1205-1228`), which runs it on a private 4-worker executor (`server/database.py:1201`) instead of the event loop. Since encryption-at-rest every page touched costs HMAC-SHA512 + AES, so a blocking query on the loop stalls the whole appliance, three production freezes came from exactly that shape (`server/services/audit_middleware.py:15-21`). The function passed to `run_db` must be pure-sync and must open and close its own session inside the worker thread.
- **Ward scope / department scope**, the server-side answer to "which patients may this account see?", derived **only** from the authenticated user's row, never from anything in the request (`server/permissions.py:222-248`). A patient's department comes from their *admission*, not from `patients.ward`, which is an unconstrained free-text bed label (`server/permissions.py:264-275`). Out-of-scope returns **404, never 403**, because a 403 confirms the patient exists (`server/permissions.py:474-477`). See §1.
- **Shadow mode**, the pilot's evidence instrument: Orb runs beside the incumbent system and records what it *would* have done, without acting. `server/services/shadow_service.py` owns the write path for `shadow_events`; aggregation reads the `shadow_events_current` view, never the base table. See `docs/SHADOW_MODE.md`. *This subsystem has no dedicated section in this manual yet, see §17.*
- **Append-only audit log**, the `audit_logs` table can be inserted into but never updated or deleted (enforced by database triggers), so the record of who-did-what is tamper-evident (a HIPAA requirement). See §2, §10.
- **WAL**, Write-Ahead Logging, the SQLite mode that lets reads and writes happen concurrently.
- **Correlation ID**, one ID stamped on all the audit events from a single request, so you can trace a request end-to-end.

### 0.8 The change-safety checklist (use this before every edit)

Orb is a clinical system: a careless change can hide an alert or fabricate data. Before you change a file:

1. **Read its section in this manual**, especially the "⚠️ If you change this" notes.
2. **Find its callers.** A service function is usually called by a route and sometimes by the event bus or a bot. Search the codebase for the function name.
3. **Check the database contract.** If you change a table's columns, find every service and the frontend that reads them (§2 lists table owners).
4. **Check the event/Socket.IO contract.** If you rename an event (e.g. `news2_recomputed`) or a Socket.IO message, the frontend and other subscribers must change too. (We learned this the hard way: renaming the NEWS2 event required updating the admin UI and a test.)
5. **Never weaken a safety gate** without understanding it: the model-trained gates, the fail-closed note-signing, the Bridge emergency guardrail, and the append-only audit triggers exist on purpose.
6. **Run the relevant tests** (§13 maps each module to its test file). The suite is `pytest tests/` with the env vars in §13/§15. **6,161 tests are collected across 181 files** (`pytest --collect-only`, 2026-08-15), the old figure of 285 in this checklist was years of drift. Run *your* test files, not the whole suite, unless you have time to spare.
7. **A test must assert the invariant and must be proven able to fail.** A test that passes against the broken tree proves nothing. Break the code deliberately, watch the test go red, then fix it.
8. **If it touches the UI**, type-check the frontend (`npx tsc --noEmit -p tsconfig.app.json`), run `node scripts/check-contrast.mjs`, and look at the screen.
9. **If it touches an `async def` that reads or writes SQLite**, it goes through `run_db` (§0.7). If it touches cancellation, `CancelledError` must propagate. If it catches a DB exception, use the `db_backend` aliases, never stdlib `sqlite3` (§2).

Now pick your section.
