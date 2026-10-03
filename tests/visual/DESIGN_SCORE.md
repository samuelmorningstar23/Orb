# Orb — Design & UX Score

A harsh, evidence-based scoring of the rendered product across every visual
aspect, after the audit-fix-improve pass. Grounded in browser captures of every
screen in **both themes** and **all roles** (172 mocked captures + 40 live),
not on the code alone. Scores are the *current* state; where a dimension was
rescued from broken, the "before" is noted.

**Overall: 8.0 / 10 — B+.** A genuinely premium-looking clinical UI with a
disciplined token system and strong safety semantics, held back from an A by
mobile density, a few dataviz hue collisions, and accessibility long-tail
(sub-24px targets, faint disabled controls).

| # | Dimension | Score | One-line verdict |
|---|-----------|:---:|------------------|
| 1 | Color system & theming | **8.5** | Two complete, tokenized themes; a few accent-hue drifts remain |
| 2 | Typography & type scale | **8.5** | Confident 3-family system; scale is slightly top-crowded |
| 3 | Layout & spacing | **8.0** | Consistent 4px rhythm; occasional heading/card crowding |
| 4 | Visual hierarchy | **8.5** | Severity reads at a glance; strong KPI→attention→list flow |
| 5 | Component consistency | **8.0** | Pills/cards/tables coherent across 6 roles |
| 6 | Data visualization | **7.5** | Forecast chart is clean now; two near-blue series still collide |
| 7 | Responsive / mobile | **7.0** | Rescued from broken; still dense and FAB-collides |
| 8 | Accessibility | **8.0** | Focus ring + reduced-motion + contrast tokens; long tail remains |
| 9 | Empty / loading / error | **7.0** | Present and safe, but text-plain and under-designed |
| 10 | Motion & polish | **8.0** | Aurora + glass + stagger feel premium and are motion-safe |

---

## 1 — Color system & theming — 8.5

**Strong.** Two fully-specified themes live entirely in tokens: a warm
near-black gold theme (dark, default) and a Sierra-blue-on-paper theme (light).
Status is semantic (`--status-ok/warn/danger/info`) and used as fills, borders,
*and* text with contrast-aware deepening in light. The audit added the missing
**`--accent-text`** (deep accent for glyphs — the raw light-mode accent is a
pale blue that fails as text at ~2.4:1) and routed ~30 accent-colored glyph
sites through it. Contrast was verified with alpha **and gradient-stop**
compositing, so translucent glass and gradient buttons are measured as they
actually render.

**Rescued:** whole surfaces were dark-locked (Helix pharmacist module, patient
Bridge, command bar, Vigil modals, the off-brand blue DischargeAccess page) —
now theme-aware. The emergency patient row, previously clobbered in dark and
*lost entirely* in light by a `.glass-card` specificity trap, is now a distinct
red safety surface in both, with a scenario-level assertion guarding it.

**Still costs 1.5:** the Sage FAB orb stays **gold in light mode**, where the
whole accent system is Sierra blue — a visible brand-hue drift. In the Forecast
legend, *Occupancy* (line) and *Discharges* (bars) are both blue-ish; they're
separable by shape but not by a color-blind glance.

## 2 — Typography & type scale — 8.5

**Confident.** Three self-hosted variable families with clear jobs: **Inter**
(clinical body/UI, with contextual alternates `cv02/03/04/11` for a cleaner
single-storey read), **Space Grotesk** (distinctive display headings),
**JetBrains Mono** (IDs, timestamps, codes — e.g. `SG-201`, `May 23, 09:14 AM`).
The mono-for-data choice is the mark of someone who has read a real chart. Scale
is rem-based off a 16px root.

**Costs 1.5:** the top of the scale is tight (`--text-xl 1.6rem` →
`--text-2xl 2.1rem`) so page titles and big KPI numerals don't separate as much
as they could; a couple of section headings (`Accounts & Access`) sit close
enough to the cards above to read as crowding rather than grouping.

## 3 — Layout & spacing — 8.0

**Consistent.** A single shell grammar (sidebar rail + header + body) across
every role, a 4px spacing scale, and a repeated KPI-card → content pattern that
makes new screens instantly legible. Tables (admin Users, work queues) are
clean and column-aligned.

**Costs 2:** spacing is applied but not always *rhythmic* — the admin overview
heading crowds the KPI row; the Vigil detail pane previously left a large void
beside the sepsis card (now filled). Long tab bars (admin has 13) overflow into
a horizontal scroll with the last tab clipped and no gradient/affordance hint.

## 4 — Visual hierarchy — 8.5

**Strong.** The eye lands correctly: KPI trio (Admitted / Elevated / Critical,
the last two color-coded) → the amber "Sage needs your attention" block with
per-item severity stripes (red/amber/blue) → the patient list with a red
EMERGENCY banner and right-aligned risk %. Risk color runs a proper gradient
(red → amber → green → grey by acuity). Nothing important competes with
decoration.

**Costs 1.5:** within a KPI card the label and number are both present but the
weight jump could be bigger; some secondary metadata (`GM-102`, ward codes) sits
at a contrast that's legible but easy to lose.

