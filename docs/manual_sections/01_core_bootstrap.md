## 1. Core & Application Bootstrap (Entry, Config, Auth, Permissions)

# 1. Core & Application Bootstrap

This section documents the four files that form the absolute foundation of Orb. Every HTTP request, WebSocket event, and background task passes through this layer. Understand these files first; everything else in the codebase depends on them.

---

### How a Request Flows Through Orb
Before diving into individual files, here is the end-to-end journey of a typical HTTP request. Knowing this flow helps you understand why each piece exists.

```
Browser / Mobile App
        │
        │  HTTP request (e.g. GET /api/patients)
        ▼
  Uvicorn (ASGI server)          ← the Python process started by main.py
        │
        ▼
  CORSMiddleware                 ← checks the Origin header, adds CORS headers
        │
        ▼
  FastAPI router dispatch        ← matches URL to a route function
        │
        ▼
  Dependency injection chain     ← FastAPI calls dependencies listed in Depends()
        │
        ├─ HTTPBearer()          ← extracts "Authorization: Bearer <token>" header
        ├─ get_current_user()    ← verifies the JWT, looks up the user in SQLite
        └─ require_permission()  ← checks the user's role against the permission map
                │
                ▼  (if all checks pass)
         Route handler function  ← your actual business logic
                │
                ▼
         Service layer           ← e.g. sage_service, vigil_service
                │
                ▼
         SQLite database         ← raw queries via database.py / get_db()
                │
                ▼
         JSON response back to client
```

**Socket.IO** (real-time events) follows a parallel path. Instead of HTTP routes, it uses event handlers registered on the `sio` object in `ws/realtime.py`. Authentication for Socket.IO connections is handled separately at connection time, not via the HTTP dependency chain.

---

### `server/config.py`: Global Configuration

**Role:** Single source of truth for every tunable value in Orb, server binding, database path, JWT secrets, model names, pipeline settings, and feature flags. All other modules import from here.

**Key imports / libraries:**

- `os`, reads environment variables with `os.getenv()`. Environment variables are the standard way to configure software per-deployment without changing code.
- `pathlib.Path`, cross-platform file path handling. `Path(__file__).resolve().parent` means "the directory that contains this file", i.e. the `server/` folder.

---

#### Constants and Config Variables (every one documented)

**Path variables**

| Variable | Default | What it does |
|---|---|---|
| `BASE_DIR` | The `server/` directory | Root for all relative paths below |
| `DB_PATH` | `server/orb.db` | Full path to the SQLite database file. Override with `ORB_DB_PATH` env var so automated tests can point at a throwaway database instead of the live one. |
| `UPLOAD_DIR` | `server/uploads/` | Folder where Lens medical images and Scribe audio recordings are stored on disk. Created automatically at import time if it does not exist. Contains PHI (patient health information, individually identifiable medical data). |

**Server binding**

| Variable | Env var | Default | What it does |
|---|---|---|---|
| `HOST` | `ORB_HOST` | `127.0.0.1` | Network address the server listens on. The default `127.0.0.1` (loopback) means the server is only reachable from the same machine, safe. Setting `0.0.0.0` exposes it on every network interface, enabling LAN or internet access but also exposing unencrypted PHI if TLS is not in place. Change only when you have a plan for network security. |
| `PORT` | `ORB_PORT` | `8000` | TCP port number. The React frontend expects `8000` by default. |
| `CORS_ORIGINS` | `ORB_CORS_ORIGINS` | `http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173` | **CORS** (Cross-Origin Resource Sharing) is a browser security rule that blocks JavaScript on one domain from calling APIs on a different domain. This list tells FastAPI which origins are allowed. In production, lock this down to your actual frontend URL. Multiple origins are comma-separated in the env var. |
| `ALLOWED_HOSTS_EXTRA` | `ORB_ALLOWED_HOSTS` | *(empty)* | Extra host names the backend answers to, added to the set `allowed_host_names()` derives (`localhost`, the hosts in `ORB_CORS_ORIGINS` and `ORB_PUBLIC_APP_BASE_URL`, `ORB_HOST` when it is a name, the machine's own name and its `.local` form). `services/host_gate.HostGate` wraps `main.socket_app` and returns 400 for any other `Host` name, which stops DNS rebinding; any IP address passes. `*` turns the check off. |

**Authentication**

| Variable | Env var | Default | What it does |
|---|---|---|---|
| `JWT_SECRET` | `ORB_JWT_SECRET` | crashes in prod if unset | The cryptographic key used to sign and verify JWTs (JSON Web Tokens, see auth.py section). **If this value leaks, anyone can forge staff credentials.** Must be a long random string. In production the server refuses to start if this is missing. In dev/test mode a hardcoded fallback is used, never deploy that fallback. |
| `JWT_ALGORITHM` | n/a | `HS256` | The mathematical algorithm used to sign JWTs. HS256 means HMAC with SHA-256. Do not change this without also rotating all existing tokens. |
| `JWT_EXPIRY_HOURS` | n/a | `24` | How many hours a login token remains valid. After 24 hours the user must log in again. Reducing this (e.g. to 1 hour) is more secure but less convenient. |

**LLM / Ollama (the on-device AI model server)**

`Ollama` is an open-source tool that runs large language models locally. Orb uses it to run MedGemma, a medical AI model from Google. "VRAM" means video memory on the GPU, large models require significant VRAM to run.

| Variable | Env var | Default | What it does |
|---|---|---|---|
| `OLLAMA_BASE_URL` | n/a | `http://localhost:11434` | URL where Ollama listens. Hard-coded because Ollama always runs locally. |
| `LLM_MODEL` | `ORB_LLM_MODEL` | `medgemma:27b` | The main AI model used by Sage (clinical Q&A), Scribe (note drafting), and Bridge. 27B = 27 billion parameters, the large, high-quality version. Requires ~48 GB RAM or a Mac Studio. |
| `VISION_MODEL` | `ORB_VISION_MODEL` | same as `LLM_MODEL` | Model used for medical image analysis (Lens module). Defaults to the same MedGemma instance to avoid loading two large models simultaneously. |
| `OLLAMA_KEEP_ALIVE` | `ORB_OLLAMA_KEEP_ALIVE` | `1h` | How long Ollama keeps the model in memory after the last request. `1h` means the model stays warm for an hour of idle time. Can be a number (seconds) or a string like `"1h"`. |

**Embeddings (for RAG, Retrieval-Augmented Generation)**

RAG is a technique where the AI searches a knowledge base before answering, so it can cite actual documents rather than guessing. BGE-M3 converts text to a list of 1024 numbers (a "vector") that captures meaning, so similar texts have similar vectors.

| Variable | Default | What it does |
|---|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | The model used to embed text for semantic search. Used by Sage's clinical knowledge base. |
| `EMBEDDING_DIM` | `1024` | The number of dimensions in each embedding vector. Must match what BGE-M3 actually outputs, do not change unless switching models. |

**Whisper / Scribe ASR (Automatic Speech Recognition)**

Whisper is OpenAI's open-source speech-to-text model. Orb uses it via MLX (Apple's machine learning framework for Apple Silicon) to transcribe clinical dictation.

| Variable | Default | What it does |
|---|---|---|
| `WHISPER_MODEL` | `large-v3` | Which Whisper model to use. `large-v3` is the most accurate. Kept as a top-level var for backward compatibility; the `SCRIBE` dict is the authoritative config. |
| `WHISPER_BATCH_SIZE` | `12` | How many audio chunks to process at once. Higher = faster on capable hardware. |

**Deployment profile**

| Variable | Env var | Default | What it does |
|---|---|---|---|
| `DEPLOYMENT_PROFILE` | `ORB_DEPLOYMENT_PROFILE` | `prod` | Controls which defaults apply throughout the codebase. `"prod"` enforces strict security checks (secrets required, locked CORS). `"dev"` relaxes them and swaps in the faster 4B model for some pipeline stages. |
| `is_dev` | derived | n/a | Boolean shorthand: `True` if profile is `"dev"`. Used to derive defaults below. |

**Scribe pipeline configuration (`SCRIBE` dict)**

The Scribe module has a multi-stage pipeline: (1) ASR transcribes audio to raw text, (2) correction fixes medical vocabulary, (3) draft structures the note, (4) verify optionally double-checks the draft. Each stage is independently configurable.

```python
SCRIBE = {
    "asr": {
        "model": ...,       # Whisper model name
        "provider": ...,    # "whisper-mlx" (Apple Silicon) or other
        "batch_size": ...,  # audio chunks per batch
        "quant": ...,       # quantization (compression) level; None = full precision
    },
    "correction": {
        "enabled": ...,     # run medical vocabulary correction? (default: True)
        "model": ...,       # dev=medgemma-1.5-4b-it (fast), prod=27B (accurate)
        "provider": ...,    # "mlx" or "ollama"
        "temperature": ..., # randomness (0.1 = very deterministic, good for correction)
        "max_tokens": ...,  # maximum output length in tokens
        "ctx_size": ...,    # how much context (in tokens) the model can see
    },
    "draft": { ... },       # same shape; always uses 27B for quality
    "verify": {
        "enabled": ...,     # second-pass verification; default OFF (slow, increases latency)
        ...
    },
}
```

