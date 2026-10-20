# Orb engineering follow-up - 20 October 2026

This follow-up builds on `fa1159a` and addresses additional local reliability,
maintainability and verification work. It does not change clinical decisions,
enable held modules, connect an external system or deploy Orb. It is not a claim
that every engineering backlog item has been completed.

## Implementation

- Extracted security-header middleware, escaped SPA bootstrap rendering and
  model-warming policy into three independent typed modules. Main retains its
  compatibility imports and operator-unload seam. Streaming remains unbuffered;
  route-specific headers and existing HTTPS/HSTS policy are preserved.
- Warming budgets reject non-finite values; retry backoff caps the exponent before
  computing it. Model selection deduplicates without reordering and respects
  resident and operator-unloaded models.
- Extracted 98 public API wire interfaces/type aliases into `src/api/contracts.ts`.
  Existing exports remain available from the client; contracts add no runtime
  dependency. The selected commit preserves prior uncommitted contract changes
  as working-copy changes rather than absorbing them.
- Extracted bounded SSE framing with CR, LF and CRLF support across chunks, fatal
  UTF-8 decoding, explicit incomplete-event failure, event size limits and reader
  cancellation/release. Protocol tests exercise framing, encoding and cleanup.
- Removed unreachable legacy substitution suggestions after the active exact
  molecule/strength/form/confirmed/in-date-stock checks. No new substitution
  policy was introduced. Fixed lazy annotation bindings, duplicate imports,
  malformed lint annotations and worker readiness checks. Debug model probes use
  only installed local files.
- Restore drills and standby checks now honor an explicitly selected interpreter,
  refuse an invalid explicit override, and read modification times portably through
  Python instead of platform-specific `stat` flags. Synthetic restore tests use
  the running test interpreter. CI requires encryption/QR/SQLCipher dependencies
  before measuring coverage. Existing backup/key/verification policy is preserved.
- Closed the five ambient whole-word prefix regression cases while preserving
  clinical inflections and intentional stems. The old admission-volume/retraction
  measurements remain historical; this is not a new busy-ward latency benchmark.
- Action outcomes now use the originating assessment lane and episode, including
  delayed decisions. Ambient chat keeps its existing fallback. Background Vigil
  drafts retain their true timings but no longer enter the clinician time-saved
  count or latency distribution. These close two pinned shadow-metric defects.
- Relay rejects foreign-room socket traffic and obsolete room/send responses.
  Changing rooms clears that room's composer/send status; returning to a room
  creates a new ownership generation. Old responses, refreshes and failures cannot
  repaint the new room or settle its composer. The two known UI regressions are
  now ordinary passing tests, with additional room/return/send coverage.
- Repaired acceptance fixtures: relative simulated-result dates, synthetic QR
  encryption keys, successful Lens terminal frames, an independently owned
  census for absent-patient assertions, unique stocked medication identities,
  explicit two-model premises, declared-global teardown inspection, void-returning
  mock reset callbacks and FileReader-based Blob assertions in jsdom. These
  preserve the real product contracts. Lexical-store tests seed their actual FTS
  corpus explicitly and never load cached models; sockets use the installed real
  manager instead of an incomplete library test double.

## Quality checks and reproducible acceptance

`tools/backend-quality` inventories every production Python file and fingerprints
legacy lint/type diagnostics by source expression. Missing annotation slots are
budgeted per function. New diagnostics or new untyped functions fail; critical
syntax, undefined-name/control-flow and duplicate-binding findings cannot be
allowlisted. Changed unformatted legacy files must be formatted after this initial
reviewed adoption snapshot.

The initial baseline records both the selected commit and the larger concurrent
working copy. Their approved formatter hashes and maximum diagnostic counts are
explicit. This allows the same adoption gate to inspect either source without
committing unrelated product work. This is a regression gate, **not elimination of
existing debt or full third-party typing**. Baseline updates require review.

The disposable backend runner copies source and built assets while excluding live
configuration, databases, uploads, backups, model weights and runtime/evaluation
data. Each shard owns its database and caches. It records commands, source hashes,
JUnit results and branch coverage. Pinned developer tooling stays separate from
the installed appliance runtime. CI wiring has been authored; hosted execution
has not been performed here. The broad source runs use a fresh isolated candidate
test runtime built from the working-copy requirements; its package freeze is
recorded. Selected application dependency pins and the installed appliance runtime
remain distinct, so these results do not establish hosted-CI/runtime parity.

The independent strict utility gate now covers six modules, with no diagnostic
baseline: lint, formatting, mypy strict, 100% statements and 100% branches.

## Verification

The machine-readable evidence is
`docs/audit-2026-10/engineering-20261020/verification.json`. Counts overlap and
must not be summed as unique checks.

- Complete concurrent working-copy backend: **17,410 passed, 8 skipped** across
  six disposable shards plus serial cleanup tests; no failures/errors. The eight
  skips are one real encrypted-backup exercise, six cross-encoder relevance
  checks without model assets, and one direct-channel test without seeded direct
  channels. Synthetic execution does not qualify any of those skipped behaviors.
