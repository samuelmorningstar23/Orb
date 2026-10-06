## 17. Verification Status: what in this manual has been checked, and what has not

> **This section exists because the manual's own rule requires it.** The rule is *"every claim traced to `file:line`, and no claim written that was not verified"*. The honest corollary is that a reader must be able to tell **which parts of a 12,000-line document have actually been through that process and when**. Without this section the manual's citations imply a uniform level of confidence it does not have.
>
> **If you are answering a security questionnaire, a CIO's diligence list or an auditor's question: read §17.3 first.** It is the list of things this manual does not currently establish.

### 17.1 The 2026-08-15 fact-check pass: method

Every claim below was checked by opening the cited file and reading the code, not by consulting another document. Where the code carries its own explanation of *why*, that explanation was quoted rather than paraphrased, because in this codebase the reasoning is usually the load-bearing part.

**Counts.**

| | Count |
|---|---|
| Individual factual claims re-checked against code | **~240** |
| Claims found **wrong** and corrected | **42** |
| Subsections **newly written** (subsystems with no prior coverage) | **23** |
| Claims listed as **unverified** rather than asserted | see §17.3 |
| Sections **not re-verified at all** in this pass | §3, §4 (except one claim), §6, §7 (except one), §8, §9, §10.1–10.4, §10.6–10.8, §11, §12, §13.3–13.17 |

**What "checked" covered:** §0 in full; §1 in full; §2.1–2.3 and §2.10; §5 partially (the new §5.7 in full, §5.1 spot-checked); §10.5; §13.2; §14.1–14.4; §15.2. These are the sections carrying the security, architecture and access-control surface, the ones an external reader greps.

### 17.2 What was wrong, split into **rot** and **drift**

The distinction matters for how much to trust the rest: **rot** means the manual was wrong before this week and nobody noticed, which suggests other unchecked sections are wrong too. **Drift** means the last three days outran the document, which is expected and is the cheaper problem.

#### 17.2.1 ROT: wrong *before* this week

