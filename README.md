# 🌐 Orb Hospital OS

<div align="center">
  <h3>A local-first, AI-native platform designed for modern clinical environments.</h3>
</div>

---

Orb Hospital OS combines clinical records, ward workflows, real-time telemetry and optional local AI assistance on hospital hardware, including Apple Silicon Mac Studios. It is a pre-pilot system; deployment and clinical validation remain separate work.

The current founding-hospital product is **Orb Ward Record**: clerk-entered observations and the rounds file. `ORB_WARD_RECORD=1` disables scoring, AI models, Lens and sepsis autostart. The wider hospital-system build includes registration, OPD, ADT, laboratory and pharmacy-store workflows behind default-off switches. Implemented code is not evidence of clinical validation or approval to enable a module. See [the HIS implementation plan](docs/HIS_BUILD_PLAN_2026-10.md) and [current product rulings](docs/RULINGS_NEEDED_2026-10.md).

The [October offline implementation pass](docs/OFFLINE_IMPLEMENTATION_2026-10.md) adds local complaints, mortuary/CSSD registers, reporting and finance exports, clinical documentation, records governance and equipment safety evidence. These modules remain default-off and contain no external submission or device transport. The same report records the implemented boundaries, tests and remaining engineering work. The [completion and browser acceptance record](docs/OFFLINE_COMPLETION_AND_BROWSER_2026-10.md) covers the subsequent reliability fixes and synthetic browser screenshots.

## ✨ Core Modules

### 🏥 Vigil (Real-time Monitoring & Risk Scoring)
Vigil continuously tracks patient vital signs and computes real-time risk scores using the NEWS2 (National Early Warning Score 2) system, including Scale 2 SpO2 scoring for patients with COPD / chronic type-2 respiratory failure.
- **Real-time Engine**: Vitals streamed via Socket.IO.
- **Risk Assessment**: Compares recent vitals against historical data to classify patient trajectories (improving, stable, deteriorating).
- **AI Triage**: Generates human-readable clinical assessments and recommendations based on current vitals, risk levels, and clinical notes.

### 🧠 Sage (Clinical Intelligence Assistant)
Sage acts as a highly capable medical knowledge assistant utilizing a local Retrieval-Augmented Generation (RAG) pipeline.
- **Embeddings**: Uses `BAAI/bge-m3` and `sqlite-vec` for rapid, local semantic search across clinical guidelines.
- **Inference**: Powered by `medgemma:27b` from the official Ollama library (MedGemma 1, the multimodal build), with `medgemma:4b` (MedGemma 1) for the ambient tier, to draft answers to clinical questions for clinician review. Note: the model is not fine-tuned or clinically validated yet, outputs are decision support, not ground truth.

### 🎙️ Scribe (Voice-to-Text Clinical Documentation)
Scribe automates clinical note-taking by transcribing voice and structuring it into standardized formats (SOAP, Discharge, Handoff).
- **Transcription**: High-speed, accurate voice transcription using `Whisper large-v3` running on Apple MLX.
- **Multi-Stage Pipeline**: Features an ASR stage, an LLM correction stage, a drafting stage, and an optional hallucination verification stage.
- **Deployment Profiles**: Tunable for `dev` (faster, lighter models) or `prod` (high fidelity).

### 🔍 Lens (Medical Image Analysis)
Lens analyzes medical imaging (ECG, X-ray, MRI, CT) to provide AI-assisted draft observations for clinician review (not a diagnostic device).
- **Vision Model**: Uses `medgemma:27b` (MedGemma 1 27B Multimodal) to stream back structured draft notes (Findings, Impression, Recommendations).

### 🌍 Pulse (Season and Conditions)
Pulse shows a bundled seasonal calendar for the hospital's configured Indian state, including the current season and the next 60 days.
- **Strictly offline**: No weather, AQI, flu or recall feeds, browser geolocation, or external lookup.
- **Review status**: Calendar entries remain labelled as drafts until reviewed by the site's clinical lead. Forecast displays this as context without applying an invented seasonal multiplier.

### 🛏️ Bridge (Patient Portal)
Bridge offers a patient-facing interface that translates complex clinical data into plain, reassuring language.
- **AI Q&A**: Answers patient questions in non-technical terms without dispensing direct medical advice.
- **Discharge Summaries**: Automatically generates comprehensive, patient-friendly timelines and summaries.

