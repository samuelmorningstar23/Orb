# Gap review, 19 October 2026: what the October passes built, what is left, what is still open

Written from the working tree on 19 October 2026 against the last commit on main, `04e176e` (HIS wave 8, 10 October). Everything below was checked in code today, not copied from the work logs. Where a log claims a fix, the file was opened to confirm it.

## 1. Where the tree stands

**Update, 20 October 00:30.** Four commits landed on main overnight: `f5a0afb` and `ffeac66` (Sage bed locator R026, attempt 13f.2, shipped after fourteen reviews, with four items left for rulings), `e0427fb` (the security and UX remediation, 23:28) and `fa1159a` (the remaining source-only inference and dictation findings, 00:07). 566 paths still differ from main. `server/requirements.txt` at HEAD still carries the old pins; the dependency upgrade is in the working tree only, so main as committed is not green against its own pins (section 3.2). The rest of this section describes the tree as it was at 22:30 on 19 October.

- 498 paths differed from main: 272 modified, 224 new, 2 deleted. The tracked diff alone is 13,221 lines added and 3,949 removed. The new files add thirteen modules, six migrations (81 to 86) plus m0072, 56 backend test files, 24 screens and components, and 56 evidence folders and manifests under `docs/audit-2026-10/` (757 files).
- Nothing from 10 to 13 October is committed. The repository has no remote, so the three GitHub workflows the pass added (`ci.yml`, `security.yml`, `release-candidate.yml`) have never run anywhere.
- The work came in four passes, each with its own record: the repository audit (`docs/REPOSITORY_AUDIT_2026-10.md`, 11 October), the offline module build and its completion (`docs/OFFLINE_IMPLEMENTATION_2026-10.md`, `docs/OFFLINE_COMPLETION_AND_BROWSER_2026-10.md`), the ML dependency upgrade and Sage speed work (`docs/ML_SECURITY_AND_SAGE_LATENCY_2026-10.md`, `docs/SAGE_SPEED_FIX_2026-10.md`), and a security and browser assessment (`docs/SECURITY_AND_UX_ASSESSMENT_2026-10.md`, 12 October). The a16z and Speedrun decks under `docs/fundraising/` date from 13 October and are separate.
- The HIS build lanes had already left wave 9 in the working tree before these passes started (clerk admission, strict locations, live bed board, presence lanes, lifecycle screens, ward returns, crash carts, consent at registration, terminology loader, facility identity and users). The audit preserved it. There is no "wave 9 verified" commit, so wave 9 is built but unverified by the lane's own verifier.

## 2. What they built, confirmed in code

### 2.1 Repository audit pass (A01 to A34)

Thirty of the thirty-four matrix items are fixed in the tree. The ones that matter most:

- Request bodies are counted on the ASGI receive path (`server/request_limits.py`), with per-route multipart caps for Lens, Scribe, roster and history import.
- An offline chart cache is evicted on a 401, 403 or 404 and cannot reappear on remount (`useOfflineSnapshot`, `phiCache`).
- Offline replay and refresh retries stop when the signed-in user changes.
- Media writes stage privately, fsync and replace atomically; Whisper and the live subprocess fallback own their temp files.
- `/api/ready` checks the schema and migration ledger off the loop.
- Proactive insights with a null recipient no longer leak names across departments (A33); manual evaluation is scoped and rejects malformed bodies.
- Relay cross-room races (A25), outcome-lane attribution and the time-saved proxy (A26), ambient-gate prefix matches (A27) and the downtime log that printed names and MRNs (A29) are fixed.
- `src/constants.ts` and `src/lib/a11y.ts` are deleted; nothing imports them.
- README, SETUP, OPERATIONS and the docs index now describe the Ward Record product and offline Pulse.

Still open from that matrix: A17 (an expired JWT cannot refresh; cross-tab and offline revocation policy undecided), A20 in part (Python lint, typing and coverage gate only two files; hosted CI never run), A21 (main.py extraction, hardware soak, SQLite single-writer limits unmeasured), A23 in part (`HandoffPrintView.tsx` unreachable but kept).

### 2.2 Thirteen new default-off modules

