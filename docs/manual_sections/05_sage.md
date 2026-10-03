## 5. Sage: Clinical Decision Support, RAG, Ambient & Handoff

Sage is Orb's clinical intelligence layer. It answers questions from clinicians in plain language, grounding every answer in retrieved guidelines and live patient data. It also passively listens to Relay case-room conversations and can hand off confirmed AI-detected actions to other Orb modules.

This section covers all five files that make up Sage:

| File | Role |
|---|---|
| `server/services/sage_service.py` | Core query engine: context assembly, RAG retrieval, prompt construction, Ollama call |
| `server/services/clinical_knowledge.py` | Knowledge base: corpus, embedding, vector store build and hybrid search |
| `server/services/sage_ambient_service.py` | Passive ambient observation of Relay case rooms |
| `server/services/sage_handoff_service.py` | Dispatches confirmed ambient actions to downstream modules |
| `server/routes/sage_routes.py` | HTTP/SSE endpoints that wire everything together |

---

### 5.1 `server/services/sage_service.py`: Core Query Engine

This is the brain of Sage. Every time a clinician sends a question, execution passes through this file to: (1) figure out what the question is about, (2) pull the right patient data and guidelines, (3) build a prompt, and (4) stream the LLM's answer back.

#### Libraries

- **`httpx`**, async HTTP client used to talk to Ollama (the local LLM server). `httpx` supports true async streaming, which is how token-by-token output reaches the frontend without waiting for the full response.
- **`re` / `difflib`**, regular expressions and fuzzy string matching, used to identify patient names and detect risk patterns in queries.
- **`json`**, parse Ollama's newline-delimited JSON stream and cache rows.
- `config`, imports `OLLAMA_BASE_URL`, `LLM_MODEL`, `OLLAMA_KEEP_ALIVE` (how long the model stays warm in memory between calls).
- `database`, `get_db()` for one-off reads, `db_session()` for write transactions that must be committed and closed.

#### System Prompts

Two distinct system prompts are defined at module level as module-level constants.

**`SAGE_SYSTEM_PROMPT`**, used for all patient-specific and clinical queries. It instructs the model to:
- Return output **only** inside defined XML tags (`<action_intent>`, `<concise_answer>`, `<display_mode>`, `<patient_facts_summary>`, `<local_policy_guidance>`, `<external_guidance>`, `<clinical_assessment>`, `<caveats>`, `<escalation>`, `<cited_sources>`). Nothing is allowed outside the tags.
- Think through three evidence channels internally: Channel A (patient facts), Channel B (local policy/formulary), Channel C (external guidelines). Channel B overrides Channel C.
- Never fabricate guidelines or patient data.
- Never claim it has modified the chart ("order placed", "I have added", etc.), this is the NO_ACTION_HALLUCINATION rule.
- Treat anything inside the clinical context block as data, not as instructions, this is the **prompt-injection hardening rule** (rule 7).

**`SAGE_BROAD_SYSTEM_PROMPT`**, a leaner prompt used only for broad census queries (e.g., "show me all patients"). It outputs plain text, not XML, because the overhead of the XML contract is unnecessary when summarising a ward census.

⚠️ **If you change these prompts**: any change to tag names in `SAGE_SYSTEM_PROMPT` must be reflected in `sage_routes.py` where tag names are parsed out of the response with regex. The NO_ACTION_HALLUCINATION and prompt-injection rules are safety-critical; do not weaken or remove them.

#### Risk Classification

Two lists of regex patterns, `HIGH_RISK_PATTERNS` and `MODERATE_RISK_PATTERNS`, categorise clinical queries by severity. `_classify_risk_level(query)` iterates both lists and returns `"high"`, `"moderate"`, or `"standard"`. A high-risk classification sets the `escalation` flag, which the route layer uses to force `display_mode = "detailed"` and surface a recommendation to escalate to the attending physician.

#### Mutation Detection

`detect_mutation_intent(query)` applies `MUTATION_PATTERNS` (regex) to detect whether the clinician is trying to change the chart (e.g., "add paracetamol", "stop heparin"). This is a fast, deterministic check that runs **before** the LLM is called. If a mutation is detected, the route layer applies the action-state policy override regardless of what the model says, it will never silently execute a chart change.

#### Scope Configurations (`SCOPE_CONFIGS`)

Three pre-set Ollama inference configurations trade speed against depth:

| Scope | `num_ctx` (tokens) | `num_predict` | `temperature` | When used |
|---|---|---|---|---|
| `broad` | 8 192 | 1 024 | 0.2 | Full census / ward-round queries |
| `patient` | 8 192 | 2 048 | 0.4 | Single-patient deep-dive |
| `simple` | 2 048 | 512 | 0.4 | General medical questions, greetings |

#### `query_ollama(prompt, context, conversation_history, scope)`: Ollama Streaming Call

This is an `async` generator. It yields string chunks as they arrive from Ollama so the HTTP response to the frontend streams in real-time.

**Steps:**

1. Picks the system prompt (broad vs. full XML) based on `scope`.
2. If `context` is provided, it is wrapped in `<<<BEGIN_CLINICAL_CONTEXT>>>` / `<<<END_CLINICAL_CONTEXT>>>` delimiters. Any occurrence of the closing fence inside the data is neutralised to prevent record content from breaking out of the data block. The system prompt already tells the model to treat this block as data only, this fencing is a second layer of defence.
3. Conversation history is appended (trimmed to last 6 turns, each capped at 1 000 chars, and only for non-simple scopes, to prevent stale patient context from leaking into unrelated questions).
4. Acquires the **LLM queue slot** (`llm_queue.acquire(priority=1)`) from `services.llm_queue_service`. The on-device MedGemma model can only serve one request at a time; the queue prevents GPU thrashing when multiple clinicians query simultaneously. The slot is always released in the `finally` block.
5. Opens an `httpx` streaming POST to `OLLAMA_BASE_URL/api/chat`. Timeout is generous (300 s read) because the model may be slow on CPU.
6. Each line of the response is parsed as JSON. The `message.content` field contains the next token fragment. A `strip_markdown` inner function removes bold/italic syntax and bullet prefixes that the model sometimes emits, they would render badly in the frontend.
7. When Ollama signals `done: true`, the generator exits.
8. If the HTTP client is closed mid-stream (client disconnected), `GeneratorExit` is caught, the connection is force-closed, and Ollama stops generating. This prevents wasted CPU on abandoned requests.

⚠️ **If you change this**: changing `OLLAMA_BASE_URL` or `LLM_MODEL` affects all Sage calls. Changing `num_ctx` without testing on your hardware can cause out-of-memory crashes on the 8 GB Mini. Always release the queue slot in a `finally` block, a leaked slot will deadlock all future Sage queries.

#### `_assemble_patient_context(db, patient_id)`: Pulling Patient Data

