# Real browser UI/UX assessment — 12 October 2026

Three reproducible layout/focus defects were found. A fourth observation is an authenticated mobile account-security discoverability gap; mandatory enrollment at login remains available. No application fixes were made in this assessment.

This evidence uses real login and API responses from a disposable synthetic Orb backend and the production bundle. There are no API mocks. Model inference points to the closed loopback port 9; browser requests to external origins and accidental generation submissions are blocked, with zero such attempts recorded. No shared Ollama endpoint or live clinical database was used.

## Verified findings

| ID | Priority | Observation | Evidence |
| --- | --- | --- | --- |
| UX-01 | P1 | Phone admission retains a 280px summary and collapses form content to 12px, painting over fields/tabs. Close target shrinks to 19.406×32px. | [Mobile admission](focused-final/screenshots/036-390-light-admission-overlap-viewport.png), [dark](focused-final/screenshots/045-390-dark-admission-overlap-viewport.png); focused-final/results.json layout arrays. |
| UX-02 | P2 | Radiation-safety form is 664.781px wide inside 366px content; actual sideways pan is 299px in both themes. Medical records fits the same viewport. | [Before pan](registers-verified/screenshots/002-390-light-radiation-safety-viewport.png), [after pan](registers-verified/screenshots/003-390-light-radiation-safety-horizontal-pan-end-viewport.png); registers-verified/results.json. |
| UX-03 | P2 | Escape closes admission but restores focus to BODY rather than its trigger, at all three sizes and both themes. | focused-final/results.json `focus`: 6/6 observations. Screenshots alone cannot establish this focus transition. |
| UX-04 | P2 | Authenticated Account security/MFA enrollment shortcut exists but is hidden at 390px; no alternate authenticated shell activation found. | focused-final/results.json `security`; mandatory pre-login enrollment remains in Login.tsx:513. This is not an MFA enforcement bypass. |

Source anchors, excerpts and file hashes are in [provenance.json](provenance.json). The responsive admission body/summary are `AdmitPatientModal.css:87` and `:328`; radiation uses `RecordGovernancePanel.css:1–5`. Admission's `autoFocus` is `AdmitPatientModal.tsx:795`; `useFocusTrap.ts:91` captures focus after mount and `:147` restores it. The authenticated security button is `ClinicianShell.tsx:546`, hidden with its card at `ClinicianShell.css:1082`.

Admission trial clicks can reach a tiny exposed edge. The finding is the visibly collapsed/obstructed form, not a claim that every possible pointer click is rejected. Radiation's document width remains 390px: the overflow occurs in the inner content container and needs screenshot/geometry review beyond document-wide overflow checks.

The floating Sage orb also overlaps a mobile radiation pagination label and some scrolling content. The seeded next-page control is disabled, so an obstructed live pagination action is not proven. Automatic contrast candidates remain information in the raw records rather than new confirmed security/UI blockers.

## Scope and evidence integrity

Chromium 149.0.7827.55, device scale factor 1. Desktop 1440×1000, compact desktop 1280×800 and phone 390×844; light/dark. Phone contexts enable touch. Real doctor/admin/nurse logins use the isolated seed accounts. Tests cover core navigation, patient chart, admission, shortcuts, account-security dialog on desktop, role-gated nurse navigation, real theme switching, clinical patient selection/new-record forms, the 13 new offline modules, report preview and a persisted long feedback record.

The final focused runs contain 58 states / 116 PNGs with zero scenario errors. A few initial focused register screenshots captured loading; the eight settled `registers-verified` states supersede those register observations. The broad run and failed setup attempts remain intact: total 262 states / 366 PNGs. This is not a claim that every UI state passed or that all possible workflows were exercised.

| Run | States | PNGs | Recorded errors | Classification |
| --- | ---: | ---: | ---: | --- |
| broad | 158 | 158 | 20 | 16 new-record attempts omitted required patient selection (corrected later); 2 disabled ward-record navigation attempts; 2 real hidden-mobile-MFA observations. |
| followup-initial | 36 | 72 | 6 | Superseded harness login setup: switching to admin needed explicit logout. Doctor forms/focus evidence retained. |
| focused-final | 50 | 100 | 0 | Final corrected doctor/admin interaction and geometry checks; settled registers below supersede loading captures. |
| registers-initial | 4 | 8 | 2 | Superseded harness selector matched hidden and visible retained headings. |
| registers-settled | 6 | 12 | 2 | Superseded pan selector matched hidden and visible retained containers. |
| registers-verified | 8 | 16 | 0 | Final visible, settled register and actual-pan proof. |

No browser console/page errors, external browser requests or model-generation attempts were recorded. Raw geometry includes intentional scroll rails and controls underneath open modal backdrops; these are not automatically promoted to defects. Existing visual checker findings are retained, including information-level contrast/small-target results. The assessment found real issues despite having no automatic error-level checker findings.

Two real persisted feedback statements containing synthetic `<img ... onerror=...>` text rendered as plain text, created no image element and did not execute in either theme. This establishes that single rendering path only. Cookie evidence contains names and flags, not values. Local HTTP session cookies are HttpOnly/SameSite=Lax, and the readable CSRF cookie is expected. Browser storage keys contain no bearer-token key in this login path; no storage values are retained.

## Reproduction

Run from the repository root with installed dependencies and the approved local browser/runtime. These scripts create and clean up their own disposable backend; they never attach to an existing listener. Use a fresh output directory.

```sh
NODE=node
PYTHON=/private/tmp/orb-ml-upgrade-20261012/bin/python
"$NODE" scripts/browser_ux_assessment.cjs --python="$PYTHON" --port=18122 --out=/private/tmp/orb-browser-ux-new
"$NODE" scripts/browser_ux_followup.cjs --python="$PYTHON" --port=18122 --out=/private/tmp/orb-browser-ux-focused-new
"$NODE" scripts/browser_ux_followup.cjs --python="$PYTHON" --port=18122 --register-only --out=/private/tmp/orb-browser-ux-registers-new
```

The scripts invoke `scripts/e2e_server.py --offline-modules` with an owned source/DB/HOME. The production index SHA256 for every recorded run is `c7a759ed29e706bb547eae838157a4de3919393fd8c88d521d13054d2a7577d2`. The captured backend source manifest contains 445 files and matched current sources at packaging. The original broad and followup harnesses are retained and their hashes match their raw results. Current scripts include subsequent setup/selector corrections; earlier runs have not been relabeled as produced by the newer files. Both new scripts pass Node syntax checks and ESLint.

All six browser runs awaited browser/context closure and backend termination. Each backend log confirms ASGI shutdown completed. Supervisor exit 130 records the intentional termination, not an application failure. Final independent port check returned connection-refused 61 for owned port 18122; see [cleanup-final.json](cleanup-final.json).

[summary.json](summary.json) records classifications/counts; [screenshots.json](screenshots.json) maps every PNG to viewport, theme, steps, dimensions and SHA256; [sha256-manifest.json](sha256-manifest.json) hashes every packaged artifact except itself. All raw screenshots, results, original harnesses, backend logs and console logs remain in their run subdirectories. No DB, upload, temp HOME, credential key or model file is packaged.
