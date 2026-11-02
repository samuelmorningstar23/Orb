# Offline MLX versus Ollama benchmark — 21 October 2026

## Current conclusion

**This Mac has not demonstrated 200 simultaneous completed answers within
3/5/15 seconds. The first MLX conversion ran but failed factual fidelity:
2/8 synthetic checks passed, versus Ollama's 8/8. It is rejected for timing
qualification. Higher-precision and source-preserving candidates stopped
before answers with ordinary and staged loading. No quality-qualified MLX
timing or speedup is available.**

The host is an Apple M2 Max, 64 GiB unified memory, 12 CPU cores. These results
are measurements of this development Mac, not a Mac Studio Ultra forecast or
a hospital deployment qualification.

The benchmark temporarily unloaded and then restored the regular 27B
model. It was verified idle and serving the pinned source before release.
After all six release windows, its manifest digest, 8192-token context and
indefinite residency were restored and verified. The original 4B model's
digest/context remained unchanged. Model files and daemon configuration were
not changed. See [machine-readable comparison and restoration summary](comparison-summary.json)
and [final restoration evidence](suspended-resident-source-repack4-f16-staged-b/lease-report.json).

## Measured engine-only results

These are complete HTTP answers through the benchmark proxy, with real
generation but retrieval unloaded. The inputs came from Orb's actual full
retrieval path. They are **not end-to-end Orb response times**.

| Exact captured query | Target | Ollama, one active slot | Output tokens | MLX |
| --- | ---: | ---: | ---: | --- |
| General: metoprolol side effects | <3 s | 21.302 s | 232 | No qualified timing |
| Patient: recorded observations, medicines and tasks | <5 s | 23.190 s | 301 | No qualified timing |
| Department: ward review priorities | <15 s | 25.099 s | 84 | No qualified timing |

All three Ollama answers ended normally. Ollama also passed all eight synthetic
fact-preservation checks. This narrow diagnostic is not clinical validation.

The first executable MLX candidate completed seven of eight diagnostic streams
normally but preserved the required facts in only two cases. It invented
`metronidazole` where the synthetic source said stopped `metformin`, corrupted
allergy/patient fields, changed numerical values into unit-bearing strings, and
returned string `"null"` where JSON null was required. The medication answer
repeated text until its unchanged 512-token limit and failed completion. Only
the negation and long-context/corrected-lab cases passed. These are actual
generation failures, not the earlier HTTP 502 failures during guard aborts.

All eight model request bodies are identical to the Ollama baseline, and native
Ollama prompt counts match all eight MLX captured lengths. Exact native IDs for
these eight diagnostic inputs were not separately observed. Independent source
reviews found no demonstrated architecture, input-mapping or active serving-path
defect. Additional affine re-quantization changes the weights, reaching 9% first-
row relative L2 error in sampled 4-bit matrices. This supports investigating a
higher-precision conversion; it does not prove the sole cause of the failures.

[Actual mixed 4/6-bit fidelity report](suspended-resident-a/mlx-p1/report.json).

For the simultaneous engine test, 200 distinct original inputs were released
together: 67 general, 67 patient and 66 department. With two Ollama slots,
**0/200 completed** before their 20-second whole-request deadlines. All 200
timed out; two began streaming and 198 returned no response headers before
timeout. No request was removed from the denominator. Partial text is not a
successful answer. Successful-answer quantiles are null because none completed.

Evidence:

- [Ollama one-slot report](ollama-engine-p1-a/report.json)
- [Ollama two-slot, 200-input report](ollama-engine-p2-200-a/report.json)
- [200-input capture report](full-rag-input-capture-200-a/report.json)

The regular 27B/4B residents were present during these measurements. The
one-slot Ollama run had zero new swap-out or compression growth; the two-slot
run had zero new swap-out and 1.26 GB compression growth. Memory statistics
are system-wide, not an individual process's peak RSS or GPU usage.

## MLX candidate and input fidelity

The source is the installed `medgemma:27b` GGUF, SHA-256
`4b97da6b21a8fb1405a0d1efee462bfd6c4d3c46307bac8b34373835f07ca758`.
No model weights were downloaded. The original GGUF is unchanged.

Native MLX loading of its mixed Q4_K/Q6_K format expands those quantised
tensors to FP16; all weights would require about 51.10 GiB before caches and
working memory. A small real-format probe verified this behavior. Instead,
the approved conversion was written separately, tensor by tensor, to:

`/private/tmp/orb-mlx-benchmark-20261021/model-mixed46-b`

It contains 808 text-backbone tensors and 18,182,211,008 bytes of native MLX
weights, using 4/6-bit affine quantisation with group size 32. The conversion
is **lossy and numerically different** from the source K-quants. It excludes
439 vision/projector tensors and is suitable only for this text-serving
experiment. It does not qualify imaging, transcription or clinical deployment.

The conversion preserves source/file hashes and explicit unqualified status.
Normalization-weight handling and model architecture are pinned. No quantised
KV cache, smaller model, reduced clinical context, speculative decoding or
shortened response budget is used.

