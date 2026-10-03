## 12. Developer, Data & Model Scripts + Small Schema Files

This section covers every script in `server/scripts/`, the small schema files in `server/models/`, and the miscellaneous one-off utilities living at `server/` root. Together these form the developer toolbox: you run them from the command line, never from the running server.

> **Convention used below:** paths are relative to the repository root (`Orb Project/server/`). Run all scripts from inside `server/` with `python scripts/<name>.py` unless noted otherwise.

---

### Seed & Reset

These scripts are the primary way to populate a fresh (or reset) database with demo data. **They mutate the database directly, do not run them against a production instance.**

---

#### `scripts/seed_clinical_data.py`

**One-line role:** The master clinical seeder. Creates 13 staff accounts and admits 30 realistic patients across six departments, then back-fills 24 hours of vital-sign history and computes initial risk scores.

**How to run:**

```bash
python scripts/seed_clinical_data.py
```

**What it does, step by step:**

1. **Calls `init_db()`**, runs all schema migrations before inserting anything, so it is safe to run on an empty database.
2. **Inserts users**, 7 doctors, 1 nurse, 1 admin, 1 pharmacist, and 2 patient accounts. Passwords are SHA-256 hashed inline (not bcrypt, fine for demo, not for production).
3. **Admits 30 patients**, the full patient list is hardcoded at the top of the file. For each patient the script inserts rows into: `patients`, `admissions`, `patient_profiles`, `medications`, `vitals` (one reading at admission), `channels` (a per-patient case room), and `sage_room_memory` (initial summary). Foreign key checks are disabled during this bulk insert to avoid ordering issues.
4. **Generates 24-hour vitals history**, for each patient, 24 hourly vital records are inserted into `vitals`. Patients flagged `is_deteriorating=True` show a realistic decline in the final 6 hours (heart rate rises, SpO2 falls, etc.).
5. **Computes risk scores**, calls `services.vigil_service.get_patient_risk()` for each patient and writes results to the `risk_scores` table. If the service is unavailable (import error), this step is silently skipped.
6. **Also seeds 7 department channels**, one per specialty (General Medicine, Cardiology, etc.) with a welcome system message.

**Inputs:** None (all patient data is hardcoded). Requires `database.py` and `services/vigil_service.py` to be importable.

**Outputs / tables written:**
`users`, `patients`, `admissions`, `patient_profiles`, `medications`, `vitals`, `risk_scores`, `channels`, `messages`, `sage_room_memory`

**⚠️ MUTATES DATABASE.** Existing rows with the same MRN are skipped (not overwritten). Running twice is safe but will not update existing patients.

---

#### `scripts/reset_db_and_seed.py`

**One-line role:** Nuclear reset, wipes all patient-related tables, then runs the full seed pipeline in sequence: `seed_clinical_data.py` → `seed_helix.py` → random admission-date scramble.

**How to run:**

```bash
python scripts/reset_db_and_seed.py
```

**What it does, step by step:**

1. **Disables FK constraints** and issues `DELETE FROM <table>` for 27 tables: all patient, channel, message, Sage memory/action, clinical-document, medication, surgery, and forecast tables. It does **not** wipe `users` or system configuration.
2. **Re-enables FK constraints** and calls `seed()` from `seed_clinical_data.py`.
3. **Calls `seed_helix()`** from `seed_helix.py` to load the pharmacy catalog.
4. **Randomizes admission dates**, updates `patients.admission_date`, `admissions.created_at`, `channels.created_at`, and `sage_room_memory` timestamps to random moments between 21 and 23 May 2026. This prevents the demo from showing every patient admitted at the same second.

**Inputs:** Reads `DB_PATH` from `config.py`.

**Outputs:** A fully re-populated `orb.db`.

**⚠️ DESTRUCTIVE. Wipes all patient data.** This is the script to run when the demo database has become stale or corrupted. Do not run it while the server is serving live traffic.

---

#### `scripts/seed_helix.py`

**One-line role:** Populates the Helix pharmacy module's master drug catalog and inventory lots with 14 drugs and 14 lot entries (several intentionally out-of-stock to demonstrate substitution logic).

**How to run:**

```bash
python scripts/seed_helix.py
```

