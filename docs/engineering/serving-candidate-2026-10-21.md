# Controlled local inference qualification

The new [`serve_ollama_candidate.py`](../../scripts/serve_ollama_candidate.py)
launcher is an opt-in qualification tool. It does not change the existing
`serve-ollama.sh` default or qualify a hospital deployment by itself. An inspect
command sends no model request. An execute command starts only its own daemon
on an explicitly chosen loopback port other than 11434. It never stops another
daemon, downloads a model, changes a cached weight, or changes the existing runner
configuration.

## What is held constant

The candidate pins installed Ollama 0.40.4, its llama-server build at
`b42b7e6d3`, and the existing MedGemma 27B model blob
`4b97da6b21a8fb1405a0d1efee462bfd6c4d3c46307bac8b34373835f07ca758`.
The model remains Q4_K_M and KV remains f16. Requests retain the existing clinical
prompts, sampling, output budgets, record coverage and 8192-token context. No
speculative draft model, smaller clinical model, shortened answer, dropped source
data or changed safety threshold is introduced by this tool. Engine changes can
still affect floating-point execution and sampled output; preserving these
settings is not a substitute for clinical output evaluation.

The owned 0.40.4 log confirms that Ollama itself passes `--context-shift --keep 4`
to llama-server. This CLI policy takes precedence over an environment setting;
the candidate does not claim to disable it. Orb retains its existing prompt
coverage/budget guards and treats truncated generated answers as unsuccessful.

The variables under test are full versus compact SWA cache storage, real engine
parallelism of 1/2/4 and optionally automatic/on/off flash attention. A private
runner INI retains prompt similarity 0.6, with separate KV buffers and an 8192 MiB
RAM prompt cache. Prompt caching reuses matching input token state; it does not
reuse an answer to a different clinical question. The server must remain local,
behind Orb's authorization and bounded admission.

[Pinned Ollama source](https://github.com/ollama/ollama/blob/v0.40.4/llm/llama_server.go)
passes total context as per-request context times parallelism and passes the
parallel slot count separately. Thus 2 slots with an 8192-token request context
do not divide each request into 4096 tokens. The inspected
[llama-server build](https://github.com/ggml-org/llama.cpp/blob/b42b7e6d3/tools/server/README.md)
supports continuous batching and these cache settings.

`OLLAMA_FLASH_ATTENTION=auto` is deliberately absent from the child environment.
In this Ollama version the environment parser treats an unparseable boolean
string as true. Unsetting the variable selects genuine automatic policy; the
tool maps explicit on/off to 1/0. See the pinned
[environment parser](https://github.com/ollama/ollama/blob/v0.40.4/envconfig/config.go)
and runner source above. An inherited model, context, output-limit, backend,
proxy or prompt-logging override cannot silently change the controlled profile.

## Inspection and memory guard

Choose a new, unused port and a new directory with an existing parent:

```sh
server/.venv/bin/python scripts/serve_ollama_candidate.py \
  --port 11449 --parallel 2 \
  --models-dir /absolute/path/to/existing/.ollama/models \
  --run-dir /private/tmp/orb-ollama-two-slot-qualification
```

This prints a plan and creates nothing. Adding `--execute` creates the private
configuration/manifest and executes `ollama serve`; it still calls no model.
Run the daemon under the existing disposable load-test supervisor so only its
owned process group is stopped. Do not use the example port if another test
already owns it. Sandboxed environments which forbid bind inspection fail
explicitly; no conflicting process is stopped to bypass the failure.

The preflight records `memory_pressure -Q` and `vm_stat`. It budgets the exact
weight-file size plus 4 GiB of full 8192-token KV per slot, 8 GiB RAM prefix
cache, 4 GiB workspace and 8 GiB additional headroom. This is a conservative
qualification estimate, not a measured high-water mark. At 64 GiB total and a
75% OS free-memory estimate, the current 27B cache fits this estimate at 1/2
slots; 4 slots are refused. A failed 4-slot preflight is not a 4-slot throughput
measurement. Do not force-unload someone else's model or reduce clinical context
to get through the guard.

The benchmark supervisor must also sample ongoing pressure and stop its owned
daemon on sustained pressure. Initial and peak resident memory, compression,
swap deltas and other running model occupancy belong in the result. Refuse
promotion if the engine silently reduces context, falls back to CPU or cannot
provide stable request isolation and cancellation.

The [`qualify_ollama_serving.py`](../../scripts/qualify_ollama_serving.py)
supervisor performs the bounded local sequence. It starts no work by default:

```sh
server/.venv/bin/python scripts/qualify_ollama_serving.py \
  --models-dir /absolute/path/to/existing/.ollama/models \
  --output-dir /private/tmp/orb-serving-new-qualified-run
```

On a dedicated test window, adding `--execute --allow-model-load
--allow-heavy-model-load` starts the owned 1-slot and 2-slot candidates. The
window is capped at 300 seconds. A separate one-token warmup is excluded from
answer acceptance; the 1-slot profile then runs one actual general, patient and
department request sequentially. The 2-slot profile runs two concurrent queries,
then 200 requests with a 20-second hard request deadline and actual capacity2.
Every completed phase retains all attempted requests, failures and rejections.
An interrupted phase is marked incomplete and cannot establish acceptance.

`--profiles 1 --burst-only` runs the actual 200-request burst against one real
engine slot after warmup and fixture startup, without repeating the sequential
diagnostic. The default remains profiles1/2 with the diagnostic sequence above.
Requests are never removed from the denominator because they were busy, timed
out or returned an incomplete stream. A skipped profile does not pass.

The report separates `procedure_completed` from `acceptance_passed`. Acceptance
requires a completed 200-request phase, unchanged MedGemma 27B provenance, fresh
full answers within all targets and full-retrieval proof with no failed/pending
work or lexical fallback. An executed procedure can finish successfully while
acceptance fails; the supervisor then exits nonzero. Engine token timings include
warmup/cancelled tasks and cannot independently prove HTTP answer completion.

The supervisor checks pressure every two seconds. It stops only its own daemon
group on two consecutive samples below 15% OS free-memory estimate, more than
512 MiB new swapouts, or more than 4 GiB new compressor occupancy. These are
conservative host-protection thresholds, not medical operating limits. It
captures pre/post counters and verifies its allocated port is closed afterward.
Disposable Orb authenticates and reads only its synthetic, isolated database;
synthetic streamed answers are retained locally for review. Actual patient data
and external inference services are absent from this workflow.

## Acceptance sequence

Run CPU/HTTP profiling separately from real model work. Compare a serial baseline
with each admissible candidate, using actual synthetic Sage prompts captured
from the disposable application and unchanged request options. Record cold
model loading separately from ready, preloaded serving. Include completed
answers, real token counts, prompt-cache counters, ordinary stop versus
truncation, errors, queue time, first readable content and full completion.
Inspect answer correctness and source coverage as well as timing.

Only then increase simultaneous distinct requests under an application worker
lease whose capacity matches the actual daemon slot count. A fictional 200-slot
stub, 200 identical cached answers or 200 fast overload errors do not qualify
200 fresh clinical generations. Every attempt stays in the denominator. Do not
run this benchmark concurrently with another benchmark, model generation or
large test/build process on the same host.

Report general/patient/department p50, p95, p99, maximum and deadline misses for
the strict 3/5/15-second completed-answer targets. The
[Ollama concurrency documentation](https://docs.ollama.com/faq#how-does-ollama-handle-concurrent-requests)
describes additional KV memory with parallel context; it does not promise latency
or throughput. An admissible 2-slot profile is an experiment, not evidence that
one machine can serve 200 fresh answers within those targets.
