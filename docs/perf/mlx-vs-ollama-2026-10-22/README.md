# Offline MLX versus Ollama: 2026-10-22 evidence

The repaired MLX candidate passes all eight unchanged synthetic factual-extraction checks, up from 2/8. The defect was FP16 residual saturation at 65504 beginning in block 8. Mixed arithmetic keeps the embedding and residual stream in F32 while retaining F16 projection inputs and KV storage. After bounding allocation cache, reducing prefill chunks and disabling reusable prefix storage, the full Orb resource test completed without a memory abort: 0.724 GB compression growth and no new swap. Its 200-request burst still completed zero answers within 20 seconds. Both engines also completed zero of 200 requests in the separate engine-only comparison.

This is an experiment on the current Apple M2 Max with 64 GiB unified memory. It does not establish a hospital hardware configuration, clinical equivalence or the 200-request 3/5/15-second targets.

## Model lineage and controls

- Native source: installed MedGemma 27B GGUF, SHA256 `4b97da6b21a8fb1405a0d1efee462bfd6c4d3c46307bac8b34373835f07ca758`; the Ollama manifest digest is a separate identity.
- Pinned serving software: Ollama 0.40.4 and MLX-LM 0.32.2. Model conversion and inference use existing local weights; runtime network access is restricted to owned loopback services.
- Scope: text-only Sage workloads. Conversion excludes the vision/projector tensors; imaging, transcription, continuous monitoring and 30-minute patient-summary refresh workloads are not evaluated here.
- The eight synthetic fidelity cases retain 8192 context, 512 maximum output tokens, temperature 0, repeat penalty 1.15, last 256 history and the original stop policy. They test facts/schema, not treatment decisions or clinical accuracy.
- Exact Sage replay retains each captured request's own output budget and sampling settings. Engine-only replay excludes Orb's retrieval and application work.

## Candidate representations

| Candidate | Approximate weight storage | What changed from the GGUF |
| --- | ---: | --- |
| Source Q4 integer repack / Q6 dense FP16 | 22.37 GiB | Q4 integers retained with FP16 affine scale/bias parameters; Q6 source values stored densely in FP16. |
| Source Q4 integer repack / Q6 affine8 hybrid | 18.14 GiB | Uses the Q4 repack parent and source-derived affine8 Q6 parent files; all 435 text matrices remain quantized. |

Neither candidate is lossless. FP16 parameter rounding remains in the repack, and Q6 affine8 introduces requantization error. Composing the hybrid adds no fresh quantization or rounding; this does not remove the parents' errors. See the [hybrid composition manifest](composition-source-repack4-affine8-manifest.json).

## Completed memory and fidelity experiments

| Run | Observed outcome | Interpretation |
| --- | --- | --- |
| [Staged startup trace](source-repack4-f16-trace-a/mlx-p1/report.json) | Loading finished in 6.15 s; 24.019 GB active memory at exit. Later compression growth 8.289 GB triggered the guard before a quality answer. | Model construction/quantization did not create a phantom full FP32 weight copy. Successful loading alone did not establish usable serving memory. |
| [Norm dtype repair, dense Q6 candidate](source-repack4-f16-precision-a/mlx-p1/report.json) | 2/8 fidelity cases passed; 46.6 MB maximum compression growth, no swap growth. | Returning normalization output to its input dtype substantially reduced memory pressure, but quality remained unqualified. |
| [Same dense candidate, prefix cache disabled](source-repack4-f16-precision-cache0-a/mlx-p1/report.json) | Compression growth 7.328 GB; aborted before quality cases. | Disabling prefix reuse did not guarantee this candidate would fit the current host state. |
| [Hybrid, prefix cache disabled](source-repack4-affine8-precision-cache0-a/mlx-p1/report.json) | 2/8 fidelity cases passed; 196.8 MB compression growth, no swap growth. | The smaller representation allowed the checks to run without resolving the factual/schema failures. |
| [Hybrid, sequential serving and no prefix reuse](source-repack4-affine8-serial-cache0-a/mlx-p1/report.json) | 2/8 again; no compression or swap growth. | The failures persisted without continuous batching or cross-request prefix reuse. |

The resource monitor retains the existing compression/swap/availability abort conditions. Its pressure figures are system-wide, not model RSS or measured GPU peak. Admission estimates and generic headroom are not guarantees of physical free memory. Raw aborted reports remain unchanged.

The dense cache-disabled abort also left the original 4B resident absent. Its [lease report](source-repack4-f16-precision-cache0-a/lease-report.json) correctly records restoration failure. Both original 27B/4B models were subsequently restored and verified in a separate [recovery record](source-repack4-f16-precision-cache0-a/post-lease-recovery.json); the failure was not rewritten as a successful lease.

