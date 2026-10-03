# Sage: Ambient, Omnipresent, Context-Aware Intelligence

This document describes the architecture that turns Sage from a per-tab chat box
into an ambient intelligence that lives on every page, knows what the clinician
is looking at, begins streaming a useful answer as it forms, and powers a broader
AI layer across Orb, all through **one spine**: a local provider seam, a live
context bus, an incremental streaming sanitizer, and fail-closed governance.
It is written to be extended by someone who has never seen the code.

The four faces are one architecture. Read the spine first (§1–§4); the surfaces
(§5) are all just callers of it.

---

## 0. The one non-negotiable: PHI never leaves the building

Orb's thesis is that patient data stays on-prem (`docs/ORB_TECHNICAL_MANUAL.md`,
the loopback-only `data_residency` control, `HOST` defaulting to `127.0.0.1`).
Every decision below is downstream of that. Inference is **local-only**: local
MedGemma is the engine for every turn, PHI or not, so there is no external
inference path to reason about. If you remember one rule: **nothing that touches
the request text runs off-box, because the request text carries the PHI.**

---

## 1. The provider seam

**Files:** `server/services/llm_providers.py`, `server/services/sage_service.py`
(`query_ollama`), `server/services/scribe_providers.py`.

### The interface

`ChatProvider.astream_chat(messages, options)`, messages in, text chunks out.
One implementation:

- **`OllamaChatProvider`** (`name="ollama"`), the local MedGemma lane.
  Byte-for-byte the same wire behavior as the old inline httpx code in
  `query_ollama`. It is the offline/PHI engine, and the only engine.

One implementation still earns an ABC: it lets the streaming sanitizer (§3), the
audit record (§5), and the route layer be written once against an interface
rather than against Ollama's wire format.

### The call path

`sage_service.query_ollama(...)` kept its historical name (routes and tests
import it) but now goes through the provider seam rather than inline httpx:

```
every turn (PHI or not)  ──►  local MedGemma lane  (llm_queue P1 + GPU gate)
```

Every turn writes a per-inference audit record (§5). `route_info` carries the
provider and model that actually ran, so the route layer can surface provenance
to the UI (`metadata.provider`).

### Scribe's parallel seam

`scribe_providers.py` has its own `LLMProvider` ABC + registry, Scribe streamed
true tokens before Sage did. Each stage selects its provider by name
(`ORB_SCRIBE_DRAFT_PROVIDER=ollama`) and runs through the same local
`llm_queue`. Scribe payloads are clinical narrative, the transcript *is* the
note, so those purposes are **always PHI**, which is exactly why they run on the
same on-prem engine as everything else.

### Config

```
ORB_CLOUD_SPEECH_ALLOWED   browser cloud ASR permitted      (default: false)
```

There is no provider switch: every request runs on local MedGemma. The one
posture flag here governs the **browser's** speech recognition (§5), not where
the model runs.

---

## 2. The live context bus

**Files:** `src/contexts/ActiveViewContext.tsx`, `src/contexts/ActivePatientContext.tsx`,
`src/lib/orbBus.ts`, `src/pages/ClinicianShell.tsx`, panels.

"Sage on every page" is a **client-wiring** problem, not a model swap, the
global `<SageFloatingAssistant>` already mounts once in `ClinicianShell`. What
was missing was telling the backend what's on screen.

### ActiveViewContext

A sibling to `ActivePatientContext`, carrying `{ activeTab, selection }` where
`selection` is `{ kind, id, label, patientId }` (an open chart, order, channel,
lab, bed). `ClinicianShell`'s `<ViewTabSync>` publishes the active tab and
clears stale selection on tab change; panels publish their selection the way
`VigilPanel`'s `PatientDetailView` already publishes the active patient.

`buildWireViewContext()` renders this into the `view_context` object sent in the
`querySage`/`querySageVoice` body. The backend (`sage_routes._describe_view`)
whitelists + length-clamps the fields and injects them as a **DATA line inside
the fenced clinical-context block**, never as instructions. The on-screen
patient becomes the pronoun-resolution fallback: "what are her latest labs?"
scopes to the open chart without the clinician naming anyone.

