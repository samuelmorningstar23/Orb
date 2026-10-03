# Orb: Sequenced Roadmap (2026-07)

Vertical, shippable slices. Each slice lands green through all four CI gates
(backend pytest · frontend tsc/build/vitest/lint-ratchet · 86-capture visual
audit in both themes · 5-journey e2e) plus a live browser smoke test against a
seeded backend before merge. Ordering is by leverage: trust → truth →
reliability → security → craft → scale. Derived from
[DISCOVERY_BASELINE.md](./DISCOVERY_BASELINE.md).

---

## Slice 0: CI truth (small; unblocks everything)

The "existing green CI" is red at head. Restore it honestly.

- Fix `test_mfa_disable_requires_password_and_code`: align the test with the
  route's documented 400-on-purpose contract (`auth_routes.py:304-307`).
- Fix the e2e job's disk exhaustion: slim CPU-only venv (requirements-dev +
  `--index-url` CPU torch) instead of full torch+CUDA; free runner disk.
- Stop the Ollama retry storm in CI: cache LLM unavailability (circuit
  breaker) instead of per-vitals connection attempts + error log each cycle.
- Ratchet the lint baseline down 401 → 388 (current actual) and restore the
  "only down" contract.
- Sync stale harness docs (78→86 captures, baseline numbers).

**Gate:** all four CI jobs green on the PR head. No behavior change.

## Slice 1: One medication safety gate, enforced everywhere (flagship)

The discovery's worst finding: drug-safety screening runs on 1 of 6
medication write paths, and the loop has no administration step. Make Orb's
core claim true.

- Extract a single `medication_safety_gate` (allergy + DDI + "not screened"
  verdict for unknown classes, never fail open silently) and enforce it in
  all six paths: Vigil med-add, CPOE med orders, Relay ambient execute,
  signed-note auto-extraction, Helix approve, Sage handoff (already gated).
- Surface it in the UI everywhere meds are entered: blocking severe-alert
  acknowledge-with-reason flow (mirror the Scribe 428 pattern), inline
  interaction/allergy panel, "screening unavailable" states.
- Fix provenance laundering: extracted meds write `source='llm_extracted'`
  and enter the pharmacist queue; pharmacist verify gets a severe-alert
  acknowledgment gate.
- Fix wrong-patient hazards: Helix approve by exact patient id (kill
  `name LIKE`); Relay stop/replace by medication id (kill substring match);
  block dispatch with dose/frequency `'TBD'`.
- Close the loop with a minimal real eMAR: `administrations` table +
  administer/not-given endpoints + barcode-style confirm UI (type-to-confirm
  patient + drug), five-rights panel, audit-chained.
- Repair agentic integrity: `confirm_agentic` audit rows through
  `log_audit_event` (restore hash chain); PIN co-sign → bcrypt +
  constant-time + per-account attempt lockout + admin provisioning (retire
  universal `1234`).

**Gate:** new backend tests per path; e2e journey CPOE → verify → administer;
visual captures of the new surfaces (both themes); no unscreened med write
path remains (grep-provable).

## Slice 2: Truth in the UI: Vigil integrity + explainable NEWS2

- Remove fabricated comorbidities and the PAT007 demo pin from clinical
  surfaces (behind `ORB_DEMO_MODE` at most, clearly badged).
- Stop presenting normalized NEWS2 as "% risk": show the raw NEWS2 score,
  band, and trend, as the backend contract demands.
- Add feature attribution: per-parameter NEWS2 contribution breakdown
  ("why this score") with trend rationale, deterministic, from the scoring
  table itself; explicitly labeled as *not* clinically validated.
- Make "Estimated" lab-trend fallbacks visually distinct, not just badged.
- Hotword mic: default OFF, explicit opt-in, and never cloud ASR, local
  Whisper path or nothing (aligns with the on-prem PHI promise).

**Gate:** visual captures of new Vigil detail; unit tests on the attribution
math; no synthetic data rendered unlabelled anywhere.

## Slice 3: Realtime that survives the real world (live collaboration)

- Reconnect correctness: re-join channel rooms on `connect`, socket-level
  `token_version`/`is_active` re-check, versioned event dedupe.
- Scope `vitals:update`/`patient_status_update` broadcasts by role/care-team
  (kill the PHI over-broadcast).
