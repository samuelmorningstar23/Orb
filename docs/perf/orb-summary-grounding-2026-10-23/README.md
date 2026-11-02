# Orb source-grounded summaries and offline serving — 23 October 2026

The source-grounding repair is implemented and CPU-tested. **Real-model source-contract and 200-request validation remain blocked by host memory pressure.** Across six preserved attempts, no sequential workload or 200-request burst began. The final native attempt reached retrieval readiness and started the fidelity suite, then the unchanged compression guard interrupted it. This is not a 0/200 completion measurement.

These are engineering results on the current Apple M2 Max, 64 GiB. They do not validate clinical reasoning, clinical equivalence, a hospital SLA or a hardware purchase.

## What changed

Patient summaries now use the `source-facts-v1` extractive contract. The model orders a supplied fact ledger; it must return every source fact ID exactly once and cannot add clinical prose. The server renders exact source excerpts, references, scope and explicit unknowns. Missing imaging documentation remains unknown rather than becoming an assertion that imaging was not performed; an empty current-medication list does not establish an empty change history. This is a change to the summary workflow, not evidence that arbitrary model-generated clinical prose is now reliable.

The checker uses a separately captured authorized source bundle, not the response's own claims as its expected answer. It verifies patient scope, one proof, contract version, exact ID coverage, source fingerprint/references/metadata, exact rendered output, unknowns and scope. Failed, omitted, duplicated or foreign selections fail closed. The actual number and outcome of these checks are recorded below and in JSON.

Exact preservation proves that output matches the supplied records under this contract. It does not prove the chart is correct or complete, identify the most important clinical fact, establish clinical noninferiority or replace clinician review. The current source window does not retrieve a complete medication-change history or formal outstanding-task ledger; those domains remain explicitly unknown.

## Workload and comparison limits

The disposable fixture has **one populated chart and 99 empty charts**. This differs from the 22 October empty-chart fixture, so elapsed-time differences from its J/K runs cannot be presented as an engine speedup. The 200 fresh-request plan and complete-answer targets remain unchanged: 67 general requests at <3 s, 67 patient requests at <5 s and 66 department requests at <15 s. Queueing and retrieval remain inside request latency. First content/readable text are separate milestones.

The native route uses the unchanged installed MedGemma 27B GGUF. The separately labelled MLX conversion shares its source checkpoint lineage but is not identical quantized weights. Inputs, output budgets, 8192 global context, full local-window behavior, F16 KV and resource/quality guards are retained. All inference is local and offline. Exact fact output construction is server code; the model still performs the fact-selection/order request.

Reported admission widths are bounded application/serving configurations, not a claim that 200 model generations run concurrently. An already-resident native service retains its existing engine slots and cache/batch settings; the JSON records its independent identity/idle/configuration attestation. Its new-allocation admission policy requires 12 GiB extra headroom for retrieval because the weights are already allocated, while the same 4 GiB compression and 512 MiB swap growth guards remain active. It owns only the passthrough proxy, never terminates the original engine, and restores original indefinite residency afterward. This resident-service mode is a distinct allocation/configuration experiment, not an equal-configuration engine comparison. The planned bursts launch 200 simultaneous HTTP requests and retain all original targets; none was reached in these attempts. The runs share the frozen source blueprint, while fixture nonces, timestamps, sampling and physical decode widths can differ. They are not an exact-body paired engine comparison.

The last resident-service attempt serializes installed Hugging Face parameter materialization with `HF_DEACTIVATE_ASYNC_LOAD=1` before imports. This changes loading concurrency only; weights, dtype/casts, device placement and inference math remain unchanged. A tiny complete synthetic CPU checkpoint verified bitwise state, buffer and output equality across both loader paths. This does not prove a full-production-model memory saving: F still hit the same compression guard. E and F ran under different host memory states, so their elapsed times or pressure cannot establish a serial-loader speedup or reduction in peak memory. See [installed-source loader audit](loader-audit.json).

## Selected sequential observations

| Engine / lane | Full seconds | Transport complete | Exact source checks | Automated complete/task pass | Meets target |
|---|---:|---|---|---|---|
| Original native: aborted | — | No sequential result | — | Not demonstrated | Not measured |
| MLX: aborted | — | No sequential result | — | Not demonstrated | Not measured |

No sequential observations were collected; latency distributions are unavailable. Automated source-contract success is not clinical validation.

## 200-request results

| Engine | Report | Fidelity checks | Observed / planned | Completed valid answers | Errors |
|---|---|---|---|---:|---|
| Original native | [run-f-resident-serial-load](run-f-resident-serial-load/report.json) | 0/8 completed and valid; 2 observed | 0/not run | not run | {} |
| MLX | [run-d-mlx](run-d-mlx/mlx-p4-full-rag/report.json) | Not run | 0/not run | not run | {} |


An unobserved or aborted cohort is not a 0/200 completion measurement. Timeouts, rejections and failed source-contract responses remain failures. No cohort was run, so p50/p95/p99 are unavailable. The separate eight-case fidelity suite checks narrow synthetic extraction; it does not establish clinical quality.

## Retrieval, memory and cleanup

