# Orb Visual Browser Audit — Light & Dark Mode

Automated Playwright audit of every screen (login, all clinician tabs per role, patient
bridge, discharge access, admit modal) in **both themes** at 1440×900, with a mocked API
so every panel renders populated. 30 captures, contrast/visibility/overflow checks per
capture, plus manual screenshot review. Harness: `tests/visual/driver.js` (see README).

Contrast ratios are WCAG relative-luminance ratios against the composited effective
background. Anything below ~3:1 is hard to read; below ~1.6:1 is effectively invisible.

---

## Critical — invisible or unreadable controls

### 1. Vigil patient names are white-on-white in light mode
The default clinician screen. `.patient-row__name` hard-codes `color: #FFFFFF`
(`src/pages/Vigil.css:52`), and emergency rows force `color: white !important` on name,
bed, diagnosis and score over a 20%-alpha red tint (`src/pages/Vigil.css:469-473`).
In light mode patient names, beds and the entire emergency row content disappear.
Evidence: `evidence/doctor-vigil-light.png` (rows at the bottom — "Rosa Martinez" and
"Margaret Chen" are invisible).
**Fix:** use `var(--text-primary)`; for emergency rows use a solid red background or a
theme-aware text token.

### 2. Admin panel stylesheet is missing entirely (both themes)
`AdminPanel.tsx` renders `admin-tabs`, `admin-tab`, `admin-stat-card__*`,
`admin-stat-chip` etc., but **no CSS file in the repo defines any of these classes**.
Consequences:
- Dark mode: the six tab buttons (Overview / Audit Logs / Trace / Ingestion / Security /
  Alerts) render with the UA default (near-black) text on the dark background —
  measured 1.06:1, i.e. an invisible nav (`evidence/admin-admin-dark.png`, top).
- Both modes: tabs and icons are mashed together with no padding, and stat rows
  concatenate without gaps: "ingestion: 1420auth: 312clinical: 110",
  "1388 parsed24 failed8 dupes", "Vigil: 14Relay: 6" (`evidence/admin-admin-light.png`).
**Fix:** add/restore an `Admin.css` (tabs bar, active state, chip spacing) and import it.

### 3. Helix (Pharmacy) panel ignores light mode
`src/components/helix/Helix.module.css` contains ~50 hard-coded white/near-white colors
and **zero** `[data-theme="light"]` overrides, plus hard-coded dark surfaces. In light
mode the whole Pharmacy panel renders as a dark island inside the light shell
(`evidence/pharmacist-helix-light.png`). Text stays readable only because the background
is equally hard-coded. Same pattern (hard-coded white text, no light overrides) exists in
`src/components/sage/Sage.css`, `src/components/surgery/*.module.css`,
`src/pages/PatientShell.css`.
**Fix:** replace hard-coded whites with `var(--text-*)` / `var(--bg-*)` tokens.

### 4. Admit Patient modal is muddy gray-on-gray in light mode
`src/components/AdmitPatientModal.css` has no light-theme overrides; the dark
`rgba(0,0,0,0.6)` overlay plus white-alpha input surfaces composite into gray fields with
low-contrast labels. Measured: tab labels ~2.1:1, Cancel 2.2:1, the **"Review Admission"
primary button 1.3:1 (invisible)**, select text 2.3:1
(`evidence/doctor-admit-modal-light.png`). Dark mode is fine.
Also (both themes): the tab strip clips the last tab ("Review" → "Revie") at 1440px.

### 5. Scribe voice-dictation button is invisible in light mode
`.scribe-record` hard-codes `color: #fff` on `background: rgba(255,255,255,0.05)`
(`src/pages/panels/ScribePanel.css:180-192`) — a white mic icon on a white composer,
measured 1.03:1 (`evidence/doctor-scribe-light.png`, bottom input bar). This is the
primary CTA of the Scribe panel.

