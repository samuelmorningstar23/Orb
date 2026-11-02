# Retrieval and serving optimisation evidence — 21 October 2026

**The 200-fresh-answer 3/5/15-second requirement is not qualified.** Software
improvements are implemented, but a successful HTTP response, a fast fake
model, and an unchanged clinical configuration are different kinds of evidence.

## Implemented changes

* Scoped, indexed latest-observation probes replace repeated whole-hospital
  history aggregation. Migration 90 adds observation and risk tie-break indexes.
  Voids, corrections, admission boundaries and patient/site scope remain live.
* Bounded immutable text preprocessing avoids repeatedly parsing the same name,
  bed and ward labels. Every turn still reads the current roster; no chart,
  permission decision or patient-resolution result is shared between requests.
  Parsed names are volatile PHI in RAM, bounded and never emitted in metrics.
* Sage uses a separate, bounded retrieval executor, with four workers selected
  for the full-retrieval profile after comparing one and four. The
  private queue holds at most 200 active plus waiting jobs. Cancelling queued
  work removes it; cancelling a running HTTP waiter does not release the
  physical thread's admission slot. Context variables remain request-local.
* Local model clients reuse verified CA trust material, preserving certificate
  and hostname checks and `trust_env=False`. Clients, headers and cookies remain
  request-local. Two hundred concurrent cold residency checks share one bounded
  engine probe; invalidation, timeouts and cancellation cannot reuse an old result.
* Conversation question/answer pairs now use one transaction and commit,
  including unpooled SQLite. Failure of the second insert rolls back the pair.
  Provenance, shadow records and fail-closed access audits are retained.
* The serving qualification launcher pins the installed model weight digest,
  Q4_K_M weights, f16 KV, 8192-token interactive context and existing clinical
  prompts/output budgets. It tests real engine slots and refuses insufficient
  memory headroom; no 200-slot configuration is enabled by a mock result.

## Application-path results

The controlled default-executor sweep is in
[profiling-notes.md](profiling-notes.md). All four copies have the same source
hashes. One worker completed the fake-model burst in 3.585s, versus 5.873s for
the runtime default. Wider fan-out worsened this workload.

The subsequent
[production-executor run](round3-production-executor-200-mock.json) includes the
dedicated retrieval executor and PHI-classifier improvements, with no fixture
override of the generic default executor:

| Request class | Count | First readable max | Full completion p95 | Full answers within target |
| --- | ---: | ---: | ---: | ---: |
| General, 3s | 67 | 2.962s | 3.111s | 58/67 |
| Patient, 5s | 67 | 2.939s | 3.092s | 67/67 |
| Department, 15s | 66 | 2.961s | 3.149s | 66/66 |

All 200 completed without protocol errors; strict completed-answer acceptance
still failed. This run uses a **fake model and uninitialised guideline models**:
it measures authorised chart retrieval, parsing, streaming and persistence,
not real guideline retrieval, AI throughput or clinical correctness. Its exact
source hashes precede the final executor lifecycle fixes, four-worker default,
and MPS/FP16 reranker batch selection; later runs retain their own hashes.

All disposable application databases in these runs are **unencrypted SQLite**,
with synthetic records. Encryption was not disabled on a live installation.
Production encrypted-database performance and the Mini/network path remain
unqualified by these measurements.

## Full retrieval exposes the next limit

The initial [installed-model retrieval burst](round3-full-rag-200-mock.json) uses real
BGE-M3 and BGE-reranker-v2-m3 with the fake LLM. All 19 corpus vectors and the
query probe are nonzero, the cross-encoder is exercised, and no lexical
downgrade or worker failure is recorded. At a hard 30-second request deadline:

* 185/200 responses completed; 15 timed out.
* General: 3/67 within 3s; patient: 8/67 within 5s; department: 30/66 within 15s.
* Embeddings accumulated 2.55s; cross-encoder work accumulated 25.56s.
* One running physical job retained its slot after the client's deadline, and
  queued cancelled jobs were removed. Counts captured before teardown include
  one unfinished reranker call; it is not a completed result.

