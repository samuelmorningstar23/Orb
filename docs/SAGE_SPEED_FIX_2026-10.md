# Sage prompt-cache performance fix, 12 October 2026

## Goal and boundaries

Measure **Send to first browser-visible readable answer**, with strict targets
of general <3 seconds and patient <5 seconds. Retain full successful completion,
initial startup, all repeats and every miss. Department queries remain a <10-second
regression check. The same cached offline `medgemma:27b` model, thinking mode,
clinical safety floor, prompts, chart facts and action gates remain in use.
Held clinical decisions and live appliance data are unchanged.

The [preceding security and timing report](ML_SECURITY_AND_SAGE_LATENCY_2026-10.md)
retains the failed browser baseline and previous rejected optimizations. This
continuation tests a distinct cache-storage defect; no historical miss is erased.

## Final browser result

The unchanged browser harness passes **9/9 resident-model speed checks**, all
**11 complete answers**, and **11/11 real browser workflow journeys**. Measured
from Send to the first visible answer containing at least 12 characters and two
words, the three fixed-sequence repeats are:

| Category | First readable, seconds | Strict target | Result |
|---|---|---|---|
| General | 1.631 / 0.723 / 0.969 | <3s | 3/3 pass |
| Patient | 3.545 / 3.532 / 3.416 | <5s | 3/3 pass |
| Department | 5.377 / 0.432 / 0.429 | <10s | 3/3 pass |
| Separate chart-prepared patient | 2.039 | <5s after Send | Pass |

This qualifies an **idle synthetic local workload** on M2 Max /64 GiB, Ollama
0.35.1, the unchanged cached `medgemma:27b` digest, one 8192-token slot and actual
AUTO batch 512. Real cached BGE-M3/reranker snapshots and a new 19-document/vector
corpus were used. Thinking, the clinical safety floor and the careful 27B model
were enabled for every turn, with no downgrade. Automatic startup model jobs
were disabled only in this disposable benchmark; ordinary maintenance and the
chart's Timeline job remained enabled. Busy-ward concurrency is not qualified.

**Startup and chart preparation remain limitations.** The initial nonresident
general answer took **19.982s** to readable text and 24.215s to complete, and is
retained as a cold miss. The separate chart case waited **23.255s** for prefetch:
the existing Timeline P3 job occupied the background lane for 21.854s before
the 470ms prefix warm could run. Chart-open to readable answer was **25.409s**,
despite Send to readable being 2.039s. This pass does not establish a universal
3s/5s guarantee from cold startup or chart opening. Queue priorities, clinical
Timeline behavior and held decisions remain unchanged.

Warm full completions were general 4.996–5.973s, patient 36.278–47.260s and
department 17.931–25.623s; the separate chart answer completed in 33.977s.
Every turn had HTTP 200, valid stream framing, explicit terminal success,
nonempty content and no error or length truncation. There were no page errors,
console errors or attempted external browser requests. All repeats and the cold
miss are retained in the [final timing and screenshot evidence](audit-2026-10/browser-evidence/sage-timing/cache-save-20261012/README.md).

Both post-department general turns restored **696 of 697 tokens** and evaluated
one in 64–65ms. Patient turns used 573 cached plus 432 evaluated tokens from a
1005-token prompt. The preceding run's synthetic patient prompt had 1092 tokens;
the same questions and current code were used with fresh disposable seed data.
The raw timings therefore establish this run's targets, while the separate
fixed-prompt control establishes the cache mechanism.

The owned daemon and browser/backend workers exited cleanly, its port closed,
all 33 cached model files retained size and modification time, and the shared
Ollama service remained idle and unchanged. Live `.env`, appliance records,
uploads and model weights were not modified. The profile applies when Orb starts
its own daemon; an already-running service remains unaffected.

## Confirmed cause and controlled comparison

Ollama 0.35.1 uses llama.cpp b11232. The cached Gemma3 model has a 1024-token
sliding attention window. With compact SWA cache storage, a request whose common
prefix precedes the retained window must restore an earlier checkpoint and
process the remaining prompt again. After a full general answer, the next
patient request reused only **184 of 1092 total prompt tokens**, although the
actual shared prefix allowed **573** tokens to be reused with full storage.
The default runner log records the checkpoint rollback explicitly.

