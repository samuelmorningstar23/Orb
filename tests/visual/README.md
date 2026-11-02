# Orb Visual Browser Tests

Playwright-driven visual audit of every Orb screen in **light and dark** mode.
No backend is needed in mocked mode. All `/api/*` calls come from `fixtures.cjs`,
including the health switch board required at startup. The driver seeds a
synthetic user and legacy token in `localStorage`; `/api/auth/me` returns that
scenario's same identity. Socket transport is aborted and service workers are
blocked. This exercises the UI, while the real login/session contract is covered
by the repository's separate [E2E journeys](../../e2e/README.md).

Findings from the original audit: see [REPORT.md](./REPORT.md) (key screenshots
in `evidence/`). Final end-to-end verification that all fixes + the liquid-glass
redesign landed: [VERIFICATION.md](./VERIFICATION.md).

## What it does

For each theme × screen (login staff/patient, all clinician tabs as doctor, surgeon,
admin and pharmacist, the Admit Patient modal, patient bridge, discharge access):

1. Seeds theme (`orb-theme` + `orb_theme`) and user in `localStorage`, mocks the API.
2. Waits for render, then runs `checks.cjs` inside the page:
   - **invisible interactive elements** — zero-size, cumulative `opacity < 0.15`, or
     text/icon contrast < 1.6:1 against the composited effective background;
   - **low-contrast buttons/links** — contrast < 3:1;
   - **misalignment** — horizontal page overflow, elements escaping the viewport
     outside any scroll container, clipped text without ellipsis;
   - **gradient inventory** — every rendered `*-gradient` background, for manual
     theme review (elements with gradient fills are marked `gradientBg: true` in
     findings because background-color compositing can't measure them — verify those
     visually).
3. Writes `out/shots/*.png`, `out/findings.json`, `out/captures.json`, and an
   `out/gallery.html` containing the light/dark capture pairs.
4. `ci-gate.cjs` applies the documented exemptions and existing intentional
   allowlist, writes `out/gate-summary.json`, and fails on remaining gating
   findings. Driver setup/step failures also return a nonzero exit status.

## Running

```bash
npm ci
npx playwright install chromium      # CI/Linux: add --with-deps
npm run build
npm run preview -- --host 127.0.0.1 --port 5173 --strictPort
```

From another terminal at the repository root:

```bash
node tests/visual/driver.cjs
node tests/visual/ci-gate.cjs
```

Use `ORB_BASE_URL=http://127.0.0.1:5173` to select another frontend origin.
`ORB_CHROMIUM_PATH=/absolute/path/to/chromium` selects an existing Chromium
binary; `ORB_CHROMIUM` is also accepted. `--headed` opens a visible browser.
The root lockfile supplies Playwright, so no nested package installation is
required.

Run one scenario and keep its evidence separate:

```bash
node tests/visual/driver.cjs --filter=doctor/vigil --out=/tmp/orb-vigil-audit
node tests/visual/ci-gate.cjs --in=/tmp/orb-vigil-audit
```

Comma-separated filters select the union of matching scenario names. This lets
independent partitions keep the full scenario set in separate output directories.

CI also runs the browser regressions for the checker and actual scenario runner:

```bash
node --test tests/visual/checks-regression.cjs tests/visual/driver-regression.cjs tests/visual/offline-fixtures-regression.cjs
```

Startup/action errors and unmatched API fixtures become findings. Each console
event is consumed once; warnings do not gate. Intentional disabled
features must have an explicit fixture or scenario override.

The 13 new local workflows are covered by `offline/*` scenarios: feedback,
mortuary, CSSD, reporting, finance tools, radiology, care plans, emergency,
perioperative records, dialysis, record governance, radiation safety and
infection surveillance. The current manifest contains 78 states and 156 expected
captures across dark and light themes. These scenarios exercise actual forms,
record details, writes, revision/history displays, local CSV downloads and
small-cell suppression with explicit synthetic records. Each scenario receives
a fresh fixture store; an unhandled API request remains a gating failure.

```bash
node tests/visual/driver.cjs --filter=offline/ --out=tests/visual/offline-2026-10/run-name
node tests/visual/ci-gate.cjs --in=tests/visual/offline-2026-10/run-name
```

These mocked checks verify rendering and browser interactions. They do not
replace backend authorization, transaction, or clinical acceptance tests.

Runs write only to their selected output directory. Historical `shots/` and
`audit-results.json` evidence is left untouched. CI uploads the canonical `out/`
directory, including
the gallery, capture index, gate summary, and screenshots.

## Files

- `driver.cjs` — orchestrates roles/tabs/themes, captures, writes results
- `checks.cjs` — in-page audit (WCAG contrast with alpha compositing, overflow, etc.)
- `fixtures.cjs` — realistic demo API payloads keyed by URL path (`:param` wildcards)
- `checks-regression.cjs` — verifies hidden labels and genuine visible defects
- `driver-regression.cjs` — verifies console collection and unmatched-fixture gating
- `offline-scenarios.cjs` — interactive browser coverage for the 13 local workflows
- `offline-fixtures.cjs` — isolated synthetic records and explicit API contracts
- `offline-fixtures-regression.cjs` — verifies coverage, isolation and suppressed data
- `evidence/` — screenshots referenced by REPORT.md