It clears `helix_inventory_lots` and `helix_medication_master` first, then inserts fresh rows. Called automatically by `reset_db_and_seed.py`.

**Inputs:** None.

**Outputs / tables written:** `helix_medication_master`, `helix_inventory_lots`.

**⚠️ MUTATES DATABASE.** Clears both Helix tables before inserting.

---

#### `scripts/seed_relay_chats.py`

**One-line role:** Populates every patient case-room channel with a 4-message nurse/doctor conversation thread, removes the first active medication from the DB so it appears as a pending Sage action, and inserts the matching `sage_actions` suggestion card.

**How to run:**

```bash
python scripts/seed_relay_chats.py
```

Requires `seed_clinical_data.py` to have already run (it reads from `channels` and `patients`).

**Outputs / tables written:** `messages`, `sage_actions`, `sage_action_events`, `sage_room_memory`. Also deletes one row from `medications` per patient (by design, makes the UI show a pending prescription action).

**⚠️ MUTATES DATABASE.** Clears all messages and Sage actions in case-room channels before inserting fresh ones.

---

### Ingestion Replay / Test

#### `scripts/replay_ingestion.py`

**One-line role:** CLI tool for replaying fixture data files against the running server's ingestion API, used to verify that the HL7/JSON/CSV ingest pipeline works correctly.

**How to run:**

```bash
# Replay all fixture types
python scripts/replay_ingestion.py

# Replay only HL7
python scripts/replay_ingestion.py --type hl7

# Target a different server
python scripts/replay_ingestion.py --url http://192.168.1.10:8000 --key MY_KEY
```

**What it does:** Reads static fixture files from `scripts/fixtures/` (`sample.json`, `sample.hl7`, `sample.csv`) and POSTs them to `/api/ingest/json`, `/api/ingest/hl7`, and `/api/ingest/replay` respectively, then queries `/api/ingest/status` and prints a summary (event IDs, vitals inserted, duplicates, errors).

**Inputs:** Files in `scripts/fixtures/`. Requires the FastAPI server to be running.

