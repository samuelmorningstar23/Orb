# ML dependency security and Sage latency, 12 October 2026

Follow-up: [Sage prompt-cache performance fix](SAGE_SPEED_FIX_2026-10.md)
records the subsequent full-SWA cache investigation and refreshed verification.
The timing results and source snapshots below remain the preceding baseline.

## Scope and acceptance plan

Upgrade the vulnerable local ML runtime without changing held clinical rules,
model weights or live appliance data. Orb inference remains offline. The preceding
installed-tree scan reported 47 ML package/advisory matches: Torch 2.6.0,
Transformers 4.49.0 and the unused Accelerate training chain. The inherited
orphan pypdf installation belongs to that older test environment, not Orb's
declared dependencies.

| Work | Acceptance |
|---|---|
| Replace the legacy FlagEmbedding loader and its training dependency chain | Preserve BGE-M3 normalized CLS dense vectors and single-logit reranking; local snapshots only, no remote code or downloads; safe checkpoint loading. |
| Coordinate secure Torch/Transformers pins across runtime, tests and CI | Fresh isolated resolution, `pip check`, strict direct and installed-tree security scans, no advisory exclusions. |
| Validate compatibility | Curated backend suite, separate process cleanup checks, real synthetic local-model adapter tests, frontend suite and real-browser workflows. |
| Time Sage in a browser | User-confirmed target is submit to first readable answer: general <3s, patient <5s, department <10s. Also report successful terminal completion; reject empty, failed or truncated answers. |
| Preserve timing provenance | Record model/digest, scope, cold/warm residency, retrieval mode, idle/background load, query and rendered screenshots using synthetic records. Report misses without changing model/safety policy to improve a number. |

The optional `ORB_BACKGROUND_MODELS_ENABLED=0` setting disables automatic model
startup jobs only in the disposable idle benchmark. Ordinary use remains
default-on. Interactive inference, model selection, safety floor and permission
checks are unchanged. Idle results do not qualify hospital background-load
performance. Existing weights may be read for these tests; none are downloaded,
trained, converted or overwritten.

## Results

### Dependency and compatibility changes

- Production, test and CPU CI requirements now use Torch **2.14.1** and
  Transformers **5.19.0**. The inference adapter replaces FlagEmbedding and its
  unused sentence-transformers/Accelerate/PEFT/datasets training chain.
- Existing local BGE-M3 weights retain normalized CLS dense vectors; the
  reranker retains single-logit sigmoid scores and its 384/512 token boundaries.
  Transformers 5 tokenization uses its supported Rust post-processor. Loaders
  require local files, prohibit remote code and prefer safetensors. Existing
  tensor checkpoints require explicit `weights_only=True`; there is no unsafe
  retry or remote fallback.
- CoreML conversion also uses safe checkpoint loading and the current Vigil
  multi-horizon constructor/metadata. Synthetic contract tests cover it; no
  actual conversion or model artifact was produced.
- Bootstrap pip **26.2.1** is installed before pip-based setup and CI resolution.
  Filtered macOS requirements stay beside their relative constraint include,
  with temporary-file cleanup on failure and success.
- Security CI retains strict direct and installed-tree scans and adds the
  complete Apple production resolution. No advisory is ignored.

| Security check | Result | Boundary |
|---|---|---|
| Fresh installed macOS slim runtime | **70 packages, zero advisories**, strict exit 0; `pip check` passes | Actual installed packages, including transitives and bootstrap pip; separate scanner environment. |
| Complete Apple production resolution | **76 packages, zero advisories**, strict exit 0 | Metadata resolution includes MLX, lightning-whisper-mlx, Numba and tiktoken; native speech build/inference was not run. |
| Direct production / test / quality / HTTP constraints / bootstrap pins | **17 / 18 / 8 / 3 / 1 packages, zero advisories** | Supplements the resolved checks; not a substitute for them. |
| Existing appliance/shared environments | **Unchanged** | Old extras remain installed there. A fresh validated environment is required before switching the service interpreter. |

The original fresh-venv scan found six bootstrap pip advisories. Upgrading that
owned environment and repeating the entire installed-tree scan removed them.
This was not an advisory exclusion. The optional Apple tokenizer still needs a
source build on Python 3.12; successful metadata resolution does not prove that
build. Hosted Linux/macOS CI has been configured but has not run here.

