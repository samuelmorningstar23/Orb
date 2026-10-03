## 6. Scribe: Ambient Documentation & Voice/ASR

Scribe is the module that listens to a conversation between a clinician and a patient, converts the audio to text (transcription), and then drafts a structured clinical note from that text. Everything runs on-premises, no audio or patient data leaves the hospital's machine.

This section covers eight files. Read them in this order to understand the system bottom-up:

1. `scribe_providers.py`, how the LLM is called
2. `whisper_daemon.py`, the always-warm speech-to-text process
3. `voice_agent_service.py`, the conversational "Sage" voice assistant
4. `rxnorm_corrector.py`, phonetic drug-name spelling fixer
5. `rxnorm_reconciler.py`, maps drug names to the hospital formulary
6. `scribe_service.py`, the four-stage documentation pipeline
7. `ws/scribe_stream.py`, real-time audio streaming over Socket.IO
8. `routes/scribe_routes.py`, the HTTP API and note lifecycle endpoints

---

### Glossary (plain-English quick reference)

| Term | Plain-English meaning |
|---|---|
| **Ambient scribe** | Records a clinician-patient conversation in the background and automatically drafts a structured clinical note from it, the clinician does not need to dictate separately |
| **ASR** | Automatic Speech Recognition, software that converts audio to text |
| **Whisper** | OpenAI's open-weight speech-to-text model. Orb runs it locally via the `lightning_whisper_mlx` library, which uses Apple's MLX framework to run the model on the MacBook's unified GPU memory |
| **Streaming transcription** | Partial transcription results are sent back to the UI as audio arrives, rather than waiting for the entire recording to finish |
| **Socket.IO** | A real-time communication protocol (built on top of WebSockets) used here to stream audio chunks from the browser to the server and stream transcript text back |
| **SOAP note** | A four-section clinical note format: **S**ubjective (patient's complaints in their own words), **O**bjective (exam findings, vitals, labs), **A**ssessment (the clinician's diagnosis), **P**lan (what will be done next) |
| **Discharge / Handoff** | Two alternative note formats. Discharge summarises a hospital stay. Handoff uses the I-PASS framework to transfer patient responsibility between clinicians |
| **Note lifecycle** | The sequence of statuses a note can hold: `draft` → `signed` → `superseded` (or `rejected`). Explained fully below |
| **Provider abstraction** | A software pattern where the code that uses a service (here, "generate text from an LLM") does not depend on which specific LLM is being used. You can swap Ollama for MLX without rewriting the pipeline |
| **RxNorm** | A standardised vocabulary of drug names published by the US National Library of Medicine. Orb downloads a subset and uses it to spot-correct misspelled drug names in transcripts |
| **Fail-closed** | When an automated check is uncertain or fails, it must produce an explicit warning rather than silently passing. In Orb's scribe: if the hallucination verifier cannot complete its job, it returns a high-severity "could not verify" flag, never a false pass |
| **ZMQ / ZeroMQ** | A fast inter-process messaging library. Used here to let the main FastAPI server send audio paths to the Whisper daemon process and receive transcriptions back |
| **MLX** | Apple's machine-learning framework for Apple Silicon (M-series chips). Runs neural-network inference on the unified GPU/CPU memory without needing a discrete GPU |
| **QLoRA adapter** | A small set of additional weights (fine-tuning) layered on top of a base model to specialise its behaviour, here, tuned on medical vocabulary |
| **SSE** | Server-Sent Events, a one-way push mechanism where the server streams text events to the browser. Used here to stream the drafted note token-by-token as the LLM generates it |

---

### Note Lifecycle: State Machine

This is the single most important concept to understand before touching any Scribe code.

```
             [audio uploaded / streaming session]
                           │
                    (pipeline runs)
                           │
                           ▼
                        DRAFT  ◄─────────────────────────────────────┐
                       /      \                                       │
             [sign]  /          \ [reject]                           │
                    /            \                                    │
                   ▼              ▼                                   │
                SIGNED         REJECTED                               │
                   │                                                  │
            [revise]│                                                 │
                   │                                                  │
                   ▼                                                  │
             new DRAFT (version N+1, supersedes_note_id = original) ─┘
                   │
            [sign new draft]
                   │
                   ▼
             new note → SIGNED
             original → SUPERSEDED
```

**Rules enforced by the code (never bend these without reading the implications):**

- Only a `draft` note can be edited (`PATCH /api/scribe/notes/{id}`)
- Only a `draft` note can be signed (`POST /api/scribe/notes/{id}/sign`)
- Only a `draft` note can be rejected (`POST /api/scribe/notes/{id}/reject`)
- Only a `signed` note can be revised (`POST /api/scribe/notes/{id}/revise`), which creates a **new** draft; the original signed note stays `signed` until the revision is itself signed
- When a revision is signed, the original transitions from `signed` → `superseded` atomically in the same DB transaction
- Once `superseded` or `rejected`, a note cannot transition further

**Why this matters:** if you change these guards in `scribe_service.py` or `scribe_routes.py`, you can accidentally allow editing of a legally-signed note, or leave the audit trail inconsistent.

---

### `server/services/scribe_providers.py`

**Role:** Defines the provider abstraction, a thin layer so the pipeline stages (correction, drafting, verification) can call an LLM without knowing or caring whether it is running inside Ollama or via Apple MLX directly.

**Libraries:**
- `httpx`, async-capable HTTP client used for Ollama API calls
- `abc` (Python standard library), defines the abstract base class interface
- `mlx_lm`, Apple's MLX inference library, used in `MLXProvider` for in-process GPU inference
- `queue`, `threading` (standard library), used to run the synchronous MLX generator in a background thread while yielding tokens to the async caller

#### `LLMProvider` (abstract base class)

The contract every provider must fulfil. Three methods:

| Method | Sync/Async | Streaming | Used by |
|---|---|---|---|
| `generate(prompt, system, model, temperature, max_tokens, ctx_size)` | Sync | No | Correction stage (runs in subprocess context) |
| `agenerate(...)` | Async | No | Drafting and verification stages |
| `agenerate_stream(...)` | Async | Yes (yields token strings) | Streaming drafting endpoint |