Feedback, mortuary, CSSD, reporting, finance tools, radiology, care plans, emergency, perioperative, dialysis, record governance, radiation safety and infection surveillance. Each has a router mounted in `server/main.py`, a service, a migration (81 to 86), an API client, a panel wired into `src/shellConfig.tsx`, a module switch (`ORB_FEEDBACK` and so on in `.env.example`), permissions in `server/permissions.py` and `src/lib/roles.ts`, backend tests, visual fixtures in both themes and a real-browser Playwright spec (`e2e/06-offline-workflows.spec.ts` with `scripts/e2e_server.py`). All of it was exercised on synthetic data only; no hospital acceptance.

Beside the modules: legal holds now protect erasure at adjudication and again at execution; patient exports slice shared journals through patient-owned rows; Bridge medicines, vitals and discharge readers use the current stay (R029); the OPD counter writes charges, invoice, number and audit in one transaction.

### 2.3 Completion pass

Sage history is grouped by thread and local day; six hospital-domain clients plus Forecast and Appointments share the cookie and CSRF transport; Lens materialisation is off the loop and stream failures are typed; backups publish atomically under one lock with integrity receipts before any rotation; the WER benchmark runs in a disposable database; `scripts/release_candidate.py` packages a checksummed candidate without publishing. Dependencies moved to FastAPI 0.133 with Starlette 1.3.1, Torch 2.14.1 and Transformers 5.19.0; FlagEmbedding, sentence-transformers and Accelerate are gone, replaced by `server/services/local_transformers.py`.

### 2.4 Sage speed

`scripts/serve-ollama.sh` is the owned loopback daemon launcher with a full sliding-window cache profile gated to Ollama 0.35.1 on Darwin arm64 with 64 GiB, and a committed runner config under `config/ollama-xdg/`. Chart-open prefetch had a patient-access bypass (`sage.query` alone sufficed); it is scoped now. Warm browser timings meet the targets (general under 3 s, patient under 5 s, department under 10 s, three of three each). Two misses remain and are recorded: a cold start of about 20 s, and a chart opened before the prefetch has finished waits 23 to 25 s.

### 2.5 Security and browser assessment

Eight security findings and four browser findings, reproduced with scripts under `docs/audit-2026-10/security-assessment-20261012/`. The document says plainly that it is an assessment, not a remediation. Section 3.1 confirms that none of the twelve has been fixed since.

## 3. Gaps and holes still in the tree

### 3.1 The twelve assessment findings: all present at 22:35, remediation committed at 23:28

Checked file by file at about 22:35 on 19 October; the table below is that baseline. A separate session then fixed them in the main checkout and committed the result as `e0427fb` at 23:28 (58 files, with `server/tests/test_security_remediation.py` as the acceptance list and a remediation table added to the top of the assessment document). `fa1159a` at 00:07 closes the four remaining source-only findings (live Scribe scope and bounds, explicit Lens completion, private ASR hint transport, loopback-only inference destination). Verified here only this far: on the shipping venv, 45 of the 48 remediation tests pass and the 3 encrypted-QR tests fail because HEAD's pins lack `pypng` (section 3.2). The row-by-row fixes were not re-read; run the remediation file on the pinned environment before calling any row closed.

| ID | Finding | What the code still does |
|---|---|---|
| SEC-01 P1 | Scribe mutations skip the scoped read | `_note_in_scope_or_404` (scribe_routes.py:582) is called only from two GET handlers (661, 690). PATCH `/notes/{id}` (781), `/sign` (843), `/reject` (949), `/revise` (990) and `/verify` (703) do not call it. Revise copies another author's transcript. |
| SEC-02 P1 | Connected sockets outlive revocation | `save_session` at realtime.py:382 stores id, name, role and department only; no token version, expiry or device. Rooms keep publishing vitals after `token_version` changes. |
| SEC-03 P1 | Revoked tablet token still opens a socket | Socket auth checks user and token version (realtime.py:234) but never the enrolled-device row that HTTP checks (auth.py:513). |
| SEC-04 P1 | Ward downtime pack widens scope | `ward_pack` (downtime_routes.py:104) scope-checks the anchor patient, then serves the whole physical ward, boarders included. |
| SEC-05 P1 conditional | Restore media extraction escapes the upload subtree | standby-restore.sh:323 still runs `tar -xf "$MEDIA_TAR" -C "$(dirname "$UPLOADS_DIR")"` with no member validation or staging. |
| SEC-06 P2 | TOTP step consumed twice under concurrency | `consume_totp` (auth_routes.py:305) is SELECT, compare, UPDATE with no conditional update or write lock. |
| SEC-07 P2 | Lens keeps orphan uploads | lens_routes.py writes the encrypted upload (line 226) before `enforce_patient_scope` (line 245); denial and failed streams leave the file. |
| SEC-08 P2 | Discharge QR written plaintext, mode 0644 | `generate_qr_code` (discharge_service.py:797) calls `img.save` directly, bypassing the encrypted media writer. |
| UX-01 P1 | Mobile admission form obscured | `AdmitPatientModal.css` has no media query; the 280 px summary (line 329) stays beside the form at 390 px. |
| UX-02 P2 | Radiation safety clips on mobile; the orb overlaps a label | `RecordGovernancePanel.css` has no mobile layout. |
| UX-03 P2 | Admission dialog loses focus on close | `useFocusTrap` captures `document.activeElement` after the dialog's autoFocus input already has it (line 91). |
| UX-04 P2 | Self-service MFA entry hidden on mobile | `.shell__user-card` is `display: none` at 768 px or less (ClinicianShell.css:1083); no other trigger. |

