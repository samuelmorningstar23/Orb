# Unchanged reranker batch experiment

The script-only experiment in `reranker-batch-candidates.json` used the installed
BGE-reranker-v2-m3 on **MPS / FP16**, unchanged weights, sigmoid normalization,
384-token query cap, 512-token passage cap and 512-token `only_second` pair cap.
It opened no database and changed no production service.

All 62 existing calibration questions plus a long-query boundary case were
scored against all 19 literal built-in public/synthetic clinical documents:
1,197 pairs per configuration. Actual baseline forward inputs were observed
without padding and hashed; the length-bucket candidate retained the exact
same IDs/masks in original pair positions. Separate timing calls exercised
12 candidates per query, matching the current reranking pool, without the
diagnostic tensor copies/hashes inside the timed interval.

| Configuration | Twelve-pair median | Pairs/second | Median speedup | Max score difference |
| --- | --- | --- | --- | --- |
| Current batch 8 | 0.198345s | 50.71 | 1.000× | reference |
| Batch 16 | 0.181046s | 54.51 | 1.096× | **0** |
| Batch 32 | 0.179590s | 55.16 | 1.104× | **0** |
| Length-bucket batch 8 | 0.189930s | 50.77 | 1.044× | **0** |

Every alternative produced **bitwise identical scores** for both the 1,197
quality pairs and 144 timing pairs. Top-three order and the calibrated
0.31541 relevance-floor decisions were unchanged. Source/model/device/dtype
fingerprints and unpadded input hashes matched. The script would reject a
candidate on any top-three/threshold change, dropped/misrouted score, changed
input or maximum score difference over 0.005; no tolerance was needed here.

Batch 16 is the smallest useful candidate for a twelve-passage call: it avoids
splitting it into eight plus four. Batch 32 also executes that call as twelve,
so the small measured advantage over 16 is insufficient reason to select it.
Length bucketing reduced quality-workload padded tokens from 256,689 to 240,569,
but its added preparation/scatter work left throughput near the baseline.

Each configuration ran in a new owned copied-source process, avoiding allocator
carry-over. Maximum process RSS was approximately 4.20 GB for each. The maximum
**observed** MPS driver allocation was 2.257 GB for batch 8 and 2.291 GB for
16/32; those snapshots are not a guaranteed transient GPU high-water measure.
The full four-configuration experiment took 112.50s, with at least 65% OS free
estimate and no memory-guard trigger. No existing Ollama daemon was contacted.

These results justify testing a small batch-16 production change, followed by
the full retrieval and real-LLM HTTP burst. They do not establish a clinical
accuracy improvement, independently validate the calibration labels, qualify
other hardware, or satisfy the 200 simultaneous 3/5/15s requirement.

The subsequent full-retrieval fake-LLM bursts, with batch 16 on the qualified
MPS/FP16 path, completed all 200 requests in 27.943s at retrieval width 1 and
26.821s at width 4. Neither met the per-request targets. See `profiling-notes.md`
and the `round4-full-rag-*` result/guard/settings files for the complete failure
denominators and source fingerprints. This promotion changes batch grouping;
it does not change the model, evidence pool, score floor or truncation limits.

The final production selector applies the implicit batch of 16 only when the
loaded model's actual device is MPS and its actual dtype is FP16. CPU, CUDA and
other dtypes retain the prior implicit batch of 8 until separately qualified.
Explicit caller batch sizes remain supported. The two full-RAG comparison
reports predate this device gate but used the same qualified MPS/FP16 branch;
their recorded source hashes are retained rather than rewritten. The final
real-model qualification must record the later gate and selected executor
configuration. After the gate, 118 focused adapter/reranker/calibration/eval and
harness regressions passed; critical Ruff and targeted mypy also passed.

Reproduction, with the model already installed and no competing qualification:

```sh
server/.venv/bin/python scripts/benchmark_reranker_batches.py --model /absolute/installed/bge-reranker-v2-m3/snapshot --execute --output /tmp/new-reranker-batch-experiment.json
```

The script refuses to overwrite a result. It requires 12 GiB estimated available
and 20% OS free initially, uses the same pressure safeguards as the serving
qualification, and bounds the complete experiment at 300s. The 15 companion
unit tests protect exact scatter ordering, no dropped/duplicate scores,
threshold decisions despite small numeric differences, token caps/state restore
and literal-only loading of static corpus source.