All sub-values are overridable via env vars (`ORB_SCRIBE_ASR_MODEL`, `ORB_SCRIBE_CORRECTION_ENABLED`, etc.) without changing code.

**Ingestion mode**

| Variable | Env var | Default | What it does |
|---|---|---|---|
| `INGESTION_MODE` | `ORB_INGESTION_MODE` | `"ingestion"` | Controls whether simulation bots run. `"ingestion"` = real data only, bots disabled. `"simulation"` = synthetic bots, no real data. `"hybrid"` = both. The safe default is `"ingestion"` to prevent fake data from entering a live deployment. |
| `INGESTION_API_KEY` | `ORB_INGESTION_API_KEY` | crashes in prod if unset | API key required to call `/api/ingest/*` endpoints. Protects the real-patient-data write path. Must be set in production; server refuses to start otherwise. |
| `AUDIT_SIMULATION_EVENTS` | `ORB_AUDIT_SIMULATION_EVENTS` | `false` | Whether to write simulation-bot events to the audit log. Turn on only when debugging bots. |

**Discharge access**

| Variable | Env var | Default | What it does |
|---|---|---|---|
| `DISCHARGE_ACCESS_TTL_DAYS` | `ORB_DISCHARGE_ACCESS_TTL_DAYS` | `30` | How many days a discharge summary access link remains valid. |
| `DISCHARGE_ACCESS_MAX_ATTEMPTS` | `ORB_DISCHARGE_ACCESS_MAX_ATTEMPTS` | `5` | Failed PIN attempts before lockout. |
| `DISCHARGE_ACCESS_LOCK_MINUTES` | `ORB_DISCHARGE_ACCESS_LOCK_MINUTES` | `15` | Lockout duration in minutes after too many failed attempts. |
| `PUBLIC_APP_BASE_URL` | `ORB_PUBLIC_APP_BASE_URL` | `http://localhost:5173` | The https address a patient's phone opens from the discharge link and QR code; on an appliance, the address staff type. The default is the Vite dev server: the `prod` profile issues no discharge link while it is unset, not https, or names this machine. |

**Sage ambient config**

The Sage module has an "ambient" mode that passively monitors conversations and proactively surfaces clinical insights.

| Variable | Env var | Default | What it does |
|---|---|---|---|
| `SAGE_AMBIENT_ENABLED` | `ORB_SAGE_AMBIENT` | `true` | Enable/disable ambient intelligence. |
| `SAGE_AMBIENT_DEBOUNCE_SECONDS` | `ORB_SAGE_AMBIENT_DEBOUNCE` | `3` | Wait this many seconds of silence before processing ambient input, to avoid processing incomplete sentences. |
| `SAGE_AMBIENT_CONTEXT_WINDOW` | `ORB_SAGE_AMBIENT_WINDOW` | `15` | Number of conversation turns the ambient model considers when generating suggestions. |
| `SAGE_AMBIENT_MAX_SUMMARY_CHARS` | `ORB_SAGE_AMBIENT_MAX_SUMMARY` | `1500` | Maximum character length of ambient summaries sent to the model. |
| `SAGE_AMBIENT_MAX_INTENTS` | `ORB_SAGE_AMBIENT_MAX_INTENTS` | `8` | Maximum number of detected intents/suggestions per analysis pass. |
| `SAGE_AMBIENT_TIMEOUT_SECONDS` | `ORB_SAGE_AMBIENT_TIMEOUT` | `30` | Seconds before an ambient analysis request times out. |
| `SAGE_AMBIENT_SAMPLE_RATE` | `ORB_SAGE_AMBIENT_SAMPLE_RATE` | `1.0` | Fraction of conversations that are ambientally monitored. `1.0` = 100%. Lower to reduce compute load. |

**Voice intelligence pipeline (`VOICE` dict)**

| Sub-key | What it configures |
|---|---|
| `daemon` | The streaming Whisper daemon that listens via Unix socket (`ipc:///tmp/orb-whisper.sock`) for real-time transcription. Faster than per-request model loading. |
| `vad` | Voice Activity Detection, the Silero VAD ONNX model that detects when someone is speaking vs. silence, so Whisper only runs on actual speech. Threshold `0.5` = 50% confidence required to classify as speech. |
| `corrector` | Post-transcription drug name corrector that checks transcribed words against the RxNorm drug database (a US standard medication vocabulary). Max edit distance `2` means it corrects up to 2-character spelling errors. |
| `conversational` | The MedGemma 4B voice agent loaded into Apple Silicon Unified Memory (shared CPU/GPU memory). A smaller, faster model suited to real-time back-and-forth conversation. |

**Other**

| Variable | Default | What it does |
|---|---|---|
| `VITALS_INTERVAL_SECONDS` | `5` | How often the simulation bots emit new vital sign readings (seconds). |
| `SAGE_RERANKING_ENABLED` | `False` | Whether to rerank RAG search results using a cross-encoder model. Currently disabled. |

---

#### Config groups the tables above were missing entirely

> **Added 2026-08-15.** `server/config.py` reads **109 distinct `ORB_*` variables**. The tables above cover roughly half. The rest are grouped below; the exhaustive reference is §15.2. Four of these groups are things a CIO or an auditor will specifically ask about.

**Encryption at rest**, `server/config.py:67-74`

| Variable | Env var | Default | What it does |
|---|---|---|---|
| `DB_ENCRYPTION_KEY` | `ORB_DB_ENCRYPTION_KEY` | unset | When set **and** a SQLCipher driver is importable, `orb.db` is opened encrypted (§2.10). Absent either, the app falls back to plaintext `sqlite3` **with a startup warning**, it does not refuse to boot. |
| `UPLOAD_ENCRYPTION_KEY` | `ORB_UPLOAD_ENCRYPTION_KEY` | unset | When set, uploaded PHI media (Lens images, Scribe audio) are encrypted at rest with AES-256-GCM (`services/crypto_service.py`). |

**Multi-factor auth**, `server/config.py:59-65`: `ORB_MFA_ISSUER` (default `"Orb Hospital OS"`, shown in authenticator apps) and `ORB_MFA_REQUIRED_ROLES` (comma-separated, **empty by default**, MFA is optional per user until a role is named here).

**Thinking mode, the clinician-facing effort switch**, `server/config.py:150-215`. One control a doctor understands without a manual: think harder, or answer fast.

| Variable | Default | What it does |
|---|---|---|
| `ORB_THINKING_DEFAULT` | `on` | **The default is ON, and an absent or unparsable flag resolves to ON** (`:161-163`). An old client, a scripted caller or a dropped JSON field must never quietly downgrade a clinician's answer. |
| `ORB_FAST_MODE_ENABLED` | `true` | Master switch for offering the toggle at all. OFF pins every Sage turn to the careful model, the panic switch for a site that decides the fast lane is not for them (`:167-170`). |
| `ORB_FAST_MODEL` | `medgemma:4b` | The model that serves thinking-OFF. `_resolve_fast_model` (`:173-201`) errs **toward the careful model**: if the fast tag is not pulled, thinking-OFF is served by `LLM_MODEL`, the toggle becomes inert but safe, rather than 404ing. |
| `ORB_FAST_MODE_SAFETY_FLOOR` | `1` (on) | Routes certain question classes to the **careful** model even when the clinician has thinking OFF, because the fast model was measured getting them dangerously wrong on this appliance. The measurements and the exact rules are at the definition in `services/thinking_mode.py`. `=0` disables the floor; the config file says outright: *"Do not do this on a pilot ward"* (`:205-215`). |

**The eleven hospital-module switches and their tuning constants**, `server/config.py:540-771`. Covered in §0.5 and §16. Note the two groups of site-settable numbers that are **defaults a site must review, not measurements**, and are labelled as such in the UI: housekeeping turnaround targets (`ORB_HK_STANDARD_CLEAN_MINUTES` 45, `ORB_HK_TERMINAL_CLEAN_MINUTES` 120, `:635-636`) and workforce thresholds (`ORB_WORKFORCE_OT_DAILY_MINUTES` 480, `ORB_WORKFORCE_LATE_GRACE_MINUTES` 10, `ORB_WORKFORCE_MAX_SPAN_MINUTES` 960, `:661-676`). The overtime comment is explicit that 8 h is not a legal finding, Indian working-hours law for a hospital is state law.

**Other flags worth knowing:** `ORB_DEMO_MODE` (`:316`, defaults to **true in dev, false in prod**), `ORB_LENS_ENABLED` (`:323`, same split, Lens is **off by default in prod** unless an operator opts in), `ORB_CLOUD_SPEECH_ALLOWED` (`:295`, default **false**, the on-prem guarantee), `ORB_MODEL_FALLBACK_ENABLED` (`:145`), the background-LLM governor `ORB_BG_LLM_*` (`:225-254`), `ORB_SAGE_HISTORY_SUMMARIZE` (`:274`, default **off**), the `ORB_MONITOR_*` bedside-monitor listener group (`:406-436`, `ORB_MONITOR_ENABLED` default **false**), `ORB_MAX_JSON_BODY_BYTES` (`server/main.py:708`, 512 KB), `ORB_DB_BACKEND` (`server/db_backend.py:24`, only `sqlite` is wired) and `ORB_DISCHARGE_FINANCIAL_ENFORCE` (`:799`, default **off**, and §0.5 explains why it must stay off for the pilot).

