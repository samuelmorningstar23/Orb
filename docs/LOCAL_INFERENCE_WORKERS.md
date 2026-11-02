# Qualified local inference workers

Orb preserves its existing one-interactive / one-background appliance queue
unless `ORB_LLM_WORKERS` is explicitly configured. A 200-request admission
ceiling is a memory and overload bound, **not** 200 model-generation slots.
Each physical worker's capacity must be qualified against the selected model,
context/output sizes, concurrent imaging/transcription/monitoring workload and
completed-answer latency targets on the actual hardware.

Example configuration **shape**, not a measured production profile:

```json
[
  {
    "name": "interactive-a",
    "origin": "http://127.0.0.1:11434",
    "models": ["replace-with-installed-qualified-model"],
    "capacity": 1,
    "lanes": ["interactive"],
    "qualified": true,
    "qualification": "replace-with-real-local-load-report-id"
  },
  {
    "name": "background-a",
    "origin": "http://127.0.0.1:11435",
    "models": ["replace-with-installed-qualified-background-model"],
    "capacity": 1,
    "lanes": ["background"],
    "qualified": true,
    "qualification": "replace-with-real-local-load-report-id"
  }
]
```

Set the JSON as `ORB_LLM_WORKERS` before starting Orb. Set
`ORB_LLM_POOL_MAX_INFLIGHT` to at most `200` (default `200`): it includes both
assigned and waiting model requests, not the API's independent HTTP limit.
An invalid or unqualified configuration fails startup without echoing private
environment values. Loopback origins only; duplicate ports, including aliases
such as localhost/127.0.0.1, are rejected. Different workers must be distinct
local serving daemons with model residency and engine parallelism consistent
with their qualified capacities. This module does not launch daemons, download
models or override the launcher's conservative engine defaults.

A worker serving both lanes must reserve at least one slot explicitly:

```json
{
  "name": "shared-local-worker",
  "origin": "http://127.0.0.1:11434",
  "models": ["replace-with-installed-qualified-model"],
  "capacity": 2,
  "lanes": ["interactive", "background"],
  "interactive_reserve": 1,
  "qualified": true,
  "qualification": "replace-with-real-local-load-report-id"
}
```

This example requires actual two-stream qualification; it is not safe to
assume that increasing capacity on one GPU meets the targets. Dedicated
background capacity avoids periodic summaries occupying foreground slots.
An interactive-only worker cannot run background summaries. Models must be
listed exactly; unsupported models fail closed rather than using another
model silently. Independent Ollama processes on one Studio still compete for
the same GPU/memory bandwidth. Additional local hospital compute would require
a separately designed, authenticated local transport; the current offline
boundary deliberately does not permit arbitrary LAN model origins.

## Provider integration

```python
ticket = await llm_queue.acquire(priority=1, timeout=remaining_seconds, model=actual_model)
try:
    url = llm_queue.route_url("/api/chat", model=actual_model)
    # Hold the ticket through reading and closing the HTTP stream.
    # The request's messages/options remain request-local.
finally:
    llm_queue.release(ticket)
```

Retain the explicit ticket in async generators: finalization can occur in a
different task/context. A pooled release without its own ticket refuses to
guess which patient's worker is active. `current_worker()` returns the
task-local lease; providers should record transport/daemon 5xx failures using
`record_failure()` and completed healthy operations using `record_success()`.
After two failures a worker opens its circuit for 15 seconds; recovery admits
one probe. Cancellation never counts as healthy recovery. Busy/deadline/worker
exceptions subclass `asyncio.TimeoutError` so existing background fallbacks
continue to work. Interactive HTTP callers should expose explicit busy or
deadline outcomes, never count them as successful answers.

The pool owns only worker identifiers, model names and counters. It retains no
prompt, patient record, user identity or prior model conversation. Per-model
matching, request-local tickets, bounded waits and synchronous idempotent
release prevent capacity leakage or routing one request through another
request's lease. The existing ref-counted GPU gate still excludes in-process
MLX/torch sessions while qualified Ollama streams are active; it does not
serialize qualified workers against one another.

All existing queue-owning services declare their actual selected model when
taking a lease, including careful-tier history/extraction work and vision.
Ambient micro-batches declare one homogeneous model and cap all live job
futures at 200; a batch cannot conceal an unlimited backlog behind one lease.
In pool mode `/transcribe` completes ASR first and then performs correction
through an async lease for the configured correction model. Direct synchronous
correction keeps the original transcript instead of bypassing admission from
an ASR thread. Pooled Scribe supports the qualified Ollama backend; an MLX
drafting backend is explicitly refused, and optional MLX correction/verifier
stages preserve the transcript/unverified result. An in-process GPU executor
must be separately designed and qualified before enabling those backends.

This scheduler does not qualify, batch or scale ASR subprocesses, imaging
preprocessing, clinical scoring or the database. Those resources require their
own admission limits and end-to-end mixed-workload testing. Residency and
administrative model-switch checks remain conservative: configuring a worker
does not silently reload a model the administrator unloaded.

Run one Orb API process for this in-process pool. Multiple uvicorn worker
processes would each believe they own the configured slots and over-admit the
same daemons; distributed admission must be introduced before doing that.
Expose the API through the hospital's HTTPS gateway and managed network
infrastructure. A Mac mini hosting the gateway is not a substitute for access
points, firewall, switch, UPS, backup and failover planning.