## Checks that narrowed the cause

- [Actual installed server tokenizer paths](actual-server-tokenizer-path-check.json): all eight fidelity inputs exactly match the native oracle IDs, and segmented prompts reconstruct the full input.
- [Fidelity plus Sage token comparison](exact-fidelity-and-sage-token-comparison.json): all 11 observed inputs match exactly. This establishes the recorded subset, not every possible future prompt.
- [Independent storage audit](independent-source-candidate-row-audit.json): 1305 sampled rows across 435 matrices and 373 norm tensors. Worst sampled matrix relative L2 error is 0.000820; reconstructed norm parameters match exactly. This samples three rows per matrix and does not prove complete tensor/runtime equivalence.
- [Native penalty observation](native-baseline-diagnostics-a/ollama-p1/native-penalty-analysis.json): changing repeat penalty 1.0 to 1.15 suppresses two prompt-tail token IDs before the first generated token, while 28 nonprompt relative-odds controls remain stable. Native does not simply ignore prompt history in this observed case.
- [Saved token-distribution comparison](token-distribution-serial-b/comparison-offline-analysis.json): the first nine greedy IDs match; at index 9 native emits a space and MLX's unique greedy argmax adds a quote, beginning the number-versus-string mismatch. Probability differences are already visible at token 0.

Distribution evidence is diagnostic. Native provides top 20 alternatives; pinned MLX permits top 11. MLX's outer logprob ID comes from an unsorted candidate list, so its greedy ID is inferred from the unique maximum. Probability stages and emitted precision differ; no complete-distribution distance or KL divergence is claimed. Raw responses were preserved when offline parsers were extended.

## Concrete numerical defect: FP16 residual clipping

The [62-block streamed Metal/NumPy probe](all62-blocks-metal-vs-source-f32.json) evaluates all blocks for eight recorded input tokens, using an independent original-GGUF float32 reference. It does not load the whole model at once, generate an answer or capture native Ollama intermediate states.

The separate [clipping analysis](all62-blocks-fp16-clipping-analysis.json) shows:

1. Before block 8's feed-forward residual addition, relative L2 error is 0.0004625.
2. At that addition, the source reference reaches 81246.92 while the MLX FP16 path reaches its 65504 limit; relative L2 error jumps to 0.19343.
3. At the final block residual, the reference reaches 846173.25 while the candidate remains capped at 65504; relative L2 error reaches 0.87789.
4. Later normalization stages also contain nonfinite candidate values that subsequent finite clipping can conceal. Checking only final residual finiteness would miss the defect.

This identifies a real numerical failure in the observed FP16 path. It does not establish that every quality failure has the same cause or that a proposed repair passes the clinical workload.

## Arithmetic and memory repair

The [F32 diagnostic policy](source-repack4-affine8-f32-policy.json) pins the scoped launcher/helper sources. Its embedding expression casts output to F32 and applies `sqrt(hidden_size)` directly in F32, preserving the residual stream without changing stored quantized weights, masks, layer order, sampling or task/context budgets. The [all-F32 attempt](f32-comparison-a/mlx-p1/report.json) passed 8/8 extraction checks but both first replay requests timed out at 60 seconds. Its third replay was interrupted by the old 300-second overall procedure budget; that interrupted request is not a complete department-latency measurement.

The final [mixed candidate policy](source-repack4-affine8-mixed-policy.json) retains F32 residuals and post-attention/post-FFN/final normalization. Only normalized projection inputs and normalized Q/K are cast to F16. It refuses composition with the earlier global norm-output dtype repair, which would reintroduce rounding in the residual additions. All 435 matrix targets remain quantized; no stored weight casting or replacement clinical model is involved.

Mixed KV budgeting uses two-byte elements: 1,107,296,256 bytes per active width 1 request for the existing 8192 global / 1024 local layer pattern, compared with 2,214,592,512 bytes in the F32-only diagnostic. These are estimates, not total-serving peaks. Final runs use a bounded 8 GiB prefix cache with at most 10 entries; the earlier F32 diagnostic disabled prefix reuse.

## Initial mixed comparison

The [machine-readable summary](comparison-summary.json) retains source-report hashes, failures and qualification limits. [Frozen runtime sources](sources-mixed-comparison-a/manifest.json) match all 23 recorded comparison-source hashes, with the parent residency lease saved separately. [Independent input/answer auditing](mixed-comparison-a/mlx-p1/independent-profile-audit.json) confirms all 11 actual MLX input token sequences match the original native oracle for the eight extraction cases and three captured Sage inputs.

