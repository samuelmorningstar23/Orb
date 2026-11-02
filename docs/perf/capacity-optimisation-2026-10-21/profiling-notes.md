# Retrieval and serving diagnostics — 21 October 2026

These artifacts use invented patients in a disposable copied Orb instance.
Their model endpoint is a fake local HTTP service with fictional 200-slot
capacity. They measure application overhead, never actual clinical generation
capacity, clinical correctness or hospital readiness.

The earlier baseline, thread sweeps and globally profiled runs also had empty
offline embedding caches and did not initialize the clinical knowledge corpus.
They are **chart/auth/control-plane tests, not full BGE retrieval tests**. The
full installed-model retrieval experiment below closes that measurement gap.

| Artifact | Result | Interpretation |
| --- | --- | --- |
| `baseline-200-mock.json` | 200/200 completed; general full p95 6.50s, patient 6.58s, department 6.57s | Uninstrumented baseline; strict 3/5/15s acceptance failed |
| `profile-baseline-200-mock.json` | **Invalid diagnostic** | Competing Python 3.12 profiling-tool instances and a custom per-thread timer corrupted function timings and caused instrumentation failures. Retained and explicitly marked invalid; use no timings or response counts from this run |
| `profile-retrieval-200-mock.json` | Diagnostic; 200/200 completed | Retrieval changes present; repeated TLS-context construction still present. Global profiling adds overhead |
| `ssl-context-cache-200-mock.json` | 200/200 completed; general full p95 6.10s, patient 6.09s, department 6.12s | Uninstrumented retrieval + TLS-context reuse; strict acceptance still failed |
| `profile-ssl-context-cache-200-mock.json` | Diagnostic; 200/200 completed | Explicit TLS-factory stage consumed 0.0010s aggregate thread CPU for 200 calls; retrieval, executor queueing and finalisation still require work |

Each report retains its source hashes. The after-TLS uninstrumented run includes
the immutable ward/bed string caches and scoped latest-vitals query. Do not
combine fastest individual requests from different runs into an acceptance pass.

## Frozen-source thread-width sweep

The four `round2-*-200-mock.json` clean runs have identical 22 copied application
source hashes, including atomic history-pair persistence, current pure-string
retrieval caches and the model-router readiness coalescer. All complete 200/200
responses with the fake model. This is a controlled configuration comparison;
the default-executor overrides are confined to the fixture.

| Requested default executor | Wall | General full p95 | General under 3s | Patient under 5s |
| --- | --- | --- | --- | --- |
| Runtime default | 5.873s | 5.727s | 7/67 | 24/67 |
| 1 | 3.585s | 3.486s | 52/67 | 67/67 |
| 4 | 4.563s | 4.438s | 35/67 | 67/67 |
| 8 | 5.657s | 5.393s | 27/67 | 57/67 |

Every department response meets 15s in these fake-model runs. No configuration
meets every target. The uvloop runtime does not expose its implicit default
executor, so the first report's actual default width remains unknown. The
explicit widths were installed with `ThreadPoolExecutor(max_workers=...)`;
these sweep reports retain the requested width but their optional effective
introspection field is null. Later fixture versions record the owned executor's
configured width directly instead of relying on uvloop's private attributes.

`round2-thread1-profile-stages-200-mock.json` uses explicit stages without global
cProfile and finishes in 3.614s, close to the clean 3.585s case. It records:

* Context assembly: 0.757s aggregate thread CPU, 2.601s summed service elapsed,
  0.0343s p95 service elapsed, 2.445s p95 default-executor queue delay.
* Patient locator: 0.280s CPU. Census refresh: 0.063s CPU, with 199 exact cache
  hits and one miss. The earlier globally profiled 2.31s census CPU was heavily
  distorted by instrumentation and must not be treated as its clean workload cost.