The [memory guard](full-rag-200-memory-guard.json) completed without triggering;
the minimum OS available-memory estimate was 67%. This is a retrieval-only
experiment, not a simultaneous 27B-generation result. The serial cross-encoder
and head-of-line blocking dominate full retrieval. The chart-only one-worker
comparison cannot establish the best production setting for this slower stack.

After the qualified batch-16 change, a frozen-source full-retrieval comparison
completed all 200 attempts with both settings:

| Retrieval workers | Burst wall | General within 3s | Patient within 5s | Department within 15s |
| --- | ---: | ---: | ---: | ---: |
| [1](round4-full-rag-width1-200-mock.json) | 27.94s | 6/67 | 9/67 | 36/66 |
| [4](round4-full-rag-width4-200-mock.json) | 26.82s | 7/67 | 12/67 | 38/66 |

Both exercised all 134 embeddings and 134 reranks, returned 1,608
cross-encoder scores with zero errors or lexical downgrade, and drained all
200 executor jobs. Four workers is selected for the final full-retrieval
configuration. The modest difference in this single comparison is not a
general hardware optimum; neither configuration meets the requested SLA.

For this scripted mix alone, the 67 general and 67 patient requests require
134 reranks of 12 pairs each. Completing those by the patient 5s deadline needs
at least **322 query/passage scores per second**, before allowing time for
embeddings, permissions, chart assembly or generation. The observed reranker
rate is about **58 pairs/s**. This arithmetic is specific to this workload;
larger hospital corpora or different questions can change it.

## Clinical quality and test scope

The retrieval equivalence tests compare returned facts across department/site
scopes, null and tied timestamps, correction/voiding, discharge/readmission,
live roster changes and legacy name/bed/ward parsing. The indexed query uses
over ten times fewer SQLite VM instructions in a case with 20,000 unrelated
observations, while returning the same scoped facts.

An earlier integrated backend selection passed 320 tests. The final
current-source selection passed **184 tests in 11.80s**, covering HTTP-client
isolation, atomic conversation writes, SSE behaviour, retrieval admission and
shutdown, residency-probe sharing, reranker defaults, provider routing,
patient-summary snapshots and load-test accounting. Separate retrieval,
governance and serving-supervisor selections also passed. These selections
overlap and must not be added into a distinct-test count. Targeted backend
typing and lint checks passed. An earlier HTTP-client policy run detected a
file changing during collection; the subsequent frozen-source policy selection
passed.

These tests do not establish clinical accuracy. The clinical model, record
coverage, safety gates, citation handling, permissions and answer budgets have
not been reduced to obtain faster timings. Full guideline-model and real
generation tests must be considered separately from the fast application test.

## Hardware decision

The [reranker batch experiment](reranker-batch-candidates.json) compared batch
8/16/32 and length bucketing on the same installed FP16 cross-encoder. All
1,197 calibration/boundary pair outputs and 144 timing pair outputs matched
bit-for-bit, including top-three order and calibrated keep/withhold decisions.
The comparison includes both accepted and rejected passages. Batch 16 reduced
the 12-candidate median from 0.198s to 0.181s; batch 32 added little benefit and
length bucketing added code for less gain. Batch 16 is the selected small
change on the qualified MPS/FP16 path. CPU, CUDA and MPS/FP32 retain an implicit
batch of 8 until separately qualified; explicit caller sizes remain supported.
This microbenchmark does not establish an end-to-end latency pass.
See [the experiment notes](reranker-batch-notes.md).

The [real-model warm-host diagnostic](../capacity-2026-10-21/serving-full-rag-20261021-b/report.json)
completed three sequential, distinct actual Orb queries with full installed
retrieval and no lexical downgrade:

| Class | Target | First readable | Full answer |
| --- | ---: | ---: | ---: |
| General | 3s | 6.57s | 21.45s |
| Patient | 5s | 2.51s | 24.48s |
| Department | 15s | 20.62s | 26.01s |

All were complete, untruncated outputs. The runner reported about 14.4–14.8
decoded tokens/s: 233 general-answer tokens and 320 patient-answer tokens.
The department prompt evaluated 2,739 tokens in 20.16s before decoding its
84-token answer. The patient prompt reused 573 tokens, confirming prefix reuse
is active. Prompt truncation, a smaller model or shorter answers were not used.
The captured invented-patient outputs acknowledge missing observations and
clinical information; three such examples do not establish clinical accuracy.

The [first cold combined startup](../capacity-2026-10-21/serving-full-rag-20261021-a/report.json)
was stopped by the unchanged 4 GiB compressor-growth guard before a clinical
query. The single warm-host retry used the same guards and had no new swapouts;
it does not erase the cold-start result. The two-slot profile was then refused
by its memory preflight, approximately 45.5 MB short of the fixed estimate.
It generated no two-slot or 200-request evidence. A diagnostic procedure's
`status: complete` is not an SLA pass; inspect each phase and skipped profile.

The final [200-attempt real-model qualification](../capacity-2026-10-21/serving-full-rag-20261021-c-burst200/report.json)
used the current four-worker retrieval configuration and MPS/FP16 batch-16
adapter. The 27B model warmed, installed guideline retrieval initialised, and
the 200-concurrent-request phase began with one actual generation slot. The
unchanged memory guard stopped it during the burst after system compressor
growth reached 4.34 GB. The minimum OS available-memory estimate was 28%, with
no new swapouts. This is a guarded abort, not a measured out-of-memory crash.

The report deliberately contains no fabricated completed-cohort timings or
success counts: the burst had not returned when it was stopped. Its
`procedure_completed`, `acceptance_passed` and
`strict_200_request_targets_demonstrated` flags are all false. All owned model
and fixture processes were cleaned up and their port closed; the existing
Ollama daemon was not changed. No further heavy retry was attempted. This
qualification excludes ASR, imaging and background summary-refresh load, so it
cannot establish capacity for those additional hospital workloads either.

Using the observed answer lengths as a **sizing illustration**, 67 general
answers need 15,611 output tokens by 3s, and those plus 67 patient answers need
37,051 tokens by 5s: over 7,400 output tokens/s before accounting for the
department answers, prefill, full retrieval, background work or headroom.
This is not a prediction that all real answers have those lengths, and batched
throughput cannot be extrapolated by multiplying the single-stream rate.

The measured host is **M2 Max, 64 GiB, 12 CPU cores**. It is not the proposed
Ultra. An isolated preflight for the unchanged 27B model admits one and two
8192-token/f16 slots with the sampled headroom; four slots fail the conservative
memory guard. A refused preflight is not a throughput measurement.

The inspected runner allocated **3,968 MiB of f16 KV per 8,192-token slot**
with full SWA. Two hundred such active slots would use **775 GiB for KV alone**,
before model weights and other workloads. This applies to the pinned full-cache
profile, not every serving engine: compact/paged cache designs can change the
memory requirement and need their own quality and throughput qualification.

Do not purchase a max-storage Studio on the assumption that it satisfies 200
simultaneous fresh clinical generations. SSD capacity does not establish model
throughput. A hospital-local inference server or cluster needs a benchmark with
the actual model, full retrieval, context lengths, output lengths, deadlines,
encrypted database and background summary/ASR/imaging demand before any exact
configuration can be called sufficient. The Mini remains a gateway and cannot
add AI compute or unified memory to the Studio.

See [the overall architecture](../../CAPACITY_200_REQUESTS.md) and
[the isolated serving qualification](../../engineering/serving-candidate-2026-10-21.md).