Security commands, raw scans and hashes are in
[ML security verification](audit-2026-10/ml-security-verification.json).
Adapter contracts, exact focused tests and actual cached-model outputs are in
[ML runtime verification](audit-2026-10/ml-runtime-verification.json).
The cached models passed offline MPS checks: three 1024-dimensional float32 unit
vectors, higher semantic similarity for the related pair, and reranker scores
approximately **0.996888** for the relevant passage versus **0.0000162** for the
unrelated passage. Existing model weights were read without downloads or writes.

The clean environment also exposed a discharge QR bug: qrcode's default image
factory attempted to import optional Pillow despite the declared PyPNG backend.
Discharge generation now explicitly selects `PyPNGImage`. The existing PNG
regression runs with Pillow unavailable, and 185 discharge/lab-critical tests
pass without adding an image dependency.

Broad tests use disposable source copies and synthetic databases because upload
paths are relative to the application source. No live database or upload tree is
used. Scope inventory tests now recognize only the reviewed service delegation
shapes and include negative AST cases and real cross-ward/replay denial checks;
the empty bare-route ratchet remains enforced. Other repairs isolate catalogue
and machine-principal fixtures, explicitly seed the lexical retrieval fixture,
and align test expectations with strict backup receipts, the generic terminal
stream error and the additional opt-in offline modules. Production permission
checks, foreign keys and clinical assertions are retained.

Repartitioning the full suite after the gateway regressions exposed two more
existing fixture dependencies. Discharged charts named "Unassessed Vitals"
matched invented-name questions ending in "vitals"; a lab catalogue registered
LOINC 2524-7 as "Lactate" before the Orders test asserted the curated starter
display. The resolver still searches discharged locator rows, and the real
terminology seed still preserves existing site wording. Those two test modules
now use the existing module-owned database fixture. The exact failing orders
were reproduced, then 186 patient-scope cases and 17 Orders/lab cases passed with
their original assertions. The contaminating charts and original database are
retained after scope-fixture teardown. The intermediate full run's seven
failures are saved in
[pre-isolation evidence](audit-2026-10/backend-pre-isolation-20261012/results.json);
fixture details are in the ML runtime verification and
[Orders fixture verification](audit-2026-10/orders-terminology-fixture-verification-20261012.json).

The broad run also found a real restore-verification gap from the preceding
offline feature implementation. The shared verifier now includes all **45** new
tables across migrations 81–86, including the ten generated clinical
parent/history tables. Its explicit manifest covers **199 tables in 47 groups**.
Regression tests rederive the generated migration in an in-memory database and
reject each new table's absence under strict verification. Unknown dynamic DDL
still fails the manifest guard. No backup or live database was opened or changed.

### Sage correctness and measurement

The local provider now requires successful HTTP/NDJSON framing, nonempty content
and an explicit terminal `done:true`. Engine errors, premature EOF and
`done_reason=length` fail. Sage does not save a failed partial as a replayable
answer or emit a success frame for it. Cancellation closes the provider before
releasing the queue; the inference audit records failure unless the stream
finishes successfully.

The first department runs failed the response budget. Further tracing found the
actual cause: the bare imperative detector treated **"Give a concise overview"**
as a medication command, forcing the broad request into the buffered simple
scope with its 512-token cap. A speculative broad-budget increase did not fix
that route and was withdrawn. The informational wording and mixed-command
regressions now distinguish those requests at the matched verb. Real and mixed
medication/chart commands retain their guards, including later orders naming
unknown drugs. The original broad 1024-token budget remains. The exact browser
question is also a retrieval-to-route-to-provider integration regression,
asserting the actual department census, broad prompt and terminal completion.
Any later truncation still fails.

Chart-open prefetch also had a patient-access bypass: `sage.query` alone could
pass a caller-named patient into the system warm cache. It now requires the
existing `patient.read_all` permission and canonical department/facility scope
before any cache/model work. Patient existence and the existing fail-closed PHI
read audit precede warming. Thirteen regressions exercise authorized doctor/admin
reads, forbidden department/cache hits, a Sage-enabled pharmacist, forged body
identity, invalid/missing IDs and audit failure. This changes no patient-name
resolver or held clinical ruling.