Given a database connection and a patient ID, this function queries **six tables** and assembles a structured dictionary that becomes Channel A (patient facts) in the prompt. It also tracks what data is missing.

| Data pulled | Table | Detail |
|---|---|---|
| Demographics | `patients` | name, age, sex, bed, ward, diagnosis |
| Vitals | `vitals` | Last 6 readings; latest 2 formatted; simple ↑/↓/→ trend for HR and SpO2 |
| Risk score | `risk_scores` | Most recent score, level, and drivers |
| Medications | `medications` | All non-discontinued drugs: name, dose, route, frequency |
| Labs | `clinical_documents` | Last 2 lab results (first 200 chars each) |
| Scribe notes | `scribe_notes` | Last signed structured note or raw transcript (truncated to 500 chars) |
| Imaging | `lens_analyses` | Most recent Lens analysis (first 200 chars) |

A `completeness` sub-dict records which categories are missing. If a clinical query is being answered but key data is absent, the confidence level is downgraded to `"medium"` so the frontend can flag this to the clinician.

#### `get_relevant_context(query, patient_id, history)`: Query Routing and Context Assembly

This is the main orchestration function. It returns a 7-tuple:

```
(context_string, scope, sources_list, confidence_state, escalation_flag, patient_context_dict, query_scope)
```

The function walks through a decision tree:

**1. Broad query detection.** If the query contains any of ~40 keywords (`"all patients"`, `"census"`, `"ward round"`, etc.), it runs three efficient batched SQL queries to gather every admitted patient's demographics, latest vitals, and latest risk score in one pass, instead of the O(N) per-patient approach. This path returns immediately with scope `"broad"`.

**2. Patient name/bed/ward matching.** For every admitted patient, the function checks whether the query contains the patient's name (token-by-token), bed ID, or ward ID. It also does **fuzzy matching** using `difflib.SequenceMatcher` with a 0.70 similarity threshold, this catches ASR mis-transcriptions (e.g., "stress ready" → "Suresh Reddy"). Discharged patients are also checked, but only if no admitted patient matched.

**3. Pronoun detection.** If no explicit patient was named but the query contains pronouns like "he", "her", "this patient", the function tries to resolve the referent from the passed `patient_id` argument, or by scanning recent conversation history for a name.

**4. Query scope classification.** Based on the above, the query is assigned one of these scopes:
- `DISAMBIGUATE`, query matched more than one patient; ask the clinician to clarify
- `PATIENT_SPECIFIC`, explicit single patient match
- `CONTEXTUAL_FOLLOWUP`, pronoun resolved to a known patient
- `GENERAL_MEDICAL`, no patient context needed; answer from guidelines only
- `AMBIGUOUS`, pronoun but no patient could be resolved

**5. Patient context assembly.** For `PATIENT_SPECIFIC` and `CONTEXTUAL_FOLLOWUP`, the function checks `sage_decision_cache` for a recent pre-compiled context JSON. Cache hit = fast path. Cache miss = call `_assemble_patient_context`.

**6. Guideline retrieval.** `_get_guideline_context(query)` is called to run the RAG search (see §5.2 below). Results are formatted into Channel B (local policy) and Channel C (external guidelines) strings.

**7. 3-Channel context assembly.** Patient facts, local policy, and external guidelines are concatenated with clear header dividers that match the channel names the system prompt describes.

#### `_get_guideline_context(query)`: RAG Search and Confidence Gating

This helper calls `search_guidelines` from `clinical_knowledge.py` (covered in §5.2), then formats the results and computes a confidence level.

**Confidence logic** uses two signals:
- **Fusion score**, the RRF (Reciprocal Rank Fusion) score from hybrid search; reflects how well the retrieved chunks ranked across both dense and lexical search.
- **Lexical overlap**, fraction of non-trivial query words that appear literally in the top result's text. This catches cases where vector similarity is high but the topic is actually different.

A **relevance floor** is applied: if the top result's fusion score is below `0.02` AND lexical overlap is below `0.15`, the retrieval is treated as irrelevant and nothing is returned. This prevents Sage from citing the "closest" guideline as authoritative when the corpus contains nothing on-topic.

Confidence thresholds (currently magic numbers, see the inline comment noting this is not properly tuned):
- `high`: fusion score > 0.04 or lexical overlap > 0.5
- `medium`: fusion score > 0.025 or lexical overlap > 0.3
- `low`: otherwise

Results are split into local (trust tier 1 or 2) and external (tier 3) buckets and emitted under the matching channel header. Each entry is tagged with its prefix: `[LOCAL POLICY - SYNTHETIC DEMO]`, `[LOCAL FORMULARY]`, `[LOCAL POLICY]`, or `[EXTERNAL GUIDELINE]`.

⚠️ **If you change this**: the magic-number thresholds (`0.04 / 0.025 / 0.5 / 0.3`) are not tuned against a held-out evaluation set. A larger or different corpus will require re-tuning. The relevance floor prevents false positives but can also suppress valid results if the corpus grows but the floor is not adjusted.

#### Conversation History Functions

`save_conversation(user_id, role, content, sources)`, inserts a single turn into `sage_conversations`. Called after every query (both user message and assistant response). Sources metadata is stored as a JSON string for auditing.

`get_conversation_history(user_id, limit)`, fetches the last `limit` turns in chronological order. It applies a cleanup step: if two consecutive `user` messages appear (which happens when a stream was aborted before the assistant replied), the older one is dropped to prevent the LLM from seeing an orphaned prompt.

`clear_sage_history(user_id)`, deletes all conversation turns for a user.

---

### 5.2 `server/services/clinical_knowledge.py`: RAG Knowledge Base

This file owns the clinical knowledge corpus, 25 hard-coded documents, and the pipeline that embeds them into a hybrid vector + full-text search store. It is the backing library for Sage's guideline retrieval.

#### Libraries

- **`FlagEmbedding.BGEM3FlagModel`**, the BGE-M3 embedding model. Converts text to dense floating-point vectors (default 1 024 dimensions). BGE-M3 was chosen for its strong multilingual and biomedical performance. Uses FP16 to halve memory usage.
- **`numpy`**, converts the embedding float array to raw bytes (`np.float32.tobytes()`) for storage in sqlite-vec.
- **`sqlite_vec`**, a SQLite extension that adds approximate nearest-neighbour vector search. Loaded at runtime with `db.enable_load_extension(True)`.
- **`hashlib` / `json`**, compute a SHA-256 fingerprint of the entire corpus to detect whether a rebuild is needed on startup.
- `config` imports: `DB_PATH`, `EMBEDDING_MODEL` (BGE-M3 model name), `EMBEDDING_DIM` (1 024), `SAGE_RERANKING_ENABLED`.

#### Lazy Embedding Model

`_get_embedder()` loads the BGE-M3 model on first use (not at import time). If `FlagEmbedding` is not installed, it returns a `MockEmbedder` that produces zero vectors, this allows the schema to be initialised even in environments where the model is not yet downloaded (for example, on the 8 GB Mini before the model is pulled). The real embedder is a singleton stored in the module-level `_embed_model` variable.