---

**⚠️ If you change `config.py`:**

- Adding or renaming a variable: update every `from config import X` across the codebase. Run `grep -r "from config import" server/` to find all consumers.
- Changing `DB_PATH`: affects `database.py`'s `init_db()` and `get_db()`. Tests that set `ORB_DB_PATH` before import will break if the import order changes.
- Changing `JWT_SECRET` or `JWT_ALGORITHM`: **immediately invalidates all active user sessions.** All logged-in staff must re-authenticate.
- Changing `LLM_MODEL`: the warmup task in `main.py` and every Ollama call across `sage_service`, `scribe_service`, `bridge_service`, and `lens_service` will attempt to use the new model. Ensure it is pulled in Ollama first (`ollama pull <model>`).
- Changing `INGESTION_MODE` default: could enable simulation bots in a production environment with real patients, creating phantom clinical events. Treat like a security change.
- Changing `DEPLOYMENT_PROFILE` default from `"prod"`: disables JWT_SECRET enforcement. Never do this in a deployed hospital system.

---

### `server/main.py`: FastAPI Application Entry Point

**Role:** Creates the FastAPI application, registers all route modules, configures CORS, mounts Socket.IO, runs startup/shutdown logic, and starts the Uvicorn web server.

**ASGI** (Asynchronous Server Gateway Interface) is the Python standard that connects a web server (Uvicorn) to a web framework (FastAPI). Think of it as a standardized electrical socket that any compliant server and framework can plug into.

**Key imports / libraries:**

| Import | Why it is here |
|---|---|
| `asyncio` | Python's built-in library for writing concurrent code. `asyncio.create_task()` starts a background job without blocking the server. |
| `logging` | Standard Python logging. The `orb` logger appears in server logs. |
| `socketio` | The `python-socketio` library, creates the real-time WebSocket server. |
| `contextlib.asynccontextmanager` | Decorator that turns a generator function into a context manager (the `lifespan` pattern). |
| `FastAPI` | The main web framework class. |
| `CORSMiddleware` | Middleware is a function that wraps every request/response. CORS middleware adds the correct browser security headers. |
| `config.*` | All tunable settings imported by name. |
| `database.init_db` | Creates SQLite tables on first run. |
| `auth.get_current_user` | The JWT-verification dependency, imported here for the `/api/auth/me` and `/uploads/{filename}` endpoints. |
| `models.user.UserOut` | Pydantic model (data shape with automatic validation) used to serialize the response from `/api/auth/me`. |
| Route modules (`routes.*_router`) | Each Orb module (Vigil, Sage, Scribe, etc.) has its own router file. Importing and calling `app.include_router()` registers all its URL patterns. |
| `ws.realtime.sio` | The Socket.IO server object, defined once in `ws/realtime.py` and shared here. |
| `services.bot_service.*` | Background simulation bots that generate synthetic vital signs, lab results, and reports for demo purposes. |

---

#### `vitals_simulator()`: Background Task

```python
async def vitals_simulator():
```

- **Purpose:** Runs forever in the background, periodically calling the three simulation bots.
- **Respects `INGESTION_MODE`:** If mode is `"ingestion"` (real-data only), it exits immediately without starting the loop.
- **Loop:** Every `VITALS_INTERVAL_SECONDS` (5 seconds), it calls `run_vitals_bot(sio)`, `run_reports_bot()`, and `run_lab_bot()`.
- **Error handling:** Wraps the bot calls in `try/except`, if a bot crashes, it logs the error and keeps going rather than crashing the whole server.
- Note: `run_chat_bot(sio)` is commented out, it existed but was disabled.

---

#### `lifespan(app)`: Startup and Shutdown

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
```

A **lifespan** function is FastAPI's way of running code at server startup and shutdown. Everything before `yield` runs on startup; everything after runs on shutdown. Think of it as `__init__` and `__del__` for the entire server process.

**Startup sequence (in order):**

1. **`init_db()`**, creates all SQLite tables if they do not exist. Safe to call on every restart; uses `CREATE TABLE IF NOT EXISTS`.
2. **`register_triggers()`**, wires up the "nervous system" event bus (reactive triggers that respond to clinical events like deterioration alerts). Non-fatal: server continues if this fails.
3. **`init_helix_db()`**, sets up the pharmacy (Helix) database tables. Non-fatal.
4. **`reconcile_stale_pending_verifications()`**, if the server crashed mid-transcription, some Scribe notes may be stuck in "pending verification" state. This resets them. Non-fatal.
5. **`init_knowledge_base()`**, loads WHO/SSC/AHA clinical guidelines into the vector database for Sage's RAG queries. Non-fatal.
6. **`warmup_model()`**, sends a minimal dummy request to Ollama so MedGemma is already loaded in GPU memory before the first real user request arrives. Uses the LLM queue at lowest priority (`priority=5`) so it yields to any real incoming request immediately. Non-fatal.
7. **`warmup_voice_agent()`**, pre-loads MedGemma 4B into Apple Silicon Unified Memory for real-time voice. Only runs if `VOICE["conversational"]["enabled"]` is `True`. Non-fatal.
8. **`asyncio.create_task(vitals_simulator())`**, starts the background vitals loop as a concurrent task (not blocking startup).

**Shutdown:** The vitals simulator task is cancelled (`task.cancel()`), logging a shutdown message.

---

#### `app = FastAPI(...)`: Application Object

```python
app = FastAPI(
    title="Orb Hospital OS",
    description="Local-first hospital operating system API",
    version="0.1.0",
    lifespan=lifespan,
)
```

This creates the FastAPI application. `lifespan=lifespan` wires the startup/shutdown manager.

---

#### CORS Configuration

```python
if DEPLOYMENT_PROFILE == "prod":
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, ...)
else:
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, ...)
```

In **production**, only listed origins (from `ORB_CORS_ORIGINS`) can make cross-origin requests. `allow_credentials=True` means cookies and `Authorization` headers are permitted.

In **dev**, any origin is allowed (`"*"`). Note: `allow_credentials` must be `False` when `allow_origins` is `["*"]`, this is a browser security requirement. If you need `allow_credentials=True` in dev, you must list specific origins instead.

**`expose_headers` (both profiles)**, `server/main.py:731-743`. `allow_headers` governs *request* headers and does nothing for response headers; without an explicit expose list, `fetch()` silently returns `null` for any non-CORS-safelisted response header. The one that matters is the MFA self-enrolment ticket (`MFA_ENROLL_TICKET_HEADER`, imported from `routes.auth_routes` at `server/main.py:728`): it is the only way an account in `ORB_MFA_REQUIRED_ROLES` with no TOTP factor can enrol, and on a fresh appliance that account is the first and only admin. Single-origin worked by accident; the Vite dev server on `:5173` and any split-origin/LAN deployment did not, and the failure was invisible.

---

#### The other three middleware layers (all added 2026-08: not in earlier printings)

The manual previously documented CORS as the only middleware. There are four things wrapped around every request, in this order:

| Layer | `server/main.py` | What it does |
|---|---|---|
| `JSONDecodeError` exception handler | `:697-699` | Several routes call `await request.json()` directly. A malformed body would otherwise surface as a 500; this returns a clean **400**. |
| `_limit_body_size` | `:711-723` | Rejects any non-multipart request whose `Content-Length` exceeds `ORB_MAX_JSON_BODY_BYTES` (default **512 KB**, `:708`) with **413**; a non-integer `Content-Length` gets **400**. JSON routes read the whole body into memory, so without a cap a client could force the server to buffer an arbitrarily large body. File uploads are multipart and are bounded by their own route logic. |
| `CORSMiddleware` | `:744-762` | As above. |
| `audit_mutations` | `:765-766` | The PHI-mutation audit safety net. **See §10.5a**, this is the layer that records who changed what, and it had a serious defect until 2026-08-14. |

**CSRF is deliberately *not* a middleware.** Cookie-authenticated mutations are protected inside `auth.get_current_user`, the single point every session-protected route passes through, so the check fires only for requests actually authenticated by the session cookie, and never falsely blocks public or alternately-authenticated endpoints (discharge access, `X-API-Key` ingestion) that a logged-in clinician's browser might POST to while carrying a session cookie (`server/main.py:769-775`).

---

#### Route Registration

```python
app.include_router(auth_router)
app.include_router(vigil_router)
# ... 52 more
```

> **Correction (2026-08-15).** Earlier printings listed **16 routers** and said "`# ... 14 more routers`". `server/main.py` makes **54 `include_router()` calls** across **53 route modules** (`server/routes/` holds 53 `*_routes.py` files; `compliance_routes.py` contributes two routers). The old table was missing two-thirds of the API surface. Below is the full registration list, in file order.

Each `include_router()` call registers all the URL paths defined in that module's router. Imports are at `server/main.py:51-106`; registration at `:778-832`.

**Clinical / AI core** (§3–§9)

