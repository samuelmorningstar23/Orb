# Orb independent engineering fixes - 19-20 October 2026

This follow-up implements the concrete remaining source findings and browser-audio/performance gaps using existing product and offline-policy decisions. It follows security/UI commit `e0427fb`. It does not change clinical thresholds, signing authority, feature enablement, retention rules or external integrations.

## Implemented

- **Live dictation authorization:** current Scribe capability and patient scope precede hint lookup and audio acceptance. Hint reads run through `run_db`. Scope is rechecked before inference and publication. Replaced/retired sessions cannot receive old results; stream identifiers also guard client callbacks.
- **Bounded dictation:** at most 16 sessions, 60 seconds of buffered PCM16 per utterance, 64 KiB per input packet, one hour per session, four queued/running transcription jobs globally, two per session and two concurrent native workers. Oversize/malformed audio or overload produces a visible error and closes the recording. Disconnect does not prematurely release a still-running native worker's slot. These are technical resource limits, not clinical policies.
- **Honest model completion:** Lens and Ollama Scribe require successful HTTP status, well-formed frames, nonempty text and explicit successful `done:true`. Premature EOF, malformed frames, model errors and length truncation fail. Partial Lens output cannot be saved as successful analysis; its unregistered upload is removed. Provider cancellation closes the provider before releasing its inference slot; MLX provider failures propagate instead of becoming prose or empty success.
- **Private speech hints and files:** patient-derived hints use a bounded stdin envelope, never subprocess arguments. Legacy argument hints are refused before model lookup, and failed worker stderr is not copied into logs. Worker failures remain typed failures, including the HTTP chunk route, while genuine silence remains valid. Private WAV conversion is shared by the daemon and worker, cleans failure/interruption and preserves caller-owned source audio. The benchmark uses the new protocol.
- **Local inference destinations:** configuration allows only HTTP(S) loopback model origins, rejects remote/wildcard/credential-bearing/malformed addresses and validates again at gateway/provider use. This enforces the existing offline rule; it does not add a remote model connection.
- **AudioWorklet capture:** replaces deprecated main-thread ScriptProcessor PCM/VAD. The audio thread retains speech while confirming VAD, splits continuous speech below the server limit and emits silent speaker output. Receipt-based message credits bound delivery; a stalled queue fails explicitly. Stop drains captured PCM before the final server flush. Late microphone permission, changed chart/language, unmount, startup failure, worklet failure, delivery timeout and disconnect release capture resources.
- **Vigil inference reuse:** a bounded in-memory cache is keyed by patient, outcome, complete freshly assembled feature/time tensors, normalization metadata, model identity and parameter/buffer versions. Age/history/time corrections, changed model state and readmission inputs therefore cannot reuse an unrelated result. Returned predictions are copied; failures are not cached. NEWS2 and existing overlay thresholds are unchanged. Historical `risk_scores` lack sufficient provenance and are not substituted for the displayed row.
- **Incremental quality:** an independent CI job adds Ruff formatting/lint, strict mypy and 100% statement/branch coverage for three pure inference utilities. Its pinned developer requirements install separately from the appliance runtime. This is a measured scoped gate, not whole-backend compliance.

The selected commit also includes the directly required frontend build prerequisites: explicit Node test types, two mock-call annotations and the current Vite/Rolldown minification/discovery configuration. It includes the directly required ASR/private-file/stream-error prerequisites and regression tests already present in the working tree. Other concurrent admission, pharmacy, infrastructure and UI changes are excluded. Source-only selected files are verified against a fresh HEAD export before staging; the live database, uploads, keys and weights are not used by tests.

## Verification

The machine-readable record, logs and source hashes are in [the evidence package](audit-2026-10/autonomous-fixes-20261019/verification.json). Selections overlap and must not be added together.

- **279 passed:** clean HEAD source export plus exactly the selected repairs. Covers authenticated Socket.IO scope refusals, bounded worker concurrency, stream identity, Lens persistence/cleanup, Scribe completion/failure contracts, private speech files, model/input cache invalidation and prior security/scoring compatibility.
- **951 passed:** broader disposable-copy backend selection before the final Scribe-provider completion refinement. Its later delta is recorded separately in the evidence, not presented as a new full-backend run.
- **87 passed in 12 frontend files:** hooks, worklet, Scribe and Lens components.
- **49 passed:** pure utility gate; all 65 statements and 20 branches covered, with strict typing, lint and formatting passing. Integration cases are explicitly excluded from this pure selection and executed separately.
- **8 passed:** real-browser Scribe/responsive selection plus the initial built AudioWorklet check. **Two final audio cases passed** after stop/drain refinements: silence completion and explicit stop. These overlap the earlier audio check; both verify PCM/VAD and zero speaker echo using synthetic audio.
- Typecheck, production build and targeted ESLint passed. The selected frontend's independent typecheck/build and lifecycle/worklet results are recorded in the evidence.

Application tests run on owned disposable synthetic databases/copies. Browser audio is generated, not recorded from a person; no inference integration is needed for those tests. Existing environment deprecation warnings do not establish a new dependency upgrade or a hosted CI result.

## Still left

- **Owner/clinical decisions:** expiry/renewal and cross-tab/offline revocation policy; time-window retention; held medication, discharge, escalation, laboratory, restricted-record and break-glass rules; four separately ruled Sage bed-locator follow-ups. Clinical train/serve feature definitions and checkpoint-promotion gates require approved definitions and validation.
- **External integration work:** PostgreSQL backend port/acceptance environment; ABDM, insurer/claims, payment, PACS/DICOM, analyser/device and external-reporting connections, consistent with offline policy.
- **Site/native acceptance:** approved model/data validation, native speech accuracy and target-browser/device acceptance, cold/chart-preparation and busy-ward latency, appliance runtime rebuild, restore/key escrow, power-loss, backup-under-load and hardware soak. These cannot be certified by synthetic unit tests.
- **Ongoing engineering backlog:** full-backend strict typing/coverage adoption and large-module extraction remain incremental work. Hosted CI/release acceptance has not been run. This follow-up closes the named concrete defects; it does not claim to finish every architectural improvement or HIS roadmap epic.
- **Adoption and product scope:** pilot ownership, training, sign-offs and deliberate enablement of the 13 default-off local workflows; gated multi-site/transfer surfaces and later Pulse stages.

The updated PDF retains the prior work summary, adds these repairs and separates the remaining work. Nothing here represents production deployment or clinical approval.