The seven source-only concerns in the assessment (Scribe structure accepting a foreign patient id, presence rooms without patient scope, live Scribe without capability or byte caps, Lens accepting a stream with no `done` frame, ASR hints in subprocess argv, no loopback restriction on `ORB_OLLAMA_BASE_URL`, presence disconnect cleanup overwritten by the Scribe handler) are also untouched.

### 3.2 The shipping Python environment does not match the code

`server/.venv` is what `run-orb.sh` starts. Against the pins in `requirements.txt` and `requirements-dev.txt` it is behind on nine packages and missing two:

| Package | Installed | Pinned |
|---|---|---|
| torch | 2.6.0 | 2.14.1 |
| transformers | 4.49.0 | 5.19.0 |
| fastapi | 0.115.12 | 0.133.0 |
| cryptography | 44.0.0 | 50.0.2 |
| pyjwt | 2.10.1 | 2.15.1 |
| python-socketio | 5.12.1 | 5.16.4 |
| python-multipart | 0.0.20 | 0.0.31 |
| pytest / pytest-asyncio | 8.3.4 / 0.25.3 | 9.0.3 / 1.3.0 |
| pypng | missing | 0.20220715.0 |
| lightning-whisper-mlx | missing | 0.0.10 |

The validated environment the logs cite, `/private/tmp/orb-ml-upgrade-20261012`, no longer exists. Consequences today: the app still imports and mounts 932 routes in the old venv, so boot is not broken, but `pytest` cannot collect (`test_discharge_qr_backend.py` imports `png`), the new BGE adapter and request-limit code run on library versions they were not validated on, and the 47 ML advisories the upgrade was meant to close are still installed on this machine. The fix is the one SETUP.md now describes: a fresh `.venv-next` from the pins, `pip check`, the full suite there, then swap the interpreter.

Two of the mismatches break behaviour, not just tests, when the current code runs on the old venv:

- **Discharge links raise.** `generate_qr_code` now renders through qrcode's pure-Python PNG factory (`PyPNGImage`), which needs `pypng`. The venv has Pillow (the old renderer) and no `pypng`, and `HAS_QR` in `discharge_service.py` only tests that the factory imports, not that `png` does. Issuing a discharge link therefore throws inside the route (`PyPNG library not installed`). Four discharge test files fail the same way. Either install `pypng` into the venv now or make `HAS_QR` test `import png`.
- **Malformed tokens become 500s, not 401s.** `pyjwt` 2.10.1 raises `TypeError` on a null, list or dict `iat`, `nbf` or `exp` claim and `RecursionError` on a deeply nested payload; the pinned 2.15.1 raises a JWT error that the auth dependency maps to 401. Ten tests in `test_auth_dependency_hardening.py` fail on the old venv for this reason. Low exposure, since the token must be validly signed, but the hardening the pass added does not exist on the shipping interpreter.

**The split has reached HEAD (20 October).** `e0427fb` and `fa1159a` ship code that needs the upgraded dependencies: `pypng` for the encrypted QR writer and reader, `pyjwt` 2.15 and Starlette 1.3 for the hardening. Neither commit touches `server/requirements.txt`, which at HEAD still pins fastapi 0.115.12, pyjwt 2.10.1, cryptography 44.0.0, torch 2.6.0 and transformers 4.49.0, with no `pypng`. The HIS build session ran a clean archive of `fa1159a` against `server/.venv`, which matches those pins: 40 failures in the dev profile across eight files (`test_discharge_access`, `test_discharge_public_base_url`, `test_discharge_review`, `test_lab_critical`, `test_lens`, `test_scribe_sign_ack_and_audit`, `test_security_remediation`, `test_warm_standby`), 43 in prod, and wave 9 on top adds none. Either the requirements upgrade is committed with the code it supports and the venv rebuilt from it, or those commits wait. That is Samuel's decision, and it is now the first item in section 4.