| Router | `server/routes/` file | Domain |
|---|---|---|
| `auth_router` | `auth_routes.py` | Login, MFA, device bind/PIN, refresh, logout |
| `vigil_router` | `vigil_routes.py` | Vigil deterioration alerts and the patient board |
| `relay_router` | `relay_routes.py` | Relay clinical messaging |
| `sage_router` | `sage_routes.py` | Sage AI clinical assistant |
| `sage_proactive_router` | `sage_proactive_routes.py` | Sage proactive/ambient surfaces |
| `scribe_router` | `scribe_routes.py` | Scribe transcription and note lifecycle |
| `bridge_router` | `bridge_routes.py` | Bridge patient portal |
| `lens_router` | `lens_routes.py` | Lens medical image analysis |
| `forecast_router` | `forecast_routes.py` | Forecast LSTM / capacity predictions |
| `surgery_router` | `surgery_routes.py` | Surgical schedule management |
| `ingestion_router` | `ingestion_routes.py` | HL7 / FHIR / CSV ingestion |
| `admission_router` | `admission_routes.py` | Patient admission workflow |
| `discharge_access_router` | `discharge_access_routes.py` | Patient access to the discharge summary |
| `helix_router` | `helix_routes.py` | Helix pharmacy management |
| `pulse_router` | `pulse_routes.py` | Pulse environmental/operational risk |
| `clinical_records_router` | `clinical_records_routes.py` | Clinical record documents |
| `bundle_router` | `bundle_routes.py` | Sepsis and other care bundles |
| `ipass_router` | `ipass_routes.py` | I-PASS shift handover (and the full reasoned Sage assessment, §5.7) |
| `pharmacy_router` | `pharmacy_routes.py` | Pharmacy verification queue |
| `orders_router` | `orders_routes.py` | Lab/imaging/procedure orders |
| `emar_router` | `emar_routes.py` | Electronic medication administration record |
| `nurse_router` | `nurse_routes.py` | The nurse shift board and bedside sheets |
| `appointments_router` | `appointments_routes.py` | Appointments |
| `chart_timeline_router` | `patient_timeline_routes.py` | Patient chart timeline |
| `search_router` | `search_routes.py` | Cross-entity search |
| `fhir_export_router` | `fhir_export_routes.py` | FHIR R4 export / EHR write-back |
| `downtime_router` | `downtime_routes.py` | Downtime (paper) mode |
| `capacity_sim_router` | `capacity_sim_routes.py` | Capacity simulation |
| `command_center_router` | `command_center_routes.py` | Hospital command centre |
| `coding_router` | `coding_routes.py` | Clinical coding |

**Governance, ops and the pilot instrument** (§10, §16)

| Router | `server/routes/` file | Domain |
|---|---|---|
| `audit_router` | `audit_routes.py` | Audit log queries and correlation trace |
| `admin_router` | `admin_routes.py` | Admin governance dashboard |
| `admin_users_router` | `admin_users_routes.py` | User administration |
| `model_governance_router` | `model_governance_routes.py` | Model governance / model cards |
| `flight_recorder_router` | `flight_recorder_routes.py` | Flight recorder |
| `roster_import_router` | `roster_import_routes.py` | Staff/patient roster import |
| `backup_router` | `backup_routes.py` | Backup and restore |
| `metrics_router` | `metrics_routes.py` | Metrics |
| `impact_router` | `impact_routes.py` | Impact dashboard |
| `compliance_router` + `consent_router` | `compliance_routes.py` | DPDP-Act compliance and consent (two routers from one module, `server/main.py:86-88`) |
| `shadow_router` | `shadow_routes.py` | **Shadow mode**, the pilot's evidence instrument |
| `ops_router` | `ops_routes.py` | Appliance ops health |

**Hospital-administration modules** (§16), `billing`, `payments`, `payer`, `procurement`, `frontdesk`, `dietary`, `workforce`, `biomedical`, `housekeeping`, `nabh`, `abdm`, each from its `*_routes.py` and each mounted at `/api/<module>`.

---

#### `/uploads/{filename:path}`: Authenticated File Serving

```python
@app.get("/uploads/{filename:path}")
def serve_upload(filename: str, user: dict = Depends(get_current_user)):   # server/main.py:873-874
```

Medical images (Lens) and consultation audio (Scribe) are PHI. This endpoint replaces what would otherwise be a public static file server.

> **Correction (2026-08-15).** This handler now has **seven** controls, not four, and it is a plain `def`, not `async def`. Earlier printings described a version with no ward scoping, no audit and no decryption, a reader who trusted them would have told an auditor that PHI media access was unlogged and unscoped, which was true then and is not true now.

**Security layers, in execution order** (`server/main.py:873-970`):

1. **Authentication**, `Depends(get_current_user)`; unauthenticated requests get 401.
2. **Role check**, only `doctor`, `nurse`, `admin`; patients get 403 (`:905-906`).
3. **Path-traversal prevention**, `target.relative_to(_UPLOAD_ROOT)`; anything resolving outside `uploads/` raises `ValueError` and is returned as **404** (`:908-914`).
4. **Registered-media allowlist** *(new)*, `_resolve_media_patient()` (`:852-870`) maps the filename back to the patient who owns it via `lens_analyses.image_path` or `scribe_notes.audio_path`. A file that is not referenced by a clinical record is **not served at all** (`:913-915`). An arbitrary file dropped into `uploads/` used to be downloadable by any clinician; it no longer is.
5. **Ward scope** *(new)*, `enforce_patient_scope(user, patient_id)` (`:929-933`), the same derivation and refusal as every other patient-addressed surface. The refusal is re-worded to this route's uniform "Not found" so an out-of-scope caller cannot even learn that the filename maps to a registered clinical record. An unscoped governance account (`department` NULL) still passes.
6. **Fail-closed PHI audit** *(new)*, a `phi_access` / `media_view` row is written before the bytes are read (`:940-956`). `phi_access` is a fail-closed category, so an `AuditWriteError` aborts the request with **503 "Audit temporarily unavailable; media not served"**. The scope check runs *before* the audit write so a denied caller leaves no success row for media they never received.
7. **Transparent decryption + `Content-Disposition: attachment`** (`:958-970`), PHI media is encrypted at rest (§2.10); `crypto_service.read_decrypted` passes legacy plaintext files through unchanged. A decrypt failure returns **503** with a generic message; the specific cause (tamper / wrong key / missing driver) is logged, not returned.

**Why it is `def` and not `async def`**, the reasoning is recorded in the docstring at `server/main.py:890-903` and is worth reading before anyone "modernises" it. Every statement blocks: two encrypted-SQLite lookups, a fail-closed audit write, and a whole-file AES decrypt that can run to many MB. It awaits nothing, so FastAPI runs a `def` endpoint in the anyio worker threadpool for free and the event loop never sees any of it. Converting it to `async def` + `run_db` would (a) insert an `await` between the fail-closed audit write and the read that write authorises, a suspension point in the middle of a safety control, and (b) push a multi-MB radiology decrypt through `run_db`'s **four**-worker pool, convoying every interactive query in the appliance behind one image download.

---

#### `/api/auth/me`: Authenticated Identity Endpoint

```python
@app.get("/api/auth/me", response_model=UserOut)
async def get_me(user: dict = Depends(get_current_user)):
```

Returns the current user's profile (id, name, role, department, registration number). The frontend calls this after login to populate the UI. The `response_model=UserOut` tells FastAPI to filter out any fields not in `UserOut` (like `password_hash`) before serializing the response.

---

#### `/api/health` and the three health sub-endpoints

`GET /api/health` (`server/main.py:985-1004`), **unauthenticated.** Returns `{"status", "service", "version", "modules"}`.

`modules` is new and load-bearing: it is `config.enabled_modules()` (`server/config.py:735-758`), and it is the **one** place the frontend learns which hospital-administration modules this site runs. It rides on the health endpoint rather than a new `/api/config` because this is the only unauthenticated endpoint the shell can read before a session exists, and the shell has to know which tabs to build at first paint (`src/shellConfig.tsx` consumes it, §14). It carries no PHI and no secret: it is a list of which screens this appliance offers.

Three further endpoints exist that earlier printings did not mention:

| Endpoint | `server/main.py` | Auth | What it returns |
|---|---|---|---|
| `GET /api/health/loop` | `:1006-1027` | none | Event-loop watchdog **stall history**. Note the docstring's own caveat: a 200 here cannot prove the loop is alive, because it is served *by* the loop. |
| `GET /api/health/access` | `:1028-1047` | `admin.overview` | Access-control self-report. |
| `GET /api/health/models` | `:1048-1105` | any authenticated user | Which models are resident. `POST /api/health/models/load` (`:1140`) and `/unload` (`:1172`) require `admin.overview` and are how an operator reclaims ~17 GB by evicting the 27B (see `services/model_router.py` for what happens to the next request). |

---

#### Socket.IO Mount

```python
socket_app = socketio.ASGIApp(sio, app)
```

This wraps both the Socket.IO server (`sio`) and the FastAPI app (`app`) into a single ASGI application. The result `socket_app` is what Uvicorn actually runs.

How it works: incoming connections that start with the Socket.IO handshake (`/socket.io/...`) are handled by `sio`; all other connections go to `app` (FastAPI). This is why you can use both HTTP routes and WebSocket events on the same server port.

---

#### `if __name__ == "__main__":`: Development Entry Point