### The navigation bus

`orb:goto-tab` / `orb:open-patient` window CustomEvents (`src/lib/orbBus.ts`).
`ClinicianShell` listens for `goto-tab` (role-checked); `VigilPanel` listens for
`open-patient` and also warms Sage's context (§4). The command palette, deep
links, and the ward brief all drive navigation through this one bus. (The bus
the original brief assumed did not exist in the repo; it was built here, both
sides.)

### Unified surface + mic ownership

`SageRuntimeContext` is the shared runtime for **every** Sage surface:

- **Governance posture**, fetched once from `GET /api/sage/ai-posture`. Until
  it loads, cloud speech is treated as *not allowed* (fail-closed).
- **Mic ownership**, one registry (`claimMic`/`releaseMic`) so the floating
  orb's continuous recognition, `SagePanel`'s voice recording, and Scribe's
  dictation stop fighting over the single input device. A surface claims the mic
  before opening it; the others observe and pause.

The floating orb's old blanket self-suspend on the sage/scribe tabs is gone, it
is now genuinely omnipresent; what pauses it is mic contention, not the tab.

---

## 3. Streaming with sanitization

**File:** `server/routes/sage_routes.py`
(`SentenceStreamSanitizer`, `XmlAnswerStreamer`, `stream_response`).

### The problem

The old pipeline buffered the **entire** generation before emitting anything, 
a deliberate safety control, not a perf accident. The model can assert "I've
added X, order placed" in the raw `<concise_answer>` *before* the end-of-stream
action-state override rewrites it, so the route buffered every scope, ran the
override + an unconditional forbidden-phrase strip, and only then emitted one SSE
event. Consequence: **the browser's first byte ≈ total generation time**, not
`ttft_ms`. You cannot get streaming by "trusting a better model"; you must build
an incremental sanitizer that streams the answer as it forms while keeping the
mutation-turn override buffered.

### The two primitives

- **`SentenceStreamSanitizer`**, releases text at **sentence granularity**,
  running the forbidden-phrase strip over each completed sentence before
  release. Every forbidden pattern terminates at/before a period, so a phrase
  can never span two released segments; the streamed text is exactly as
  sanitized as the buffered text was. It holds back the trailing incomplete
  sentence until a boundary (or `flush()`) arrives.
- **`XmlAnswerStreamer`**, gates streaming on the response contract's **tag
  order**. `<action_intent>` arrives before `<concise_answer>`, so we know
  whether this is a mutation turn *before any answer text exists*. Mutation turns
  (deterministic gate **or** model-declared `<action_intent>`) emit **nothing**
  incrementally, they stay fully buffered behind the action-state override,
  exactly as before. Only `intent == "None"` turns stream, and even those pass
  every sentence through the sanitizer. It also holds back any tail from the last
  `<` so markup never leaks. If the model never produces well-formed tags,
  nothing streams and the buffered path handles it, fail-closed.

Broad (census) scope uses the prose sanitizer directly. A mutation turn is never
allowed to ride the broad lane (it has no `<action_intent>` contract, so the
override could not run), such turns are forced onto the XML contract.

The voice route (`/sage/voice`) gets the same sentence sanitizer: a hallucinated
"order placed" must not be **spoken** either.

### Proof

`server/tests/test_sage_streaming.py`:
- a non-mutation turn produces **multiple** content events (streaming is real)
  and no client-visible byte contains a forbidden phrase, even split across
  7-byte chunks;
- a mutation turn (deterministic **and** model-declared) emits exactly **one**
  content event, the override's safe message, and never the hallucinated
  confirmation.

---

## 4. The latency budget: where each second goes

Sub-2s time-to-first-token is unlocked in layers, cheapest first:

| Lever | Where the time goes | Applies to |
|---|---|---|
| **Incremental sanitizer** (§3) | First content leaves at first-sentence time, not total-generation time. Backend-independent. | All turns |
| **Speculative context prefetch** | Retrieval (facts + guideline RAG) leaves the TTFT clock. `POST /api/sage/prefetch` on "patient opened" re-keys the same warm path a critical Vigil alert uses (`nervous_system_triggers.warm_patient_context` → `sage_decision_cache`), shorter TTL. `sage_service` reads the cache on patient-scoped turns. | Patient-scoped turns |
| Prompt/context caching keyed on patient+view, warm keep-alive, trimmed prompt | Retrieval reuse + fewer tokens the model must read before answering. | All turns |

**The honest promise:** every turn runs on the one local GPU, serialized through
`llm_queue.max_concurrent=1` behind the process-wide GPU lock. So TTFT under
concurrency is bounded by that single shared device: the clinician at the head of
the queue can see sub-2s, the Nth waits behind N−1 turns. The levers above shrink
each turn's share of the clock; none of them add parallelism, because the
hardware does not. Buying TTFT under concurrency means more local GPU, not a
different backend.

### Measurement

`scripts/loadtest_sage.py` records **TTFT** (time to the first streamed content
event, by streaming the SSE response and stamping the clock on the first content
delta), not just total latency, across concurrency, reporting p50/p95 TTFT. With
no Ollama reachable it reports honestly that there are no TTFT samples rather
than inventing a number.

---

## 5. The PHI governance model

**File:** `server/services/ai_governance.py`. Two controls.

### (a) Local-only PHI classifier

`classify_query_sensitivity(query, patient_id, view_context)` runs regexes and a
local DB name/ward/bed match, entirely in-process, the query text carries
patient names and beds, so shipping it anywhere to classify it would itself be
the leak. It reuses the existing local scope-detection signals, and its verdict
is what the per-inference audit record (b) stamps on every turn. What it treats
as PHI (fail-closed):

- any patient-scoped query (attached `patient_id`, on-screen patient, a matched
  name/ward/bed, or a pronoun that may resolve to a patient);
- the hospital **census / patient list / discharge board / per-patient
  forecast**, bed+ward+dx+status per patient identifies people even without
  names, and Orb's broad-scope context builder returns exactly those rows, so
  **every broad-scope query is PHI**.

Only general medical-knowledge / guideline questions with no patient reference
classify as PHI-free. That label is provenance on the audit trail, not a routing
decision, PHI-free and PHI turns run on the same local engine.

### (b) Per-inference audit records

`record_inference(...)` appends one record per request to the **tamper-evident
audit hash chain** (`audit_service.log_audit_event`): which provider/model
actually ran, the PHI classification, timing, outcome. That is what makes "which
model saw this patient's data, and when" answerable after the fact. (The old
`model_governance` registry was descriptive-only, hardcoded `medgemma-27b`, and
neither logged nor timed the call.)

### The mic egress leak

`SageFloatingAssistant`'s `webkitSpeechRecognition` streams mic audio to a cloud
ASR, egress of spoken PHI, and the one path in Sage that can still leave the
building. It is gated by the governance posture: recognition only runs when
`cloud_speech_allowed` (from `/sage/ai-posture`) is true; clicking the orb
otherwise shows a card explaining the privacy posture. `SagePanel` and
`ScribePanel` gate their parallel `webkitSpeechRecognition` the same way, their
local-Whisper path (the source of truth) is unaffected.

Guardrails apply to **every** surface: the safety/injection gate, the mutation
gate, the incremental sanitizer + action-state override, cited-source validation,
and provenance all run on every turn.

---

## 6. Other omniscient surfaces (same spine)

Engine-selection discipline: **deterministic floor first → local MedGemma only
where bounded reasoning adds something the rules cannot.** For each surface:
what it watches, what it emits, what it hooks, its posture.

### Situational-awareness ward narrator: NEW

**File:** `server/services/sage_narrator_service.py`.
- **Watches:** `vigil.alert.*`, `scribe.note.signed`, `bridge.emergency.flag` on
  `event_bus` (the last had no subscriber before), plus a slow idle refresh.
- **Emits:** `sage:ward_brief` socket event; served by `GET /api/sage/ward-brief`;
  rendered by `src/components/sage/WardBrief.tsx` on the Vigil page.
