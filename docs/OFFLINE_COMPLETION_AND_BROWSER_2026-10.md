# Offline completion and browser acceptance, 11–12 October 2026

The later [ML security and Sage latency follow-up](ML_SECURITY_AND_SAGE_LATENCY_2026-10.md)
supersedes this report's residual ML dependency status for the fresh candidate
environment. The measurements below retain their original runtime and scope;
they do not describe an upgrade of a live appliance.

## Executive assessment

At the start of this pass, the thirteen local modules had backend and component
coverage but needed browser acceptance for authentication, saving, reloading,
authorisation refusal and both visual themes. Confirmed engineering debt also
remained in HTTP wrappers, streamed failures, backup destinations/rotation and
the ASR benchmark's database isolation. The fixes and scoped acceptance below
are complete; the listed clinical, deployment and dependency limits remain open.

## Prioritised execution plan (recorded before implementation)

| Priority | Task and paths | Expected behaviour |
|---|---|---|
| High | `scripts/e2e_server.py`, `e2e/06-offline-workflows.spec.ts` | Explicit opt-in demo modules in a disposable checkout; real browser saves and reloads preserve records; denied roles cannot enter administrative modules; evidence uses synthetic data only. |
| High | `tests/visual/driver.cjs`, new offline fixtures/scenarios | Every new module has light/dark register, form and detail acceptance with strict API fixtures, console checks and saved screenshots. |
| High | Backup producer/rotation scripts and administration routes | Concurrent/same-second runs cannot overwrite archives; rotation respects verification receipts; failures retain recoverable evidence. |
| High | `server/scripts/benchmark_wer.py` | Benchmark synthetic rows can only enter an owned disposable database; seed failures abort; model registry reads do not modify appliance state. |
| Improvement | `src/api/client.ts`, six hospital-domain wrappers, Forecast/Appointments | Shared identity/session/CSRF handling retains typed module errors and conflict payloads, including text exports. |
| Improvement | Lens/Scribe routes and stream clients | Blocking materialisation leaves the event loop; stream failures have stable, sanitised machine-readable semantics. |
| Improvement | Sage history panel/sidebar | Group the bounded stored-message window into threads with their original conversation identity and local calendar date; retain old unthreaded history separately. |
| Cleanup | Admin/roster mapping parser, legacy model/time APIs; unused Vigil forecast request | Remove confirmed duplication/unread work without changing clinical rules or persisted time interpretation. |
| Improvement | Python quality/release workflow | Introduce enforceable incremental baselines and local release checks, with no publication or deployment. |

PostgreSQL needs a complete database-specific port and runnable acceptance;
R026's latest candidate remains under review. Neither can be described as shipped
by substituting a driver scaffold or importing an unaccepted candidate. Clinical
holds, external integrations, inference/training, live configuration and appliance
data stay outside this pass. Acceptance results and concrete remaining limitations
are recorded below.


## Implemented behaviour

- **Real offline workflows:** opt-in E2E modules run only in an owned temporary
  checkout with a fresh synthetic database. Eleven authenticated Chromium
  journeys cover the existing five entry points and new module navigation,
  complaint resolution/reload/history, care-plan outcomes/reload/conflicts,
  radiology report signing/locking/addenda, administrative role refusals, and
  saved reporting definitions with CSV cohort parity. Browser requests are
  restricted to loopback and these journeys do not mock API responses.
- **Response contracts:** saved reporting definitions and payout statements now
  unwrap their actual API envelopes. Fixtures use those same envelopes. A
  source review covers all 104 endpoints across the thirteen new modules in
  [the contract inventory](audit-2026-10/offline-browser-api-contracts.json).
- **Visual acceptance:** strict fixture handling rejects unknown reads and
  mutations. Clinical workflow fixtures use a doctor, with stored-role metadata;
  administrators retain clinical read-only grants. Light/dark scenarios exercise
  register, form, saved detail, revision/history, exports and suppression states.
  Shared pagination reserves the floating Sage corner. The infection
  susceptibility table has a labelled, keyboard-focusable horizontal scroll
  region and readable column widths.
