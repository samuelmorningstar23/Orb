# Orb: Discovery Baseline (2026-07-07)

Evidence-based snapshot of the codebase at `orb-visual-browser-tests-a31en1`
(the most advanced branch; 45 commits ahead of `orb-v2-agentic-execution`).
Produced by a 10-agent parallel audit (9 subsystem readers + an adversarial
completeness critic that spot-checked every high-severity claim against code).
File:line references were verified at commit `0296627`.

## 1. CI truth (verified live, not from docs)

| Gate | Status at head | Detail |
|---|---|---|
| Visual audit (86 captures, light+dark) | ✅ green | Re-run locally: 86/86 captures, 0 gate failures |
| Frontend (lint ratchet + tsc + build + vitest) | ✅ green | Ratchet 388 problems vs baseline 401 (can ratchet down); 14 unit tests |
| Backend (pytest) | ❌ red | 1 failure: `test_mfa_disable_requires_password_and_code` asserts 401; route deliberately returns 400 (documented rationale in `auth_routes.py:304-307`), test/route contract mismatch |
| E2E (5 golden journeys) | ❌ red | Runner disk exhaustion: e2e venv installs full `requirements.txt` (torch+CUDA ≈8 GB) and the backend retries Ollama per-vitals in a tight error loop (`vigil_service.py:685`) until "database or disk is full" |

Note: `tests/visual/VERIFICATION.md` still says 78 captures / baseline 379; the
Wave-2 commit silently ratcheted the lint baseline UP 379→401 (history:
389→379→401), contradicting its own "can only go down" contract.

## 2. Pillar map: what's real vs scaffolded

All 15 clinician tabs + patient portal are **genuinely wired** (UI → real
FastAPI endpoint → SQLite): Vigil (NEWS2), Orders/CPOE-lite, Relay, Sage
(RAG + proactive insights + agentic handoff), Scribe (4-stage pipeline),
Lens (gated, non-diagnostic), Forecast, Command Center, What-If Simulator,
Revenue Integrity, Appointments, Surgical Suite, Helix, Pharmacy Verify,
Admin (12 sub-tabs), Bridge/PatientShell, Discharge access, Downtime, I-PASS.
36 routers mounted in `server/main.py`. This is not a mockup.

Qualifiers on "real":

- **Vigil (flagship) renders fabricated data**: comorbidities deterministically
  hashed from the MRN shown unlabelled in the patient header
  (`VigilPanel.tsx:639-642, 931-933`); `PAT007` hard-pinned as EMERGENCY
  (`:146-152, 242`); the normalized NEWS2 index displayed as "% risk" which the
  backend explicitly forbids (`vigil_service.py:320-327`).
- **No eMAR**: the medication loop stops at pharmacist verification. No
  administrations table/endpoint; Helix dispense/deliver are `is_mock_event=1`.
- **CfC risk overlay + Forecast LSTM ship no trained weights** (no
  .pt/.safetensors anywhere), both fail closed to NEWS2-only/heuristics
  (genuinely good gating), so all "trained model" language is aspirational.
- **WHO surgical checklist backend is real but has no UI caller**; the panel
  renders an unrelated LLM-generated string list under the same name.
- **No shipped deployment path serves the frontend**, no StaticFiles mount, no
  proxy in docker-compose, `install.sh` starts backend only. The SPA is only
  reachable via `npm run dev`. PWA is uninstallable (manifest has no icons)
  and its precache omits all woff2 fonts.

## 3. Top debt, ranked by blast radius

### Patient-safety (the trust gap)
1. **Drug-safety screening runs on 1 of 6 medication write paths.** Enforced
   only in the Sage agentic handoff (`sage_handoff_service.py:143-156`).
   Unscreened: Vigil UI med-add (`vigil_routes.py:495-546`, the primary
   clinician path), CPOE med orders (terminology-only), Relay ambient execute
   (`relay_routes.py:232-333`, which also discontinues meds by drug-name
   substring), signed-note auto-extraction, Helix approve. The one advisory
   endpoint (`clinical_records_routes.py:99-104`) has **zero frontend callers**.
2. **Provenance laundering**: LLM-extracted meds from signed notes INSERT with
   no `source` column → default `'clinician'` → silently bypass the pharmacist
   verify queue (`nervous_system_triggers.py:163-185` vs
   `pharmacy_verification_service.py:27`).
3. **Wrong-patient ordering**: Helix approve resolves patient via
   `name LIKE %x%` with `'UNKNOWN'` fallback (`helix_routes.py:57-75`).
4. **Agentic dispatch integrity**: `confirm_agentic` inserts audit rows raw,
   bypassing the hash chain and creating chain-restart gaps
   (`relay_routes.py:656-662`); its human gate is a universal PIN `1234`
   (unsalted SHA-256, non-constant-time, re-seeded every boot,
   `database.py:944-946`); orders dispatch with dose/frequency `'TBD'`.