The first chart-prewarm measurement exposed a second issue: with the default
shared primary/ambient model, ambient chart work requested a 4096-token context
and replaced the interactive 8192-token runner. The gateway now aligns the two
known small ambient windows only when the primary, ambient and request model
tags match exactly. Five existing ambient purposes reuse the interactive
context; distinct models and absent, unfamiliar or larger windows retain their
settings. Requests and nested options are copied. Prompts, output budgets,
timeouts, queue priorities and the models-off guard are unchanged. Fifty-one
focused tests and an independent review pass; actual speed remains subject to
the repeated browser measurement. See
[shared-model context verification](audit-2026-10/shared-model-context-verification-20261012.json).

Two measured reranker experiments were rejected. Doubling the batch size from
eight to sixteen slowed all six candidate sets by 341–431 ms. Length sorting
gave inconsistent gains and descending order slowed every set. Scores and
rankings matched exactly, but neither change established a useful latency
improvement. The existing adapter batching remains; commands, raw timings and
memory-measurement limits are retained in the ML runtime verification.

The opt-in [browser timing harness](../scripts/sage_browser_timing.md) uses real
authentication, seeded synthetic charts, the installed careful **medgemma:27b**,
thinking mode and safety floor, and a real vectorized corpus in an isolated
knowledge worker. It records actual browser submit-to-readable and successful
completion clocks, wire scope/provenance, residency, model digest, screenshots
and failure evidence. It neither mocks inference nor treats progress labels as
answers. Initial startup/prefix loading is reported separately; misses remain
visible. Ordinary non-model maintenance, including synthetic FHIR exports,
continues and its overlap is recorded.

The complete preceding run is retained in
[prior timing evidence](audit-2026-10/browser-evidence/sage-timing/prior-optimization-20261012/evidence-manifest.json).
It failed acceptance: the third general turn took **3048.9 ms**, and chart
prewarming timed out behind ambient work. A separate measurement bug required
the concise answer to remain inside the viewport at completion, even after long
expanded details scrolled it away. The timer now requires a previously visible
readable answer that remains rendered, successful wire completion and a finished
UI stream; hidden or unmounted answers still fail. All ten integrity regressions
pass. The old failure is preserved, not retrospectively converted into a pass.

### Real browser timing after the context fix

**Functional verification passes; latency acceptance fails.** The quiet run on
an Apple M2 Max with 64 GiB RAM used Ollama 0.35.1 and the same installed
`medgemma:27b` Q4_K_M model, digest
`58238ae38f99827496301de22016b2f94157552971f5a38db226f13802c2437e`,
resident with an 8192-token context. Thinking and the clinical safety floor
remained enabled, with no downgrade. Real local BGE retrieval and ordinary
non-model maintenance remained active. The department contained five verified
synthetic admitted patients. These results do not qualify larger or busy wards.

| Query | Strict first-readable target | Repeat 1 | Repeat 2 | Repeat 3 | Successful full completion |
|---|---|---|---|---|---|
| General | <3s | 0.8843s, pass | 5.0032s, miss | 3.8904s, miss | 4.43–8.00s |
| Patient | <5s | 6.3361s, miss | 6.3143s, miss | 6.3188s, miss | 42.15–51.53s |
| Department | <10s | 5.3920s, pass | 0.4994s, pass | 0.4960s, pass | 18.02–26.81s |

The initial actual query is also retained: **16.8867s** to readable text and
21.1559s to completion, with an initially cold owned retrieval worker/context
despite a resident model. It misses the general target. It was not a deliberately
unloaded-model cold-start experiment. No observation was replaced by a retry.

The additional real chart-open case completed authorized prefetch in 4.0892s
and confirmed a 3.136s model-prefix warm, replacing the earlier observed
25-second queue timeout. Its patient answer still missed: **6.7299s** from submit
to readable text, 48.309s to completion, and 10.9508s from chart open to readable
text. The log shows the separately requested Timeline P3 job acquiring the lane
immediately after prewarming. Sage arrived 1.20s later and Timeline yielded
28ms after that. This establishes an opportunity to disturb the shared prefix;
it does not prove KV eviction or explain the entire provider delay. Confirming
a completed prewarm is not proof that its prefix remained usable. Simply
reversing these requests would make prewarming wait behind Timeline generation;
no speculative change to chart display or queue priorities was adopted.