* Sensitivity classification: 0.570s CPU and 0.378s p95 DB-executor queue delay.
* Durable history save: 0.025s p95 DB queue delay; provenance: 0.051s;
  post-request audit: 0.397s. These are separate stages and some overlap.

The next production comparison must use a dedicated retrieval executor rather
than the global default-executor fixture override. These runs do not establish
performance for unrelated `to_thread` workloads, encrypted production storage,
real guideline models, real inference or simultaneous transcription/imaging.

## Installed full-retrieval result

`round3-full-rag-200-mock.json` uses the production dedicated retrieval executor
at width one, current sensitivity optimisations, real BGE-M3 and the real
BGE-reranker-v2-m3, with fake LLM compute. Startup verified all 19 seeded corpus
vectors are nonzero 1024-dimensional embeddings, plus a separate embedding and
cross-encoder probe. Config hashes, installed snapshot revisions and corpus
fingerprint are retained without local model paths or source contents.

The 200 simultaneous requests produced **185 complete responses and 15 whole-call
timeouts at 30s**. Only **3/67 general, 8/67 patient and 30/66 department** requests
met completed-answer limits. Passing chart-only timings would not have predicted
this result, so they must not substitute for full retrieval qualification.

The measured bottleneck is the serial cross-encoder stage: 124 embedding calls
completed in 2.55s aggregate elapsed; 124 rerank calls started and 123 completed
in 25.56s aggregate elapsed before the result snapshot. Their 1,476 returned
candidates used the cross-encoder, with zero lexical downgrades or reported
worker errors. One physical retrieval remained active after its caller timed
out and correctly retained admission until physical completion; 14 queued jobs
were cancelled. The fixture was then shut down by its owner. Partial RAG work
is not counted as a completed answer, regardless of the model-integrity checks.

`full-rag-200-memory-guard.json` records 44.23s total including startup/cleanup,
22 pressure samples, 75% initial and 74% final OS free estimate, and a minimum
67%. No guard threshold triggered. These are system-wide OS counters, not a
measurement of isolated model RSS or GPU high-water usage. The guard cancels
and awaits the capacity runner's owned-resource cleanup; it never stops an
existing Ollama daemon.

Reproduce only with installed models and no competing CPU/model qualification:

```sh
server/.venv/bin/python scripts/qualify_retrieval_capacity.py --output /tmp/full-rag-memory.json -- --orb-stub --fixture-settings docs/perf/capacity-2026-10-21/mock-pool-settings.json --embedding-model /absolute/installed/bge-m3/snapshot --reranker-model /absolute/installed/bge-reranker-v2-m3/snapshot --requests 200 --concurrency 200 --timeout 30 --output /tmp/full-rag-200.json
```

The wrapper requires at least 12 GiB estimated available and 20% OS free before
starting, samples every two seconds, and cancels on two successive samples below
15% free, over 512 MiB new swapouts, over 4 GiB compressor growth, or 300s total.
Optional full-retrieval initialization is bounded at 180s. Missing models,
empty/zero-vector corpora and unavailable cross-encoder probes fail startup;
they cannot silently become a faster lexical qualification. Aggregate runtime
embedding/rerank successes, errors and returned ranking methods are reported.

## Reading timings

### Full retrieval after batch-16 qualification

The clean `round4-full-rag-width{1,4}-200-mock.json` reports each used the same
frozen application source, installed BGE-M3 embeddings, unchanged FP16
BGE-reranker-v2-m3 weights and 19-document corpus. Only the explicit
`ORB_SAGE_RETRIEVAL_WORKERS` setting differed. The mocked LLM had 200 slots;
these results isolate retrieval and application overhead, not real generation.

| Retrieval workers | HTTP completions | Burst wall | General <3s | Patient <5s | Department <15s |
| --- | --- | --- | --- | --- | --- |
| 1 | 200/200 | 27.943s | 6/67 | 9/67 | 36/66 |
| 4 | 200/200 | 26.821s | 7/67 | 12/67 | 38/66 |