- **Engine:** deterministic snapshot (census, risk mix, watchlist, recent
  events) is the always-on trusted floor; a debounced local-MedGemma narrative
  layers on top at `PRIORITY_BACKGROUND` (never contends with a clinician). If
  Ollama is down it degrades to the snapshot, never to silence.
- **Posture:** local-only, the snapshot is per-patient PHI. State is in-memory,
  regenerated on demand (deliberately not a new PHI-at-rest store, see §7).

### Per-patient background precompute: NEW wiring

**Files:** `nervous_system_triggers.warm_patient_context`, `POST /api/sage/prefetch`,
`VigilPanel` (prefetch on patient open).
- **Watches:** critical Vigil alerts (10-min TTL) **and** the new "patient opened"
  UI event (5-min TTL, a navigation is a weaker signal).
- **Emits:** warmed `sage_decision_cache` rows; read by `sage_service` on
  patient-scoped turns so interactive Sage is warm.
- **Posture:** local. Short TTLs because the cache is a PHI-derived store (§7).

### Proactive risk co-pilot: EXISTING, kept as the floor

**File:** `server/services/sage_proactive_service.py`.
- **Watches:** vitals/risk/meds every 15s. **Emits:** `sage:insight`;
  `SageInsightsFeed` on Vigil. **Engine:** LLM-free deterministic rules, this
  determinism is a trust feature, kept as the floor. Any draft order routes
  through the existing action-state `request_drafted` override.

### Cross-shift memory: EXISTING

**File:** `server/services/sage_handoff_service.py`. Local synthesis briefing the
next clinician at confirmed-action handoff. Local engine; PHI stays in.

### Two free correctness fixes (done)

- `sage_ambient_service.py` used `asyncio.create_task` (~L690) but never imported
  `asyncio`, so the voice-prewarm publish silently `NameError`ed and never fired.
  `import asyncio` added.
- The ambient Ollama classify call **bypassed** `llm_queue`, contending unqueued
  with interactive Sage for the single slot. It now acquires the queue at
  `PRIORITY_BACKGROUND` and skips (rather than blocks) when a clinician is using
  the model.

### Not wired (honest note)

`coding_service.py` and `ipass_service.py` have **dormant LLM stubs**, no live
Ollama/llm_queue calls today. Extending AI there means wiring the LLM path, not
repointing a provider; left out of scope to avoid vaporware.

---

## 7. The encryption-at-rest dependency

Every new PHI-derived store this work adds, warmed `sage_decision_cache`
answers, room memory, SBAR handoffs, **compounds the unmet encryption-at-rest
gap**: `orb.db` and uploads hold PHI in cleartext unless `ORB_DB_ENCRYPTION_KEY`
/ `ORB_UPLOAD_ENCRYPTION_KEY` are set (`config.py`, `crypto_service.py`). This is
a known deferred, cross-cutting control (SQLCipher/sqlite3mc + key from an OS
keychain). Two mitigations were applied here: (1) the ward narrator keeps its
briefing **in memory**, not in a new table; (2) precompute caches use **short
TTLs**. Until encryption-at-rest lands, treat every cache row as cleartext PHI
and keep TTLs tight.

---

## 8. How to verify

```bash
# Backend: provider seam, governance, streaming
cd server && . .venv-dev/bin/activate
pytest tests/test_ai_governance.py tests/test_llm_providers.py \
       tests/test_sage_streaming.py -q

# Frontend: view-context wire builder + existing SSE tests
npm test

# Visual gate (both themes, zero new app JS errors, orb on every page)
npm run build
#   start backend on :8000 + `vite preview` on :5173, then:
ORB_E2E_BASE_URL=http://127.0.0.1:5173 node tests/visual/driver.cjs
node tests/visual/ci-gate.cjs

# Latency harness (needs Ollama running for real numbers)
python scripts/loadtest_sage.py --target sage --levels 1,4,8
```

Screenshots of the key states (ward brief + insights, the omnipresent orb on a
previously Sage-less page, the voice governance gate, both themes) are in
`docs/assets/sage/`.
```
