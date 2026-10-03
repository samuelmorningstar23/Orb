# Orb visual audit — how to run & verify

## Prereqs

- Backend seeded + running on :8000, Vite on :5173:

  ```bash
  cd server && source .venv/bin/activate
  python scripts/reset_db_and_seed.py          # once (wipes + reseeds demo data)
  ORB_DEPLOYMENT_PROFILE=dev uvicorn main:socket_app --port 8000
  # separate terminal:
  npm run dev
  ```

- Chromium: uses `ORB_CHROMIUM` / `ORB_CHROMIUM_PATH` if set, else the sandbox
  binary `/opt/pw-browsers/chromium` if present, else Playwright's own install
  (`npx playwright install chromium` — this is what CI does). In the sandbox,
  do NOT run `playwright install`; the system binary is already there.

## The loop

```bash
node tests/visual/driver.cjs             # mocked pass — every screen, both themes + night pass
node tests/visual/ci-gate.cjs            # gate: 0 non-allowlisted findings = green
node tests/visual/driver.cjs --live      # live smoke vs the real backend
node tests/visual/ci-gate.cjs --in=tests/visual/out-live
```

Useful flags: `--filter=<substr>` (subset of scenarios), `--headed`,
`--verbose` on the gate, `--out=DIR` on the driver.

CI (`.github/workflows/ci.yml` → `visual-audit`) runs the same two commands;
the default mocked run additionally mirrors `out/findings.json` →
`tests/visual/audit-results.json` and `out/shots/` → `tests/visual/shots/`,
which the workflow uploads as build artifacts.

## What green means

- `ci-gate.cjs` prints the capture count and exits 0 with
  `GATE GREEN — 0 non-allowlisted findings`.
- Remaining allowlisted items are exactly the known-intentional set (disabled
  discharge verify button, hover-reveal Sage hotword toggle, forced-401 /
  session-expired console noise) — anything new in the allowlisted bucket is a
  review flag.
- `out/gallery.html` — open it and eyeball the light/dark pairs, especially
  anything the gate marked `visual-verify` (gradient/image backgrounds are
  verified visually, not by compositing — don't "fix" a compositing false
  positive).

## Verifying a fix

1. Reproduce first: run with `--filter=<scenario>` and confirm the finding
   appears in `out/findings.json`.
2. Apply the fix **via design tokens** (`--text-*`, `--bg-*`, `--accent-*`,
   `--status-*`, `--btn-primary-text`…) — never a hard-coded theme color.
   Accent-colored TEXT in light mode must use `--accent-deep` (the light
   `--accent-gold` is a pale blue that fails as text).
3. Re-run the same filter — the finding must be gone **in both themes** (and
   in `night/*` when the surface is part of the night pass) with zero new
   console errors, then run the full driver + gate.
4. Mind the specificity trap: `[data-theme="light"] .glass-card` (0,2,0)
   beats single-class semantic modifiers — safety surfaces (e.g.
   `.patient-row--emergency`) need an explicit `[data-theme="light"]` override.
   The same applies to `[data-theme="night"]`.
5. If a fix would change clinical meaning (risk levels, emergency markers,
   med-safety semantics) — stop and flag it in the report instead of forcing.

## Live smoke notes

Mocked mode catches rendering/contrast; integration bugs (fixture-shape
mismatch, null access, permission 403s, white screens) only show against real
data. The live pass logs in through the real form per role, loads real
patients, opens a real patient detail, pages the real audit log, and books a
bridge appointment. LLM-backed streams (Sage/analysis) are expected to degrade
gracefully in environments without the model server — those console errors are
allowlisted for live mode only.

---

# Appendix — trunk integration & hardening report (historical)

The report below was written on the trunk while the harness lived at an
earlier revision (captures were flat `<name>-<theme>.png` files under
`tests/visual/shots/`, findings in `audit-results.json`). Capture counts and
paths refer to that revision; the surfaces it verified are all still driven by
the merged harness (see FEATURES_AND_CHECKS.md).

This branch carries three staged bodies of work, each closed out with a full
browser-audit gate (every screen, every role, both themes) plus build checks.