`_embed_text(text)`, embeds a single string, returns raw bytes.
`_embed_texts(texts)`, batch-embeds a list of strings (batch size 8). More efficient than calling `_embed_text` in a loop.

#### The Corpus (`CLINICAL_DOCUMENTS`)

25 documents defined as Python dicts at module level. Each document has:

| Field | Purpose |
|---|---|
| `document_id` | Unique identifier (e.g., `"EXT-SEPSIS-001"`) used in citations |
| `category` | Clinical domain (e.g., `"Sepsis"`, `"Cardiac"`) |
| `title` | Human-readable name |
| `source` | Authoritative source (e.g., `"Surviving Sepsis Campaign (SSC) 2021"`) |
| `source_type` | `"hospital_sop"`, `"local_formulary"`, or `"external_guideline"` |
| `source_origin` | `"demo_local"` or `"external"` |
| `trust_tier` | 1 = hospital SOP, 2 = local formulary, 3 = external guideline. Tier 1/2 override tier 3. |
| `specialty` | Medical specialty |
| `effective_date` | Date the guideline came into effect |
| `version` | Version string |
| `is_synthetic` | `True` for demo/placeholder SOPs, `False` for real guidelines |
| `content` | The actual guideline text |

Current corpus breakdown:
- 2 local SOPs (tier 1, synthetic): infection control, code blue
- 1 local formulary entry (tier 2, synthetic): empiric sepsis antibiotics
- 22 external guidelines (tier 3, real): sepsis bundles, STEMI, ARDS, DKA, stroke, heart failure, CAP, UTI, VTE, potassium correction, ventilator weaning, transfusion, ICU sedation

⚠️ **If you change the corpus**: adding, removing, or editing any document changes the corpus fingerprint, which triggers a full rebuild (DROP + re-embed all documents) on the next server restart. This takes several minutes on the 8 GB Mini because BGE-M3 must encode every chunk. The rebuild is automatic; no manual step is needed. However, changing the **embedding model** or **`EMBEDDING_DIM`** also changes the fingerprint and triggers a rebuild, and any existing embeddings become incompatible with the new model. Both must be changed together.

#### Query Expansion

`MEDICAL_ABBREVIATIONS` and `DRUG_ALIASES` are two dicts mapping clinical shorthand to full terms. `_expand_query(query)` appends the expanded forms to the query string before searching. For example, a query containing `"dka"` becomes `"dka diabetic ketoacidosis"`, improving recall in both vector and full-text search.

#### Text Chunking

`chunk_text(text, words_per_chunk=150, overlap=50)` splits a long document into overlapping windows. Documents short enough to fit in 150 words are returned as a single chunk. For longer documents, consecutive chunks share 50 words, so context at chunk boundaries is preserved. This matters because semantic meaning often spans sentence groups.

#### Corpus Fingerprint

`_corpus_fingerprint()` computes a SHA-256 hash of: the embedding model name, the embedding dimension, and the full JSON-serialised corpus. This fingerprint is stored in a `clinical_knowledge_meta` table on first build. On subsequent server restarts, `init_knowledge_base()` compares the stored fingerprint to the current one. If they match and the `clinical_knowledge` table has rows, the rebuild is skipped entirely, making normal server restarts fast.

#### `init_knowledge_base()`: Knowledge Base Initialisation

Called once on server startup. Controlled by the environment variable `ORB_SKIP_KNOWLEDGE_BASE=1` (set this to skip the build during fast boots or demo mode before BGE-M3 is downloaded, RAG retrieval is disabled when skipped).

**Build steps (only runs when fingerprint changed or first run):**

1. Creates `clinical_knowledge_meta` (stores fingerprint).
2. Drops and recreates three tables:
   - `clinical_knowledge`, metadata table: one row per chunk (document_id, chunk_index, category, title, content, source, trust_tier, is_synthetic, etc.).
   - `clinical_knowledge_vec`, sqlite-vec virtual table: stores `float[1024]` embeddings. Each row's `rowid` matches the corresponding `clinical_knowledge` row.
   - `clinical_knowledge_fts`, SQLite FTS5 virtual table: full-text index on title, content, source, category, specialty. Falls back gracefully if FTS5 is unavailable.
3. For each document, calls `chunk_text` to split it. For each chunk, builds a **composite embedding text**: `"Title: {title}\nCategory: {category}\nSource: {source}\nContent: {chunk}"`, this gives the embedding model more context than the raw chunk alone.
4. Batch-embeds all composite texts with `_embed_texts`.
5. Inserts each chunk's metadata into `clinical_knowledge`, the embedding bytes into `clinical_knowledge_vec` (using the matching `rowid`), and the text into `clinical_knowledge_fts`.
6. Writes the fingerprint to `clinical_knowledge_meta`.

⚠️ **If you change this**: the schema columns in `clinical_knowledge` must match what `search_guidelines` selects. If you add a column here, add it to the SELECT in `search_guidelines` too. The sqlite-vec extension must be available as a shared library; if it is missing the build silently aborts.

#### `search_guidelines(query, limit=5)`: Hybrid Search with RRF Fusion

This is the function called by `sage_service._get_guideline_context` for every query. It implements a hybrid retrieval pipeline:

**RAG pipeline end-to-end (worked example):**

Suppose a clinician asks: *"What antibiotics for sepsis?"*

1. **Query expansion**: `_expand_query` detects no abbreviations in this query, so it passes through unchanged.

2. **Dense retrieval (vector search)**:
   - `_embed_text("What antibiotics for sepsis?")` calls BGE-M3, producing a 1 024-dimensional float vector.
   - sqlite-vec executes: `SELECT rowid, distance FROM clinical_knowledge_vec WHERE embedding MATCH ? AND k = 15`, approximate nearest-neighbour search over all stored embeddings using cosine distance.
   - Returns up to 15 results with their distances. Distance is converted to similarity (`1.0 - distance`), and each result gets a rank (1 = closest).

3. **Lexical retrieval (FTS5 keyword search)**:
   - The query is cleaned (punctuation stripped, words shorter than 4 chars dropped) and joined with ` OR `: `"what OR antibiotic OR sepsis"`.
   - FTS5 BM25 ranking returns up to 15 results ordered by relevance score.
   - Each result gets a lexical rank.

4. **Reciprocal Rank Fusion (RRF)**:
   - For every unique `rowid` that appeared in either result set, RRF computes:
     ```
     score = 1/(60 + dense_rank) + 1/(60 + lexical_rank)
     ```
     where `k=60` is a smoothing constant. Results that only appeared in one list get a default rank of 1000 (effectively penalised). The `k=60` value is a standard RRF default and means neither list's top result dominates the other entirely.
   - Results are sorted by descending RRF score. Top `limit` (default 5) are kept.