| # | Claim as written | Reality | Where |
|---|---|---|---|
| 1 | *"Orb currently does **not** encrypt the database at rest"*, plus *"Why SQLCipher is not in use yet"* and a *"Recommended path"* telling the reader not to add encryption | **SQLCipher whole-database encryption has shipped since 2026-07-30**, validated against sqlite-vec + FTS5 + WAL on this build; uploads are AES-256-GCM | §2.10 |
| 2 | *"This is a filesystem-level access control, not encryption. If the disk is removed or a backup is made, the data is still readable."* | False when a key is configured | §2.1 |
| 3 | *"⚠️ The DB holds PHI in cleartext"* in the env-var reference | Same | §15.2.1 |
| 4 | *"There is no TLS in the current build"* | TLS is **opt-in and implemented**, `ORB_TLS_CERT` + `ORB_TLS_KEY` make uvicorn serve HTTPS directly (`server/main.py:1361-1373`) | §15.2.1 |
| 5 | `SCHEMA_VERSION = 1` | It is **5**, and `schema_version` is no longer the migration mechanism at all | §2.3 |
| 6 | Migration system not mentioned anywhere | `server/migrations.py`, **31 versioned migrations** with their own ledger | §2 |
| 7 | **285 tests passing**, in three separate places, with the instruction *"if you see fewer than 285, a test file was deleted"* | **6,161 tests across 181 files** | §0.6, §0.8, §13.2.3 |
| 8 | **16 routers** registered ("`# ... 14 more routers`") | **54 `include_router()` calls across 53 route modules** | §1 |
| 9 | Every backend route filename in the panel→router map (`server/routes/vigil.py`, `sage.py`, `helix.py` …) | They have always been `*_routes.py`. The table's own caption said *"confirm with the actual file tree"*, the manual admitting it had not verified its own claim | §14.4 |
| 10 | *"FHIR … (on Orb's roadmap)"* | Implemented **both directions**: `FHIRAdapter` inbound, `fhir_export_service.py` outbound with AI-draft provenance downgrading | §0.7 |
| 11 | *"the audit write is not wrapped in try/except, it is expected to always succeed"* | Denial audits go through `run_db` and are awaited; the described synchronous version no longer exists | §1 |
| 12 | `_FAIL_CLOSED_CATEGORIES` listed as four | **Five**, `ai_egress` is fail-closed by design. *Superseded 2026-09-04:* `auth` and `ai_egress` were never emitted by any call site; the set now reads `security`, `clinical_access`, `phi_access`, `authentication`, `ai_inference` | §10.5 |
| 13 | The SPA stores its JWT in `localStorage` under `orb_token`; HttpOnly cookies are *"tracked as a deferred task"* | That task **shipped**. Auth is the HttpOnly `orb_session` cookie + CSRF double-submit | §14.2 |
| 14 | `serve_upload` documented as `async def` with **four** security layers | It is a plain `def` with **seven**, including a registered-media allowlist, ward scoping, a fail-closed PHI audit and transparent decryption | §1 |
| 15 | `run_db`, **zero mentions** in the entire manual | The most-enforced house rule in the codebase, and the fix for three production freezes | §2.10a |
| 16 | `db_backend.py`, no mention | Backend seam, pooling, **and the DB-API exception aliases** that made every `except sqlite3.IntegrityError` dead code on encrypted deployments | §2.10b |
| 17 | Ward/department PHI scoping, **no mention** | The entire second half of access control (`department_scope`, `require_patient_access`, 404-not-403, `log_phi_read`) | §1 |
| 18 | MFA/TOTP, the device-PIN credential, CSRF, no mention | All three exist | §1 |
| 19 | Audit field bounds, no mention | The bound that stops one logged-in account writing ~92 MB/s into an undeletable table | §10.5 |
| 20 | `/api/health` returns *"`{"status": "ok", ...}`"*; no mention of `/api/health/loop`, `/access`, `/models` | Four endpoints | §1 |
| 21 | Middleware documented as CORS only | Four layers, including the mutation audit safety net | §1 |
| 22 | Permission constants list missing `VITALS_WRITE`, `ORDER_READ`, `ORDER_WRITE`, `OUTCOME_RECORD` | All four predate this week | §1 |
| 23 | Repository map missing `migrations.py`, `db_backend.py`, `adapters/`, `eval/`, `corpus/`, `site_config/`, `dev_tools/`, `ops/` | n/a | §0.6 |
| 24 | Test-run instructions used a stale repo path and a bare `python` | n/a | §13.2.2 |
| 25 | Shadow mode, **zero mentions**, despite being the pilot's evidence instrument | Still has no section (§17.3) | n/a |

> **The most serious of these is #1–#4.** A security control that **exists** was documented as **absent**, in the four places an auditor greps first. Anyone who answered a security questionnaire from this manual **understated** Orb's posture, and would have contradicted `docs/MODULE_MANUAL.md` and `docs/USER_MANUAL.md`, both of which describe SQLCipher, AES-256-GCM and opt-in TLS correctly. **A CIO reading two Orb documents side by side would have found them in direct conflict.** Where they disagree, the code is the referee, and on this point the other two manuals were right.

#### 17.2.2 DRIFT: invalidated by the last three days

| # | What changed | What it invalidated |
|---|---|---|
| 1 | **Eleven hospital-administration modules** shipped visible with per-module switches | §0.1's *"suite of ten modules"*; §0.5's table; the router list; the env-var reference; the whole of what is now §16 |
| 2 | **Orb repositioned to replace the HIS/EHR** rather than sit on top of it (`server/config.py:542-547`) | §0.7's glossary definition of EHR |
| 3 | **Two new home screens**, doctor "Today", nurse "My Shift" | §14.3's tab table (every row) and the implicit "landing tab is `accessibleTabs[0]`" rule |
| 4 | **`src/shellConfig.tsx`**, the shell's single role input, with a per-deployment override seam | §14.3 entirely; the file was not in the repo map |
| 5 | **The Sage alert assessment, built three times, ending as three tiers** | §5 had no coverage; now §5.7 |
| 6 | **Migrations 20–31** (twelve of the thirty-one) | §2's schema coverage; §2.4's table catalogue is now base-schema-only |
| 7 | **The audit-actor defect**, every browser mutation recorded `actor_user_id` NULL until 2026-08-14 | §10.5's description of the audit trail as complete; now §10.5a |
| 8 | **Nine unscoped PHI routes closed**; `enforce_patient_scope` added for query-string and surrogate-id spellings | §1's RBAC section, which described only permission checks |
| 9 | `payer.read` / `payer.write` split out of `admin.overview` (2026-08-12) | §1's permission tables |
| 10 | Money-correctness work (`services/money.py` and the four payment rules) | No prior coverage; now §16.3, §16.5 |
| 11 | Six swallowed-cancellation shutdown hangs fixed | Not individually documented, see §17.3 |
| 12 | `ORB_DISCHARGE_FINANCIAL_ENFORCE` added, default off | §15.2 |