### 💬 Relay (Internal Messaging)
Internal clinical communication with dedicated case rooms per patient.
- Supports direct messages, group channels, and automated bot alerts for deteriorating patients.

### 💊 Helix & Surgical Suite
- **Helix**: Comprehensive pharmacy and medication inventory management.
- **Surgical Suite**: Live tracking and timeline management for surgical operations.

### 🔮 Forecast (LSTM Clinical Prediction)
- **Neural Net**: A Multi-task Bidirectional LSTM with Temporal Attention.
- **Predictions**: Calculates expected Length-of-Stay (LoS), Discharge Probability, and Acuity Trajectory based on 19-dimensional patient time-series features.

### 🧪 Clinical Safety & Interoperability (built, pre-pilot)
The following modules are implemented but have not yet been through a clinical pilot:
- **Drug Safety**: Order-time allergy and drug–drug interaction screening.
- **Care Bundles**: Time-tracked care bundles, including Sepsis Six timers.
- **I-PASS Handoff**: Structured shift handoff following the I-PASS format.
- **EHR Downtime Mode**: Read-only continuity of care when upstream systems are unavailable.
- **Model Governance**: Registry and governance tracking for deployed AI models.
- **FHIR Interoperability**: FHIR ingestion (`/api/ingest/fhir`) and FHIR export (`$everything` bundles) for standards-based data exchange.

---

## 🛠️ Technology Stack

**Frontend**
- React 19, TypeScript, Vite
- Glassmorphic, highly responsive UI tailored for clinical density

**Backend & Data**
- Python, FastAPI
- SQLite (with `sqlite-vec` for vector similarity search; opt-in SQLCipher encryption at rest, which coexists with vector search, FTS5 and WAL)
- Socket.IO for real-time telemetry streaming

**AI & Local Inference**
- **Ollama**: Serves large language and vision models (`medgemma:27b` for careful answers, `medgemma:4b` for the fast and ambient tiers). Without `medgemma:4b` pulled, fast mode quietly runs on the 27B. MedGemma 1.5 exists only at 4B, so the 27B tier is MedGemma 1 by necessity, see SETUP-NOTES.md.
- **Apple MLX**: Hardware-accelerated transcription (`whisper-large-v3-mlx`).
- **Custom LSTM**: PyTorch-based multi-head sequence prediction.

---

## 🚀 Getting Started

