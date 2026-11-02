# Offline Orb workflow and serving benchmark — 22 October 2026

This is an engineering benchmark on the current Apple M2 Max, 64 GiB. It does not establish clinical equivalence or validate a hardware purchase.

The targets are complete valid answers, including queueing, retrieval and generation: general <3 s, patient <5 s, department <15 s. First content and first readable text are separate observations. A transport completion can still fail its patient-summary task checks.

## Final selected runs

MLX uses four internal decode slots; native Ollama uses two. Both cohorts issue 200 simultaneous HTTP requests. Internal batching changes neither the full-context requirement nor the 3/5/15-second targets.

The final MLX run uses four physical decode slots and the native run uses two. Both bursts launch 200 simultaneous HTTP requests. The separately labelled bounded MLX run reduces reserved KV from 8,858,370,048 to 4,429,185,024 bytes while preserving full per-request context and every request/quality target. This is not a comparison at identical engine widths.

| Engine / lane | Full response seconds | Transport complete | Automated summary checks | Automated completion/task pass | Meets target |
|---|---:|---|---|---|---|
| MLX / general | 23.91 | True | Not task-scored | True | False |
| MLX / patient | 27.44 | True | 12/12 | True | False |
| MLX / department | 21.80 | True | Not task-scored | True | False |
| Original native / general | 18.61 | True | Not task-scored | True | False |
| Original native / patient | 26.36 | True | 12/12 | True | False |
| Original native / department | 23.09 | True | Not task-scored | True | False |

Each sequential cell is one observation, not a p95 or a representative production latency distribution.

| Engine | Run | Fidelity cases | Burst observed / planned | Complete valid answers | Burst failures |
|---|---|---|---|---:|---|
| MLX | [run-k-mlx-bounded](run-k-mlx-bounded/mlx-p4-full-rag/report.json) | 8/8 | 200/200 | 0 | {"capacity_rejected": 50, "timeout": 150} |
| Original native | [run-j-original-reclaim](run-j-original-reclaim/ollama-p2-full-rag/report.json) | 8/8 | 200/200 | 0 | {"capacity_rejected": 48, "timeout": 152} |

- MLX, general: 0/67 complete valid answers within 3 s; successful-only p95: — s.
- MLX, patient: 0/67 complete valid answers within 5 s; successful-only p95: — s.
- MLX, department: 0/66 complete valid answers within 15 s; successful-only p95: — s.
- Original native, general: 0/67 complete valid answers within 3 s; successful-only p95: — s.
- Original native, patient: 0/67 complete valid answers within 5 s; successful-only p95: — s.
- Original native, department: 0/66 complete valid answers within 15 s; successful-only p95: — s.

An absent or aborted burst is not a 0/200 measurement. When zero answers succeed, successful-only p50/p95/p99 are null. Timeouts, capacity rejections and semantic failures remain in the denominator.

## Quality and comparison limits

A manual engineering review found that the selected native J patient reply passes all 12 lexical fixture checks while making an unsupported causal inference: "No active medications are listed, so there is no change history to report." An empty active list does not prove absence of medication-change history. Its unresolved-task wording also uses missing Scribe notes as a proxy for a task ledger. Full source grounding remains **unqualified**; automated transport/task success must not be read as clinical correctness. The selected K MLX reply also passes all 12 lexical checks but states "No recent imaging has been performed." The source only says imaging is unavailable; it does not establish that no procedure occurred. K uses a cautious unknown for medication history, but that does not validate an active-list proxy. Both final summaries remain unqualified for full source grounding. The JSON preserves the actual snippets and review, without altering raw reports or checker thresholds.

The eight fidelity fixtures check synthetic factual extraction, units, medication status, negation, identity, missingness, chronology and a quoted instruction. They do not establish clinical safety, prescribing accuracy or noninferiority. The additional patient workflow checks require the expected identity/diagnosis and explicit coverage of missing observations, active medications, medication-change history, labs and unresolved tasks, without fabricated clearance, unsupported stability or a chart-update refusal. The JSON records the actual check count and failures for each reply.

Both routes retain the installed MedGemma 27B checkpoint lineage, complete prompts, request output budgets, 8192 global context and F16 KV. The MLX candidate is a separately converted quantization with repaired activation precision; it is not identical GGUF weights and remains clinically unqualified. The native route uses the original pinned GGUF. All inference is local and offline.