All eleven answers had HTTP 200, actual wire EOF, explicit `done:true`, nonempty
rendered content and successful completion. All eleven existing real browser
journeys passed in 26.411s, with zero external requests, console errors or page
exceptions. The run correctly exits 1 because five of nine warm timing targets
missed, along with the initial and chart cases. The screenshots' existing UI
"first words" footer uses the server clock; the table above uses the independent
browser submit-to-readable clock that the acceptance plan specifies.

The recurring warm misses occur in provider prefill, rather than browser
rendering or retrieval. Each baseline patient request reports the same 1094
total prompt tokens: approximately 4.0s in the retained preceding run versus
5.6s here. The old logger did not distinguish cached from evaluated tokens;
the follow-up uses Ollama's separate authoritative cached counter.
Provider/source hashes, model digest, context size and Ollama version match.
Decode rates remain similar. No varying timestamp/UUID or ambient inference
explains the baseline difference; the FHIR job overlaps only the first patient
turn in both runs. The approximate `cache_reused~` log is not an authoritative
cache counter. A single post-run swap observation is also insufficient to infer
causality. Prompts, model tier and clinical guards were not weakened to meet a
number.

A final bounded inference diagnostic compared omitted/AUTO, explicit 512 and
explicit 1024 token batches on the same installed model, with three rotated
repeats of the general and patient prompts. All 18 requests used a leading
nonce and reported zero actually cached prompt tokens. Explicit 1024 improved
median prefill by only **85.941ms general / 31.766ms patient** versus explicit
512; changing the runner incurred **2.43–8.48s** reloads. AUTO used both batch
sizes, so it cannot be treated as a fixed-512 control. The original AUTO request
posture and 8192-context/512-batch runner were restored. These intentionally
one-token requests are diagnostic evidence only, not completed answers or SLA
acceptance. No batch configuration change was adopted. See the
[complete negative batch experiment](audit-2026-10/ollama-batch-diagnostic-20261012.json).

[All timing observations and posture checks](audit-2026-10/sage-browser-latency-post-context.json)
and the
[raw evidence manifest](audit-2026-10/browser-evidence/sage-timing/post-context-20261012/evidence-manifest.json)
retain commands, source/bundle hashes, logs, successful completions and 22 PNGs.
Representative completed screenshots:
[general](audit-2026-10/browser-evidence/sage-timing/post-context-20261012/04-general.png),
[patient](audit-2026-10/browser-evidence/sage-timing/post-context-20261012/08-patient.png),
[department](audit-2026-10/browser-evidence/sage-timing/post-context-20261012/09-department.png).

### Regression acceptance

| Check | Final result |
|---|---|
| Complete curated backend suite | **16,023 passed, 8 skipped, zero failures/errors**; 16,031 collected cases in 511 files, six independent source/database copies, 327.39s wall time. |
| Separate process cleanup | **7 passed**, within the 32-test launcher/process selection; the other 25 launcher cases overlap the complete suite. |
| Frontend suite | **3,700 passed** in 366 files. |
| Browser measurement integrity | **10 passed**; synthetic harness tests qualify the clock/error/isolation checks, not model speed. |
| Real browser journeys | **11 passed**, zero skipped/flaky/unexpected; 26.411s. Timing qualification separately fails as detailed above. |
| Production frontend and lint | Build passes; lint ratchet passes with its existing eight warnings and zero errors. |
| JavaScript security | **752 dependencies, zero vulnerabilities**. |
| Documentation | **9,020 citations and 490 symbol anchors** valid. |

The final backend run repeats the entire suite after the restore fix, shared
model-context fix and fixture repairs. All 1,038 captured application, script
and CI source hashes match the tested snapshot. The eight skips are individually
retained: six cross-encoder-specific clinical-evidence cases in the deterministic
lexical unit profile, an absent direct-message fixture, and an opt-in real-appliance
backup check. Actual cached BGE inference and the real retrieval browser run are
separate checks; no live backup was used to fill that last test's premise.

Full shard logs, counts, skip reasons and source hashes are saved in
[backend acceptance](audit-2026-10/backend-acceptance-final-20261012/results.json).
Frontend/build/security evidence is in
[frontend acceptance](audit-2026-10/frontend-build-security-final-20261012.json);
launcher and measurement checks are in
[browser harness verification](audit-2026-10/sage-browser-harness-verification.json).

The clean dependency scan and passing functional suites do not establish the
requested general/patient latency targets or clinical deployment approval.
Held clinical decisions and external integrations remain untouched.