- **Transport ownership:** six hospital-domain clients, appointments and forecast
  use the shared session/CSRF transport. JSON and text downloads retain structured
  permission, version conflict and MFA payloads. Streamed and uploaded responses
  are checked against their initiating account after asynchronous boundaries;
  a late refresh/refusal cannot expire the replacement account.
- **Sage history and cancellation:** the bounded latest 200-message window is
  grouped by original thread and local calendar date. Reopening resumes the
  original thread and preserves stored metadata. Legacy unthreaded messages
  remain explicitly separate. Safe-answer projection pairs the question within
  its own thread. Switching threads, starting a new chat, stopping a response,
  losing history permission and unmounting invalidate active consumers. Delayed
  microphone permissions, ASR results and speech callbacks cannot submit into
  another conversation or restart cancelled speech. Switching to text keeps
  incoming answer text while stopping speech.
- **Media and inference failure contracts:** Lens decrypt/read/base64 work runs
  off-loop without a plaintext temporary file. Lens/Scribe reject empty or failed
  drafts, close producers and release slots on cancellation. SSE errors are
  sanitised and typed; clients reject premature EOF. Local ASR distinguishes
  worker/protocol/timeout failure from genuine silence. Live-audio workers own
  temporary WAV cleanup through completion, including cancelled awaits.
- **Backup and developer isolation:** producers use unique private atomic
  destinations; producer/verification/media/offsite/rotation share a lock;
  rotation requires matching integrity receipts. Offsite copies publish their
  receipt last. The WER launcher allocates a disposable database before app
  imports, seeds transactionally with foreign keys enabled, copies installed
  model registry data privately, avoids warm-daemon reuse and cleans up owned
  state after failures or interruption.
- **Maintainability, quality and release:** shared import mapping removes duplicate
  parsing; Pydantic and UTC construction use supported APIs, with compatibility
  checks for stored timestamps and equivalent offsets. Vigil no longer makes
  an unread forecast request. Candidate verification resolves an existing tag
  to an immutable matching-version SHA, runs CI and strict security, and packages
  a checksummed frontend candidate with acceptance metadata. It does not publish
  or deploy. Strict Ruff lint/format, mypy typing and measured branch coverage
  now gate the explicitly declared two-file pure-utility scope
  (`server/routes/import_mapping.py`, `scripts/release_candidate.py`): 35
  meaningful tests cover 90 statements and 34 branches at 100%. This is an
  incremental gate, not a claim that the entire backend meets those standards.
  Security CI also audits the installed slim dependency tree and pinned tooling,
  retaining direct-runtime/ML failures.