Input checks found and fixed two real compatibility issues:

1. Orb emits two leading system messages. Ollama joins their contents with
   exactly two line feeds; their original contents are preserved.
2. The tokenizer needs SentencePiece's dummy prefix around ordinary segments
   separated by special tokens. A space-replacement-only normalizer produced
   different IDs despite identical rendered text.

After the separately recorded metadata amendments, rendered bytes and every
effective input token ID match **3/3 original Sage inputs and 11/11 selected
inputs from the 200-input capture**. The other 189 cohort inputs were not
observed with the native oracle. This is not an all-200 tokenization claim.
Only documented single-BOS handling is reconciled; whitespace is never
normalized to force a match. Original metadata and the original conversion
manifest are preserved. All 808 weight-file manifest entries remain unchanged.

The wrapper pins the active sampler order to top-k, top-p, then temperature;
small real-MLX probability probes match an independent reference within 1e-6.
Repetition-penalty probes pass. Random-number generators, arithmetic,
requantisation and output detokenization remain distinct. Input parity and
sampler probes do not establish equivalent clinical quality.

- [Conversion manifest](conversion-manifest.json)
- [Original conversion manifest](conversion-manifest.before-template-alignment.json)
- [Three-input token comparison](template-comparison.json)
- [Eleven-input cohort subset comparison](template-comparison-200-subset.json)
- [Sampler diagnostic](sampler-filter-probe.json)

## Attempts stopped by the resource guard

The guard stops only owned benchmark process groups, with no relaxed limits:
two low-memory samples below 15%, more than 512 MiB new swap-out, more than
4 GiB compression growth, or expiration of the bounded time window.

| Attempt | Scope | Outcome |
| --- | --- | --- |
| `ollama-p1-a` | Ollama and full RAG together | Compression guard before clinical-answer results |
| `ollama-p1-b` | Warm retry, full RAG | Compression guard; eight HTTP 502 transport failures, not eight incorrect clinical answers; zero observed sequential results |
| `mlx-engine-p1-a` | MLX without retrieval | Compression guard before fidelity/timing; 6.22 GB growth |
| `mlx-engine-p1-b` | MLX with unused Torch/TF/Flax imports disabled | Compression guard before fidelity/timing; 5.87 GB growth |
| `suspended-resident-affine8-a/mlx-p1` | Higher-precision 8-bit candidate, regular 27B released | Compression guard before answers; 9.44 GB growth |
| `suspended-resident-affine8-staged-a/mlx-p1` | Same 8-bit weights, eager per-file uncached loader | No completed warmup/answers; HTTP failure reported while compression exceeded the guard at 6.38 GB growth |
| `suspended-resident-source-repack4-f16-a/mlx-p1` | Source-preserving Q4 repack / dense Q6 FP16, ordinary loading | Compression guard before answers; 11.07 GB growth |
| `suspended-resident-source-repack4-f16-staged-a/mlx-p1` | Same source-preserving weights, staged loading selected | Not launched: preparation-time memory estimate below the unchanged preflight requirement |
| `suspended-resident-source-repack4-f16-staged-b/mlx-p1` | Same source-preserving weights, staged loading with fresh preflight | Passed admission at 79% availability, then no warmup/answers; HTTP 502 while compression grew 11.50 GB |

With the approved regular 27B release, `suspended-resident-a/mlx-p1` completed
the eight-case diagnostic within the original guard (minimum 40% OS free,
3.41 GB compression growth, zero new swap-out). The **quality gate**, rather
than memory, then stopped timing. Restoration followed owned-process cleanup
and was verified. A watcher stops owned testing if another requester reloads
the regular model; it never repeatedly evicts that requester's work.

The unsuccessful startup attempts in the table provide no MLX token rate,
answer latency or factual-check score. They are not proof of physical OOM.
All completed release windows restored and verified the original 27B resident
and preserved the 4B model after cleaning up their owned processes. The affine8
staged and final source-repack attempts report HTTPStatusError while pressure
samples independently show growth above the 4 GiB threshold. During those
runs, a monitor that started killing the owned engine could be cancelled
during cleanup before it raised its guard exception, leaving HTTP 502 as the recorded
primary failure. Those records are retained without relabelling them as an
observed exception or as physical OOM.

After the final model run, the monitor was updated to record its selected
guard reason before awaiting owned-process termination. Future reports retain
that explicit trigger even if teardown cancels the monitor before it raises;
actual task exceptions remain separate. Unit tests cover this cancellation
window. The updated diagnostic instrumentation was not another inference run.

The first source-repack staged attempt took its preflight snapshot before the
large file-hash check: OS availability rose from 54% at that snapshot to 78%
after validation. No MLX process launched. This exposed a stale-preflight
measurement; subsequent runs take the authoritative snapshot immediately
before launch and retain the earlier preparation snapshot separately. Thresholds
and memory estimates are unchanged. Both source-repack staged attempts restored and
verified the original resident and preserved the 4B model. The corrected retry
did launch at 79% availability, but stopped before quality answers. All native
and proxy ports from that retry were verified closed before restoration.