- Complete working-copy frontend: **3,722 tests passed in 369 files**.
- Exact selected-commit focused backend: **1,150 passed**, including extracted
  infrastructure, Lens, encrypted discharge QR, live scope, simulated results and
  the full bed-locator selection.
- Final selected frontend: **3,252 tests passed across 334 files**, with no
  expected-failure markers; typecheck and production build passed. The earlier
  3,243-pass/two-expected-failure snapshot preceded the Relay fixes and is
  superseded for those behaviors. Scratch exports missing server/visual fixtures
  or `.d.mts` declarations were corrected before the final accepted run.
- Final selected backend defect selection: **696 passed, 1 skipped**, covering
  all Relay/ambient/shadow/wedge modules. All seven previously expected backend
  failures are ordinary passing tests after the final three product fixes. Those
  three production files exactly match the green working-copy snapshot too.
- Acceptance-fixture checks: **463 passed, 6 model-dependent skips**; a separate
  aggregate/restore selection passed **55 with one real-backup skip**. Counts
  overlap the broad runs and must not be added.
- Strict utilities: **67 passed, 4 integration cases deselected**; **122 statements
  and 34 branches, 100% covered**. These are pure utility tests, not a full app run.
- Quality-checker/runner regression tests: **24 passed**. Checker strict typing,
  tool lint/format and full-backend critical lint passed.
- Working-copy backend branch report: **88.37% statement coverage; 80.18% branch
  coverage**, over 86,983 statements and 28,504 branches in 384 measured modules.
  Namespace packages are included; scripts/tests/evaluation data are excluded.

The broad selected backend coverage cohort before the final three defect fixes
finished with **16,139 passes, 11 explicit skips and 7 expected-failure cases**.
Two failed shards were rerun in full after census-fixture corrections; passing
shards were retained with byte-identical production code. The seven expected
cases belong to the ambient/provenance/proxy defects subsequently fixed and
verified by the final 696-test selection. This is not one monolithic all-green
invocation of the final tree. The broader working-copy run already contained
those exact three production-file fixes, with their hashes verified.

## Still open

The selected-commit inventory contains **7,835 lint diagnostics, 996 typing
errors, 4,117 missing annotation slots and 371 files requiring formatting**. The
concurrent working-copy inventory is larger (434 production Python files versus
387): 8,499 lint diagnostics, 1,081 typing errors, 4,983 missing annotation slots
and 417 unformatted files. The baseline prevents regression; it does not make
these counts zero. Further cleanup and extraction of configuration, migrations,
large services and panels remain engineering work. A20/A21 are partial, not closed.

Other remaining work needs policy/clinical rulings, model/data qualification,
external systems or a target appliance/site: session renewal/offline revocation
policy; held medication/clinical workflows and the four separate bed-locator
rulings; train/serve benchmark and checkpoint promotion definitions; busy-ward
GPU/concurrency measurement; native speech/Safari/iPad acceptance; PostgreSQL and
hospital interfaces; hosted CI/release runs; restore/key-escrow, signed-media,
power-loss and backup-under-load drills; and pilot ownership, training and
approved module enablement. The updated PDF preserves those boundaries.


## Legacy lint and typing follow-up — 20 October 2026

This follow-up supersedes the legacy diagnostic counts above for the reviewed
source at parent `59ee1bc` (including committed HIS changes). Across 398 production
Python modules, Ruff findings dropped from 8,314 to zero, mypy errors from 1,051
to zero, and unformatted files from 382 to zero. The lint/type/format budgets are
now empty. Frontend ESLint dropped from one error and nine warnings to zero;
TypeScript and the production build pass. Existing concurrent feature edits are
preserved outside the selected commit.

The full disposable final backend selection passes 16,773 tests with 11 explicit
skips; the frontend passes 3,481 tests across 357 files. The 24 quality-tool tests,
67 strict utility tests and all 9,028 documentation citations / 490 symbol anchors
pass. The final 520-test boundary selection also passes. Counts overlap and must
not be added. Exact manifests, hashes and logs are in
`docs/audit-2026-10/legacy-lint-types-20261020/verification.json`.

Type fixes retain lazy import barriers, FastAPI authorization dependency
construction, enum display strings, valid optional receipt fields and safe
external-input errors. Added regressions cover authorization-source inspection,
verified-backup retention and invalid/missing procurement and clinic inputs.

**Still open:** 4,334 missing parameter/return annotation slots remain under the
separate adoption gate (starting inventory: 4,345). This is zero configured mypy
errors, not full strict annotation coverage. The per-module coverage budget check
still fails: the starting commit already exceeds older budgets, and cleanup adds
uncovered defensive guards and changes statement counts. Limits are unchanged;
`coverage-budget-findings.json` records both cohorts. Further service decomposition,
optional SDK typing, hosted CI/release execution and the owner/site/integration
work listed above remain. No deployment, live appliance change or full CI success
is claimed.

After the full run, a final whitespace-only Relay SQL edit passed the complete
Relay selection: 163 passed, one explicit skip. Other production files remain
byte-identical to the full-run source; this final delta is recorded separately.