- **Branch:** `orb-visual-browser-tests-a31en1` (PR #2 → `orb-v2-agentic-execution`)
- **Harness:** `tests/visual/driver.cjs` — 86 captures per run (2 themes ×
  [logins, 11 doctor tabs + 4 forecast sections, admit modal ×2 states,
  surgical ×2 views, admin ×12 tabs, MFA-enroll modal, command palette,
  helix ×3 views + verify queue, bridge + records modal, discharge,
  session-expired modal scenario]), mocked API,
  WCAG-contrast/visibility/misalignment checks + JS-error capture.
  `ci-gate.cjs` fails CI on any non-allowlisted finding.
  (Counts below in Parts A–B are the historical run sizes at that stage.)

## Part A — Branch integration (gate: 76 captures, then clean)

Merged `codebase-overview-5pptez` (investor panels, e2e suite, i18n,
server hardening) into this branch in the safe direction (feature branch first,
presentation re-verified on top):

- Conflicts resolved: `ForecastPanel.tsx` import union; **AdminPanel dual
  stylesheet consolidated** (both branches had independently created the
  missing admin CSS — kept the superset `admin/Admin.css`, removed
  `AdminPanel.css`); verified ClinicianShell kept both theme unification and
  the offline pending-count.
- Re-implemented the other branch's unpushed follow-ups: **Manrope Variable**
  body + **Fraunces Variable** serif (activates on Impact hero figures), all
  fonts self-hosted (removed the Google Fonts CDN @import and a stray Figtree
  CDN import; tokenized the last hardcoded font refs), **Forecast NaN guard**
  (4 chart sites), **audit-chain reseal wired into `reset_db_and_seed.py`**
  (verified end-to-end: seed + reseal run green).
- Gate findings fixed: RevenueIntegrity hard-coded `#E9CE7A` text (1.31:1 in
  light) → `var(--accent-deep)`; CapacitySimulator 6px slider hit target →
  24px input with track pseudo-elements.

## Part B — Clinical-safety / robustness / a11y hardening (gate: 78 captures, 0 failures)

Applied as a 12-agent fan-out, each fix adversarially verified by an
independent read-only agent (11/12 clean; the verifier caught the re-auth
modal's z-index sitting under Vigil's 9999 overlays — fixed to 10000, plus a
refocus-while-disabled no-op and an MFA-aware error message):

| Fix | Substance |
|---|---|
| End-to-end idempotency | UUID per note/med submit → `Idempotency-Key` header on direct AND offline-replayed POSTs; server UNIQUE partial indexes + return-existing-row. Offline replays can no longer double-submit medication orders. |
| Session expiry | 401s dispatch `orb:session-expired` (no more clearToken+reload destroying unsaved input); blocking glass re-auth modal preserves all work (see the `errors/session-expired` captures). |
| Audit-chain race | `BEGIN IMMEDIATE` around read-prev+insert, guarded partial `UNIQUE(prev_hash)` index, one retry on IntegrityError. |
| Note-write permission | `add_patient_note` now requires a write permission (was `patient.read_all`). |
| XSS defense-in-depth | DOMPurify allow-list sanitization behind the existing escaping on all 3 `dangerouslySetInnerHTML` sinks. |
| Socket auth | `autoConnect: false`; connects only with a token present. |
| Mount-on-first-view | ClinicianShell renders a panel subtree only once visited (was ~15 panels fetching/polling at login); visited panels stay mounted. |
| Admit modal | ChipInput hoisted (no more focus loss per chip); dialog semantics, Escape, focus trap, aria-labels. |
| Vigil | Print fires after the print view commits (was a 100ms timeout race); discharge modal dialog semantics + Escape. |
| Surgical Suite | Fallback poll reads `activeView` via ref (stale closure). |
| Relay/Alerts | Empty catches → logged, user-visible degraded states. |
| SizedChart | recharts mounts only at non-zero width (kills first-paint NaN console noise). |
| PWA | dev SW off; `navigateFallbackDenylist` for `/api`, `/uploads`, `/socket.io`. |

Build checks: `tsc` clean; server modules compile; **lint ratchet 389 → 379**
(baseline lowered — the fan-out reduced debt).

## Part C — CI pipeline

`.github/workflows/ci.yml` now runs four jobs on every push/PR:
1. **backend** — pytest (pre-existing).
2. **frontend** — `tsc`+build+vitest, with lint upgraded from non-blocking to
   the **ratchet** (`scripts/lint-ratchet.mjs` + `.lint-baseline.json`): fails
   only if the problem count rises; lower the baseline with `--update` as debt
   is paid down.
3. **visual-audit** — boots Vite, runs `driver.cjs` (78 captures both themes),
   gates via `ci-gate.cjs`, uploads screenshots + results as artifacts.
4. **e2e** — creates `server/.venv-dev`, seeds the demo DB, runs the 5
   golden-journey Playwright specs against the real backend.

## Gate allowlist (intentional states)

- Sage hotword toggle at rest (hover/`:focus-visible` reveal).
- Disabled discharge button (WCAG 1.4.3 exempts inactive controls).
- "Session expired" console logs **only** within the session-expired scenario,
  which force-401s every API by design.

## Final state

86/86 captures pass the gate with **0 failures and 0 app-level JS errors** in
both themes. `tsc` clean, production build green. Lint baseline: the Wave-2
feature commit ratcheted it UP to 401 (against the ratchet's intent); it has
been reset to the true current count (388) and should only go down from here.
Every fix above was verified by an independent adversarial agent and
exercised in the browser.

Known accepted gaps (documented, not defects): TOTP-enrolled users must
sign out and re-login through the full MFA flow rather than the password-only
re-auth modal (modal explains this); demo seed passwords remain intentionally
simple for the quick-access login chips.