| Engine / profile | Extraction checks | General full answer | Patient full answer | Department full answer |
| --- | ---: | ---: | ---: | ---: |
| [MLX, one decode slot](mixed-comparison-a/mlx-p1/report.json) | 8/8 | 31.65 s | 47.23 s | Timed out at 60 s |
| [Ollama, one decode slot](mixed-comparison-a/ollama-p1/report.json) | 8/8 | 32.95 s | 43.64 s | 40.45 s |

These are engine-only, exact-body replays of synthetic Orb inputs, excluding live retrieval and HTTP application work. Each row contains one observation per lane, not a latency distribution. Outputs have different lengths (MLX general/patient: 206/360 tokens; native: 174/295) and nonzero sampling has no fixed cross-engine seed. They do not establish an engine speedup or semantic noninferiority. First raw content is separately recorded and is not a complete or clinically usable answer.

| 200 simultaneous clients | Finished within 20 s | General under 3 s | Patient under 5 s | Department under 15 s | Failures |
| --- | ---: | ---: | ---: | ---: | --- |
| [MLX, eight decode slots](mixed-comparison-a/mlx-p8-200/report.json) | 0/200 | 0/67 | 0/67 | 0/66 | 187 timeouts; 13 HTTP 502 |
| [Ollama, two decode slots](mixed-comparison-a/ollama-p2-200/report.json) | 0/200 | 0/67 | 0/67 | 0/66 | 200 timeouts |

Both replay the same 200 captured request bodies, with unchanged contexts, output budgets and sampling. Decode widths differ: these are tested serving configurations, not an equal-width engine ranking. All failures remain in the target denominators. With zero completed responses, successful-response p50/p95/p99 values are null, not zero.

Engine-only MLX system compression growth was 1.784 GB at width 1 and 0.193 GB during the width 8 quality-plus-burst profile; neither produced new swap. The fresh native profiles produced no compression or swap growth. These system-wide observations depend on host state and are not isolated model-memory measurements.

The [initial full Orb RAG attempt](mixed-comparison-a/mlx-p8-full-rag/report.json) completed a one-token model warmup, then aborted while starting the disposable retrieval fixture, before any quality/sequential/burst phase. Maximum observed compression growth was 10,105,569,280 bytes; swap growth was zero. The unchanged guard (`compression_growth_over_4_gib`) stopped owned processes. Retrieval integrity was not verified, and this initial run supplies no end-to-end latency or clinical answer. Later resource rechecks below use separately recorded configurations.

The [initial residency lease](mixed-comparison-a/lease-report.json) verifies restoration of both original 27B/4B digests, contexts 8192/4096 and indefinite residency; all owned benchmark ports closed. The original GGUF files and production provider remain unchanged.

The extraction gate tolerates markdown fences and validates extracted facts/types; it is not strict raw JSON-format validation. Free-form Sage replay remains outside that gate: raw MLX output names sources absent from the input, and department output contains unsupported wording. The existing Sage parser filters unrecognized citation identifiers, so the raw metadata is not evidence of a user-visible fabricated citation. Both engines also exhibit format deviations. These observations require representative clinical review rather than treating 8/8 as a clinical quality guarantee.

## Final serving and memory rechecks

The final bounded configuration retains the same converted checkpoint, arithmetic, F16 KV, full 8192 context, sampling and output budgets. It changes serving resource policy:

- An owned HTTP listener backlog of 256 replaces the installed backlog of five; the earlier 13 early HTTP 502 responses disappeared in subsequent 200-client engine tests.
- The real BGE-M3 embedder and BGE cross-encoder initialize before the eager 27B engine, and remain resident for measured queries. This avoids overlapping cold checkpoint-load peaks.
- Prefill processes 256 tokens per chunk instead of 512. All input tokens remain; this is not context truncation.
- Unused MLX allocation cache is capped at 1 GiB. This is distinct from active model/KV memory and the reusable prefix cache; it is not a total RAM cap.
- The final full-Orb run disables reusable prefix entries. Active requests still receive their complete prompts and full cache windows. All 24 recorded prompt-cache observations show zero sequences / 0.00 GB.

The [bounded engine policy](source-repack4-affine8-mixed-bounded-policy.json) retains the 10-entry / 8 GiB prefix bound. The separately labelled [full-Orb cache-disabled policy](source-repack4-affine8-mixed-bounded-cache0-policy.json) sets its entry count to zero. Recomputing prefixes can change timings and numerical outputs; no bit-identical-output or controlled speedup claim follows from this policy change.