5. **Metadata fetch**: The winning `rowid`s are used to fetch full metadata from `clinical_knowledge` (title, content, source, trust_tier, is_synthetic, etc.).

6. **Retrieval method annotation**: Each result is tagged as `"dense"`, `"lexical"`, or `"hybrid"` depending on which list ranked it higher.

7. **Optional reranking** (`SAGE_RERANKING_ENABLED`): Placeholder for a cross-encoder reranker. Not yet implemented, currently a no-op.

Result: a list of dicts, each representing a retrieved guideline chunk, sorted by relevance. This list is returned to `sage_service._get_guideline_context`, which applies the confidence gate and formats the channel B/C context strings.

⚠️ **If you change this**: the `k=60` RRF constant and the confidence thresholds in `_get_guideline_context` are calibrated to each other. Changing one without the other may produce erratic confidence scores. If the corpus grows significantly, the relevance floor and confidence thresholds will need re-tuning. If you change `EMBEDDING_DIM` you must also change the `vec0` table definition, the dimension is baked into the schema.

---

### 5.3 `server/services/sage_ambient_service.py`: Passive Ambient Observation

Sage Ambient is a completely silent, background layer. It watches every message sent to Relay case rooms and extracts clinical signals, without ever sending a message, surfacing a notification, or blocking the clinician's workflow. All output is written to internal tables (`sage_room_memory`, `sage_observations`, `sage_actions`) for consumption by other services or the frontend.

Controlled by `SAGE_AMBIENT_ENABLED` config flag. Key config values:
- `SAGE_AMBIENT_DEBOUNCE_SECONDS`, minimum time between processing cycles for the same room
- `SAGE_AMBIENT_CONTEXT_WINDOW`, how many recent messages to include in the LLM prompt
- `SAGE_AMBIENT_MAX_SUMMARY_CHARS`, rolling cap on the stored discussion summary
- `SAGE_AMBIENT_MAX_INTENTS`, max number of pending action intents stored per room
- `SAGE_AMBIENT_TIMEOUT_SECONDS`, how long to wait for the LLM classification call
- `SAGE_AMBIENT_SAMPLE_RATE`, fraction of signal-bearing messages to send to the LLM (for phased rollout; 1.0 = all)

#### Libraries