### 6. Forecast view-switcher unreadable in light mode
`ForecastPanel.tsx:231` hard-codes inline colors for the section tabs:
inactive `#cbd5e1` on white → **1.37:1** ("Ward Capacity", "Discharge Board",
"Admission Patterns" are ghosts), active `#eab308` on a pale amber pill → **1.65:1**
(`evidence/doctor-forecast-light.png`). `ForecastPanel.tsx:499` uses the same `#cbd5e1`
for metric values. Additionally the chart legend row renders mashed together
("Occupancy Admissions Discharges|Capacity") and the boxed legend labels a bar "Amber
Bar" that renders slate-blue in light mode.

### 7. Surgical Suite "Schedule" toggle is dark-on-dark in dark mode
The active Schedule/Active-Cases toggle renders dark text on a dark gold-alpha gradient
(`src/pages/panels/SurgicalSuitePanel.css:821`), measured ~1.25:1
(`evidence/surgeon-surgical-suite-dark.png`, top-left). Light mode is fine.

---

## Medium

### 8. Theme preference is split across two different localStorage keys
`App.tsx:35` reads/watches **`orb-theme`** (hyphen); `ClinicianShell.tsx:177` reads/writes
**`orb_theme`** (underscore). Consequences observed in code:
- A system-prefers-light user gets a light login page, then the dashboard forces dark
  (shell defaults to `'dark'` when `orb_theme` is unset).
- A user who toggles the theme in the dashboard writes `orb_theme`, which the boot code
  never reads — after reload the app boots in the wrong theme, then flips.
- The system-theme-change listener (`App.tsx:54`) checks the never-written `orb-theme`
  key, so it overrides explicit user choices.
**Fix:** one key, one source of truth.

### 9. Discharge Access page ignores the theme entirely
`/discharge/:token` renders hard-coded light styles in both themes
(`src/pages/DischargeAccess.css`). Acceptable for a public patient page, but if
intentional, document it. Its disabled "Access Records" button is white-on-`#a0c4e8`
(1.8:1) — the disabled state is indistinguishable from an enabled washed-out button.

### 10. Sage hotword toggle is an invisible button
`.sage-fab__toggle` sits at opacity 0 (16×16 px, still in the tab/hit order) and is only
revealed on FAB hover (`src/components/sage/SageFloatingAssistant.css:307-328`). It is
present on every clinician screen. Hover-reveal is by design, but it's undiscoverable on
touch devices and invisible to the audit user until mouse-over.

### 11. Bridge "My Records" buttons lose their chrome in light mode
`src/pages/PatientShell.css` (zero theme overrides) gives the record buttons white-alpha
pill backgrounds/borders that vanish on white — four actions render as floating labels
with no button affordance in light mode.

### 12. Sidebar "Admit Patient" + active filter chips are washed out in light mode
Light-mode accent `#7AA5C7` text on near-white surfaces: "Admit Patient" 2.38:1 (every
clinician screen), active sort chips ("Stability"/"Recency") 2.26:1, Relay's active
"Recency" chip likewise. The light accent is decorative-grade, not text-grade.
**Fix:** use `--accent-deep` (#2C465E) for accent-colored text in light mode.

### 13. Login "Sign In" is white-on-gold in dark mode (2.68:1)
`--btn-primary-text: #000000` exists for exactly this, but `.login__submit` uses white
text on the gold gradient. Light mode (navy button) is fine.

---

## Verified good
- Login (both themes) — Aurora WebGL background correctly swaps gold↔Sierra-blue,
  card, inputs and quick-access chips all readable.
- Relay, Sage, Appointments, Lens — clean in both themes.
- Surgical Suite light, Patient Bridge dark, Vigil dark — clean.
- No horizontal page overflow anywhere; no zero-size interactive elements beyond the
  intentional hotword toggle.

## Known false positives (already filtered out above)
The checker composites `background-color` only, so buttons whose fill is a CSS
*gradient* (gold "Draft …" insight buttons, Sage FAB orb, suggestion cards, bridge week
cells) report their page-background contrast — visually verified fine and excluded.
