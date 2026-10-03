# 🌐 Orb Hospital OS

<div align="center">
  <h3>A local-first, AI-native platform designed for modern clinical environments.</h3>
</div>

---

Orb Hospital OS leverages real-time data streaming and heavy local AI inference to provide clinical decision support, continuous patient monitoring, and workflow automation. Built to run securely and locally on hospital hardware (like Apple Silicon Mac Studios), it ensures complete data privacy while delivering state-of-the-art intelligence.

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

### 🌍 Pulse (Epidemiological & Environmental Intelligence)
Pulse monitors macro-environmental factors that impact hospital admissions and respiratory conditions.
- **Surveillance**: Tracks live local Weather, Air Quality Index (AQI), and Flu/Viral Surveillance data.
- **Clinical Context**: Cross-references environmental hazards with clinical data to provide proactive recommendations (e.g., asthma exacerbation risks during poor air quality).

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
- Node.js 18+
- Python 3.12+
- [Ollama](https://ollama.com/) (installed and running)
- Mac with Apple Silicon (recommended for MLX performance)

### 1. Clone & Install
```bash
# Clone the repository
git clone https://github.com/samuelmorningstar23/Orb.git
cd Orb

# Install Frontend Dependencies
npm install

# Install Backend Dependencies
cd server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Pull AI Models (Ollama)
Ensure Ollama is running, then pull the necessary models:
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
python main.py            # starts FastAPI + Socket.IO on http://0.0.0.0:8000
# (equivalent: uvicorn main:socket_app --host 0.0.0.0 --port 8000)
```

**Frontend (Terminal 2):**
```bash
npm run dev
```

---

## 🔒 Architecture & Privacy

Orb is intentionally designed as a **local-first** environment. By running heavy inference directly on hospital edge hardware (like the Mac Studio), Orb keeps Protected Health Information (PHI) inside the hospital's network and removes the cloud data-egress vector that cloud-AI vendors introduce.

> **Compliance status (honest):** Local-first removes the external-leak vector but does **not** by itself confer HIPAA/GDPR/DPDP compliance. Orb is *designed to support* those technical safeguards; it is not certified or compliant out of the box. **Done today:** bcrypt + JWT auth, role-based access, append-only audit logging (SHA-256 hash-chain triggers enforce immutability at the database layer, plus a mutation-logging middleware that records every state-changing request), opt-in TLS in transit (enable by setting `ORB_TLS_CERT` + `ORB_TLS_KEY`; otherwise terminate TLS at a reverse proxy), and **encryption at rest**, the SQLite database via SQLCipher (set `ORB_DB_ENCRYPTION_KEY`; an existing plaintext DB is migrated once with `server/scripts/encrypt_db.py --migrate`, a fresh one is created encrypted) plus AES-256-GCM encryption of uploaded PHI media (`ORB_UPLOAD_ENCRYPTION_KEY`). **Backups (as of 2026-07-30):** `scripts/backup.sh` takes a SQLCipher-aware, transactionally-consistent snapshot, proves it opens with the key and holds real clinical rows before it will rotate anything, and copies it off-box with `--offsite`; `scripts/install-backup-schedule.sh` runs it nightly under launchd and `scripts/restore-drill.sh` verifies a backup restores (row counts + audit hash-chain linkage). **Key escrow is still on the operator**, the backups are encrypted with `ORB_DB_ENCRYPTION_KEY` and are unreadable without it. See [`docs/BACKUP_AND_RESTORE.md`](docs/BACKUP_AND_RESTORE.md). **Not yet built (roadmap, funded by the raise):** data-subject-rights / erasure, retention policy, DPIA, and BAAs. Do not deploy against real PHI until these are in place.
>
> **What encryption at rest does and does not buy you (equally honest):** with the key stored in `.env` on the same disk as the database, it protects **copies** of the data, nightly backups, exports, a disk or volume image read on another machine. It does **not** protect a running appliance, or a stolen one, because an attacker who can read `orb.db` can read `.env` too. Enable FileVault as well, and treat moving the key into the OS keychain (rather than a dotfile) as the stronger posture. Note also that backups inherit the same key: the key must be escrowed somewhere other than the machine it protects, or the backups cannot be restored. None of this is a compliance certification, it is one technical safeguard, now present and testable.

---

## 🚢 Deploy

### Prerequisites
- Node.js 18+ (CI uses Node 20) and Python 3.12+
- [Ollama](https://ollama.com/) running with the required models pulled (`ollama pull medgemma:27b` and `ollama pull medgemma:4b`)
- Frontend deps installed (`npm install`) and a backend virtualenv with `pip install -r server/requirements.txt`
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

### 3. Start the services
**Backend:**
```bash
cd server
source .venv/bin/activate
python main.py            # FastAPI + Socket.IO on http://0.0.0.0:8000
# (equivalent: uvicorn main:socket_app --host 0.0.0.0 --port 8000)
```
**Frontend:**
```bash
npm run dev              # dev server, proxies /api + /socket.io -> :8000
# production build:
npm run build && npm run preview
```
Bind the backend to loopback (`ORB_HOST=127.0.0.1`, the default) and terminate TLS
at a reverse proxy, or set `ORB_TLS_CERT` + `ORB_TLS_KEY`, before exposing beyond localhost.

### Continuous Integration
`.github/workflows/ci.yml` runs four jobs on every push and pull request:
1. **Backend (pytest)**, installs `requirements-dev.txt` and runs `pytest -q` in the `dev` profile.
2. **Frontend (build + tests)**, lint ratchet (`scripts/lint-ratchet.mjs` against `.lint-baseline.json`), `npm run build` (which runs `tsc -b` first, gating typecheck + build), then `npm run test`.
3. **Visual audit (light + dark)**, drives every screen in both themes against a mocked API, checks contrast/visibility/alignment + JS errors, and fails on any non-allowlisted finding.
4. **E2E (5 golden journeys)**, boots the real FastAPI backend + Vite dev server (via `playwright.config.ts`), seeds the demo DB with `reset_db_and_seed.py`, and runs the Playwright golden-journey suite.

### Further reading
- **Documentation index**: [`docs/README.md`](docs/README.md), the full corpus, grouped (learning the system, ops, ML, audits, fundraising)
- **Learning the system**: [`docs/ORB_MASTER.md`](docs/ORB_MASTER.md) + the 41 subsystem deep-dives in [`docs/guide/`](docs/guide), indexed by [`00-screen-to-code.md`](docs/guide/00-screen-to-code.md) and the 15-file [`screen-map/`](docs/guide/screen-map)
- **Branch history**: [`BRANCHES.md`](BRANCHES.md), what every branch contained and how the repo was consolidated (2026-07-10)
- Design system: [`DESIGN.md`](DESIGN.md)
- Visual audit tooling and allowlist: [`tests/visual/README.md`](tests/visual/README.md)
- Verification methodology and results: [`tests/visual/VERIFICATION.md`](tests/visual/VERIFICATION.md)

---
*Built for the future of clinical care.*
