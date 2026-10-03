# Orb: Design System ("Quiet Clinical")

The visual language of Orb: a calm, **flat, warm-neutral** system tuned for
legibility at speed in clinical work. Opaque paper surfaces, hairline borders,
one clay accent used sparingly, semantic status colour is the only thing on
screen allowed to shout. This document is the source of truth for tokens,
theming, motion, data-viz, and accessibility. Every surface consumes these
tokens; every new surface must pass the visual gate
(`tests/visual/driver.cjs` + `ci-gate.cjs`) in **all three themes**.

> The previous "gold liquid-glass" system (frosted blur, gradients, glows,
> hover lifts) was retired in the 2026-07-30 overhaul. If you see any of it in
> a diff, backdrop-filter, decorative gradients, colored glow shadows,
> translateY hovers, it is design debt, not a style option.

## 1. Themes

Themes are switched by a `data-theme` attribute on `<html>` (persisted to
`localStorage['orb-theme']`, broadcast via the `theme-changed` CustomEvent for
canvas/chart consumers). The shell's header control **cycles** them:

| Theme | `data-theme` | Use | Character |
|---|---|---|---|
| **Dark** (default) | `dark` | Standard low-light clinical setting | Warm charcoal paper (#262624 family), clay accent |
| **Light** | `light` | Bright rooms, day shift | Warm ivory paper (#FAF9F5 family), deeper clay accent |
| **Night Shift / OR** | `night` | Dim wards, night rounds, the OR | True-black, AAA-leaning text, **warm amber accent (low blue light)**, brighter semantic colours |

Because every surface is an opaque token (`--bg-*`), themes flip **entirely at
the token layer**, component stylesheets should not need `[data-theme]`
overrides except for true semantic differences (e.g. night's ink-on-amber
button label via `--btn-primary-text`).

## 2. Tokens (`src/index.css`)

The important families:

- **Backgrounds**, `--bg-primary` (canvas) / `--bg-secondary` (sidebar rail)
  / `--bg-surface` (cards) / `--bg-elevated` (modals, popovers).
  `--bg-glass` / `--bg-glass-hover` are **flat translucent fills** for quiet
  hover/selected states (the "glass" name is legacy, there is no blur).
- **Text**, `--text-primary/secondary/muted`. All three clear WCAG AA (≥4.5:1)
  on canvas, surface AND elevated in every theme, verified numerically.
- **Accent**, terracotta clay. `--accent-gold` (legacy name) is the fill
  accent; `--accent-text` is the value that is safe **as text** (≥4.5:1);
  `--accent-deep` is the button fill paired with `--btn-primary-text`
  (≥4.5:1 in rest and hover states, all themes). Use the accent sparingly:
  primary buttons, active nav, focus, small highlights. Never for status.
- **Semantic status**, `--status-ok / --status-warn / --status-danger /
  --status-info`, distinct from the accent, text-safe on canvas and surface.
  These are the ONLY status palette, no Tailwind hexes. Tinted chips use
  `color-mix(in srgb, var(--status-X) 10%, transparent)` fills with 28%
  borders. Severity everywhere maps: ok→green, warn→amber, danger→red.
- **Borders**, `--border-subtle` (default hairline) / `--border-glass`
  (stronger, for elevated layers and inputs) / `--border-focus` (accent-tinted).
  Borders define surfaces; shadows are reserved for ephemeral layers.
- **Focus**, `--focus-ring`, consumed by the global `:focus-visible` rule.
- **Radii**, one scale: `--radius-sm` 8 (buttons, chips, inputs) /
  `--radius-md` 12 (small cards, popovers) / `--radius-lg` 16 (cards, composer)
  / `--radius-xl` 20 (modals) / `--radius-full` (pills).
- **Layout constants**, `--sidebar-w`, `--sidebar-w-rail`, `--topbar-h`,
  `--page-x`, `--page-y`: every page aligns to these; none invents its own.
- **Spacing**, `--space-1..8` on a 4px grid.
- **Type**, `--font-family` (Inter, all UI), `--font-display` (Fraunces, 
  brand, page titles and hero moments ONLY, via `.font-display` or explicit
  rules), `--font-mono` (JetBrains Mono, IDs, doses, timestamps, codes).
  Scale: `--text-xs..2xl`.
- **Elevation**, `--elev-1..3`. `--elev-3` belongs to modals/popovers/menus;
  cards get borders, not shadows.
- **Motion**, `--transition-fast/base/slow`.
- **Legacy aliases**, `--glass-sheen`, `--glass-highlight`, `--btn-gradient`,
  `--blur-*` etc. resolve to flat equivalents so stale references degrade
  safely. Do not use them in new code.

Prefer tokens over literals everywhere, including TSX inline styles and SVG
stroke/fill (`var(--status-danger)` resolves in presentation attributes).

## 3. Surface & control recipes

- **Card**: `background: var(--bg-surface); border: 1px solid
  var(--border-subtle); border-radius: var(--radius-lg)`. Interactive cards
  shift `border-color`/background on hover, nothing moves.
- **Overlay** (modal/popover/menu): `var(--bg-elevated)` + `--border-glass` +
  `box-shadow: var(--elev-3)`.
- **Primary button**: `background: var(--accent-deep); color:
  var(--btn-primary-text); border-radius: var(--radius-sm)`; hover
  `--btn-gradient-hover`. Secondary: surface + `--border-glass`. Ghost:
  transparent, `--text-secondary`, hover `--bg-glass`.
- **Hover** = background/border shift only. No translateY, no scale, no glow.

## 4. Accessibility

- **Keyboard focus**, one global convention: `:focus-visible` draws a 2px
  `--focus-ring` outline with 2px offset on every focusable element
  (`src/index.css`, `!important` so component resets can't erase it).
  Composite fields (Sage composer, Scribe input, Relay input, Admit fields)
  suppress the inner ring because their wrapper shows `:focus-within`.
- **Modals**, `role="dialog"` + `aria-modal` + labelled title + the shared
  `useFocusTrap` (initial focus, Tab cycle, Escape, focus restore).
- **Live regions**, presence chips, typing indicators, offline/sync status,
  and safety alerts announce via `role="status"`/`role="alert"` + `aria-live`.
- **Contrast**, every text token was chosen numerically: ≥4.5:1 on every
  surface it sits on, in every theme; UI fills/rings ≥3:1. The night theme is
  deliberately AAA-leaning. The visual gate flags contrast per capture in every
  theme; keep it green.
- **Reduced motion**, a global `prefers-reduced-motion` kill switch zeroes
  animations/transitions (including stagger delays).
- **Colour is never the only signal**, severity is also encoded in text
  (SEVERE/MODERATE chips), shape (severity stripes, NEWS2 number), and icons.

## 5. Data visualization

- **Semantic mapping**, chart series reuse the status tokens; risk/acuity
  colours match the rest of the app (green/amber/red), never the accent.
- **Tabular figures**, numbers that carry clinical meaning use
  `font-variant-numeric: tabular-nums lining-nums` (global rule on
  `.tabular-nums`, stat/metric/score classes, `td/th/input`).
- **Emphasised endpoints**, trend sparklines mark the latest point larger and
  in the status colour; reference bands are drawn as low-alpha fills.
- **Estimated vs measured**, model-estimated trends render **dashed** with an
  "Estimated" badge. NEWS2 is shown as the raw aggregate + band, never a "%".
- **Theme-aware charts**, the chart kit (`src/components/charts/useChartTheme.ts`)
  resolves `--chart-axis/grid/tick/tooltip-*` off the document root and
  re-reads them on every theme flip, feeding resolved colours to recharts.
  Hand-rolled SVG can use `var(--chart-*)` / `var(--border-subtle)` directly.
  Every theme defines the full `--chart-*` set.
- **Gradients** are permitted ONLY when they encode data (progress fills,
  severity ramps) or a functional cue (scroll-fade truncation mask), never as
  surface decoration. The one sanctioned decorative gradient is the OrbLogo
  core (brand mark).

## 6. Motion

Restrained, purposeful. `--transition-fast` (140ms) for hovers/toggles,
`--transition-base` (220ms) for surface changes, `--transition-slow` (360ms)
for larger moves. Entrances (`animate-fade-in`, `animate-slide-up` with
`stagger-*`) are subtle (≤8px travel). Keyframes that signal live state
(recording pulse, offline dot, streaming caret) are meaningful and stay.
Motion signals state change; it never decorates.

## 7. Contributing

1. Consume tokens; never hardcode colour/spacing/radii, in CSS or TSX.
2. Keep semantic status distinct from the accent.
3. Every focusable element keeps a visible keyboard focus.
4. No backdrop-filter, no decorative gradients, no glow shadows, no hover
   transforms. Borders define surfaces; `--elev-3` is for ephemeral layers.
5. The display serif (Fraunces) is reserved for brand, page titles and hero
   moments, dense clinical UI stays in Inter.
6. Every new surface must pass the visual gate in **dark, light, and night**.