All parameters have sensible defaults. `ctx_size` is the LLM's context window size, how many tokens of prompt + response the model can hold at once.

**⚠️ If you add a new provider:** implement all three methods. The pipeline calls whichever method matches the calling context. If you only implement two, the third will raise `TypeError` at runtime, not at import time.

#### `OllamaProvider`

Talks to a locally-running [Ollama](https://ollama.com) server over its HTTP API at `OLLAMA_BASE_URL` (from `config.py`). Ollama runs the LLM as a separate process and exposes a REST API.

- `generate`: uses `httpx.Client` (synchronous) with a 120-second timeout. Posts to `/api/chat` with `stream=False`. Returns `message.content` from the response JSON.
- `agenerate`: same but uses `httpx.AsyncClient` (non-blocking).
- `agenerate_stream`: posts with `stream=True` and iterates lines from the response body. Each line is a JSON object; it extracts `message.content` and yields it. Handles `GeneratorExit` gracefully by closing the response.

#### `MLXProvider`

Runs the LLM in-process on Apple Silicon using the same `VoiceAgent` model object (MedGemma 4B). This avoids loading a second copy of the model into memory.

- `generate`: calls `get_voice_agent()` to get the singleton, calls `.load()` to ensure the model is in memory, then calls `mlx_lm.generate()` synchronously inside a `vram_guard` context manager (which coordinates GPU memory with other inference tasks).
- `agenerate`: wraps `generate` in `asyncio.get_running_loop().run_in_executor(None, ...)` so it doesn't block the event loop.
- `agenerate_stream`: more complex. It acquires the UMSQ GPU lock at `PRIORITY_SCRIBE` (priority 2, lower than Sage voice queries at priority 1, higher than background scoring). It then starts a background thread that calls `mlx_lm.stream_generate()` and pushes each token into a `queue.Queue`. The async method reads from the queue via `run_in_executor` and yields each token. The `None` sentinel signals the end of the stream.

#### `_PROVIDERS` registry and `get_provider()`

A simple dictionary mapping the string name `"ollama"` or `"mlx"` to the class. `get_provider("ollama")` instantiates and returns an `OllamaProvider`. This is called by `scribe_service.py` at the start of each pipeline stage.

**⚠️ If you add a new provider:** add it to `_PROVIDERS`. If you rename an existing key, update every reference in `config.py` (the `SCRIBE` dict specifies `"provider"` for each stage).

#### `StageTimer`

A simple utility class used throughout the pipeline to measure how long each stage takes. Call `timer.start("draft")`, run the stage, call `timer.stop()`. The timings dictionary is then serialised into `pipeline_meta_json` in the database so you can see where time was spent when reviewing a note.

---

### `server/services/whisper_daemon.py`

**Role:** A standalone Python process that keeps the Whisper speech-to-text model permanently loaded in memory and accepts transcription requests over a ZeroMQ socket. The alternative (the "fallback") is launching a fresh subprocess for every transcription, which wastes 10–20 seconds loading the model each time.

**Libraries:**
- `zmq` (pyzmq), ZeroMQ IPC socket. The daemon binds a `REP` (reply) socket; clients connect with a `REQ` (request) socket
- `lightning_whisper_mlx`, an optimised wrapper around Whisper that uses Apple MLX for GPU acceleration
- `ffmpeg` (subprocess), used to convert non-WAV audio to 16kHz mono WAV before feeding it to Whisper
- `signal` (standard library), handles SIGINT/SIGTERM for graceful shutdown

**ZMQ Protocol:**
```
Request  (JSON): {"audio_path": "/tmp/orb_upload.wav", "hints": "Metoprolol, Furosemide", "language": "en"}
Response (JSON): {"text": "Patient is taking metoprolol 50mg twice daily.", "duration_ms": 312, "status": "ok"}
Error    (JSON): {"text": "", "error": "Audio file not found", "status": "error"}
```

The socket address defaults to `ipc:///tmp/orb-whisper.sock` (an IPC socket, local machine only, no network). This can be overridden with the `ORB_WHISPER_SOCKET` environment variable.

#### `convert_to_wav(input_path)`

If the audio file is not already a `.wav`, calls `ffmpeg` to convert it to 16kHz mono 16-bit PCM WAV, the format Whisper works best with. If conversion fails, returns the original path as a fallback.

#### `load_model(model_name)`

Loads the Whisper model from `server/mlx_models/{model_name}/` into Apple Silicon unified memory using `LightningWhisperMLX`. This is called once on daemon startup. Subsequent calls are no-ops if the model is already loaded. The `batch_size=12` parameter controls how many audio segments Whisper processes in parallel, higher values improve throughput on longer recordings.

#### `transcribe(audio_path, hints, language)`

The core transcription function. Steps:
1. Checks the model is loaded; returns an error dict if not
2. Converts to WAV if needed (via `convert_to_wav`)
3. Builds a `clinical_prompt`, a string of known medical terms prepended to the audio. Whisper's decoder is biased toward generating words that appear in this prompt, which dramatically improves accuracy for drug names and clinical terminology. The base prompt includes a handful of common IV antibiotics hardcoded; the `hints` parameter appends patient-specific terms (medication names, diagnosis, patient name) supplied by the caller
4. Calls `transcribe_audio()` from `lightning_whisper_mlx`
5. Returns `{"text": "...", "duration_ms": ..., "status": "ok"}` or an error dict

#### `run_daemon(socket_addr, model_name)`

The main loop. Sequence:
1. Removes the stale socket file if it exists (IPC sockets leave files on disk)
2. Calls `load_model()`, the one-time warmup cost
3. Binds the ZMQ `REP` socket
4. Registers signal handlers for clean shutdown
5. Loops forever: `socket.recv_string()` blocks until a request arrives, parses the JSON, calls `transcribe()`, sends the JSON response back

**⚠️ ZMQ REQ/REP is strictly one-at-a-time:** the daemon processes one transcription request before accepting the next. If the server sends a second request before the first completes, it will block. On the 8GB Mini with a large-v3 Whisper model this is fine for typical ward usage, but bear it in mind if you try to add concurrency.

#### `test_transcribe(audio_path)`

A quick standalone test, loads the model directly (no socket), transcribes, prints JSON. Useful for verifying the model is working: `python whisper_daemon.py --test my_audio.wav`.

#### Running the daemon

```
# Start (foreground):
python server/services/whisper_daemon.py

# Or with custom model and socket:
python server/services/whisper_daemon.py large-v3 ipc:///tmp/orb-whisper.sock
```

**⚠️ If you change the ZMQ socket address:** update `VOICE["daemon"]["socket"]` in `config.py` and restart both the daemon and the FastAPI server.

---

### `server/services/voice_agent_service.py`

**Role:** Implements "Sage", the conversational clinical AI assistant that a clinician can talk to in real time, asking questions like "what's this patient's creatinine trend?" or "what's the dose of vancomycin for a 70kg patient?". This is distinct from ambient scribe (which is passive documentation). Sage is also the model host that `MLXProvider` borrows when running LLM inference for Scribe stages.

**Libraries:**
- `mlx_lm`, `load`, `generate`, `stream_generate` for in-process Apple Silicon inference
- `services.gpu_coordinator`, `umsq` (Unified Model Scheduling Queue) and `vram_guard` for coordinating GPU memory between concurrent inference tasks
- `services.sage_service`, retrieves relevant patient context based on query semantics (department census, individual patient, disambiguation when multiple patients match)

#### System prompts

Two system prompt templates live at module level:

- `VOICE_SYSTEM_PROMPT`, used when a patient is selected. Injects `{hospital_name}` and `{patient_context}`. Instructs Sage to be concise (responses are spoken aloud via TTS), always cite which data informed the answer, add safety qualifiers for drug dosing, and never fabricate data.
- `VOICE_SYSTEM_PROMPT_NO_CONTEXT`, fallback when no patient is selected.

These are the guardrails. Do not weaken the "Never fabricate data" rule.

#### `build_patient_context(patient_id)`

Queries the local SQLite database to build a plain-text context block. Fetches:
- Patient demographics, diagnosis, bed, and ward from `patients`
- Active medications (non-discontinued) from `medications`, uses `dosage` column (not `dose`)
- Latest vitals (most recent row) from `vitals`
- Latest risk score from `risk_scores`

Converts every `sqlite3.Row` to a plain `dict` before calling `.get()`, important because `sqlite3.Row` does not have a `.get()` method.

Returns a formatted multi-line string or an error message. If the function fails, the agent falls back to the no-context prompt.

#### `VoiceStreamCleaner`

A stateful cleaner that filters the raw token stream coming out of the LLM. Problems it solves:

1. **Header stripping:** some model outputs start with scaffolding like `"Clinical Assessment:"` or `"<concise_answer>"`. The cleaner detects these and skips them.
2. **XML/markdown tag removal:** strips tags like `<concise_answer>`, `</concise_answer>`.
3. **Loop detection:** LLMs can sometimes enter infinite repetition loops (four or more commas in a row, five or more dots in a row, or the last three words repeating). When detected, `terminated` is set to `True` and the stream is cut off at the start of the loop.
4. **Incremental yielding:** tracks `yielded_len` so each call to `feed()` only returns the *new* characters since the last call.

Use `cleaner.feed(chunk)` for each token, then `cleaner.feed("")` at the end to flush any remaining content. Check `cleaner.terminated` to know if the stream was cut short.

#### `VoiceAgent`

The singleton class wrapping the MedGemma 4B model.

**`__init__`:** Sets `model_path` to `server/models/medgemma-1.5-4b-it`. Auto-detects QLoRA adapters at `server/adapters/adapters.safetensors`, if they exist they are loaded on top of the base model at `load()` time.

**`load()`:** Loads the model into unified memory using `mlx_lm.load(model_path, adapter_path=...)`. Sets `_loaded = True`. Subsequent calls are no-ops. If the model path does not exist, prints an install hint and returns without loading (the next call to `generate` will return a "not available" message rather than crashing).

**`_build_system_prompt(query, patient_id, is_follow_up, is_first_turn)`:** Calls `sage_service.get_relevant_context()` to determine query scope:
- `"DISAMBIGUATE"`, multiple patients match; prompt asks the clinician to clarify
- `"BROAD"`, a ward-level query; prompt includes the full department census
- patient-specific, injects individual patient context via `VOICE_SYSTEM_PROMPT`

If `sage_service` fails, falls back to `build_patient_context()`. Appends modifier instructions for first-turn greetings and follow-up brevity.

**`generate(query, patient_id)`:** Synchronous single-turn generation. Builds the system prompt, formats a chat template, calls `mlx_lm.generate()` inside a `vram_guard`, then runs the full response through `VoiceStreamCleaner`.

**`generate_stream(query, patient_id, conversation_history)`:** Async streaming generation. This is the primary method used by the voice routes. Key design:
1. Acquires `umsq` (GPU lock) at `PRIORITY_SAGE` (1, highest priority) before spawning the producer thread. This ensures Sage preempts any background Scribe MLX drafting.
2. Starts a background `threading.Thread` that calls `mlx_lm.stream_generate()` and pushes tokens into a `queue.Queue`, with `None` as the end sentinel.
3. The async method reads from the queue via `run_in_executor` (so the event loop is not blocked) and yields each token through `VoiceStreamCleaner`.
4. Maintains up to 10 prior conversation exchanges in the message history for follow-up context.

**`get_voice_agent()`:** Returns the module-level singleton `_voice_agent`, creating it on first call. Called by `MLXProvider` to borrow the already-loaded model for Scribe LLM stages.

---

### `server/services/rxnorm_corrector.py`

**Role:** Fixes drug name misspellings in raw Whisper transcripts before they reach the LLM drafting stage. Drug names are the hardest tokens for any ASR system, they are rare, long, borrowed from Latin/Greek, and phonetically similar to common words. A missed correction here propagates into the structured note.

**Libraries:** entirely standard library, `sqlite3`, `csv`, `re`, `time`. No external dependencies.

**How it works:**

Whisper might transcribe "amoxicilin" (misspelled) or "metopro lol" (split across words). RxNorm correction catches both. It works in two passes per token:

1. **Exact match (case-insensitive):** `self._drug_names[token.lower()]`, if the lowercase token is in the index, return the canonical capitalisation.
2. **Phonetic + Levenshtein match:** compute the metaphone key of the token, look up candidates with the same key in `_phonetic_index`, then measure Levenshtein edit distance to each candidate. If the best candidate is within `max_edit_distance` edits (default: 2), substitute it.

Tokens shorter than 4 characters and tokens in `_ENGLISH_STOPWORDS` are skipped to avoid false positives (e.g., "vital" or "pulse" should not be replaced with a drug name).

#### `levenshtein(s1, s2)`

Pure-Python dynamic-programming implementation of Levenshtein edit distance. Returns the minimum number of single-character insertions, deletions, or substitutions needed to turn `s1` into `s2`. Used for fuzzy-match scoring.

#### `metaphone_key(word)`

A simplified phonetic hash. Applies a series of substitutions (`PH`→`F`, `CK`→`K`, etc.), collapses duplicate consecutive consonants, strips vowels (except the first character), and caps at 8 characters. Two words that sound similar will produce the same or similar metaphone keys, allowing efficient candidate lookup. This is **not** a full Double Metaphone implementation, it is a practical approximation tuned for medical vocabulary.

#### `_ENGLISH_STOPWORDS`

A large frozen set of common English words that phonetically resemble drug names but are not drugs (e.g., "vital", "pulse", "fever", "organ"). Without this filter, common clinical words would be incorrectly replaced. If you add drug names to RxNorm that collide with clinical vocabulary, you may need to expand this set.

#### `RxNormCorrector` class

**`__init__(db_path, csv_path, max_edit_distance)`:** If the SQLite DB does not exist but a CSV is provided, builds the DB from the CSV. Then calls `_load_index()`.

**`_build_db(csv_path)`:** Reads the RxNorm CSV (fields: `rxcui`, `name`, `tty`), writes each drug into a SQLite table with a lowercase version and a pre-computed metaphone key. Indexed on both `name_lower` and `metaphone`.

**`_load_index()`:** Loads the entire drug table into two in-memory Python dicts: `_drug_names` (lowercase → canonical name) and `_phonetic_index` (metaphone key → list of canonical names). At runtime, all lookups happen in memory, the SQLite DB is only used for building the index, not querying it.

**`_find_match(token)`:** The actual matching logic. Returns the canonical drug name if found, or `None` if no match meets the threshold.

**`correct(text)`:** Tokenises the text using `re.findall(r'\b\w+\b|\s+|[^\w\s]', text)`, this preserves whitespace and punctuation. For each alphabetic word token of length >= 4, calls `_find_match`. Reassembles the corrected text and returns it along with a log of every correction made (original → corrected, edit distance).

**Performance target:** claimed <2ms per correction pass. The in-memory index makes this realistic for typical transcript lengths.

**⚠️ If you change the RxNorm CSV or DB path:** update `VOICE["corrector"]["rxnorm_db"]` and `VOICE["corrector"]["rxnorm_csv"]` in `config.py`. Deleting the DB file triggers a rebuild from CSV on next startup.

---

### `server/services/rxnorm_reconciler.py`

**Role:** Maps a free-text drug name (extracted from a transcript or note) to a row in the `helix_medication_master` table, the hospital's canonical drug formulary. This is a different problem from the corrector: the corrector fixes spelling; the reconciler finds which hospital formulary entry the drug corresponds to.

**Libraries:** `re` (standard), `database.db_session`, and the `metaphone_key` and `levenshtein` functions imported from `rxnorm_corrector`.

#### `calculate_similarity(s1, s2)`

Normalised Levenshtein similarity. Returns a float from 0.0 (completely different) to 1.0 (identical). Formula: `1.0 - (distance / max_length)`.

#### `calculate_metaphone_similarity(s1, s2)`

Phonetic token overlap. Splits both strings into words, computes the metaphone key of each word, and returns the Jaccard-like overlap: `|intersection| / max(|set1|, |set2|)`. Words shorter than 3 characters are ignored.

#### `reconcile_medication(name)`

The main function. Input: a raw drug name string. Output: `(matched_medication_dict, score)` or `(None, 0.0)`.

Algorithm:
1. Fetches every row from `helix_medication_master`
2. For each formulary entry, computes four scores: Levenshtein similarity against the `name` column, Levenshtein similarity against `generic_name`, and metaphone similarity against both
3. Hybrid score: `(max_lexical × 0.7) + (max_phonetic × 0.3)`, lexical match weighted more heavily
4. Bonus: if the input is a substring of either the name or generic name, score is boosted to at least 0.85
5. Returns the best match if its hybrid score >= 0.8, otherwise returns `(None, best_score)`

**The 0.8 threshold is a safety boundary.** Below 0.8 the match is considered too uncertain to use programmatically. If you lower this threshold, you risk mapping the wrong drug. If you raise it, you will miss valid matches with unusual spellings.

**⚠️ If you change the formulary table name or schema:** update the `SELECT * FROM helix_medication_master` query and the field names `"name"` and `"generic_name"` used in the scoring loop.

---

### `server/services/scribe_service.py`

**Role:** The four-stage clinical documentation pipeline. Each call to `transcribe_audio()` and the sequence `structure_transcript()` → `verify_note()` moves audio → raw text → corrected text → structured clinical note → verification report. Also contains all database operations for creating, reading, and transitioning scribe notes.

**Libraries:** `subprocess`, `asyncio`, `json`, `zmq` (conditional), `config.SCRIBE`, `database.db_session`, `services.scribe_providers`, `services.event_bus`

#### Stage 0: Hint Building

**`build_whisper_hints(patient_id)`**

Queries the DB for the patient's name, active diagnosis, all active (non-discontinued) medication names, and their attending doctor's name. Returns these joined as a comma-separated string. This string is passed to Whisper as the `initial_prompt`, biasing the decoder toward recognising these specific terms. If the query fails for any reason it returns an empty string (non-fatal, transcription still works, just with lower accuracy for specialised terms).

#### Stage 1: ASR: Transcription

**`transcribe_audio(audio_path, apply_correction, hints)`**

The public entry point for file-based transcription (used by the HTTP upload endpoints). Does not handle streaming, that is `scribe_stream.py`'s job.

Steps:
1. Tries the warm daemon via `_transcribe_via_daemon(audio_path, hints)`
2. If the daemon is unavailable (not running, timed out, ZMQ not installed), falls back to `_transcribe_via_subprocess(audio_path, hints, asr_cfg)`
3. If the transcript is non-empty, applies RxNorm phonetic correction via `_apply_rxnorm_correction(draft)`
4. If `apply_correction=True` and the transcript is >= 150 characters, applies LLM correction via `correct_transcript(draft)`

The 150-character threshold is a fast-path optimisation: very short transcriptions are highly accurate from Whisper and do not need the 20–30 second LLM correction pass.

**`_transcribe_via_daemon(audio_path, hints)`**

Opens a ZMQ `REQ` socket with a configurable timeout (from `VOICE["daemon"]["timeout_ms"]`), sends the audio path and hints as JSON, waits for the response. Returns the transcript string on success, `None` on any failure (timeout, connection error, error status from daemon). The caller then tries the subprocess fallback. ZMQ import errors are caught, if pyzmq is not installed, this always returns `None`.

**`_transcribe_via_subprocess(audio_path, hints, asr_cfg)`**

Launches `server/scripts/transcribe_worker.py` as a child process with the audio path, model name, hints, batch size, and quantisation level as arguments. Parses the output looking for a `###RESULT###` marker followed by JSON (the marker disambiguates from any debug output the worker emits). Returns the `text` field from that JSON, or an empty string on failure or timeout (120 seconds).

**`_apply_rxnorm_correction(text)`**

Checks `VOICE["corrector"]["enabled"]`. If enabled, instantiates a `RxNormCorrector` and calls `corrector.correct(text)`. Returns the corrected text. On any exception, returns the original text unchanged (non-fatal).

#### Stage 2: Correction / Normalisation

**`correct_transcript(draft)`**

Calls the correction-stage LLM with a carefully crafted prompt. Key features of the prompt:

- Instructs the model to fix acoustic errors (e.g., "hyper" vs "hypo", wrong drug spellings)
- Explicitly says: "Do not summarise, shorten, or omit any clinical information"
- Applies **prompt-injection hardening**: the raw transcript is untrusted ASR output. It is fenced between `<<<BEGIN_DICTATION>>>` and `<<<END_DICTATION>>>` markers, and a SECURITY note tells the model to treat everything inside the fences as data, not instructions. The transcript is pre-sanitised to replace any literal `<<<END_DICTATION>>>` sequences that might appear in the audio.

Uses `get_provider(cfg["provider"]).generate(...)`, synchronous, because this may be called in non-async contexts.

#### Stage 3: Drafting / Structuring

Three prompt templates live in `STRUCTURING_PROMPTS`:

- **`"soap"`**, asks for a DIALOGUE section (speaker-attributed), then SUBJECTIVE, OBJECTIVE, ASSESSMENT, PLAN
- **`"discharge"`**, discharge summary with DIALOGUE, ADMISSION DIAGNOSIS, HOSPITAL COURSE, PROCEDURES, DISCHARGE MEDICATIONS, FOLLOW-UP, DISCHARGE INSTRUCTIONS
- **`"handoff"`**, I-PASS format with DIALOGUE, ILLNESS SEVERITY, PATIENT SUMMARY, ACTION LIST, SITUATION AWARENESS, SYNTHESIS

All templates share the `DRAFT_SYSTEM_PROMPT` which identifies the model as a "medical documentation specialist" and adds a SECURITY note about treating the dictation as data.

**`_fence_transcript(transcript)`** sanitises the transcript and wraps it in `<<<BEGIN_DICTATION>>>` / `<<<END_DICTATION>>>` fences before embedding it in a prompt.

**`structure_transcript(transcript, format)`**, non-streaming version. Acquires the LLM queue at priority 2, calls `agenerate`, releases the lock, returns the full text. Used when streaming is not needed.

**`structure_transcript_stream(transcript, format)`**, streaming version. Same queue priority, calls `agenerate_stream`, yields tokens as they arrive. Used by the `/api/scribe/structure` SSE endpoint.

#### Stage 4: Verification (optional)

**`verify_note(transcript, structured_note)`**

This stage is the safety net. It compares the original transcript against the generated note and checks for hallucinations, omissions, and entity mismatches (wrong drug name, wrong dose, wrong vital).

The LLM is asked to return a strict JSON object with:
- `verified` (boolean)
- `confidence` (0.0–1.0)
- `warnings`, list of `{type, section, text, severity}` objects
- `omissions`, list of `{text, severity}` objects
- `sections_verified`, which SOAP sections were checked

**The critical safety property, fail-closed:**

The function has three possible outcomes:

1. `return None`, **only** when verification is explicitly disabled in config (`verify_cfg["enabled"] == False`). This signals "verification was not requested."
2. `return _unverified_report(reason)`, when verification was requested but **could not complete** (empty transcript/note, backend error, empty LLM response, unparseable JSON). This is the fail-safe path.
3. `return parsed_report`, when verification ran and the LLM returned a parseable report.

**`_unverified_report(reason)`** always returns `verified=False`, `confidence=0.0`, and a high-severity warning saying the note has not been verified and requires manual review before signing. It never returns a passing verdict.

**Why this matters:** if instead the verifier returned `None` on failure (the "fail-open" design), the frontend would interpret `None` as "no issues found" and display no warning. The fail-closed design ensures any failure in the verification chain surfaces as an explicit flag on the note.

**⚠️ Known TODO (from code comment at line 675):** the sign endpoint does not yet enforce a gate requiring clinician acknowledgement when `verified=False` or `verification_status="unverifiable"`. The fail-closed signal is available, the gate needs to be added in `scribe_routes.py`.

The verification LLM runs at queue priority 3 (lowest), it never delays interactive workflows.

The response may arrive wrapped in markdown code fences (```json...```). The parser strips these before `json.loads()`.

#### Persistence Functions

**`save_scribe_note(...)`**, INSERT into `scribe_notes`. Fields stored: `patient_id`, `author_id`, `raw_transcript`, `structured_note`, `note_format`, `audio_path`, `verification_json`, `pipeline_meta_json`, `status` (default `"draft"`), `version` (default 1), `supersedes_note_id`, `ai_generated`. Returns the new row's `id`.

**`get_scribe_notes(patient_id, author_id, status)`**, SELECT with optional filters. Returns up to 50 rows ordered by `created_at DESC`.

**`get_scribe_note_by_id(note_id)`**, Fetches a single note. Parses `verification_json` and `pipeline_meta_json` from stored JSON strings into Python dicts before returning.

#### Note Lifecycle Functions

**`update_scribe_note_content(note_id, structured_note, editor_id)`**

Status guard: only proceeds if `status == "draft"`. Updates `structured_note`, `last_edited_by`, `last_edited_at`. Returns `{"ok": True}` or `{"error": "reason"}`.

**`sign_scribe_note(note_id, signer_id)`**

Status guard: only proceeds if `status == "draft"`. Sets `status = "signed"`, `signed_by`, `signed_at`, `finalized_at`. If `supersedes_note_id` is set on this note, atomically sets the original note from `signed` → `superseded` in the same transaction. Publishes a `scribe.note.signed` event to the event bus (handles both async and sync call contexts). Returns `{"ok": True, "superseded_note_id": int|None}`.

**`reject_scribe_note(note_id, rejector_id, reason)`**

Status guard: only proceeds if `status == "draft"`. Sets `status = "rejected"`, `rejected_by`, `rejected_at`, `rejection_reason`.

**`create_revision_from_signed(original_note_id, editor_id)`**

Status guard: only proceeds if `status == "signed"`. Creates a **new** row in `scribe_notes` copying all content from the original, with `status = "draft"`, `version = original.version + 1`, and `supersedes_note_id = original_note_id`. The original note is **not** touched here, it stays `signed`. The transition to `superseded` only happens when the new draft is itself signed. Returns `{"ok": True, "new_note_id": int, "version": int}`.

**`update_scribe_note_verification(note_id, verification_json, pipeline_meta_json)`**

Updates the verification columns on an existing note. Contains two race-condition guards:
- Does not overwrite a `complete` status with `failed` (stale task from a prior run)
- Does not overwrite a `complete` status with `pending` (double-dispatch)

Both guards silently return `True` (treating it as a success) rather than raising an error.

#### Pipeline Metadata

**`build_pipeline_meta(timer)`**, assembles a dict with timing data from `StageTimer` and the model names from `config.SCRIBE`. Stored as JSON in `pipeline_meta_json`. Useful for performance analysis and audit.

---

### `server/ws/scribe_stream.py`

**Role:** The real-time streaming transcription endpoint. The browser microphone streams audio to this endpoint over Socket.IO and receives transcript text back as the clinician speaks. This is the "ambient" part of ambient scribe, the session runs in the background while the clinician talks to the patient.

**Libraries:** `socketio` (via `ws.realtime.sio`), `wave`, `asyncio`, `tempfile`

**Protocol (Socket.IO events):**

| Event | Direction | Data | Meaning |
|---|---|---|---|
| `scribe:start_session` | Client → Server | `{patient_id?, hints?}` | Start a streaming session |
| `scribe:audio_chunk` | Client → Server | Binary PCM16 bytes | A chunk of microphone audio |
| `scribe:end_utterance` | Client → Server | `{}` | Client's VAD (voice activity detector) detected end of speech |
| `scribe:stop_session` | Client → Server | `{}` | Session is over |
| `scribe:status` | Server → Client | `{status: "ready"\|"processing"}` | Server availability |
| `scribe:transcript` | Server → Client | `{text, duration_ms, is_final, utterance_index}` | Transcription result |
| `scribe:error` | Server → Client | `{error: "message"}` | Error occurred |

**⚠️ If you change this protocol:** update `server/ws/scribe_stream.py`, the frontend `ScribePanel` component, the `useStreamingTranscription` hook, and any `nervous_system_triggers` that listen to scribe events.

#### Authentication (`_require_authenticated`)

Before any session can start, the handler checks that the Socket.IO session (set during the JWT handshake in `ws/realtime.py`) contains a `user_id`. If not, it emits `scribe:error` with `"unauthenticated"` and returns. Scribe streams carry Protected Health Information (PHI), patient audio, so this check must never be removed.

#### Session State (`_sessions`)

A module-level dict mapping Socket.IO client IDs (`sid`) to session dicts. Each session holds:
- `patient_id`, which patient this session is for (may be empty)
- `hints`, clinical vocabulary hints for Whisper
- `audio_buffer`, a `bytearray` accumulating raw PCM16 audio between utterances
- `utterance_index`, counter so the frontend can match transcripts to utterances in order
- `is_active`, whether the session is still running
- `created_at`, timestamp

#### Audio Format

The client must send raw **PCM16 audio at 16kHz, mono**. This is the native format for Whisper. The `_write_wav` function wraps the raw bytes in a standard WAV header (channels=1, sample width=2 bytes, frame rate=16000) before passing the file path to Whisper.

#### `handle_start_session`

1. Requires authentication
2. Reads `patient_id` and `hints` from the event data
3. If `patient_id` is provided, calls `build_whisper_hints(patient_id)` and appends the DB-derived hints
4. Creates the session dict, emits `scribe:status {"status": "ready"}`

#### `handle_audio_chunk`

Appends the received bytes to `session["audio_buffer"]`. Handles both raw binary and `{audio: ArrayBuffer}` dict format (different browsers/Socket.IO versions send data differently).

#### `handle_end_utterance`

Fires when the client's VAD detects silence, signalling that the clinician has finished a sentence or phrase. Creates an `asyncio.Task` to run `_transcribe_utterance()` without blocking the event loop. The buffer draining and transcription happen in the background.

#### `_transcribe_utterance(sid, session)`

The core processing step for one utterance:
1. Drains `session["audio_buffer"]` (swaps it for a new empty bytearray)
2. Skips if fewer than 3200 bytes (< 0.1 seconds of audio, too short to be real speech)
3. Emits `scribe:status {"status": "processing"}`
4. Writes the PCM bytes to a temp WAV file (blocking I/O run in executor)
5. Tries daemon transcription, falls back to subprocess, applies RxNorm correction
6. Emits `scribe:transcript {text, duration_ms, is_final: True, utterance_index}`
7. Emits `scribe:status {"status": "ready"}`
8. Always deletes the temp WAV file in the `finally` block

Note that this endpoint does **not** run LLM correction on streaming transcripts, only the RxNorm phonetic correction. LLM correction (which takes 20–30 seconds) would make the streaming feel unresponsive. The full LLM correction is available through the batch `/api/scribe/transcribe` endpoint.

#### `handle_stop_session`

If there is remaining audio in the buffer (>3200 bytes), transcribes it first. Then sets `is_active = False` and destroys the session.

#### `handle_scribe_disconnect`

Socket.IO fires this on any disconnect. Cleans up the session if it exists.

---

### `server/routes/scribe_routes.py`

**Role:** The HTTP API router. All Scribe operations accessible over REST live here: audio upload and transcription, note structuring (with streaming and async verification), note retrieval, and all lifecycle transitions (edit, sign, reject, revise).

**FastAPI router prefix:** `/api/scribe`

**Auth pattern:** all endpoints use either `require_role(...)` (checks the user's role) or `require_permission(...)` (checks a named permission string from the user's role's permission list). Scribe is not publicly accessible.

#### Verification State Constants

```python
VERIFY_NOT_REQUESTED = "not_requested"  # verify=False was passed to /structure
VERIFY_PENDING       = "pending"        # verification task is running
VERIFY_COMPLETE      = "complete"       # verification finished successfully
VERIFY_FAILED        = "failed"         # verification failed or was interrupted
```

These constants appear in `verification_json.status` in the database. `_TERMINAL_STATES = {VERIFY_COMPLETE, VERIFY_FAILED}`, no transitions out of these states are permitted.

#### Verification Registry

`_verification_registry: dict[int, asyncio.Task]` maps note IDs to their running verification tasks. Prevents dispatching duplicate verification tasks for the same note. `_is_verification_in_flight(note_id)` checks if a non-done task exists. `_register_verification_task(note_id, task)` stores the task and attaches a done-callback that removes it from the registry.

#### `reconcile_stale_pending_verifications()`

Called once on server startup (from the FastAPI lifespan handler). If the server crashed or restarted while a verification task was in flight, the note is left with `verification_json.status = "pending"` forever. This function scans for notes in that state older than `STALE_PENDING_THRESHOLD_MINUTES` (10 minutes) and marks them `VERIFY_FAILED` with an explanation. This is important for audit integrity, a note should never be stuck permanently in "pending."

#### `_run_verification_async(note_id, transcript, structured_note, draft_timer, patient_id, actor_id)`

The background coroutine dispatched after the SSE stream has delivered the drafted note. Sequence:
1. Logs `verification_started` to the audit trail
2. Calls `verify_note(transcript, structured_note)` (the fail-closed verifier)
3. If the result is not `None`, stamps it with `status=VERIFY_COMPLETE`, timestamps, elapsed time, and updates the note in the DB
4. If the result is `None` (verifier was disabled and returned None, which per the fail-closed contract only happens when `enabled=False`), stores a `VERIFY_FAILED` payload
5. On any exception, stores `VERIFY_FAILED` with the error message
6. All paths write to the audit log

#### POST `/api/scribe/transcribe`

**Auth:** doctor, nurse, or admin role

Accepts a multipart upload with fields: `audio` (file), `hints` (string), `skip_correction` ("true" to skip LLM correction), `patient_id`. Enriches hints from the DB if `patient_id` is provided. Saves the file to `UPLOAD_DIR` with a UUID filename. Calls `transcribe_audio()`. Returns `{"transcript": "...", "audio_path": "/path/to/file"}`.

#### POST `/api/scribe/transcribe-chunk`

**Auth:** doctor, nurse, or admin role

Same as above but for short audio segments (e.g., a 3-5 second chunk from the frontend's rolling buffer). Skips LLM correction (`apply_correction=False`) for speed. Deletes the temp file immediately after transcription. Returns `{"text": "...", "chunk_index": N}` (returns empty text on error rather than raising, so the frontend can continue accumulating chunks).

#### POST `/api/scribe/structure`

**Auth:** doctor, nurse, or admin role

**This is the main endpoint** for turning a transcript into a structured clinical note. Accepts JSON: `{transcript, format, patient_id?, verify?, audio_path?}`.

Returns a **Server-Sent Events (SSE) stream**. The client connects and receives events as the LLM generates tokens. Two things happen inside `event_stream()`:

1. **Streaming draft delivery:** calls `structure_transcript_stream()` and forwards each token as `data: {"content": "token"}\n\n`. The client appends tokens to build the note in real time.

2. **Note persistence + done event:** once all tokens are received, saves the note to the DB with `status="draft"` and `verification_json={"status": "pending"}` (or `"not_requested"` if verify was not requested). Immediately sends the done event: `data: {"done": true, "id": note_id, "format": ..., "status": "draft", "pipeline": {...}, "verification": {"status": "pending"}}\n\n`. The client receives the `note_id` here and can begin polling for verification status.

3. **Background verification:** if `verify=True` and verification is enabled in config, dispatches `_run_verification_async()` as an asyncio task via `asyncio.create_task()`. The SSE connection can now close, verification happens in the background.

The frontend polls `GET /api/scribe/notes/{note_id}/verification` to check when verification completes.

#### GET `/api/scribe/notes`

**Auth:** `scribe.read_all` permission

Returns up to 50 notes. If `patient_id` query param is provided, filters by patient. Otherwise filters by the current user's author ID. Optional `status` filter.

#### GET `/api/scribe/notes/{note_id}`

**Auth:** `scribe.read_all` permission

Returns a single note with all fields. `verification_json` and `pipeline_meta_json` are returned as parsed dicts (not raw JSON strings).

#### GET `/api/scribe/notes/{note_id}/verification`

**Auth:** `scribe.read_all` permission

Lightweight polling endpoint. Returns only `{note_id, verification, pipeline}`. The frontend calls this periodically after `/structure` returns the `done` event until `verification.status` becomes `complete` or `failed`.

#### POST `/api/scribe/notes/{note_id}/verify`

**Auth:** doctor, nurse, or admin role

Manual retry of verification for notes in `failed` state. Guards: verification must be enabled in config, note must exist, current status must not be `complete` or `pending`, no task must already be in flight, and the note must have both a transcript and structured note. Resets status to `pending`, dispatches a new background task, logs to audit trail.

#### PATCH `/api/scribe/notes/{note_id}`

**Auth:** `scribe.edit_draft` permission

Edit the `structured_note` text of a draft. Only works when `status == "draft"`. Calls `update_scribe_note_content()`. Logs to audit trail. Returns `{"ok": True, "note_id": N}`.

#### POST `/api/scribe/notes/{note_id}/sign`

**Auth:** `scribe.sign` permission (typically doctor-only)

Signs a draft note, transitioning it to `signed`. Calls `sign_scribe_note()`. If the note has a `supersedes_note_id`, the original note is transitioned to `superseded` atomically. Both transitions are logged to the audit trail. Returns `{"ok": True, "note_id": N, "status": "signed", "superseded_note_id": M|null}`.

**Current limitation (see TODO in `scribe_service.py`):** signing does not currently enforce a gate requiring clinician acknowledgement when the verification report shows `verified=False` or `verification_status="unverifiable"`. The fail-closed signal is in the data, the gate needs to be added here.

#### POST `/api/scribe/notes/{note_id}/reject`

**Auth:** `scribe.reject` permission

Rejects a draft note, requiring a non-empty reason. Transitions to `rejected`. Logged to audit trail.

#### POST `/api/scribe/notes/{note_id}/revise`

**Auth:** `scribe.edit_draft` permission

Creates a new draft revision from a signed note. The original remains signed until the revision is itself signed. Returns `{"ok": True, "new_note_id": N, "version": V, "status": "draft"}`.

#### GET `/api/scribe/config`

**Auth:** `admin.read` permission

Returns the active pipeline configuration: model names, enabled stages, batch sizes. Does not expose API keys or secret parameters.

#### GET `/api/scribe/daemon/status`

**Auth:** doctor, nurse, or admin role

Sends a ping to the Whisper daemon via ZMQ (with a 1-second timeout) and returns the daemon's status. Useful for debugging whether the warm daemon is running.

---

### Cross-Cutting Safety Properties

#### Prompt injection hardening

Raw ASR transcripts are untrusted. They could contain anything a person said (deliberately or accidentally) that might look like an instruction to the LLM. Every prompt that embeds a transcript:
- Sanitises the text to replace any accidental fence markers
- Wraps the transcript in `<<<BEGIN_DICTATION>>>` / `<<<END_DICTATION>>>` fences
- Includes a SECURITY note telling the LLM to treat everything inside the fences as data, not instructions

#### Fail-closed verification

`verify_note()` returns `None` only when verification is explicitly disabled. Any failure mode (empty transcript, backend error, empty response, unparseable JSON) returns an explicit high-severity "could not verify" report. The design ensures the UI always has an actionable signal, either a passing report, a failing report, or an explicit "could not check" flag.

#### Note immutability after signing

The status guards in `update_scribe_note_content`, `sign_scribe_note`, and `reject_scribe_note` all check `status == "draft"` before proceeding. Signed notes cannot be edited. To change a signed note, a revision must be created, edited, and re-signed, creating an audit trail of every version.

#### No auto-injection of medications or vitals

Scribe does not read the patient's medication list and automatically write it into the note. It only structures what was spoken in the transcript. If a medication was not mentioned in the conversation, it does not appear in the note. The RxNorm corrector fixes spellings; it does not add medications. The reconciler maps names to formulary IDs, it does not populate the structured note. This is the "fail-closed" property as it applies to clinical content fabrication.

#### Audit trail

Every lifecycle transition (edit, sign, reject, revise, verify) calls `log_audit_event()` with a correlation ID, actor ID, patient ID, and before/after status. The audit trail is append-only and cannot be altered through the Scribe API.

---

### Where to Look If Something Goes Wrong

| Symptom | First place to check |
|---|---|
| Transcription is slow | `GET /api/scribe/daemon/status`, if daemon is not running, start it |
| Drug names are wrong in transcript | Check `VOICE["corrector"]["enabled"]` in config; verify the RxNorm DB exists |
| Structured note is empty or garbled | Check Ollama/MLX is running; check `SCRIBE["draft"]["model"]` model name |
| Verification stuck in "pending" | Server restart may have orphaned the task; `reconcile_stale_pending_verifications()` runs on next startup |
| Can't sign a note | Note may not be in `draft` state; check `status` field via `GET /api/scribe/notes/{id}` |
| Drug name in note does not match formulary | `reconcile_medication()` threshold may be too high; check `helix_medication_master` table |
| Socket.IO stream drops audio | Check that the client is sending PCM16 at 16kHz mono; check `is_active` flag on session |