## 5 — Component consistency — 8.0

**Good.** Pills (status, MFA on/off, EMERGENCY), cards, primary gold-gradient
buttons, outline secondaries, and data tables are the same components
everywhere, reskinned only by theme. The role picker, admit modal, and Sage
cards all share the same button and input grammar.

**Costs 2:** a few one-off treatments (the gold FAB, the discharge page's older
card styling) haven't fully converged on the shared kit; secondary buttons vary
subtly in padding between the shell and some panels.

## 6 — Data visualization — 7.5

**Adequate-plus.** The Forecast 7-day chart now reads correctly: a legend that
**matches the series** (it previously named the wrong colors and floated a
redundant box over the plot), a full-width plot, occupancy as a line, admissions
/ discharges as grouped bars, capacity as a dashed red ceiling. Risk gauges and
lab-trend arrows elsewhere are tokenized and clear.

**Costs 2.5:** the two blue-family series (occupancy line vs discharge bars)
are too close in hue; there's little chart variety (mostly bars/lines/gauges);
axis labels are quiet. This is the dimension with the most headroom.

## 7 — Responsive / mobile — 7.0 (was ~4)

**Rescued, not yet loved.** Phone Vigil now stacks cleanly: KPI cards go 2-up,
Sage cards stack with their severity stripes intact, and every risk % sits
**un-clipped on the right** (it was being cut off at the left edge before). The
biggest fix this round: the mobile top-bar nav was **completely broken** — the
vertical-rail `width:100%` was never reset for the horizontal bar, so the active
tab stretched to the full nav width, centered its icon *off-screen*, and pushed
the other ten tabs thousands of px past the scroll viewport. It rendered as an
empty gold-outlined box. It's now a proper 44px-icon scrollable strip in both
themes.

**Costs 3:** the Sage FAB still overlaps card content mid-scroll on narrow
widths; the 11-icon horizontal nav is cramped and has no scroll affordance; the
tablet breakpoint is functional but under-tuned.

## 8 — Accessibility — 8.0

**Strong foundation.** A global `:focus-visible` ring now exists on every
interactive element (there was **none** before — a WCAG 2.2 keyboard gap);
`prefers-reduced-motion` is honored down to the **canvas** aurora (RAF stopped,
static frame) and entrance-stagger delays (elements no longer sit invisible up
to 0.5s); contrast is tokenized to clear AA/AA-large where it counts;
disabled controls are handled per WCAG (reported, not gated).

**Costs 2:** a long tail of sub-24px touch targets (WCAG 2.2 AAA target size)
and AA-nicety contrast remains (tracked, non-gating); disabled primary buttons
(the admit submit, the discharge verify) sit at 1.66–2.45:1 — WCAG-exempt while
disabled, but faint enough that "is this button even here?" is a fair reaction.

## 9 — Empty / loading / error — 7.0

**Present and safe, under-designed.** Panels lazy-load behind a Suspense
fallback, tables have empty states, the forced-401 path shows a real
"session expired" overlay, and Relay templates now hide absent fields instead of
printing empty `Procedure:` / `OR:` labels. Nothing crashes or shows raw JSON.

**Costs 3:** the loading fallback is a plain `Loading panel...` string; empty
states are functional text, not designed moments; there's no skeleton shimmer to
match the otherwise-premium finish.

## 10 — Motion & polish — 8.0

**Premium.** A WebGL aurora + dot-grid backdrop, glass sheen/elevation tokens
(`--glass-sheen`, `--elev-1/2/3`) with light-mode counterparts, and staggered
entrance animations give the product a genuinely high-end feel — and, crucially,
all of it degrades under reduced-motion.

**Costs 2:** stagger timing is occasionally long enough to feel like latency on
slower first paints; hover/active micro-states are consistent but not
particularly characterful.

---

## What would move this to an A (≥9)

1. **Mobile pass 2** — reclaim the FAB from content, add a scroll affordance to
   the icon nav (or a "more" overflow), tune the tablet breakpoint.
2. **Dataviz hue separation** — pull Occupancy and Discharges onto distinct
   hues; add axis/label contrast; consider a color-blind-safe status ramp.
3. **Designed empty/loading states** — skeletons and illustrated empties instead
   of bare strings.
4. **Converge the last one-offs** — bring the Sage FAB and DischargeAccess onto
   the shared component kit and the light accent hue.
5. **Close the a11y long tail** — nudge sub-24px targets up; give disabled
   primaries a perceptible-but-clearly-inactive treatment.

## Method

Every score is backed by a real browser capture (`tests/visual/out/shots/`,
both themes) audited by `tests/visual/checks.cjs` and gated by
`tests/visual/ci-gate.cjs`. Gate state at time of scoring: **172 captures, 0
gating findings, green** (mocked) and **40 captures, 0 gating, green** (live,
real backend). The scores are deliberately harsh: green CI is the floor, not the
ceiling — it proves nothing is broken or illegible, while these scores judge
whether it's *good*.