### Prerequisites
- Node.js 20.19+ or 22.12+ (the Vite 8 engine requirement)
- Python 3.12+
- [Ollama](https://ollama.com/) for enabled model features; the Ward Record profile runs with models off
- Mac with Apple Silicon (recommended for MLX performance)

### 1. Clone & Install
```bash
# Clone the repository
git clone https://github.com/samuelmorningstar23/Orb.git
cd Orb

# Install Frontend Dependencies
npm ci

# Install Backend Dependencies
cd server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Pull AI Models (Ollama)
For model-enabled evaluation, provision these models before joining the offline hospital network:
```bash
ollama pull medgemma:27b            # primary clinical tier, 17 GB
ollama pull medgemma:4b             # fast and ambient tier, 3.3 GB
```

### 3. Run the OS
Start both the backend and frontend servers:

**Backend (Terminal 1):**
```bash
cd server
source .venv/bin/activate
python main.py            # starts FastAPI + Socket.IO on http://127.0.0.1:8000
# (equivalent: uvicorn main:socket_app --host 127.0.0.1 --port 8000)
```

**Frontend (Terminal 2):**
```bash
npm run dev
```

---

## 🔒 Architecture & Privacy

Orb's standing product rule is **strictly offline operation**. Inference runs on hospital hardware, and unavailable models must be provisioned before the appliance joins the hospital network. Local execution alone does not establish privacy or regulatory compliance.

> **Compliance status:** Orb is not certified or compliant out of the box. Implemented safeguards include bcrypt/JWT authentication, cookie/CSRF protection, role and patient scope checks, hash-chained audit records, consent/data-rights/retention services, optional TLS and SQLCipher database encryption, and AES-GCM upload encryption. These need site configuration, clinical and legal review, and operational validation. `scripts/backup.sh` creates and verifies a SQLCipher-aware snapshot; `--offsite` supports a locally attached backup destination. Restore drills and off-machine key escrow remain operator responsibilities. See [backup and restore](docs/BACKUP_AND_RESTORE.md) and the [repository audit](docs/REPOSITORY_AUDIT_2026-10.md) for unresolved release blockers.
>
> **What encryption at rest does and does not buy you (equally honest):** with the key stored in `.env` on the same disk as the database, it protects **copies** of the data, nightly backups, exports, a disk or volume image read on another machine. It does **not** protect a running appliance, or a stolen one, because an attacker who can read `orb.db` can read `.env` too. Enable FileVault as well, and treat moving the key into the OS keychain (rather than a dotfile) as the stronger posture. Note also that backups inherit the same key: the key must be escrowed somewhere other than the machine it protects, or the backups cannot be restored. None of this is a compliance certification, it is one technical safeguard, now present and testable.

---

## 🚢 Deploy

### Prerequisites
- Node.js 20.19+ or 22.12+ and Python 3.12+
- For model-enabled deployments, [Ollama](https://ollama.com/) with required models provisioned before joining the offline network
- Frontend deps installed (`npm ci`) and a backend virtualenv with `pip install -r server/requirements.txt`
- A configured environment file, Orb reads all runtime config from `ORB_*` env vars

### 1. Configure environment
Every variable Orb reads is enumerated (with safe placeholders, purpose, and when-required notes) in [`.env.example`](.env.example):
```bash
cp .env.example .env
# then edit .env: at minimum, for a prod deployment set:
#   ORB_DEPLOYMENT_PROFILE=prod
#   ORB_JWT_SECRET=<long random secret>        # startup FAILS in prod if unset
#   ORB_INGESTION_API_KEY=<secret>             # startup FAILS in prod if unset
#   ORB_DB_ENCRYPTION_KEY / ORB_UPLOAD_ENCRYPTION_KEY  # encryption at rest
#   ORB_CORS_ORIGINS=<your https frontend origin>
```

**Encryption at rest.** Set `ORB_DB_ENCRYPTION_KEY` (`openssl rand -hex 32`); the SQLCipher
driver is already pinned in `server/requirements.txt`. A database created after the key is
in place is encrypted from birth. An **existing plaintext `orb.db` must be migrated once**, 
SQLCipher cannot open a plaintext file, so the app fails with `file is not a database`
until you run it:
```bash
cd server
.venv/bin/python scripts/encrypt_db.py --check     # report state, change nothing
.venv/bin/python scripts/encrypt_db.py --migrate   # with the server stopped
```
The migration backs up the plaintext original, verifies the encrypted copy (per-table row
counts, audit hash-chain linkage, vector search, FTS5), and only then swaps it in. It leaves
the plaintext backup on disk, that file still holds unencrypted PHI, so delete it once you
are satisfied. Keep the key escrowed off the machine: backups are encrypted with it, and
without it they cannot be restored.

### 2. Create the first admin: or seed a demo

These are two different paths. Pick the one that matches what you are building.

**A real deployment: create an admin account, do NOT seed.**
A freshly created database has no login-capable user, so this step is mandatory, 
without it nobody can get into the appliance at all:
```bash
cd server
.venv/bin/python scripts/create_admin.py          # interactive prompts
# non-interactive (CI / provisioning):
#   .venv/bin/python scripts/create_admin.py --non-interactive --id ADM001 --name "Jane Doe"
```
Then create the rest of the staff from the in-app Admin → Users screen, so every
account is created through the audited path.

> **Do not run the demo seeder on an appliance that will hold real patients.**
> `reset_db_and_seed.py` wipes patient tables *and* creates fixed demo accounts, 
> `DOC001` / `NUR001` / `PHA001` / `ADM001`, every one of them with the password
> `password`. That is fine for a laptop demo and a serious breach on a hospital
> network. Earlier revisions of this section put the seeder in the production
> deploy path; it has been moved out deliberately.

**A demo or evaluation box (no real PHI).**
The seeder wipes patient-related tables, loads clean clinical + Helix pharmacy demo
data, randomizes admission dates, links the demo patient login (P001), backfills
labs/notes/surgeries, and finally **re-seals the audit hash chain** (seeding writes
audit rows through several paths that can break the tamper-evidence chain at the
root, the reseal step, `reseal_audit_chain.py --force`, guarantees a freshly-seeded
DB verifies clean):
```bash
cd server
source .venv/bin/activate
python scripts/reset_db_and_seed.py    # DEMO ONLY, creates password-`password` accounts
```

### 2b. Set up backups before you admit a patient
Backups are not optional on a single-appliance deployment. See
[`docs/BACKUP_AND_RESTORE.md`](docs/BACKUP_AND_RESTORE.md) for the full runbook;
the short version:
```bash
set -a && . ./.env && set +a
./scripts/backup.sh --offsite /Volumes/OrbBackup   # verified backup + off-box copy
./scripts/install-backup-schedule.sh               # run it nightly via launchd
./scripts/restore-drill.sh                         # prove a backup actually restores
```

### 3. Build and start
From the repository root, the launcher starts the backend and serves the built frontend on the same origin:
```bash
npm run build
./run-orb.sh
```
Use the two-terminal development instructions above when working on the UI.
Bind the backend to loopback (`ORB_HOST=127.0.0.1`, the default) and terminate TLS
at a reverse proxy, or set `ORB_TLS_CERT` + `ORB_TLS_KEY`, before exposing beyond localhost.

### Continuous Integration
`.github/workflows/ci.yml` runs four jobs on every push and pull request:
1. **Backend (pytest)**, installs `requirements-dev.txt` and runs `pytest -q` in the `dev` profile.
2. **Frontend (build + tests)**, lint ratchet (`scripts/lint-ratchet.mjs` against `.lint-baseline.json`), `npm run build` (which runs `tsc -b` first, gating typecheck + build), then `npm run test`.
3. **Visual audit (light + dark)**, drives every screen in both themes against a mocked API, checks contrast/visibility/alignment + JS errors, and fails on any non-allowlisted finding.
4. **E2E (11 browser journeys)**, boots a disposable FastAPI checkout with synthetic data and the production frontend, then checks real authentication, clinical navigation and the offline modules.

`.github/workflows/security.yml` audits the npm lockfile, Python pins, the installed slim Linux runtime and the resolved Apple production dependency tree on pushes, pull requests and a weekly schedule. The [ML security and Sage timing record](docs/ML_SECURITY_AND_SAGE_LATENCY_2026-10.md) records the fresh environment migration and local acceptance. Existing environments must be rebuilt and validated; updating requirements does not remove their obsolete packages. Apple speech native-build and target-site acceptance remain separate checks.

The [Sage prompt-cache speed fix](docs/SAGE_SPEED_FIX_2026-10.md) documents the
subsequent cache investigation, guarded local Ollama launch profile and timed
browser qualification with the same offline model and clinical safeguards.

### Verification and operation

Use `npm run check:types` for both TypeScript projects; the solution-style root alone contains no source files. `npm run check` also checks contrast and documentation citations. Backend tests use `server/requirements-dev.txt` and isolated temporary databases; the slim test environment includes the encryption drivers so encryption and restore tests execute in CI.

`GET /api/health` is cheap liveness and publishes feature switches. `GET /api/ready` returns 503 when the clinical database cannot be read; it does not require AI models. `/metrics` exposes operational counters and can require `ORB_METRICS_TOKEN`. Keep backup, restore and key-escrow drills in the deployment runbook.

### Further reading
- **Documentation index**: [`docs/README.md`](docs/README.md), the full corpus, grouped (learning the system, ops, ML, audits, fundraising)
- **Learning the system**: [`docs/ORB_MASTER.md`](docs/ORB_MASTER.md) + the 41 subsystem deep-dives in [`docs/guide/`](docs/guide), indexed by [`00-screen-to-code.md`](docs/guide/00-screen-to-code.md) and the 15-file [`screen-map/`](docs/guide/screen-map)
- **Branch history**: [`BRANCHES.md`](BRANCHES.md), what every branch contained and how the repo was consolidated (2026-07-10)
- Design system: [`DESIGN.md`](DESIGN.md)
- Visual audit tooling and allowlist: [`tests/visual/README.md`](tests/visual/README.md)
- Verification methodology and results: [`tests/visual/VERIFICATION.md`](tests/visual/VERIFICATION.md)

---
*Built for the future of clinical care.*