```python
uvicorn.run(
    "main:socket_app",
    host=HOST, port=PORT,
    reload=True,
    reload_excludes=[".venv/*", "*.db", "uploads/*"],
)
```

When you run `python main.py` directly (development), this starts Uvicorn with hot-reload. The `reload_excludes` list prevents Uvicorn from restarting every time a database record is written or a file is uploaded, without this exclusion, any patient data write would restart the server.

In production, Uvicorn is typically started externally: `uvicorn main:socket_app --host 0.0.0.0 --port 8000`.

---

**⚠️ If you change `main.py`:**

- **Adding a new route module:** add `from routes.your_routes import router as your_router` and `app.include_router(your_router)`. Failure to call `include_router` means the endpoints silently do not exist, no error is thrown.
- **Changing the lifespan startup order:** `init_db()` must run before any code that queries the database. The knowledge base init must run after `init_db()`. The warmup tasks are safe to reorder.
- **Removing a startup step:** if you remove `init_db()`, the server will crash on first DB query with "no such table". If you remove `register_triggers()`, the nervous system event bus will not fire.
- **Changing CORS settings:** the frontend will break with CORS errors if the `allow_origins` list does not include the frontend's actual URL.
- **The `socket_app` variable name matters:** `uvicorn.run("main:socket_app", ...)`, if you rename it, the dev runner breaks.

---

### `server/auth.py`: Password Hashing and JWT Authentication

**Role:** Handles all credential operations: hashing passwords for storage, verifying passwords at login, issuing JWT tokens, and providing the `get_current_user` FastAPI dependency that every protected route uses to identify the caller.

**Plain-language glossary:**

- **JWT (JSON Web Token):** A small signed "ticket" issued after login. It contains the user's ID, role, and an expiry time, all cryptographically signed with the server's secret key. The client stores the token and sends it in the `Authorization` header with every request. The server verifies the signature rather than looking up a session in a database, this is called "stateless" authentication.
- **bcrypt:** A password hashing algorithm specifically designed for passwords. Unlike fast hashes (MD5, SHA-256), bcrypt is intentionally slow, making brute-force attacks impractical. It also includes a built-in random "salt" (extra random data mixed into the hash) so two users with the same password get different hashes.
- **HMAC (Hash-based Message Authentication Code):** A way to compute a hash using both the data and a secret key, so only someone with the key can verify it. Used here for timing-safe string comparison.
- **Dependency injection:** FastAPI's system for declaring that a route function needs some input prepared beforehand (e.g., "give me the current user") and having the framework call the preparation function automatically.

**Key imports / libraries:**

| Import | Why it is here |
|---|---|
| `jwt` | The `PyJWT` library, creates and verifies JWTs. |
| `hashlib` | Python's built-in crypto hash library, used only for legacy SHA-256 password verification. |
| `hmac` | Python's built-in HMAC library, used for timing-safe string comparison (`hmac.compare_digest`). |
| `uuid` | Generates random unique IDs for the JWT's `jti` (JWT ID) field. |
| `datetime, timedelta, timezone` | Calculates token expiry timestamps. |
| `HTTPBearer` | FastAPI security utility that reads the `Authorization: Bearer <token>` header. |
| `bcrypt` | The `bcrypt` library, hashes and verifies passwords using the bcrypt algorithm. |
| `database.get_db, db_session` | Database connection utilities to look up users by ID. |

---

#### Three credential channels, not one: read this before anything else in `auth.py`

> **This subsection is new (2026-08-15).** Earlier printings described `auth.py` as bearer-token-only. It has carried three credential channels for some time, and the manual's silence about the cookie is directly implicated in a real defect, see §10.5a.

**1. The session JWT, cookie *or* bearer.** `server/auth.py:15-29`. The JWT may arrive either as `Authorization: Bearer …` (machine clients, the test suite, backward compatibility) **or** as an HttpOnly `orb_session` cookie (`SESSION_COOKIE`, `:26`). The cookie is unreadable to JavaScript, so a single XSS, or a compromised npm dependency, can no longer exfiltrate a clinician's persistent session, which the previous `localStorage` token could. The SPA uses the cookie: `src/api/client.ts:17-21` and every `fetch` there passes `credentials: 'include'`.

**2. CSRF, for cookie-authenticated mutations only.** `CSRF_COOKIE = "orb_csrf"`, `CSRF_HEADER = "x-csrf-token"`, `UNSAFE_METHODS = {POST, PUT, PATCH, DELETE}` (`server/auth.py:27-29`). Cookies are auto-sent by the browser, which reintroduces CSRF risk, so cookie-authenticated mutations additionally require a double-submit token. **Bearer-authenticated requests are CSRF-exempt**, an explicit header is something a cross-site attacker cannot forge. Enforcement lives inside `get_current_user`, not in a middleware, for the reason given at `server/main.py:769-775`.

**3. The device credential, PIN-on-device identity.** `server/auth.py:31-44`. A shared ward tablet is enrolled **once** by an administrator and holds a long-lived device credential (`DEVICE_COOKIE = "orb_device"`, `DEVICE_HEADER = "x-orb-device-token"`); a **person** then signs in on top of it with a short PIN. The credential travels as an HttpOnly cookie in the normal state, after `POST /api/auth/device/bind` exchanges the raw secret for it. HttpOnly is the point: the raw secret never has to live in `localStorage` where a single XSS could read it off the tablet.

- `DEVICE_COOKIE_MAX_AGE` is **one year** (`:44`), deliberately: re-binding every day would put the raw secret back in a human's hands over and over, which is worse than a long-lived cookie an admin can revoke server-side in one click.
- `DEVICE_PIN_SESSION_MINUTES` defaults to **15** (`server/auth.py:65`, env `ORB_DEVICE_PIN_SESSION_MINUTES`). A tablet abandoned on a trolley is the threat, so a PIN session is minted with a lifetime in minutes rather than the `JWT_EXPIRY_HOURS` a password session gets. This is the **server-side floor**, enforced by the token's own `exp`, so it holds even if the tablet's clock or JS is tampered with; the SPA additionally arms a shorter idle timer.

**Why `_extract_token` matters.** Because there are three channels, there must be exactly **one** function that decides which one a request is using. That function is `auth._extract_token`, and `get_current_user` authenticates with it. Anything else in the codebase that needs to know who the caller is must call it too, §10.5a is what happened when one place re-implemented it.

**MFA (TOTP).** `server/services/mfa_service.py` implements RFC 6238 with the standard library only (`hmac`/`hashlib`/`base64`/`struct`), so enabling MFA pulls in no new native or pip dependency. Secrets are per-user, base32-encoded in `users.totp_secret`. Enrolment is **two-phase**, generate a secret + provisioning URI, then confirm one live code before `totp_enabled` is flipped on, so a user can never lock themselves out with a mis-scanned QR. Verification allows a ±1 time-step window for clock drift. Configured by `ORB_MFA_ISSUER` (`server/config.py:61`) and `ORB_MFA_REQUIRED_ROLES` (`:63-65`, empty by default). *This service has no dedicated section in this manual, see §17.*

---

#### `security = HTTPBearer()`: Bearer Token Extractor

```python
security = HTTPBearer()
```

This FastAPI utility object automatically reads the `Authorization` header, validates it has the format `Bearer <something>`, and extracts the token string. If the header is missing or malformed, it returns HTTP 403 before your code runs. **It is not the only way a token reaches the server**, see the three channels above.

---

#### `hash_password(password: str) -> str`

```python
def hash_password(password: str) -> str:
```

- **Input:** a plaintext password string (e.g., `"MySecretPass123"`).
- **Output:** a bcrypt hash string (e.g., `"$2b$12$abc...xyz"`).
- **Process:** converts the password to bytes (`encode('utf-8')`), generates a random salt (`bcrypt.gensalt()`), hashes the bytes+salt together, and returns the result as a string.
- **Called by:** `auth_routes.py` when creating a new user or changing a password. The hash is what gets stored in `users.password_hash` in SQLite, never the plaintext password.

---

#### `verify_password(password: str, hashed: str) -> bool`

```python
def verify_password(password: str, hashed: str) -> bool:
```

- **Input:** a plaintext password attempt and the stored hash from the database.
- **Output:** `True` if they match, `False` otherwise.
- **Legacy support:** bcrypt hashes always start with `$2` (e.g., `$2b$12$...`). If the stored hash does not start with `$2`, the system assumes it is an old SHA-256 hash and uses `hmac.compare_digest()` to compare in constant time (constant-time comparison means the function takes the same amount of time whether it matches or not, preventing "timing attacks" where an attacker can guess the hash character by character based on how long the comparison takes).
- **Called by:** `auth_routes.py` at login time.

---

#### `create_token(user_id: str, role: str) -> str`

```python
def create_token(user_id: str, role: str) -> str:
```

- **Input:** a user's database ID and their role string.
- **Output:** a signed JWT string that the client stores and sends with future requests.
- **Payload fields:**