- **`httpx`**, async HTTP call to Ollama for classification (non-streaming `api/generate`).
- **`re`**, acknowledgment and clinical signal patterns.
- **`difflib`**, not used here (that's `sage_service`); signal detection is purely regex.
- **`collections.deque`**, bounded ring buffer for message deduplication.
- **`hashlib`**, SHA-256 content hashing for dedup.
- **`ws.realtime.sio`**, Socket.IO instance; used to emit `sage:memory_update` events when room memory changes.
- `database.db_session`, all DB writes.

#### The 5-Gate Pipeline

`sage_ambient_observe(channel_id, message_id)` is the entry point. It is called as a FastAPI `BackgroundTask` from `relay_routes` after each new Relay message is saved. It **must never raise** and **must never block**, it catches all exceptions internally.

The function applies gates in sequence. Each gate is cheap; the expensive LLM call only happens if all gates pass.

**Gate 1, Sender check** (`_gate_sender`): Skip if the sender is a bot (`sender_id` starts with `"bot_"`) or system account (`sender_role` is `"system"` or `"intelligence"`). Prevents Sage from observing its own outputs or automated alerts.

**Gate 2, Room check** (`_gate_room`): Queries `channels` table; only processes messages from rooms with `type = "case_room"` AND a linked `patient_id`. Relay channels that are general discussion rooms are ignored.

**Gate 3, Content check** (`_gate_content`): Skip if the message is fewer than 8 characters, or matches `_ACK_PATTERN`, a regex covering common acknowledgements ("ok", "thanks", "noted", "👍", etc.). These carry no clinical signal.

**Gate 4, Dedup check** (`_gate_dedup`): An in-memory ring buffer (`_RECENT_HASHES`, bounded at 10 entries per room) stores SHA-256 hashes of recent normalised message texts. A message is a duplicate if: same hash + same sender within 60 seconds, OR same hash from any sender within 10 seconds. Prevents double-processing when messages are re-sent or edited.

**Gate 5, Signal check** (`_gate_signal`): `_SIGNAL_PATTERNS` is a large regex covering: clinical deterioration words ("wors", "deteriorat", "desat"), care decisions ("start", "stop", "prescribe", "consult"), vital signs ("spo2", "bp", "hr"), and medication terminology ("dose", "mg", "mcg", "drip"). A message that passes this gate likely contains actionable clinical content. Messages that fail it still update the room memory counter (to track message volume), but no LLM call is made.

**Sampling gate**: If `SAGE_AMBIENT_SAMPLE_RATE < 1.0`, a random float decides whether this particular signal message proceeds to the LLM. This is for phased rollout: start at 10% to validate LLM behaviour before going to 100%.

#### LLM Classification

If all gates pass, the function:
1. Fetches patient demographics and current medications (`_fetch_patient_context`).
2. Fetches the last `SAGE_AMBIENT_CONTEXT_WINDOW` messages from the room (`_fetch_recent_messages`).
3. Loads existing room memory from `sage_room_memory` (`_load_room_memory`).
4. Builds a classification prompt (`_build_prompt`) combining: patient context (name, age, diagnosis, risk, up to 5 meds), current concern and discussion summary from room memory, last 5 recent messages (truncated to 150 chars each), and the triggering message.
5. Calls Ollama's `api/generate` endpoint (non-streaming) with `_CLASSIFY_SYSTEM` prompt (`_classify_with_llm`). Temperature is 0.1 (near-deterministic), max 300 tokens, 4096 context.

The LLM is asked to classify the message into exactly one of:
- `coordination_update`, logistical coordination between clinicians
- `summary_update`, new clinical information about the patient
- `possible_action`, a suggested clinical action (medication, procedure, etc.)
- `risk_flag`, a safety concern or potential adverse event
- `escalation_signal`, something requiring urgent attention
- `topic_shift`, the conversation has changed subject

The response must be strict JSON (no markdown).

Note: This LLM call does NOT go through the `llm_queue` used by the main Sage query path. Ambient is called as a background task that should degrade gracefully; queue integration is a future improvement.

#### Memory Update (`_update_memory`)

After classification (or even if it failed), the room memory is upserted into `sage_room_memory` using `INSERT OR REPLACE`. This table has one row per room and tracks a rolling structured picture of the conversation:

| Field | What it holds |
|---|---|
| `patient_summary` | Static patient info |
| `discussion_summary` | Rolling text summary (capped at `SAGE_AMBIENT_MAX_SUMMARY_CHARS`); new `summary_delta`s are appended; old content is trimmed at sentence boundaries |
| `current_concern` | The primary clinical concern; updated from `concern_update` or derived from `signal` |
| `likely_next_steps` | JSON array, capped at 5 |
| `unresolved_questions` | JSON array, capped at 5 |
| `active_intents` | JSON array of detected action intents (for backward compatibility); capped at `SAGE_AMBIENT_MAX_INTENTS` |
| `risk_flags` | JSON array, capped at 5; duplicates are merged and confidence updated |
| `participants` | JSON array of sender names seen so far, capped at 20 |
| `message_count` | Total messages processed |
| `llm_call_count` | How many reached the LLM |
| `last_processed_msg_id` / `last_processed_at` | Freshness tracking |

When a `possible_action` is classified, the intent is also written to `sage_actions` (a separate staging table for Stage 3 action proposals) and `sage_action_events` (audit log). Duplicate actions (same type + target, in `suggested` or `acknowledged` state) are merged rather than duplicated.

When a `risk_flag` is classified, the risk detail is merged into `risk_flags` using substring deduplication.

After memory is updated, a `sage:memory_update` Socket.IO event is emitted to the room. Frontend panels subscribed to this room can refresh their ambient sidebar.

#### Observation Write (`_write_observation`)

For non-trivial classification types, a row is inserted into `sage_observations` with: `observation_type`, a JSON payload of the signal/intent/risk/summary, and the `confidence` score. This table is a permanent audit trail of what Sage observed.

#### Voice Prewarm

If the message content contains phrases like `"hey sage"` or `"speak to sage"`, the function publishes a `relay.intent.voice_prewarm` event on the internal event bus. This is a hint to the voice agent service to warm up the model before the clinician switches to voice mode.

⚠️ **If you change this**: the LLM classification JSON schema (`_CLASSIFY_SYSTEM`) and `_update_memory` must stay in sync, the classification types are matched by string in `_update_memory`. The `sage_room_memory`, `sage_observations`, `sage_actions`, and `sage_action_events` tables are all written here; schema changes require migration. Ambient runs as a background task and must never raise to the caller.

---

### 5.4 `server/services/sage_handoff_service.py`: Action Dispatch to Downstream Modules

This file is Stage 4 of the Sage Ambient pipeline. When a clinician reviews and confirms an ambient-detected action (via the frontend), this service dispatches it to the appropriate Orb module: Helix (pharmacy orders), Scribe (clinical documentation), or the Admissions module (patient transfers). It maintains an auditable `sage_handoffs` + `sage_handoff_events` log.

#### Libraries

- `uuid`, generate unique handoff IDs.
- `json`, serialise action payloads.
- `database.db_session`, all DB writes (committed and closed).
- `services.helix_service.create_order`, creates a pharmacy order in Helix.
- `services.scribe_service.save_scribe_note`, saves a clinical note via Scribe.

#### `create_and_dispatch_handoff(action, actor_id)`: Entry Point

Creates a durable handoff record for a confirmed ambient action, then dispatches it.

1. **Idempotency check**: queries `sage_handoffs` for an existing record with the same `source_action_id` in `pending` or `accepted` state. If found, returns the existing record without re-dispatching. This prevents double execution if the clinician clicks confirm twice.
2. Inserts a new row in `sage_handoffs` with `status = "pending"` and logs the initial event in `sage_handoff_events`.
3. Calls `_dispatch_handoff` to route to the correct adapter.

#### `_dispatch_handoff(handoff_id, action, actor_id)`: Router

Routes by `action_type`:
- `"prescribe"` → `_dispatch_to_helix`
- `"document"` → `_dispatch_to_scribe`
- `"transfer"` → `_dispatch_to_transfer`
- anything else → `status = "unsupported"`

After dispatch, updates `sage_handoffs` with: `target_module`, `new_status` (`accepted`, `failed`, or `unsupported`), `downstream_ref_id` (the ID returned by the downstream module), `error_summary` (if failed), and `last_attempted_at`. Logs the status transition in `sage_handoff_events`.

#### `_dispatch_to_helix(action, actor_id)`: Pharmacy Order

Calls `create_order` with the patient ID and medication name from the action. Dose, route, and frequency are extracted from the action's `rationale` text using simple heuristics (checks for " iv ", " im ", " sc " substrings). If those signals are absent, defaults of `"TBD"` are used so a pharmacist must complete the order before it is dispensed. Returns the Helix `order_id` as the `downstream_ref_id`.

⚠️ **This is safety-critical**: dose and route extraction from free-text rationale is heuristic and can be wrong. A pharmacist review step is mandatory for any prescription created through this path.

#### `_dispatch_to_scribe(action, actor_id)`: Clinical Note

Calls `save_scribe_note` with a synthetic transcript: `"Sage Ambient Observation: {target}. {rationale}"`. The note is marked `ai_generated=True` and format `"soap"`. The structured note is `None` on creation, it would be structured asynchronously by Scribe if configured. Returns the Scribe note ID.

#### `_dispatch_to_transfer(action, actor_id)`: Patient Transfer

This is the most complex adapter:

1. Finds the patient's active admission in the `admissions` table and updates its `department` to the target department.
2. Posts a system message to the Relay case room (`"SYSTEM: Patient transfer to {dept} confirmed and initiated."`).
3. For the demo patient `PAT007`, activates a pre-seeded surgery record `SURG-ALEX-EM` by setting its status to `"emergency_ready"`.
4. For all other patients, creates a new surgery record in `"emergency_pending"` status. The procedure name is inferred from keywords in the rationale ("hartmann" → Hartmann's procedure, "laparotomy" → exploratory laparotomy, otherwise a generic default).
5. Schedules an async background task `generate_transfer_surgery_package_async` to populate the surgery record with AI-generated risk assessment, checklist, instruments, and staff.

#### `generate_transfer_surgery_package_async(surgery_id)`: Surgery Package Generation

An async function that generates a structured pre-operative package for emergency surgical transfers:

1. Sets `status = "emergency_context_generating"` and emits a `surgery:emergency_intake` Socket.IO event so the frontend shows a "generating" state.
2. Waits 2 seconds (simulating AI processing time in the demo).
3. Populates hard-coded but realistic demo data: ASA classification, risk assessment narrative, complication list, success probability (78.5%), surgical checklist, required instruments, and required staff roles.
4. Updates the `surgeries` table with all generated fields and sets `status = "emergency_ready"`.
5. Emits `surgery:emergency_intake` again to signal completion.

⚠️ **If you change this**: the `sage_handoffs` / `sage_handoff_events` schema is the audit trail for all confirmed AI actions. Do not alter it without a migration. The Helix and Scribe service interfaces (`create_order`, `save_scribe_note`) are the contracts between Sage and those modules; if those functions change signature, update the dispatch adapters here. The demo-patient hard-coding (`PAT007`, `SURG-ALEX-EM`) must be removed before production use.

---

### 5.5 `server/routes/sage_routes.py`: HTTP Endpoints

This file defines the FastAPI router mounted at `/api/sage`. It is the public interface between the frontend (SagePanel, floating assistant) and the service layer.

#### Libraries / Imports

- `fastapi.StreamingResponse`, sends the response as a Server-Sent Events (SSE) stream, so the frontend receives tokens as they arrive rather than waiting for the full answer.
- `auth.get_current_user` / `permissions.require_permission("sage.query")`, every Sage endpoint checks that the caller is authenticated and has the `sage.query` permission.
- All functions from `sage_service`, the route layer is deliberately thin; it delegates all logic to the service.

#### `POST /api/sage/query`: Main Sage Query Endpoint

The endpoint the frontend's SagePanel calls when a clinician submits a question.

**Request body:**
```json
{"query": "What is the sepsis antibiotic protocol?", "patient_id": "PAT001"}
```

**Flow:**

1. **Mutation detection**: `detect_mutation_intent(query)` runs immediately. This is deterministic and fast, it does not involve the LLM.

2. **History**: Fetches the last 6 conversation turns (before saving the new message) to provide context to the LLM.

3. **Context retrieval**: `get_relevant_context(query, patient_id, history)` runs the full query routing and RAG pipeline. The return value includes `scope`, either `"broad"` (plain text) or `"patient"`/`"simple"` (XML contract).

4. **Streaming strategy**: For `scope == "broad"`, the raw token stream is sent directly to the client as it arrives (fastest, lowest latency). For all other scopes, tokens are buffered server-side; only the sanitised `<concise_answer>` is emitted after parsing.

5. **`stream_response()` async generator**:
   - Iterates `query_ollama(...)` and accumulates `full_response`.
   - If the client disconnects mid-stream (`request.is_disconnected()`), the loop breaks and the Ollama stream is aborted.
   - After the stream completes, XML tags are extracted with regex from `full_response`. Each named tag (`action_intent`, `concise_answer`, `display_mode`, etc.) becomes a key in `parsed_json`.

6. **Action-state policy override**: If either the deterministic mutation detector OR the model's own `<action_intent>` tag indicates a chart change is requested:
   - The system queries `helix_medication_master` to check if the requested medication exists in the pharmacy database.
   - If found: builds an `action_state` dict with `status_code = "request_drafted"` and a safe user message asking the clinician to review and approve.
   - If not found: `status_code = "not_in_helix_database"`.
   - For non-medication mutations: `status_code = "unsupported"`.
   - In all cases, the `<concise_answer>` is **overwritten** with the safe policy message. The model's original answer is discarded.

7. **Forbidden-phrase strip**: `_strip_forbidden` runs unconditionally on `concise_answer` and `clinical_assessment`, removing self-confirmation phrases ("order placed", "I have added", "Done", etc.) even on non-mutation queries. This is the final safety net.

8. **Source validation**: `cited_sources` from the model response are cross-checked against the document IDs actually retrieved. Only validated citations are emitted; hallucinated document IDs are silently dropped. If the model cited something it wasn't given, confidence is downgraded to `"low"`.

9. **Display mode decision**: The app overrides the model's suggested `display_mode`. It forces `"detailed"` if: model requested detailed, OR escalation flag is set, OR confidence is low, OR patient data is incomplete. Otherwise `"concise"`.

10. **Metadata emission**: A `metadata` object is emitted as an SSE event containing:
    - `action_state`, for frontend to render the medication-draft UI
    - `sources`, validated cited sources with trust tier and fusion score
    - `confidence`, high/medium/low
    - `escalation`, boolean
    - `display_mode`, concise/detailed
    - `query_scope`, PATIENT_SPECIFIC, GENERAL_MEDICAL, etc.
    - `patient_context_used`, `patient_id`, `patient_name`, `patient_source`
    - `data_completeness`, which data categories were missing
    - `local_policy_found`, whether tier 1/2 sources were retrieved
    - `timing`, retrieval, time-to-first-token, total milliseconds

11. **Structured response**: The full `parsed_json` is also emitted as `structured_response` for frontend components that render the detailed expanded view.

12. **Conversation save**: After the stream (in `finally`), both the user message and the full assistant response (with metadata) are saved to `sage_conversations`.

⚠️ **If you change this**: the XML tag names parsed here must match the tags defined in `SAGE_SYSTEM_PROMPT` in `sage_service.py`. The forbidden-phrase strip and action-state override are the primary safeguards against hallucinated chart mutations, do not remove them. The SSE format (`data: {json}\n\n`) is consumed by the SagePanel frontend component; changing it breaks the UI. The `timing` field is used by the SagePanel performance overlay; removing it causes a frontend error.

#### `GET /api/sage/history`: Conversation History

Returns the authenticated user's conversation history as a JSON list. Uses `get_conversation_history` with no limit override.

#### `DELETE /api/sage/history`: Clear Conversation

Deletes all conversation rows for the authenticated user.

#### `POST /api/sage/voice`: Voice Agent Endpoint

A parallel SSE endpoint for conversational voice queries. Differs from `/query` in that:
- It accepts `conversation_history` in the request body (the voice agent maintains its own history client-side rather than relying on `sage_conversations`).
- It streams tokens in `{"token": "..."}` format (not `{"content": "..."}`) for compatibility with the TTS pipeline.
- It delegates to `voice_agent_service.get_voice_agent().generate_stream(...)` rather than `query_ollama`.
- It checks `VOICE["conversational"]["enabled"]` from config before proceeding.
- It saves conversation turns prefixed with `[voice]` to distinguish them from text queries.

The voice endpoint is gated by `sage.query` permission (same as the text endpoint).

⚠️ **If you change this**: the voice endpoint is consumed by the frontend's floating voice assistant; the `{"token": ...}` event format is expected by the TTS token-streaming logic. Do not merge this with the main query endpoint without updating the frontend.

---

### 5.6 Cross-Cutting Notes

#### RAG Pipeline End-to-End Summary

```
Clinician types: "What antibiotics for sepsis?"
        │
        ▼
sage_routes: POST /api/sage/query
        │
        ▼
get_relevant_context()
  ├── _classify_risk_level() → "high" (sepsis pattern)
  ├── No patient name match → scope = GENERAL_MEDICAL
  └── _get_guideline_context()
        ├── _expand_query() → query unchanged (no abbreviations)
        ├── _embed_text() [BGE-M3] → 1024-dim float vector
        ├── sqlite-vec KNN search → top 15 by cosine similarity
        ├── FTS5 BM25 search → top 15 by keyword relevance
        ├── RRF fusion → combined ranked list
        ├── Relevance floor check → passes (sepsis is well-covered)
        └── Confidence = "high" (EXT-SEPSIS-001, FORM-ID-001 match well)
        │
        ▼
3-channel context assembled:
  Channel A: [none, no patient matched]
  Channel B: FORM-ID-001 (local ESBL/sepsis formulary)
  Channel C: EXT-SEPSIS-001/002/003 (SSC guidelines)
        │
        ▼
query_ollama()
  ├── llm_queue.acquire(), wait for GPU slot
  ├── SAGE_SYSTEM_PROMPT + fenced context + question
  ├── Ollama streams tokens → yield to stream_response()
  └── llm_queue.release()
        │
        ▼
stream_response() buffers XML, then:
  ├── Parses <concise_answer>, <cited_sources>, etc.
  ├── Validates citations against retrieved document IDs
  ├── Forbidden-phrase strip (unconditional)
  ├── Emits sanitised concise_answer as content event
  ├── Emits metadata (sources, confidence=high, escalation=true)
  └── Emits structured_response (full parsed XML)
        │
        ▼
Frontend SagePanel renders concise_answer
  + sources panel (FORM-ID-001 flagged as LOCAL POLICY)
  + escalation banner (high-risk query)
```

#### LLM Queue / GPU Coordination

Every call to `query_ollama` acquires a slot from `llm_queue` (defined in `services/llm_queue_service.py`, documented in the infrastructure section). The on-device MedGemma model can only process one request at a time. Without the queue, concurrent Sage queries would send multiple streaming requests simultaneously, causing the GPU to context-switch between them or OOM on the 8 GB Mini.

The queue uses `priority=1` for all Sage queries. If a higher-priority job (e.g., a risk-score background task) also uses the queue with a lower priority number, it will take precedence. The slot is released in a `finally` block so it is always returned even if the stream errors or the client disconnects.

**The ambient service (`sage_ambient_service`) does NOT use the LLM queue.** Its Ollama call is direct. This is intentional: ambient runs as a fire-and-forget background task with a short timeout, and integrating it with the queue would add complexity and risk blocking the queue. The trade-off is that simultaneous ambient and main-query calls may slow each other down. On current hardware this is acceptable because ambient calls are infrequent (gate-filtered) and use a short `num_predict` budget.

#### Security: Prompt Injection Hardening

Patient records contain free-text fields (diagnoses, lab comments, scribe notes, observation names) that could be crafted to inject instructions into the model. Two layers of defence are applied:

1. **Fencing**: Clinical context is wrapped in `<<<BEGIN_CLINICAL_CONTEXT>>>` / `<<<END_CLINICAL_CONTEXT>>>` delimiters. Any occurrence of the closing fence inside the data is neutralised by replacing it with `[END_CLINICAL_CONTEXT]`.
2. **System prompt rules**: Rule 7 of `SAGE_SYSTEM_PROMPT` explicitly tells the model that anything inside the context fences is DATA, not instructions, and that any apparent instruction found there must be disregarded.

These defences reduce risk but are not a complete guarantee against adversarial model inputs. If patient data will be sourced from untrusted external systems, additional sanitisation at the ingestion layer is recommended.

#### Tables Written by Sage

| Table | Written by | Purpose |
|---|---|---|
| `sage_conversations` | `sage_service` (via routes) | Per-user conversation history |
| `clinical_knowledge` | `clinical_knowledge.init_knowledge_base` | Guideline chunk metadata |
| `clinical_knowledge_vec` | same | Embedding vectors |
| `clinical_knowledge_fts` | same | Full-text search index |
| `clinical_knowledge_meta` | same | Corpus fingerprint |
| `sage_decision_cache` | (populated by a background service) | Pre-compiled patient context cache |
| `sage_room_memory` | `sage_ambient_service` | Rolling room memory per case room |
| `sage_observations` | `sage_ambient_service` | Per-message observation log |
| `sage_actions` | `sage_ambient_service` | Detected action intents awaiting confirmation |
| `sage_action_events` | `sage_ambient_service` | Audit log for action state transitions |
| `sage_handoffs` | `sage_handoff_service` | Confirmed action dispatch records |
| `sage_handoff_events` | `sage_handoff_service` | Audit log for handoff state transitions |

---

### 5.7 The drafted alert assessment: three tiers

> **New section (2026-08-15).** This subsystem did not exist when §5 was written. It was **built three times** in three days; what follows is the third and current shape. Files: `server/services/sage_assessment_service.py` (3,479 lines), `server/services/patient_specific_screen.py` (1,906 lines), migrations **29** (`sage_assessments`) and **30** (`sage_assessment_tiers`).

#### What it is for

The middle of Orb's wedge. Orb sells a loop: **Vigil detects deterioration → Sage reasons over it → Relay puts a drafted action in front of the right clinician.** Until this module the middle was hollow, the only Sage output on a critical alert was one sentence of 4B prose appended to the alert text, on the simulator lane only, with nothing for a clinician to accept or reject (`sage_assessment_service.py:3-10`; pinned by `tests/test_wedge_loop.py::test_the_alert_draft_is_not_an_approvable_assessment`).

#### Why there are three tiers: 45 seconds was useless

The first version wrote four prose sections and was **measured at 42–57 s end to end (median 46 s), first words 3.3–12.7 s** (`sage_assessment_service.py:15-18`; the same measurement is recorded in migration 30, `server/migrations.py:4534-4546`). At a deteriorating bedside that is past the point where a second opinion changes anything.

**The diagnosis was a design error, not a model one.** The most clinically useful part of that assessment, which NEWS2 parameters are scoring, by how much, and which way they are moving, is arithmetic Orb **already performs deterministically** in `vigil_service.compute_news2`. It had been handed to a language model to write out in sentences, which forced the token count, which forced the decode time. This appliance decodes ~10–15 tok/s, so **length is latency**: the prose the engine could have rendered instantly *was* the 45 seconds.

| Tier | Depth | Model? | What it is |
|---|---|---|---|
| **1, drivers & trend** | (always) | **No model at all** | The scoring NEWS2 parameters and their movement, rendered from the deterministic engine's own per-parameter breakdown. |
| **2, patient-specific facts** | `depth='brief'` | Deterministic pre-screen, then a model that only *judges* | What in **this** patient's chart changes what happens next. Often silent. |
| **3, full reasoned assessment** | `depth='full'` | Yes | The original four-section draft, **relocated** to the I-PASS handover and ward round. |

#### Tier 1: instant, no model, cannot fail

`_compute_tier1` / `_tier1` (`sage_assessment_service.py:720-738`). Written into the **same INSERT that creates the row**, before a single token is generated, so it is on the record from the moment the alert is on screen.

> It cannot be shed, cannot time out, cannot hallucinate and cannot be late. **A dead model leaves it standing**, that is the property `tests/test_assessment_tiers.py` proves with the provider stubbed to raise.

The `tier1` column is nullable only because a patient with no scoreable observation genuinely has no breakdown to show, in which case the block itself says so (`server/migrations.py:4548-4554`).

#### Tier 2: patient-specific facts only, and why it stopped being "next steps"

**This changed on 2026-08-15.** Tier 2 originally shipped as three prioritised next steps and was **measured at 22% delivery overall, 0% across a quiet five-minute control**, and, when it did arrive, two of its three steps restating the alert they were attached to ("Escalate immediately / Critical NEWS2"). The verdict was that it was close to decoration (`sage_assessment_service.py:45-56`).

> Generic advice is decoration on this surface **by construction**: Tier 1 already tells the clinician what is wrong, better than most hospital systems, and a second panel telling them to escalate a critical NEWS2 teaches them to skip the panel, at which point they also skip the line that mattered.

The tier now has exactly one job: **say what depends on this patient's own chart, and nothing else.** A rate-controlling drug making a reassuring heart rate misleading. A creatinine that changes the calculus on a standard fluid bolus. An allergy that rules out the obvious first-line agent. Anticoagulation before a procedure. A steroid blunting a fever. Every one needs the chart, none can be arithmetic over vitals, precisely the gap Tier 1 cannot fill and a tired clinician at 3 a.m. might not cross-reference.

**Two steps, and the first has no model in it** (`sage_assessment_service.py:68-79`):

- **Step A, `services/patient_specific_screen.py`.** Deterministic rules over data Orb already holds: the allergy list the bedside interlock screens against, the shared drug→class vocabulary, the formulary, the structured labs. Every rule has the same shape, **a chart fact AND something this patient is doing right now** (`patient_specific_screen.py:549-556`), because the conjunction is what makes a flag patient-specific rather than a restatement of the drug chart. Every candidate carries the evidence that produced it. At most `MAX_CANDIDATES = 4` (`:122`). Entry point `screen(db, patient_id, tier1, *, facts=None)` (`:223`).
- **Step B, the model judges, and only judges.** It is handed the short candidate list and asked which of them actually change what happens next. **It is never shown the chart**, so it cannot name a drug, dose or lab the screen did not supply, and `patient_specific_screen.verify_line` checks every one it does name back against that list, dropping and counting anything else.

**If Step A finds nothing, no model is called at all.** The draft resolves instantly, at zero GPU cost, to *"no patient-specific factors identified"*, a first-class, honest, common answer, never filler (`sage_assessment_service.py:81-86`). That is also the throughput fix the 22%/0% number was asking for: most alerts never reach the one background model slot, so the ones that do are not queued behind them.

**Three rules enforced in code, not only in the prompt** (`sage_assessment_service.py:88-97`, enforcement at `:1800-1845`):

1. **Silence is valid and expected.** A tier that always finds something to say is padding, which is what made the last one decoration.
2. **Never restate the alert.** A sentence true of any patient with those numbers is deleted, by the positive rule that every flag must name its source fact (`names_a_fact`) *and* by `_GENERIC_PHRASES`, a table of the exact strings observed on screen.
3. **Every flag names its source fact.** Not *"consider renal function"* but *"on ramipril; creatinine 180 six days ago"*. A checkable claim beats a confident one, the same discipline as the citation work.

> The code is explicit that **the prompt instruction is not the enforcement, `verify_line` is** (`:1806-1812`, `:1838-1842`). Prompt text is a first line, not a control.

Other Tier-2 mechanics: a hard output-token cap (`_max_advice_tokens`, `:596`), line-delimited output rather than JSON (braces and keys are pure decode time), and each **complete** flag emitted to the surface as it lands rather than at the end. **A flag that has landed is kept even if the generation then times out**, it is whole, screened and already read; the list is labelled as cut short rather than retracted, via the `advice_note` column (`server/migrations.py:4568-4574`).

#### Tier 3: the four-section assessment, relocated not deleted

`generate_full_assessment` is its entry point. The four-section draft *is good work whose 45 seconds are invisible on the I-PASS handover and the ward round, where a clinician is reviewing rather than reacting* (`sage_assessment_service.py:41-44`).

It is read and requested through I-PASS (`server/routes/ipass_routes.py:9-12`, `:99-148`), on a strict split:

- `GET /api/ipass/{patient_id}/reasoned-assessment`, **reading never generates.** Returns what is there, or `null`. `null` is returned rather than a spinner, because a spinner would promise an assessment that may be 40 seconds away or may never come (`ipass_routes.py:106-121`).
- `POST /api/ipass/{patient_id}/reasoned-assessment/prepare`, **asking** starts one and returns immediately.

Both are guarded by `require_patient_access("vitals.read", audit_action=…)` (`:103`, `:148`), ward-scoped and audited, because the assessment reasons over the entire chart.

Reuse rules for the handover sheet (`services/ipass_service.py:344-419`): only `depth='full'` rows qualify, *"a brief draft under a heading that says 'assessment' would put three bullet points where a reader expects four sections"*, age is filtered in Python rather than SQL because `created_at` is a Python `isoformat()` string, and a stale assessment is refused rather than shown, because *"a 30-hour-old assessment is lying in its own header"*.

#### States, and why Tier 1 and Tier 2 are separate ones

`STATE_GENERATING` / `STATE_READY` / `STATE_UNAVAILABLE` / `STATE_APPROVED` / `STATE_DECLINED` (`sage_assessment_service.py:227-231`); only `ready` may transition to `approved`/`declined` (`:248`).

The payload carries **`tier1` always, independent of `state`**, plus a separate `advice_state` (generating / ready / unavailable) (`:98-102`). A surface can therefore render the drivers immediately while the advice is still arriving, and can render the drivers **alone, honestly labelled**, when the advice fails. *"No advice yet" and "no assessment" are different sentences and the payload will not let them be confused.*

`_derive_state` (`:1160`, design note at `:1098`) makes an abandoned generation resolve rather than wedge a patient shut, it holds even if the process dies mid-generation.

#### What it is, and what it is not

- **It is decision support. The assessment is a DRAFT.** Nothing it suggests executes without an explicit clinician click, and it never claims to have changed the chart (`sage_assessment_service.py:106-112`).
- Every text field it produces goes through the **one** forbidden-phrase sanitizer, `routes.sage_routes._strip_forbidden`, the same one the buffered, streamed and voice lanes use. **A phrase any lane strips, every lane strips.** (This is the guardrail-lane-parity rule; see `docs/` for the history of what happens when one lane is fixed and the others are not.)
- NEWS2 values are **pinned, never re-rendered by the model** (`_pin_news2`, `:2080`), in line with the house rule that deterministic values are pinned and never recalculated by a model.

#### The alert never waits for this

> *"Vitals-in to clinician-visible alert is 51 ms and that number IS the product."* (`sage_assessment_service.py:114-121`)

Generation is scheduled from `bot_service.publish_vigil_critical`, called **after** the Relay message row is written, as a task nobody awaits. The alert path pays one `loop.create_task`. If generation is slow, shed, evicted or fails, the row goes to `unavailable` **with a reason** and the surface says so honestly; it never leaves a row that looks pending when nothing is coming.

#### Surfaces

`src/pages/panels/VigilPanel.tsx`, with dedicated tests `VigilPanel.tier1.test.tsx` and `VigilPanel.assessment.test.tsx`.