The frontend has the same split in the other direction. `node_modules` was installed from the uncommitted lockfile (Vitest 4.1.11, up from 3.2.6 at HEAD), and the uncommitted `tsconfig.app.json` adds `"node"` to `types` because Vitest 4 no longer supplies Node's ambient types to the 24 source-adjacent tests that read `node:fs` and `process`. So HEAD's own `tsconfig.app.json` fails `tsc` against the installed `node_modules`, while the working tree passes. Reported by the wave 9 build session and consistent with the diff. Whoever commits must keep `package.json`, `package-lock.json`, `tsconfig.app.json` and the Vitest 4 test changes in one slice, or HEAD stops typechecking against the installed modules.

### 3.3 Suite state on this machine (Node 22.23.1, shipping venv)

| Check | Result |
|---|---|
| `tsc -b` | Pass |
| Lint ratchet | Pass, 8 problems against a baseline of 8 |
| Frontend unit suite | 3,697 pass, 3 fail, all in `src/api/pharmacyStore/index.test.ts`: `Response.blob()` returns Node's Blob and jsdom's `FileReader` refuses it. The logs ran on Node 24.19.0 and passed; CI pins Node 22, so CI would be red |
| Backend suite in the shipping venv | 17,201 pass, 36 fail, 1 collection error in 32 minutes. 29 failures plus the collection error are environment pins (`pypng`, `pyjwt`, Starlette), 6 are order effects that pass alone, 1 is a stale fixed-date test in committed code. Section 7 |
| Doc citation gate (`npm run check:docs`) | Fail: two stale anchors in `docs/guide/41-thinking-mode.md` (lines 436 and 442) point at moved symbols in `sage_routes.py`. Pre-existing drift in the uncommitted tree, so `npm run check` is red |
| Playwright journeys, visual sweep | Not rerun here |

### 3.4 The founding ward list (WORKFLOWS_2026-10-10.md, section 6) today

| # | Item | Status on 19 October |
|---|---|---|
| 1 | Pain score on the observation sheet | Built 21 October 2026: `vitals.pain_score` (migration 87), nurse sheet and clerk copy, inpatient record print, downtime pack, chart trend, rounds-file blank count; never scored (`tests/test_vitals_pain_score.py`) |
| 2 | Ward clerk admits with ward and bed picker; marks a patient left | Built in wave 9 (`services/adt/clerk.py`, `DepartAction`, `AssessmentAction`, `test_ward_clerk_admit_depart.py`). Uncommitted, unverified by the lane |
| 3 | Roster import on the Ward Record rail | Not done. `WARD_RECORD_TAB_IDS` is still vigil, shift, adt, observations, admin |
| 4 | A nursing note type nurses can write | Built 21 October 2026: `nursing_notes` (migration 88), `services/nursing_note_service.py`, routes under `/api/patients/{id}/nursing-notes` wearing `require_patient_access`; nurse's shift board sheet, chart timeline, I-PASS, inpatient record print; off under the Ward Record profile (`tests/test_nursing_notes.py`) |
| 5 | Allergy add and deactivate on the chart | Not built. No chart control calls the allergy routes |
| 6 | Stop, hold, resume a charted medicine | Server half still empty: `routes/medorders/lifecycle.py` is a 20-line docstring. The screens exist and hide their buttons behind `MEDORDER_HOLD_STOP_ROUTES = false` |
| 7 | Escalation screen with acknowledge wired, on-call rota | Not built. The acknowledge route is still a permission combination standing in (vigil_routes.py:1209); no rota |
| 8 | Receipt print, refund and discount at the counter, one-step OPD bill | Partly. Receipt print landed (`payments_routes.print_receipt`, wave 9) and OP counter billing in wave 8. Refund and discount requests raised at the counter are not new |
| 9 | Elective case booking and theatre master | Not built |
| 10 | Death, LAMA, DAMA on a default site | Still behind `ORB_ADT` (`routes/adt/end_stay.py` inherits the package gate) |

