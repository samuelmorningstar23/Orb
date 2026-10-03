# Orb Visual Browser Tests

Playwright-driven visual audit of every Orb screen in **light and dark** mode.
No backend needed — all `/api/*` calls are fulfilled from `fixtures.cjs`, and auth is
bypassed by seeding `localStorage` (`orb_user` / `orb_token`) per role.

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
3. Screenshots to `shots/<name>-<theme>.png` and writes `audit-results.json`.

## Running

```bash
npm run dev &                          # vite on 127.0.0.1:5173
cd tests/visual
npm init -y && npm i playwright       # or reuse a repo-level install
node driver.cjs
```

If Playwright's managed browser isn't downloaded, point `executablePath` in
`driver.cjs` at any local Chromium.

## Files

- `driver.cjs` — orchestrates roles/tabs/themes, captures, writes results
- `checks.cjs` — in-page audit (WCAG contrast with alpha compositing, overflow, etc.)
- `fixtures.cjs` — realistic demo API payloads keyed by URL path (`:param` wildcards)
- `evidence/` — screenshots referenced by REPORT.md