| Field | Meaning |
|---|---|
| `sub` | "Subject", the user's database ID. Standard JWT claim. |
| `role` | The user's role at token creation time. **Important:** this is embedded for convenience only. `get_current_user` always re-reads the role from the database, so a stale role in the token cannot escalate privileges. |
| `jti` | "JWT ID", a unique random UUID. In a future version, this can be stored in a revocation table so individual tokens can be invalidated (e.g., on logout). Currently not checked. |
| `exp` | Expiry timestamp, current time + `JWT_EXPIRY_HOURS`. |
| `iat` | "Issued at" timestamp. |

- **Signing:** `jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)` signs the payload with the secret. Anyone who modifies the payload without knowing the secret will produce an invalid signature that `decode_token` will reject.
- **Called by:** `auth_routes.py` after a successful login.

---

#### `decode_token(token: str) -> dict`

```python
def decode_token(token: str) -> dict:
```

- **Input:** a JWT string from the `Authorization` header.
- **Output:** the decoded payload dictionary (e.g., `{"sub": "user-123", "role": "doctor", ...}`).
- **Error handling:**
  - `jwt.ExpiredSignatureError` → HTTP 401 "Token expired" (the `exp` timestamp passed).
  - `jwt.InvalidTokenError` → HTTP 401 "Invalid token" (signature mismatch, malformed token, etc.).
- **Called by:** `get_current_user`.

---

#### `get_current_user(credentials) -> dict`

```python
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
```

This is the most important function in `auth.py`. It is a **FastAPI dependency**, any route function that declares `user: dict = Depends(get_current_user)` will automatically have this run first, and the returned dict is passed as `user`.

**Process:**
1. `security` (the `HTTPBearer` instance) has already extracted the token string from the `Authorization` header.
2. `decode_token(credentials.credentials)` verifies the JWT signature and expiry, returning the payload.
3. Extracts `user_id` from `payload["sub"]`. If missing, returns HTTP 401.
4. Queries the database: `SELECT * FROM users WHERE id = ?`. This is the **authoritative lookup**, the database row is the ground truth, not the JWT payload. If the user has been deleted from the database, this returns 401 even with a valid JWT.
5. Returns `dict(row)`, a dictionary with all columns: `id`, `name`, `role`, `department`, `registration_number`, `password_hash`, `pin_hash`, `created_at`.

**Why re-query the DB?** The role embedded in the JWT was accurate at login time, but an admin might have changed the user's role since then. By reading from the database on every request, Orb ensures that role changes take effect immediately without waiting for the old token to expire.

---

#### `require_role(*roles: str)`: Deprecated Role Guard

```python
def require_role(*roles: str):
```

- **Input:** one or more role strings (e.g., `require_role("doctor", "nurse")`).
- **Output:** a FastAPI dependency that allows access only if the user's role is in the list.
- **Status:** marked `DEPRECATED`. New endpoints should use `require_permission()` from `permissions.py` instead. `require_role` bypasses the central permission map and audit logging. It is kept only so existing routes continue to work.

---

**⚠️ If you change `auth.py`:**

- **Changing the JWT payload structure** (adding/removing fields): all existing tokens immediately become partially incompatible. `get_current_user` callers that expect a new field will fail until users log in again.
- **Changing `hash_password`** (e.g., switching from bcrypt to Argon2): existing `$2b` hashes are still verified by the legacy-fallback path. But if you remove the bcrypt branch, existing users cannot log in. Plan a migration.
- **Changing `JWT_SECRET` or `JWT_ALGORITHM`** (in `config.py`, but affects this file): all active sessions invalidated immediately.
- **`get_current_user` is used everywhere:** `main.py` (uploads, /me), all 16 route modules via `require_permission` or `require_role`, and `permissions.py`. Breaking changes here break the entire API.

---

### `server/permissions.py`: Role-Based Access Control (RBAC)

**Role:** Defines every permission in Orb as a string constant, maps those permissions to roles, and provides FastAPI dependency factories that enforce access control with automatic audit logging.

**Plain-language glossary:**

- **RBAC (Role-Based Access Control):** A security model where users are assigned roles (e.g., "doctor"), and roles are granted permissions (e.g., "can sign notes"). You grant access by managing role assignments, not by configuring each user individually.
- **Least privilege:** A security principle meaning each role should have only the permissions it actually needs, nothing more. If a pharmacist does not need to read patient notes, they should not have that permission even if it would be convenient.
- **Audit log:** A tamper-evident record of who did what and when. Orb writes an audit entry every time a permission check fails, so security incidents can be reconstructed later.
- **Dependency factory:** A function that *returns* a FastAPI dependency. `require_permission("sage.query")` is called once at route definition time and returns the `_checker` function, which FastAPI calls on every request.

**Key imports / libraries:**

| Import | Why it is here |
|---|---|
| `uuid` | Generates a unique `correlation_id` for each audit event. |
| `fastapi.Depends, HTTPException, Request` | FastAPI's dependency injection, HTTP error responses, and request metadata (IP address, path). |
| `auth.get_current_user` | This module builds on top of `auth.py`, it always starts from an authenticated user. |
| `services.audit_service.log_audit_event` | Writes a structured record to the audit log on permission denial. |

---

#### Permission Constants

All permissions are defined as string constants at module level. The naming convention is `domain.action`, for example, `"scribe.sign"` means "sign action in the scribe domain".

**Clinical domain:**

| Constant | String value | Who needs it |
|---|---|---|
| `PATIENT_READ_ALL` | `"patient.read_all"` | View any patient record |
| `PATIENT_WRITE` | `"patient.write"` | Create or edit patient records |
| `VITALS_READ` | `"vitals.read"` | View vital signs |
| `ALERT_READ` | `"alert.read"` | View clinical deterioration alerts |
| `MEDICATION_WRITE` | `"medication.write"` | Prescribe or modify medications |

**Scribe domain:**

| Constant | String value | Notes |
|---|---|---|
| `SCRIBE_CREATE_DRAFT` | `"scribe.create_draft"` | Transcribe and structure a new note |
| `SCRIBE_READ_ALL` | `"scribe.read_all"` | View any note; future: scope to team |
| `SCRIBE_EDIT_DRAFT` | `"scribe.edit_draft"` | Edit draft content |
| `SCRIBE_SIGN` | `"scribe.sign"` | Finalize a note, doctors only |
| `SCRIBE_REJECT` | `"scribe.reject"` | Reject a draft, doctors only |

**AI module domain:**

| Constant | String value | Notes |
|---|---|---|
| `SAGE_QUERY` | `"sage.query"` | Query the Sage AI assistant |
| `LENS_ANALYZE` | `"lens.analyze"` | Upload and analyze medical images |
| `LENS_READ` | `"lens.read"` | View existing Lens analyses |

**Discharge domain:**

| Constant | String value |
|---|---|
| `DISCHARGE_READ` | `"discharge.read"` |
| `DISCHARGE_GENERATE` | `"discharge.generate"` |
| `DISCHARGE_PUBLISH` | `"discharge.publish"` |

**Relay (clinical messaging) domain:**

| Constant | String value | Notes |
|---|---|---|
| `RELAY_READ` | `"relay.read"` | Read channels and messages |
| `RELAY_SEND` | `"relay.send"` | Send messages |
| `RELAY_EXECUTE_ACTION` | `"relay.execute_action"` | Execute ambient action cards |

**Forecast and Surgery domains:**

| Constant | String value |
|---|---|
| `FORECAST_READ` | `"forecast.read"` |
| `SURGERY_READ` | `"surgery.read"` |
| `SURGERY_PREP` | `"surgery.prep"` |
| `SURGERY_WRITE` | `"surgery.write"` |

**Bridge (patient portal) domain:**

| Constant | String value | Notes |
|---|---|---|
| `BRIDGE_READ_OWN` | `"bridge.read_own"` | Patient reads their own data only |
| `BRIDGE_ASK` | `"bridge.ask"` | Patient asks questions |

**Helix (pharmacy) domain:**

| Constant | String value |
|---|---|
| `HELIX_READ` | `"helix.read"` |
| `HELIX_WRITE` | `"helix.write"` |

**Governance domain:**

| Constant | String value |
|---|---|
| `AUDIT_READ` | `"audit.read"` |
| `AUDIT_TRACE` | `"audit.trace"` |
| `INGEST_MONITOR` | `"ingest.monitor"` |
| `ADMIN_OVERVIEW` | `"admin.overview"` |

**Permissions the tables above were missing** (added since the manual was first written; verified against `server/permissions.py` 2026-08-15):

| Constant | String value | `permissions.py` | Notes |
|---|---|---|---|
| `VITALS_WRITE` | `"vitals.write"` | `:38` | Record a bedside observation set. Deliberately **not** folded into `PATIENT_WRITE`: a nurse charts obs but does not edit the patient record (`:132-134`). |
| `ORDER_READ` | `"order.read"` | `:79` | View lab/imaging/procedure orders. |
| `ORDER_WRITE` | `"order.write"` | `:80` | Create orders and drive the order state machine. Also the permission that gates diet orders, which is why Dietary is the one hospital module a nurse can write to (§16). |
| `PAYER_READ` | `"payer.read"` | `:98` | View payers, policies, claims, aging, denials. |
| `PAYER_WRITE` | `"payer.write"` | `:99` | Capture cover, drive the claim lifecycle. |
| `OUTCOME_RECORD` | `"outcome.record"` | `:108` | Record a deterioration outcome / confirm a shift with none, **the pilot's ground truth** (`docs/OUTCOME_CAPTURE.md`). Held by nurse, doctor and admin. |