The failed input-capture attempts `full-rag-input-capture-a/b` exposed adapter
validation errors around retention `1h` and multiple leading system messages.
They are retained as failed attempts and never counted as performance evidence.
Capture `c` completed with three exact original inputs; the later 200-input
capture verified 134/134 embedding calls, 134/134 rerank calls and 1,608
cross-encoder scores, with no errors or lexical fallback. Generation during
capture was an explicitly labelled stub. Capture timing is not inference timing.

## Offline execution and verification

The run used a one-time missing-software installation and a separately
labelled conversion. Software is isolated under
`/private/tmp/orb-mlx-benchmark-20261021/venv`; Orb's environment is unchanged.
Package provenance is in [software-provenance.json](software-provenance.json).

All conversion and inference runs use `scripts/offline_benchmark.sb` through
macOS `sandbox-exec`: outbound network is denied except localhost. A real
probe found that loopback access was not denied by the policy (the unused port
refused the connection), while external access was denied. Successful benchmark
requests subsequently used localhost. Hugging Face/Transformers offline flags
are also set. No cloud
inference, real patient records or production database is used.

The benchmark database is disposable, synthetic and unencrypted; encryption,
hospital networking, summary refresh, imaging, ASR, failover and background
workloads are outside these measurements. Complete-answer latency is measured;
first raw content is recorded separately and never treated as usable-answer
or end-to-end latency.

Verification after the current changes: **386 focused tests passed** with the
isolated software environment, including real GGUF CPU conversion/repack
fixtures. Ruff F/I passed across eight benchmark/guard sources; mypy passed for seven
orchestration, conversion, repack and loading sources.
[Verification details and source hashes](verification-final.json).
Tests exercise conversion/tokenization, stream completion, failed denominators,
incremental cancellation accounting and refusal to award clinical or purchase
qualification from timing alone.

## Higher-precision experiment

A separately labelled affine 8-bit/group32 candidate completed conversion
directly from the original GGUF, never from the failed MLX weights. Its tensor
payload is 30,389,408,768 bytes (28.302 GiB). Conversion took 626.094 seconds,
with zero new swap-out or compression growth. The worst sampled first-row
relative L2 error was 0.636%, versus approximately 9% for the original 4-bit
re-quantization. These samples are not full-tensor or clinical equivalence.

Both ordinary and staged loading stopped before answers, as recorded above.
The staged loader evaluates each file before closing its uncached descriptor;
it does not change weights, dtypes or sampling. A real tiny-fixture check
verified bit-for-bit equality for packed uint32, FP16 and FP32 arrays, including
evaluation after the renamed source file was closed and removed. This proves
the loader behavior for that fixture, not full-model memory or clinical quality.

- [Affine8 conversion manifest](conversion-affine8-manifest.json)
- [Affine8 conversion guard](conversion-affine8-guard.json)
- [Ordinary-loader attempt and restoration](suspended-resident-affine8-a/lease-report.json)
- [Staged-loader attempt and restoration](suspended-resident-affine8-staged-a/lease-report.json)
- [Tiny real-loader bitwise check](staged-loader-probe.json)

A further separately labelled candidate preserves the original Q4_K integer
codes with FP16 affine scale/bias storage, and stores the original Q6_K
dequantized values directly as dense FP16. It performs no fresh quantization.
Its tensor payload is 24,016,268,288 bytes (22.367 GiB), across 808 files.
Conversion completed in 549.146 seconds (557.944-second supervised window),
with zero new swap-out or compression growth. All 376 original Q4_K matrices
preserve source integer codes; all 59 Q6_K matrices use dense FP16. The worst
sampled first-row relative L2 errors are 0.0786% and 0.0210%, respectively.
FP16 rounding still makes this candidate numerically distinct and unqualified;
the exact source integers do not establish equivalent inference or clinical
quality. Its prompt bytes and token IDs match all 14 previously observed cases.

- [Source-repack conversion manifest](conversion-source-repack4-f16-manifest.json)
- [Source-repack conversion guard](conversion-source-repack4-f16-guard.json)
- [Source-repack three-input token check](template-comparison-source-repack4-f16.json)
- [Source-repack eleven-input subset token check](template-comparison-source-repack4-f16-200-subset.json)

Neither newer candidate produced a quality answer on this host within the
unchanged startup resource policy. Their single-request replay, higher-width
200-request replay and combined full-retrieval phases were therefore omitted.
The original low-bit candidate was rejected for quality instead. No candidate
was substituted into Orb, and no MLX speedup ratio can be calculated from these
attempts. Conversion itself did not unload the regular service.

The remaining measurement needed is a candidate that both passes factual
checks and runs inside the resource policy. Any future larger-Mac evaluation
must then repeat the exact 200-input workload and the combined Orb path;
these M2 Max measurements cannot establish a Studio Ultra specification,
required machine count or linear speedup forecast.

Do not substitute the candidate into Orb or recommend a hospital hardware
purchase from these results. A reproducible, clinically reviewed evaluation and
complete 200-request end-to-end pass remain necessary.
