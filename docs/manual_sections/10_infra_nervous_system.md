## 10. Infrastructure: Event Bus, Triggers, LLM Queue, GPU Coordinator, Audit & Realtime

This section covers the connective tissue that holds all of Orb's modules together. None of the clinical features (Vigil, Sage, Scribe, Relay, etc.) call each other directly, they speak through a shared event bus, queue LLM work through a central gatekeeper, and push live updates to browser clients over WebSockets. Getting this layer right is what prevents the single on-device GPU from being overwhelmed, what makes HIPAA-grade auditing possible, and what lets you add a new module without touching any existing one.

---

### 10.1 `server/services/event_bus.py`: The Local Publish-Subscribe Broker

**What this file is**

This is Orb's internal nervous system message broker. Every significant thing that happens in the system, a Vigil alert fires, a Scribe note is signed, a voice-intent is detected, gets published here as an event. Other parts of the system that care about those events have registered "subscriptions" and get called automatically. The key word is *local*: everything stays on-premises, in the same Python process, with no external message queue needed.

**Libraries used**

- `asyncio`, Python's built-in concurrency toolkit. Events are dispatched asynchronously so that publishing a burst of alerts doesn't block the web server from answering requests.
- `json`, Serializes the event payload to a string for storage in SQLite.
- `uuid`, Generates globally unique IDs for each event and correlation chain.
- `database.db_session`, Orb's database context manager for SQLite writes.

**The `_MAX_CONCURRENT_DISPATCH = 64` constant**