> **Note on `payer.read` / `payer.write` (2026-08-12).** These exist so the TPA desk no longer *depends on* `ADMIN_OVERVIEW`. Today a TPA clerk is seeded as an `admin` account, so the grant changes nobody's access; the point runs the other way, the day a `tpa_clerk` role exists it can be given exactly these two and not the audit log (`server/permissions.py:167-176`). *(Earlier printings said the other four admin modules still routed through `ADMIN_OVERVIEW`. Since 2026-08-18 billing, payments, front desk and procurement have their own families too, next table.)*

**The desk split (2026-08-18), and three permissions added since** (verified against `server/permissions.py` 2026-10-03). Each back-office desk left `ADMIN_OVERVIEW`, the governance permission that also opens the audit log, so a clerk no longer has to be handed the hospital's tamper-evidence record to do the job. All of them are granted to `admin` (`:306-314`), so the split changed nobody's access on the day; what it changed is what a dedicated desk account must be given.

| Constant | String value | `permissions.py` | Notes |
|---|---|---|---|
| `BILLING_READ` | `"billing.read"` | `:153` | View invoices, charges, GST summaries, the tariff. |
| `BILLING_WRITE` | `"billing.write"` | `:154` | Capture charges; raise, adjust and settle invoices. |
| `BILLING_TARIFF` | `"billing.tariff"` | `:155` | Edit the tariff master itself. A separate pen because a price change alters every future invoice. |
| `PAYMENTS_READ` | `"payments.read"` | `:165` | View receipts, deposits, counter status, the day-end sheet. |
| `PAYMENTS_RECORD` | `"payments.record"` | `:166` | Take a payment or deposit, open a counter, raise a refund or discount request. |
| `PAYMENTS_APPROVE` | `"payments.approve"` | `:167` | Decide a refund or discount, reverse a receipt. The service also refuses the second signature from the person who raised the request or took the payment (§16.5). |
| `PAYMENTS_CLOSE_COUNTER` | `"payments.close_counter"` | `:168` | Close, count and reconcile a counter shift. |
| `FRONTDESK_WRITE` | `"frontdesk.write"` | `:176` | Registration and transfer paperwork. Front-desk reads stay on the clinical read permissions. |
| `PROCUREMENT_READ` | `"procurement.read"` | `:185` | Catalogue, indents, purchase orders, stock ledgers. |
| `PROCUREMENT_WRITE` | `"procurement.write"` | `:186` | Raise and edit indents and purchase orders. Also held by the pharmacist. |
| `PROCUREMENT_APPROVE` | `"procurement.approve"` | `:187` | Approve and issue purchase orders, blacklist vendors, write off stock. |
| `MEDICATION_PRESCRIBE` | `"medication.prescribe"` | `:71` | The prescribing decision itself (2026-08-15). Doctor only; the only permission that clears the allergy interlock. |
| `HELIX_RECEIVE` | `"helix.receive"` | `:121` | The ward confirms a dispensed drug arrived (2026-09-07). Nurse only, deliberately not the pharmacist who dispensed it. |
| `CONSENT_RECORD` | `"consent.record"` | `:206` | Record or withdraw DPDP consent. Doctor only today. |

---

#### `ROLE_PERMISSIONS`: The Master Access Control Table

```python
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "doctor": { ... },
    "nurse": { ... },
    "pharmacist": { ... },
    "admin": { ... },
    "patient": { ... },
    "cashier": { ... },
}
ROLE_PERMISSIONS["clinician"] = ROLE_PERMISSIONS["doctor"]
```

This dictionary is the single authoritative source of what each role can do. The value for each role is a Python `set` of permission strings.

**Role breakdown:**

| Role | Can do |
|---|---|
| `doctor` | Everything clinical: read/write patients, sign notes, prescribe, use all AI modules, generate/publish discharges, execute relay actions, manage surgery |
| `nurse` | Read patients, create/edit (but not sign) notes, view vitals and alerts, prescribe, use AI modules, read discharges, send relay messages, prep surgery |
| `pharmacist` | Sage queries for clinical context, Helix read/write only, no patient records, no notes, no imaging |
| `admin` | Read-only clinical access + full governance (audit, ingestion monitoring, admin dashboard) + pharmacy. Cannot write patient records, sign notes, prescribe, or analyze images. |
| `patient` | Self-only Bridge access: read own status, ask questions. Cannot see other patients or any clinical staff functions. |
| `cashier` | The cash counter and nothing else: `PAYMENTS_READ`, `PAYMENTS_RECORD`, `PAYMENTS_CLOSE_COUNTER` (`server/permissions.py:330-338`). Takes money, opens and closes a counter, raises refund and discount requests. **Not** `PAYMENTS_APPROVE`, so a cashier cannot decide a refund or discount or reverse a receipt, and no chart, Relay or audit access. Storable since migration 33 (`server/roles.py` is the single list). On the frontend the cashier lands on Payments and the rail holds Payments alone, with no admit button (`src/shellConfig.tsx` `ROLE_CONFIG.cashier`, `src/App.tsx` `UserRole`); before 2026-10-03 the shell had no cashier row and the account fell through to the Vigil and Relay fallback, both of which 403 for it. |
| `clinician` | Alias for `doctor`, same permission set (`server/permissions.py:195`). Mirrored on the frontend in `src/lib/roles.ts` and `src/shellConfig.tsx:838-842`; a role aliased on one side and not the other produced a real defect (a `clinician` account holding every doctor permission but seeing two tabs). |

**Corrections to the role breakdown (2026-08-15):** `nurse` and `doctor` also hold `VITALS_WRITE`, `ORDER_READ`, `ORDER_WRITE` and `OUTCOME_RECORD` (`server/permissions.py:124-153`). `admin` also holds `SURGERY_WRITE`, `ORDER_READ`, `PAYER_READ`, `PAYER_WRITE` and `OUTCOME_RECORD` (`:158-190`). The `admin` grant of `OUTCOME_RECORD` is explicitly the *backfill* path, the charge nurse records outcomes through `src/pages/panels/nurse/OutcomeCaptureSheet.tsx`, not the primary one (`:177-183`).

---

#### Ward / department PHI scoping: the half of access control the manual never had

> **This section is new (2026-08-15).** RBAC answers *"may this role call this endpoint?"*. It does **not** answer *"may this account see **this patient**?"*, and until 2026-08-05 nothing did. Holding `patient.read_all` let a Cardiology nurse open an Oncology chart, notes, imaging and handoff draft. Any CIO questionnaire answered from earlier printings of this manual would have overstated Orb's access control, because the manual documented only the first question.

**`department_scope(user) -> str | None`**, `server/permissions.py:222-248`. The single source of truth. Derived from the **authenticated user's row**, never from the request.

> *"Every patient-list surface … must call this and MUST NOT accept a department or 'show me everything' switch from the client, a scope the caller can name is not a scope."* The docstring records why: `/api/nurse/board` once took `?department=` and `?all_departments=true` straight off the query string, and a nurse who saw five patients on `/api/vigil/patients` could pull thirty across six departments through it. The hole existed because the nurse board grew its own idea of scope beside Vigil's.

`None` means **not ward-scoped**, the pre-existing convention that lets a hospital-wide governance account (an admin seeded without a department) read across wards. It is a property of the *user row*, changeable only by an administrator and recorded on every PHI read. **A clinical account with a blank department therefore reads hospital-wide**; set departments on clinical users.

**Where a patient's department comes from**, `PATIENT_DEPARTMENT_SQL`, `server/permissions.py:270-275`. It is a property of the patient's **admission**, resolved as *active admission first, then most recent*. It is deliberately **not** `patients.ward`: `patients` has no department column and `ward` is a free-text bed label nothing constrains. Scoping the wrong column is how a scope silently stops scoping.

| Helper | `permissions.py` | Use it when |
|---|---|---|
| `scope_patient_ids_sql(dept)` | `:278-288` | You need a subquery of every in-scope patient id, embeddable as `… IN (<sql>)`. `dept=None` returns the unfiltered set. |
| `patient_in_scope(db, pid, dept)` | `:290-312` | You already hold a connection. **Fail-closed:** a patient with no admission row has no derivable department and is in no scoped caller's ward. |
| `patient_in_scope_blocking(pid, dept)` | `:314-320` | From an `async def`, wrapped in `run_db`. |
| `enforce_patient_scope(user, pid)` | `:322-347` | From a plain `def` route. Raises **404**. |
| `require_patient_access(*perms, audit_action=…)` | `:475-551` | The dependency form, hang it on any `/{patient_id}` route. |

**`require_patient_access`** is the preferred guard: *"ONE guard hung off every `/{patient_id}` route rather than N copies of the check, so a route added later is either wearing it or is visibly bare in review."* It resolves `patient_id` from the **path**, so the request can name the patient it wants but never a department. On a scope miss it writes a `patient_scope_denied` authorization audit row **before** raising, so a cross-ward probe is visible in the trail (`:511-535`), then returns **404**.

**404, never 403, deliberately.** A 403 confirms the patient exists, which is itself a disclosure. A scoped caller cannot tell "no such patient" from "not your ward" (`:483-486`).