- **Transitive dependency follow-up:** the newly measured installed dependency
  tree exposed additional parser/network advisories. Production and slim
  requirements now coordinate FastAPI 0.133.0 with Starlette 1.3.1 and constrain
  urllib3 2.8.0 and multidict 6.9.1 only when those optional dependencies are
  resolved. The prepared test environment alone was upgraded; installed appliance
  environments were untouched. The compatibility choice follows
  [FastAPI's Starlette 1 support](https://fastapi.tiangolo.com/release-notes/#01330)
  and the [Starlette form-limit advisory](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq),
  [urllib3 security release](https://github.com/urllib3/urllib3/releases/tag/2.8.0)
  and [multidict advisory](https://github.com/aio-libs/multidict/security/advisories/GHSA-54p9-h82j-f925).
  The audit also found an inherited orphan `pypdf` installation outside declared
  Orb dependencies; it is recorded separately and was not added to the project.
  Before/after scans of the prepared environment removed 11 HTTP advisory matches.
  The remaining 61 matches comprise 47 in ML dependencies and 14 in that orphan
  installation. Counts refer to package/advisory matches, not unique CVEs or an
  installed appliance audit. The direct production and test security gates
  remain failing without exclusions; HTTP constraint and quality-tool pins have
  no reported advisories.

## Verification record

| Check | Result and evidence |
|---|---|
| Frontend unit/component suite | **3,700 passed across 366 files**, no unhandled errors; includes deferred identity, thread, microphone, ASR, speech and Stop regressions. |
| Typecheck / production build / lint | `tsc -b` and Vite build pass. ESLint has **8 existing warnings, zero errors**; the budget was lowered from 9 to 8. |
| New-module visual coverage | **156 effective captures, 78 states, 13 modules, both themes; zero findings.** Exact coverage has no missing/extra/duplicate keys. |
| Existing visual scenarios | **245 effective captures, 49 scenarios; zero gating findings**, 10 existing allowlisted and 163 informational findings. No new allowances were added. |
| Browser harness regressions | **13 passed**, including actual-runner unknown GET/POST failures and role-correct fixture contracts. |
| Real browser acceptance | **11 passed in 26.5 seconds** on the final frontend and security-patched backend. CSV IDs equal the preview and rendered rows; five screenshots and the synthetic CSV are saved. |
| Media / ASR / import / timestamp backend | **1,290 passed** in the declared 23-file selection; separate focused results overlap. |
| Patched HTTP compatibility | **675 passed in 36 files**, covering transport, authentication, uploads, streams, sockets and audit. One supported TestClient deprecation warning remains. |
| Offline feature backend | **573 passed in 30 files on the patched runtime**, covering feature/integration/governance/billing/Bridge/migration boundaries. This overlaps the HTTP selection. |
| Backup / benchmark / release | **79 passed** plus **7 separately selected synthetic restore compatibility checks**; no real backup test was invoked. |
| Incremental Python quality | **35 passed; 100% statements and branches in two utility files**, strict lint/format/type checks pass; deliberate regressions are rejected. |
| HTTP security regressions | **11 passed**, including Host URL integrity, streamed form count/size boundaries and suffix file-range clamping. |
| Dependency security | Patched HTTP constraints and eight quality tools have zero reported advisories. Residual ML findings still fail the strict security gate. |
| Documentation | **9,020 citations and 490 symbol anchors** resolved after changes; shared-parser prose and out-of-range roster links corrected. |

Counts from overlapping selections must not be summed. The full historical
backend collection was not run as one suite. The visual manifest preserves
bundle provenance: 142 new-module captures retain the earlier verified bundle;
14 infection captures and 52 affected legacy/Sage captures use the final bundle.
The unchanged module states are complemented by final real-server navigation
and workflow checks. An earlier strict legacy run exposed five missing exact
fixtures; that failure is retained as superseded evidence, followed by corrected
fixtures and a clean actual gate.

Exact commands, versions, logs, hashes, scoped limitations and test selections:

- [Combined completion record](audit-2026-10/completion-verification.json)
- [Backend and authenticated transport record](audit-2026-10/backend-completion-verification.json)
- [Operations, release and quality record](audit-2026-10/operations-completion-verification.json)
- [Patched HTTP compatibility record](audit-2026-10/http-dependency-compatibility.json)
- [Independent quality gate review](audit-2026-10/independent-quality-gate-review.json)
- [Resolved dependency before/after audit](audit-2026-10/python-resolved-runtime-after-20261011.json)
- [Strict browser coverage and capture provenance](audit-2026-10/offline-browser-verification.json)
- [Real workflow screenshots](audit-2026-10/browser-evidence/real-offline-workflows/screenshots.json)

## Acceptance boundaries

The modules remain **default-off**; enabling them in the disposable demo does
not enable an installed appliance. Browser fixtures prove rendering and
interaction; the separate real-server journeys prove the listed persistence and
permission behaviours. Neither proves every endpoint end to end. All evidence
uses synthetic data; no live configuration, clinical database, uploads, model
weights or appliance backup was modified.

PostgreSQL remains deliberately unsupported until a complete dialect/schema/
transaction/concurrency port has runnable acceptance. R026's candidate remains
under review. Held clinical prescribing, substitution/reconciliation, stat-dose,
discharge/escalation, laboratory and cross-site transfer decisions remain with
the owner. External transport, including ABDM/NHCX/insurers/portals/devices, stays
excluded. No model training, model download or target-hardware inference
benchmark was performed.

The coordinated ML dependency migration and strict security acceptance remain
open; the release workflow retains those failures. Clean Linux CI execution,
target-device latency/soak, power-loss/restore/key escrow and site clinical
acceptance remain unperformed. Full-backend Python quality coverage must expand
beyond the declared incremental scope. A17's expiry/cross-tab/offline-revocation
policy and the legacy unreachable handoff print component remain documented
follow-ups; this pass does not invent that policy or delete its retained print
contract.