The two other founding-ward bites from the inventory stand: the downtime pack still lives only on the appliance disk (per-facility mirrors are wave 12, unbuilt), and offsite backup means a locally attached volume (`--offsite DIR`), nothing off the premises.

### 3.5 HIS plan position

Waves 1 to 8 are committed as verified. Wave 9 is built in the working tree, uncommitted, with no verifier record. Waves 10 to 14 are not started: bed-day and MRP billing with per-facility numbering (migration 73), facility-aware admission and ingestion binding, ward stock consumption, multi-site columns and the SQL-site registry (migrations 74, 75), the two scope sweeps, per-facility price lists and stock, visiting consultants, and the two-facility drill. The held clusters are unchanged: medorders-12 to 18, lis-17 and multisite-20, blocked on R001, R002, R009, R010, R011, W099, W111 and R023.

### 3.6 Rulings still waiting

`docs/RULINGS_NEEDED_2026-10.md` is unchanged since 10 October. Still open: R001, R002, R003 to R008, R009, R010, R011, R012 to R021, R025 (b), R027 (c), R030 stages 2 and 3, the four pricing terms, the website wording sweep, and the Bridge red-flag rulings (including replacing the US 988 line with Tele-MANAS 14416). R026, the Sage bed locator, shipped on 19 October at 22:43 (`f5a0afb`, attempt 13f.2, fourteen independent reviews, 0 wrong charts against the old code's 169 per 614 realistic questions); `ffeac66` records four items left for separate rulings.

### 3.7 Work stranded in worktrees

Uncommitted edits sit in six local worktrees, each on an old base:

| Worktree | Base | Uncommitted | What it is |
|---|---|---|---|
| peaceful-kare-b1fe8a | 7c3a730 (9 Oct) | 4 files, +97 | R032 Relay transfer rule C, flag-off (`sage_handoff_service.py`, new `test_handoff_transfer_cover.py`). Memory says it must merge with adt-6c |
| mystifying-mirzakhani-ac73a2 | 61ac46e (6 Oct) | 4 files, +183 | `patient_timeline_service.py` and `SageAlertAssessment.tsx` changes |
| youthful-turing-5fe67e | 66e6bd8 (7 Oct) | 6 files, +93, new `tests/prerequisites.py` | Test prerequisite refactor across backup, encryption, standby tests |
| zen-mestorf-9d36aa | 0939e33 (4 Oct) | 2 files, +136 | conftest and discharge access edits; its three new limiter test files are already on main |
| recursing-mestorf-af3b0c | 89bad46 (4 Oct) | 17 files, 43/43 | The 4 October UI wording sweep (`docs/UI_WORDING_SWEEP_2026-10-04.md`), never committed |
| wonderful-rubin-48b6bc | detached, 3 commits ahead | none | HostGate, Vite hosts, Docker retirement; all three appear on main already, so the branch is probably stale |

### 3.8 Open engineering items the logs themselves list

PostgreSQL is deliberately refused in `db_backend.py` and needs a real port; the AudioWorklet migration in `useStreamingTranscription.ts`; train/serve feature skew and checkpoint promotion in `train_vigil_cfc.py`; the non-sepsis dataset adapters (`NotImplementedError`); provenance-aware retention in `bot_service.py`; the Vigil baseline cache; key escrow and an on-site restore drill; Sage cold start and chart-prefetch misses; busy-ward concurrency unqualified; no clean Linux CI run; Python lint, typing and coverage only on two files.

## 4. What to do first, in order

1. Done on 20 October (section 8): the pins are committed (`60c1aee`, `cf6121d`, `c91eb2c`), verified green, and `server/.venv` is rebuilt from them with the real embedder confirmed. Left from this step: fix the three Node 22 Blob tests, commit the rest of the tree in slices (audit pass, offline modules, Sage speed, docs), and prune or merge the six worktrees.
2. Land the security remediation that started at 23:07 on 19 October (section 3.1): the five P1 findings with negative tests for every foreign-object operation, then SEC-06 to 08, then the seven source-only concerns, which its test file does not yet name. Run its tests on the pinned environment, not the shipping venv.
3. The four browser findings, three of which the same session has started; UX-02 (radiation safety on mobile) is not in its file list yet.
4. Founding ward: pain score, roster import on the Ward Record rail, the medication lifecycle routes (the screens are waiting), a nursing note, allergy controls.
5. A rulings session on the list in 3.6; the held HIS clusters cannot move without it.

## 5. Against Sol's own summary of 19 October

Sol's six-page recap (`output/pdf/Orb_Work_Summary_2026-10-19.pdf`) agrees with this review on the substance: thirteen modules built, the audit fixes landed, Sage warm timings met, the eight security and four UI findings "assessed and documented; they remain open" (true when it was written; remediation began later the same evening, section 3.1). It is accurate as far as it goes. Five things it does not say, which change what to do next:

1. The 16,103-pass backend run it cites was made in a candidate environment that no longer exists. On this machine the suite cannot collect, and the frontend suite has three Node 22 failures (section 3.3).
2. Nothing is committed and there is no remote. The recap says "extensive changes remain in the working tree" without saying that every one of the three days' work is in that state.
3. Wave 9 of the HIS plan is built but unverified and uncommitted, and the shared files it touches (`main.py`, `permissions.py`, `roles.ts`, `shellConfig.tsx`, `realtime.py`) also carry the module wiring, so a wave 9 commit by paths will sweep module edits with it.
4. The founding-ward list is mostly untouched: eight of its ten items are unbuilt (section 3.4), including the pain score, the one item the inventory said bites on day one.
5. Six worktrees hold stranded uncommitted work (section 3.7), including the R032 Relay transfer rule.

## 6. What this review did not do

No code was changed. The Playwright journeys and visual sweep were not rerun. The thirteen modules were not opened in a browser. The backend suite was run in the shipping environment only; a run on the pinned environment is the real gate. The security findings were confirmed by reading the code at the cited lines, not by rerunning the assessment scripts.

## 7. Backend suite record

Run on 19 October from 22:40 to 23:12 in the shipping `server/.venv`, with `--continue-on-collection-errors`, dev profile, a throwaway database and a suite-only JWT secret.

| | |
|---|---|
| Collected | 17,247 tests in 514 files, 1 file failed to collect |
| Result | 17,201 passed, 36 failed, 9 skipped, 1 collection error, 31 min 51 s |

Every failure was rerun alone and classified:

| Class | Count | Files | Cause |
|---|---|---|---|
| Missing `pypng` | 11 failures + the collection error | `test_discharge_access`, `test_discharge_review`, `test_discharge_public_base_url`, `test_discharge_qr_backend`, `test_lab_critical` | QR rendering now needs `pypng` (section 3.2). Environment, and a live defect on this venv |
| `pyjwt` 2.10.1 instead of 2.15.1 | 11 | `test_auth_dependency_hardening` | Malformed time claims and nested payloads raise `TypeError` or `RecursionError` instead of a JWT error (section 3.2). Environment |
| Starlette behind the pin | 7 | `test_http_dependency_security` | Streamed form field limits and malformed-Host handling the pass added rely on Starlette 1.3.1; the venv has the 0.115-era Starlette. Environment |
| Order-dependent or edited mid-run | 6 | `test_ward_reference` (2), `test_sage_bed_locator` (2), `test_zzz_scope_diag`, `test_warm_standby` | All six pass when their files run alone. The standby test most likely hit `standby-restore.sh` while the remediation session was editing it |
| Stale fixed date | 1 | `test_simulated_results::test_a_newer_simulated_result_does_not_hide_an_older_genuine_one` | Committed code, not part of the new work. The test plants a genuine creatinine dated 3 October 2026; `_latest_creatinine` only reads inside `LAB_WINDOW_DAYS` (14), so from 17 October the row is too old and the assertion fails. Fix the test to use a relative date |

So on the pinned environment the expected state is 17,247 collected with one stale-date failure to fix. The three environment classes (29 tests and the collection error) are the proof that section 3.2 is not theoretical. The frontend result (3,697 pass, 3 fail on Node 22) is in section 3.3.

**Addendum, 20 October 00:30.** After `e0427fb` and `fa1159a`, `tests/test_security_remediation.py`, `test_lens.py` and `test_scribe_sign_ack_and_audit.py` on the shipping venv: 45 pass, 3 fail, all three the encrypted-QR tests (`pypng`). The HIS build's clean-archive run of HEAD against the same venv found 40 failures in eight files in the dev profile and 43 in prod (section 3.2). The full suite was not rerun after those commits.

## 8. Pins and adapter committed, 20 October

Samuel chose to commit the dependency upgrade with the code that needs it. The pins landed in two commits: `60c1aee` at 13:47 (`server/requirements.txt`, `requirements-dev.txt`, `constraints-security.txt`, `requirements-bootstrap.txt` and the CI install steps), then `cf6121d` with the eight files those pins still needed: `services/local_transformers.py`, the three callers rewritten off FlagEmbedding (`clinical_knowledge.py`, `corpus_loader.py`, `knowledge_worker.py`), `scripts/convert_to_coreml.py` with `weights_only=True`, `tests/test_local_transformers.py`, and the frontend `package.json` and `package-lock.json` (Vitest 4.1.11). HEAD no longer imports FlagEmbedding anywhere, and the installed `node_modules` now match the committed lockfile.

The verification environment is `server/.venv-dev` (gitignored): Python 3.12.13, built from `requirements-bootstrap.txt` then `requirements-dev.txt`, `pip check` clean, torch 2.14.1, transformers 5.19.0, fastapi 0.133.0, pyjwt 2.15.1, cryptography 50.0.2, pypng and sqlcipher3 present.

| Check | Where | Result |
|---|---|---|
| Full backend suite | clean export of `88ef773` + the twelve files, `.venv-dev` | 16,818 passed, 20 skipped, 0 failed, 24 min 18 s |
| Knowledge-lane files after HEAD moved | clean export of `dab1132` + the eight files, `.venv-dev` | 210 passed (corpus loader, knowledge worker, reranker, local transformers, Sage retrieval and guardrails, CfC safe load) |
| `tsc -b` | export of `88ef773` + slice, Node 22.23.1 | pass |
| Lint ratchet | same | 0 problems against a baseline of 0 |
| Frontend unit suite | same | 3,478 passed, 3 failed: the Blob tests in `src/api/pharmacyStore/index.test.ts` (section 3.3), unchanged by this work |
| `npm ci --offline --ignore-scripts` | scratch copy of the new lockfile | exit 0 after one online install filled the local npm cache; it failed with ENOTCACHED on the Vitest 4 tarball before that |

An earlier run on `59ee1bc` plus the slice had one failure, the CoreML pickle-refusal test, because the converter script was missing from the slice; adding `scripts/convert_to_coreml.py` closed it. The stale-date test from section 7 passed on the pinned environment, so `a1fd336` must have corrected it.

**Swap done, 20 October 14:06.** With no process left in `server/.venv` (all other work had moved to `.venv-dev` first), the old environment was renamed `server/.venv-old-20261020` and kept as the rollback (926 MB), and `server/.venv` was rebuilt in place from the committed pins: `uv venv` with Python 3.12.13, then `requirements-bootstrap.txt`, then `requirements.txt` without `lightning-whisper-mlx` (never installed in the old environment either; SETUP.md section 5) together with `requirements-dev.txt`, plus `reportlab` and `markdown`, which only the PDF build scripts under `scripts/` import and which no requirements file pins. `pip check` is clean.

Proofs on the new interpreter:

- The knowledge service loads `services.local_transformers.LocalDenseEmbedder` from the offline BGE-M3 snapshot in 8.5 s and returns unit-norm 1024-dimensional vectors for two sentences. No mocked fallback.
- `./run-orb.sh` boots on `server/.venv/bin/python`; `/api/health` and `/api/ready` answer 200 within 8 s, the knowledge worker pool starts and the corpus is reported up to date; `./stop-orb.sh` stops it cleanly and leaves port 8000 free.

The mandatory backend quality gate (`tools/backend-quality/backend_quality.py check`, ruff 0.17.0 and mypy 2.4.0, made mandatory by `159806b`) then reported six findings in `cf6121d`'s two files, found by the HIS build session: a `zip` without `strict=`, three missing return annotations and both files unformatted. `c91eb2c` closes them with no behaviour change; on a clean export the gate scans 399 files at zero lint, zero typing, zero unformatted and the annotation inventory back at 4,334, and the knowledge-lane tests pass 210 of 210. Lesson for every Python commit from now on: run that gate on a `git archive` export before committing (the untracked `config/` directory at the repo root adds an I001 that CI never sees), with the tools from `tools/local-inference-quality/requirements.txt` in a separate environment.

Samuel then had both spare environments deleted (`server/.venv-old-20261020` and `server/.venv-dev`, nothing running in either), so `server/.venv` (971 MB) is the one Python environment on this machine and it matches the committed pins. Left: `lightning-whisper-mlx` remains uninstalled, as before; the three Blob tests and UX-02 on mobile remain as noted.
