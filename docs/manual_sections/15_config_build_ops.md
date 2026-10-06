## 15. Setup, Configuration, Build & Operations

This section is a complete runbook for getting Orb running from a fresh clone, understanding every
configuration knob, and knowing what the build tooling does. An intern with basic Python and
JavaScript knowledge should be able to follow it without guessing.

---

### 15.1 Key Concepts in Plain English

**Environment variable**, a named value the OS passes to a running process. In Orb, all
tunable settings (secrets, model names, ports) are environment variables prefixed with `ORB_`.
You set them in your shell before starting the server, or store them in a `.env` file (which is
gitignored, never commit it). This means you can change server behaviour without touching any
source code.

**Virtual environment (venv)**, an isolated folder (`.venv/`) that holds a private copy of Python
and all its packages. It means Orb's dependencies never clash with other Python projects on the
same machine. You "activate" it so your `python` and `pip` commands point inside the folder.

**uv**, a very fast Python package manager and venv tool written in Rust, made by Astral. It
replaces the slower `pip + venv` workflow. Think of it as `pip` and `python -m venv` rolled into
one command that runs 10-100x faster. Orb uses it exclusively; never use `pip install` directly
inside the Orb server.

**nvm**, Node Version Manager. Different projects need different Node.js versions. nvm lets you
install several versions and switch between them without administrator rights. Orb requires
Node 22 (because Vite 8 requires Node 20+).

**Ollama**, a local server that downloads and runs large language models (LLMs) on your machine.
Orb sends HTTP requests to it (at `http://localhost:11434`) rather than calling a cloud API, so
patient data never leaves the hospital. Think of Ollama as a local OpenAI-API-compatible server
that uses your GPU/Apple Neural Engine.

**MLX**, Apple's open-source machine-learning framework optimised for Apple Silicon (M-series
chips). Orb uses it to run Whisper (speech-to-text) fast on an M2/M3/M4 Mac because MLX
compiles operations directly for the unified memory architecture instead of copying data between
CPU and GPU RAM. You do not write MLX code; it runs automatically when the right packages are
installed.

**Pinned dependency versions**, `requirements.txt` specifies exact versions (e.g.
`transformers==4.49.0`). This prevents a future `pip install` from pulling in a newer version
that breaks something. See §15.3 for the specific reasons behind each pin.

**Dev vs prod deployment profiles**, Orb has two operating modes controlled by
`ORB_DEPLOYMENT_PROFILE`. In `dev` mode it uses small, fast 4B models (runs on the 8 GB Mac
Mini). In `prod` mode it uses the full 27B model and enforces security checks (missing secrets
crash the server at startup rather than silently using insecure defaults).

**JWT secret**, JSON Web Tokens are the login tokens Orb issues after a user authenticates.
The `JWT_SECRET` is a private key used to sign them. If someone knows the secret they can forge
tokens and log in as any user. It must be a long random string, set only via `ORB_JWT_SECRET`,
never hardcoded.

**CORS**, Cross-Origin Resource Sharing. Browsers block a web page from calling an API on a
different domain/port unless the server explicitly allows it. Orb's FastAPI backend returns
CORS headers listing which frontend origins (e.g. `http://localhost:5173`) are permitted.
Setting `ORB_CORS_ORIGINS` tightly in production prevents other websites from hitting the API
from inside a clinician's browser.

---

### 15.2 Environment Variable Reference

All variables are read in `server/config.py`. None are required to start in `dev` profile; all
marked **required in prod** will raise a `ValueError` and prevent startup if missing when
`ORB_DEPLOYMENT_PROFILE` is `prod`.

#### 15.2.1 Core / Server

