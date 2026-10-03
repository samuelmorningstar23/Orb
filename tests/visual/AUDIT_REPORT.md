# Orb UI/UX audit — before / after

Browser-driven audit of every screen, both themes (dark default + light), across
all roles (doctor, surgeon, nurse, admin, pharmacist, patient), plus modals,
print views, the forced-401 overlay, and tablet/phone widths.

## Result

| Pass | Captures | Gating findings |
|---|---|---|
| Baseline (mocked) | 168 | **~1,955** |
| Final (mocked) | 172 | **0** |
| Final (live, real backend) | 40 | **0** |

Gate green in both modes. Remaining non-gating buckets are WCAG-AA niceties
(`aa-contrast`, sub-24px `small-target`) and the known-intentional allowlist
(disabled discharge button, hover-reveal Sage hotword toggle, the gradient Sage
FAB orb, forced-401 console noise, and the deliberately-blocked socket.io in
mocked mode).

## The harness (new — `tests/visual/`)

- **driver.cjs** — Playwright scenario engine: drives every screen to its real
  on-screen state (opens the modal, types the query, triggers the state) in both
  themes, with mocked API + localStorage auth; plus a `--live` mode that logs in
  through the real form and hits the real backend.
- **checks.cjs** — in-page audit: WCAG contrast with alpha **and gradient-stop**
  compositing (via `elementsFromPoint` so positioned sibling layers are seen),
  occlusion-aware (skips chrome behind a modal backdrop), invisible/zero-size
  focusable controls, horizontal overflow, clipped text, icon contrast, WCAG-2.2
  target size.
- **fixtures.cjs** — mock payloads matching the **real** backend response shapes
  (bare array vs `{key:[…]}`), exercising every semantic state: risk levels, the
  emergency-surgery row, safety alerts, an overdue sepsis bundle, stock-outs,
  parse failures.
- **ci-gate.cjs** — fails on non-allowlisted findings; prints the capture count
  and a ranked summary.

## Findings → fixes

### Design-token foundation (`src/index.css`)
- **No accent-as-text token existed.** In light mode `--accent-gold` is a pale
  Sierra blue that fails as text (~2.4:1). Added **`--accent-text`** (deep accent)
  and routed every accent-colored glyph/label through it — nav icons, avatars,
  sort buttons, sage insights, clock, admit tabs, ~30 sites.
- Added **`--btn-gradient`** (contrast-matched primary-button fill) and
  **`--status-info`**; deepened light `--status-ok/warn` one step so status TEXT
  clears 3:1 on same-hue tint pills; nudged dark `--text-muted` so muted text
  clears 3:1 on glass cards.
- Added light-theme `--elev-*` / glass-kit counterparts (dark shadows and white
  inset highlights were leaking into light mode).
- **Zero global `:focus-visible` rule existed** → added one (WCAG 2.2 keyboard
  path); `button { color: inherit }` (UA `buttontext` black was vanishing in
  dark); reduced-motion now also zeroes stagger `animation-delay`.
- Dropped two blocked Google-Fonts `@import`s (index.css + the Figtree import in
  a relay module) that logged a console error on every load offline.

### Patient-safety semantics (both themes)
- **Emergency patient row** was clobbered in *dark* by ScribePanel's global
  `.glass-card` redefinition, and lost entirely in *light* by the
  `[data-theme="light"] .glass-card` (0,2,0) specificity trap. Scoped
  ScribePanel's override and restored the red safety surface with an explicit
  light-theme override at matching specificity. A scenario-level assertion now
  proves the emergency row stays visually distinct in both themes.

### Dark-locked surfaces made theme-aware
- Sidebar, command bar, Vigil modals + tooltips, the Sage history sidebar, the
  Helix pharmacist module (whole file, 39 hexes), the patient-Bridge modal
  (`var(--surface-1,#1e1e1e)` — an undefined token → dark in both themes), and a
  full re-skin of the off-brand blue DischargeAccess page.
- Header chips (clock, theme/lang toggles) → `--bg-elevated` so they stay legible
  over the animated aurora backdrop.

### Systemic bug classes fixed across many files
- **`accent-gold`/hex status colors used as text** → tokens (relay, admin,
  forecast, command, capacity, coding, pulse, surgical-suite, bridge, login).
- **Opacity dimming that pushed text under the contrast floor** — timeline ghost
  nodes, FlightRecorder timestamps, relay stale/handled cards, ProvenanceChip
  model/version — replaced with color/weight de-emphasis.
- FlightRecorder's base `color: var(--text)` referenced an **undefined token**,
  so all its text stayed near-white and vanished in light — one fix
  (`--text-primary`) repaired the whole inheritance chain.

### Caught only by the live smoke (real backend)
- AppointmentsPanel's today-badge used `background: var(--accent-amber)88` — an
  alpha suffix on a CSS var is **invalid CSS**, so the gradient silently dropped
  out and the day number rendered black-on-dark (1.08:1). It compiled clean and
  passed the mocked gate because mock dates are all in May and `isToday` was
  never true. Only real, current-dated appointments triggered it.

### Backend (the audit brief's missing admin surfaces)
- The brief expected **Users** and **Data & Backups** admin sub-tabs; the map
  confirmed neither existed. Added `GET /api/admin/users` (account posture: role,
  MFA state, last login, failed-login count — no secrets) and
  `GET/POST /api/admin/backups` (DB posture + a consistent online backup via the
  sqlite3 backup API, audited), plus the two frontend sub-tabs and 5 backend
  tests.
- Fixed `GET /api/admin/overview`: the auth-events summary filtered
  `event_category='auth'`, which never matches (real categories are
  `authentication`/`authorization`), so it was always empty.

## Improvements (craft, beyond pass/fail)
- Global keyboard focus-visible ring on every interactive element (WCAG 2.2 AA).
- Aurora WebGL + dot-grid now honor `prefers-reduced-motion` (static frame, RAF
  loop stopped) — the CSS reduced-motion rule can't reach a canvas.
- Consistent semantic-color discipline (status stripes/pills via `--status-*` +
  `color-mix` tints) so a ward reads state at a glance in either theme.
- Timeline ghost/future nodes convey "not yet" via a dashed dot instead of
  dimming the date label out of legibility.
- Reduced-motion also neutralizes entrance stagger delays (elements no longer
  stay invisible up to 0.5s).

## Verify
```
node tests/visual/driver.cjs && node tests/visual/ci-gate.cjs            # mocked
node tests/visual/driver.cjs --live && node tests/visual/ci-gate.cjs --in=tests/visual/out-live
```
Open `tests/visual/out/gallery.html` for the light/dark capture gallery.