The selected runs share the final source blueprint, but disposable chart identifiers, timestamps and stochastic outputs differ. This is not an exact-body paired engine comparison. No clinical noninferiority or engine speedup can be concluded from these few sequential samples.

## Changes exercised

- Sage distinguishes reading a patient summary from requesting a chart mutation. Its summary prompt explicitly asks for documented diagnosis and missingness across the requested domains. Success is judged from actual output, not merely HTTP/SSE completion.
- Retrieval reuses only exact model outputs with bounded keys, lifetimes and memory; it does not merge distinct clinical generations or strip benchmark nonces. Torch releases unused MPS allocator storage after CPU results are available. Live weights and numeric outputs remain intact.
- Request-scoped cancellation closes blocked streams and releases the originating worker lease; MLX bounds FIFO admissions so decoding gets a turn during bursts. Cancellation is cooperative, not GPU-kernel preemption.
- Native compact SWA preserves 8192 global attention and the model's local-window behavior. The observed two-slot active KV falls from 7.75 GiB with full SWA storage to approximately 2.47 GiB with compact storage. Native RAM prompt archiving is disabled in prefix-cache-zero runs, separately from active generation KV. The JSON records actual native log confirmation.

Embedding microbatching remains disabled. Its [offline qualification](embedding-batch4-qualification.json) preserved token IDs and dense top-15 order but exceeded the preset vector/cosine tolerances. Those thresholds were not relaxed for a speed improvement; final retrieval uses width one. This numerical gate is separate from clinical validation.

## Retrieval, cleanup and memory

- MLX: observed retrieval invocations embedding 44/44, reranking 44/44; lexical fallback results 0; outstanding retrieval jobs 0.
- MLX: exact-output hits 0, coalesced waits 0, underlying unique worker submissions 91, multi-request embedding batches 0 (width one retained). Invocation counters include reused results; completed underlying calls do not mean every HTTP request was answered.
- MLX: maximum observed system compression growth 515670016 bytes; swap-out growth 0 bytes. These are system-wide observations, not process/GPU peaks.
- MLX: MPS unused-cache release events 45; cumulative released bytes 49794777088. This sum includes repeated reuse/release of storage and is not peak RAM saved or extra hardware capacity.
- MLX, after sequential: cleanup observed for 0.02 s, settled=True; this observation occurs after request deadlines and is excluded from latency.
- MLX, after burst: cleanup observed for 1.59 s, settled=True; this observation occurs after request deadlines and is excluded from latency.
- Original native: observed retrieval invocations embedding 41/41, reranking 41/41; lexical fallback results 0; outstanding retrieval jobs 0.
- Original native: exact-output hits 0, coalesced waits 0, underlying unique worker submissions 85, multi-request embedding batches 0 (width one retained). Invocation counters include reused results; completed underlying calls do not mean every HTTP request was answered.
- Original native: maximum observed system compression growth 370950144 bytes; swap-out growth 0 bytes. These are system-wide observations, not process/GPU peaks.
- Original native: MPS unused-cache release events 42; cumulative released bytes 46472888320. This sum includes repeated reuse/release of storage and is not peak RAM saved or extra hardware capacity.
- Original native, after sequential: cleanup observed for 0.01 s, settled=True; this observation occurs after request deadlines and is excluded from latency.
- Original native, after burst: cleanup observed for 2.13 s, settled=True; this observation occurs after request deadlines and is excluded from latency.

Memory and quality guards are retained. A completed benchmark procedure is not a passed capacity qualification; final acceptance flags and request outcomes decide that.

## All attempts retained