| Variable | Default | What it controls | When to set it |
|---|---|---|---|
| `ORB_DEPLOYMENT_PROFILE` | `prod` | `dev` → uses 4B models, skips secret checks. `prod` → enforces secrets, uses 27B model. | Set to `dev` on any machine with < 32 GB RAM. |
| `ORB_HOST` | `127.0.0.1` | The network interface the FastAPI server binds to. `127.0.0.1` = local only. `0.0.0.0` = accessible on the LAN. | Set to `0.0.0.0` only when other devices (bedside tablets) need to reach the server. ⚠️ **Corrected 2026-08-15, TLS exists and is opt-in, not absent.** Set `ORB_TLS_CERT` + `ORB_TLS_KEY` and `main.py`'s `uvicorn.run` serves HTTPS directly (`server/main.py:1361-1373`); the prod profile logs a warning if they are unset. Otherwise terminate TLS at a reverse proxy. Binding `0.0.0.0` **without** either exposes cleartext HTTP + PHI on the LAN. |
| `ORB_TLS_CERT` / `ORB_TLS_KEY` | *(none)* | Paths to a PEM certificate and key. When **both** are set, uvicorn serves HTTPS directly (`ssl_certfile` / `ssl_keyfile`). | Set in any deployment reachable beyond localhost, unless a reverse proxy terminates TLS. `install.sh` generates a self-signed **dev** cert; replace it for production. See `docs/DEPLOYMENT.md` §3. |
| `ORB_PORT` | `8000` | TCP port for the FastAPI server. | Only change if 8000 is taken by another service. |
| `ORB_CORS_ORIGINS` | `http://localhost:5173, http://localhost:5174, http://127.0.0.1:5173` | Comma-separated list of frontend origins the browser is allowed to call the API from. | In production, set to the real HTTPS URL of the frontend (e.g. `https://orb.yourhospital.internal`). Never leave as `*`. |
| `ORB_ALLOWED_HOSTS` | *(empty)* | Extra host names Orb answers to, added to the ones it derives itself (`localhost`, the hosts in `ORB_CORS_ORIGINS` and `ORB_PUBLIC_APP_BASE_URL`, the machine's own name). A request whose `Host` is any other name gets a 400, which stops a DNS-rebinding page; any IP address passes. | Leave empty unless staff or a device reach Orb by a name in none of those, or you open the Vite dev server through a tunnel such as ngrok (`vite.hosts.ts` uses the same list). Never `*` on a ward. |
| `ORB_DB_PATH` | `server/orb.db` | Path to the SQLite database file. | Override in tests to point at a temp file so tests never touch the live DB. ⚠️ **Corrected 2026-08-15:** this row used to read *"The DB holds PHI in cleartext"*. With `ORB_DB_ENCRYPTION_KEY` set, the file **and its `-wal`/`-shm` sidecars** are SQLCipher-encrypted (§2.10). Without it, the cleartext warning stands and the server logs a one-time warning rather than refusing to boot. Keep it on an encrypted volume regardless. |
| `ORB_DB_BACKEND` | `sqlite` | Database backend selection. | Only `sqlite` is wired. `postgres`/`postgresql`/`pg` are recognised and **fail fast at import** with an actionable message (`server/db_backend.py:24-45`) rather than silently using the wrong store for a PHI database. |
| `ORB_MAX_JSON_BODY_BYTES` | `524288` (512 KB) | Ceiling on non-multipart request bodies; over it → **413** (`server/main.py:708-723`). | Raise only if a legitimate JSON route needs it. Uploads are multipart and bounded by their own routes. |

#### 15.2.2 Authentication & Security

| Variable | Default | What it controls | When to set it |
|---|---|---|---|
| `ORB_JWT_SECRET` | *(none, crashes in prod)* | The HMAC-SHA256 signing key for all user session tokens. | **Required in prod.** Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. Store in a secrets manager or encrypted `.env`. ⚠️ Never commit this value. |
| `ORB_INGESTION_API_KEY` | *(none, crashes in prod)* | API key that protects the `/api/ingest/*` endpoints (real patient data write path). | **Required in prod.** Generate the same way as the JWT secret. Must be sent by HL7/EHR integrations in the `X-Ingestion-Key` header. |
| `ORB_DB_ENCRYPTION_KEY` | *(none)* | **Encryption at rest for `orb.db`** via SQLCipher (`server/config.py:71`, `server/database.py:875-893`). | **Strongly recommended in prod.** `openssl rand -hex 32`. A pre-existing plaintext DB must be migrated once: `scripts/encrypt_db.py --migrate`. Unset ⇒ plaintext with a one-time log warning, **not** a boot failure. See §2.10 for the honest limitation (key on the same disk). |
| `ORB_UPLOAD_ENCRYPTION_KEY` | *(none)* | **Encryption at rest for uploaded PHI media** (Lens images, Scribe audio) with AES-256-GCM (`server/config.py:74`, `server/services/crypto_service.py`). | Set alongside the DB key. Reads are format-sniffing, so enabling it does not orphan existing plaintext files. |
| `ORB_MFA_ISSUER` | `Orb Hospital OS` | Issuer name shown in authenticator apps (`server/config.py:61`). | Set to the hospital's name. |
| `ORB_MFA_REQUIRED_ROLES` | *(empty)* | Comma-separated roles for which TOTP MFA is required once enrolled (`server/config.py:63-65`). | ⚠️ Empty by default, **MFA is optional per user until a role is named here.** Note the self-enrolment ticket header must survive CORS; see §1. |
| `ORB_DEVICE_PIN_SESSION_MINUTES` | `15` | Server-side lifetime of a PIN-on-device session (`server/auth.py:65`). | Lower for a ward where tablets are left unattended. This is the floor enforced by the token's `exp`; the SPA also arms a shorter idle timer. |

#### 15.2.3 AI Models: LLM & Vision

| Variable | Default | What it controls | When to set it |
|---|---|---|---|
| `ORB_LLM_MODEL` | `medgemma:27b` | The Ollama model tag for all LLM inference (Sage, Bridge). | Set to `alibayram/medgemma:4b` (or similar 4B tag) on the 8 GB Mac Mini; the 27B model won't load in 8 GB. |
| `ORB_VISION_MODEL` | *(same as `ORB_LLM_MODEL`)* | Ollama model tag for image analysis (Lens). Shares the same running model instance as the LLM to avoid loading two models in VRAM. | Only override if you want a separate vision model. |
| `ORB_OLLAMA_KEEP_ALIVE` | `1h` | How long Ollama keeps the model loaded in VRAM after the last request. Can be seconds (int) or a duration string like `1h`. | Lower (e.g. `5m`) on low-RAM machines to free VRAM sooner. Higher on prod servers for lower latency. |

#### 15.2.4 Scribe Pipeline (Speech-to-Text)

The Scribe pipeline has four stages, each independently configurable. The `dev` profile defaults
to small/fast models; `prod` defaults to full-quality.

| Variable | Default (dev / prod) | What it controls |
|---|---|---|
| `ORB_SCRIBE_ASR_MODEL` | `large-v3` | Whisper model size for transcription. |
| `ORB_SCRIBE_ASR_PROVIDER` | `whisper-mlx` | ASR backend. Currently `whisper-mlx` (Apple MLX). |
| `ORB_SCRIBE_ASR_BATCH_SIZE` | `12` | Inference batch size for Whisper. Reduce on low-RAM machines. |
| `ORB_SCRIBE_ASR_QUANT` | *(empty, no quant)* | Optional quantisation level (e.g. `4bit`) for the Whisper model. |
| `ORB_SCRIBE_CORRECTION_ENABLED` | `true` | Toggle the LLM correction stage (fixes transcription errors). |
| `ORB_SCRIBE_CORRECTION_MODEL` | `medgemma-1.5-4b-it` (dev) / `medgemma:27b` (prod) | Model for the correction stage. |
| `ORB_SCRIBE_CORRECTION_PROVIDER` | `mlx` (dev) / `ollama` (prod) | Inference backend for correction. |
| `ORB_SCRIBE_CORRECTION_TEMP` | `0.1` | Temperature for correction LLM (low = deterministic). |
| `ORB_SCRIBE_CORRECTION_MAX_TOKENS` | `2048` | Max output tokens for correction. |
| `ORB_SCRIBE_CORRECTION_CTX` | `8192` | Context window size for correction. |
| `ORB_SCRIBE_DRAFT_MODEL` | `medgemma:27b` | Model for structured note drafting (always 27B for quality). |
| `ORB_SCRIBE_DRAFT_PROVIDER` | `ollama` | Backend for the draft stage. |
| `ORB_SCRIBE_DRAFT_TEMP` | `0.3` | Temperature for draft LLM. |
| `ORB_SCRIBE_DRAFT_MAX_TOKENS` | `4096` | Max output tokens for drafting. |
| `ORB_SCRIBE_DRAFT_CTX` | `8192` | Context window for drafting. |
| `ORB_SCRIBE_VERIFY_ENABLED` | `false` | Toggle the hallucination-verification stage. Off by default (adds latency). |
| `ORB_SCRIBE_VERIFY_MODEL` | `medgemma-1.5-4b-it` (dev) / `medgemma:27b` (prod) | Model for verification. |
| `ORB_SCRIBE_VERIFY_PROVIDER` | `mlx` (dev) / `ollama` (prod) | Backend for verification. |
| `ORB_SCRIBE_VERIFY_TEMP` | `0.1` | Temperature for verification LLM. |
| `ORB_SCRIBE_VERIFY_MAX_TOKENS` | `1024` | Max output tokens for verification. |
| `ORB_SCRIBE_VERIFY_CTX` | `8192` | Context window for verification. |

#### 15.2.5 Voice Intelligence

| Variable | Default | What it controls |
|---|---|---|
| `ORB_WHISPER_SOCKET` | `ipc:///tmp/orb-whisper.sock` | IPC socket path for the Whisper streaming daemon. |
| `ORB_WHISPER_STREAMING_MODEL` | `large-v3` | Whisper model for real-time streaming ASR (separate from Scribe batch ASR). |
| `ORB_WHISPER_TIMEOUT_MS` | `5000` | Timeout in milliseconds waiting for a Whisper daemon response. |
| `ORB_WHISPER_DAEMON_ENABLED` | `true` | Enable/disable the streaming Whisper daemon process. |
| `ORB_VAD_THRESHOLD` | `0.5` | Silero VAD voice-activity detection threshold (0–1). Lower = more sensitive. |
| `ORB_VAD_MIN_SPEECH_MS` | `250` | Minimum milliseconds of detected speech before a segment is kept. |
| `ORB_VAD_MIN_SILENCE_MS` | `300` | Silence duration in ms after which a speech segment is considered ended. |
| `ORB_CORRECTOR_MAX_EDIT` | `2` | Max Levenshtein edit distance for RxNorm drug-name spelling correction. |
| `ORB_CORRECTOR_ENABLED` | `true` | Enable/disable the drug-name spelling corrector in the voice pipeline. |
| `ORB_VOICE_AGENT_MAX_TOKENS` | `256` | Max tokens for the conversational voice agent response. |
| `ORB_VOICE_AGENT_TEMP` | `0.3` | Temperature for the conversational voice agent. |
| `ORB_VOICE_AGENT_ENABLED` | `true` | Enable/disable the conversational voice agent. |

#### 15.2.6 Sage Ambient Intelligence

| Variable | Default | What it controls |
|---|---|---|
| `ORB_SAGE_AMBIENT` | `true` | Enable/disable the ambient intelligence background watcher. |
| `ORB_SAGE_AMBIENT_DEBOUNCE` | `3` | Seconds to wait after activity before triggering ambient analysis. |
| `ORB_SAGE_AMBIENT_WINDOW` | `15` | Number of recent context events fed to the ambient model. |
| `ORB_SAGE_AMBIENT_MAX_SUMMARY` | `1500` | Max characters in an ambient summary. |
| `ORB_SAGE_AMBIENT_MAX_INTENTS` | `8` | Max number of user intents tracked concurrently by ambient mode. |
| `ORB_SAGE_AMBIENT_TIMEOUT` | `30` | Seconds before an ambient inference request times out. |
| `ORB_SAGE_AMBIENT_SAMPLE_RATE` | `1.0` | Fraction of ambient events actually processed (1.0 = all; lower to throttle on low-RAM machines). |

#### 15.2.7 Ingestion & Simulation

| Variable | Default | What it controls |
|---|---|---|
| `ORB_INGESTION_MODE` | `ingestion` | `ingestion` = only real data via `/api/ingest/*` (default, safe). `simulation` = synthetic bots only (useful for demos). `hybrid` = both. |
| `ORB_AUDIT_SIMULATION_EVENTS` | `false` | If `true`, simulation bot events are written to the audit log. Keep `false` in prod to avoid polluting the audit trail with fake data. |

#### 15.2.8 Bridge / Patient Portal

| Variable | Default | What it controls |
|---|---|---|
| `ORB_DISCHARGE_ACCESS_TTL_DAYS` | `30` | How many days a discharge-access token remains valid. |
| `ORB_DISCHARGE_ACCESS_MAX_ATTEMPTS` | `5` | Max failed login attempts before the discharge access portal locks. |
| `ORB_DISCHARGE_ACCESS_LOCK_MINUTES` | `15` | Minutes the discharge portal stays locked after too many failed attempts. |
| `ORB_PUBLIC_APP_BASE_URL` | `http://localhost:5173` | Base of every patient discharge link and QR code (`<this>/discharge/<token>`). Set it to the appliance's https address (install runbook, section 8.4); the `prod` profile issues no discharge link while it is unset, not https, or names this machine. |
| `ORB_DISCHARGE_LINK_ENABLED` | `true` | Discharge links and QR codes on or off. Set `false` where no patient Wi-Fi reaches the appliance (install runbook, section 7, item 6): no link is issued in any profile and the printed summary is the handover. |

#### 15.2.9 Hospital-administration module switches

> **New 2026-08-15.** Eleven module switches, **all default ON**. `_module_enabled` (`server/config.py:562-570`) treats unset *and* blank as ON; only an explicit `0` / `false` / `off` / `no` turns a module off. `HOSPITAL_MODULE_ENV` (`server/config.py:720-733`) is the id→env-var map, and the ids are the same words used as shell tab ids in `src/shellConfig.tsx`. See §0.5 and §16.

| Variable | Default | Hides |
|---|---|---|
| `ORB_BILLING` | on | Patient billing: masters, charge capture, invoices |
| `ORB_PAYMENTS` | on | Receipts, deposits, refunds, discounts, cash counter |
| `ORB_PAYER` | on | Payer master, policies, TPA/PM-JAY claims, denials |
| `ORB_PROCUREMENT` | on | Vendors, item master, indents, POs, GRN, stock |
| `ORB_FRONTDESK` | on | Registration desk, counters, OPD queue, transfers |
| `ORB_BIOMEDICAL` | on | Asset register, PM/**calibration** schedules, AMC/CMC |
| `ORB_HOUSEKEEPING` | on | Bed turnaround, cleaning requests, linen |
| `ORB_WORKFORCE` | on | Duty roster, attendance, overtime, payroll **export** |
| `ORB_DIETARY` | on | Diet orders, meal plans, kitchen indent, NPO |
| `ORB_NABH` | on | Incident register, quality indicators, evidence export |
| `ORB_ABDM` | on | ABHA capture, care-context links, consent artefacts |

**Turning `ORB_BIOMEDICAL` off is not like the others**, and the config file says so at `server/config.py:601-612`: billing, payments and procurement are things another system can do just as well, but the calibration register is *the record that says whether the number on a monitor can be believed, and Orb READS those numbers.* A site that switches it off is choosing to keep that record somewhere Orb cannot see. Legitimate, but it *"should be a decision somebody makes out loud rather than a default."*

**Turnaround and workforce constants** (defaults a site must review, not measurements): `ORB_HK_STANDARD_CLEAN_MINUTES` 45, `ORB_HK_TERMINAL_CLEAN_MINUTES` 120, `ORB_HK_DISCHARGE_LOOKBACK_HOURS` 72, `ORB_WORKFORCE_OT_DAILY_MINUTES` 480, `ORB_WORKFORCE_LATE_GRACE_MINUTES` 10, `ORB_WORKFORCE_MAX_SPAN_MINUTES` 960 (`server/config.py:635-677`).

| Variable | Default | What it controls |
|---|---|---|
| `ORB_DISCHARGE_FINANCIAL_ENFORCE` | **off** | When off (the default), a discharge may compute, display and warn about the patient's financial position but **may not stop the discharge** (`server/config.py:760-787`). **Must stay off for the pilot**, see §0.5 for both reasons. |

#### 15.2.10 Thinking mode, model routing and the background governor

| Variable | Default | What it controls |
|---|---|---|
| `ORB_THINKING_DEFAULT` | `on` | The clinician-facing effort switch. **An absent, null or unparsable value resolves to ON** (`server/config.py:161-163`). |
| `ORB_FAST_MODE_ENABLED` | `true` | Master switch for offering the toggle at all; off pins every Sage turn to the careful model (`:167-170`). |
| `ORB_FAST_MODEL` | `medgemma:4b` | Model serving thinking-OFF. If not pulled, falls back to `LLM_MODEL`, inert but safe (`:173-201`). |
| `ORB_FAST_MODE_SAFETY_FLOOR` | `1` (on) | Routes measured-dangerous question classes to the careful model even with thinking OFF. `=0` disables it; the config comment says *"Do not do this on a pilot ward"* (`:205-215`). |
| `ORB_AMBIENT_LLM_MODEL`, `ORB_MODEL_FALLBACK_ENABLED`, `ORB_AMBIENT_BATCH_WINDOW_MS`, `ORB_AMBIENT_BATCH_MAX` | see `config.py:137-149` | Ambient tier and batching. |
| `ORB_BG_LLM_GOVERNOR`, `ORB_BG_LLM_MAX_PER_MIN`, `ORB_BG_LLM_YIELD_WAIT_SECONDS`, `ORB_BG_LLM_QUIET_AFTER_INTERACTIVE_SECONDS` | `config.py:225-254` | Background-LLM governor: keeps background generation from starving interactive turns. |
| `ORB_SAGE_HISTORY_SUMMARIZE` | `0` (off) | Sage conversation-history summarisation. |
| `ORB_SAGE_PREWARM*` | `config.py:470-478` | Sage prewarm behaviour. |

#### 15.2.11 Data-source and demo flags

| Variable | Default | What it controls |
|---|---|---|
| `ORB_DEMO_MODE` | `true` in dev, **`false` in prod** | Demo affordances (`config.py:316`). |
| `ORB_LENS_ENABLED` | `true` in dev, **`false` in prod** | Lens imaging is **off by default in production** unless an operator explicitly opts in (`config.py:323`). |
| `ORB_MONITOR_ENABLED` and the `ORB_MONITOR_*` / `ORB_HL7_DEFAULT_TZ` group | **`false`** / see `config.py:406-436` | The live bedside-monitor feed: protocol, mode, host, port, source id, timezone and clock-skew tolerance. |

> **Completeness note.** `server/config.py` reads **109 distinct `ORB_*` variables**; §15.2 as it now stands documents most but not all of them, and a handful are read outside `config.py` (`ORB_MAX_JSON_BODY_BYTES` in `main.py`, `ORB_DB_BACKEND` in `db_backend.py`, `ORB_DEVICE_PIN_SESSION_MINUTES` in `auth.py`, `ORB_TLS_*` in `main.py`, several `ORB_*` test-only vars in `tests/conftest.py`). The authoritative list is `grep -o 'ORB_[A-Z0-9_]*' server/config.py | sort -u`. See §17.

---

### 15.3 Backend Dependencies

Python packages are listed in `server/requirements.txt` and installed into `server/.venv/`.
The project uses `uv` (not `pip`) for all install operations.

#### Core Web Framework

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | 0.115.12 | The async web framework. All API routes, request validation, OpenAPI docs. |
| `uvicorn[standard]` | 0.34.2 | ASGI server that runs FastAPI. The `[standard]` extra adds `uvloop` (faster event loop) and `websockets` support. |
| `python-socketio` | 5.12.1 | Adds Socket.IO support to FastAPI for real-time bidirectional events (live vitals streaming, chat). |
| `python-multipart` | 0.0.20 | Parses `multipart/form-data` uploads (audio files to Scribe, images to Lens). Without this, FastAPI cannot accept file uploads. |

#### Authentication

| Package | Version | Purpose |
|---|---|---|
| `pyjwt` | 2.10.1 | Creates and verifies JSON Web Tokens for user sessions. |
| `passlib[bcrypt]` | 1.7.4 | Hashes and verifies passwords using bcrypt. The `[bcrypt]` extra installs the native bcrypt C library for speed. |

#### HTTP Client

| Package | Version | Purpose |
|---|---|---|
| `httpx` | 0.28.1 | Async HTTP client used to call the Ollama API (`http://localhost:11434`). FastAPI's internal `requests` is sync; `httpx` is async-native. |

#### Embeddings & AI

| Package | Version | Version pin reason |
|---|---|---|
| `FlagEmbedding` | 1.3.4 | Runs BAAI/bge-m3 embeddings locally (1024-dimensional vectors for semantic search in Sage). |
| `torch` | 2.6.0 | PyTorch, required by FlagEmbedding and the Forecast LSTM. On Apple Silicon this runs on MPS (Metal). |
| `transformers` | **4.49.0** | Hugging Face transformers library. **⚠️ Pinned to 4.49.0.** FlagEmbedding 1.3.4 calls `is_torch_fx_available()` which was removed in transformers 4.50. Upgrading transformers will break embedding loading until FlagEmbedding releases a compatible version. Do not `uv pip install transformers --upgrade`. |

#### Speech-to-Text

| Package | Version | Notes |
|---|---|---|
| `lightning-whisper-mlx` | 0.0.10 | Runs Whisper large-v3 on Apple MLX for fast on-device transcription. **⚠️ Install caveat:** this package pins `tiktoken==0.3.3` which has no pre-built Python 3.12 wheel and requires Rust to compile. See §15.5 step 4 for the workaround. |

#### Vector Search

| Package | Version | Purpose |
|---|---|---|
| `sqlite-vec` | 0.1.6 | SQLite extension that adds vector similarity search (cosine/L2) as SQL functions. Sage uses it to find clinically similar notes without a separate vector DB. |

#### Utilities

| Package | Version | Purpose |
|---|---|---|
| `aiosqlite` | 0.21.0 | Async wrapper around SQLite so database queries don't block the event loop. |
| `numpy` | 2.2.4 | Numerical arrays, used by the LSTM Forecast model and embedding calculations. |

---

### 15.4 Frontend Dependencies

Managed with `npm` in the project root (`/Users/samuelmorningstar/Orb Project/`). Node 22 is
required (install via nvm, see §15.5).

#### Runtime Dependencies (`dependencies`)

| Package | Version | Purpose |
|---|---|---|
| `react` | ^19.2.4 | UI framework. |
| `react-dom` | ^19.2.4 | Renders React into the browser DOM. |
| `react-is` | ^19.2.5 | Type-checking utilities for React elements (used by recharts). |
| `react-router-dom` | ^7.13.2 | Client-side routing between pages (Vigil, Sage, Scribe, etc.). |
| `recharts` | ^3.8.1 | Chart library built on D3, renders vitals trends, risk-score graphs, Forecast outputs. |
| `socket.io-client` | ^4.8.3 | Browser-side Socket.IO client for receiving real-time vitals and relay chat events from the backend. |

#### Dev Dependencies (`devDependencies`)

| Package | Purpose |
|---|---|
| `vite` | Build tool and dev server. Hot-module replacement during development. |
| `@vitejs/plugin-react` | Vite plugin that enables React fast-refresh (instant browser update on save without losing component state). |
| `vite-plugin-pwa` | Generates a Service Worker and Web App Manifest so Orb can be installed as a Progressive Web App on tablets in a ward. |
| `typescript` ~5.9.3 | TypeScript compiler. Vite uses it for type-checking; the compiled output is plain JS. |
| `eslint` + `typescript-eslint` | Linter. Enforces code quality rules on `.ts` / `.tsx` files. |
| `eslint-plugin-react-hooks` | Adds lint rules that catch incorrect React Hooks usage (e.g. calling hooks inside conditionals). |
| `eslint-plugin-react-refresh` | Ensures components are structured so Vite's hot-module replacement works correctly. |
| `globals` | Provides browser/node global type definitions for ESLint. |
| `@types/react`, `@types/react-dom`, `@types/node` | TypeScript type definitions, no runtime impact. |

---

### 15.5 Build & Tooling Configuration

#### Vite (`vite.config.ts`)

Vite is both the development server and the production bundler.

**Key settings:**

- **`plugins: [react(), VitePWA(...)]`**, enables React fast-refresh and generates the PWA service
  worker. The PWA manifest declares the app name "Orb Hospital OS", short name "Orb", and a dark
  background colour (`#0a0a0a`). `registerType: 'autoUpdate'` means the service worker silently
  updates itself when a new build is deployed.

- **`server.host: '0.0.0.0'`**, the Vite dev server binds to all interfaces (LAN-accessible).
  This allows nurses/doctors on the ward network to open the dev build. For a tightly controlled
  dev environment, override with `--host 127.0.0.1` on the CLI.

- **`server.allowedHosts`**, the names the dev server answers to. `vite.hosts.ts` builds the list
  from the same settings as the backend's Host check (`ORB_CORS_ORIGINS`, `ORB_PUBLIC_APP_BASE_URL`,
  `ORB_HOST`, the machine's own name and its `.local` name, plus `ORB_ALLOWED_HOSTS`; `*` there turns
  it off), read from `.env` or the environment. Vite always admits localhost and IP addresses. Any
  other name gets a 403, which stops a DNS-rebinding page from reading the app or calling `/api`
  through the proxy, where the backend only ever sees `Host: localhost:8000`. A tunnel such as
  ngrok must be listed in `ORB_ALLOWED_HOSTS`. Until 4 October 2026 this was `true` (no check).

- **`server.proxy`**, this is the key integration bridge between frontend and backend:

  | Proxy path | Backend target | Notes |
  |---|---|---|
  | `/api/*` | `http://localhost:8000` | All REST API calls |
  | `/uploads/*` | `http://localhost:8000` | Uploaded files (images, audio) |
  | `/socket.io/*` | `http://localhost:8000` (WebSocket) | Real-time Socket.IO events |

  Because of these proxies, React code calls `/api/sage/ask` and Vite forwards it to FastAPI
  during development. In production, a real reverse proxy (nginx/Caddy) does the same job.

**Production build:** `npm run build` runs `tsc -b && vite build`. TypeScript is type-checked
first, then Vite bundles and tree-shakes everything into `dist/`. The built files are static
HTML/JS/CSS that can be served by any web server.

#### TypeScript (`tsconfig.json`, `tsconfig.app.json`, `tsconfig.node.json`)

The project uses TypeScript's **project references**, three separate tsconfig files that compile
independently:

- **`tsconfig.json`**, the root config. Contains only `references` pointing to the other two.
  The TypeScript compiler processes each reference in parallel.

- **`tsconfig.app.json`**, governs the `src/` directory (the React app). Key settings:
  - `target: ES2023`, output modern JavaScript; no need to support older browsers.
  - `lib: ["ES2023", "DOM", "DOM.Iterable"]`, includes browser type definitions.
  - `moduleResolution: "bundler"`, tells TypeScript that Vite handles imports, so `.ts` extensions
    are allowed in import paths.
  - `noEmit: true`, TypeScript never writes `.js` files; Vite handles the actual compilation.
  - `strict: true`, enables all strict type checks (null checks, no implicit any, etc.).
  - `noUnusedLocals: false` / `noUnusedParameters: false`, these are intentionally relaxed for the
    app source (enforced more tightly for the config files below).

- **`tsconfig.node.json`**, governs `vite.config.ts` only. Uses `types: ["node"]` since the Vite
  config file runs in Node.js, not the browser. `noUnusedLocals: true` and
  `noUnusedParameters: true` are enforced here.

#### ESLint (`eslint.config.js`)

Uses the **flat config** format (ESLint v9+). Applied to all `*.ts` and `*.tsx` files.

Rulesets applied in order:
1. `js.configs.recommended`, baseline JS rules (no undeclared variables, etc.)
2. `tseslint.configs.recommended`, TypeScript-specific rules (no `any`, proper type imports, etc.)
3. `reactHooks.configs.flat.recommended`, enforces the Rules of Hooks
4. `reactRefresh.configs.vite`, ensures HMR works correctly

`dist/` is explicitly ignored (it's compiled output, not source).

Run linting: `npm run lint`

---

### 15.6 How to Run Orb Locally: Complete Runbook

This section takes you from a fresh `git clone` to a fully running Orb. Follow the steps in order.

**Prerequisites:**
- Apple Silicon Mac (M1/M2/M3/M4) running macOS
- Internet connection for the initial install (after that, everything is local)
- No `sudo` needed for any of these steps

#### Step 1: Install uv (Python toolchain)

`uv` is a single binary that manages Python versions and virtual environments.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# Add to shell PATH permanently:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
uv --version   # should print uv 0.x.x
```

#### Step 2: Install nvm and Node 22

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
# Reload your shell (or open a new terminal):
export NVM_DIR="$HOME/.nvm"; source "$NVM_DIR/nvm.sh"
nvm install 22
nvm alias default 22
node --version   # should print v22.x.x
```

#### Step 3: Install Ollama and pull the LLM

macOS has no headless Ollama installer, you must use the GUI:

1. Download and install Ollama from <https://ollama.com/download>
   (Alternatively: `brew install ollama` if you have Homebrew)
2. Launch the Ollama app once, it installs the CLI and starts the background server.
3. Pull the model(s) you need:

```bash
# On the 8 GB Mac Mini (dev profile: 4B only):
ollama pull alibayram/medgemma:4b

# On a 48 GB or 96 GB machine (production: full 27B):
ollama pull medgemma:27b

# ⚠️ Do NOT pull medgemma:27b on the 8 GB Mini: it requires ~17 GB VRAM and will fail to load.
```

Verify Ollama is running:
```bash
curl http://localhost:11434/api/tags   # should return a JSON list of pulled models
```

#### Step 4: Set up the Python backend

```bash
cd "/Users/samuelmorningstar/Orb Project/server"

# Install Python 3.12 and create the virtual environment:
uv python install 3.12
uv venv --python 3.12 .venv

# Install core dependencies (excluding the problematic Whisper package for now):
grep -v lightning-whisper-mlx requirements.txt > /tmp/req_core.txt
uv pip install --python .venv/bin/python -r /tmp/req_core.txt

# (Optional) Install lightning-whisper-mlx for Scribe ASR.
# This requires Rust because it compiles tiktoken 0.3.3 from source:
curl https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
uv pip install --python .venv/bin/python lightning-whisper-mlx==0.0.10
```

> If you do not need Scribe/voice features right now, skip `lightning-whisper-mlx`. All other
> modules (Vigil, Sage, Lens, Bridge, Forecast, etc.) work without it.

#### Step 5: Set environment variables

Create a file `server/.env` (gitignored, never commit this file):

```bash
# server/.env: copy and edit this block

# Required on the 8 GB Mac Mini:
ORB_DEPLOYMENT_PROFILE=dev
ORB_LLM_MODEL=alibayram/medgemma:4b

# Required on any machine in prod profile (will crash at startup if missing):
# ORB_JWT_SECRET=<generate: python -c "import secrets; print(secrets.token_hex(32))">
# ORB_INGESTION_API_KEY=<generate: python -c "import secrets; print(secrets.token_hex(32))">

# Optional overrides (defaults are fine for local dev):
# ORB_HOST=127.0.0.1
# ORB_PORT=8000
# ORB_CORS_ORIGINS=http://localhost:5173
```

Load them before starting the server. The easiest way during development:

```bash
# In the terminal where you will run the server:
export $(grep -v '^#' server/.env | xargs)
```

> **⚠️ JWT_SECRET**, in `dev` profile (`ORB_DEPLOYMENT_PROFILE=dev`), the server falls back to a
> hardcoded dev secret if `ORB_JWT_SECRET` is not set. This is acceptable for local development
> on a machine that never holds real patient data. In `prod` profile, omitting `ORB_JWT_SECRET`
> raises `CRITICAL SECURITY ERROR` and the server refuses to start. Never use the dev fallback
> with real patient data.

#### Step 6: Start the FastAPI backend

```bash
cd "/Users/samuelmorningstar/Orb Project/server"
source .venv/bin/activate

# Export env vars if you haven't already:
export ORB_DEPLOYMENT_PROFILE=dev
export ORB_LLM_MODEL=alibayram/medgemma:4b

# Start the server with hot-reload:
uvicorn main:socket_app --host 127.0.0.1 --port 8000 --reload

# Verify it's running:
curl http://localhost:8000/api/health
# Expected: {"status":"ok"} or similar
```

The server logs appear in the terminal. Key log lines to look for:
- `INFO  orb: Database initialised`, SQLite DB is ready
- `INFO  uvicorn.error: Application startup complete`, server is accepting requests
- Any `ERROR` lines indicate a missing dependency or misconfiguration

**Listening address:** `http://127.0.0.1:8000` (or `http://0.0.0.0:8000` if you set
`ORB_HOST=0.0.0.0` for LAN access)

#### Step 7: Install and start the React frontend

Open a **new terminal window** (leave the backend running):

```bash
cd "/Users/samuelmorningstar/Orb Project"

# Install npm dependencies (do this on each fresh clone or after package.json changes):
npm install

# Start the Vite dev server:
npm run dev

# Expected output:
#   VITE v8.x.x  ready in xxx ms
#   ➜  Local:   http://localhost:5173/
#   ➜  Network: http://192.168.x.x:5173/
```

Open `http://localhost:5173` in your browser. The Vite proxy will forward all `/api` calls to the
FastAPI backend at `localhost:8000`.

#### Step 8: Verify the full stack

1. Log in at `http://localhost:5173` (default dev credentials: check the database seed in
   `server/database.py` or `server/add_test_action.py`)
2. Navigate to Vigil, you should see the patient list populate
3. Navigate to Sage and ask a clinical question, Ollama should respond (first response is slow
   while the model loads into VRAM)
4. Check the backend terminal for any error logs

#### Quick-reference: ports and processes

| Process | Address | Started by |
|---|---|---|
| Ollama LLM server | `http://localhost:11434` | Ollama app (background daemon) |
| FastAPI + Socket.IO | `http://localhost:8000` | `uvicorn main:socket_app ...` |
| Vite dev server (frontend) | `http://localhost:5173` | `npm run dev` |
| Vite PWA dev service worker | Same port | Enabled automatically via `devOptions.enabled: true` |

#### Production build (frontend)

```bash
cd "/Users/samuelmorningstar/Orb Project"
npm run build          # outputs to dist/
npm run preview        # serves dist/ locally to check the build
```

The `dist/` folder contains the bundled static files. Serve them with nginx, Caddy, or any
static file server, and point `/api` and `/socket.io` proxy rules at the FastAPI backend.

---

### 15.7 Website vs App Separation

There are two separate projects on this machine:

| Project | Path | What it is |
|---|---|---|
| **Orb App** | `/Users/samuelmorningstar/Orb Project/` | The clinical hospital OS, this codebase. FastAPI + full React app. |
| **Orb Website** | `/Users/samuelmorningstar/Orb-website/` | The public marketing site for orb.health (or similar). Separate repo. |

The website (`Orb-website`) is a standalone Vite + React project with minimal dependencies
(`react`, `react-dom`, `react-router-dom` only, no `socket.io-client`, `recharts`, or PWA
plugin). It has no backend, no authentication, and no connection to the hospital app.

**Important:** the two projects share a similar folder structure (`src/`, `components/`, `pages/`)
and some visual assets may be duplicated. They are **not linked**, changes to the Orb App do not
affect the website and vice versa. Never confuse the two when running `npm install` or `npm run
dev`. Always check your terminal's working directory before running npm commands.

To run the website locally (for marketing/content work only):
```bash
cd "/Users/samuelmorningstar/Orb-website"
npm install
npm run dev   # http://localhost:5173, starts on same port as the app, so only run one at a time
```

---

### 15.8 `.gitignore` Rationale

The `.gitignore` at the project root (`/Users/samuelmorningstar/Orb Project/.gitignore`) is
deliberately broad. Here is why each major category is excluded:

#### Model weights (never commit)

```
*.safetensors  *.gguf  *.pt  *.onnx  *.bin
server/models/*/    /models/
```

Model weight files are gigabytes in size and would make the repository unusable (git is not
designed for binary blobs of this size). They are downloaded or fine-tuned locally. The source
code files inside `server/models/` (`.py` files) **are** committed, the `.gitignore` uses
negation rules (`!server/models/*.py`) to keep them while ignoring subdirectory weight folders.

#### Databases and uploaded files (⚠️ PHI)

```
*.db  *.db-wal  *.db-shm  orb.db  uploads/  server/uploads/  server/datasets/
```

> **⚠️ The database and upload folder contain Protected Health Information (PHI).** Committing
> them would be a HIPAA violation. They must stay on encrypted local storage only. If you
> accidentally stage `orb.db` or an upload, run `git rm --cached orb.db` immediately and
> do not push.

#### Environment files (⚠️ secrets)

```
.env  server/.env
```

> **⚠️ `.env` files contain `ORB_JWT_SECRET` and `ORB_INGESTION_API_KEY`.** These are
> cryptographic secrets. Committing them exposes them permanently in git history even if you
> delete them later. Use `git secret` or a secrets manager in team environments.

#### Virtualenv and build output

```
.venv/  venv/  node_modules/  dist/  dev-dist/  __pycache__/  *.py[cod]
```

These are generated locally and can be exactly recreated from `requirements.txt` and
`package.json`. Committing them wastes space and causes spurious diffs.

#### PDFs and large docs

```
*.pdf
```

PDF exports (e.g. pitch decks, reports) are regenerated from source. Large binary files create
unnecessarily large commits.

#### Vendored external repo

```
server/scripts/mlx-examples/
```

The `mlx-examples` directory is a cloned external repository (Apple's MLX examples) with its own
git history. Tracking it inside Orb's repo would create a nested repository conflict. It is
cloned locally for reference and excluded from Orb's history.

#### IDE and OS noise

```
.DS_Store  .vscode/  .idea/  *.swp  *.log
```

Machine-specific editor settings and OS metadata files differ per developer and should not be
shared.

#### Agent config

```
.claude/
```

The `.claude/` directory contains local tool state and project-specific
settings. These are machine-local and not part of the committed codebase.

---

### 15.9 Common Operations Reference

#### Restart the backend after a code change

The `--reload` flag in the uvicorn command watches for file changes and restarts automatically.
If you added a new dependency, you must re-run `uv pip install`:

```bash
uv pip install --python .venv/bin/python -r /tmp/req_core.txt
# then restart uvicorn
```

#### Add a new Python dependency

```bash
cd "/Users/samuelmorningstar/Orb Project/server"
# Add it to requirements.txt manually, then:
uv pip install --python .venv/bin/python <package>==<version>
# Commit the requirements.txt change
```

#### Add a new npm package

```bash
cd "/Users/samuelmorningstar/Orb Project"
npm install <package>
# This updates package.json and package-lock.json: commit both
```

#### Run the TypeScript type-checker standalone

```bash
cd "/Users/samuelmorningstar/Orb Project"
npx tsc --build   # checks all tsconfig references; exits non-zero if there are type errors
```

#### Switch to the 27B model (on a 48 GB+ machine)

```bash
# Unset or change the model override:
export ORB_LLM_MODEL=medgemma:27b
export ORB_DEPLOYMENT_PROFILE=prod
export ORB_JWT_SECRET="<your-secret>"
export ORB_INGESTION_API_KEY="<your-key>"
# Restart the backend
```

#### Check which model Ollama has loaded in VRAM

```bash
ollama ps   # shows running models and their memory footprint
```

#### Reset the database (development only)

```bash
# ⚠️ This deletes all data. Development only.
rm "/Users/samuelmorningstar/Orb Project/server/orb.db"
# The next server start will call init_db() and recreate the schema
```