### 17.3 UNVERIFIED: read this before quoting the manual externally

**These are honest gaps, not oversights to be papered over.** Each is something this manual either does not cover or covers without re-verification as of 2026-08-15.

#### 17.3.1 Sections not re-verified in this pass

§3 (Vigil internals), §4 (Forecast, *only* the "no checkpoint / heuristic fallback" claim was re-verified, and it **still holds**: no `.pt` file exists in `server/models/`, and the `if not getattr(model, "trained", False)` guard is live at `server/services/forecast_service.py:353`), §6 (Scribe/voice), §7 (Ingestion, only the existence of `FHIRAdapter` was re-checked), §8 (Helix/Lens/Pulse/Bridge), §9 (Relay/Surgery/Admission/Discharge, note that migration 27 `surgery_timing` landed this week, so §9 is *likely* stale), §10.1–10.4 and §10.6–10.8, §11 (bots and auth routes, **likely stale**, since `auth_routes.py` now carries MFA, device-bind and refresh flows), §12 (scripts), §13.3–13.17.

**Treat every line-number citation in those sections as unverified.**

#### 17.3.2 Subsystems with no section at all

| Subsystem | Why it matters |
|---|---|
| **Shadow mode** (`services/shadow_service.py`, `shadow_scorecard_service.py`, `shadow_verdicts.py`, `shadow_export_service.py`, `routes/shadow_routes.py`) | **The pilot's only evidence instrument.** It fails open by design (a shadow write must never fail a clinical action), a property that belongs in this manual next to the fail-closed audit rule. |
| **Backup and restore** (`routes/backup_routes.py`, `docs/BACKUP_AND_RESTORE.md`) | A CIO will ask. The manual says nothing. The known design, verify-before-rotate, so an older copy is never pruned until the new one is proved restorable, is described in `docs/MODULE_MANUAL.md` and `docs/USER_MANUAL.md` but **has not been verified against the code in this pass.** |
| **Key escrow / key ceremony** (`docs/KEY_CEREMONY.md`) | §2.10 states the honest limitation (key on the same disk as the data). Escrow is an **open item, not a shipped control**, do not describe it as one. |
| **`services/field_crypto.py`** | Field-level PHI encryption. Existence confirmed; behaviour **not read**. |
| **`services/ai_governance.py`** | Only its relationship to the `ai_egress` fail-closed category was confirmed. *2026-09-04:* that relationship never existed. `record_inference` writes `ai_inference`, which replaced `ai_egress` in the fail-closed set on 2026-09-04 (§10.5). |
| **`services/mfa_service.py`** | Summarised in §1 from its module docstring; the verification window and enrolment flow were **not traced through the routes**. |
| **~40 further services** | `escalation_service`, `medication_safety_gate`, `allergy_interlock`, `drug_safety_service`, `loop_watchdog`, `world_model_service`, `thinking_mode`, `model_router`, `sage_narrator_service`, `sage_provenance_service`, `compliance_service`, `consent_service`, `downtime_service`, `flight_recorder_service`, and others have **no manual coverage**. |

#### 17.3.3 Specific claims left standing but unchecked