- Presence: who's viewing/editing each patient (per-patient rooms), shown as
  presence chips in Vigil detail + Relay; typing indicators (server handler
  exists, wire the client).
- Optimistic Relay messaging hardened (rollback already exists; add resend +
  failure states) and remove dead one-sided events.
- Offline queue → first-class for all clinical mutations (orders, verify,
  sign, messages) with idempotency keys end-to-end; reconcile fabricated
  offline ids on replay.

**Gate:** vitest for queue/replay/dedupe; e2e with forced socket drop;
presence visible in both themes' captures.

## Slice 4: Auth hardening

- HttpOnly + Secure + SameSite cookie session with short-lived access +
  refresh rotation; CSRF protection; keep socket + LAN/ngrok flows working.
- `is_active` enforced at login and in `get_current_user`; per-account
  persistent lockout table (survives restarts, proxy-aware).
- Retire the unsalted-SHA-256 path: bcrypt-only seeder, migration window,
  then reject legacy hashes.
- `/uploads` per-patient authorization; document every ORB_* env var
  (`.env.example` made actually complete).

**Gate:** full backend auth test suite green; e2e login/MFA journeys green;
no localStorage token anywhere.

## Slice 5: Design-system evolution (the signature slice)

- Token layer: semantic severity ramp distinct from accent in BOTH themes;
  text-safe accent token for light mode (kill the 2.40:1 patches); one status
  palette (retire the 3 competing ones); spacing/type token adoption pass.
- A global `:focus-visible` convention (and delete ~30 `outline:none`s);
  dialog semantics + focus traps for every overlay (Vigil's 7, surgical
  popups) using the existing `useFocusTrap`.
- Bring Orders into the system; extract VigilPanel's 153 and ForecastPanel's
  100 inline styles into tokenized stylesheets with light-theme coverage.
- Unified data-viz language: one theme-aware chart kit (axes, area fills,
  emphasized endpoints, tabular-nums) applied to every recharts surface;
  sparklines wherever numbers trend.
- Motion system: purposeful transitions + skeleton loaders replacing spinner/
  blank states; live-data micro-animations; reduced-motion respected
  including canvas.
- Third theme: high-contrast "night shift / OR" alongside light+dark.
- Summary-before-detail information hierarchy on Vigil board and Command
  Center: severity stripes, pills, at-a-glance ward scan.

**Gate:** 86+ captures green in all themes (extend driver for the third
theme); WCAG 2.2 AA contrast sweep on changed surfaces; DESIGN.md documenting
tokens/motion/viz/a11y.

## Slice 6: Performance + platform

- Pause all polling on hidden tabs/panels (`visibilitychange` + active-tab
  gating); fix the defeated `PatientRow` memo.
- Vendor chunking + font diet (2 families + Devanagari coverage);
  PWA installability (icons, font precache) for ward tablets.
- Serve the built SPA from FastAPI StaticFiles (today *no deployment path
  serves the frontend at all*), makes Docker/bare-metal deploys real.
- Pagination defaults on remaining unbounded list endpoints; move blocking
  SQLite work off the event loop on hot paths.
- Measured budgets: Lighthouse + bundle-size checks recorded in CI.

## Slice 7: Unified patient story

One scrollable clinical narrative per patient merging vitals, notes, labs,
meds, imaging, orders, and events on a single filterable timeline, the
differentiator that makes handoffs and "what happened overnight?" instant.
Builds on `patient:timeline_updated` (currently emitted to a room nobody
joins) and the event log.

## Deferred (explicitly not now)

- Postgres path / Alembic migrations (SQLite + additive migrations hold at
  pilot scale; revisit at multi-ward load).
- Family/caregiver Bridge extension; voice-first rounds mode (after Slice 2's
  hotword rework); i18n completion beyond shell+Bridge (Bridge Hindi rides
  along in Slice 5's empty-state pass if capacity allows).
- Event-bus outbox/replay redesign (documented; needs its own design).

---

**Sequencing rationale:** 0 unblocks CI truth for everything after; 1–2 are
the patient-safety and honesty core (the "trust" in 10x); 3–4 make it
dependable and secure enough to demo unattended; 5–6 are the craft and speed
that make it *feel* 10x; 7 is the headline differentiator once the
foundations hold.