| Latest engine-only 200-client configuration | Extraction gate | Finished within 20 s | General / patient / department target passes |
| --- | --- | ---: | --- |
| [MLX, eight decode slots, bounded allocation cache](mixed-bounded-recheck-a/mlx-p8-200/report.json) | 8/8 | 0/200; 200 timeouts | 0/67; 0/67; 0/66 |
| [Ollama, two decode slots](mixed-bounded-recheck-a/ollama-p2-200-after-reclaim/report.json) | 7/8; long-record case timed out at 60 s | 0/200; 200 timeouts | 0/67; 0/67; 0/66 |

The native timeout ended in incomplete JSON; it was not a demonstrated wrong fact in a complete answer. It still fails the gate and remains in the results. The earlier one-slot native run passed 8/8. These latest wider configurations therefore do not establish matched clinical quality or an engine ranking. They preserve the same 200 captured bodies, and neither demonstrates the requested service targets. The [first native attempt](mixed-bounded-recheck-a/ollama-p2-200/report.json) was skipped for memory headroom; a separately recorded retry after five seconds used an unchanged admission threshold and passed preflight. MLX added no compression or swap in this engine-only window; native compression grew 2.662 GB with no new swap.

The [reordered startup attempt](mixed-serving-recheck-a/mlx-p8-full-rag/report.json) reached six completed extraction checks before its guard abort. The [bounded-cache attempt with prefix reuse](mixed-bounded-recheck-a/mlx-p8-full-rag/report.json) passed 8/8 and exercised three real Orb queries, then hit the guard at 5.199 GB compression growth; its 200-request phase returned no observed results and is not a completed capacity measurement.

The [final full Orb run with prefix reuse disabled](mixed-bounded-cache0-rag-a/mlx-p8-full-rag/report.json) completed all planned phases without a memory abort. It passed 8/8 extraction checks with both retrieval models resident. The real application results were:

| Lane | Sequential complete HTTP answer | First readable observation | Target |
| --- | ---: | ---: | ---: |
| General | 14.94 s | 5.69 s | 3 s |
| Patient | 27.71 s | 27.71 s | 5 s |
| Department | Timed out at 60 s | 16.20 s in its partial stream | 15 s |

These are single observations, not representative p95 values. First readable is a character/word heuristic, not a clinical usefulness judgement. The subsequent burst observed all 200 results: **0 successful answers, 152 timeouts and 48 capacity rejections**. SSE errors/rejections carried HTTP 200 but correctly remained failures. Each lane had zero completed answers meeting its target; successful-response quantiles are null.

Transport completion is not task correctness. The 27.71-second patient reply misinterpreted the summary request as a chart-update request and returned a modification refusal; it is not a validated patient summary. The incomplete department reply also placed unscored patients under a stable-patients heading despite missing clinical data. These failures remain visible and prevent a claim that clinical workflow quality is established by the extraction gate.

Maximum compression growth was 723,566,592 bytes, with zero swap growth. These are system-wide window deltas, not an isolated GPU/RSS peak, and host baselines differ between runs. This is evidence that the complete bounded resource test ran on this host; it does not establish a maximum supported concurrency or a memory guarantee for other workloads.

Actual BGE embedding/reranking was exercised with no recorded lexical fallback. At the final deadline, two embedding and two reranking calls were still outstanding (47/45 and 45/43 completed), so `full_retrieval_integrity_verified` correctly remains false. Completion of the benchmark procedure must not be confused with completed clinical requests or retrieval qualification.

The [final lease](mixed-bounded-cache0-rag-a/lease-report.json) verifies both original models restored with their original digests, contexts and indefinite residency; owned benchmark ports closed. [Final verification](verification-final.json) records 501 focused CPU engineering tests passing, targeted Ruff checks passing, Mypy passing for 11 runtime sources and all [31 frozen runtime/policy sources](sources-mixed-bounded-cache0-rag-a/manifest.json) matching the saved snapshot. Three additional pre-existing Mypy findings in the broader qualifier are separately recorded and reproduced in its original snapshot.

## Existing native baseline and interpretation limits

Today's [native baseline](native-baseline-diagnostics-a/ollama-p1/report.json) passed 8/8 narrow synthetic checks. Its captured Sage replay completed general/patient answers in 25.88 s / 33.95 s; the department request timed out at 60 s. These engine-only results miss the 3/5/15-second completed-answer targets and cannot establish full Orb capacity.

The [previous day's evidence](../mlx-vs-ollama-2026-10-21/README.md) retains the earlier 200-input failures and full-retrieval input capture. Capturing real retrieval with generation stubbed proves neither model quality nor an end-to-end SLA. These experiments do not include summary refresh, other hospital workloads, deployment resilience or production security qualification.

These results do not establish clinical equivalence or purchase-ready hardware sizing. There is no measured basis for extrapolating to a Mac Studio Ultra, a number of Macs or 200 concurrent hospital users. Clinical review and representative end-to-end validation remain separate gates.