Neither first-readable nor full-answer acceptance passed. Each run completed
134 embedding calls and 134 cross-encoder calls, yielding 1,608 cross-encoder
results with zero model errors or lexical downgrades. Department census paths
do not all invoke clinical knowledge retrieval, which explains the count below
200. Both physical executors drained to zero outstanding work. Width 1's
aggregate reranker elapsed time was 23.914s; width 4's 62.665s includes overlapping
waiting for the serialized knowledge worker and cannot be treated as GPU compute.

Width 4 finished about 4% sooner in this single comparison. That is evidence
against declaring the chart-only winner, width 1, optimal for full retrieval.
Use explicit width 4 for the next full-retrieval real-model qualification; this
single comparison does not establish a universal production optimum or meet the
latency target. A separate run is required after any source or model change.

The memory guards finished normally in 40.80s and 36.63s including startup and
cleanup; their minimum observed OS free estimates were 66% and 67%. No competing
Ollama benchmark ran. The fixture settings are saved alongside the reports, and
all copied-source fingerprints match between the two runs.

`full_s` and the existing acceptance counts still measure HTTP stream closure.
The additional `terminal_completed_answer` distribution measures receipt of the
successful terminal marker. `post_terminal_stream_close` measures the interval
between that marker and closure; it can include durable history, provenance,
shadow and audit work. Both distributions require an otherwise successful
response. Errors, rejections and missing terminals stay in SLA denominators.
The new distributions do not relax the acceptance rule.

Explicit synchronous-stage `cpu_sum_s` uses the executing thread's OS CPU clock.
It includes child calls and profiler overhead. Outer context CPU includes
locator/census CPU; those numbers must not be added together. Thread totals can
exceed elapsed wall time when native operations run concurrently. Comparing CPU
from an instrumented run with elapsed time from a different clean run is invalid.

The global Python 3.12 cProfile collection supplies aggregate code-object call
counts, without arguments or local variables. Its elapsed durations span
interleaved threads and can show inconsistent inclusive/exclusive attribution;
do not sum them or use them as an exclusive CPU budget. Filesystem calls may
also arise from profiler label normalization, so they do not independently prove
a production path-resolution bottleneck. The stage-only mode avoids the global
function profiler and is preferable for measuring candidate stage improvements.

## Controlled copied-fixture experiments

```sh
server/.venv/bin/python scripts/loadtest_capacity.py --orb-stub --fixture-settings docs/perf/capacity-2026-10-21/mock-pool-settings.json --profile-fixture --profile-stages-only --requests 200 --concurrency 200 --timeout 60 --output /tmp/orb-stage-only-200.json
server/.venv/bin/python scripts/loadtest_capacity.py --orb-stub --fixture-settings docs/perf/capacity-2026-10-21/mock-pool-settings.json --fixture-thread-workers 4 --requests 200 --concurrency 200 --timeout 30 --output /tmp/orb-thread4-200.json
```

`--fixture-thread-workers` supports 1, 4, 8, 16 and 32. It changes only the
owned copied process's asyncio default executor. The separate production DB
executor is unaffected. This is not solely a retrieval setting: any call using
`asyncio.to_thread` in that process uses the same default executor. The report
records the requested override and observed effective executor width. A missing
override does not mean 32 workers; Python's platform/runtime default applies.

Run candidate widths sequentially, with no simultaneous model/CPU benchmark.
Repeat promising widths uninstrumented before selecting a production setting;
repeat a fixed candidate later to detect changing host load. Profiles are
diagnostic and always marked as unsuitable for latency qualification. Changing
thread count cannot establish real model throughput or justify a clinical
quality tradeoff.

The profiler records fixed module/function labels, counters, cache hit/miss
totals and timings. It never serializes SQL, cache keys, query/answer text,
arguments, local variables, credentials or patient identifiers. The profiling
control route exists only in the disposable fixture, requires its random nonce,
and is not added to deployed Orb.