`LLAMA_ARG_SWA_FULL=1` retains the complete cache for rollback. The model's
sliding attention mask remains unchanged. This changes storage, without
changing clinical instructions, model weights, sampling, output budgets or
the queue's single inference owner.

Two separately owned loopback processes used the same synthetic Sage prompts
and completed all eight G/P/G/P answers, with explicit terminal `stop` and no
truncation or errors. Only the SWA storage setting varied:

| Diagnostic | Compact storage | Full storage |
|---|---:|---:|
| First patient request, first model content | 5.928s | 3.395s |
| Second patient request, first model content | 5.930s | 3.462s |
| Warm general request, first model content | 3.798s | 0.944s |
| Patient reused / evaluated prompt tokens | 184 / 908 | 573 / 519 |
| Actual global + SWA cache allocation | 640 + 624 MiB | 640 + 3328 MiB |

These diagnostic clocks include the raw model marker and **are not browser
acceptance**. Initial requests retain model load and cold prefill separately.
The additional cache costs 2704 MiB on the tested M2 Max /64 GiB /8192-context
configuration. A point memory-pressure observation reported 51% free while
the full-cache model was resident; this is not a continuous memory peak.

All 33 existing model-cache files retained their sizes and modification times.
The shared Ollama service was never restarted or loaded; its model list was
empty before and after. Both owned processes exited normally and their port
closed. [Complete control evidence](audit-2026-10/sage-swa-cache-control-20261012.json)
includes raw answers, counters, runner logs, commands and primary source links.

## Saved-state defect and the final cache policy

Full SWA storage alone fixes the patient case but does not retain the general
prefix across department turns. The [real browser intermediate](audit-2026-10/browser-evidence/sage-timing/full-swa-20261012/README.md)
completed every answer and all 11 workflow journeys, but passed only 7 of 9 warm
targets: general **1.452 / 4.906 / 4.878s**, patient **3.987 / 3.981 / 3.990s**,
department **5.228 / 0.315 / 0.349s**. Chart first-readable was **3.810s**; the
initial cold diagnostic was **20.141s**. These misses remain in the raw evidence.

Pinned llama.cpp serializes only the recent SWA window into its RAM prompt
cache, even with full live storage. After a long patient answer, the saved
state's earliest SWA position can be later than the general shared prefix.
The engine finds a matching prompt, restores it, then explicitly forces a full
re-evaluation: both failed general turns reused **zero of 697 tokens**.

The final policy retains one daemon slot and sets supported
`slot-prompt-similarity=0.6` through an Orb-owned INI. General-to-patient
similarity is approximately 0.525, so that transition now saves the short
general state before replacing it. Returning from department can restore that
state with usable history. This changes cache retention; the model, attention
mask, clinical prompts and existing interactive/background scheduling remain
unchanged.

A [full-answer G/P/D/G diagnostic](audit-2026-10/sage-cache-save-control-20261012.json)
confirmed the mechanism: the final general turn reused **696 of 697 tokens**,
evaluated one and produced raw engine content in **0.141s**. All four answers
completed normally. Its AUTO batch was 1024 versus 512 in the preceding browser
run, so these clocks are not a paired latency comparison or browser acceptance.

A [two-slot prototype](audit-2026-10/sage-two-slot-rejected-20261012/manifest.json)
was rejected before any model call or production promotion. Orb has independent
interactive and background lanes; allowing two daemon requests could change
their concurrency. The adopted profile retains daemon parallelism of one.

## Implementation and verification

The provider now logs authoritative total, cached and actually evaluated prompt
counts. Older engines with missing counters report `unknown`; malformed optional
diagnostics cannot break an otherwise completed stream or leak record content.
Thirty-one focused provider/stream tests pass, including ten new metric cases.
[Captured provider evidence](audit-2026-10/provider-metrics-acceptance-20261012/verification.json)
includes the exact command, log and source hashes.