- Original native: bounded admission width 1; embedding invocations unavailable/unavailable, reranking unavailable/unavailable, lexical fallback results unavailable.
- Original native: exact-output hits unavailable, coalesced waits unavailable, underlying worker submissions unavailable, physical embedding batches unavailable. Invocation counters can include reused outputs; completed underlying work is not equivalent to every HTTP request being answered.
- Original native: system compression growth 5091524608 bytes, swap-out growth 0 bytes. These are system-wide observations, not process/GPU peaks.
- Original native: MPS unused-cache events 1, cumulative released bytes 1073741824. The sum counts repeated reuse/release and is not peak RAM saved or additional capacity.
- MLX: bounded admission width 4; embedding invocations unavailable/unavailable, reranking unavailable/unavailable, lexical fallback results unavailable.
- MLX: exact-output hits unavailable, coalesced waits unavailable, underlying worker submissions unavailable, physical embedding batches unavailable. Invocation counters can include reused outputs; completed underlying work is not equivalent to every HTTP request being answered.
- MLX: system compression growth 17085333504 bytes, swap-out growth 1114112 bytes. These are system-wide observations, not process/GPU peaks.
- MLX: MPS unused-cache events unavailable, cumulative released bytes unavailable. The sum counts repeated reuse/release and is not peak RAM saved or additional capacity.

F reached full retrieval readiness: the saved startup proof identifies the installed BGE embedding/reranking architectures, 19 nonzero corpus vectors, a nonzero embedding probe and a cross-encoder probe. This proves startup availability over the synthetic corpus, not completion of a clinical or 200-request workload. Its startup metadata records three underlying worker submissions and zero workload retrieval invocations at that point; final workload counters are unavailable because the fidelity phase was interrupted.

## Every saved attempt

| Attempt | Status | Fidelity | Burst observed / planned | Valid answers | Failure / skip | Restoration |
|---|---|---|---|---:|---|---|
| [run-a-native](run-a-native/ollama-p2-full-rag/report.json) | aborted | Not run | 0/not run | not run | compression_growth_over_4_gib | restored_and_verified |
| [run-b-native-retry](run-b-native-retry/ollama-p2-full-rag/report.json) | aborted | Not run | 0/not run | not run | Owned candidate memory refusal: 47416438947 available / 47461982592 required bytes | restored_and_verified |
| [run-c-native-p1](run-c-native-p1/ollama-p1-full-rag/report.json) | aborted | Not run | 0/not run | not run | compression_growth_over_4_gib | restored_and_verified |
| [run-d-mlx](run-d-mlx/mlx-p4-full-rag/report.json) | aborted | Not run | 0/not run | not run | compression_growth_over_4_gib | restored_and_verified |
| [run-e-resident-original](run-e-resident-original/report.json) | aborted | Not run | 0/not run | not run | compression_growth_over_4_gib | restored_and_verified |
| [run-f-resident-serial-load](run-f-resident-serial-load/report.json) | aborted | 0/8 completed and valid; 2 observed | 0/not run | not run | compression_growth_over_4_gib | restored_and_verified |

The final native attempt completed a one-token warmup in 1.624 s; this is not an answer, quality result or target pass. Its eight-case fidelity phase recorded only two incomplete error rows: `RemoteProtocolError` after guard-driven proxy closure, then `ConnectError`. Neither completed or received a valid factual score, and six cases were not observed. These are interrupted transport calls, not evidence of incorrect clinical facts. No fresh patient source-contract response, sequential lane or burst was measured. Partial text receives no post-hoc success credit. Failed startup attempts provide no cohort measurement. Each original-model restoration and all source/report hashes remain in JSON.

## Reproducibility and limits

- [sources](sources/manifest.json): 68 captured files; snapshot hash mismatches 0.
- [sources-c-onward](sources-c-onward/manifest.json): 68 captured files; snapshot hash mismatches 0.
- [sources-e](sources-e/manifest.json): 70 captured files; snapshot hash mismatches 0.
- [sources-f](sources-f/manifest.json): 71 captured files; snapshot hash mismatches 0.

The core frozen snapshot contains 68 files, the resident-harness snapshot 70, and final serial-loader snapshot 71. These are reproducibility records, not the entire repository. The [verification registry](verification.json) records 906 tests across disjoint core/lease file batches, eight resident-harness safety tests, and a 75-test worker/serial-loader batch. The batches are reported separately; no unverified combined total or clinical-case count is claimed. Earlier overlapping subgroups are not added again. [Fake-model smoke checks](fake-model-smoke.json) completed six HTTP requests and two patient source-contract checks, but cannot qualify real-model throughput. [The source contract](source-contract.md) describes the behavior and explicit coverage limits. [Final source/restoration integrity](final-integrity.json) verifies all 71 final files against the workspace and confirms all six restorations; both resident-mode proxies exited.

No engineering or lexical count awards clinical equivalence. A successful extractive contract demonstrates exact source preservation within its authorized supplied record only. No Mac Studio/Ultra configuration, hospital workload capacity or 200-request SLA is established by this source-control change alone. Monitoring, transcription, imaging and ongoing summary-refresh throughput are not tested in these cohorts.

[Machine-readable results](results-summary.json)