5. **Ward audio → Google cloud ASR by default**: hotword mic defaults ON and
   uses `webkitSpeechRecognition` (`useSageHotword.ts:11`,
   `SageFloatingAssistant.tsx:480-501`), contradicts the on-prem PHI posture.
6. Interaction screening **fails open** for drugs outside a ~12-class map with
   no "not screened" signal; pharmacy-queue safety-check exceptions render as
   "No alerts".

### Security
- `is_active` never checked at login/token validation, deactivated staff can
  re-login (`auth_routes.py`, acknowledged in `admin_users_routes.py:21-25`).
- Socket.IO handshake skips `token_version`/`is_active`, revoked sessions
  keep PHI streams up to 24 h (`ws/realtime.py:28-65`).
- JWT + user + offline queue (contains PHI) in localStorage; 24 h expiry, no
  refresh flow (deferred-in-code, `client.ts:15-26`).
- Legacy unsalted SHA-256 password path still live and still what the seeder
  writes; rate limiting per-IP/in-memory only (no per-account lockout).
- Encryption at rest opt-in with silent plaintext fallback; backups plaintext.
- `vitals:update` broadcast to every authenticated socket regardless of role
  or care-team (PHI over-broadcast).

### Reliability
- **Relay realtime silently dies after any reconnect**, channel rooms are
  never re-joined (no `connect` listener in src); channel-scoped critical
  alerts stop until the user switches channels.
- Offline queue covers 2 of ~30 clinical mutations; only triggers on
  `navigator.onLine`; fabricates success objects.
- Event bus is fire-and-forget (a subscriber crash drops a sepsis-bundle
  autostart silently); no outbox/replay.
- Dead socket events both directions (client listens for `ambient:*` nobody
  emits; server emits `patient:timeline_updated` to a room nobody joins).

### Design system / UX
- Tokens are strong (~100 vars, light+dark, glass kit, motion, print) but
  adoption is uneven: `Orders.module.css` fully off-system (hardcoded
  black/white + third gold); VigilPanel has **153 inline style objects**,
  ForecastPanel 100 with **no stylesheet at all**; light-theme overrides in
  only 15 of ~30 stylesheets; 3 competing status palettes.
- **One `:focus-visible` rule in the entire app** vs ~30 `outline:none`, the
  single biggest a11y gap for a keyboard-heavy clinical app.
- Light-mode accent (#7AA5C7) measures 2.40:1 as text (self-acknowledged);
  dark `--text-muted` 3.84:1 at 0.6-0.75 rem carrying clinical values.
- A11y collapses inside panels: VigilPanel (2,059 lines) has 1 aria attribute;
  57 of 61 clickable non-buttons keyboard-dead; ~7 Vigil overlays without
  dialog semantics/traps (the shell itself is genuinely good: WAI-ARIA
  tablist, shared focus trap in 8 modals, 27 live regions).
- No high-contrast / night-shift theme. i18n is a 58-key facade (shell +
  login only); PatientShell, the India-market patient surface, is 100%
  English; `<html lang>` never updates.

### Performance
- Visited panels stay mounted and **poll forever** (30s Vigil, 20s Command,
  20s Insights, 10s Surgical, 15s+1Hz bundles, 2s queue…) with zero
  `visibilitychange` gating.
- `PatientRow` memo defeated by inline closure → ~100 rows re-render every 2s.
- Entry chunk 505 KB + 343 KB recharts chunk; 5 variable font families
  (493 KB woff2, none covering Devanagari); PWA precaches 2.4 MB but not fonts.
- Pagination on only 3 endpoints; blocking SQLite calls inside `async def`
  routes; per-call connections (no pool); hand-rolled additive migrations
  (idempotent, but single SCHEMA_VERSION, no rollback).

### Docs
- Root `ORB_DOCUMENTATION.md` is 3 months stale and actively misleading
  (MEWS-era, deleted seed.py, retired shared passwords, 0.0.0.0 bind advice).
- The entire 14-feature wave (2026-07-06) is undocumented everywhere.
- `.env.example` claims completeness but misses ≥8 ORB_* vars; 4 different
  Ollama model tags across docs (wrong tag = all LLM features silently dark).

## 4. Real strengths to build on (verified)

- Hash-chained audit log with DB-level UPDATE/DELETE triggers + generic
  mutation-audit middleware safety net.
- TOTP MFA with genuine RFC-6238 replay protection; hardened discharge-access
  token flow (constant-time DOB compare, atomic lockout, aud-scoped grants).
- Idempotency keys with server-side UNIQUE partial indexes for notes/meds/
  appointments; guarded state machines (orders, Helix) with transition logs.
- Fail-closed gating on untrained models; honest model-governance registry.
- The 86-capture visual gate + 5-journey e2e + 449 backend tests + lint
  ratchet, an unusually strong verification chassis to extend.
- Socket handshake auth done right (JWT in auth payload, DB-backed identity,
  per-channel RBAC mirroring REST).