The canonical [local launcher](../scripts/serve-ollama.sh) applies the profile
uniformly to the daemon's callers. `ORB_OLLAMA_SWA_FULL=auto` enables full storage
only for the tested Ollama 0.35.1 /Darwin arm64 /at least 64 GiB profile. Explicit
`on` requires the same qualification; conflicting settings fail before launch.
Other hosts retain their defaults. The launcher uses loopback, cloud-disabled
Ollama, no startup pruning, one request per model and f16 cache. It contains no
pulls and never stops or reconfigures another daemon.

The optimized branch uses only the committed
[runner configuration](../config/ollama-xdg/llama.cpp/config.ini), with exact
content validation before launch. Its daemon-local `XDG_CONFIG_HOME` preserves
`HOME` and the model directory. Existing user/system configuration, relative
roots, dangling links or uninspectable locations prevent automatic optimization;
explicit `on` refuses to replace them. Off/unqualified operation preserves the
original configuration environment. Qualified execution requires separate KV
storage and an 8192 MiB RAM prompt cache; conflicting overrides fail clearly.

`setup-mac.sh` and the normal `run-orb.sh` entrypoint use this launcher for a
daemon they start; an existing daemon is explicitly reported as unaffected.
App startup uses the backend's configured loopback endpoint, including explicit
HTTP default-port mapping, and refuses to probe remote or malformed addresses.
Private startup logs and bounded cleanup cover only the owned daemon. An
interruption exits before launching the backend; the optional AI daemon cannot
hold the app's final wait open. AI startup failure still permits the app to boot.
The setting must be present when starting
Ollama. Editing the backend `.env` cannot apply it to an already-running daemon.
See [setup instructions](SETUP.md#orb-owned-offline-daemon-and-prompt-cache-profile).
The documented `off` setting does not override independently enabled llama.cpp
`config.ini` settings. Cloud-disabled Ollama is not an operating-system firewall.

The preceding combined launcher, installer, app-startup and ops-health stage
passed **110 tests** (50 launcher, 20 app-startup, 15 installer and 25 ops-health).
This includes an external-daemon stand-in remaining alive, failure cleanup,
conflicting flags, version/hardware guards, endpoint validation and interruption.
[Historical entrypoint evidence](audit-2026-10/ollama-entrypoints-acceptance-20261012/verification.json)
retains the command, stdout, exact membership, source hashes and earlier stage.

The final cache-policy refinement passes **160 focused tests** in 110.14s:
100 launcher, 20 app-startup, 15 installer and 25 ops-health. The 50 additional
cases cover exact INI content and absence, original user/system configuration,
dangling/uninspectable paths, KV/RAM conflicts and preservation on skipped
profiles. Shell syntax and whitespace checks pass. The first scratch invocation
was refused by the test database safety guard before assertions; its log is
retained, and the successful run used a fresh compliant owned database with
unchanged source. [Final focused evidence](audit-2026-10/ollama-cache-profile-acceptance-20261012/verification.json)
records all commands and hashes. These cases overlap earlier acceptance and are
not added to the backend suite as unique tests.

The first full-suite snapshot exposed catalogue contamination between two tests:
a stock-rule fixture's `5000 units/mL` brand reached the unrelated closed-prose
vocabulary invariant. The vocabulary module now uses the established isolated
module-database fixture. All clinical assertions and production lexicon/resolver
remain unchanged. The predecessor and vocabulary modules plus an ephemeral
catalogue-restoration proof pass **115 checks**; the earlier failed snapshot is
[retained](audit-2026-10/backend-sage-speed-pre-startup-20261012-v7/verification.json).

The [six-shard backend application suite](audit-2026-10/backend-sage-speed-acceptance-20261012/results.json)
passes **16,103 tests**, with **8 explicit skips** and no failures or errors
from 16,111 collected cases in 514 files (365.21 seconds). Six relevance cases
use the suite's offline lexical setup; encrypted-backup and direct-channel
checks lack their stated fixtures. Seven process-cleanup cases are separately
verified and excluded from the parallel run, as recorded in the manifest.

That immutable v8 run preceded the final launcher/INI refinement. Independent
[parity and delta verification](audit-2026-10/backend-sage-speed-runtime-delta-20261012.json)
compared 1283 application files and 181 built frontend assets with zero drift.
The isolated launcher/config/test delta passed the 160 focused checks above and
the final real-browser run. Earlier source hashes and manifests retain their
historical meaning; no failed evidence was overwritten.