This is a ceiling on how many subscriber callbacks can run at exactly the same time. Without it, a flood of alerts (say, every patient's Vigil score crossing threshold at once) would spawn hundreds of simultaneous background tasks, all trying to acquire the single-slot LLM queue. The semaphore forces them to line up. If you need to raise this limit, be aware that every slot in this semaphore can become an LLM queue entry, raising the number directly strains the GPU.

> ⚠️ **If you change this:** Raising `_MAX_CONCURRENT_DISPATCH` substantially without also controlling downstream concurrency (especially the LLM queue) can starve interactive Sage queries during alert storms.

---

#### Class: `StateEvent`

**Purpose:** A single event message, the fundamental unit of communication between modules.

**Constructor arguments:**
- `topic` (str), A dot-separated name like `"vigil.alert.critical"` or `"scribe.note.signed"`. This is how subscribers know which events to listen to.
- `source_node` (str), Which module created this event (e.g., `"vigil"`, `"scribe"`). Used for tracing.
- `event_type` (str), A finer-grained label within the topic (e.g., `"mews_threshold_exceeded"`).
- `patient_id` (Optional[str]), Which patient this event is about. Some system events (startup, config) have no patient.
- `payload` (Dict), Arbitrary data the publisher wants to share with subscribers. For a Vigil alert, this includes the MEWS score, the clinical drivers, the risk tier.
- `correlation_id` (Optional[str]), A shared trace ID that threads through every downstream action triggered by this event. If you see failures in the logs, search for the `correlation_id` to find every related log line across modules.

**Auto-assigned fields:**
- `event_id`, A new UUID for this specific event.
- `timestamp`, UTC ISO-8601 when the event was created.
- `correlation_id`, If the caller does not supply one, the bus generates one automatically (`corr-<12-hex-chars>`).

**`to_dict()`**, Returns all fields as a plain dictionary. Used when serializing to SQLite and when passing events to the WebSocket layer.

---

#### Class: `LocalEventBus`

**Purpose:** The singleton broker. Modules call `event_bus.subscribe(topic, callback)` at startup to register interest. When something publishes an event, the bus finds all matching subscribers and calls each one.

**Key internal state:**
- `_subscribers`, A dictionary mapping topic patterns to lists of async callback functions.
- `_lock`, An `asyncio.Lock` that protects reads of `_subscribers` during publish. This prevents a race condition where a module subscribes at the same moment an event is being dispatched.
- `_dispatch_sem`, The bounded semaphore (max 64 concurrent dispatches). It is created lazily on first use so it is always created in the correct event loop.

**`subscribe(topic, callback)`**

Registers an async function to be called whenever an event matches the topic pattern. A duplicate registration is silently ignored (the `if callback not in` guard). Topics support two matching forms:
- Exact match: `"scribe.note.signed"` matches only that precise topic.
- Wildcard prefix: `"vigil.*"` matches any topic starting with `"vigil."`.
- Universal: `"*"` matches every event.

This is called at startup via `register_triggers()` in `nervous_system_triggers.py`. You do not call it in request handlers.

**`publish(event)`**

This is the method you call to fire an event. It does two things in sequence:

1. **Persist to the write-ahead log.** The event is written to the `event_log` SQLite table before any subscriber is notified. This means there is always a durable record that the event happened, even if the process crashes before the subscriber runs. If the SQLite write fails, this is logged as an `ERROR` (loud, with stack trace) but the bus still continues to dispatch in-memory, the WAL record is just missing.

   > ⚠️ **Durability caveat (important for interns):** The `event_log` row is a diagnostic audit trail, *not* a delivery guarantee. If the process crashes after writing the row but before the subscriber runs, the event is lost, there is no replay on restart. The code comment mentions an "outbox/poller" as the fix for this, but it has not been built yet. Do not rely on the event log to guarantee that every signed note has been processed.

2. **Dispatch to subscribers.** The bus collects all callbacks whose topic pattern matches the event's topic, then fires each one as a separate `asyncio.create_task()`. This means the `publish()` call returns quickly, it does not wait for subscribers to finish. Each subscriber runs in its own task, wrapped in `_safe_dispatch`.

**`_match_topic(pattern, topic)`**

The topic-matching logic. Three rules: `"*"` matches everything; exact string equals match exactly; `"foo.*"` matches anything where the topic begins with `"foo."`. Nothing more complex than this, there are no multi-segment wildcards.

**`_safe_dispatch(callback, event)`**

This is the wrapper around every subscriber call. Before calling the subscriber, it acquires one slot from the `dispatch_sem` semaphore (the limit-64 backpressure). If all 64 slots are busy, this coroutine waits until one is free.

When the subscriber raises an exception, `_safe_dispatch` catches it, logs it as `ERROR` with the correlation ID, and moves on. The subscriber failure does *not* propagate to the publisher or crash the bus. However, the downstream workflow triggered by that subscriber is silently dropped. This means a bug in a subscriber can cause clinical data to not flow without any visible error to the user.

> ⚠️ **If you change this:** If you add a new subscriber and it throws an exception, the failure appears only in logs (search for `[EventBus] Error in subscriber`). The event_log row will exist, but the subscriber's work (e.g., caching context, writing to the EHR) will not have happened. Always add try/except inside new subscribers and emit your own error events if needed.

**`event_bus` (module-level singleton)**

At the bottom of the file: `event_bus = LocalEventBus()`. This is the single global instance. Every other file imports this object. Do not create a second `LocalEventBus()`.

---

#### Concrete event flow (end to end)

To make publish-subscribe concrete, here is the full chain for a Vigil critical alert:

1. `vigil_service.py` detects a patient's MEWS score has crossed the critical threshold.
2. It creates `StateEvent(topic="vigil.alert.critical", patient_id="P123", payload={"risk_score": 7.4, "drivers": ["tachycardia", "hypotension"]}, ...)`.
3. It calls `await event_bus.publish(event)`.
4. The bus writes the event to `event_log` in SQLite.
5. The bus finds one matching subscriber: `on_critical_vigil_alert` (registered for `"vigil.alert.critical"`).
6. The bus fires `asyncio.create_task(_safe_dispatch(on_critical_vigil_alert, event))`.
7. `publish()` returns. Vigil's code continues immediately.
8. In the background, `on_critical_vigil_alert` runs (described below in section 10.2).

This decoupling is the whole point: Vigil does not import Sage, does not call Sage's functions, has no compile-time dependency on Sage. If Sage is refactored or removed, Vigil's code is unchanged.

---

### 10.2 `server/services/nervous_system_triggers.py`: Cross-Module Reactive Triggers

**What this file is**

This file defines the "reactions", the async functions that are registered as subscribers on the event bus. Think of these as the hospital's automated reflex arcs: when event X happens, automatically do Y. There are three triggers defined here, covering three of Orb's most important cross-cutting workflows.

**Libraries used**

- `asyncio`, `json`, `logging`, `re`, standard Python.
- `httpx`, Async HTTP client, used to call the local Ollama LLM API.
- `database.db_session`, SQLite access.
- `services.event_bus`, The event bus singleton and `StateEvent` class.
- `services.sage_service`, Two internal helpers for assembling patient context and guideline text.
- `services.voice_agent_service`, Gets the MLX voice model agent object.
- `services.llm_queue_service`, The LLM priority queue (so this trigger's LLM call lines up behind interactive Sage queries).

---

#### Function: `on_critical_vigil_alert(event: StateEvent)`

**Triggered by:** `"vigil.alert.critical"` events.

**Purpose:** When Vigil fires a critical alert, this trigger proactively assembles the clinical context that Sage would need to answer questions about that patient. This is a *pre-warming* optimization: if a nurse opens Sage a few seconds after the alert, the context is already ready and the response is faster.

**What it does step by step:**

1. Extracts `patient_id`, `mews_score`, and clinical `drivers` from the event payload.
2. Calls `_assemble_patient_context(db, patient_id)` inside `asyncio.to_thread()`. The `to_thread` call is important: these are blocking SQLite reads, and running them directly in an async function would freeze the event loop. By wrapping in `to_thread`, they run in a separate thread while the event loop continues serving requests.
3. Calls `_get_guideline_context(query_terms)` with the alert's clinical drivers as a search query (or a default sepsis/hemodynamic query if no drivers were provided). This retrieves relevant guideline text from the RAG vector store.
4. Bundles everything, patient facts, guideline text, source document IDs, confidence score, into a `compiled_data` dictionary.
5. Writes that bundle to the `sage_decision_cache` SQLite table with an expiry 10 minutes in the future (`INSERT OR REPLACE`). Sage checks this cache before doing its own context assembly.
6. If anything fails, logs the error and returns cleanly (no crash, no partial write).

**What this does NOT do:** It does not call the LLM, does not generate a summary, does not notify the nurse. It only pre-positions data so that if Sage is invoked, it starts faster.

---

#### Function: `on_scribe_note_signed(event: StateEvent)`

**Triggered by:** `"scribe.note.signed"` events.

**Purpose:** When a clinician signs a SOAP note in Scribe, this trigger uses the LLM to parse the note and extract structured clinical data (vitals, medication changes, diagnoses) back into the EHR tables.

**What it does step by step:**

1. Reads the signed note text and author ID from `scribe_notes` by `note_id`.
2. If the note is empty, returns early.
3. Builds a prompt asking the LLM (via Ollama's `/api/chat`) to extract exactly three things from the SOAP note: vitals from the Objective section, medication changes from the Plan section, and diagnoses from the Assessment section. Output must be JSON only, with `temperature=0.0` to minimize hallucination.
4. Calls `await llm_queue.acquire(priority=3)`, this is `PRIORITY_BACKGROUND`, the lowest priority. The EHR extraction is a background job; it must not compete with a clinician actively chatting with Sage (`PRIORITY_SAGE = 1`).
5. Sends the HTTP request to Ollama. Strips markdown fencing if the model wrapped the JSON in backticks.
6. Parses the JSON response.

**FAIL CLOSED, critical behavior:**

If *anything* goes wrong in steps 3–6 (network error, JSON parse failure, Ollama OOM, timeout), the code does this:

```python
# FAIL CLOSED. A parse/extraction failure must NEVER fabricate clinical data.
logger.error("[EHRExtractor] Extraction failed ... aborting auto-EHR write (fail-closed): %s", err)
updates = {}
```

And then:

```python
if not updates:
    return
```

The function exits without writing anything to the database. The signed note remains as the source of truth. The `release()` is called in a `finally` block so the LLM queue slot is always freed.

> ⚠️ **Why this matters:** The code comment explains that a previous version of this function had an `except` branch that *injected a hardcoded Metoprolol 2.5mg IV order* whenever an error occurred and the word "metoprolol" appeared in the note. This is classified as a critical SaMD (Software as a Medical Device) failure mode, a medication order being written to the EHR not because the LLM produced it, but because an exception was raised. The current code explicitly avoids this by writing nothing on error.

> ⚠️ **If you change this:** Never add fallback data to the `except` block. If the LLM call fails, the correct answer is always: write nothing, log loudly, let a human review the signed note.

**If the LLM succeeds:**

7. Iterates over `updates["medications"]`. For each medication entry, validates that `drug_name` and `action` are present (skips malformed entries rather than crashing). Executes `UPDATE medications SET status = 'discontinued'` for "stop" actions, or `INSERT INTO medications` for "start" actions.
8. Inserts a vitals row if the extracted vitals include at least one of heart_rate, systolic_bp, or spo2.
9. Inserts a `clinical_documents` row summarizing the auto-extraction, so there is a human-readable record of what the LLM wrote and why.
10. Emits a `patient:timeline_updated` Socket.IO event to all clients in the patient's room so the UI refreshes without a page reload.

> ⚠️ **Deferred limitation (call out to reviewers):** These auto-extracted writes currently go directly into the `medications` and `vitals` tables with no `source` column distinguishing them from clinician-entered data. The code comments flag this: the recommended fix is to add a `source`/`provenance` column and stage LLM-extracted records as "PROPOSED" requiring explicit clinician confirmation before becoming "active". This is a schema change (tracked separately) and has not been implemented yet.

---

#### Function: `on_relay_voice_prewarm(event: StateEvent)`

**Triggered by:** `"relay.intent.voice_prewarm"` events.

**Purpose:** When something in the system (typically Relay, which monitors for speech-intent markers) signals that a clinician is about to use voice interaction, this trigger schedules the MLX voice model to be loaded into memory before the first microphone input arrives.

**What it does:**

1. Calls `get_voice_agent()` to get the singleton voice agent object.
2. Fires `asyncio.create_task(asyncio.to_thread(agent.load))`, this starts loading the model weights from disk into GPU memory in a background thread, non-blocking.
3. Logs success or failure.

The intent is to hide model-load latency (which can be several seconds for a large MLX model) behind the time between the system detecting voice intent and the clinician actually speaking.

---

#### Function: `register_triggers()`

**Purpose:** Wires all three functions to the event bus. Called once at server startup.

```python
event_bus.subscribe("vigil.alert.critical", on_critical_vigil_alert)
event_bus.subscribe("scribe.note.signed", on_scribe_note_signed)
event_bus.subscribe("relay.intent.voice_prewarm", on_relay_voice_prewarm)
```

> ⚠️ **If you add a new trigger:** Add it here. If you change a topic name anywhere in the system (e.g., rename `"scribe.note.signed"` to `"scribe.note.submitted"`), you must update both the publisher (in `scribe_service.py` or wherever it is published) and the subscriber registration here. A mismatch causes the trigger to silently never fire, no error, no warning, just the downstream action not happening.

---

### 10.3 `server/services/llm_queue_service.py`: The LLM Priority Queue

**What this file is**

Every part of Orb that needs the local language model, Sage answering a clinician's question, Scribe drafting a note, Vigil generating an alert explanation, or the EHR extractor parsing a signed note, must go through this queue. Because Ollama runs a single model stream at a time on the local hardware, this queue is what prevents two LLM calls from trying to run simultaneously (which would cause errors, slow responses, or GPU OOM).

Think of it as a traffic light for the GPU: exactly one car (LLM call) moves at a time, and the car with the highest priority (lowest number) goes first.

**Libraries used**

- `asyncio`, `asyncio.PriorityQueue` for the waiting room, `asyncio.Event` for signaling when a waiter's turn has arrived, `asyncio.Lock` for mutual exclusion when modifying queue state.
- `services.gpu_coordinator`, Imports the canonical priority constants. The queue does not define its own numbers; it uses the same `PRIORITY_SAGE = 1`, `PRIORITY_SCRIBE = 2`, `PRIORITY_BACKGROUND = 3` that the GPU coordinator defines (see section 10.4). This ensures both serializers agree on priority meaning.

---

#### Class: `PriorityLLMQueue`

**Internal state:**
- `_lock`, An `asyncio.Lock` protecting modifications to queue state. Created lazily (first use) to avoid event-loop mismatch at import time.
- `_queue`, An `asyncio.PriorityQueue`. Items placed in it are tuples of `(priority, counter, event)`. The priority queue always pops the lowest-priority-number (highest urgency) item first.
- `counter`, A monotonically increasing integer. When two requests have the same priority number, the one that arrived first (lower counter) goes first. This makes priority-ties FIFO.
- `active_workers`, How many LLM calls are currently running. In practice this is 0 or 1 because `max_concurrent = 1`.
- `max_concurrent = 1`, Ollama with a single loaded model handles exactly one stream at a time optimally. Do not raise this without verifying your hardware can run concurrent inference.

---

#### Method: `acquire(priority, timeout=120.0)`

**What it does:**

A caller wanting to use the LLM calls `await llm_queue.acquire(priority=PRIORITY_SAGE)`. This method:

1. Creates an `asyncio.Event`, a one-time signal flag.
2. Acquires `self.lock`, increments `self.counter`, and puts `(priority, counter, event)` into the priority queue.
3. Calls `_maybe_wake()`, which checks if there is a free slot right now (active_workers < max_concurrent). If yes, it immediately sets the new waiter's event so it doesn't have to wait at all.
4. Releases the lock and waits on `event.wait()` with a deadline (`asyncio.wait_for`).
5. When the event is set (by a previous caller releasing the queue), `acquire()` returns. The caller now "holds" the LLM.

**Timeout and cancellation handling:**

If `event.wait()` exceeds `timeout` seconds (default 120 seconds), the method catches `asyncio.TimeoutError` or `asyncio.CancelledError`. It marks the event as cancelled (`event._is_cancelled = True`) so `_maybe_wake` can skip it later, decrements `active_workers` if the slot had already been granted, and re-raises the exception. The caller's `finally` block should still call `release()`, though in the timeout path the slot will already have been freed.

> ⚠️ **If you change this:** The timeout of 120 seconds is a hard ceiling. A Sage streaming call that takes longer than 2 minutes will be cancelled even mid-stream. If you're seeing frequent timeouts, the cause is usually a slow model or queue backup, not the timeout value itself.

---

#### Method: `release()`

**What it does:**

Called in a `finally` block by every caller after the LLM call completes (or errors). Decrements `active_workers` by 1 (never below 0) and immediately calls `_maybe_wake()` to let the next waiter in.

**Critical design note, synchronous release:**

This method is synchronous (not `async`). The comment in the code explains why this was a deliberate fix:

> Previously this scheduled `asyncio.create_task(self._safe_release())` and returned immediately; callers invoke release() from `finally` blocks (often inside streaming async generators), so during client disconnect / loop teardown the fire-and-forget task could be cancelled or never scheduled (RuntimeError: no running event loop). A single dropped release leaves `active_workers` stuck at 1 forever, wedging every future acquire() until process restart.

In other words: if `release()` were async and that task got dropped (e.g., because a client disconnected mid-stream), the queue would be permanently stuck. Making it synchronous means the decrement happens the instant `release()` is called, guaranteed.

> ⚠️ **If you change this:** Do not convert `release()` to async or wrap it in `create_task()`. The whole point of making it synchronous was to prevent the permanent-wedge bug. Every caller must also call `release()` inside a `finally` block (not just on success). Missing a `release()` in an error path will wedge the queue until restart.

---

#### Method: `_maybe_wake()`

**What it does:**

Checks if `active_workers < max_concurrent` (i.e., there is a free slot) and the queue is not empty. If both are true, pops the highest-priority item from the queue, skips any that are marked cancelled, increments `active_workers`, and sets the item's event to wake the waiter. Loops until either all slots are full or the queue is empty.

Must be called from the queue's owning event loop (never from a different thread). It is called from `acquire()` (when enqueuing), from `release()` (when a slot is freed), and from the timeout/cancel handler (to ensure the slot stays available).

---

#### `llm_queue` (module-level singleton)

`llm_queue = PriorityLLMQueue()` at the bottom. Every module imports this object. Usage pattern:

```python
await llm_queue.acquire(priority=PRIORITY_SAGE)
try:
    # call Ollama here
finally:
    llm_queue.release()
```

The `finally` is non-negotiable. If you forget it, the queue wedges.

---

### 10.4 `server/services/gpu_coordinator.py`: VRAM Management and Metal Queue Serialization

**What this file is**

Where the LLM queue (section 10.3) governs Ollama's HTTP API calls, this file governs *in-process* GPU inference, the PyTorch MPS (Vigil's CfC neural network) and MLX (the voice model) workloads that run directly on Apple Silicon's GPU. It provides two tools: a synchronous context manager that cleans up GPU memory after each inference, and an async lock that prevents two in-process GPU tasks from running simultaneously.

**Background: why Apple Silicon needs explicit coordination**

On Apple Silicon (M1/M2/M3/M4 chips), the CPU and GPU share the same unified memory pool (called "unified memory" or informally "VRAM" in these docs, the same physical RAM chips serve both). PyTorch (via the MPS backend) and MLX (Apple's own ML framework) both submit GPU work as Metal command buffers to the same hardware command queue. If two model inferences run at the same time, three bad things happen:
1. **Metal command queue contention**, the driver context-switches between competing workloads, degrading throughput for both.
2. **MPS allocator fragmentation**, PyTorch's MPS allocator does not release memory between allocations without an explicit cache flush, so the fragmentation count grows over time.
3. **MLX cache bloat**, MLX keeps internal command buffer pools alive until told to clear them, consuming 50–100 MB per inference cycle if not flushed.

`gpu_coordinator.py` solves all three.

**Libraries used**

- `gc`, Python's garbage collector, used to reclaim Python-side references to GPU tensors before flushing the hardware cache.
- `time`, For measuring GPU task duration (logged in milliseconds).
- `asyncio`, For the priority-aware async lock.
- `contextlib.contextmanager`, For the `vram_guard` context manager.
- `torch` (optional import at runtime), PyTorch, accessed only if installed; the import is guarded so the coordinator works even if PyTorch is not present.
- `mlx.core` (optional import at runtime), MLX, similarly guarded.

---

#### Priority Constants

```python
PRIORITY_SAGE = 1        # Real-time voice / interactive, always wins
PRIORITY_SCRIBE = 2      # User-initiated document gen
PRIORITY_BACKGROUND = 3  # Vigil CfC, Forecast LSTM (nobody is staring)
```

These are the **canonical definitions** for the entire backend. The LLM queue service (`llm_queue_service.py`) imports these rather than defining its own numbers. This is important: both the Ollama HTTP serializer and the in-process Metal serializer use the same priority scale, so `PRIORITY_SAGE` means the same thing everywhere.

> ⚠️ **If you change these values:** Any code that hardcodes the numbers `1`, `2`, or `3` instead of using these constants will silently use the wrong priority. Always import and use the constants. If you add a new priority level (e.g., `PRIORITY_PHARMACY = 1.5`), you must also update every `acquire()` call that should use it, and verify that the Python `PriorityQueue` sorts your values correctly (it compares tuples lexicographically, so floats work but can be surprising).

> ⚠️ **Known gap (deferred):** The code comment in the file notes that `llm_queue` (Ollama) and `umsq` (in-process Metal/MLX) still serialize *independently*. They share the same physical GPU/unified memory but do not coordinate with each other. This means a `PRIORITY_BACKGROUND` Vigil CfC run under `umsq` can execute at the same time as a `PRIORITY_SAGE` Ollama stream under `llm_queue`. The comment says this is tracked in `ORB_ANALYSIS.md` as a larger refactor. For now, be aware that the priority guarantee is per-serializer, not end-to-end across all GPU users.

---

#### `vram_guard(label="")` context manager

**Purpose:** Wraps any GPU inference block with pre/post memory cleanup.

**Usage:**
```python
with vram_guard("Vigil CfC"):
    output = cfc_model(input_tensor)
```

**What happens on exit (always, even if the inference throws):**

1. `gc.collect()`, tells Python's garbage collector to run immediately, releasing reference cycles that are holding GPU tensor memory on the Python side.
2. `torch.mps.empty_cache()`, tells PyTorch's MPS allocator to release all unused cached memory back to the system. Only called if PyTorch is installed and the method exists.
3. `mx.metal.clear_cache()`, tells MLX to release its Metal command buffer pool.
4. `mx.clean_eval()`, tells MLX to clean up its computation graph evaluator state.

Both PyTorch and MLX imports are wrapped in `try/except (ImportError, RuntimeError, AttributeError)` so if either framework is not installed (as on the current 8GB development Mini where full model inference may not be active), `vram_guard` is still safe to call, it simply skips the unavailable cleanup steps.

The elapsed time is logged at DEBUG level so you can see exactly how long each GPU task took.

> ⚠️ **If you change this:** Add `vram_guard` around every new in-process GPU inference block. Forgetting it on a frequently-called path (like Vigil's per-patient CfC score) will cause gradual memory growth. On an 8GB or 16GB system, this can cause OOM failures within hours of continuous operation.

---

#### Class: `MetalQueueCoordinator`

**Purpose:** Ensures that at most one major in-process GPU inference runs at any instant, with higher-priority tasks (Sage voice) always preempting lower-priority ones (background Vigil scoring).

**Internal state:**
- `_queue`, An `asyncio.PriorityQueue` of `(priority, seq, event, task_name)` tuples. Created lazily to avoid event-loop mismatch.
- `_guard_lock`, An `asyncio.Lock` protecting reads/writes of `_active`, `_active_task`, and the queue. Created lazily.
- `_counter`, Sequence number for FIFO tie-breaking within the same priority.
- `_active`, Boolean, True if a task currently holds the GPU lock.
- `_active_task`, Name string of the currently running task, for log messages.

**`acquire(task_name, priority)` method**

This returns an `_AcquireContext` async context manager. You use it like:

```python
async with umsq.acquire("Sage Voice", PRIORITY_SAGE):
    with vram_guard("Sage MLX"):
        # GPU inference here
```

When the `async with` block is entered (`__aenter__`):
1. Creates an `asyncio.Event`.
2. Under `guard_lock`, increments the counter and puts `(priority, seq, event, task_name)` onto the priority queue.
3. If the GPU is idle (`not self._active`), immediately calls `_wake_next()` to grant the lock.
4. If the GPU is busy, logs a "queued, GPU busy with X" message.
5. Waits on `event.wait()` until woken.

When the `async with` block exits (`__aexit__`):
1. Logs how long the task held the GPU (in milliseconds).
2. Under `guard_lock`, sets `_active = False`, clears `_active_task`.
3. Calls `_wake_next()` to grant the GPU to the next highest-priority waiter.
4. Returns `False` (exceptions from the inference block are not suppressed).

**`_wake_next()` method**

Must be called under `guard_lock`. Pops items from the priority queue until it finds one that is not cancelled (cancelled events have `event._is_cancelled = True`). Sets `_active = True`, `_active_task = task_name`, and calls `event.set()` to wake the waiter.

**`is_busy` and `active_task` properties**

Read-only status checks. Useful for health endpoints or logging.

**`umsq` (module-level singleton)**

`umsq = MetalQueueCoordinator()` at the bottom of the file. Import and use this everywhere in-process GPU work is done.

> ⚠️ **If you change this:** Adding a new in-process GPU task (e.g., a new MLX model) must use `async with umsq.acquire(...)` with the appropriate priority constant, and the inference block must be inside `with vram_guard(...)`. Skipping either of these can cause concurrent GPU access with unpredictable results on Apple Silicon.

---

### 10.5 `server/services/audit_service.py`: Append-Only HIPAA Audit Log

**What this file is**

Every significant action in Orb, a user logging in, a clinician viewing a patient record, Sage generating a recommendation, Scribe signing a note, an API key being created, must be recorded in a tamper-evident audit log. This file provides the single function for writing those records, and the tamper-detection mechanism that proves the log has not been altered.

HIPAA's audit-control requirement (§ 164.312(b)) mandates that covered entities implement hardware, software, and procedural mechanisms to record and examine activity in systems that contain PHI. This file is Orb's implementation of that requirement.

**Libraries used**

- `hashlib`, For SHA-256 hashing (the chain integrity mechanism).
- `json`, For serializing metadata dictionaries and for the canonical JSON used in hash computation.
- `database.db_session`, SQLite access.
- `config.AUDIT_SIMULATION_EVENTS`, A boolean flag. If False, events sourced from simulation data are silently skipped so demo runs don't pollute the real audit trail.

---

#### The two-layer tamper-evidence model

The comments at the top of the file describe two independent defenses against tampering:

1. **Database-level triggers:** The `audit_logs` table has `BEFORE UPDATE` and `BEFORE DELETE` SQLite triggers that call `RAISE(ABORT)`. This means the application's own database user cannot execute `UPDATE` or `DELETE` on audit rows, the database engine itself rejects those statements before they execute. Even a bug (or malicious code) that tries to delete an audit row will get an exception.

2. **Per-row SHA-256 hash chain:** Every row stores two hash columns: `prev_hash` (the `row_hash` of the previous row) and `row_hash` (SHA-256 of this row's fields combined with `prev_hash`). This forms a chain: to tamper with any row, an attacker would also have to recompute and update every subsequent row's hash, but they cannot do that because the database triggers block writes. Even if someone bypassed the DB (editing the SQLite file directly), the chain verification function would detect the break.

---

#### `_FAIL_CLOSED_CATEGORIES`

```python
_FAIL_CLOSED_CATEGORIES = frozenset({
    "security", "clinical_access", "phi_access", "authentication", "ai_inference",
})                                         # server/services/audit_service.py:43-45
```

These are the event categories where an audit write failure is *not* recoverable. If writing the audit record fails for any of these categories, the `log_audit_event` function raises `AuditWriteError` so the caller can refuse to proceed. For example: if the audit write fails for a login attempt, no token is issued. None of the `authentication` sites in `server/routes/auth_routes.py` catch the error, so the request fails with an unhandled 500 rather than allowing an unaudited authentication.

For other categories (like `"operational"` or `"simulation"`), a write failure is logged as `CRITICAL` but the function returns normally, the operational path continues.

> **Correction (2026-08-15):** earlier printings listed four categories. There are **five**. `"ai_egress"` is fail-closed **by design**: a hosted AI call may only proceed once its per-inference record is durably on the hash chain (`server/services/ai_governance.py`), an unaudited egress of clinical queries is treated exactly like an unaudited PHI access.

> **Correction (2026-09-04):** the note above was wrong about `"ai_egress"`. No hosted lane exists, and no call site ever emitted `"ai_egress"` or `"auth"`. Logins, MFA and lockouts are written as `"authentication"` (27 sites in `server/routes/auth_routes.py`) and the per-inference AI record as `"ai_inference"` (`record_inference` in `server/services/ai_governance.py`), so until 2026-09-04 a failed audit write for a login or an AI inference was fail-open in practice while the set claimed otherwise. Fixed 2026-09-04: the set now reads as printed above, and `server/tests/test_audit_fail_closed_categories.py` asserts that every name in it is emitted by a real call site. Two nuances. `"authentication"` is fail-closed end to end: no caller catches `AuditWriteError`, so a login, PIN login or device bind whose audit write fails returns an unhandled 500 and issues no token or cookie; for the MFA, device and PIN admin routes the DB mutation is committed before the audit call, so the mutation persists and the client sees 500. `"ai_inference"` is fail-closed at the audit layer only: all three `record_inference` callers (`server/services/sage_service.py`, `server/routes/sage_routes.py`, `server/services/sage_narrator_service.py`) catch the exception and serve the turn, so an audit outage does not refuse AI inference. `"authorization"` (six sites, all permission denials), `"user_management"`, `"system_administration"` and the operational categories remain fail-soft.

> ⚠️ **If you change this:** Adding a new event category that touches PHI or controls access should be added to `_FAIL_CLOSED_CATEGORIES`. Omitting it means a storage failure could allow an unaudited security action to proceed silently.

**The downstream consequence a CIO should be told about, because it is a real availability trade.** Because `phi_access` / `clinical_access` are fail-closed, **a full disk means clinicians cannot open charts**, verified by execution: 17 of 18 PHI surfaces refuse the read when the audit write cannot persist (`server/services/audit_service.py:57-60`). That is the control working as designed, and it is why the field bounds below exist.

---

#### Field bounds: the append-only table is a DoS surface

`server/services/audit_service.py:43-125`. **Executed 2026-08-06.** `audit_middleware` takes `correlation_id` straight off the client's `x-correlation-id` header and `user_agent` off `user-agent`, and neither was bounded anywhere on the way to the INSERT. One ordinary logged-in nurse, POSTing to any `/api/` route with a 200 KB header, wrote **260 KB of attacker-chosen bytes per row, measured at ~92 MB/s of audit-table growth from a single account.**

On an appliance that is a clinical outage with three steps and no way back:

1. `audit_logs` is DELETE-blocked by `trg_audit_logs_no_delete`, so the space **cannot be reclaimed**, not by an admin, not by a cleanup job. The append-only guarantee that makes the trail evidence also makes this damage permanent short of a restore.
2. When the disk fills, the audit INSERT starts failing.
3. `phi_access` / `clinical_access` are fail-closed → clinicians cannot open charts.

| Bound | Value | `audit_service.py` |
|---|---|---|
| `_MAX_ID_CHARS` | 200 | `:68` |
| `_MAX_PATH_CHARS` | 512 | `:69` |
| `_MAX_USER_AGENT_CHARS` | 512 | `:70` |
| `_MAX_SUMMARY_CHARS` | 4000 | `:71` |
| `_MAX_METADATA_BYTES` | 8192 | `:72` |

Two details that are load-bearing:

- **Control characters are stripped from the single-line identifier columns** (`_ID_FIELDS`, `:78-81`; `_CONTROL_CHARS`, `:85`), C0 controls, DEL, and U+2028/U+2029. `correlation_id` is the field forensics GROUPs BY when reconstructing "everything that happened in session X"; a caller who can embed a newline plus a plausible-looking row into it can make an exported or rendered trail read as though it contains events it does not.
- **`bound_audit_fields` runs BEFORE `compute_audit_row_hash` and before the INSERT** (`:104-108`), so the hash is taken over exactly the bytes that get stored and `database.verify_audit_chain()` still reconciles. That ordering is not stylistic; reverse it and every bounded row fails verification.
- Oversized `metadata_json` **keeps the row and loses only the blob**, replacing it with `{"_truncated": true, "original_bytes": N}` (`:117-124`), a missing row silently changes what the trail says happened, and a truncated fragment would not parse as JSON.

The bound lives in `log_audit_event` rather than in the middleware deliberately: this function is the single choke point every audit row in the codebase passes through, so a route added tomorrow inherits the limit instead of having to remember it.

---

#### `_HASH_FIELDS` tuple

```python
_HASH_FIELDS = (
    "timestamp", "correlation_id", "event_category", "actor_user_id", "actor_role",
    "target_type", "target_id", "patient_id", "encounter_id", "source_type",
    "source_event_id", "request_path", "http_method", "ip_address", "user_agent",
    "session_id", "outcome", "action_type", "summary_message", "metadata_json",
)
```

These are the fields that participate in the hash chain, in a fixed order. The order and membership are part of the chain contract.

> ⚠️ **If you change this:** Adding, removing, or reordering fields in `_HASH_FIELDS` **breaks the chain**. All previously written rows will fail verification because their stored `row_hash` was computed with the old field list. If you need to add a field to the hash, you must treat it as a schema migration: bump a chain-version column, verify all existing rows before migration, and recompute hashes for migrated rows. Do not change this lightly.

---

#### `compute_audit_row_hash(row, prev_hash)`

**Input:** A dictionary of the row's fields (keyed by the names in `_HASH_FIELDS`) and the previous row's hash (or `None` for the very first row).

**Output:** A 64-character hex string (SHA-256 digest).

**How it works:**
1. Builds a canonical JSON string of only the `_HASH_FIELDS` fields from `row`, using `sort_keys=True` and `separators=(",", ":")` for deterministic serialization.
2. Creates a SHA-256 hash object.
3. Updates it with the `prev_hash` string (empty string if `None`).
4. Updates it with the canonical JSON string.
5. Returns the hexdigest.

This function is used both when writing a new row (to compute its hash) and by `verify_audit_chain()` (to recompute expected hashes and compare against stored ones).

---

#### `log_audit_event(...)`: the main function

**Arguments (all keyword, most optional):**

| Argument | Meaning |
|---|---|
| `correlation_id` | Trace ID linking this audit event to an event bus event or request. |
| `event_category` | Broad category: `"auth"`, `"clinical_access"`, `"phi_access"`, `"security"`, `"operational"`, `"simulation"`. |
| `action_type` | Specific action: `"login_success"`, `"patient_viewed"`, `"note_signed"`, `"vigil_alert_generated"`, etc. |
| `outcome` | `"success"` or `"failure"`. |
| `db` | If the caller already has a database connection open in a transaction, pass it here to write the audit row in the same transaction. If omitted, a new connection is opened. |
| `summary_message` | Human-readable description of what happened. |
| `actor_user_id` | Who did it (user ID from the `users` table). |
| `actor_role` | Their role at the time of the action. |
| `target_type` | What was acted on: `"patient"`, `"user"`, `"note"`, etc. |
| `target_id` | The specific record's ID. |
| `patient_id` | Which patient (if PHI was involved). |
| `ip_address`, `user_agent`, `session_id` | Network/session context from the HTTP request. |
| `metadata` | A dictionary of additional structured data. Serialized to JSON and stored in `metadata_json`. |

**What it does:**

1. Checks whether to suppress the event (if `AUDIT_SIMULATION_EVENTS` is False and `source_type == "simulation"` or `event_category == "simulation"`). Note: the old code suppressed on `actor_role == "system"`, which was removed because it would have silently skipped audits for automated system actions on real PHI.
2. Determines whether this is a fail-closed category.
3. Gets the current UTC timestamp.
4. Serializes `metadata` to JSON.
5. Inside a database connection, reads the most recent `row_hash` from `audit_logs` (the chain's current tail).
6. Computes the new row's hash using `compute_audit_row_hash`.
7. Executes the `INSERT INTO audit_logs` with both `prev_hash` and `row_hash` populated.
8. If anything fails: logs `CRITICAL`, then raises `AuditWriteError` for fail-closed categories or returns normally for others.

---

#### Class: `AuditWriteError`

A custom exception (`RuntimeError` subclass) raised only when a fail-closed audit write fails. Callers that catch this should refuse to proceed with the action that would have been audited.

---

### 10.5a `server/services/audit_middleware.py`: the mutation safety net

> **New section (2026-08-15).** This file was never documented. It is the layer that guarantees no state change goes unaudited, and it carried a defect until 2026-08-14 that an auditor would consider severe.

**What it is** (`server/services/audit_middleware.py:1-10`). HIPAA §164.312(b) requires audit controls over PHI access. Rather than hand-wire an audit call into every route, the prior state, in which **only ~4 of 18 route modules logged anything**, this middleware records **every** state-changing API request generically: who (actor from the session), what (method + path), and the outcome (HTTP status). Route-level audit events with richer domain metadata still fire where they exist; this is the safety net underneath them.

**Scope.** `POST/PUT/PATCH/DELETE` on paths starting `/api/`, excluding `/api/auth/login` (`:31-36`, `:81-86`), which is audited in `auth_routes` and has no bearer token yet anyway.

**Read-only GETs are intentionally not logged here** (`:11-13`), too high-volume, and reads are better audited at the sensitive-endpoint level via `permissions.log_phi_read` (§1). Know this boundary: it is why the 2026-08-05 PHI-scope leaks were unprovable after the fact.

#### The actor defect: fixed 2026-08-14

`_actor_from_request` (`server/services/audit_middleware.py:39-74`). Read this docstring in full before touching auth or the audit trail.

> This middleware used to re-implement the lookup as "read the `Authorization` header", and that copy **went stale the moment the SPA moved onto the HttpOnly `orb_session` cookie** (`server/auth.py:26`). The browser stopped sending an `Authorization` header, this function stopped finding a token, and **EVERY mutation made from the app, every order, every appointment, every receipt, landed on the tamper-evident chain with `actor_user_id` NULL.** The rows kept being written, the chain kept verifying, and the trail said nobody did any of it.

Two things make this worse than a cosmetic gap, and both are worth stating to a compliance officer verbatim:

1. `server/auth.py:31-44` rests the entire PIN-on-device design on the claim that **the actor is the person, recorded per request**. A chain of unattributed mutations cannot support that claim.
2. **The loss is unrecoverable after the fact**, because `audit_logs` is append-only. You cannot go back and attribute those rows.

**The fix, and why it is structural rather than a patch:** extraction is delegated to `auth._extract_token`, *the same function `get_current_user` authenticates with*. There is now exactly **one** token-extraction rule in the process. A future credential channel (another cookie name, a header rename) reaches the audit trail for free, and cannot silently un-attribute it again.

An unauthenticated attempt is still audited, just without an actor it cannot prove (`:70-74`); the function never raises.

#### Three properties of the write

| Property | `audit_middleware.py` | Why |
|---|---|---|
| **Logged in a `finally`** | `:92-105` | A mutation that raises an unhandled exception (→ 500) is still audited, not just ones that return a response object. `status` defaults to 500 and is overwritten once a response exists. |
| **Off-loop via `run_db`, but still awaited** | `:15-21:106-124` | This is the highest-traffic DB write in the codebase, every mutation pays one, and since encryption every page costs HMAC-SHA512 + AES. Three production freezes came from this shape. **Off-loop is not the same as fire-and-forget:** the response is not returned until the audit row is durable. |
| **`asyncio.shield()`** | `:104-108:125-129` | The write is submitted to the DB executor and allowed to finish even if *this request* is cancelled (client disconnect, timeout). Without it a mid-flight disconnect would cancel the queued write and silently drop the audit row for a mutation the server had already applied, the sync version could not lose it, so neither may this one. The `CancelledError` re-raise at `:126-129` is the house rule: our await was cancelled, not the write; the shielded task runs to completion and cancellation propagates. |

**Outcome mapping** (`:104`): `success` for status < 400, `denied` for 401/403, `failure` otherwise.

**`api_mutation` is fail-SOFT** (`:134-136`). A failed safety-net row must not turn a successful clinical write into a 500. This is the deliberate counterpart to the fail-closed categories in §10.5, the safety net may not itself become a failure mode.

---

### 10.6 `server/ws/realtime.py`: Real-Time WebSocket Layer

**What this file is**

The browser clients (the nurse station dashboard, the Sage chat UI, Relay) need live updates, new Vigil alerts, new Relay messages, Sage streaming responses, without polling the server every second. This file sets up the Socket.IO WebSocket server that pushes those updates in real time.

**Libraries used**

- `socketio`, The `python-socketio` library, which implements the Socket.IO protocol on top of WebSockets. Provides rooms (named groups of connections), sessions (per-connection server-side state), and event routing.
- `database.db_session`, For looking up the authenticated user's DB record during handshake.
- `config.CORS_ORIGINS`, `config.DEPLOYMENT_PROFILE`, CORS configuration. The allowed origins list is explicitly set (never `"*"`) to prevent unauthorized origins from opening authenticated sockets.
- `auth.decode_token`, JWT decoder used during the handshake authentication.
- `permissions.has_permission`, RBAC permission checker.
- `services.relay_service.get_channels_for_user`, Returns the list of Relay channels a given user/role can access. Used to enforce channel authorization on WebSocket join.

---

#### The `sio` object

```python
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=_cors_allowed,
    ...
)
```

This is the Socket.IO server instance. It is imported by other parts of the code (especially `nervous_system_triggers.py`) to push events to clients: `await sio.emit("patient:timeline_updated", {...}, room=patient_id)`. The `room` parameter means only clients who have joined that room (typically identified by `patient_id`) receive the message.

> ⚠️ **If you change this:** The `cors_allowed_origins` previously used `"*"` (any origin). It was changed to `CORS_ORIGINS` from config. If you are testing locally and WebSocket connections fail with a CORS error, check that your development origin (e.g., `http://localhost:5173`) is in `CORS_ORIGINS`.

---

#### `_authenticate(auth)`: JWT validation at handshake

**Input:** The `auth` object from the Socket.IO handshake. This is a dictionary the browser sends at connect time, typically `{"token": "<JWT>"}`.

**Output:** The full user record from the `users` database table, or `None` if authentication fails.

**What it does:**

1. Checks that `auth` is a dictionary (rejects bare values).
2. Extracts the token from `auth["token"]`, `auth["Authorization"]`, or `auth["authorization"]` (any of these forms).
3. Strips the `"Bearer "` prefix if present.
4. Calls `decode_token(token)`, if the token is expired or invalid, this raises an exception, which is caught and returned as `None`.
5. Extracts `user_id` from the token's `sub` claim. If `user_id` is missing or is the special `"discharge_access"` subject (used for patient discharge QR codes, not staff sessions), returns `None`.
6. Looks up the user record in the `users` table by `user_id`. This is important: the role, name, and ID are taken from the *database*, not from the token's claims. This means if a user's role is changed, their next WebSocket connection will see the new role even if they have an old token.
7. Returns the full database row as a dictionary, or `None` if the user does not exist.

> ⚠️ **Security note:** The identity and role stored in the per-connection session always come from the database lookup, never from client-supplied fields. Client-supplied data (channel ID, message content) is never trusted for identity. If you add a new event handler, always read identity from `await sio.get_session(sid)`, never from `data`.

---

#### `@sio.event connect(sid, environ, auth=None)`

**Triggered when:** A browser opens a new WebSocket connection.

**What it does:**

1. Calls `_authenticate(auth)` with the handshake credentials.
2. If `_authenticate` returns `None`, returns `False`, this refuses the connection. The browser will receive a connection error.
3. If authentication succeeds, stores the user's `id`, `name`, and `role` in the per-connection session: `await sio.save_session(sid, {...})`. This is the server-side session that subsequent event handlers read to know who is on the other end of the socket.
4. Logs the successful connection.
5. Returns `True` (or simply falls through).

> ⚠️ **If you change this:** Never skip the authentication step. Never return `True` without calling `_authenticate`. Adding a test shortcut that bypasses authentication ("if in development mode, accept any connection") would allow unauthenticated PHI access over WebSockets.

---

#### `@sio.event disconnect(sid)`

Simple cleanup. Logs the disconnection. Socket.IO automatically handles room cleanup.

---

#### `_user_can_access_channel(user_id, user_role, channel_id)`

An internal helper that reuses `get_channels_for_user()`, the same function that the REST API uses, to check whether the authenticated user is authorized for a given Relay channel. Returns `True` if the channel appears in the user's visible channel list.

This ensures that WebSocket channel authorization matches the REST API. If a REST endpoint denies a user access to a channel, the WebSocket handler also denies it.

---

#### `@sio.event join_channel(sid, data)`

**Triggered when:** A client sends `socket.emit("join_channel", {channel_id: "..."})`.

**What it does:**

1. Reads the server-side session for `sid` (contains `user_id` and `role`).
2. Extracts `channel_id` from `data`.
3. Calls `_user_can_access_channel`, if the user is not authorized, emits an `"error"` event back to the client and returns.
4. If authorized: `await sio.enter_room(sid, channel_id)`, adds this connection to the Socket.IO room named after `channel_id`. Future `sio.emit(..., room=channel_id)` calls will reach this client.

---

#### `@sio.event leave_channel(sid, data)`

Removes the client from the named channel room. Straightforward.

---

#### `@sio.event typing(sid, data)`

Broadcasts a typing indicator to all other members of a channel. Re-derives sender identity from the server-side session (never from `data`). Checks `_user_can_access_channel` before broadcasting.

---

#### `@sio.event new_message(sid, data)`

Handles a client sending a new Relay message over WebSocket. This mirrors the REST `POST /api/relay/channels/{id}/messages` endpoint but in real time.

**What it does:**

1. Reads the server-side session. Rejects unauthenticated connections.
2. Extracts `channel_id` from `data`.
3. Checks `has_permission(session["role"], "relay.send")`, RBAC enforcement.
4. Checks `_user_can_access_channel`, channel membership enforcement.
5. Calls `relay_service.send_message(...)` with identity from the session (never from `data`).
6. Emits `"message:new"` to all members of the channel room.

---

#### The `import ws.scribe_stream` at the bottom

```python
import ws.scribe_stream  # noqa: F401, E402
```

This import has a side effect: registering Scribe's streaming-transcription Socket.IO event handlers on the same `sio` instance. The handlers live in `ws/scribe_stream.py` and are registered simply by importing that module. There is no explicit registration call.

> ⚠️ **If you add new WebSocket event handlers:** Either add them directly to this file, or create a new `ws/your_module.py` and import it here at the bottom.

---

### 10.7 `server/routes/audit_routes.py`: REST API for Audit Log Queries

**What this file is**

A FastAPI router that exposes the audit log to authorized users through a REST endpoint. Compliance officers, security administrators, and on-call engineers can query audit records without direct database access.

**Router:** Mounted at `/api/audit`. All endpoints require the `"audit.read"` permission (enforced via `Depends(require_permission("audit.read"))`).

---

#### `GET /api/audit/logs`

**Query parameters (all optional):**

| Parameter | Purpose |
|---|---|
| `patient_id` | Filter to events touching a specific patient. |
| `correlation_id` | Filter to all events sharing a correlation chain (trace a single workflow). |
| `action_type` | Filter by specific action (e.g., `"login_success"`, `"note_signed"`). |
| `event_category` | Filter by category (e.g., `"auth"`, `"clinical_access"`). |
| `outcome` | Filter by `"success"` or `"failure"`. |
| `start_date` / `end_date` | Date range filter (ISO-8601 strings compared against `timestamp`). |
| `limit` | Page size (1–1000, default 50). |
| `offset` | Pagination offset (default 0). |

**Returns:**

```json
{
  "logs": [...],
  "total": 1234,
  "limit": 50,
  "offset": 0
}
```

Results are ordered by `timestamp DESC` (newest first).

**Correlation ID lookups are especially powerful:** Because every event bus event, every LLM queue acquire, and every audit write share the same `correlation_id`, searching by a single correlation ID can reconstruct the entire chain of events triggered by one Vigil alert or one signed note.

> ⚠️ **Read-only:** This endpoint only reads. There is no DELETE or UPDATE endpoint, the database triggers prevent those anyway. If you are asked to build a "delete audit record" feature, this is not permitted by the architecture (and would violate HIPAA).

---

### 10.8 `server/routes/admin_routes.py`: Governance Dashboard Backend

**What this file is**

A FastAPI router that provides a pre-aggregated governance snapshot for Orb's Admin dashboard. Rather than having the frontend run multiple queries, this single endpoint returns a comprehensive summary of the last 24 hours across audit, ingestion, alerts, and authentication.

**Router:** Mounted at `/api/admin`. All endpoints require `"admin.overview"` permission.

---

#### `GET /api/admin/overview`

**Returns a single JSON object with these sections:**

| Section | Contents |
|---|---|
| `ingestion_mode` | Current data ingestion mode (from config). |
| `audit_simulation_enabled` | Whether simulation events are being written to the audit log. |
| `audit_total_24h` | Total audit events in the last 24 hours. |
| `audit_categories` | Count breakdown by event category (e.g., `{"auth": 42, "clinical_access": 310}`). |
| `audit_outcomes` | Count breakdown by outcome (`{"success": 340, "failure": 12}`). |
| `ingestion` | Summary of data ingestion: total messages, parsed, failed, duplicate, vitals inserted, observations inserted. |
| `source_mix` | Count by ingestion source type. |
| `alerts` | Count of Vigil alerts and Relay alerts generated. |
| `recent_failures` | Last 5 ingestion parse failures with their error messages and correlation IDs. |
| `auth_events` | Auth event breakdown by action type and outcome (useful for detecting credential-stuffing or lockout patterns). |

All queries use a `cutoff_24h` variable (`now - 24 hours`) as the time window, computed fresh on each request.

**How this relates to the audit log:** The `audit_by_category`, `audit_by_outcome`, `alert_counts`, and `auth_summary` queries all read from the `audit_logs` table. This means the admin dashboard is a live view of the audit trail, every clinical action, authentication event, and alert is summarized here automatically, because every module calls `log_audit_event`.

> ⚠️ **If you add a new module:** If your module emits audit events (it should), those events will automatically appear in the admin overview under their category without any changes to this file. If you add a new `action_type` that you want explicitly counted in the `alerts` section, you must add it to the `IN ('vigil_alert_generated', 'relay_alert_generated', 'your_new_type')` filter.

---

### Summary: How the infrastructure layers work together

To close this section, here is a complete picture of how all six components interact during the most complex workflow, a Vigil critical alert triggering Sage pre-warming and EHR extraction from a signed note:

```
1. Vigil fires:
   vigil_service.py → event_bus.publish(StateEvent("vigil.alert.critical", patient_id="P123", ...))

2. Event bus:
   - Writes row to SQLite event_log (WAL)
   - Finds matching subscriber: on_critical_vigil_alert
   - Fires asyncio.create_task(_safe_dispatch(on_critical_vigil_alert, event))

3. nervous_system_triggers.on_critical_vigil_alert():
   - Runs _assemble_patient_context() in a thread (blocking SQLite reads)
   - Runs _get_guideline_context() for the RAG lookup
   - Writes compiled context to sage_decision_cache with 10-min TTL

4. Nurse signs a Scribe note:
   scribe_service.py → event_bus.publish(StateEvent("scribe.note.signed", ...))
   audit_service.log_audit_event(event_category="clinical_access", action_type="note_signed", ...)

5. Event bus dispatches on_scribe_note_signed():
   - await llm_queue.acquire(priority=3)   ← waits behind any active Sage call
   - Calls Ollama /api/chat with SOAP extraction prompt
   - FAIL CLOSED on any error: writes nothing, calls llm_queue.release() in finally
   - On success: writes medications/vitals/clinical_documents
   - await sio.emit("patient:timeline_updated", room="P123")  ← pushes to browser

6. Browser (nurse's dashboard):
   - Receives "patient:timeline_updated" over the authenticated Socket.IO connection
   - Only possible because connect() validated the JWT at handshake time

7. Admin reviews:
   GET /api/admin/overview  → reads audit_logs for last 24h summary
   GET /api/audit/logs?correlation_id=corr-abc123  → full trace of the note-signing chain
```

Every arrow in this diagram passes through one of the files in this section. Change the event name, change a priority constant, break the `finally: llm_queue.release()`, or bypass the `_authenticate` check, and the arrow either silently disappears or creates a security hole. That is why these files deserve careful review before any change.