- **§10.5 `_HASH_FIELDS` and `compute_audit_row_hash`**, the field list, its order, and the canonical-JSON hashing procedure were **not re-read** this pass. The *bounds* applied before hashing were (§10.5). Since the hash-chain contract is exactly the kind of thing an auditor tests, **verify this before citing it.**
- **§2.4's table catalogue** documents the ~52 tables in `database.py`'s base `SCHEMA`. `migrations.py` defines **89 further `CREATE TABLE` statements** and `helix_db.py` seven more; **none of the migration-created tables are documented column-by-column.**
- **§13.5's module-to-test map** was written against a suite of 285 tests. With 181 test files it is now, at best, a small sample.
- **§15.2** documents most but not all of the 109 `ORB_*` variables `config.py` reads, and several more are read outside `config.py`.
- **"Six swallowed-cancellation shutdown hangs"** were fixed this week. The house rule (`CancelledError` always propagates) is stated in §0.8 and its one verified instance is documented at §10.5a; **the other five call sites were not located or verified.**
- **Migration 31 (`sage_patient_specific`)**, added during this pass (§17.4a). Its two columns are named in §2.3.2's table but their JSON payload shape and the repeat-suppressor comparison key were **not read**.
- **Latency figures quoted in §5.7** (51 ms vitals-to-alert; 42–57 s for the four-section assessment; ~10–15 tok/s decode) are the code's own recorded measurements on the development appliance. They were **not independently re-measured** in this pass and should not be quoted to a customer as a performance guarantee without a fresh run.

### 17.4 Left deliberately for a human decision

These were found and **not acted on**, because they are judgement calls the founder should make rather than a documentation pass:

1. **How bluntly to state the encryption limitation externally.** §2.10 now states plainly that with the key in `.env` on the same disk, encryption protects *copies* (backups, exports, a stolen disk image) but **not a running or stolen appliance whose `.env` an attacker can also read**. That is the accurate posture and it is what the code says. Whether the CIO-facing one-pager phrases it identically is a positioning call, but it must not phrase it *more favourably*.
2. **Whether the manual should carry the "Orb replaces the EHR" framing at all.** It is in the code and it is now in §0.1 and §0.7. It is also a strategy statement that a hospital's incumbent-EHR vendor would read differently from an engineer. Flagged, not softened.
3. **`docs/USER_MANUAL.md` is stale on the two new home screens**, it contains **zero** occurrences of "My Shift". `docs/MODULE_MANUAL.md` covers it (§19). Bringing a 3,721-line user manual up to date is a writing job with a training-materials deadline attached (§14's note on Weeks 9–10), not a fact-check.
4. **Whether to regenerate §13.5's module-to-test map mechanically.** It is a script's worth of work for 181 files, but a generated map with no commentary may be worth less than an honest note that the old one is a sample.
5. **Whether §2.4 should grow to cover the 89 migration-created tables.** That is roughly a doubling of §2 and it is the single largest remaining documentation debt.
6. **Whether shadow mode gets its own section before the pilot.** Given that it is the instrument the pilot's evidence depends on, this manual's silence on it is the gap most likely to matter in Week 13.

### 17.4a A live demonstration of why §17 exists

**Migration 31 (`sage_patient_specific`) landed in `server/migrations.py` while this fact-check pass was running.** The pass began against a tree with 30 migrations and finished against one with 31; every citation into `migrations.py` shifted by ~70 lines in the process, and the §5.7 text describing the patient-specific tier was written against `services/patient_specific_screen.py` at a point when its persistence column did not yet exist.

The migration count and the affected line numbers were re-verified and corrected before this section was written. **Migration 31's own columns (`patient_specific` and the repeat-suppressor key) are documented here only by name, their contents were not read** (§17.3.3).

The general lesson, which is the reason this section is not optional: **a manual verified against a moving tree is accurate as of a timestamp, not as of "now".** The stamp on this pass is **2026-08-15**. Anything merged after it is undocumented until someone repeats the exercise.

### 17.5 How to keep this section true

When you change code, update its section **and** update the relevant row here. When you re-verify a section that §17.3 lists as unchecked, move it out of that list and date it. A verification-status section that is itself stale is worse than none, because it launders unchecked claims as checked ones.