**Outputs:** Prints to stdout. **Does write to the DB indirectly** (through the server's ingestion endpoint, vitals and observations are inserted). No direct SQLite access.

**Flags:** `--url` (default `http://localhost:8000`), `--key` (default `orb-ingest-dev-key`), `--type {json|hl7|csv|all}`.

---

#### `scripts/sage_ambient_qa_manual.py`

**One-line role:** End-to-end QA harness for the Sage Ambient Stage 1 pipeline, sends carefully crafted messages to five clinical scenarios (real messages, noisy chatter, duplicates, dept channels, mixed-risk), waits for the LLM classifier to run, then queries the DB to verify that observations, memory, and deduplication all behaved correctly.

**How to run (with server live):**

```bash
python scripts/sage_ambient_qa_manual.py
```

Requires `DOC001` and `DOC002` to exist (they are seeded by `seed_clinical_data.py`). Some test steps `sleep()` up to 45 seconds to allow the LLM to process. Prints a pass/fail report at the end.

**⚠️ MUTATES DATABASE**, inserts messages and Sage observations through the live API.

---

#### `scripts/test_transfer_flow.py`

**One-line role:** End-to-end test for the Sage surgical handoff pipeline, inserts a mock "transfer" action for Ananya Kapoor, calls `create_and_dispatch_handoff()`, polls for progressive hydration, then cleans up all test rows.

**How to run:**

```bash
python scripts/test_transfer_flow.py
```

Requires a seeded DB with Ananya Kapoor present. Cleans up after itself.

**⚠️ TEMPORARILY MUTATES DATABASE** during the test, then deletes its own rows.

---

### Model Download & Prep

#### `scripts/download_medgemma_4b.py`

**One-line role:** Downloads the `google/medgemma-1.5-4b-it` model weights from Hugging Face into `server/models/medgemma-1.5-4b-it/`.

**How to run:**

```bash
export HF_TOKEN=hf_...
python scripts/download_medgemma_4b.py
```

**Inputs:** `HF_TOKEN` environment variable (mandatory, the script exits immediately if unset).

**Outputs:** ~8 GB of model weights in `server/models/medgemma-1.5-4b-it/`.

**⚠️ Downloads ~8 GB.** Do not run on a metered connection. The model requires an accepted Hugging Face license agreement on `google/medgemma-1.5-4b-it`.

On failure it prints the exception type, details, and `__cause__` (there was
previously a near-identical `download_medgemma_4b_v2.py` whose only difference
was the `__cause__` output; the two were merged).

---

#### `scripts/download_new_model.py` (root utility, `server/`)

**One-line role:** Downloads `google/medgemma-1.5-4b-it` to `server/models/medgemma-1.5-4b-it/`, optionally deleting any previously downloaded base or fused model directories first.

**How to run:**

```bash
export HF_TOKEN=hf_...
python download_new_model.py
```

This is the **recommended** download script for a clean installation because it includes a cleanup step that removes the `medgemma-1.5-4b-it-fused` directory if present.

**⚠️ Downloads ~8 GB. Can delete existing model directories.**

---

#### `scripts/download_physionet.py`

**One-line role:** Downloads the first 2,000 patient `.psv` (pipe-separated vitals) files from the PhysioNet 2019 Sepsis Detection Challenge dataset into `server/datasets/physionet_2019/`.

**Background, PhysioNet:** PhysioNet is a public repository of de-identified clinical datasets. The 2019 Sepsis Challenge dataset contains hourly ICU vital-sign telemetry for thousands of real (anonymized) patients, making it a valuable source of realistic time-series data for training the Vigil risk model. Each file is one patient's stay.

**How to run:**

```bash
python scripts/download_physionet.py
```

Uses 16 concurrent threads. Already-downloaded files are skipped (cached check by file size > 0). Warns if more than 10% of files fail.

**Inputs:** None (public dataset, no token required).

**Outputs:** Up to 2,000 `.psv` files in `server/datasets/physionet_2019/`. Each file is named `p000001.psv` … `p002000.psv`.

**⚠️ Downloads potentially hundreds of MB.** Run once; subsequent runs skip cached files quickly.

---

#### `scripts/download_sample_images.py`

**One-line role:** Downloads four Wikimedia public-domain medical images (chest X-ray, brain MRI, CT scan, ECG) into `server/uploads/` for use as demo media attachments.

**How to run:**

```bash
python scripts/download_sample_images.py
```

Already-present files are skipped. Total download is a few MB.

---

#### `scripts/prep_llm_dataset.py`

**One-line role:** Converts the MTSamples clinical transcription CSV into `training_data/llm/train.jsonl` and `valid.jsonl` in a format suitable for MedGemma fine-tuning via MLX LoRA.

**Background, MTSamples:** MTSamples is a publicly available collection of de-identified medical transcription reports. The script wraps each `(specialty, transcription, description)` triplet in a `<system>/<user>/<model>` template and writes the resulting JSON objects to JSONL files, with a 90/10 train/validation split.

**How to run:**

```bash
python scripts/prep_llm_dataset.py
```

**Inputs:** `server/datasets/mtsamples/mtsamples.csv` (must be pre-downloaded separately).

**Outputs:** `server/training_data/llm/train.jsonl`, `server/training_data/llm/valid.jsonl`.

---

#### `scripts/prep_audio_dataset.py`

**One-line role:** Streams two Hugging Face medical ASR datasets (`intelmedica/medical-tts-parquet-2-16khz` and `ekacare/eka-medical-asr-evaluation-dataset`), saves up to 10,000 samples each as WAV files, and writes companion JSONL metadata files for Whisper fine-tuning.

**How to run:**

```bash
python scripts/prep_audio_dataset.py
```

**Inputs:** Downloads datasets on demand from Hugging Face (no token required). Cached under `server/datasets/`.

**Outputs:** WAV files under `server/training_data/audio/<dataset_name>/wavs/` and metadata at `server/training_data/audio/<dataset_name>/train.jsonl`.

**⚠️ Can download several GB of audio data.**

---

### Fine-Tuning

#### `scripts/finetune_voice_agent.py`

**One-line role:** Full QLoRA fine-tuning harness for MedGemma 4B on Apple Silicon, prepares training data from MTSamples and Indian drug brand Q&A pairs, then launches `mlx_lm lora` for parameter-efficient fine-tuning.

**Background, fine-tuning and QLoRA:** Fine-tuning means continuing a model's training on task-specific data so it better matches your use case. QLoRA (Quantized Low-Rank Adaptation) is an efficient method that only trains a small set of adapter weights rather than the full model, making it feasible on consumer hardware. The result is a small `adapters.safetensors` file that is loaded alongside the frozen base model at inference time.

**How to run:**

```bash
# Prepare data and train (100 iterations, batch 4, 16 LoRA layers)
python scripts/finetune_voice_agent.py

# Prepare data only: no training
python scripts/finetune_voice_agent.py --prepare-only

# Custom training parameters
python scripts/finetune_voice_agent.py --iters 200 --batch-size 2 --layers 8
```

**What it does:**

1. **Builds training data** (`build_finetuning_data`):
   - Reads up to 250 MTSamples transcriptions from `server/datasets/mtsamples/mtsamples.csv`
   - Generates two training examples per transcription (SOAP summarization + diagnosis extraction)
   - Appends 17 hardcoded Indian brand-name drug Q&A pairs (Dolo 650, Crocin, Monocef, etc.)
   - Shuffles all examples and writes a 90/10 train/validation JSONL split to `server/datasets/mtsamples/processed/`

2. **Runs QLoRA** via `python -m mlx_lm lora ...` as a subprocess with streaming output. Default: 100 iterations, batch size 4, 16 LoRA layers, max sequence length 256 tokens, gradient checkpointing enabled.

**Inputs:** `server/datasets/mtsamples/mtsamples.csv` (must exist). `server/models/medgemma-1.5-4b-it/` (must exist and contain model weights).

**Outputs:** `server/datasets/mtsamples/processed/train.jsonl`, `valid.jsonl`, and (after training) `adapters.safetensors` in the working directory.

**⚠️ Compute-intensive.** Even 100 iterations will take many minutes on an 8 GB Mac Mini. Use `--iters 20` for a quick smoke-test. Do not run while Ollama is serving the model (they will compete for GPU memory).

---

### Benchmarks

#### `scripts/benchmark_asr.py`

**One-line role:** Measures and compares the latency of the Whisper transcription pipeline in three modes: cold subprocess, warm ZMQ daemon, and RxNorm phonetic correction.

**How to run:**

```bash
python scripts/benchmark_asr.py                         # All benchmarks
python scripts/benchmark_asr.py --daemon-only           # Daemon only
python scripts/benchmark_asr.py --rxnorm-only           # Corrector only
python scripts/benchmark_asr.py --audio path/to/file.wav # Real audio file
```

**What it benchmarks:**

- **Subprocess worker** (baseline): Spawns `transcribe_worker.py` as a fresh subprocess for each transcription call. Runs 3 iterations and reports average wall-clock time. This is the "cold start" path.
- **ZMQ daemon** (warm path): Sends audio path over the ZMQ IPC socket to a long-running Whisper daemon process. Runs 5 iterations. Requires the daemon to already be started. Reports both client-side and server-side timing.
- **RxNorm corrector**: Runs 100 iterations of phonetic drug-name correction across 5 test phrases. Reports avg, P95, and P99 latency in milliseconds.

If no `--audio` file is provided, a 3-second silent WAV is generated in a temp directory and deleted after the run.

**Inputs:** Running server not required. ZMQ daemon must be running for daemon benchmark. RxNorm benchmark requires `datasets/rxnorm/rxnorm_drugs.db`.

**Outputs:** Prints timing report to stdout. No DB writes.

---

#### `scripts/benchmark_wer.py`

**One-line role:** End-to-end Word Error Rate (WER) benchmark for the clinical Whisper pipeline, comparing baseline ASR, context-biased ASR, and fully corrected ASR on four Indian drug-name test cases.

**Background, WER:** Word Error Rate measures how accurately a speech recogniser transcribes audio. It is calculated as the number of word-level insertions, deletions, and substitutions divided by the number of words in the reference transcript. WER = 0% means perfect; WER = 100% means no correct words. Lower is better.

**How to run (macOS only, uses the built-in `say` command):**

```bash
python scripts/benchmark_wer.py
```

**What it does:**

1. For each of 4 clinical test cases (Dolo, Pantocid, Monocef, Combiflam), the script:
   - Seeds a temporary patient and medications into `orb.db` to enable context-hint building
   - Synthesizes a WAV audio file from the test sentence using macOS `say`
   - Runs three transcription passes: (a) no hints, no RxNorm correction; (b) patient context hints fed to Whisper; (c) full pipeline with RxNorm phonetic correction
   - Computes WER for each pass using a Levenshtein distance implementation
   - Cleans up the temporary patient from the DB
2. Prints a summary table showing WER improvement per case and averages.

**Inputs:** Running server not required; calls scribe/RxNorm services directly. Reads from `orb.db`. Requires `mlx-whisper` or the ZMQ daemon.

**Outputs:** WER report printed to stdout. **Temporarily mutates `patients` and `medications` tables** (cleans up after each case).

---

#### `scripts/scribe_benchmark.py`

**One-line role:** Benchmarks the full Scribe note-generation pipeline (correction → draft → verify) against JSON fixture files, measuring latency and checking that clinical entities and SOAP sections are preserved.

**How to run:**

```bash
python scripts/scribe_benchmark.py
python scripts/scribe_benchmark.py --fixtures scripts/fixtures/scribe/
python scripts/scribe_benchmark.py --correction-model gemma3:4b --draft-model orb-sage
python scripts/scribe_benchmark.py --enable-verify --output results.json
```

**What it does:** Loads all `.json` fixtures from the fixtures directory. Each fixture specifies a `transcript`, a list of `expected_entities`, and `expected_sections`. For each fixture the script:
1. Runs the correction stage (`correct_transcript`) if enabled
2. Runs the drafting stage (`structure_transcript`) to produce a SOAP note
3. Optionally runs the verification stage (`verify_note`)
4. Measures per-stage latency and checks entity preservation (were all expected clinical terms present in the note?) and section completeness (do all expected SOAP sections appear?).
5. Prints a per-case report and an aggregate summary.

**Flags:** `--correction-model`, `--draft-model`, `--verify-model` (override Ollama models for this run only), `--enable-verify` (enable the verify stage), `--disable-correction`, `--output <file.json>` (write full JSON results).

**Inputs:** Fixture JSON files; running Ollama instance. **No DB writes.**

---

### Helix Data

#### `scripts/populate_helix_from_medgemma.py`

**One-line role:** For every admitted patient in the DB, queries the local MedGemma model via Ollama to suggest 3 medications, then inserts those medications into the Helix pharmacy catalog, inventory, patient med orders, and the Vigil `medications` table, achieving full Helix/Vigil sync from a single run.

**How to run (Ollama must be running with medgemma:4b pulled):**

```bash
python scripts/populate_helix_from_medgemma.py
```

**What it does, step by step:**

1. **Clears** `medications`, `helix_patient_med_orders`, `helix_medication_master`, `helix_inventory_lots`, `helix_stock_movements`, and `helix_transitions`, a full wipe of pharmacy data.
2. **Fetches all admitted patients** with their diagnosis and chief complaint.
3. **For each patient**, calls `query_medgemma_for_drugs(diagnosis, chief_complaint)`, a POST to `http://localhost:11434/api/generate` with a structured JSON prompt asking for 3 medications. If Ollama times out (10-second timeout) or fails, a hardcoded `FALLBACK_MEDS` dictionary (keyed by diagnosis string) is used instead. There is a catch-all fallback of Paracetamol + Pantoprazole.
4. **For each suggested medication:**
   - Inserts into `helix_medication_master` (if not already present, identified by a truncated slug ID like `MED-PARACETAMOL-650MG`)
   - Inserts into `helix_inventory_lots` with 100 units of stock
   - Inserts into `helix_patient_med_orders` with a staggered status: first med = `delivered`, second = `verified`, third = `pending`
   - Inserts a `helix_transitions` audit log entry
   - Inserts into `medications` (Vigil side) as `active`
5. **Updates `patient_profiles.current_medications`** with the new medication list.

**Inputs:** `orb.db` with admitted patients (requires `seed_clinical_data.py` to have run). Ollama accessible at `localhost:11434`.

**Outputs / tables written:** `helix_medication_master`, `helix_inventory_lots`, `helix_patient_med_orders`, `helix_transitions`, `medications`, `patient_profiles`.

**⚠️ DESTRUCTIVE, clears all Helix and medication tables before inserting.** This is an expensive script (one LLM call per patient, ~30 calls for a full seed). Runtime is 3–10 minutes depending on hardware.

---

#### `scripts/add_indian_drugs.py`

**One-line role:** Appends 20 Indian brand-name drugs (Dolo, Crocin, Monocef, Pantocid, etc.) to the RxNorm dataset CSV and rebuilds the RxNorm SQLite database used by the phonetic drug-name corrector.

**How to run:**

```bash
python scripts/add_indian_drugs.py
```

**Inputs/Outputs:** Reads `server/datasets/rxnorm/rxnorm_drugs.csv` and writes back to it; rebuilds `server/datasets/rxnorm/rxnorm_drugs.db`. Run this once after initial setup, or whenever you add new brand names to the corrector.

**⚠️ Writes to `rxnorm_drugs.csv` and `rxnorm_drugs.db`.** These are data files, not the main orb.db, so the risk is low, but a backup is wise.

---

### Model Download & Prep (continued)

#### `scripts/transcribe_worker.py`

**One-line role:** Standalone Whisper transcription worker, run as a child subprocess by `scribe_service.py` to isolate the MLX/tiktoken memory space from the FastAPI server process.

This is a **worker process**, not a script you run by hand. A worker is a separate program launched to do one job and exit, keeping its heavy dependencies out of the main process.

**How it is called (by the server):**

```bash
python scripts/transcribe_worker.py <audio_path> [model_name] [hints] [batch_size] [quant]
```

**What it does:**
1. Converts the input audio to 16 kHz mono WAV using `ffmpeg` (handles WebM, M4A, etc.)
2. Loads `LightningWhisperMLX` with the specified model (default `large-v3`)
3. Prepends a generic clinical prompt plus any patient-specific hint words to bias Whisper's beam search toward medical terminology
4. Prints the JSON result to stdout delimited by `###RESULT###` markers (the server reads stdout and parses between these markers)
5. Cleans up the temporary WAV file

**Outputs:** JSON on stdout in format `{"text": "..."}`. No DB writes.

---

### Small Model / Schema Files (`server/models/`)

These three files are thin, they define Pydantic v2 schema classes for API request and response validation, plus a package docstring.

---

#### `models/user.py`

**One-line role:** Defines the three Pydantic schemas used by the authentication API.

| Class | Used for |
|---|---|
| `UserLogin` | Body of `POST /api/auth/login`, contains `employee_id` and `password` |
| `UserOut` | Public-safe user representation returned in login responses, `id`, `name`, `role`, `department`, `registration_number` |
| `TokenResponse` | The full login success response, `token` (JWT string) + `user` (a `UserOut`) |

`department` and `registration_number` are `Optional[str]` because admin and patient accounts may not have them.

---

#### `models/message.py`

**One-line role:** Defines the single Pydantic schema used when a client sends a chat message.

| Class | Used for |
|---|---|
| `MessageCreate` | Body of `POST /api/relay/channels/{channel_id}/messages`, `content` (stripped, 1 to 50,000 characters, `MAX_MESSAGE_CHARS`) and `message_type`, which accepts `"text"` only. *(Earlier printings listed `"alert"`, `"system"`, `"ambient_action"` and `"surgical_alert"` too: the server writes those itself, and taking the type from the request let a clinician forge a system line, an alert or a hidden action card.)* |

---

#### `models/__init__.py`

**One-line role:** Package docstring only, notes that this package contains both Pydantic API schemas and neural-network architecture classes (`vigil_cfc.py`, `forecast_lstm.py`), and that the architectures were reconstructed after a machine migration (original source lost; checkpoints must be regenerated).

No imports are re-exported from this `__init__.py`.

---

### Misc Root Utilities (`server/`)

These scripts live at `server/` root rather than `server/scripts/`. Most are quick one-off diagnostic and debug tools written informally.

---

#### `server/cleanup_data.py`

**One-line role:** Keeps `clinical_documents` and `lens_analyses` within reasonable bounds, deletes excess rows beyond 3 per patient per day and enforces a global cap of 20 total for each table.

**How to run:**

```bash
python cleanup_data.py
```

Safe to run periodically (e.g., after a long demo session). Prints before/after counts.

**⚠️ DELETES rows** from `clinical_documents` and `lens_analyses`.

---

#### `server/add_test_action.py`

**One-line role:** Inserts one hardcoded Sage prescribe action (`Aspirin 81mg` for `case-PAT001`) into the `sage_actions` table, a two-line debug fixture.

**⚠️ WRITES to `sage_actions`.**

---

#### `server/get_pat.py`

**One-line role:** Logs in as `DOC001`, calls `GET /api/vigil/patients`, and prints the count and first patient, a quick smoke-test to confirm the API is up and returning data.

---

#### `server/get_room.py`

**One-line role:** Queries the DB directly and prints the `id` of the first channel that has a `patient_id`, useful for finding a valid room ID to paste into other debug commands.

---

#### `server/dump_html.py`

**One-line role:** Launches a headless Playwright browser, navigates the frontend to a patient case room for "Manoj Tiwari", and saves the rendered page HTML to `sidebar_dump.html`, a UI debugging tool for inspecting rendered state when the browser inspector is not convenient.

Requires Playwright (`pip install playwright && playwright install chromium`) and the frontend dev server running on port 5173.

---

#### `server/download_new_model.py`

(Covered above in the Model Download section, this is the recommended single-command model downloader with old-model cleanup.)

---

#### `server/time_llm_queries.py`

**One-line role:** Measures and prints Time-to-First-Token (TTFT) and total streaming latency for three representative LLM calls: Vigil patient analysis, a general Sage medical question, and a patient-context Sage query.

**How to run (server must be live with a patient loaded):**

```bash
python time_llm_queries.py
```

No DB writes. Output is latency numbers and chunk counts printed to stdout. Hardcoded to patient `PAT021` and `DOC001` credentials.

---

#### `server/time_pipeline.py`

**One-line role:** Measures Scribe pipeline latency from within the server process, calls `structure_transcript_stream()` directly (no HTTP), times TTFT and total stream duration, then calls `verify_note_content()` and reports its duration.

**How to run (from `server/` directory):**

```bash
python time_pipeline.py
```

Calls `init_db()` at import time. No DB writes beyond schema init.

---

#### `server/time_verify.py`

**One-line role:** Isolated benchmark for the `verify_note()` coroutine only, calls it with a fixed transcript and structured note, prints elapsed time and the verification result JSON.

**How to run:**

```bash
ORB_SCRIBE_VERIFY_ENABLED=true python time_verify.py
```

The environment variable must be set because verification is disabled by default (it adds ~5 seconds per note with the 4B model).

---

### Diagnostic / Inspect Scripts

These are lightweight one-time investigation scripts with no long-term maintenance role.

| Script | What it does |
|---|---|
| `scripts/test_medgemma_questions.py` | Loads MedGemma 4B locally (via `mlx_lm` or HuggingFace Transformers fallback) and runs 16 clinical questions (6 basic, 5 complex, 5 USMLE-style) to validate model capability and inference speed. No DB access. |
| `scripts/test_indian_drugs.py` | Runs 6 misspelled Indian drug-name phrases through `RxNormCorrector.correct()` and prints original vs corrected text plus edit distance. Validates the phonetic corrector works after `add_indian_drugs.py`. |
| `scripts/inspect_ds.py` | Streams the first item from both audio datasets (`intelmedica/medical-tts` and `ekacare/eka-medical-asr`) and prints column names, used to figure out the dataset schema before writing `prep_audio_dataset.py`. |
| `scripts/inspect_ds2.py` | Variant of `inspect_ds.py` that also casts the audio column using `_cast_storage` to debug a HuggingFace audio decode error. |
| `scripts/test_audio.py` | Streams the first item from `ekacare/eka-medical-asr-evaluation-dataset` test split and prints the audio object type, attributes, and the first 10 samples of the array, raw format investigation. |