**`audit_action`** writes a fail-closed `phi_access` row on success. It is passed `None` for high-frequency polled reads such as `/vitals`, because one encrypted-SQLite write per poll is the shape blamed for the 2026-07-30 loop stalls. **Those routes still get the scope check**, that is the control; the read audit for them belongs in a batched reader, not on the request path (`:487-492`).

**Why `enforce_patient_scope` exists as well as the dependency**, `server/permissions.py:322-347`. The dependency reads the path, so it cannot guard a route where the caller names the patient in the **query string** (`/api/scribe/notes?patient_id=`, `/api/appointments?patient_id=`) or through a **surrogate id** that resolves to one (`/api/scribe/notes/{note_id}`). Those spellings are the same disclosure and were the blind spot in the first pass at the incident, because the drift guard greps for `{patient_id}` in the path and looked straight past them.

**Known, deliberate duplication.** `services/sage_service.py` implements the same derivation for the Sage lane (`_patient_in_scope` at `sage_service.py:1462`, `_scope_sql` at `:1448`). The two are deliberately identical and `tests/test_patient_scope_routes.py` asserts they agree on the same fixtures, change one without the other and that test fails (`server/permissions.py:264-268`).

**The tests that pin this**, and which are the honest answer to "prove it" from an auditor: `test_patient_scope_routes.py`, `test_caller_named_patient_scope.py`, `test_phi_read_scope_gaps.py`, `test_upload_patient_scope.py`, `test_relay_channel_scope.py`, `test_sage_department_scope.py`, `test_surgery_scope.py`, `test_nurse_write_scope.py`, `test_ward_brief_scope_and_attribution.py`. Each asserts three things: a departmented caller never receives a patient outside their department; an unscoped governance account still reads hospital-wide (so the fix is not over-applied); and a PHI read leaves a row on the hash chain.

**`log_phi_read(...)`**, `server/permissions.py:349-…`. Records a PHI **read** on the hash chain. Blocking; never call it on the event loop. This matters because the audit middleware (§10.5a) fires on `POST/PUT/PATCH/DELETE` only, reads were, and without this remain, invisible. The 2026-08-05 audit found **three** audit rows written across 23 PHI-returning GETs, which is why the leaks were not merely exploitable but unprovable afterwards.

---

#### `has_permission(role, permission) -> bool`

Simple lookup: `permission in ROLE_PERMISSIONS.get(role, set())`. Returns `True` if the role has that permission. Used for programmatic checks within service logic, not as a FastAPI dependency.

---

#### `has_all_permissions(role, *permissions) -> bool`

Checks that the role has every permission in the list. Useful when you want to assert multiple permissions at once in service code.

---

#### `get_permissions_for_role(role) -> set[str]`

Returns a copy of the permission set for a role. The `.copy()` prevents external code from accidentally modifying `ROLE_PERMISSIONS` (sets are mutable).

---

#### `require_permission(*permissions: str)`: Primary Auth Dependency

```python
def require_permission(*permissions: str):
    async def _checker(request: Request, user: dict = Depends(get_current_user)) -> dict:
        ...
    return _checker
```

This is the preferred way to protect any route in Orb. Usage in a route file:

```python
from permissions import require_permission

@router.get("/admin/overview")
async def overview(user = Depends(require_permission("admin.overview"))):
    # Only runs if the logged-in user has admin.overview permission
    ...
```

**Process for each request:**
1. FastAPI calls `get_current_user` first (via `Depends`), which verifies the JWT and returns the user dict.
2. `role = user.get("role", "")`, reads the role from the DB-authoritative user dict.
3. `user_perms = ROLE_PERMISSIONS.get(role, set())`, looks up that role's permission set.
4. `missing = [p for p in permissions if p not in user_perms]`, finds which required permissions the user is missing.
5. **If any are missing:**
   - Calls `log_audit_event(...)` with event category `"authorization"`, action type `"permission_denied"`, outcome `"denied"`. Records the user's ID, role, request path, HTTP method, IP address, user agent, and which permissions were missing. This creates a forensic trail.
   - Raises HTTP 403 with a structured body that names the missing permissions, useful for debugging and for the frontend to display a helpful message.
6. **If all permissions present:** returns the user dict to the route handler.

**Accepting multiple permissions:** `require_permission("patient.write", "medication.write")` requires the user to have both. This is an AND condition, all listed permissions must be present.

---

#### `require_patient_self()`: Patient Ownership Dependency

```python
def require_patient_self():
    async def _checker(request: Request, user: dict = Depends(get_current_user)) -> dict:
        ...
    return _checker
```

Used exclusively on Bridge (patient portal) endpoints. Unlike `require_permission`, this checks:

1. The user's role must be exactly `"patient"`. Any other role (including doctor) gets HTTP 403.
2. This prevents staff from accidentally (or maliciously) calling patient-only endpoints.

It also calls `log_audit_event` on failure, with `"patient_role_required"` as the reason.

The returned user dict contains the patient's own `id`, which route handlers use to scope queries: `SELECT * FROM ... WHERE patient_id = ?` using the authenticated user's ID. This ensures a patient can only ever see their own data.

---

**⚠️ If you change `permissions.py`:**

- **Adding a new permission constant:** you must also add it to the relevant roles in `ROLE_PERMISSIONS`, and use it in `Depends(require_permission(...))` on the appropriate routes. Defining a constant but not granting it to any role means no one can ever access those routes.
- **Removing a permission constant:** find all `require_permission("the.permission")` calls with `grep -r "the.permission" server/routes/` and update or remove them first, or those routes will permanently return 403 for all users.
- **Changing a role's permission set:** takes effect immediately on next request, no restart needed. Be especially careful when removing permissions from `doctor` or `nurse`, as this silently breaks clinical workflows.
- **Adding a new role:** three steps, all loud when missed (see `server/roles.py`, the single authority): add the string to `roles.STORABLE_ROLES`, grant a permission set in `ROLE_PERMISSIONS` here, and add a users-rebuild migration freezing the new list (`migrations._rebuild_users_role_check` does the heavy lifting, SQLite cannot ALTER a CHECK constraint). The SCHEMA CHECK, the admin staffing route's `ALLOWED_ROLES`, and the UsersPanel dropdown all render from the same list, and a parity test in `tests/test_migrations.py` fails the suite on any drift. This replaced the old hand-sync procedure after `cashier` spent six days grantable in `ROLE_PERMISSIONS` but unstorable in `users`, `INSERT OR IGNORE` swallowed the CHECK violation silently and staffing 401'd against a row that never landed.
- **The denial audit write goes through `run_db`, and it is still awaited.** `server/permissions.py:435-453`. The happy path (nothing missing) never touches the DB and stays on the loop with no threadpool hop; only the denial branch pays one. That asymmetry is deliberate: a burst of denials is exactly what an attack or a misconfigured client produces, and that is precisely when the loop must not be stalled by encrypted-SQLite writes. It is a **blocking await, not fire-and-forget**, the 403 is not raised until the denial is durably on the hash chain. The same pattern is used in `require_patient_access` (`:511-535`) and `require_patient_self` (`:565-580`). *(Earlier printings said "the audit write is not wrapped in try/except, it is expected to always succeed", which described a synchronous version that no longer exists.)*
- **`"clinician"` alias:** `ROLE_PERMISSIONS["clinician"] = ROLE_PERMISSIONS["doctor"]` makes both point to the exact same set object. Modifying one modifies the other at runtime (though you would be modifying a set object directly, which is unusual). If you need clinician to diverge from doctor, assign a copy: `ROLE_PERMISSIONS["clinician"] = ROLE_PERMISSIONS["doctor"].copy()`.

---

### Section Summary: How the Pieces Fit Together

```
config.py
  └─ read by everyone at import time (no circular deps)

main.py
  ├─ imports config.py for HOST, PORT, CORS_ORIGINS, etc.
  ├─ creates FastAPI app
  ├─ registers all routers
  └─ starts lifespan (init_db → warmup → vitals simulator)

auth.py
  ├─ imports config.py for JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRY_HOURS
  ├─ imports database.py for db_session
  └─ exports get_current_user (used by main.py and permissions.py)
       └─ called on every authenticated request

permissions.py
  ├─ imports auth.get_current_user
  ├─ imports audit_service.log_audit_event
  ├─ defines ROLE_PERMISSIONS dict (the access control table)
  └─ exports require_permission (used by every protected route)
       └─ called on every protected request, AFTER get_current_user
```

Every protected API request follows this exact path:

1. Uvicorn receives the HTTP request.
2. `CORSMiddleware` validates the `Origin` header.
3. FastAPI matches the URL to a route handler.
4. FastAPI calls the route's dependencies in order:
   - `HTTPBearer()` extracts the token from `Authorization: Bearer <token>`.
   - `get_current_user()` verifies the JWT and loads the user row from SQLite.
   - `require_permission("some.permission")` checks the role against `ROLE_PERMISSIONS`; logs and rejects if missing.
5. The route handler function runs with `user` available.
6. The handler calls service functions, which query SQLite via `get_db()` or `db_session()`.
7. FastAPI serializes the return value to JSON and sends the HTTP response.
