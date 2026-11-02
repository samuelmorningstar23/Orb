# Capacity evidence — 21 October 2026

Historical snapshot before the subsequent retrieval/serving optimisation.
Use the [follow-up evidence](../capacity-optimisation-2026-10-21/README.md) for
later source versions; the raw results below are retained unchanged.

## Results to use

| Evidence | Result | Scope |
| --- | --- | --- |
| [Current real 200-request burst](orb-current-real-200-deadline.json) | 0/200 successful completed answers; 199 capacity rejections, 1 hard timeout | Actual copied Orb + installed local model; 3/5/15s targets failed |
| [Current cached-summary burst](orb-current-summary-200-mock.json) | 200/200; p50 0.437s, p95 0.808s, p99 0.854s, max 0.869s | Actual auth/scopes/migrations/cache; synthetic generator populated 100 real-service snapshots |
| [Current app with fictional inference capacity](orb-current-pool-200-mock-ollama.json) | 200/200 completed; general 0/67 under 3s, patient 16/67 under 5s, department 66/66 under 15s | Actual Orb; fake model and 200 fictional slots; **not hardware capacity** |
| [Full browser journey](summary-browser-e2e.json) | Patient draft displayed; refresh POST 202; no browser errors or external requests | Built UI → actual authorised API → disposable DB; synthetic summary generator |

The cache and app/mock reports' copied-source fingerprints match the current
19 tracked application components. The real-model report precedes only the
later `patient_summary_service.py` reservation fix: periodic summaries now use
required-work priority in a qualified pool instead of being starved by the
legacy ambient governor. Summaries and the worker pool were disabled in that
real-model workload, so that branch was not exercised. Its hash is retained,
not overwritten. The new branch is covered by a direct test holding an
interactive lease while the dedicated summary worker runs.

The real burst ran on **Apple M2 Max / 64 GiB / 12 CPU cores**, local Ollama
**0.40.4**, configured `medgemma:27b` Q4_K_M, 8,192-token interactive context,
one engine slot. Installed tag digest observed before the probe:
`58238ae38f99827496301de22016b2f94157552971f5a38db226f13802c2437e`.
An owned one-token synthetic generation completed before the current burst
to make model weights resident (2.974s total, 2.572s load). It was a prewarm,
not an answer or a clinical-quality result. Prompt/KV cache hits for subsequent
unique questions were not guaranteed. The process was stopped after the probe;
port 11436 closed and the existing Ollama service on 11434 remained present.

All cases used temporary source copies and invented patients. The fixtures
record an actual HTTP peak of 200. Plaintext SQLite, isolated offline
embedding/reranker caches and disabled ASR/imaging/scoring/background workloads
mean these are **not encrypted hospital production qualification**. No tests
measured the proposed Ultra, the physical mini proxy/network, sustained
clinical traffic or real generation of 200 summaries/hour. Browser/API success,
strict output parsing and source freshness do not establish clinical accuracy.

## Interpretation

An HTTP 200 is not an answer success. The harness rejects SSE errors, failed
terminal markers, empty/invalid/truncated streams, missing model provenance
and requests that exceed a whole-call deadline. Failures remain in every SLA
denominator. Full-success latency quantiles are null when there are no full
successes; they must not be read as zero latency. First readable and completed
answers are counted independently.

The stub run's negligible worker-admission wait and much larger retrieval time
show an application-side burst bottleneck even before real model compute.
The reported retrieval interval includes history/classification/DB queueing,
chart assembly and instrumentation. It is not a pure vector-search timer.

## Verification

* Root regression selection: 214 tests passed, covering worker/provider
  routing, Sage streams, summaries, HTTP admission, migrations, facility
  fences, security headers and the harness.
* Follow-up summary/reservation/provider selection: 42 passed after the final
  reservation change. This overlaps the regression selection; do not sum it
  into a distinct-test count.
* Harness protocol/safety/deadline checks: 27 passed (included in the root
  selection). Dedicated inference/call-site regression selections also passed.
* UI selections: 11 passed, including patient switching, failure withholding
  and expiring displayed prose without waiting for the next poll.
* TypeScript compilation, production build, targeted ESLint/Ruff and targeted
  backend mypy checks passed. This is not a claim that every unrelated dirty
  workspace file or the entire repository's test suite was checked.

The browser journey first found a doubled `/api/api` prefix. The fixed journey
passes; failure evidence is retained in `summary-browser-e2e-failure.*` so that
the broken boundary and correction remain reviewable.

## Earlier diagnostics

`orb-default-*`, `orb-pool-*`, `orb-final-*`, `orb-mixed-*` and
`orb-summary-*` are intermediate source versions. Their raw fingerprints and
results are retained. Do not combine their best percentiles into one pass or
claim they all ran on the identical latest source.

`orb-real-1.json` is explicitly marked as contaminated by overlapping work.
Another serial probe was interrupted after overlap and after discovering that
HTTP read-gap timeouts did not bound a continuously active stream. It produced
no completed acceptance artifact. The harness now uses a whole-call deadline.
The earlier real two-request probe was cancelled; it is not evidence of two
successful generations. `orb-real-200-deadline.json` records the earlier
pre-admission-limit 200-timeout failure and is not the current rejection policy.

See [the implementation/hardware decision](../../CAPACITY_200_REQUESTS.md) and
[reproduction guide](../../engineering/capacity-load-test.md).