| Attempt | Status | Fidelity | Burst observed / planned | Valid burst answers | Failure / abort | Restored original models |
|---|---|---|---|---:|---|---|
| [run-a](run-a/mlx-p8-full-rag/report.json) | aborted | 0/not run | 0/not run | not run | compression_growth_over_4_gib | restored_and_verified |
| [run-b](run-b/mlx-p8-full-rag/report.json) | complete | 8/8 | 200/200 | 0 | {"capacity_rejected": 52, "timeout": 148} | restored_and_verified |
| [run-c-original](run-c-original/ollama-p2-full-rag/report.json) | aborted | 0/not run | 0/not run | not run | compression_growth_over_4_gib | restored_and_verified |
| [run-d-mlx](run-d-mlx/mlx-p8-full-rag/report.json) | complete | 8/8 | 200/200 | 0 | {"capacity_rejected": 51, "timeout": 149} | restored_and_verified |
| [run-e-original-compact](run-e-original-compact/ollama-p2-full-rag/report.json) | aborted | 8/8 | 0/not run | not run | compression_growth_over_4_gib | restored_and_verified |
| [run-f-original-cache-release](run-f-original-cache-release/ollama-p2-full-rag/report.json) | aborted | 8/8 | 0/200 | 0 | compression_growth_over_4_gib | restored_and_verified |
| [run-g-original-cache0](run-g-original-cache0/ollama-p2-full-rag/report.json) | complete | 8/8 | 200/200 | 0 | {"capacity_rejected": 46, "timeout": 154} | restored_and_verified |
| [run-h-mlx-complete-summary](run-h-mlx-complete-summary/mlx-p8-full-rag/report.json) | not_run_memory_preflight | 0/not run | 0/not run | not run | Memory admission: 48103633715 available / 49811936912 required bytes | restored_and_verified |
| [run-i-original-complete-summary](run-i-original-complete-summary/ollama-p2-full-rag/report.json) | aborted | 0/8 | 0/not run | not run | compression_growth_over_4_gib | restored_and_verified |
| [run-j-original-reclaim](run-j-original-reclaim/ollama-p2-full-rag/report.json) | complete | 8/8 | 200/200 | 0 | {"capacity_rejected": 48, "timeout": 152} | restored_and_verified |
| [run-k-mlx-bounded](run-k-mlx-bounded/mlx-p4-full-rag/report.json) | complete | 8/8 | 200/200 | 0 | {"capacity_rejected": 50, "timeout": 150} | restored_and_verified |

- run-i-original-complete-summary: extraction-check errors {"HTTPStatusError": 8}; 0 completed cases. Interrupted/failed transport is not an observed incorrect clinical answer.

Incomplete or interrupted replies receive no post-hoc success credit, even if partial text appears useful. A resource- or transport-interrupted extraction check is not evidence that the model generated an incorrect clinical fact. Earlier attempts used earlier summary prompts/checkers. Their weaker task passes do not qualify the final summary contract. Guard-aborted startup attempts provide no timing or cohort result. All pinned-model restoration results are listed separately in the JSON.

## Source evidence and hardware conclusion

- [sources-final](sources-final/manifest.json): 39 captured sources; snapshot hash mismatches 0.
- [sources-final-bounded-mlx](sources-final-bounded-mlx/manifest.json): 64 captured sources; snapshot hash mismatches 0.
- [sources-final-cache0](sources-final-cache0/manifest.json): 64 captured sources; snapshot hash mismatches 0.
- [sources-final-compact-native](sources-final-compact-native/manifest.json): 39 captured sources; snapshot hash mismatches 0.
- [sources-final-complete-summary](sources-final-complete-summary/manifest.json): 64 captured sources; snapshot hash mismatches 0.
- [sources-qualified-rerun](sources-qualified-rerun/manifest.json): 64 captured sources; snapshot hash mismatches 0.
- [sources-run-a](sources-run-a/manifest.json): 39 captured sources; snapshot hash mismatches 0.

Recorded CPU verification groups: 202 summary-related tests, 24 disconnect tests, 72 knowledge-cache tests, 130 native-control tests and 39 final lease tests. These groups overlap, are not additive, and are engineering checks rather than clinical cases.

The final snapshot contains 64 captured source files; this is a reproducibility snapshot, not a copy of the entire repository. Per-run source coverage is recorded in the JSON.

No tested configuration has demonstrated 200 fresh clinical-model requests within the 3/5/15-second complete-answer targets. The present M2 Max results cannot specify how many Ultra Macs are needed or validate a Mac Studio purchase. A clean target-hardware test, broader clinically reviewed task evaluation and the actual monitoring/ASR/imaging/summary-refresh workload remain necessary before claiming a hospital SLA.

[Machine-readable derived results](results-summary.json)
